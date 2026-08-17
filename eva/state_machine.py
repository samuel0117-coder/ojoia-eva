"""eva/state_machine.py — OjoIA Eva v13"""
from enum import Enum


class EvaPhase(str, Enum):
    GREET       = "greet"        # Saludo + preguntar zona
    HARDWARE    = "hardware"     # Guía conexión cámara (sin LLM)
    WAIT_IMAGE  = "wait_image"   # Esperando primer frame
    ANALYZE     = "analyze"      # Eva ve la imagen y evalúa posición
    PROBLEM     = "problem"      # Captura preocupación + contexto del negocio (2-3 preguntas)
    RULES       = "rules"        # Qwen diseña 3 reglas + system_prompt (una sola llamada)
    REVIEW      = "review"       # Usuario acepta/rechaza/modifica cada regla individual
    CONFIRM     = "confirm"      # Resumen final + configuración
    DONE        = "done"         # Config guardada

# Palabras que confirman un sí
YES_WORDS = {
    "si","sí","dale","perfecto","exacto","correcto",
    "aprobado","bueno","bien","ok","vale","claro",
    "seguro","listo","ya","ajá","aha","de acuerdo",
    "me parece","está bien","eso es","sí señor","confirmar",
}

# Palabras que rechazan una propuesta
NO_WORDS = {
    "no","otra","cambiar","diferente","no me gusta",
    "no sirve","paso","otra opción","no esta bien",
}

# Palabras que indican que el hardware está listo
HARDWARE_DONE = {
    "ya","listo","conectad","encendid","terminé","termine",
    "hecho","prendido","prendió","wifi","internet","siguiente",
    "luz","led","azul","parpade","sigue","ok","bien",
}

import re as _re_sm

def _clean_msg(msg: str) -> str:
    """Limpiar emoji y espacios extra del mensaje."""
    m = msg.lower().strip()
    m = _re_sm.sub(r'[^\w\s,;:\-\(\)]', '', m).strip()
    return m

def is_yes(msg: str) -> bool:
    m = _clean_msg(msg)
    if not m:
        m = msg.lower().strip()
    return m in YES_WORDS or any(m.startswith(w) for w in YES_WORDS)

def is_no(msg: str) -> bool:
    m = _clean_msg(msg)
    if not m:
        m = msg.lower().strip()
    return m in NO_WORDS or any(m.startswith(w) for w in NO_WORDS)

def is_hardware_done(msg: str) -> bool:
    m = msg.lower()
    return any(w in m for w in HARDWARE_DONE)
