# eva_client.py - Ejemplo de integración para Eva
import httpx
import asyncio

async def ask_eva_about_camera(camera_id: str, image_bytes: bytes, prompt: str = "Describe this image briefly."):
    """
    Función principal que Eva usa para analizar imágenes.
    
    Args:
        camera_id: ID de la cámara (ej: "sala", "cocina")
        image_bytes: Imagen en bytes (JPEG/PNG)
        prompt: Pregunta para Qwen
    
    Returns:
        str: Respuesta de Qwen
    """
    files = {"image": (f"{camera_id}.jpg", image_bytes, "image/jpeg")}
    data = {
        "prompt": f"{prompt} Be concise.",
        "priority": 5,
        "max_tokens": 100
    }
    
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "http://localhost:8007/analyze",
            files=files,
            data=data
        )
        if resp.status_code == 200:
            return resp.json()["answer"]
        else:
            return f"Error: {resp.text}"

# Uso en Eva:
# result = await ask_eva_about_camera("camara_sala", image_bytes)
# print(f"Eva detectó: {result}")