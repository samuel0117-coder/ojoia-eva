#!/opt/ojoia/venv/bin/python
"""
deploy_frontend.py — Deploy frontend a Firebase Hosting via REST API.

Sin dependencia de Firebase CLI / Node. Solo Python con google-auth + requests.

Flujo (vía API REST v1beta1):
  1. POST /sites/{SITE_ID}/versions → crea versión vacía
  2. POST /sites/{SITE_ID}/versions/{VER}:populateFiles → registra paths con sus hashes
     (hash = SHA256 del contenido GZIPPEADO)
  3. Para cada archivo en uploadRequiredHashes: POST uploadUrl/{hash} con el contenido GZIPPEADO
  4. PATCH /sites/{SITE_ID}/versions/{VER} status=FINALIZED
  5. PATCH /sites/{SITE_ID}/releases/live con version.name

Uso:
  /opt/ojoia/venv/bin/python /opt/ojoia/code/deploy_frontend.py
"""
import os
import sys
import json
import gzip
import time
import hashlib
import base64
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


def sha256_of_gzip(content: bytes) -> str:
    """Firebase requiere SHA256 del contenido GZIPPEADO, en hex."""
    gzipped = gzip.compress(content)
    return hashlib.sha256(gzipped).hexdigest()


def collect_files() -> dict:
    """Collect files to deploy, skip backups/obsolete."""
    skip_dirs = {"backups", "__pycache__", ".git", "admin", "test-identity"}
    skip_files = {
        "old_v5_index_backup.html",
        "app-v12.js.backup_1787151067",
        "test-identity.html",
        "server.py",
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
        if ext in {".py", ".pyc", ".md", ".txt", ".log"}:
            continue
        content = path.read_bytes()
        files[f"/{rel}"] = content
    return files


def deploy():
    print(f"=== Deploy frontend to Firebase Hosting ===")
    print(f"Project: {PROJECT_ID}")
    print(f"Frontend dir: {FRONTEND_DIR}")

    files = collect_files()
    print(f"\nArchivos a subir: {len(files)}")
    for f in sorted(files.keys()):
        size_kb = len(files[f]) / 1024
        print(f"  {f} ({size_kb:.1f} KB)")

    # 1) Crear versión
    print("\n[1/5] Creando versión...")
    r = requests.post(f"{BASE_URL}/versions", headers=auth_hdr, json={})
    r.raise_for_status()
    version_name = r.json()["name"]  # sites/ojoia-67216/versions/XXXX
    version_id = version_name.split("/")[-1]
    print(f"  Versión creada: {version_id}")

    # 2) populateFiles con hash de gzip
    print("\n[2/5] Registrando archivos (populateFiles)...")
    files_map = {}
    gzipped_cache = {}  # path -> gzipped content (para upload)
    for path, content in files.items():
        gz = gzip.compress(content)
        gzipped_cache[path] = gz
        files_map[path] = sha256_of_gzip(content)

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

    # 3) Subir contenido gzipped de cada archivo pendiente
    print("\n[3/5] Subiendo contenido...")
    for path, content in files.items():
        gz = gzipped_cache[path]
        h = files_map[path]
        if h not in upload_required:
            print(f"  skip {path} (ya en hosting)")
            continue
        url = f"{upload_url}/{h}"
        # El uploadUrl acepta el Authorization Bearer (no Google login)
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/octet-stream"},
            data=gz,
        )
        r.raise_for_status()
        print(f"  uploaded {path} ({len(gz)/1024:.1f} KB gz)")

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

    # Esperar a que Firebase termine de procesar
    for attempt in range(30):
        r = requests.get(f"{BASE_URL}/versions/{version_id}", headers=auth_hdr)
        r.raise_for_status()
        s = r.json().get("status", "")
        if s == "FINALIZED":
            print(f"  Versión FINALIZED tras {attempt+1} polls")
            break
        time.sleep(1)

    # 5) Crear release en canal "live" para hacerlo público
    print("\n[5/5] Creando release en canal 'live'...")
    r = requests.post(
        f"{BASE_URL}/channels/live/releases?versionName={version_name}",
        headers=auth_hdr,
        json={},
    )
    r.raise_for_status()
    release = r.json()
    print(f"  Release creado: {release.get('name', '?')}")

    print(f"\n✅ Deploy completo. Versión {version_id} activa.")
    print(f"   URL: https://ojoia-67216.web.app/  (ojoia.com.do actualiza en <60s)")
    print(f"\nCache-bust URLs: ?v=20260821e (incluido en index.html)")


if __name__ == "__main__":
    try:
        deploy()
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
