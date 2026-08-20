
"""
eva/knowledge_base.py — Base de Conocimiento de Escenas para Eva Experta

Cada perfil de escena contiene:
- riesgos típicos
- checks visuales recomendados (con templates de prompt)
- ejemplos de buenas reglas específicas
- ángulos óptimos de cámara
- zonas ciegas comunes
"""

from typing import List, Dict, Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# VISUAL CHECK TEMPLATES — Plantillas de checks visuales reutilizables
# Cada check tiene: ID, descripción ES, pregunta EN, severidad, condición
# ─────────────────────────────────────────────────────────────────────────────

VISUAL_CHECK_TEMPLATES = {
    # === COLMADO / RETAIL / TIENDA ============================================
    "pocket_near_cash": {
        "category": "robo_interno",
        "es": "Empleado con manos en bolsillos cerca del dinero",
        "en": "Do you see any person moving their hand toward their own pocket or waist area while standing near the cash register, money, or valuable merchandise?",
        "en_detailed": "Look carefully at all frames. Focus on the lower body area of each person near the counter/cash area. Do you see any hand movement toward pockets, waistband, or hidden areas? This is a HIGH PRIORITY check.",
        "severity": "high",
        "roi_hint": "lower body of persons near counter/cash area",
        "yolo_trigger": "person",
        "confidence_threshold": 0.75,
        "business_types": ["colmado", "retail", "tienda", "supermercado", "farmacia"],
        "zones": ["caja", "mostrador", "counter"],
    },
    "product_without_bag": {
        "category": "control_despacho",
        "es": "Producto entregado al cliente sin funda o bolsa",
        "en": "When a product is being handed from an employee to a customer, does the product go directly into the customer's hands without being placed in a bag, funda, or packaging first?",
        "en_detailed": "Observe the handover moment between employee and customer. Look for: product → bag → customer (OK) vs product → direct to hands (VIOLATION). Check multiple frames to confirm.",
        "severity": "medium",
        "roi_hint": "handover area between employee and customer",
        "yolo_trigger": "person",
        "confidence_threshold": 0.7,
        "business_types": ["colmado", "retail", "tienda", "supermercado", "farmacia"],
        "zones": ["caja", "mostrador", "counter"],
    },
    "register_before_handover": {
        "category": "control_despacho",
        "es": "Empleado debe interactuar con la caja registradora antes de entregar producto",
        "en": "Before handing any product to a customer, does the employee interact with the cash register or POS system (press buttons, reach toward it, look at screen, open drawer)?",
        "en_detailed": "This is a TRANSACTION CHECK. Look for the sequence: employee → register interaction → product handover. If product is handed WITHOUT visible register interaction in preceding 2-3 frames, flag as violation.",
        "severity": "medium",
        "roi_hint": "cash register/POS area and employee hands",
        "yolo_trigger": "person",
        "confidence_threshold": 0.7,
        "business_types": ["colmado", "retail", "tienda", "supermercado", "farmacia"],
        "zones": ["caja", "mostrador", "counter"],
    },
    "cash_drawer_open_unattended": {
        "category": "robo_interno",
        "es": "Cajón de dinero abierto sin empleado cerca",
        "en": "Is the cash drawer open while no employee is standing within arm's reach of it?",
        "en_detailed": "Look at the cash drawer area across all frames. If drawer is visibly open and no person is within 1 meter for more than 3 consecutive frames, flag as violation.",
        "severity": "high",
        "roi_hint": "cash drawer area",
        "yolo_trigger": "person",
        "confidence_threshold": 0.8,
        "business_types": ["colmado", "retail", "tienda", "supermercado", "farmacia"],
        "zones": ["caja", "mostrador", "counter"],
    },
    "two_people_behind_counter": {
        "category": "control_acceso",
        "es": "Dos o más personas detrás del mostrador al mismo tiempo",
        "en": "Are there two or more people standing behind the counter or in the employee-only area at the same time?",
        "en_detailed": "Count the number of persons in the restricted employee area behind the counter. If count >= 2 for more than 2 frames, flag as violation.",
        "severity": "medium",
        "roi_hint": "area behind the counter",
        "yolo_trigger": "person",
        "confidence_threshold": 0.7,
        "business_types": ["colmado", "retail", "tienda", "supermercado", "farmacia", "restaurante"],
        "zones": ["caja", "mostrador", "counter", "cocina"],
    },
    "employee_outside_designated_area": {
        "category": "control_acceso",
        "es": "Empleado fuera de su área designada durante horario laboral",
        "en": "Is any employee visible in an area where they should not be during their shift?",
        "en_detailed": "Compare person positions against designated work zones. If employee is in storage, back room, or unauthorized area during shift hours, flag.",
        "severity": "low",
        "roi_hint": "full frame",
        "yolo_trigger": "person",
        "confidence_threshold": 0.6,
        "business_types": ["colmado", "retail", "tienda", "supermercado", "farmacia", "restaurante", "oficina"],
        "zones": ["caja", "mostrador", "counter", "almacen", "entrada"],
    },

    # === FINCA / AGRICULTURA ==================================================
    "person_in_pasture_after_hours": {
        "category": "intrusion",
        "es": "Persona en corral o pasto fuera de horario laboral",
        "en": "Is there any person visible in the pasture, corral, or animal area outside of working hours?",
        "en_detailed": "Working hours are {open_time} to {close_time}. Outside these hours, ANY person in animal areas is unauthorized. Flag immediately.",
        "severity": "critical",
        "roi_hint": "full frame",
        "yolo_trigger": "person",
        "confidence_threshold": 0.6,
        "business_types": ["finca", "agricultura", "granja"],
        "zones": ["corral", "pasto", "establo", "granja"],
    },
    "animal_out_of_enclosure": {
        "category": "control_animales",
        "es": "Animal fuera de su cercado o área designada",
        "en": "Do you see any animal (cow, horse, pig, etc.) outside of its fenced enclosure or designated area?",
        "en_detailed": "Compare animal positions against visible fence lines or enclosure boundaries. If animal is clearly outside, flag.",
        "severity": "medium",
        "roi_hint": "areas near fence lines and gates",
        "yolo_trigger": "cow",  # YOLO detecta cow, horse, sheep, etc.
        "confidence_threshold": 0.65,
        "business_types": ["finca", "agricultura", "granja"],
        "zones": ["corral", "pasto", "establo", "granja"],
    },
    "unauthorized_vehicle": {
        "category": "intrusion",
        "es": "Vehículo no autorizado en la finca",
        "en": "Is there any vehicle (car, truck, motorcycle) in the farm area that does not belong to authorized personnel?",
        "en_detailed": "Look for vehicles in restricted farm areas. Compare against known authorized vehicles if visible. Unknown vehicles during off-hours are HIGH severity.",
        "severity": "high",
        "roi_hint": "entry roads and parking areas",
        "yolo_trigger": "car",
        "confidence_threshold": 0.6,
        "business_types": ["finca", "agricultura", "granja", "almacen"],
        "zones": ["entrada", "camino", "estacionamiento", "corral"],
    },
    "employee_feeding_without_protocol": {
        "category": "protocolo",
        "es": "Empleado alimentando animales sin seguir protocolo",
        "en": "Is an employee feeding animals but not following the visible feeding protocol (wrong feed, wrong area, wrong time)?",
        "en_detailed": "This is a PROTOCOL check. Look for: correct feed type, designated feeding area, proper equipment. Flag deviations.",
        "severity": "low",
        "roi_hint": "feeding areas and troughs",
        "yolo_trigger": "person",
        "confidence_threshold": 0.7,
        "business_types": ["finca", "agricultura", "granja"],
        "zones": ["corral", "establo", "comedero"],
    },

    # === FARMACIA =============================================================
    "controlled_medication_access": {
        "category": "medicamentos_controlados",
        "es": "Acceso a estante de medicamentos controlados sin autorización",
        "en": "Is anyone accessing the controlled medication shelf or locked cabinet without visible authorization or supervisor presence?",
        "en_detailed": "Look for: locked cabinet being opened, person reaching for controlled substances, absence of supervisor during access. HIGH PRIORITY.",
        "severity": "critical",
        "roi_hint": "controlled medication shelf/cabinet area",
        "yolo_trigger": "person",
        "confidence_threshold": 0.8,
        "business_types": ["farmacia", "pharmacia"],
        "zones": ["caja", "mostrador", "estante_controlado"],
    },
    "customer_behind_counter": {
        "category": "control_acceso",
        "es": "Cliente detrás del mostrador",
        "en": "Is any customer or unauthorized person standing behind the pharmacy counter in the employee-only area?",
        "en_detailed": "The counter line divides customer area (front) from employee area (back). ANY person in the back area without employee uniform is a violation.",
        "severity": "high",
        "roi_hint": "area behind the counter",
        "yolo_trigger": "person",
        "confidence_threshold": 0.7,
        "business_types": ["farmacia", "pharmacia", "colmado", "retail"],
        "zones": ["caja", "mostrador", "counter"],
    },

    # === RESTAURANTE ==========================================================
    "kitchen_after_hours": {
        "category": "intrusion",
        "es": "Persona en cocina después de cerrar",
        "en": "Is there any person in the kitchen or food preparation area after closing time or outside business hours?",
        "en_detailed": "Kitchen is RESTRICTED after hours. ANY person in kitchen area after {close_time} is unauthorized. Flag immediately.",
        "severity": "critical",
        "roi_hint": "kitchen and food prep areas",
        "yolo_trigger": "person",
        "confidence_threshold": 0.6,
        "business_types": ["restaurante", "cafeteria", "comedor"],
        "zones": ["cocina", "preparacion", "kitchen"],
    },
    "food_without_ticket": {
        "category": "control_despacho",
        "es": "Plato o comida entregada sin ticket visible",
        "en": "Is any food dish or plate being handed to a customer without a visible order ticket or receipt attached?",
        "en_detailed": "Look for: ticket/receipt on plate or counter near dish before handover. No visible ticket = potential unregistered order.",
        "severity": "medium",
        "roi_hint": "counter and handover area",
        "yolo_trigger": "person",
        "confidence_threshold": 0.7,
        "business_types": ["restaurante", "cafeteria", "comedor"],
        "zones": ["caja", "mostrador", "counter", "cocina"],
    },

    # === GENÉRICOS / TODOS LOS NEGOCIOS =======================================
    "person_after_hours": {
        "category": "intrusion",
        "es": "Persona en el área fuera de horario laboral",
        "en": "Is there any person visible in the monitored area outside of business hours ({open_time} to {close_time})?",
        "en_detailed": "SENTINEL MODE: Outside business hours, ANY person is unauthorized. This triggers immediate alert without grid analysis.",
        "severity": "critical",
        "roi_hint": "full frame",
        "yolo_trigger": "person",
        "confidence_threshold": 0.5,
        "business_types": ["all"],
        "zones": ["all"],
    },
    "suspicious_loitering": {
        "category": "comportamiento",
        "es": "Persona merodeando o comportamiento sospechoso",
        "en": "Do you see any person loitering, repeatedly checking surroundings, or behaving suspiciously in the monitored area?",
        "en_detailed": "Look for: person standing in one spot for extended time, looking around nervously, checking doors/windows, avoiding cameras.",
        "severity": "medium",
        "roi_hint": "full frame",
        "yolo_trigger": "person",
        "confidence_threshold": 0.75,
        "business_types": ["all"],
        "zones": ["all"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# SCENE PROFILES — Perfiles de escena predefinidos
# Cada perfil combina checks recomendados + contexto experto
# ─────────────────────────────────────────────────────────────────────────────

SCENE_PROFILES = {
    "colmado_caja": {
        "business_type": "colmado",
        "zone": "caja",
        "description": "Cámara en caja registradora de colmado pequeño",
        "optimal_angle": "Vista frontal del mostrador, caja registradora visible, área de transacción clara",
        "common_blind_spots": ["debajo del mostrador", "atrás del cajero", "entrada lateral"],
        "recommended_checks": [
            "pocket_near_cash",
            "product_without_bag", 
            "register_before_handover",
            "cash_drawer_open_unattended",
            "two_people_behind_counter",
        ],
        "yolo_triggers": ["person"],
        "expert_context": """
Colmado típico dominicano: mostrador alto, caja registradora antigua o moderna,
productos variados (alimentos, bebidas, artículos de uso diario).
Principal riesgo: robo interno por empleados (despacho sin control, dinero).
Cliente típico: pide producto, empleado lo busca, cobra, entrega.
Secuencia normal: cliente → pide → empleado busca → pasa por caja → entrega en funda.
""",
    },

    "colmado_entrada": {
        "business_type": "colmado", 
        "zone": "entrada",
        "description": "Cámara en entrada principal del colmado",
        "optimal_angle": "Vista de frente a la puerta, área de entrada completa visible",
        "common_blind_spots": ["área justo afuera de la puerta", "laterales de la entrada"],
        "recommended_checks": [
            "person_after_hours",
            "suspicious_loitering",
            "unauthorized_vehicle",
        ],
        "yolo_triggers": ["person", "car", "motorcycle"],
        "expert_context": """
Entrada de colmado: control de acceso, detección de intrusos fuera de horario,
vehículos sospechosos. No se ven transacciones desde aquí.
Principal valor: modo sentinel fuera de horario.
""",
    },

    "finca_corral": {
        "business_type": "finca",
        "zone": "corral",
        "description": "Cámara en corral o área de animales",
        "optimal_angle": "Vista panorámica del corral, cercados visibles, puertas claras",
        "common_blind_spots": ["esquinas del corral", "área detrás de bebederos", "sombra de árboles"],
        "recommended_checks": [
            "person_in_pasture_after_hours",
            "animal_out_of_enclosure",
            "unauthorized_vehicle",
            "employee_feeding_without_protocol",
        ],
        "yolo_triggers": ["person", "cow", "horse", "sheep", "car", "truck"],
        "expert_context": """
Corral de finca: animales (vacas, caballos, cerdos), cercados, bebederos,
comedores. Horario laboral: empleados alimentan, limpian, revisan.
Fuera de horario: SOLO debería haber animales. Cualquier persona = intruso.
Principal riesgo: robo de animales, intrusión nocturna, animales fugados.
""",
    },

    "farmacia_caja": {
        "business_type": "farmacia",
        "zone": "caja",
        "description": "Cámara en caja de farmacia",
        "optimal_angle": "Vista del mostrador, caja registradora, estante de controlados visible",
        "common_blind_spots": ["estantes laterales", "área de preparación", "consultorio"],
        "recommended_checks": [
            "pocket_near_cash",
            "controlled_medication_access",
            "customer_behind_counter",
            "product_without_bag",
        ],
        "yolo_triggers": ["person"],
        "expert_context": """
Farmacia: mostrador con caja, estantes de medicamentos (algunos controlados),
clientes con recetas. Principal riesgo: hurto de medicamentos controlados,
cliente detrás del mostrador, despacho sin registro.
Diferencia con colmado: medicamentos controlados = mayor severidad.
""",
    },

    "restaurante_cocina": {
        "business_type": "restaurante",
        "zone": "cocina",
        "description": "Cámara en cocina de restaurante",
        "optimal_angle": "Vista de área de preparación, fogones, salida de platos",
        "common_blind_spots": ["área de lavado", "walk-in refrigerator", "almacén de ingredientes"],
        "recommended_checks": [
            "kitchen_after_hours",
            "food_without_ticket",
            "two_people_behind_counter",
        ],
        "yolo_triggers": ["person"],
        "expert_context": """
Cocina de restaurante: fogones, área de preparación, salida de platos.
Horario laboral: cocineros preparan, meseros recogen.
Fuera de horario: COCINA CERRADA. Cualquier persona = intruso crítico.
Principal riesgo: intrusión nocturna, robo de alimentos/equipo, incendio.
""",
    },
}


def get_scene_profile(business_type: str, zone: str) -> Optional[Dict[str, Any]]:
    """Obtener el perfil de escena más cercano para un negocio + zona."""
    biz = business_type.lower().strip()
    z = zone.lower().strip()

    # Normalizar aliases de negocio
    _biz_aliases = {
        "retail": "colmado",
        "tienda": "colmado",
        "supermercado": "colmado",
        "abastecedora": "colmado",
        "pharmacia": "farmacia",
        "restaurant": "restaurante",
        "cafeteria": "restaurante",
        "granja": "finca",
        "agricultura": "finca",
    }
    biz_normalized = _biz_aliases.get(biz, biz)

    key = f"{biz_normalized}_{z}"

    # Búsqueda exacta
    if key in SCENE_PROFILES:
        return SCENE_PROFILES[key]

    # Búsqueda parcial
    for profile_key, profile in SCENE_PROFILES.items():
        if biz_normalized in profile_key and z in profile_key:
            return profile

    # Fallback: buscar solo por business_type
    for profile_key, profile in SCENE_PROFILES.items():
        if profile.get("business_type") == biz_normalized:
            return profile

    return None


def get_checks_for_scene(business_type: str, zone: str, concern: str = "") -> List[Dict[str, Any]]:
    """Obtener checks visuales recomendados para una escena, filtrados por preocupación."""
    profile = get_scene_profile(business_type, zone)

    if not profile:
        # Fallback: checks genéricos
        return [
            VISUAL_CHECK_TEMPLATES["person_after_hours"],
            VISUAL_CHECK_TEMPLATES["suspicious_loitering"],
        ]

    checks = []
    for check_id in profile.get("recommended_checks", []):
        if check_id in VISUAL_CHECK_TEMPLATES:
            check = VISUAL_CHECK_TEMPLATES[check_id].copy()
            check["id"] = check_id
            checks.append(check)

    # Si hay preocupación específica, priorizar checks relacionados
    if concern:
        concern_lower = concern.lower()
        # Reordenar: checks cuya categoría coincide con la preocupación van primero
        def priority(c):
            cat = c.get("category", "")
            if any(word in concern_lower for word in ["robo", "dinero", "bolsillo"]):
                return 0 if cat in ["robo_interno", "control_despacho"] else 1
            if any(word in concern_lower for word in ["horario", "noche", "cerrado"]):
                return 0 if cat == "intrusion" else 1
            if any(word in concern_lower for word in ["cliente", "persona", "intruso"]):
                return 0 if cat in ["intrusion", "control_acceso"] else 1
            return 1
        checks.sort(key=priority)

    return checks


def get_yolo_triggers_for_scene(business_type: str, zone: str) -> List[str]:
    """Obtener triggers YOLO recomendados para una escena."""
    profile = get_scene_profile(business_type, zone)
    if profile:
        return profile.get("yolo_triggers", ["person"])
    return ["person"]


def build_expert_prompt_for_checks(checks: List[Dict[str, Any]], 
                                     business_type: str,
                                     zone: str,
                                     schedule: Dict[str, str],
                                     scene_analysis: str = "") -> str:
    """Construir el prompt EXPERTO de Qwen a partir de checks específicos."""

    checks_text = []
    for i, check in enumerate(checks, 1):
        checks_text.append(f"""
CHECK {i}: {check['id'].upper()}
Question: {check['en']}
Details: {check.get('en_detailed', check['en'])}
Severity: {check['severity'].upper()}
Focus area: {check.get('roi_hint', 'full frame')}
""")

    profile = get_scene_profile(business_type, zone)
    expert_context = profile.get("expert_context", "") if profile else ""

    prompt = f"""You are an expert security analyst monitoring a {business_type} in Dominican Republic.

=== SCENE CONTEXT ===
Zone: {zone}
Business hours: {schedule.get('open', '08:00')} to {schedule.get('close', '19:00')}
Current time: [AUTO_FILLED]

{expert_context}

=== SCENE ANALYSIS ===
{scene_analysis if scene_analysis else 'No detailed scene analysis available.'}

=== VISUAL CHECKS ===
Analyze the following {len(checks)} security checks across ALL frames in the grid.
For each check, answer with HIGH confidence only if you are CERTAIN.

{chr(10).join(checks_text)}

=== RESPONSE FORMAT ===
Respond ONLY with valid JSON:
{{
  "violation": true/false,
  "check_triggered": "id_of_check_or_null",
  "description": "Detailed description of what you observed (1-2 sentences)",
  "confidence": 0.0-1.0,
  "frames_with_evidence": [frame_numbers]
}}

Rules:
- violation=true ONLY if you are confident (confidence >= 0.7)
- If multiple checks trigger, report the HIGHEST severity one
- Be specific: mention clothing colors, positions, objects visible
- If unsure, respond violation=false with description "Inconclusive"
"""
    return prompt
