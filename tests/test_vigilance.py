"""Sprint E1 — Tests de regresión del pipeline de vigilancia.

Ejecutar: /opt/ojoia/venv/bin/python -m pytest tests/ -q
(cubre B1 keyword-candidatos, B2 verificador, B3 cooldown persistente,
B4 feedback loop, A4 ingest key, C1 user.json atómico, C4 rate limit)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import _detect_attention_hits, QwenOrchestrator  # noqa: E402
import orchestrator  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# B1 — keyword substring ya no es violación directa
# ─────────────────────────────────────────────────────────────────────────────

def test_b1_negacion_solo_candidato():
    vision = {"resumen": "No se observa que el empleado abra el cajon sin facturar."}
    res = _detect_attention_hits(vision, ["empleado abra el cajon sin facturar"],
                                 [], "caja", False, "normal", {})
    assert res["violation"] is True  # candidato existe
    cand = [h for h in res["hits_detail"] if h.get("needs_verification")]
    assert len(cand) == 1 and cand[0]["source"] == "keyword_match"


def test_b1_qwen_flag_no_requiere_verificacion():
    vision = {"resumen": "x", "flag": "empleado abre el cajon"}
    res = _detect_attention_hits(vision, [], [], "caja", False, "normal", {})
    assert any(h["source"] == "qwen_flag" and not h.get("needs_verification")
               for h in res["hits_detail"])


def test_b1_placeholder_schema_filtrado():
    vision = {"flag": "frase exacta de attention_phrases que detectaste cumplirse, o null",
              "resumen": "escena tranquila"}
    # frase que NO aparezca como substring del resumen/flag (evita keyword match)
    res = _detect_attention_hits(vision, ["robo de dinero en efectivo"], [], "c", False, "normal", {})
    assert res["violation"] is False


# ─────────────────────────────────────────────────────────────────────────────
# B2 — verificador de 2ª pasada
# ─────────────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, results):
        self._r = results
    def raise_for_status(self):
        pass
    def json(self):
        return {"choices": [{"message": {"content": json.dumps({"resultados": self._r})}}]}


class _FakeClient:
    def __init__(self, results=None, fail=False):
        self._r, self._fail = results or [], fail
    async def post(self, *a, **k):
        if self._fail:
            raise ConnectionError("down")
        return _FakeResp(self._r)


def _orch_with(client):
    o = QwenOrchestrator()
    async def _c():
        return client
    o._client = _c
    return o


def test_b2_verificador_rechaza_negacion():
    hits = [{"frase": "abre el cajon", "needs_verification": True, "source": "keyword_match"}]
    o = _orch_with(_FakeClient([{"regla": 1, "ocurrio": False}]))
    out = asyncio.run(o._verify_attention_candidates(hits, {"vision": {"scene": "no ocurre"}}, ""))
    assert out == []


def test_b2_verificador_confirma_real():
    hits = [{"frase": "abre el cajon", "needs_verification": True, "source": "keyword_match"},
            {"frase": "estructurado", "source": "qwen_flag"}]
    o = _orch_with(_FakeClient([{"regla": 1, "ocurrio": True, "evidencia": "lo abre"}]))
    out = asyncio.run(o._verify_attention_candidates(hits, {"vision": {"scene": "abre"}}, ""))
    assert out == ["estructurado", "abre el cajon"]


def test_b2_verificador_caido_conservador():
    hits = [{"frase": "dudosa", "needs_verification": True, "source": "keyword_match"},
            {"frase": "estructurada", "source": "qwen_explicit"}]
    o = _orch_with(_FakeClient(fail=True))
    out = asyncio.run(o._verify_attention_candidates(hits, {"vision": {}}, ""))
    assert out == ["estructurada"]


# ─────────────────────────────────────────────────────────────────────────────
# B3 — cooldown persistente por cámara+regla
# ─────────────────────────────────────────────────────────────────────────────

def test_b3_cooldown_persistent_y_por_regla(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "STORAGE_ROOT", str(tmp_path))
    o1, o2 = QwenOrchestrator(), QwenOrchestrator()
    kA = o1._cooldown_key("u", "cam", "regla A")
    kB = o1._cooldown_key("u", "cam", "regla B")
    assert kA != kB
    o1._cooldown_save(kA, time.time())
    # o2 simula reinicio (memoria vacía, disco compartido)
    assert o2._cooldown_get(kA) > 0
    assert o2._cooldown_get(kB) == 0


# ─────────────────────────────────────────────────────────────────────────────
# B4 — feedback loop: 3 falsas alarmas -> owner_note -> supresión
# ─────────────────────────────────────────────────────────────────────────────

def test_b4_feedback_loop(tmp_path, monkeypatch):
    import eva.tools as T
    monkeypatch.setattr(T, "STORAGE_ROOT", str(tmp_path))
    uid, cam = "u_b4", "cam_b4"
    ed = tmp_path / "users" / uid / "cameras" / cam / "events"
    ed.mkdir(parents=True)
    (ed / "evt_t.json").write_text(json.dumps(
        {"event_id": "evt_t", "camera_id": cam, "attention_hits": ["empleado toca la caja"]}))
    (ed.parent / "camera.json").write_text(json.dumps({"camera_id": cam, "vigilance": {}}))

    for _ in range(3):
        r = asyncio.run(T.tool_learn_from_feedback("evt_t", is_real=False, user_id=uid))
        assert r["success"]

    cfg = json.loads((ed.parent / "camera.json").read_text())
    notes = cfg["vigilance"]["owner_notes"]
    assert any("falso positivo" in n.lower() for n in notes)
    assert cfg["vigilance"]["false_alarm_counts"]["empleado toca la caja"] == 3

    res = _detect_attention_hits(
        {"resumen": "el empleado toca la caja otra vez"},
        ["empleado toca la caja"], notes, "caja", False, "normal", {})
    assert res["violation"] is False
