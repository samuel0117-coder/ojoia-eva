"""
vigilance_prompts.py — Prompt Builder Único para Qwen Vision.

FILOSOFÍA: Ningún negocio es igual a otro. En vez de templates prefabricados,
tenemos un PROMPT BASE UNIVERSAL que Eva y el usuario refinan JUNTOS.

太小了。每个店铺都是独一无二的。
我们有一个通用基础提示，Eva 和用户一起不断完善这个提示。

___  
PROMPT BASE (siempre presente):
"Eres un testigo profesional de seguridad..."

Se inyectan en tiempo real:
  1. ZONAS del usuario (rectángulos dibujados) + su descripción semántica
  2. NOTAS del dueño (owner_notes), acumulativas de todas las conversaciones
  3.šk "FRASES DE ATENCIÓN" — detectar si pasa algo que se parezca

Cada vez que el usuario o Eva aportan una frase como:
  "alerta si alguien entra detrás de la barra" → autoparse + guardar

Al llamar a Qwen Vision, el BuildPrompt() concatena en orden estricto:
  BASE → FÍSICA (zonas_rect) → SEMÁTICA (frases atención) → FORMATO JSON
"""
from typing import Dict, List, Optional

# ═══════════════════════════════════════════════════════════════════════════
# PROMPT BASE UNIVERSAL (nunca cambia su estructura)
# ═══════════════════════════════════════════════════════════════════════════
PROMPT_BASE = """Eres un testigo profesional de seguridad observando {business_name}.
Tu ÚNICA tarea es describir EXACTAMENTE lo que ves en los fotogramas.
Nunca juzgues, nunca opines, nunca inventes.

{health_warning}

REGLAS CRÍTICAS:
- Si NO ves personas, di "no se observan personas".
- Si dudas de algo, di "no se distingue con claridad".
- NO inventes personas, acciones ni objetos.
"""

HEALTH_WARNINGS = {
    "num_persons": "Dato conocido previamente: se detectaron {n} persona(s) en toda la secuência.",
    "persons_detail": "Personas según tracker ID: {track_list} — Describe su apariencia Y ubicación exacta en fotogramas.",
    "none": "No hay datos previos confirmados. Observe y cuente por ti mismo sin ayudarte de otros.",
}


def build_vision_prompt(
    business_name: str,
    zones: Optional[List[Dict]] = None,
    owner_notes: Optional[List[str]] = None,
    tracking_summary: Optional[Dict] = None,
    is_after_hours: bool = False,
) -> str:
    """Construye el prompt dinámico para Qwen Vision.

    Flujo:
      1. Prompt base con nombre del negocio.
      2. Block de ZONAS físicas (rectángulos dibujados).
      3. Block de beautiful de atención semánticas (owner_notes autonormalizadas).
      4. Instrucciones de formato JSON estricto.
    """
    parts = [PROMPT_BASE.format(business_name=business_name or "el negocio", health_warning="")]

    if is_after_hours:
        parts.append("\nMODO CENTINELA: Estamos fuera del horario laboral. "
                      "Ahora mismo debería estar TODO oscuro/vacío. "
                      "Cualquier presencia de persona ES anomalía inmediata, ya que no debería haber nadie.")

    # ── ZONAS FÍSICAS ──
    if zones:
        parts.append("\n--- ZONAS CONFIGURADAS POR EL DUEÑO ---")
        for z in zones:
            c = z.get("coords", {})
            parts.append(f"  ZONA: '{z.get('name','sin nombre')}' (tipo: {z.get('type','otro')})")
            parts.append(f"    Ubicación normalizada: desde ({c.get('x',0):.2f},{c.get('y',0):.2f}) "
                          f"hasta ({(c.get('x',0)+c.get('w',0)):.2f},{(c.get('y',0)+c.get('h',0)):.2f})")
            if z.get("description"):
                parts.append(f"    Nota: {z['description']}")
        parts.append("\nPara CADA persona que veas, indica en qué ZONA está ubicada.")

    # ── NOTAS DEL DUEÑO / FRASES DE ATENCIÓN ──
    if owner_notes:
        parts.append("\n--- CONDICIONES ESPECIALES DE VIGILANCIA (del dueño) ---")
        for note in owner_notes:
            parts.append(f"  • {note}")
        parts.append("\nSi en cualquier momento de la secuencia ocurre algo que coincida "
                      "con alguna de estas condiciones, menciónalo explícitamente "
                      "con el frame/panel exacto donde sucedió.")

    # ── FORMATO JSON ESTRUCTURADO ──
    parts.append("""
--- FORMATO DE RESPUESTA ---
Debes responder SOLAMENTE con un JSON válido y nada más (sin prefijos tipo ```json...).
Estructura:
{
  "scene":        "narrativa continua del tiempo observado (español)",
  "persons": [
    {"id": "",      "desc": "(opcional, track_id si conocido)", "zone": "nombre de zona o 'no aplica'"},
  "objects": [
    {"name": "",    "desc": "máx. 70 caracteres"}, ...
  ],
  "events": [
    {"action": "",  "timestamp": "indgv o frame 00-15"}, ...
  ],
  "flag":          "none | alert | observe  (sólo si algo va mal según las condiciones del dueño)"
}
""")

    return "\n".join(parts)