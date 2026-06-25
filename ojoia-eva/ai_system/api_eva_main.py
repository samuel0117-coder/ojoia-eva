#!/usr/bin/env python3
"""API Principal Eva - Entry point unificado para todos los servicios"""
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
import httpx, io, base64
from PIL import Image

app = FastAPI(title="Eva Vision API", version="2.0", docs_url="/docs")

# Configuración de servicios locales
SERVICES = {
    "qwen": "http://localhost:8004",
    "moondream": "http://localhost:8003",
    "yolo": "http://localhost:8002",
}

class VisionRequest(BaseModel):
    service: str = "qwen"
    prompt: str = ""
    priority: int = 10
    max_tokens: int = 150
    confidence: float = 0.3

def resize_image(img_bytes: bytes, max_size: int = 512) -> bytes:
    """Redimensiona imagen manteniendo aspect ratio"""
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w*ratio), int(h*ratio)), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    img.save(output, format='JPEG', quality=85, optimize=True)
    return output.getvalue()

@app.post("/analyze")
async def analyze_image(
    image: UploadFile = File(...),
    request: VisionRequest = None
):
    """Endpoint principal: imagen + configuración → respuesta unificada"""
    if request is None:
        request = VisionRequest()
    
    try:
        img_bytes = await image.read()
        resized = resize_image(img_bytes)
        img_b64 = base64.b64encode(resized).decode()
        
        service = request.service
        service_url = SERVICES.get(service)
        if not service_url:
            raise HTTPException(status_code=400, detail=f"Servicio {service} no disponible")
        
        # Routing por servicio
        if service in ["qwen"]:
            payload = {
                "model": service,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                        {"type": "text", "text": request.prompt or "Describe this image briefly."}
                    ]
                }],
                "max_tokens": request.max_tokens
            }
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(f"{service_url}/v1/chat/completions", json=payload)
                resp.raise_for_status()
                result = resp.json()["choices"][0]["message"]["content"]
            return {"success": True, "answer": result, "service": service, "image_size_kb": len(resized)//1024}
            
        elif service == "yolo":
            files = {"image": ("img.jpg", resized, "image/jpeg")}
            data = {"confidence": request.confidence}
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{service_url}/detect", files=files, data=data)
                return {"success": True, "detections": resp.json().get("detections", []), "service": "yolo"}
        
        else:
            return {"success": False, "error": f"Servicio {service} no implementado aún"}
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Timeout en servicio {service}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    """Health check para monitoreo"""
    status = {"eva_api": "healthy", "services": {}}
    async with httpx.AsyncClient(timeout=3) as client:
        for name, url in SERVICES.items():
            try:
                resp = await client.get(f"{url}/health")
                status["services"][name] = "healthy" if resp.status_code == 200 else "unhealthy"
            except:
                status["services"][name] = "unreachable"
    return status

@app.get("/services")
async def list_services():
    """Listar servicios disponibles"""
    return {"available": list(SERVICES.keys()), "entry_point": "https://api.ojoia.com.do/analyze"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)