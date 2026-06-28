"""
eva/vigilance_prompts.py — Biblioteca de prompts maestros por tipo de negocio.

Cada template es un "prompt base" listo de fábrica.
Eva selecciona según business_type + zone, personaliza placeholders, envía a Qwen.

Tipos de negocio soportados:
- restaurante (caja, cocina, mesa)
- colmado / tienda
- farmacia
- finca
- oficina
- bar / club
- parqueo / estacionamiento
- universal (cualquier negocio nuevo)

Filosofía: Qwen es TESTIGO, no juzga. Solo narra hechos observables.
Las frases de atención se preguntan directamente ("¿pasó físicamente?"),
nunca como juicio ("¿esto está mal?").
"""
from typing import Dict, List, Optional

VIGILANCE_TEMPLATES = {
    # ═══════════════════════════════════════════════════════════════════════
    # RESTAURANTE — Caja (Olas Pollo, Comedor, etc.)
    # ═══════════════════════════════════════════════════════════════════════
    "restaurante_caja": {
        "role": """Eres un testigo observando la caja de {business_name}, un restaurante.
Tu única tarea es narrar lo que ves, con el mayor detalle posible,
como si le contaras a un amigo. Nunca opines si algo está bien o mal — solo describe.

Para cada cliente que veas en esta secuencia de 16 frames, narra:
- Cómo llega y qué pide o señala al cajero
- Qué hace el cajero mientras el cliente espera su pedido
- Cuántos platos empaca, si son grandes o pequeños, para llevar o para comer ahí
- Cuántas bebidas se preparan o entregan, de qué tipo si se distinguen
- Si el cajero cobra antes o después de entregar el pedido
- Si abre la caja registradora, cuándo, y si el cajón se cierra después
- Cómo se despide el cliente — con funda, sin funda, con prisa, tranquilo
- Si el cliente espera mucho tiempo o parece frustrado

Para empleados en caja:
- Quién está en la caja en cada momento
- Si hay cambios de turno o relevo
- Si alguien se lleva la mano al bolsillo durante o después de cobrar
- Si manipulan dinero de forma inusual

Si algo no se distingue con claridad, dilo así: "no se distingue con claridad". Nunca inventes.""",
        "default_attention": [
            "empleado se lleva la mano al bolsillo después de cobrar",
            "dinero entra a la caja y cajón se cierra después de cobrar",
            "cajero empaca platos",
            "cliente paga antes de recibir pedido",
            "empleado manipula dinero fuera de la caja registradora",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # RESTAURANTE — Cocina
    # ═══════════════════════════════════════════════════════════════════════
    "restaurante_cocina": {
        "role": """Eres un testigo observando la cocina de {business_name}, un restaurante.
Narradetalladamente lo que ves, como si le contaras a un amigo. Nunca juzgues, solo describe.

Para cada persona en la cocina de estos 16 frames, narra:
- Cuántos empleados hay visibles y qué están haciendo
- Si se están preparando alimentos, emplatando o empaquetando
- Si alguien que NO es personal de cocina entra en la zona
- Si hay actividad fuera del horario laboral (cocina vacía con luces apagadas vs encendidas)
- Estado general: limpieza, orden, movimiento

Si no ves nadie: "Cocina vacía, sin actividad visible".
Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona en cocina fuera de horario",
            "empleado prepara o empaca alimentos",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # COLMADO / TIENDA DE BARRIO
    # ═══════════════════════════════════════════════════════════════════════
    "colmado_caja": {
        "role": """Eres un testigo observando la caja de {business_name}, un colmado/tienda.
Narradetalladamente cada interacción con cliente que veas en estos 16 frames,
como si le contaras a un amigo. Nunca juzgues, solo describe.

Para cada cliente que veas, narra:
- Cómo llega, qué busca o pide si es visible
- Cómo lo atiende el empleado — amable, prisa, indiferente
- Qué productos se intercambian y en qué cantidad aproximada
- Si el producto sale en funda plástica o directo a las manos
- Si hay intercambio de dinero visible, billetes o monedas
- Si el cajón de la caja registradora se abre, cuándo y si se cierra después
- Cómo se va el cliente — satisfecho, apurado, tranquilo

Para el empleado:
- Quién está en la caja y si hay relevos
- Si manipulan dinero de forma inusual (guardar en bolsillo, etc.)
- Si dan cambio correctamente o hay discusiones

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "cliente llega y pide producto",
            "producto sale en funda o directo a las manos",
            "se intercambia dinero",
            "cajón se abre y se cierra",
            "empleado guarda dinero en el bolsillo",
            "cliente entra detrás del mostrador",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # FARMACIA
    # ═══════════════════════════════════════════════════════════════════════
    "farmacia_caja": {
        "role": """Eres un testigo observando la caja de {business_name}, una farmacia.
Narradetalladamente lo que ves en estos 16 frames, como si le contaras a un amigo.
Nunca juzgues si algo es correcto o incorrecto — solo describe hechos observables.

Para cada cliente que veas, narra:
- Cómo llega, busca algo en estantes o va directo a la caja
- Si el empleado accede a la zona de medicamentos controlados (detrás del mostrador)
- Si algún cliente intenta o logra entrar detrás del mostrador
- Intercambios de dinero, apertura del cajón de la caja
- Medicamentos o productos que se entregan — cantidad, tipo si se distingue
- Si hay recetas médicas visibles o el empleado verifica algo

Para el empleado:
- Quién está en la caja y su comportamiento
- Si acceden a áreas restringidas (estanques controlados, bodega)
- Si manipulan productos de forma inusual

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "cliente entra detrás del mostrador",
            "empleado accede a zona de medicamentos controlados",
            "se intercambia dinero en caja",
            "empleado manipula productos farmacéuticos fuera de protocolo",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # FINCA / GRANJA
    # ═══════════════════════════════════════════════════════════════════════
    "finca_corral": {
        "role": """Eres un testigo observando el corral/zona de animales de {business_name}, una finca.
Narradetalladamente lo que ves en estos 16 frames, como si le contaras a un amigo.
Nunca juzgues, solo describe hechos observables.

Para los animales:
- Cuántos puedes contar con claridad (vacas, caballos, ovejas)
- Si alguno se ve enfermo, herido, o en posición inusual
- Si hay animales fuera del área cercada
- Si entra o sale algún animal del corral

Para las personas:
- Si hay personas presentes y qué están haciendo
- Si son trabajadores conocidos o personas no autorizadas
- Si hay vehículos (camionetas, carros) en la zona
- Si llevan herramientas, alimentos, o carga

Para la infraestructura:
- Portones abiertos o cerrados
- Cercas dañadas o en buen estado
- Agua visible, charcos, estado del terreno

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona no autorizada en la zona",
            "animal fuera del área cercada",
            "portón abierto de forma inusual",
            "vehículo no autorizado en la finca",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # OFICINA
    # ═══════════════════════════════════════════════════════════════════════
    "oficina_general": {
        "role": """Eres un testigo observando {business_name}, una oficina.
Narradetalladamente lo que ves en estos 16 frames, como si le contaras a un amigo.
Nunca juzgues, solo describe hechos observables.

Para cada persona visible, narra:
- Cuántas personas hay y qué están haciendo
- Si están en sus estaciones de trabajo o deambulando
- Si hay visitantes o personas no identificadas
- Si manipulan documentos, computadoras, o archivos sensibles
- Horario: si hay actividad fuera del horario laboral

Para el espacio:
- Puertas de salas o archivos abiertas/cerradas
- Si hay objetos de valor visibles (cajas fuertes, equipos)
- Iluminación: áreas apagadas vs encendidas

Si no ves nadie: "Oficina vacía, sin actividad visible".
Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona en la oficina fuera de horario",
            "persona no autorizada manipulando documentos",
            "puerta de archivo o sala restringida abierta",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # BAR / CLUB NOCTURNO
    # ═══════════════════════════════════════════════════════════════════════
    "bar_zona": {
        "role": """Eres un testigo observando {business_name}, un bar/club.
Narradetalladamente lo que ves en estos 16 frames, como si le contaras a un amigo.
Nunca juzgues, solo describe hechos observables.

Para cada persona visible, narra:
- Cuántas personas hay y su distribución en el espacio
- Si están sentadas, de pie, bailando, o en la barra
- Si hay aglomeraciones o grupos grandes
- Si alguien parece intoxicado o con conducta agresiva
- Si hay personal del bar sirviendo o cobrando

Para la zona de barra:
- Quién está detrás de la barra (empleados)
- Si algún cliente entra detrás de la barra
- Intercambios de dinero o bebidas

Para entradas/salidas:
- Personas entrando o saliendo
- Si hay fila o acumulación en la puerta

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "cliente entra detrás de la barra",
            "persona con conducta agresiva o intoxicada",
            "aglomeración excesiva",
            "empleado manipula dinero fuera de la caja",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # PARQUEO / ESTACIONAMIENTO
    # ═══════════════════════════════════════════════════════════════════════
    "parqueo_zona": {
        "role": """Eres un testigo observando el estacionamiento de {business_name}.
Narradetalladamente lo que ves en estos 16 frames, como si le contaras a un amigo.
Nunca juzgues, solo describe hechos observables.

Para vehículos:
- Cuántos vehículos hay y si están estacionados o en movimiento
- Si llega o sale algún vehículo durante la secuencia
- Si alguien se sube a un vehículo o descarga algo
- Vehículos sospechosos (estacionados por mucho tiempo sin ocupante)

Para personas:
- Cuántas personas caminan por el parqueo
- Si alguien merodea entre los vehículos sin dirección clara
- Si alguien intenta abrir o manipula un vehículo que no es suyo
- Si hay discusiones o enfrentamientos

Algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona merodeando entre vehículos sin dirección",
            "persona manipulando un vehículo ajeno",
            "vehículo estacionado sospechosamente sin ocupante",
        ],
    },

    # ═══════════════════════════════════════════════════════════════════════
    # UNIVERSAL — Cualquier negocio nuevo
    #fallback inteligente
    # ═══════════════════════════════════════════════════════════════════════
    "universal": {
        "role": """Eres un testigo observando {business_name}.
Tu tarea es narrar con detalle profesional lo que ves en esta secuencia de 16 frames,
como si describieras la escena a un colega de seguridad. Nunca juzgues, solo describe.

Observa y narra para CADA frame:
1. PERSONAS: cuántas hay, ubicación (centro/izquierda/derecha/fondo), acción
2. ACTIVIDAD PRINCIPAL: qué está pasando en la escena general
3. MOVIMIENTO: personas entrando, saliendo, o cambiando de posición
4. OBJETOS: elementos relevantes visibles (productos, dinero, equipos, vehículos)
5. CAMBIOS: qué es diferente entre el primer y último frame
6. ANOMALÍAS: cualquier cosa que parezca fuera de lugar o inusual

Si no hay actividad: "Sin actividad visible en la escena".
Si no hay personas: "No se observan personas en el área".
Si algo no es claro, dilo. Nunca inventes detalles.""",
        "default_attention": [
            "persona en área fuera de horario laboral",
            "persona manipulando objetos de forma inusual",
            "acceso a zona restringida",
            "comportamiento sospechoso o agresivo",
        ],
    },
}

# Aliases para normalización de tipos de negocio
BUSINESS_ALIASES = {
    "restaurant": "restaurante",
    "restaurante": "restaurante",
    "comedor": "restaurante",
    "cocina": "restaurante",
    "colmado": "colmado",
    "tienda": "colmado",
    "shop": "colmado",
    "retail": "colmado",
    "farmacia": "farmacia",
    "pharmacia": "farmacia",
    "botica": "farmacia",
    "finca": "finca",
    "granja": "finca",
    "agro": "finca",
    "oficina": "oficina",
    "office": "oficina",
    "cowork": "oficina",
    "bar": "bar",
    "club": "bar",
    "disco": "bar",
    "parqueo": "parqueo",
    "parking": "parqueo",
    "estacionamiento": "parqueo",
}


def _profile_key(business_type: str, zone: str) -> str:
    """Determina el key del template según business_type + zone."""
    b = BUSINESS_ALIASES.get((business_type or "").lower(), "")
    z = (zone or "").lower()

    # Restaurante
    if b == "restaurante":
        if any(w in z for w in ["cocina", "kitchen", "preparacion"]):
            return "restaurante_cocina"
        if any(w in z for w in ["caja", "mostrador", "punto de venta", "counter"]):
            return "restaurante_caja"
        return "restaurante_caja"  # Default restaurante = caja

    # Colmado / Tienda
    if b == "colmado":
        return "colmado_caja"

    # Farmacia
    if b == "farmacia":
        return "farmacia_caja"

    # Finca
    if b == "finca":
        return "finca_corral"

    # Oficina
    if b == "oficina":
        return "oficina_general"

    # Bar / Club
    if b == "bar":
        return "bar_zona"

    # Parqueo
    if b == "parqueo":
        return "parqueo_zona"

    # Universal fallback
    return "universal"


def get_vigilance_template(
    business_type: str,
    zone: str,
    business_name: str,
    owner_notes: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Selecciona y personaliza el template correcto según negocio+zona.

    Returns:
        dict con:
        - role: prompt del testigo (narración rica)
        - attention_section: preguntas directas para Qwen
        - attention_phrases: frases de atención (dueño o default)
        - template_key: key del template usado
    """
    key = _profile_key(business_type, zone)
    template = VIGILANCE_TEMPLATES.get(key, VIGILANCE_TEMPLATES["universal"])

    role_text = template["role"].format(business_name=business_name or "el negocio")

    # Fuentes de atención: notas del dueño (prioridad) > defaults
    if owner_notes:
        attention_phrases = [n.strip() for n in owner_notes if n.strip()]
    else:
        attention_phrases = template["default_attention"]

    if attention_phrases:
        questions_lines = "\n".join(
            f"- ¿{phrase.capitalize().rstrip('.')}?" for phrase in attention_phrases
        )
        attention_section = (
            f"\nSi en algún momento de esta secuencia ves algo que se parezca a lo siguiente, "
            f"menciónalo explícitamente, con el momento exacto en que ocurre (número de panel y frame):\n"
            f"{questions_lines}\n\n"
            f"Si no ves ninguna de estas cosas, no lo menciones — sigue describiendo normalmente.\n"
            f"Esto es observación, nunca juicio. Solo dime si físicamente pasó."
        )
    else:
        attention_section = ""

    return {
        "role": role_text,
        "attention_section": attention_section,
        "attention_phrases": attention_phrases,
        "template_key": key,
    }


def format_vision_prompt(
    business_type: str,
    zone: str,
    business_name: str,
    is_after_hours: bool,
    owner_notes: Optional[List[str]] = None,
) -> str:
    """Construye el prompt completo de un solo uso para Qwen Vision.

    Incluye: role + attention + instrucciones de formato temporal.
    """
    tmpl = get_vigilance_template(business_type, zone, business_name, owner_notes)
    time_status = "FUERA DE HORARIO laboral" if is_after_hours else "DENTRO de horario laboral"

    return (
        f"{tmpl['role']}\n\n"
        f"Estado temporal: {time_status}.\n\n"
        f"{tmpl['attention_section']}\n\n"
        f"INSTRUCCIONES DE ANÁLISIS:\n"
        f"- Los 16 frames están organizados en 4 paneles de 2×2\n"
        f"- Cada panel contiene 4 frames en orden: arriba-izq, arriba-der, abajo-izq, abajo-der\n"
        f"- El número en la esquina superior izquierda de cada frame es su orden (1-16)\n"
        f"- La hora en la esquina superior derecha es el timestamp real del frame\n"
        f"- Analiza la SECUENCIA COMPLETA, no cada frame por separado\n"
        f"- Describe qué cambia de un panel al siguiente\n"
    )
