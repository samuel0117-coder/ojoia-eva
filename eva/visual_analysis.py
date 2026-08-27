"""
Eva v10 Visual Analysis Module
Handles image analysis with Qwen for scene understanding
"""

import base64
import time
from typing import Optional
import httpx

from gateway_resize import resize_image


QWEN_VISION_URL = "http://localhost:8004/v1/chat/completions"
QWEN_VISION_TIMEOUT = 30  # seconds


async def analyze_scene(image_b64: str, business_type: str, zone: str) -> str:
    """
    Analyze a camera frame to understand the scene
    Returns a short description of what's visible
    """
    try:
        # Resize image to reduce token usage
        image_bytes = base64.b64decode(image_b64)
        resized = resize_image(image_bytes, max_size=400)
        resized_b64 = base64.b64encode(resized).decode()
        
        prompt = f"""Describe this scene from a security camera in a {business_type} located in {zone}.
        Mention only: visibility, lighting, obstructions, camera angle.
        Be very short - maximum 2 sentences. Speak naturally."""
        
        async with httpx.AsyncClient(timeout=QWEN_VISION_TIMEOUT) as client:
            response = await client.post(
                QWEN_VISION_URL,
                json={
                    "model": "qwen",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{resized_b64}"}},
                                {"type": "text", "text": prompt}
                            ]
                        }
                    ],
                    "max_tokens": 150
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                description = result["choices"][0]["message"]["content"].strip()
                return description
            else:
                return f"Unable to analyze scene clearly."
                
    except Exception as e:
        # Fallback description if analysis fails
        return f"Camera view of {zone} area in {business_type}."


async def check_visibility_issues(image_b64: str) -> str:
    """
    Check for specific visibility problems in the image
    """
    try:
        image_bytes = base64.b64decode(image_b64)
        resized = resize_image(image_bytes, max_size=400)
        resized_b64 = base64.b64encode(resized).decode()
        
        prompt = """Look at this security camera image and tell me ONLY if you see:
        1. Very bright lights causing glare
        2. Very dark areas with no visibility
        3. Objects blocking the view
        4. Bad camera angle (too high/too low)
        
        Respond with SHORT phrases like: "bright light glare", "dark corner", "obstruction", "low angle"
        If everything looks good, say: "view clear" """
        
        async with httpx.AsyncClient(timeout=QWEN_VISION_TIMEOUT) as client:
            response = await client.post(
                QWEN_VISION_URL,
                json={
                    "model": "qwen",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{resized_b64}"}},
                                {"type": "text", "text": prompt}
                            ]
                        }
                    ],
                    "max_tokens": 100
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                issues = result["choices"][0]["message"]["content"].strip()
                return issues.lower()
            else:
                return "view clear"
                
    except Exception:
        return "view clear"


async def suggest_rule_improvements(image_b64: str, business_type: str, concern: str) -> str:
    """
    Suggest how to improve rule effectiveness based on what's visible
    """
    try:
        image_bytes = base64.b64decode(image_b64)
        resized = resize_image(image_bytes, max_size=400)
        resized_b64 = base64.b64encode(resized).decode()
        
        prompt = f"""Based on this security camera view of a {business_type} in {zone} area,
        and knowing the owner's concern about "{concern}",
        suggest ONE specific improvement to camera placement or settings.
        
        Be very concrete and short. Example: "tilt camera down 10 degrees" or "move left 20cm".
        Maximum one sentence."""
        
        async with httpx.AsyncClient(timeout=QWEN_VISION_TIMEOUT) as client:
            response = await client.post(
                QWEN_VISION_URL,
                json={
                    "model": "qwen",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{resized_b64}"}},
                                {"type": "text", "text": prompt}
                            ]
                        }
                    ],
                    "max_tokens": 100
                }
            )
            
            if response.status_code == 200:
                result = response.json()
                suggestion = result["choices"][0]["message"]["content"].strip()
                return suggestion
            else:
                return "consider adjusting camera angle for better coverage"
                
    except Exception:
        return "consider adjusting camera angle"
