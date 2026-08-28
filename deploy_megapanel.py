#!/opt/ojoia/venv/bin/python
"""
deploy_megapanel.py — Deploy la SPA del megapanel a Firebase Hosting.

Site: megapanel-ojoia → https://megapanel-ojoia.web.app
Origen: /opt/ojoia/code/frontend/megapanel/ → se sirve como /index.html

Flujo (REST API v1beta1):
  1. POST /sites/{SITE_ID}/versions → crea version
  2. POST :populateFiles → registra paths+hashes
  3. POST uploadUrl/{hash} → sube contenido gzip
  4. PATCH version status=FINALIZED
  5. POST channels/live/releases → activa
"""
import os
import sys
import json
import time
import gzip
import hashlib
import requests
from pathlib import Path
from google.oauth2 import service_account
from google.auth.transport.requests import Request

PROJECT_ID = "ojoia-67216"
SITE_ID = "megapanel-ojoia"
KEY_FILE = "/opt/ojoia/config/firebase-key.json"
FRONTEND_DIR = Path("/opt/ojoia/code/frontend/megapanel")

# Auth
SCOPES = ["https://www.googleapis.com/auth/firebase.hosting"]
creds = service_account.Credentials.from_service_account_file(KEY_FILE, scopes=SCOPES)
creds.refresh(Request())
auth_hdr = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}
BASE_URL = f"https://firebasehosting.googleapis.com/v1beta1/projects/{PROJECT_ID}/sites/{SITE_ID}"


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def collect_files() -> dict:
    """Recolecta archivos del frontend, los gzipea y devuelve {path: (gzip_content, hash)}."""
    files = {}
    for p in FRONTEND_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(FRONTEND_DIR))
        if rel.startswith("."):
            continue
        # Firebase requiere paths con / al inicio
        path = f"/{rel}" if not rel.startswith("/") else rel
        raw = p.read_bytes()
        gz = gzip.compress(raw, compresslevel=6)
        files[path] = (gz, sha256(gz))
    return files


def main():
    print(f"=== Deploy megapanel SPA → {SITE_ID} ===")
    files = collect_files()
    print(f"Archivos: {len(files)}")
    for f in files:
        print(f"  - {f} ({len(files[f][0])} bytes gzip)")

    # 1. Crear version
    print("\n[1/5] Creando version...")
    r = requests.post(f"{BASE_URL}/versions", headers=auth_hdr, json={})
    r.raise_for_status()
    version = r.json()
    version_name = version["name"]
    print(f"  Version: {version_name}")

    # 2. Populate files
    print("\n[2/5] Populate files...")
    # Formato correcto: files es un map {path: hash_string} directo
    files_map = {path: h for path, (_, h) in files.items()}
    populate_body = {"files": files_map}
    full_version = f"{BASE_URL}/versions/{version_name.split('/')[-1]}"
    r = requests.post(f"{full_version}:populateFiles", headers=auth_hdr, json=populate_body)
    r.raise_for_status()
    pop = r.json()
    upload_required = pop.get("uploadRequiredHashes", [])
    upload_skipped = pop.get("uploadSkippedHashes", [])
    print(f"  Upload required: {len(upload_required)}, skipped (cached): {len(upload_skipped)}")

    # 3. Upload missing
    print("\n[3/5] Upload archivos...")
    hash_to_path = {h: p for p, (_, h) in files.items()}
    # Re-populate to get fresh upload URL
    r = requests.post(f"{full_version}:populateFiles", headers=auth_hdr, json=populate_body)
    r.raise_for_status()
    pop3 = r.json()
    upload_url = pop3.get("uploadUrl", "")
    for h in sorted(upload_required):
        if h not in hash_to_path:
            continue
        path = hash_to_path[h]
        gz_content, _ = files[path]
        if not upload_url:
            print(f"  WARN: no upload URL for {path}")
            continue
        # Formato correcto: POST {upload_url}/{hash}, sin Content-Encoding
        url = f"{upload_url.rstrip('/')}/{h}"
        r = requests.post(url, data=gz_content,
                         headers={"Authorization": f"Bearer {creds.token}",
                                  "Content-Type": "application/octet-stream"})
        r.raise_for_status()
        print(f"  ✓ {path}")

    # 4. Finalize
    print("\n[4/5] Finalize version...")
    r = requests.patch(f"{full_version}?updateMask=status", headers=auth_hdr,
                      json={"status": "FINALIZED"})
    r.raise_for_status()
    print(f"  Status: {r.json().get('status')}")

    # 5. Release
    print("\n[5/5] Release to live channel...")
    # versionName debe ser el resource name, no la URL completa
    version_resource = full_version.replace("https://firebasehosting.googleapis.com/v1beta1/", "")
    r = requests.post(f"{BASE_URL}/channels/live/releases?versionName={version_resource}",
                     headers=auth_hdr, json={})
    if r.status_code == 409:
        print("  Release already exists; using PATCH")
        r = requests.patch(f"{BASE_URL}/channels/live?updateMask=release",
                          headers=auth_hdr, json={"release": {"versionName": version_resource}})
    r.raise_for_status()
    release = r.json()
    print(f"  Release: {release.get('name')}")
    print(f"\n✓ Deploy completo: https://megapanel-ojoia.web.app")


if __name__ == "__main__":
    main()
