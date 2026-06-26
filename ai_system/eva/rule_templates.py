RULE_TEMPLATES = {
    "cash_theft": {
        "name_es": "Alerta si empleado mete dinero o productos en bolsillos cerca de la caja",
        "question": "Is any employee pocketing cash or merchandise near the register?",
        "yolo": ["person"],
        "cooldown_min": 5,
    },
    "counter_crossing": {
        "name_es": "Alerta si alguien cruza detrás del mostrador",
        "question": "Is anyone going behind the counter?",
        "yolo": ["person"],
        "cooldown_min": 3,
    },
    "after_hours": {
        "name_es": "Alerta si hay personas en la zona fuera del horario del negocio",
        "question": "Is there any person in this area outside business hours?",
        "yolo": ["person"],
        "cooldown_min": 1,
    },
    "storage_access": {
        "name_es": "Alerta si alguien entra al almacén o bodega",
        "question": "Is anyone entering the storage area?",
        "yolo": ["person"],
        "cooldown_min": 5,
    },
    "unauthorized_presence": {
        "name_es": "Alerta si hay persona no autorizada en zona restringida",
        "question": "Is there any unauthorized person in this restricted area?",
        "yolo": ["person"],
        "cooldown_min": 3,
    },
}


def match_rules(security_concern: str, zone: str, max_rules: int = 3) -> list:
    concern = security_concern.lower()
    matched = []

    if any(w in concern for w in ["robo", "robar", "roban", "robando", "ladron", "atraco", "dinero", "bolsillo", "efectivo"]):
        matched.append("cash_theft")

    if any(w in concern for w in ["mostrador", "caja", "cajero", "despachar", "despacho"]):
        matched.append("counter_crossing")

    if any(w in concern for w in ["horario", "después", "despues", "noche", "madrugada", "cerrado", "fuera"]):
        matched.append("after_hours")

    if any(w in concern for w in ["almacen", "almacén", "bodega", "inventario"]):
        matched.append("storage_access")

    if any(w in concern for w in ["empleado", "trabajador", "persona", "alguien", "autoriza", "restringid"]):
        if "unauthorized_presence" not in matched:
            matched.append("unauthorized_presence")

    if not matched:
        matched.append("counter_crossing")
        matched.append("after_hours")

    return matched[:max_rules]


def get_rule_template(rule_id: str) -> dict:
    tpl = RULE_TEMPLATES.get(rule_id, RULE_TEMPLATES["counter_crossing"])
    return {"id": rule_id, **tpl}
