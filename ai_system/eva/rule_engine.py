"""
Eva v12 — Motor de Reglas Inteligente
Qwen analiza imagen real + contexto del negocio para generar reglas personalizadas.
Cada regla incluye: descripción (ES) + pregunta de escaneo (EN).
"""
from typing import List, Dict, Any, Optional


# ── Categorías de mapeo de preocupación ──
CONCERN_CATEGORIES = {
    "robo_empleado": ["empleado", "trabajador", "interno", "ellos", "robo interno", "cobrar de más", "desviar"],
    "robo_cliente": ["cliente", "persona", "alguien", "shoplifter", "hurtar"],
    "despacho_sin_factura": ["despacho sin factura", "sin factura", "sin registrar", "no factura", "no registrar", "despachar sin"],
    "horario": ["horario", "después", "despues", "noche", "madrugada", "cerrado", "fuera de horario"],
    "acceso_no_autorizado": ["acceso", "entrada", "intruso", "desconocido", "restringido", "no autorizado"],
    "violencia": ["violencia", "arma", "pelea", "agresión", "amenaza"],
    "vandalismo": ["vandalismo", "daño", "romper", "destruir"],
}


def match_concern(concern: str) -> str:
    c = concern.lower()
    for category, keywords in CONCERN_CATEGORIES.items():
        if any(kw in c for kw in keywords):
            return category
    return "robo_empleado"  # default


def suggest_rules(
    business_type: str,
    concern: str,
    scene_desc: str,
    zone: str,
    confirmed_rules: List[Dict[str, str]],
    max_rules: int = 3
) -> List[Dict[str, str]]:
    """
    Generar sugerencias de reglas.
    Retorna lista de {es, en} con las reglas sugeridas (sin las ya confirmadas).
    """
    category = match_concern(concern)
    already_confirmed = {r.get("es", "") for r in confirmed_rules}

    # Templates base por categoría + negocio
    base_templates = _get_base_templates(business_type, category)

    # Filtrar las ya confirmadas
    available = [t for t in base_templates if t["es"] not in already_confirmed]

    # Filtrar por lo que se ve en la imagen
    filtered = _filter_by_scene(available, scene_desc)

    # Si quedó muy poco, usar templates sin filtrar
    if len(filtered) < (max_rules - len(confirmed_rules)):
        filtered = available

    if not filtered:
        filtered = available

    return filtered[:max_rules]


def _get_base_templates(business_type: str, category: str) -> List[Dict[str, str]]:
    """Obtener templates base según tipo de negocio y categoría."""

    biz = business_type.lower().strip()

    # ── COLMADO / RETAIL / TIENDA ──
    if biz in ("colmado", "retail", "tienda", "supermercado", "abastecedora"):
        if category == "robo_empleado":
            return [
                {"es": "Todo despacho solo por el lado derecho del mostrador",
                 "en": "Is anyone dispensing products on the left side of the counter?"},
                {"es": "Ningún empleado detrás del mostrador fuera de horas",
                 "en": "Is any employee standing behind the counter outside business hours?"},
                {"es": "Toda entrega de producto debe pasar por la caja registradora",
                 "en": "Is every product transaction going through the cash register?"},
                {"es": "Alerta si alguien entra al almacén sin autorización",
                 "en": "Is anyone entering the storage area without authorization?"},
                {"es": "Alerta si hay personas después del horario de cierre",
                 "en": "Is there any person in this area after business hours?"},
            ]
        elif category == "robo_cliente":
            return [
                {"es": "Alerta si alguien sale sin pasar por la caja",
                 "en": "Is anyone leaving without passing through the checkout area?"},
                {"es": "Vigilar que todos los productos pasen por el escáner",
                 "en": "Are all items being scanned at the register?"},
                {"es": "Alerta si alguien lleva bolsas grandes hacia la salida",
                 "en": "Is anyone carrying large bags toward the exit?"},
            ]
        elif category == "despacho_sin_factura":
            return [
                {"es": "Todo despacho de producto debe pasar por la caja registradora",
                 "en": "Is every product being processed through the cash register?"},
                {"es": "Solo despacho por el lado designado del mostrador",
                 "en": "Is dispensing happening only from the designated side of the counter?"},
                {"es": "Ningún producto debe salir sin transacción registrada",
                 "en": "Is any product leaving without a registered transaction?"},
            ]
        elif category == "horario":
            return [
                {"es": "Alerta si hay personas después del horario de cierre",
                 "en": "Is there any person in this area after business hours?"},
                {"es": "Alerta si se detecta movimiento fuera de horario laboral",
                 "en": "Is there any movement outside business hours?"},
            ]

    # ── FARMACIA ──
    elif biz in ("farmacia", "pharmacia"):
        if category in ("robo_empleado", "robo_cliente", "robo"):
            return [
                {"es": "Toda entrega de medicamento debe pasar por caja",
                 "en": "Is every medication transaction going through the register?"},
                {"es": "Vigilar el acceso al estante de medicamentos controlados",
                 "en": "Is anyone accessing the controlled medication shelf?"},
                {"es": "Alerta si alguien toma medicamentos sin pasar por caja",
                 "en": "Is anyone taking medication without going through checkout?"},
            ]
        elif category == "horario":
            return [
                {"es": "Alerta si hay personas después del horario de cierre",
                 "en": "Is there any person after business hours?"},
                {"es": "Alerta si se abre la puerta principal fuera de horario",
                 "en": "Is the main door being opened outside business hours?"},
            ]

    # ── RESTAURANTE ──
    elif biz in ("restaurante", "restaurant", "cafetería", "cafeteria", "comedor"):
        if category in ("robo_empleado", "robo"):
            return [
                {"es": "Solo servir por el lado designado de la barra",
                 "en": "Is service happening only from the designated side of the counter?"},
                {"es": "Vigilar que todos los platos pasen por caja",
                 "en": "Are all dishes passing through the cash register?"},
                {"es": "Ningún empleado en la cocina después de cerrar",
                 "en": "Is any employee in the kitchen after closing time?"},
            ]
        elif category == "horario":
            return [
                {"es": "Alerta si hay personas en la cocina después de cerrar",
                 "en": "Is anyone in the kitchen area after closing time?"},
                {"es": "Alerta si se detecta movimiento en el comedor fuera de horario",
                 "en": "Is there movement in the dining area outside business hours?"},
            ]

    # ── OFICINA ──
    elif biz in ("oficina", "office"):
        if category in ("robo_empleado", "robo"):
            return [
                {"es": "Alerta si alguien lleva equipos fuera de horario",
                 "en": "Is anyone carrying equipment outside business hours?"},
                {"es": "Vigilar el acceso a áreas restringidas",
                 "en": "Is anyone accessing restricted areas?"},
                {"es": "Alerta si alguien toma documentos de áreas privadas",
                 "en": "Is anyone taking documents from private areas?"},
            ]
        elif category == "horario":
            return [
                {"es": "Alerta si hay personas después del horario laboral",
                 "en": "Is there any person in the office after business hours?"},
            ]

    # ── GENÉRICO para cualquier negocio ──
    if category in ("robo_empleado", "robo_cliente", "robo"):
        return [
            {"es": "Alerta si alguien toma objetos sin pagar",
             "en": "Is anyone taking items without paying?"},
            {"es": "Vigilar las áreas de salida y entrada",
             "en": "Monitor entry and exit areas for suspicious activity"},
            {"es": "Alerta si se detecta movimiento sospechoso",
             "en": "Is there any suspicious movement in the monitored area?"},
        ]
    elif category == "horario":
        return [
            {"es": "Alerta si hay personas después del horario de cierre",
             "en": "Is there any person in this area after business hours?"},
            {"es": "Alerta si se detecta movimiento fuera de horario laboral",
             "en": "Is there any movement outside business hours?"},
        ]
    elif category == "acceso_no_autorizado":
        return [
            {"es": "Alerta si hay personas en áreas restringidas",
             "en": "Is anyone in restricted areas?"},
            {"es": "Vigilar el acceso a zonas de almacenamiento",
             "en": "Monitor access to storage areas"},
        ]

    # Default genérico
    return [
        {"es": "Vigilar movimiento sospechoso en la zona",
         "en": "Is there any suspicious movement in the monitored area?"},
        {"es": "Alerta si hay personas en horario no laboral",
         "en": "Is there any person outside business hours?"},
        {"es": "Monitorear las 24 horas",
         "en": "Monitor the area 24/7 for any unusual activity"},
    ]


def _filter_by_scene(rules: List[Dict[str, str]], scene_desc: str) -> List[Dict[str, str]]:
    """Filtrar reglas que no tienen sentido dado lo que se ve en la imagen."""
    if not scene_desc:
        return rules

    scene = scene_desc.lower()
    filtered = []

    for rule in rules:
        es = rule["es"].lower()
        skip = False

        # Reglas de mostrador/caja solo si se ve
        if any(w in es for w in ["mostrador", "caja", "despacho", "servir", "barra", "escáner", "scanner"]):
            if not any(w in scene for w in ["mostrador", "caja", "counter", "register", "barra", "scanner", "escáner"]):
                skip = True

        # Reglas de almacén solo si se ve
        if any(w in es for w in ["almacén", "bodega", "estante", "gaveta", "storage"]):
            if not any(w in scene for w in ["almacén", "bodega", "estante", "gaveta", "shelf", "storage", "cabinet"]):
                skip = True

        # Reglas de cocina solo si se ve
        if any(w in es for w in ["cocina", "preparación", "kitchen", "prep"]):
            if not any(w in scene for w in ["cocina", "preparación", "kitchen", "prep"]):
                skip = True

        # Reglas de puerta/ventana solo si se ven
        if any(w in es for w in ["puerta", "ventana", "door", "window"]):
            if not any(w in scene for w in ["puerta", "ventana", "door", "window"]):
                skip = True

        # Reglas de equipo/servidor solo si se ven
        if any(w in es for w in ["equipo", "servidor", "computer", "server"]):
            if not any(w in scene for w in ["equipo", "computador", "servidor", "computer", "server"]):
                skip = True

        if not skip:
            filtered.append(rule)

    return filtered if filtered else rules


def build_scanner_question(confirmed_rules: List[Dict[str, str]]) -> str:
    """
    Construir la scanner_question para el sistema de vigilancia.
    Pregunta única en inglés que Qwen responde durante el monitoreo.
    """
    if not confirmed_rules:
        return "Is there any person in the monitored area?"

    questions = []
    for r in confirmed_rules:
        q = r.get("en", r.get("es", ""))
        if q and len(q) > 5:
            # Asegurar que termine con ?
            if not q.endswith("?"):
                q += "?"
            questions.append(q)

    if not questions:
        return "Is there any person in the monitored area?"
    if len(questions) == 1:
        return questions[0]
    if len(questions) == 2:
        q2 = questions[1][0].lower() + questions[1][1:]
        return f"{questions[0]} Or {q2}?"
    # 3 reglas
    q2 = questions[1][0].lower() + questions[1][1:] if questions[1].endswith("?") else questions[1][0].lower() + questions[1][1:] + "?"
    q3 = questions[2][0].lower() + questions[2][1:] if questions[2].endswith("?") else questions[2][0].lower() + questions[2][1:] + "?"
    return f"{questions[0]}, {q2}, or {q3}?"
