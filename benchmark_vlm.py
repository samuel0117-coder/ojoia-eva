#!/usr/bin/env python3
"""
benchmark_vlm.py — C1 (Fase C): benchmark de modelos VLM para vigilancia.

Replaya N eventos REALES del libro (grid.jpg guardado + frases del dueño de
camera.json) contra los 3 modelos disponibles y puntúa automáticamente:

  - flag_exacto (0-3): 3 = flag es frase EXACTA de una regla del dueño;
    2 = fuzzy match; 1 = flag no-regla pero acción plausible; 0 = inventó
    regla o placeholder.
  - sin_fotograma (0-1): la narrativa NO menciona fotograma/frame/cuadro.
  - json_valido (0-1): salida parsea como JSON limpio.
  - causalidad (0-1): la narrativa usa conectores de secuencia (luego,
    después, entonces, tras) — mide que cuenta la historia como película.
  - latencia_s: tiempo de respuesta.

Uso:
    /opt/ojoia/venv/bin/python benchmark_vlm.py --user <uid> --camera <cam> [--n 6]
"""
import argparse
import asyncio
import base64
import glob
import json
import os
import re
import sys
import time
import unicodedata

sys.path.insert(0, "/opt/ojoia/code")

STORAGE = "/home/sam/storage/users"

MODELS = [
    {"name": "qwen25vl7b", "url": "http://localhost:8004", "model": "/models/7b", "port": 8004},
    {"name": "qwen3vl8b", "url": "http://localhost:8019", "model": "/models/Qwen3VL-8B-Instruct-Q4_K_M.gguf", "port": 8019},
    {"name": "qwen38_27b", "url": "http://localhost:18020", "model": "qwen3.8-27b", "port": 18020},
]


def _norm(s):
    s = str(s or "").lower().strip()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def build_prompt(phrases, zone, biz_type):
    ap = "\n".join(f"- {p}" for p in phrases[:8]) or "- (sin reglas específicas: describe la actividad)"
    return (
        f"Eres un testigo de seguridad observando UN VIDEO CORTO de 16 fotogramas consecutivos de una cámara de vigilancia.\n"
        f"Te muestro el video como 4 imágenes: cada una es una CUADRÍCULA 2×2 con 4 fotogramas numerados en amarillo con numeración continua (panel 1 = fotogramas 1-4, panel 2 = 5-8, etc.).\n"
        f"Describe SOLO lo que ves, como testigo neutral. No juzgas, no inventas, no supones.\n"
        f"PROHIBIDO mencionar fotogramas, frames, cuadrículas o números en tu narrativa. Cuenta la secuencia como UNA SOLA HISTORIA continua en pasado narrativo.\n"
        f"ENFOCA la narrativa en las ACCIONES y su ORDEN: quién hizo qué, después de qué.\n"
        f"CONTEXTO: zona \"{zone}\" en un {biz_type or 'negocio'}.\n"
        f"EL PROPIETARIO QUIERE VIGILAR:\n{ap}\n\n"
        f"Responde EXCLUSIVAMENTE con un JSON válido (sin markdown, sin ```):\n"
        "{\"scene\": \"narrativa 3-6 frases\", \"persons\": [{\"id\": 0, \"desc\": \"...\", \"zone\": \"...\"|null}], "
        "\"events\": [\"acciones con tus palabras\"], \"flag\": null}\n"
        "Reglas críticas:\n"
        "  - \"flag\": SOLO si una de las reglas del dueño se cumplió VISUALMENTE, copia la frase EXACTA (letra por letra). Si no, null. NO inventes frases, NO copies ejemplos de estas instrucciones.\n"
        "  - Responde SIEMPRE EN ESPAÑOL.\n"
    )


def score_response(content, phrases):
    """Puntúa una respuesta contra las reglas del dueño."""
    s = {}
    raw = (content or "").strip()
    raw = re.sub(r"^```(json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    # parse JSON (buscar el bloque {...} más externo)
    parsed = None
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(m.group()) if m else None
    except Exception:
        parsed = None
    s["json_valido"] = 1 if parsed else 0

    # flag
    flag_val = ""
    if parsed:
        flag_val = str((parsed.get("vision") or parsed).get("flag") or parsed.get("flag") or "" if isinstance(parsed, dict) else "")
        if not flag_val and isinstance(parsed.get("vision"), dict):
            flag_val = str(parsed["vision"].get("flag") or "")
    scene = ""
    if parsed:
        scene = str((parsed.get("vision") or parsed).get("scene") or parsed.get("scene") or "")
    else:
        scene = raw[:400]

    fn = _norm(flag_val)
    if not fn or fn == "null":
        s["flag_score"] = 3  # null honesto: no inventa
        s["flag"] = ""
    else:
        exact = any(_norm(p) == fn for p in phrases)
        if exact:
            s["flag_score"] = 3
        elif any(_norm(p) in fn or fn in _norm(p) for p in phrases):
            s["flag_score"] = 2
        elif any(w in fn for w in ("frase", "attention_phrases", "instrucc")):
            s["flag_score"] = 0  # copió el schema
        else:
            s["flag_score"] = 1  # acción plausible no-regla
        s["flag"] = flag_val[:80]

    sl = scene.lower()
    s["sin_fotograma"] = 0 if re.search(r"fotograma|frame|cuadricul|cuadrícula|panel \d", sl) else 1
    s["causalidad"] = 1 if re.search(r"luego|despu[eé]s|entonces|tras |a continuaci|mientras", sl) else 0
    s["scene_excerpt"] = scene[:120]
    return s


async def call_model(client, m, prompt, grid_b64):
    t0 = time.time()
    try:
        r = await client.post(
            f"{m['url']}/v1/chat/completions",
            json={
                "model": m["model"],
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{grid_b64}"}},
                    {"type": "text", "text": prompt}]}],
                "max_tokens": 700, "temperature": 0.2,
            },
            timeout=240,
        )
        dt = time.time() - t0
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "latency": dt}
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content")
        # C2: Qwen3.8 thinking devuelve content=null; la respuesta va en reasoning
        if not content and msg.get("reasoning"):
            content = msg.get("reasoning")
        return {"content": content, "latency": dt}
    except Exception as e:
        return {"error": str(e), "latency": time.time() - t0}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--camera", required=True)
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--models", default="")  # csv subset
    args = ap.parse_args()

    cam_dir = f"{STORAGE}/{args.user}/cameras/{args.camera}"
    cam_cfg = json.load(open(f"{cam_dir}/camera.json"))
    phrases = (cam_cfg.get("vigilance") or {}).get("attention_phrases") or cam_cfg.get("attention_phrases") or []
    zone = cam_cfg.get("zone", "")
    biz = cam_cfg.get("business_type", "")
    print(f"Reglas del dueño: {phrases}")
    print(f"Zona: {zone} | Tipo: {biz}\n")

    # eventos recientes CON grid.jpg
    events = sorted(glob.glob(f"{cam_dir}/events/evt_*/grid.jpg"), key=os.path.getmtime)[-args.n:]
    print(f"Replay de {len(events)} eventos reales\n")

    models = MODELS
    if args.models:
        keep = set(args.models.split(","))
        models = [m for m in MODELS if m["name"] in keep]

    import httpx
    results = {m["name"]: {"scores": [], "latencies": [], "errors": 0, "details": []} for m in models}

    async with httpx.AsyncClient() as client:
        for gp in events:
            ev_id = gp.split("/")[-2]
            grid_b64 = base64.b64encode(open(gp, "rb").read()).decode()
            prompt = build_prompt(phrases, zone, biz)
            print(f"── {ev_id}")
            for m in models:
                r = await call_model(client, m, prompt, grid_b64)
                if "error" in r:
                    results[m["name"]]["errors"] += 1
                    print(f"   {m['name']}: ERROR {r['error']}")
                    continue
                sc = score_response(r["content"], phrases)
                results[m["name"]]["scores"].append(sc)
                results[m["name"]]["latencies"].append(r["latency"])
                results[m["name"]]["details"].append({"event": ev_id, **sc, "latency": round(r["latency"], 1)})
                flag_txt = f"flag={sc.get('flag', '')!r}" if sc.get("flag") else "flag=null"
                print(f"   {m['name']}: flag={sc['flag_score']} json={sc['json_valido']} noFrame={sc['sin_fotograma']} causal={sc['causalidad']} {r['latency']:.1f}s {flag_txt}")

    print("\n════════ RESUMEN ════════")
    print(f"{'modelo':<12} {'flag/3':>7} {'json':>5} {'noFrame':>8} {'causal':>7} {'lat_s':>6} {'err':>4}")
    for m in models:
        rs = results[m["name"]]
        ss = rs["scores"]
        if not ss:
            print(f"{m['name']:<12} (sin resultados, {rs['errors']} errores)")
            continue
        avg = lambda k: sum(s[k] for s in ss) / len(ss)
        lat = sum(rs["latencies"]) / len(rs["latencies"])
        print(f"{m['name']:<12} {avg('flag_score'):>7.2f} {avg('json_valido'):>5.2f} {avg('sin_fotograma'):>8.2f} {avg('causalidad'):>7.2f} {lat:>6.1f} {rs['errors']:>4}")

    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "camera": args.camera,
           "phrases": phrases, "results": {k: {"summary": {
                "n": len(v["scores"]),
                "avg_flag": (sum(s["flag_score"] for s in v["scores"]) / len(v["scores"])) if v["scores"] else None,
                "avg_json": (sum(s["json_valido"] for s in v["scores"]) / len(v["scores"])) if v["scores"] else None,
                "avg_no_frame": (sum(s["sin_fotograma"] for s in v["scores"]) / len(v["scores"])) if v["scores"] else None,
                "avg_causal": (sum(s["causalidad"] for s in v["scores"]) / len(v["scores"])) if v["scores"] else None,
                "avg_latency": (sum(v["latencies"]) / len(v["latencies"])) if v["latencies"] else None,
                "errors": v["errors"],
            }, "details": v["details"]} for k, v in results.items()}}
    outp = f"/opt/ojoia/code/docs/benchmark_vlm_{int(time.time())}.json"
    with open(outp, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(f"\nDetalle → {outp}")


if __name__ == "__main__":
    asyncio.run(main())
