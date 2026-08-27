#!/usr/bin/env python3
"""
ojoia_models_client.py — Cliente ligero para que el nodo CineIA (u otro)
llame a los modelos del nodo OjoIA a través del Service Bus LAN.

El nodo OjoIA expone el bus de modelos en su IP de LAN (por defecto
http://10.0.0.71:8205). Cada request va autenticada con una API key de
billing, así que el consumo queda registrado y facturado automáticamente
en Redis del nodo OjoIA (mismo sistema que la web).

Uso:
    export OJOIA_BUS_URL="http://10.0.0.71:8205"
    export OJOIA_BUS_KEY="ojoia_live_xxxx"   # clave del cliente cineia

    from ojoia_models_client import OjoIAModels
    m = OjoIAModels()
    r = m.chat("qwen35b", [{"role":"user","content":"hola"}])
    print(r["choices"][0]["message"]["content"])

Backends disponibles (este nodo):
    qwen7b   -> 127.0.0.1:8004  (rápido, bajo costo)
    qwen9b   -> 127.0.0.1:8018
    qwen35b  -> 127.0.0.1:8019  (máxima calidad, contexto largo)
    whisper  -> 127.0.0.1:8008  (audio)
    yolo     -> 127.0.0.1:8002  (visión)

El campo "model" del body debe coincidir con el nombre que el bus usa para
facturar: qwen3-7b, qwen35 / qwen36-35b-a3b, qwen36-35b-a3b, etc.
"""
from __future__ import annotations

import os
import json
from typing import Any

import httpx


class OjoIAModelsError(RuntimeError):
    pass


class OjoIAModels:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.environ.get("OJOIA_BUS_URL", "http://10.0.0.71:8205")).rstrip("/")
        self.api_key = api_key or os.environ.get("OJOIA_BUS_KEY", "")
        if not self.api_key:
            raise OjoIAModelsError(
                "Falta OJOIA_BUS_KEY (API key de billing del cliente cineia)"
            )
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        backend: str,
        messages: list[dict[str, str]],
        model: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        stream: bool = False,
        **extra: Any,
    ) -> dict:
        """Llama a /{backend}/v1/chat/completions (OpenAI-compatible)."""
        model = model or self._default_model(backend)
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
            **extra,
        }
        url = f"{self.base_url}/{backend}/v1/chat/completions"
        try:
            resp = httpx.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise OjoIAModelsError(f"No se pudo llegar al bus OjoIA: {e}") from e
        if resp.status_code != 200:
            raise OjoIAModelsError(f"Bus devolvió {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def complete(
        self,
        backend: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 512,
        **extra: Any,
    ) -> dict:
        """Llama a /{backend}/v1/completions (prompt plano)."""
        model = model or self._default_model(backend)
        payload = {"model": model, "prompt": prompt, "max_tokens": max_tokens, **extra}
        url = f"{self.base_url}/{backend}/v1/completions"
        try:
            resp = httpx.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise OjoIAModelsError(f"No se pudo llegar al bus OjoIA: {e}") from e
        if resp.status_code != 200:
            raise OjoIAModelsError(f"Bus devolvió {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def health(self) -> dict:
        url = f"{self.base_url}/bus/health"
        resp = httpx.get(url, timeout=10)
        return resp.json()

    @staticmethod
    def _default_model(backend: str) -> str:
        return {
            "qwen7b": "qwen3-7b",
            "qwen9b": "qwen35",
            "qwen35b": "qwen36-35b-a3b",
            "whisper": "whisper-large-v3",
            "yolo": "yolo-pose",
        }.get(backend, backend)


if __name__ == "__main__":
    import sys

    m = OjoIAModels()
    try:
        out = m.chat("qwen35b", [{"role": "user", "content": "Di hola en una palabra"}])
        print(json.dumps(out, indent=2, ensure_ascii=False))
    except OjoIAModelsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
