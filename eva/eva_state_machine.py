from enum import Enum


class EvaPhase(str, Enum):
    GREET = "greet"
    HARDWARE = "hardware"
    VISION = "vision"
    RULES = "rules"
    CONFIRM = "confirm"
    DONE = "done"


SECURITY_WORDS = [
    "robo", "roben", "robar", "robado",
    "dinero", "efectivo",
    "empleado", "empleada", "trabajador",
    "caja", "cajero",
    "ladron", "ladrones", "atraco",
    "bolsillo", "bolsillos",
    "mercancia", "producto", "productos",
    "inventario", "almacen", "almacén",
    "despachar", "despacho",
    "preocup", "preocupa", "miedo", "temor",
    "vigilar", "seguridad",
    "robando", "robaron", "robará",
    "meter", "mete", "metieron",
    "cobro", "cobrar",
]

YES_WORDS = [
    "si", "sí", "dale", "perfecto", "exacto",
    "correcto", "aprobado", "bueno", "bien",
    "ok", "vale", "claro", "seguro",
]

HARDWARE_DONE_WORDS = [
    "ya", "listo", "conectad", "encendid",
    "terminé", "termine", "hecho", "listo",
    "prendido", "prendió",
    "wifi", "wi-fi", "internet",
]


def detect_security_concern(message: str) -> bool:
    if not message:
        return False
    msg = message.lower()
    return any(w in msg for w in SECURITY_WORDS)


def detect_confirmation(message: str) -> bool:
    if not message:
        return False
    msg = message.lower().strip()
    return msg in YES_WORDS or any(msg.startswith(w) for w in YES_WORDS)


def detect_hardware_done(message: str) -> bool:
    if not message:
        return False
    msg = message.lower()
    return any(w in msg for w in HARDWARE_DONE_WORDS)


def detect_position_confirmation(message: str, image_available: bool) -> bool:
    if not image_available:
        return False
    if detect_confirmation(message):
        return True
    msg = message.lower()
    position_ok = ["se ve bien", "está bien", "esta bien", "correcto", "perfecto", "ahí", "ahí está", "listo"]
    return any(w in msg for w in position_ok)


def compute_next_phase(session: dict, message: str) -> str:
    phase = session["phase"]
    has_image = bool(session.get("image_b64"))
    rules_count = len(session.get("rules", []))

    if phase == EvaPhase.GREET:
        return EvaPhase.HARDWARE

    if phase == EvaPhase.HARDWARE:
        if has_image:
            return EvaPhase.VISION
        if detect_hardware_done(message):
            return EvaPhase.HARDWARE
        return EvaPhase.HARDWARE

    if phase == EvaPhase.VISION:
        if detect_position_confirmation(message, has_image):
            return EvaPhase.RULES
        if detect_security_concern(message):
            return EvaPhase.RULES
        return EvaPhase.VISION

    if phase == EvaPhase.RULES:
        if rules_count >= 3:
            return EvaPhase.CONFIRM
        return EvaPhase.RULES

    if phase == EvaPhase.CONFIRM:
        if detect_confirmation(message):
            return EvaPhase.DONE
        return EvaPhase.RULES

    return phase
