#!/opt/ojoia/venv/bin/python
"""
deploy_frontend.py — Deploy frontend a Firebase Hosting via REST API.

Usa BROTLI en vez de gzip porque Firebase Hosting devuelve Content-Encoding: br
cuando el navegador pide Accept-Encoding: br. Si subimos gzip, Firebase a veces
miente con el header br pero envía bytes raw, causando SyntaxError en el navegador.

Solución: subir contenido BROTLI-COMPRIMIDO (mismo formato que el navegador espera).

Flujo (vía API REST v1beta1):
  1. POST /sites/{SITE_ID}/versions → crea version
  2. POST /sites/{SITE_ID}/versions/{VER}:populateFiles → registra paths+hashes
     (hash = SHA256 del contenido BROTLI-COMPRIMIDO)
  3. Para cada archivo en uploadRequiredHashes: POST uploadUrl/{hash} con contenido brotli
  4. PATCH /sites/{SITE_ID}/versions/{VER} status=FINALIZED
  5. POST /sites/{SITE_ID}/channels/live/releases → activa en producción

Uso:
  /opt/ojoia/venv/bin/python /opt/ojoia/code/deploy_frontend.py
"""
import os
import sys
import json
import time
import hashlib
import requests
from pathlib import Path
from google.oauth2 import service_account
from google.auth.transport.requests import Request

# Config
PROJECT_ID = "ojoia-67216"
SITE_ID = PROJECT_ID
KEY_FILE = "/opt/ojoia/config/firebase-key.json"
FRONTEND_DIR = Path("/opt/ojoia/code/frontend")

# Auth
SCOPES = ["https://www.googleapis.com/auth/firebase.hosting"]
creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
creds.refresh(Request())
auth_hdr = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

BASE_URL = f"https://firebasehosting.googleapis.com/v1beta1/sites/{SITE_ID}"


def sha256_of(content: bytes) -> str:
    """Firebase requiere SHA256 hex del contenido."""
    return hashlib.sha256(content).hexdigest()


def compress_for_firebase(content: bytes, ext: str) -> bytes:
    """
    Firebase Hosting REQUIERE gzip para TODOS los uploads.
    El CDN puede re-comprimir a brotli on-the-fly al servir.
    """
    import gzip
    # SIEMPRE gzipear — Firebase lo requiere para todos los archivos
    return gzip.compress(content)


def collect_files() -> dict:
    """Collect files to deploy, skip backups/obsolete."""
    skip_dirs = {"backups", "__pycache__", ".git", "admin", "test-identity"}
    skip_files = {
        "old_v5_index_backup.html",
        "app-v12.js.backup_1787151067",
        "test-identity.html",
        "server.py",
        # Cache-busting: archivos viejos tienen cache corrupto en Cloudflare CDN.
        # Solo deployamos nombres nuevos que Cloudflare NUNCA ha visto.
        "app-v12.js",
        "eva-chat-v7.js",
        "app-v13.js",
        "eva-chat-v8.js",
    }
    files = {}
    for path in FRONTEND_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(FRONTEND_DIR))
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.name in skip_files:
            continue
        ext = path.suffix.lower()
        if ext in {".py", ".pyc", ".md", ".log"}:
            continue
        content = path.read_bytes()
        files[f"/{rel}"] = content
    return files


def content_hash(content: bytes, length: int = 8) -> str:
    """SHA256 hex corto (default 8 chars) para cache-busting de filenames."""
    return hashlib.sha256(content).hexdigest()[:length]


def rewrite_index_html(html_bytes: bytes) -> bytes:
    """
    Reescribe index.html para apuntar a filenames con hash de contenido.

    P0: cache-busting robusto contra Cloudflare cache. Cada deploy genera
    URLs únicas (app-2026-{HASH8}.js) que Cloudflare nunca ha cacheado.
    Combinado con no-store en headers HTTP, garantiza que el navegador SIEMPRE
    pida el archivo al origin y reciba el encoding correcto.

    También reescribe el cleanup script del SW para usar el hash como cache name,
    evitando que un SW viejo siga activo con cache key incorrecto.
    """
    html = html_bytes.decode("utf-8")

    # Calcular hashes de los assets referenciados
    js_path = FRONTEND_DIR / "app-2026.js"
    chat_path = FRONTEND_DIR / "chat-2026.js"
    css_path = FRONTEND_DIR / "app.css"
    sw_path = FRONTEND_DIR / "sw.js"

    js_hash = content_hash(js_path.read_bytes()) if js_path.exists() else "00000000"
    chat_hash = content_hash(chat_path.read_bytes()) if chat_path.exists() else "00000000"
    css_hash = content_hash(css_path.read_bytes()) if css_path.exists() else "00000000"
    combined_hash = content_hash(
        (js_hash + chat_hash + css_hash).encode()
    )

    print(f"  Hashes para cache-busting:")
    print(f"    app-2026.js → {js_hash}")
    print(f"    chat-2026.js → {chat_hash}")
    print(f"    app.css     → {css_hash}")
    print(f"    combined    → {combined_hash}")

    # Reemplazar referencias con query string de hash
    # Patrones a reemplazar (en orden de especificidad):
    replacements = [
        # app-2026.js (con o sin query)
        (r'app-2026\.js(\?cb=[^"\']*)?', f'app-2026.js?v={js_hash}'),
        # chat-2026.js (con o sin query)
        (r'chat-2026\.js(\?cb=[^"\']*)?', f'chat-2026.js?v={chat_hash}'),
        # app.css (con o sin query)
        (r'app\.css(\?cb=[^"\']*)?', f'app.css?v={css_hash}'),
    ]

    import re
    for pattern, replacement in replacements:
        new_html = re.sub(pattern, replacement, html)
        if new_html == html:
            print(f"    [WARN] pattern '{pattern}' no encontró match")
        html = new_html

    return html.encode("utf-8")


def rewrite_sw_js(sw_bytes: bytes, cache_name: str) -> bytes:
    """
    Reescribe el CACHE_NAME del SW para que sea único por deploy.
    El cleanup script del HTML borra todos los caches viejos, pero si el SW
    llega a activarse antes del cleanup, queremos que su cache name también
    sea único por deploy.
    """
    import re
    text = sw_bytes.decode("utf-8")
    text = re.sub(
        r"(const|let|var)\s+CACHE_NAME\s*=\s*['\"]ojoia-[^'\"]*['\"]",
        f"const CACHE_NAME = 'ojoia-{cache_name}'",
        text
    )
    return text.encode("utf-8")


def deploy():
    print(f"=== Deploy frontend to Firebase Hosting (BROTLI) ===")
    print(f"Project: {PROJECT_ID}")
    print(f"Frontend dir: {FRONTEND_DIR}")

    files = collect_files()
    print(f"\nArchivos a subir: {len(files)}")
    for f in sorted(files.keys()):
        size_kb = len(files[f]) / 1024
        print(f"  {f} ({size_kb:.1f} KB)")

    # P0: reescribir index.html con cache-busting hash antes de subirlo.
    # Cada deploy genera URLs únicas (app-2026.js?v=HASH) que Cloudflare
    # nunca ha cacheado, evitando el bug Content-Encoding: br corrupto.
    print("\n[1/6] Cache-busting: reescribiendo index.html con hash de contenido...")

    # Calcular combined_hash primero (usado para sw.js CACHE_NAME)
    _js_p = FRONTEND_DIR / "app-2026.js"
    _chat_p = FRONTEND_DIR / "chat-2026.js"
    _css_p = FRONTEND_DIR / "app.css"
    _jh = content_hash(_js_p.read_bytes()) if _js_p.exists() else "00000000"
    _ch = content_hash(_chat_p.read_bytes()) if _chat_p.exists() else "00000000"
    _csh = content_hash(_css_p.read_bytes()) if _css_p.exists() else "00000000"
    combined_hash = content_hash((_jh + _ch + _csh).encode())

    if "/index.html" in files:
        files["/index.html"] = rewrite_index_html(files["/index.html"])
        print(f"  index.html reescrito ({len(files['/index.html'])} bytes)")

    # También reescribir sw.js con CACHE_NAME único por deploy.
    if "/sw.js" in files:
        files["/sw.js"] = rewrite_sw_js(files["/sw.js"], combined_hash)
        print(f"  sw.js reescrito (CACHE_NAME = ojoia-{combined_hash})")

    # 2) Crear versión con config que invalida caches viejos.
    # P0: incluir glob /** (todo, sin extensión) para que el HTML en /
    # también tenga no-store. Sin esto, Cloudflare cachea / con max-age=3600
    # y sirve HTML viejo con referencias a assets viejos (que devuelven 404).
    print("\n[2/6] Creando versión con config no-cache...")
    config = {
        "headers": [
            # HTML root (/) y todo lo demás sin extensión
            {
                "headers": {
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0"
                },
                "glob": "**"
            },
            # Sobrescribir explícitamente para JS/CSS/HTML con extensión
            {
                "headers": {"Cache-Control": "no-cache, no-store, must-revalidate"},
                "glob": "**/*.@(js|css|html)"
            },
            # Imágenes y assets estáticos: cache por 1 día
            {
                "headers": {"Cache-Control": "max-age=86400"},
                "glob": "**/*.@(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf)"
            }
        ]
    }
    r = requests.post(f"{BASE_URL}/versions", headers=auth_hdr, json={"config": config})
    r.raise_for_status()
    version_name = r.json()["name"]
    version_id = version_name.split("/")[-1]
    print(f"  Versión creada: {version_id}")

    # 3) populateFiles con hash del contenido COMPRIMIDO
    print("\n[3/6] Registrando archivos...")
    files_map = {}
    compressed_cache = {}  # path -> compressed content (para upload)
    for path, content in files.items():
        ext = Path(path).suffix.lower()
        compressed = compress_for_firebase(content, ext)
        compressed_cache[path] = compressed
        files_map[path] = sha256_of(compressed)

    print(f"  Hashes calculados:")
    for path, h in files_map.items():
        print(f"    {path} -> {h[:16]}")

    r = requests.post(
        f"{BASE_URL}/versions/{version_id}:populateFiles",
        headers=auth_hdr,
        json={"files": files_map},
    )
    r.raise_for_status()
    populate_resp = r.json()
    upload_url = populate_resp["uploadUrl"]
    upload_required = set(populate_resp.get("uploadRequiredHashes", []))
    print(f"  uploadUrl: {upload_url}")
    print(f"  Archivos que requieren upload: {len(upload_required)} / {len(files)}")

    # 4) Subir contenido comprimido
    print("\n[4/6] Subiendo contenido...")
    for path, content in files.items():
        compressed = compressed_cache[path]
        h = files_map[path]
        if h not in upload_required:
            print(f"  skip {path} (ya en hosting)")
            continue
        url = f"{upload_url}/{h}"
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/octet-stream"},
            data=compressed,
        )
        r.raise_for_status()
        print(f"  uploaded {path} ({len(compressed)/1024:.1f} KB compressed)")

    # 5) Marcar versión como FINALIZED
    print("\n[5/6] Finalizando versión...")
    r = requests.patch(
        f"{BASE_URL}/versions/{version_id}",
        headers=auth_hdr,
        json={"status": "FINALIZED"},
    )
    r.raise_for_status()
    final_status = r.json().get("status", "?")
    print(f"  Status: {final_status}")

    for attempt in range(30):
        r = requests.get(f"{BASE_URL}/versions/{version_id}", headers=auth_hdr)
        r.raise_for_status()
        s = r.json().get("status", "")
        if s == "FINALIZED":
            print(f"  Versión FINALIZED tras {attempt+1} polls")
            break
        time.sleep(1)

    # 6) Crear release en canal "live"
    print("\n[6/6] Creando release en canal 'live'...")
    r = requests.post(
        f"{BASE_URL}/channels/live/releases?versionName={version_name}",
        headers=auth_hdr,
        json={},
    )
    r.raise_for_status()
    print(f"  Release creado: {r.json().get('name', '?')}")

    print(f"\n✅ Deploy completo. Versión {version_id} activa.")
    print(f"   URL: https://ojoia-67216.web.app/  (ojoia.com.do actualiza en <60s)")


if __name__ == "__main__":
    try:
        deploy()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
