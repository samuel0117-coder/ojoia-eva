#!/usr/bin/env python3
"""Project Server - Centro de Mando Audiovisual
Gestiona proyectos, escenas, contexto y archivos generados.
"""
import os, json, time, shutil, uuid
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List

BASE_DIR = Path("/home/sam/projects")
TEMPLATES_DIR = BASE_DIR / ".templates"

app = FastAPI(title="Project Server", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Helpers ──

def project_dir(pid: str) -> Path:
    return BASE_DIR / pid

def ensure_project(pid: str) -> Path:
    d = project_dir(pid)
    if not d.exists():
        raise HTTPException(404, f"Project {pid} not found")
    return d

def read_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}

def write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def new_project_id(name: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower())
    return f"{slug}_{uuid.uuid4().hex[:6]}"

# ── Project CRUD ──

class CreateProjectReq(BaseModel):
    name: str
    description: str = ""
    style: str = "cinematic, warm characters"

@app.post("/api/projects/create")
async def create_project(req: CreateProjectReq):
    pid = new_project_id(req.name)
    pd = project_dir(pid)
    pd.mkdir(parents=True, exist_ok=True)
    
    # Create subdirectories
    for sub in ["scenes", "images", "images/upscaled", "audio/tts", "audio/music", "audio/voice_clones", "exports/final", "exports/previews", "logs"]:
        (pd / sub).mkdir(parents=True, exist_ok=True)
    
    # Config
    config = read_json(TEMPLATES_DIR / "config.json")
    config["created"] = datetime.now().isoformat()
    config["last_modified"] = config["created"]
    write_json(pd / "config.json", config)
    
    # Context
    context = read_json(TEMPLATES_DIR / "context.json")
    context["project_name"] = req.name
    context["description"] = req.description
    context["style"] = req.style
    write_json(pd / "context.json", context)
    
    return {"project_id": pid, "name": req.name, "path": str(pd)}

@app.get("/api/projects/list")
async def list_projects():
    projects = []
    for d in sorted(BASE_DIR.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_"):
            ctx = read_json(d / "context.json")
            cfg = read_json(d / "config.json")
            scenes_dir = d / "scenes"
            scene_count = len(list(scenes_dir.iterdir())) if scenes_dir.exists() else 0
            projects.append({
                "id": d.name,
                "name": ctx.get("project_name", d.name),
                "description": ctx.get("description", ""),
                "style": ctx.get("style", ""),
                "scenes": scene_count,
                "created": cfg.get("created", ""),
            })
    return projects

@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    pd = ensure_project(pid)
    return {
        "config": read_json(pd / "config.json"),
        "context": read_json(pd / "context.json"),
    }

@app.post("/api/projects/{pid}/context")
async def update_context(pid: str, ctx: dict):
    pd = ensure_project(pid)
    context = read_json(pd / "context.json")
    context.update(ctx)
    context["last_modified"] = datetime.now().isoformat()
    write_json(pd / "context.json", context)
    return {"status": "ok"}

@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    pd = ensure_project(pid)
    shutil.rmtree(pd)
    return {"status": "ok"}

# ── Scene CRUD ──

class CreateSceneReq(BaseModel):
    prompt: str
    negative_prompt: str = "blur, low quality, static"
    model: str = "wan_i2v"
    width: int = 320
    height: int = 320
    frames: int = 5
    steps: int = 6
    cfg: float = 6.0
    seed: int = 0
    source_image: str = ""

@app.post("/api/projects/{pid}/scenes/create")
async def create_scene(pid: str, req: CreateSceneReq):
    pd = ensure_project(pid)
    scenes_dir = pd / "scenes"
    scenes_dir.mkdir(exist_ok=True)
    
    # Find next scene number
    existing = sorted(scenes_dir.iterdir())
    scene_num = len(existing) + 1
    scene_id = f"scene_{scene_num:03d}"
    scene_dir = scenes_dir / scene_id
    scene_dir.mkdir(exist_ok=True)
    
    scene = read_json(TEMPLATES_DIR / "scene.json")
    scene["id"] = scene_num
    scene["prompt"] = req.prompt
    scene["negative_prompt"] = req.negative_prompt
    scene["model"] = req.model
    scene["resolution"] = f"{req.width}x{req.height}"
    scene["frames"] = req.frames
    scene["steps"] = req.steps
    scene["cfg"] = req.cfg
    scene["seed"] = req.seed if req.seed > 0 else int(time.time() * 1000) % 2**31
    scene["source_image"] = req.source_image
    scene["status"] = "pending"
    scene["created"] = datetime.now().isoformat()
    write_json(scene_dir / "metadata.json", scene)
    
    # Update context
    context = read_json(pd / "context.json")
    context.setdefault("scenes", []).append({
        "id": scene_num,
        "prompt": req.prompt,
        "status": "pending",
        "dir": scene_id,
    })
    write_json(pd / "context.json", context)
    
    return {"scene_id": scene_id, "scene_num": scene_num, "seed": scene["seed"]}

@app.get("/api/projects/{pid}/scenes")
async def list_scenes(pid: str):
    pd = ensure_project(pid)
    scenes_dir = pd / "scenes"
    if not scenes_dir.exists():
        return []
    scenes = []
    for d in sorted(scenes_dir.iterdir()):
        if d.is_dir():
            meta = read_json(d / "metadata.json")
            scenes.append(meta)
    return scenes

@app.get("/api/projects/{pid}/scenes/{scene_id}")
async def get_scene(pid: str, scene_id: str):
    pd = ensure_project(pid)
    scene_dir = pd / "scenes" / scene_id
    if not scene_dir.exists():
        raise HTTPException(404, "Scene not found")
    return read_json(scene_dir / "metadata.json")

@app.post("/api/projects/{pid}/scenes/{scene_id}/update")
async def update_scene(pid: str, scene_id: str, data: dict):
    pd = ensure_project(pid)
    scene_dir = pd / "scenes" / scene_id
    if not scene_dir.exists():
        raise HTTPException(404, "Scene not found")
    meta = read_json(scene_dir / "metadata.json")
    meta.update(data)
    write_json(scene_dir / "metadata.json", meta)
    return {"status": "ok"}

# ── File Management ──

@app.get("/api/projects/{pid}/files/{category}")
async def list_files(pid: str, category: str):
    pd = ensure_project(pid)
    cat_dir = pd / category
    if not cat_dir.exists():
        return []
    files = []
    for f in sorted(cat_dir.rglob("*")):
        if f.is_file():
            files.append({
                "name": f.name,
                "path": str(f.relative_to(pd)),
                "size": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return files

@app.get("/api/projects/{pid}/download/{file_path:path}")
async def download_file(pid: str, file_path: str):
    pd = ensure_project(pid)
    fp = pd / file_path
    if not fp.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(fp))

@app.post("/api/projects/{pid}/upload/{category}")
async def upload_file(pid: str, category: str, file: UploadFile = File(default=...)):
    pd = ensure_project(pid)
    cat_dir = pd / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    dest = cat_dir / file.filename
    with open(dest, "wb") as f:
        f.write(file.file.read())
    return {"status": "ok", "path": str(dest.relative_to(pd))}

# ── Generation Log ──

@app.post("/api/projects/{pid}/log")
async def add_log(pid: str, entry: dict):
    pd = ensure_project(pid)
    log_file = pd / "logs" / "generation_log.jsonl"
    log_file.parent.mkdir(exist_ok=True)
    entry["timestamp"] = datetime.now().isoformat()
    with open(log_file, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {"status": "ok"}

@app.get("/api/projects/{pid}/log")
async def get_log(pid: str, limit: int = Query(50, ge=1, le=200)):
    pd = ensure_project(pid)
    log_file = pd / "logs" / "generation_log.jsonl"
    if not log_file.exists():
        return []
    lines = log_file.read_text().strip().split("\n")
    return [json.loads(l) for l in lines[-limit:]]

# ── Health ──

@app.get("/api/health")
async def health():
    return {"status": "ok", "server": "project-server", "version": "1.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8012, log_level="warning")
