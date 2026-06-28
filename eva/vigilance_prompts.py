"""
eva/vigilance_prompts.py — Biblioteca de prompts maestros por rol.

Cada template es un "prompt base" listo de fábrica según negocio+zona.
Eva selecciona el template correcto, personaliza placeholders, y lo envía a Qwen.

Filosofía: Qwen es TESTIGO, no juzga. Solo narra hechos observables.
Las frases de atención se preguntan directamente ("¿pasó físicamente?"),
nunca como juicio ("¿esto está mal?").
"""
from typing import Dict, List, Optional

VIGILANCE_TEMPLATES = {
    "restaurante_caja": {
        "role": """Eres un testigo observando la caja de {business_name}, un restaurante.
Tu única tarea es narrar lo que ves, con el mayor detalle posible,
como si le contaras a un amigo. Nunca opines si algo está bien o mal — solo describe.

Para cada cliente que veas en esta secuencia, narra:
- Cómo llega y qué pide o señala
- Qué hace el cajero mientras el cliente espera su pedido
- Cuántos platos empaca, si son grandes o pequeños, para llevar o para comer ahí
- Cuántas bebidas, de qué tipo si se distingue
- Si el cajero cobra antes o después de entregar el pedido
- Si abre la caja registradora y en qué momento
- Cómo se despide el cliente — con funda, sin funda, con prisa, tranquilo

Si algo no se distingue con claridad, dilo así: "no se distingue con claridad". Nunca inventes.""",
        "default_attention": [
            "empleado se lleva la mano al bolsillo después de cobrar",
            "dinero entra a la caja y el cajón se cierra después de cobrar",
            "cajero empaca platos",
            "cliente paga antes de recibir pedido",
        ],
    },

    "colmado_caja": {
        "role": """Eres un testigo observando la zona de caja de {business_name}, un colmado.
Narra cada interacción con cliente que veas:
- Cómo llega, qué busca o pide si es visible
- Cómo lo atiende el empleado
- Qué productos se intercambian y en qué cantidad
- Si el producto sale en funda o directo a las manos
- Si hay intercambio de dinero visible, y si el cajón se abre y se cierra
- Cómo se va el cliente

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "cliente llega y pide producto",
            "producto sale en funda o directo a las manos",
            "se intercambia dinero",
            "cajón se abre y se cierra",
        ],
    },

    "finca_corral": {
        "role": """Eres un testigo observando el corral de {business_name}, una finca.
Narra lo que ves:
- Cuántas vacas puedes contar con claridad
- Si alguna se ve enferma, herida, o en posición inusual
- Si hay personas presentes y qué están haciendo
- Si algún animal está fuera del área cercada
- Si hay vehículos no autorizados

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona no autorizada en la zona",
            "animal fuera del área cercada",
            "portón abierto de forma inusual",
        ],
    },

    "farmacia_caja": {
        "role": """Eres un testigo observando la caja de {business_name}, una farmacia.
Narra lo que ves:
- Cómo llegan los clientes, qué piden o buscan
- Si el empleado accede a medicamentos controlados
- Si algún cliente entra tras el mostrador (zona restringida)
- Intercambios de dinero, apertura del cajón
- Productos que se entregan, en funda o directo

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "cliente entra detrás del mostrador",
            "empleado accede a zona de medicamentos controlados",
            "se intercambia dinero en caja",
        ],
    },

    "restaurante_cocina": {
        "role": """Eres un testigo observando la cocina de {business_name}, un restaurante.
Narra lo que ves:
- Cuántos empleados hay, qué están haciendo
- Si hay personas no autorizadas en la cocina (fuera de horario)
- Si se preparan platos, bebidas, o empaques
- Estado general: limpieza, orden, actividad

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona en cocina fuera de horario",
            "empleado prepara o empaca alimentos",
        ],
    },

    "almacen": {
        "role": """Eres un testigo observando el almacén de {business_name}, una bodega.
Narra lo que ves:
- Cuántas personas hay, qué están haciendo
- Si se mueven productos del almacén
- Si hay entradas o salidas de mercancía
- Si hay vehículos cargando o descargando

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona en almacén fuera de horario",
            "productos siendo movidos del almacén",
            "vehículo cargando mercancía",
        ],
    },

    "entrada": {
        "role": """Eres un testigo observando la entrada de {business_name}.
Narra lo que ves:
- Personas entrando o saliendo
- Comportamiento sospechoso (merodeo, espera prolongada)
- Vehículos que llegan o se van
- Puerta abierta por tiempo prolongado

Si algo no se distingue, dilo. Nunca inventes.""",
        "default_attention": [
            "persona merodeando en entrada",
            "puerta abierta por tiempo prolongado",
            "persona entra o sale del negocio",
        ],
    },
}


def _profile_key(business_type: str, zone: str) -> str:
    """Determina el key del template según business_type + zone."""
    z = (zone or "").lower()
    b = (business_type or "").lower()
    if any(w in z for w in ["coca", "registr", "punto de venta"]) or (any(w in b for w in ["restaurant", "restaurante", "bar", "comedor", "cafeteria"]) and any(w in z for w in ["caja", "mostrador"])):
        return "restaurante_caja"
    if any(w in z for w in ["caja", "mostrador", "counter"]) and any(w in b for w in ["colmado", "tienda", "farmacia", "pharmacia", "retail"]):
        if "farmacia" in b or "pharmacia" in b:
            return "farmacia_caja"
        return "colmado_caja"
    if any(w in z for w in ["cocina", "preparacion", "kitchen"]):
        return "restaurante_cocina"
    if any(w in z for w in ["corral", "pasto", "establo", "granja", "finca", "campo"]) or any(w in b for w in ["finca", "agricultura", "granja"]):
        return "finca_corral"
    if any(w in z for w in ["almacen", "bodega", "deposito", "bodega"]):
        return "almacen"
    return "entrada"


def get_vigilance_template(
    business_type: str,
    zone: str,
    business_name: str,
    owner_notes: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Selecciona y personaliza el template correcto según negocio+zona.

    Returns:
        dict con:
        - role: prompt del rol de testigo (narración rica)
        - attention_questions: preguntas directas para Qwen
        - attention_phrases: frases base (las del dueño o las default)
    """
    key = _profile_key(business_type, zone)
    template = VIGILANCE_TEMPLATES.get(key, VIGILANCE_TEMPLATES["entrada"])

    role_text = template["role"].format(business_name=business_name)

    # Fuentes de atención: notas del dueño (prioridad) > default del template
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
            f"menciónalo explícitamente, con el momento exacto en que ocurre:\n{questions_lines}\n\n"
            f"Si no ves ninguna de estas cosas, no lo menciones — sigue describiendo la escena normalmente.\n"
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

    Incluye: role (testigo) + attention (frases del dueño) + contexto temporal.
    """
    tmpl = get_vigilance_template(business_type, zone, business_name, owner_notes)
    time_status = "FUERA DE HORARIO laboral" if is_after_hours else "DENTRO de horario laboral"

    return (
        f"{tmpl['role']}\n\n"
        f"Estado: {time_status}.\n\n"
        f"{tmpl['attention_section']}\n\n"
        f"Información del frame:\n"
        f"- El número en la esquina superior izquierda es el orden del frame (1-16)\n"
        f"- La hora en la esquina superior derecha es el timestamp real del frame\n"
        f"- Los frames van de izquierda a derecha, arriba a abajo\n"
    )
