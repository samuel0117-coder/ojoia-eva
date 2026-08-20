#!/usr/bin/env python3
"""Script para verificar estabilidad del túnel Cloudflare"""
import subprocess
import time
import json
import requests

TUNNEL_URL = "https://api.ojoia.com.do"
LOCAL_URL = "http://localhost:8005"

def check_tunnel():
    """Verifica que el túnel esté funcionando"""
    try:
        resp = requests.get(f"{TUNNEL_URL}/admin/server/status", timeout=5)
        return resp.status_code == 200
    except:
        return False

def check_local():
    """Verifica que el API local esté funcionando"""
    try:
        resp = requests.get(f"{LOCAL_URL}/admin/server/status", timeout=5)
        return resp.status_code == 200
    except:
        return False

if __name__ == "__main__":
    print("Verificando sistema OjoIA...")
    print(f"✅ API Local: {'OK' if check_local() else 'FALLO'}")
    print(f"✅ Túnel Cloudflare: {'OK' if check_tunnel() else 'FALLO'}")
    
    local = check_local()
    tunnel = check_tunnel()
    
    if local and tunnel:
        print("\n✅ Sistema completamente operativo")
        exit(0)
    else:
        print("\n⚠️ Sistema con problemas")
        exit(1)
