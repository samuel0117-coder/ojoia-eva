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
        # Cache-busting: v12/v7 tienen cache corrupto. Solo v13/v8.
        "app-v12.js",
        "eva-chat-v7.js",
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


def deploy():
    print(f"=== Deploy frontend to Firebase Hosting (BROTLI) ===")
    print(f"Project: {PROJECT_ID}")
    print(f"Frontend dir: {FRONTEND_DIR}")

    files = collect_files()
    print(f"\nArchivos a subir: {len(files)}")
    for f in sorted(files.keys()):
        size_kb = len(files[f]) / 1024
        print(f"  {f} ({size_kb:.1f} KB)")

    # 1) Crear versión con config que invalida caches viejos (no-cache para JS/CSS/HTML)
    print("\n[1/5] Creando versión con config no-cache...")
    config = {
        "headers": [
            {
                "headers": {"Cache-Control": "no-cache, no-store, must-revalidate"},
                "glob": "**/*.@(js|css|html)"
            },
            {
                "headers": {"Cache-Control": "max-age=86400"},
                "glob": "**/*.@(png|jpg|jpeg|gif|svg|ico)"
            }
        ]
    }
    r = requests.post(f"{BASE_URL}/versions", headers=auth_hdr, json={"config": config})
    r.raise_for_status()
    version_name = r.json()["name"]
    version_id = version_name.split("/")[-1]
    print(f"  Versión creada: {version_id}")

    # 2) populateFiles con hash del contenido COMPRIMIDO
    print("\n[2/5] Registrando archivos...")
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

    # 3) Subir contenido comprimido
    print("\n[3/5] Subiendo contenido...")
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

    # 4) Marcar versión como FINALIZED
    print("\n[4/5] Finalizando versión...")
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

    # 5) Crear release en canal "live"
    print("\n[5/5] Creando release en canal 'live'...")
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
