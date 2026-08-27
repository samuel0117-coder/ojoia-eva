#!/usr/bin/env python3
"""Gateway para redimensionar imágenes antes de enviar a Qwen"""
from PIL import Image
import io
import base64
from typing import List


def resize_image(img_bytes: bytes, max_size: int = 512) -> bytes:
    """Redimensiona imagen manteniendo aspect ratio"""
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        new_size = (int(w * ratio), int(h * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    output = io.BytesIO()
    img.save(output, format='JPEG', quality=85, optimize=True)
    return output.getvalue()


def create_grid_image(image_bytes_list: List[bytes], max_size: int = 256) -> bytes:
    """Create a 4x4 grid image from multiple frames"""
    thumbnails = []
    for img_bytes in image_bytes_list[:16]:
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
            thumbnails.append(img)
        except Exception:
            continue
    
    if not thumbnails:
        return b''
    
    n = len(thumbnails)
    cols = 4
    rows = (n + cols - 1) // cols
    
    thumb_w, thumb_h = thumbnails[0].size
    grid = Image.new('RGB', (thumb_w * cols, thumb_h * rows), (0, 0, 0))
    
    for i, thumb in enumerate(thumbnails):
        x = (i % cols) * thumb_w
        y = (i // cols) * thumb_h
        grid.paste(thumb, (x, y))
    
    output = io.BytesIO()
    grid.save(output, format='JPEG', quality=85)
    return output.getvalue()


def image_to_base64(img_bytes: bytes) -> str:
    """Convierte bytes a base64 para API"""
    return base64.b64encode(img_bytes).decode('utf-8')


def prepare_vision_payload(image_bytes: bytes, prompt: str, max_tokens: int = 100) -> dict:
    """Prepara payload completo para Qwen Vision"""
    resized = resize_image(image_bytes, max_size=512)
    img_b64 = image_to_base64(resized)
    
    return {
        "model": "qwen",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                {"type": "text", "text": prompt}
            ]
        }],
        "max_tokens": max_tokens
    }