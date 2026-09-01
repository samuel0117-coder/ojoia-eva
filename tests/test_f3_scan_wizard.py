"""F3 — Tests del wizard de escaneo de red de Eva (2026-09-01).

Cubre: consentimiento (pide permiso, no auto-escanea, persiste),
trigger directo del ESP32, presentación de resultados, elección,
credenciales → probe → registro (chmod 600, pending v9.4), reintento
con credenciales inválidas, y el caso sin ESP32.
"""
import asyncio
import base64
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("INGEST_QUEUE", "memory")

import api_eva as A                    # noqa: E402
import eva_v2 as E                      # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Aisla STORAGE_ROOT y la resolución de discos para no tocar producción."""
    monkeypatch.setattr(E, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(A, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(A, "get_user_storage_path", lambda uid, plan: tmp_path / "users" / uid)
    E._save_session_to_disk = lambda s: None


def _mk_user(tmp_path, uid="u_f3", with_esp=True):
    d = tmp_path / "users" / uid
    d.mkdir(parents=True, exist_ok=True)
    cams = ([{"camera_id": "OJO-TEST1", "type": "esp32"}] if with_esp else [])
    (d / "user.json").write_text(json.dumps(
        {"user_id": uid, "owner_name": "Sam Test", "cameras": cams}))
    return d


def test_f3_pide_permiso_y_no_escanea_solo(tmp_path):
    _mk_user(tmp_path)
    uid = "u_f3"
    s = {"session_id": "t1", "user_id": uid, "msgs": [], "phase": "done"}
    r = asyncio.run(E._handle_scan_consent(s, "t1", uid, "escanear mi red", "Sam"))
    assert "permiso" in r["response"].lower()
    # 'escanear' NO cuenta como aceptación (substring de 'escanea' era el bug)
    assert s["phase"] == "done"
    ud = json.loads((tmp_path / "users" / uid / "user.json").read_text())
    assert "consent_network_scan" not in ud  # no guardó nada


def test_f3_consentimiento_persistido_y_trigger(tmp_path):
    uid = "u_f3"
    _mk_user(tmp_path)
    s = {"session_id": "t2", "user_id": uid, "msgs": [], "phase": "done"}
    r = asyncio.run(E._handle_scan_consent(s, "t2", uid, "sí, escanea", "Sam"))
    assert s["phase"] == E.SetupPhase.SCAN_WAIT.value
    ud = json.loads((tmp_path / "users" / uid / "user.json").read_text())
    assert ud["consent_network_scan"] is True
    flags = [c.get("scan_request") for c in ud["cameras"] if c["camera_id"] == "OJO-TEST1"]
    assert flags == [True]  # el ESP32 lo consumirá en su polling


def test_f3_resultados_y_registro_completo(tmp_path):
    uid = "u_f3"
    _mk_user(tmp_path)
    s = {"session_id": "t3", "user_id": uid, "msgs": [], "phase": "done"}
    asyncio.run(E._handle_scan_consent(s, "t3", uid, "sí escanea", "Sam"))

    # ESP32 reporta resultados
    sd = tmp_path / "users" / uid / "cameras" / "OJO-TEST1"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "last_scan.json").write_text(json.dumps({
        "camera_id": "OJO-TEST1", "found": 1, "scanned_at": time.time() + 2,
        "devices": [{"ip": "192.168.1.64", "port": 80, "vendor": "hikvision", "model": "h"}]}))

    r3 = asyncio.run(E._handle_scan_wait(s, "t3", uid, "", "Sam"))
    assert "Hikvision" in r3["response"] and "192.168.1.64" in r3["response"]
    assert s["phase"] == E.SetupPhase.SCAN_RESULTS.value

    async def probe_ok(url):
        return True, base64.b64encode(b"\xff\xd8fake" * 100).decode()
    monkey = type("M", (), {})()
    E._probe_ip_camera = probe_ok

    r4 = asyncio.run(E._handle_scan_results(s, "t3", uid, "la 1", "Sam"))
    assert "usuario y clave" in r4["response"].lower()

    r5 = asyncio.run(E._handle_scan_results(s, "t3", uid, "admin clave123", "Sam"))
    assert "registrada" in r5["response"].lower()
    assert s["phase"] == E.SetupPhase.CONTEXT.value  # vuelve al flujo normal

    cam_json = tmp_path / "users" / uid / "cameras" / "IPCAM-192-168-1-64" / "camera.json"
    cfg = json.loads(cam_json.read_text())
    assert cfg["snapshot_url"].startswith("http://admin:clave123@192.168.1.64")
    assert cfg["pending_gateway"] is True and cfg["enabled"] is False
    assert stat.S_IMODE(os.stat(cam_json).st_mode) == 0o600


def test_f3_credenciales_invalidas_reintenta(tmp_path):
    uid = "u_f3b"
    _mk_user(tmp_path, uid)
    s = {"session_id": "t4", "user_id": uid, "msgs": [],
         "phase": E.SetupPhase.SCAN_RESULTS.value,
         "scan_devices": [{"ip": "10.1.1.5", "port": 80, "vendor": "tapo"}],
         "pending_cam_choice": [{"ip": "10.1.1.5", "port": 80, "vendor": "tapo"}]}
    E._sessions["t4"] = s

    async def probe_bad(url):
        return False, ""
    E._probe_ip_camera = probe_bad

    r = asyncio.run(E._handle_scan_results(s, "t4", uid, "admin mala", "Sam"))
    assert "revisa" in r["response"].lower()
    assert "pending_cam_choice" in s  # sigue esperando credenciales correctas


def test_f3_sin_esp32_guia_instalacion(tmp_path):
    uid = "u_f3c"
    _mk_user(tmp_path, uid, with_esp=False)
    s = {"session_id": "t5", "user_id": uid, "msgs": [], "phase": "done"}
    r = asyncio.run(E._handle_scan_consent(s, "t5", uid, "escanear mi red", "Sam"))
    assert "cámara ojoia conectada" in r["response"].lower()


def test_f3_scan_intent_variants():
    assert E._is_scan_intent("quiero escanear mi red")
    assert E._is_scan_intent("busca mis camaras ip")
    assert not E._is_scan_intent("hola eva cómo estás")
