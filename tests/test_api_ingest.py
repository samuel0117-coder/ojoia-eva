"""Sprint E1 — Tests de api_eva: ingest key (A4), rate limit (C4), user.json (C1)."""
import json
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_eva as A


# ─────────────────────────────────────────────────────────────────────────────
# A4 — X-Camera-Key
# ─────────────────────────────────────────────────────────────────────────────

class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.client = None


def test_a4_legado_sin_key_pasa():
    A._enforce_ingest_key(_Req(), {}, "cam_legacy")  # no lanza


def test_a4_sin_header_rechazado():
    with pytest.raises(A.HTTPException) as e:
        A._enforce_ingest_key(_Req(), {"ingest_key": "k123"}, "cam")
    assert e.value.status_code == 401


def test_a4_key_incorrecta_rechazada():
    with pytest.raises(A.HTTPException) as e:
        A._enforce_ingest_key(_Req({"x-camera-key": "mala"}), {"ingest_key": "k123"}, "cam")
    assert e.value.status_code == 401


def test_a4_key_correcta_pasa():
    A._enforce_ingest_key(_Req({"x-camera-key": "k123"}), {"ingest_key": "k123"}, "cam")


# ─────────────────────────────────────────────────────────────────────────────
# C4 — rate limit por cámara
# ─────────────────────────────────────────────────────────────────────────────

def test_c4_rate_limit_default():
    assert A._ingest_rate_ok("cam_rl", {})
    assert not A._ingest_rate_ok("cam_rl", {})
    time.sleep(0.21)
    assert A._ingest_rate_ok("cam_rl", {})


def test_c4_custom_fps():
    assert A._ingest_rate_ok("cam_rl2", {"max_fps": 1})
    assert not A._ingest_rate_ok("cam_rl2", {"max_fps": 1})


def test_c4_config_basura_no_rompe():
    assert A._ingest_rate_ok("cam_rl3", {"max_fps": "basura"})


import time  # noqa: E402  (lo usa test_c4)


# ─────────────────────────────────────────────────────────────────────────────
# C1 — update_user_json: sin pérdidas con concurrencia
# ─────────────────────────────────────────────────────────────────────────────

def test_c1_concurrencia_sin_perdidas(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "STORAGE_ROOT", tmp_path)
    d = tmp_path / "users" / "u_c1"
    d.mkdir(parents=True)
    (d / "user.json").write_text(json.dumps({"counter": 0}))
    monkeypatch.setattr(A, "find_user_json", lambda u: d / "user.json")

    errors = []

    def work():
        try:
            for _ in range(20):
                A.update_user_json("u_c1", lambda ud: ud.__setitem__(
                    "counter", ud.get("counter", 0) + 1))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    final = json.loads((d / "user.json").read_text())
    assert final["counter"] == 120
