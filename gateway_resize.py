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

# ═══════════════════════════════════════════════════════════════════════════
# WATERMARK — Marca de agua en frames al recibirlos
# ═══════════════════════════════════════════════════════════════════════════

_frame_counter = {}  # camera_id -> counter


def add_frame_watermark(
    img_bytes: bytes,
    camera_id: str,
    timestamp_str: str,
    business_name: str = "",
) -> bytes:
    """Agrega watermark con frame_num, timestamp, camera_id y marca OjoIA.

    Layout:
        [_frame_num]              [timestamp]
        [ESCENA]
        [camera_id]               [OjoIA]
    """
    from PIL import Image, ImageDraw, ImageFont
    import io

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size

    # Escalar fuente según resolución
    font_size = max(12, min(w, h) // 30)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    # Frame número (contador por cámara)
    _frame_counter[camera_id] = _frame_counter.get(camera_id, 0) + 1
    frame_num = _frame_counter[camera_id]

    # ── Esquina superior izquierda: Número de frame ──
    num_text = f"#{frame_num}"
    draw.rectangle([3, 3, 60, font_size + 10], fill=(0, 0, 0, 160))
    draw.text((6, 5), num_text, fill="red", font=font)

    # ── Esquina superior derecha: Timestamp ──
    ts = timestamp_str[:19]  # YYYY-MM-DDTHH:MM:SS
    bbox = draw.textbbox((0, 0), ts, font=font)
    tw = bbox[2] - bbox[0]
    draw.rectangle([w - tw - 10, 3, w - 3, font_size + 10], fill=(0, 0, 0, 160))
    draw.text((w - tw - 6, 5), ts, fill="white", font=font)

    # ── Esquina inferior izquierda: Cámara ──
    cam = camera_id[:12] if camera_id else ""
    if cam:
        draw.rectangle([3, h - font_size - 12, len(cam) * (font_size // 2) + 12, h - 3], fill=(0, 0, 0, 160))
        draw.text((6, h - font_size - 10), cam, fill="white", font=font)

    # ── Esquina inferior derecha: OjoIA ──
    marca = "OjoIA"
    bbox2 = draw.textbbox((0, 0), marca, font=font)
    mw = bbox2[2] - bbox2[0]
    draw.rectangle([w - mw - 12, h - font_size - 12, w - 3, h - 3], fill=(0, 180, 220, 200))
    draw.text((w - mw - 8, h - font_size - 10), marca, fill="white", font=font)

    output = io.BytesIO()
    img.save(output, format="JPEG", quality=92)
    return output.getvalue()


def reset_frame_counter(camera_id: str = None):
    """Resetea el contador de frames (útil para testing)."""
    global _frame_counter
    if camera_id:
        _frame_counter.pop(camera_id, None)
    else:
        _frame_counter = {}


# ═══════════════════════════════════════════════════════════════════════════
# PANELS 2×2 — Divide frames en grids secuenciales para Qwen
# ═══════════════════════════════════════════════════════════════════════════

def create_panels_2x2(frames: list) -> list:
    """Divide lista de frames en panels de 2×2 para análisis secuential.

    Args:
        frames: Lista de dicts con 'image_bytes' o lista de bytes

    Returns:
        Lista de bytes (JPEG) — cada uno es un grid 2×2 = 4 frames
    """
    panels = []
    for i in range(0, len(frames), 4):
        group = frames[i:i+4]
        if len(group) < 4:
            # Rellenar con el último frame disponible
            last = group[-1] if group else None
            while len(group) < 4:
                group.append(last)
        panel_bytes = _create_single_grid_2x2(group, panel_num=i // 4 + 1)
        panels.append(panel_bytes)
    return panels


def _create_single_grid_2x2(frames_group: list, panel_num: int) -> bytes:
    """Crea un grid 2×2 de 4 frames con numeración visible."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    thumbs = []
    for f in frames_group:
        if f is None:
            continue
        if isinstance(f, dict):
            data = f.get("image_bytes", b"")
        else:
            data = f if isinstance(f, bytes) else b""
        if not data:
            continue
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.thumbnail((320, 320), Image.Resampling.LANCZOS)
            thumbs.append(img)
        except Exception:
            # Placeholder negro si falla
            thumbs.append(Image.new("RGB", (320, 320), (30, 30, 30)))

    if not thumbs:
        return b""

    # Asegurar que todos tengan el mismo tamaño
    size = thumbs[0].size[0]
    for i, t in enumerate(thumbs):
        if t.size[0] != size:
            thumbs[i] = t.resize((size, size), Image.Resampling.LANCZOS)

    grid = Image.new("RGB", (size * 2, size * 2), (15, 15, 15))
    draw = ImageDraw.Draw(grid)

    positions = [(0, 0), (size, 0), (0, size), (size, size)]
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30
        )
    except Exception:
        font = ImageFont.load_default()

    for idx, (thumb, pos) in enumerate(zip(thumbs, positions)):
        grid.paste(thumb, pos)
        x, y = pos
        label = f"{idx + 1}"
        draw.rectangle([x + 3, y + 3, x + 40, y + 40], fill=(0, 0, 0, 200))
        draw.text((x + 9, y + 5), label, fill="yellow", font=font)

    try:
        small_font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12
        )
    except Exception:
        small_font = ImageFont.load_default()
    draw.text((4, size * 2 - 16), f"Panel {panel_num}", fill="gray", font=small_font)

    output = io.BytesIO()
    grid.save(output, format="JPEG", quality=90)
    return output.getvalue()
