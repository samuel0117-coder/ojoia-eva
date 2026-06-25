#!/usr/bin/env python3
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CORETHINK_KEY = "sk_343332290bf2be7aaca28ecd18d08d1051bea029d2eb628cfdaf51aeb8193168"
CORETHINK_URL = "https://api.corethink.ai/v1/chat/completions"

app = FastAPI()

@app.post("/v1/chat/completions")
async def proxy(request: Request):
    body = await request.json()
    filtered = {
        "model": body.get("model", "openai/gpt-oss-120b"),
        "messages": body.get("messages", []),
        "temperature": body.get("temperature", 0.7),
        "max_tokens": body.get("max_tokens"),
    }
    filtered = {k: v for k, v in filtered.items() if v is not None}
    
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            CORETHINK_URL,
            json=filtered,
            headers={"Authorization": f"Bearer {CORETHINK_KEY}", "Content-Type": "application/json"}
        )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8089)