"""
eva/eva_chat_os.py — Motor de Chat de Eva como Sistema Operativo.

Este módulo maneja la conversación con Eva. A diferencia del eva_chat.py
que es solo para configuración inicial, este es el chat permanente que
el usuario usa todos los días para:
- Consultar eventos y frames
- Buscar personas
- Obtener resúmenes del negocio
- Configurar el sistema
- Dar feedback sobre alertas

Flujo:
1. Usuario envía mensaje
2. Se construye el contexto del negocio (business.json)
3. Se envía al LLM con las tools disponibles
4. Si el LLM quiere usar una tool → se ejecuta → se devuelve resultado
5. El LLM genera la respuesta final con los datos
6. Se guarda en el historial de conversación
"""

import json
import logging
import time
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .tools import (
    load_business_json, save_business_json, TOOLS_REGISTRY,
    resolve_user_events_dirs, STORAGE_ROOT
)
from eva_setup_flow import (
    SETUP_PHASES, get_eva_response, extract_business_type,
    generate_camera_prompt, is_confirmation, parse_schedule
)

logger = logging.getLogger(__name__)

QWEN_URL = "http://localhost:8004/v1/chat/completions"
QWEN_TIMEOUT = 45
MAX_HISTORY = 20
MAX_TOOL_ITERATIONS = 5


# ═══════════════════════════════════════════════════════════════
# SYSTEM PROMPT — Contexto del negocio para el LLM
# ═══════════════════════════════════════════════════════════════

def build_system_prompt(user_id: str, session: dict = None) -> str:
    """Construye el system prompt con todo el contexto del negocio."""
    business = load_business_json(user_id)
    if not business:
        return "Eres Eva, asistente de seguridad de OjoIA."

    owner = business.get("owner", {}).get("name", "amigo")
    biz_name = business.get("business_name", "tu negocio")
    biz_type = business.get("business_type", "negocio")
    schedule = business.get("schedule", {})
    concerns = business.get("main_concerns", [])
    cameras = business.get("cameras", {})
    ctx = business.get("conversation_context", {})
    weaknesses = ctx.get("weaknesses", [])

    now = time.time()
    cam_lines = []
    total_events = total_persons = total_alerts = 0
    recent_qwen = []

    for cid, cam in cameras.items():
        last = cam.get("last_frame_ts", 0)
        online = (now - last) < 120 if last else False
        today = cam.get("today_summary", {})
        te, tp, ta = today.get("total_events", 0), today.get("total_persons", 0), today.get("alerts", 0)
        total_events += te; total_persons += tp; total_alerts += ta
        cam_lines.append(f"  • {cam.get('name', cid)}: {'Online' if online else 'Offline'} | {te} eventos, {tp} personas, {ta} alertas")
        for d in today.get("qwen_descriptions", [])[-3:]:
            recent_qwen.append(f"    {d.get('time','?')} - {cam.get('name',cid)}: {d.get('description','')[:100]}")

    people = business.get("people", {})
    known = people.get("known", [])
    susp = people.get("suspicious", [])
    ptext = ""
    if known:
        ptext += "Personas conocidas:\n"
        for p in known:
            tags = ", ".join(p.get("visual_tags", []))
            arr = p.get("patterns", {}).get("usual_arrival", "variable")
            ptext += f"  • {p.get('name','N/A')} ({p.get('role','')}): {tags} | Llega: {arr}\n"
    if susp:
        ptext += "Personas sospechosas:\n"
        for p in susp:
            tags = ", ".join(p.get("visual_tags", []))
            ptext += f"  • {p.get('id','N/A')}: {tags} ({p.get('incidents',0)} incidentes) | {p.get('notes','')}\n"

    qwen_activity = f"\nACTIVIDAD RECIENTE QWEN:\n" + "\n".join(recent_qwen[-5:]) if recent_qwen else ""

    return f"""Eres Eva, asistente de seguridad de {biz_name} ({biz_type}). Hablas con {owner}.

CONTEXTO:
- Horario: {schedule.get('open','07:00')} a {schedule.get('close','19:00')}
- Preocupaciones: {', '.join(concerns) if concerns else 'seguridad general'}
- Debilidades: {', '.join(weaknesses) if weaknesses else 'no especificadas'}

HOY: {total_events} eventos | {total_persons} personas | {total_alerts} alertas
{chr(10).join(cam_lines) if cam_lines else 'Sin cámaras'}
{ptext}{qwen_activity}

REGLAS:
- Si pide datos → usa tool. Si no → responde directo.
- Datos específicos, no genéricos. Si no tienes datos, dilo claro.
- Tono dominicano. Máximo 4 líneas."""


# ═══════════════════════════════════════════════════════════════
# TOOL EXECUTION
# ═══════════════════════════════════════════════════════════════

async def execute_tool(tool_name: str, tool_args: dict, user_id: str) -> dict:
    """Ejecuta una tool y devuelve el resultado."""
    if tool_name not in TOOLS_REGISTRY:
        return {"error": f"Tool '{tool_name}' no encontrada"}

    tool_info = TOOLS_REGISTRY[tool_name]
    func = tool_info["function"]

    try:
        # Agregar user_id a los argumentos
        tool_args["user_id"] = user_id
        result = await func(**tool_args)
        return result
    except Exception as e:
        logger.error(f"Error ejecutando tool {tool_name}: {e}")
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# FORMATEO DE RESULTADOS PARA EL LLM
# ═══════════════════════════════════════════════════════════════

def format_tool_result(tool_name: str, result: dict) -> str:
    """Formatea el resultado de una tool para que el LLM lo entienda."""
    if "error" in result:
        return f"Error en {tool_name}: {result['error']}"

    if tool_name == "search_events":
        found = result.get("found", 0)
        if found == 0:
            return "No se encontraron eventos que coincidan."
        events = result.get("events", [])[:5]
        lines = [f"Encontré {found} evento(s):"]
        for evt in events:
            anomaly = " ⚠️ ANOMALÍA" if evt.get("anomaly") else ""
            lines.append(
                f"  • {evt.get('datetime','?')} | {evt.get('camera_name','?')}: "
                f"{evt.get('description','Sin descripción')} | "
                f"{evt.get('persons', 0)} persona(s){anomaly}"
            )
            if evt.get("persons_details"):
                for pd in evt["persons_details"][:2]:
                    lines.append(f"    → {pd.get('role','?')}: {pd.get('action','')} | Ropa: {pd.get('clothing','')}")
        if found > 5:
            lines.append(f"  ... y {found - 5} eventos más")
        return "\n".join(lines)

    elif tool_name == "find_person":
        if result.get("found", 0) == 0:
            msg = "No se encontraron personas con esa descripción en los eventos."
            if result.get("known_people_matches"):
                msg += "\nSin embargo, en el registro de personas conocidas:"
                for p in result["known_people_matches"]:
                    msg += f"\n  • {p['name']} ({p['role']}): {', '.join(p.get('visual_tags', []))}"
            return msg
        lines = [f"Se encontraron {result['found']} coincidencias:"]
        for m in result.get("event_matches", [])[:5]:
            lines.append(f"  • {m['datetime']} — {m['camera_name']}: {m['person_description']}")
        if result.get("known_people_matches"):
            lines.append("\nPersonas conocidas que coinciden:")
            for p in result["known_people_matches"]:
                lines.append(f"  • {p['name']} ({p['role']}): {', '.join(p.get('visual_tags', []))}")
        return "\n".join(lines)

    elif tool_name == "get_daily_summary":
        d = result
        lines = [f"Resumen del {d.get('date', 'día')}:"]
        lines.append(f"  • {d.get('total_events', 0)} eventos | {d.get('total_persons', 0)} personas | {d.get('alerts', 0)} alertas")
        if d.get("peak_hour"):
            lines.append(f"  • Hora pico: {d['peak_hour']} ({d.get('peak_persons', 0)} personas)")
        for cam_name, data in d.get("cameras_data", {}).items():
            cam_events = data.get("events", []) if isinstance(data, dict) else data[:3] if isinstance(data, list) else []
            cam_persons = data.get("total_persons", 0) if isinstance(data, dict) else 0
            cam_alerts = data.get("alerts", 0) if isinstance(data, dict) else 0
            lines.append(f"\n  {cam_name}: {cam_persons} personas, {cam_alerts} alertas")
            for evt in cam_events[:3]:
                lines.append(f"    • {evt}")
        highlights = d.get("highlights", [])
        if highlights:
            lines.append("\n  Momentos clave:")
            for h in highlights[:3]:
                lines.append(f"    • {h}")
        return "\n".join(lines)

    elif tool_name == "get_traffic_analysis":
        d = result
        lines = [f"Análisis de tráfico (últimos {d.get('period_days', 7)} días):"]
        lines.append(f"  • Total personas: {d.get('total_persons', 0)}")
        lines.append(f"  • Promedio diario: {d.get('daily_avg', 0)}")
        lines.append(f"  • Hora pico: {d.get('peak_hour', 'N/A')} ({d.get('peak_persons', 0)} personas)")
        lines.append(f"  • Tendencia: {d.get('trend', 'N/A')}")
        hourly = d.get("hourly_breakdown", {})
        if hourly:
            top_hours = sorted(hourly.items(), key=lambda x: x[1], reverse=True)[:3]
            lines.append("  • Horas más concurridas:")
            for h, count in top_hours:
                lines.append(f"    — {h}: {count} personas")
        return "\n".join(lines)

    elif tool_name == "get_business_summary":
        d = result
        period = d.get("period", "hoy")
        cams = d.get("cameras", {})
        evts = d.get("events", {})
        lines = [f"Resumen ({period}):"]
        lines.append(f"  • Cámaras: {cams.get('online',0)} online / {cams.get('total',0)} total")
        lines.append(f"  • {evts.get('total',0)} eventos | {evts.get('total_persons',0)} personas | {evts.get('alerts',0)} alertas")
        cam_details = cams.get("details", [])
        for cd in cam_details:
            status = "🟢" if cd.get("online") else "🔴"
            ago = cd.get("last_frame_ago")
            ago_str = f" (último hace {ago}s)" if ago else ""
            lines.append(f"  {status} {cd.get('name','?')}{ago_str}")
        return "\n".join(lines)

    elif tool_name == "get_camera_frames":
        d = result
        if not d.get("frames"):
            return f"No se encontraron frames recientes de {d.get('camera_name', 'la cámara')}."
        lines = [f"Frames recientes de {d.get('camera_name', '')}:"]
        for f in d["frames"][:5]:
            lines.append(f"  • {f['hour']}: {f['description']} ({f['persons']} personas)")
        return "\n".join(lines)

    elif tool_name == "update_business_context":
        if result.get("success"):
            return f"✅ Actualizado: {result['field']} = {result['value']}"
        return f"❌ Error: {result.get('error', 'Desconocido')}"

    elif tool_name == "learn_from_feedback":
        if result.get("success"):
            return f"✅ Feedback registrado: {result['action']}"
        return f"❌ Error: {result.get('error', 'Evento no encontrado')}"

    return str(result)



def _parse_tool_call(text: str) -> Optional[dict]:
    """Parsea una tool call del formato JSON que el LLM devuelve."""
    if not text:
        return None
    import re, json
    # Buscar cualquier JSON en la respuesta
    # Primero intentar parsear todo el texto como JSON
    text_stripped = text.strip()
    try:
        data = json.loads(text_stripped)
        if isinstance(data, dict) and "tool" in data:
            return data
    except Exception:
        pass
    # Buscar JSON dentro del texto
    m = re.search(r'\{[^{}]*"tool"[^{}]*\}', text_stripped)
    if m:
        try:
            data = json.loads(m.group())
            if isinstance(data, dict) and "tool" in data:
                return data
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════
# CHAT PRINCIPAL
# ═══════════════════════════════════════════════════════════════

async def handle_eva_chat_os(
    user_id: str,
    message: str,
    session_id: str = None,
    history: list = None,
    setup_phase: str = None,
    setup_session: dict = None,
) -> Dict[str, Any]:
    """
    Maneja un mensaje del usuario en el chat de Eva.
    
    Si setup_phase es None o CHAT_OS → usa el chat normal con tools.
    Si setup_phase es cualquier otro → usa el flujo de configuración.
    """
    if history is None:
        history = []
    
    # ═══════════════════════════════════════════════════════════
    # MODO CONFIGURACIÓN (setup)
    # ═══════════════════════════════════════════════════════════
    if setup_phase and setup_phase != "CHAT_OS":
        return await _handle_setup_flow(
            user_id=user_id,
            message=message,
            setup_phase=setup_phase,
            setup_session=setup_session or {},
            history=history,
        )
    
    # ═══════════════════════════════════════════════════════════
    # MODO CHAT OS (normal)
    # ═══════════════════════════════════════════════════════════
    return await _handle_chat_os(
        user_id=user_id,
        message=message,
        session_id=session_id,
        history=history,
    )


async def _handle_setup_flow(
    user_id: str,
    message: str,
    setup_phase: str,
    setup_session: dict,
    history: list,
) -> Dict[str, Any]:
    """Maneja el flujo de configuración paso a paso."""
    
    # Obtener respuesta de Eva según el estado
    result = get_eva_response(setup_phase, setup_session, message)
    
    next_phase = result.get("next_phase", setup_phase)
    response = result.get("response", "")
    data = result.get("data", {})
    
    # Actualizar sesión con datos extraídos
    setup_session.update(data)
    setup_session["last_phase"] = next_phase
    
    # Si se configuró una cámara, guardar en business.json
    if result.get("camera_configured"):
        business = load_business_json(user_id)
        camera_count = setup_session.get("camera_count", 1)
        cam_id = f"cam_{camera_count:03d}"
        
        business.setdefault("cameras", {})[cam_id] = {
            "name": setup_session.get("camera_zone", f"Cámara {camera_count}"),
            "zone": setup_session.get("camera_zone", ""),
            "active": True,
            "prompt_vigilancia": data.get("camera_prompt", ""),
            "tareas": setup_session.get("camera_tasks", []),
            "last_frame_ts": 0,
            "today_summary": {
                "date": time.strftime("%Y-%m-%d"),
                "total_analisis": 0,
                "alertas": 0,
                "qwen_analisis": []
            }
        }
        
        # Actualizar datos del negocio
        if "business_name" in setup_session:
            business["business_name"] = setup_session["business_name"]
        if "business_type" in setup_session:
            business["business_type"] = setup_session["business_type"]
        if "concerns" in setup_session:
            business["main_concerns"] = setup_session["concerns"]
        if "schedule" in setup_session:
            business["schedule"] = setup_session["schedule"]
        
        business.setdefault("conversation_context", {})["camaras_configuradas"] = camera_count
        
        save_business_json(user_id, business)
        logger.info(f"Cámara {camera_count} configurada para {user_id}")
    
    # Si la configuración está completa
    if next_phase == "CHAT_OS":
        business = load_business_json(user_id)
        business.setdefault("conversation_context", {})["configuracion_completa"] = True
        save_business_json(user_id, business)
    
    return {
        "success": True,
        "response": response,
        "next_phase": next_phase,
        "setup_session": setup_session,
        "tools_used": [],
        "events_found": [],
        "show_camera_frame": result.get("show_camera_frame", False),
        "camera_configured": result.get("camera_configured", False),
    }


async def _handle_chat_os(
    user_id: str,
    message: str,
    session_id: str = None,
    history: list = None,
) -> Dict[str, Any]:
    """Maneja el chat normal de Eva (después de configuración)."""
    if history is None:
        history = []

    full_response = ""
    tools_used = []
    all_events = []

    try:
        # System prompt simplificado para tool calling
        business = load_business_json(user_id)
        biz_name = business.get("business_name", "tu negocio") if business else "tu negocio"
        owner = business.get("owner", {}).get("name", "amigo") if business else "amigo"
        cameras = business.get("cameras", {}) if business else {}
        cam_list = ", ".join([c.get("name", cid) for cid, c in cameras.items()]) if cameras else "ninguna"
        concerns = ", ".join(business.get("main_concerns", [])) if business.get("main_concerns") else "seguridad general"

        tools_desc = []
        for name, info in TOOLS_REGISTRY.items():
            params = info["parameters"].get("properties", {})
            param_names = list(params.keys())
            tools_desc.append(f'  • {name}: {info["description"]} (params: {", ".join(param_names)})')

        system_prompt = f"""Eres Eva, asistente de seguridad de {biz_name}. Hablas con {owner}.
Cámaras: {cam_list}
Preocupaciones: {concerns}

HERRAMIENTAS:
{chr(10).join(tools_desc)}

REGLAS:
- Si el usuario pide datos del negocio, eventos, personas o estadísticas → responde SOLO con: {{"tool": "nombre", "args": {{"param": "valor"}}}}
- Si NO necesitas datos, responde directamente en español
- Tono dominicano, cercano. Máximo 4 líneas."""

        # Primera llamada al LLM
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-20:]:
            messages.append(msg)
        messages.append({"role": "user", "content": message})

        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(QWEN_URL, json={
                "model": "qwen", "messages": messages,
                "max_tokens": 300, "temperature": 0.3
            })
            resp.raise_for_status()
            result = resp.json()

        llm_text = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        logger.info(f"LLM response: {llm_text[:200]}")

        # Verificar si quiere usar tool
        tool_call = _parse_tool_call(llm_text)

        if tool_call:
            tool_name = tool_call.get("tool")
            tool_args = tool_call.get("args", {})

            if tool_name in TOOLS_REGISTRY:
                tools_used.append(tool_name)
                tool_result = await execute_tool(tool_name, tool_args, user_id)
                formatted_result = format_tool_result(tool_name, tool_result)

                # Guardar eventos para carrusel
                if tool_name in ("search_events", "find_person"):
                    if tool_name == "search_events":
                        for evt in tool_result.get("events", []):
                            all_events.append({
                                "event_id": evt["event_id"], "datetime": evt["datetime"],
                                "camera_name": evt["camera_name"], "description": evt["description"],
                                "summary": evt.get("description", ""),
                                "persons": evt["persons"], "frame_url": evt.get("frame_url", ""),
                                "thumb_url": evt.get("thumb_url", ""),
                                "anomaly": evt.get("anomaly", False)
                            })
                    elif tool_name == "find_person":
                        for m in tool_result.get("event_matches", []):
                            all_events.append({
                                "datetime": m["datetime"], "camera_name": m["camera_name"],
                                "description": m["person_description"], "frame_url": m.get("frame_url", ""),
                                "thumb_url": m.get("thumb_url", ""),
                                "event_type": m.get("event_type", "normal")
                            })

                # Segunda llamada con resultado
                messages.append({"role": "assistant", "content": llm_text})
                messages.append({"role": "user", "content": f"Resultado de {tool_name}:\n{formatted_result}\n\nResponde al usuario en español."})

                async with httpx.AsyncClient(timeout=45) as client:
                    resp2 = await client.post(QWEN_URL, json={
                        "model": "qwen", "messages": messages,
                        "max_tokens": 400, "temperature": 0.7
                    })
                    resp2.raise_for_status()
                    result2 = resp2.json()

                full_response = result2.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                full_response = llm_text
        else:
            full_response = llm_text

    except Exception as e:
        logger.error(f"Error en Eva chat: {e}")
        import traceback
        logger.error(traceback.format_exc())
        full_response = "Disculpa, tuve un problema. Intenta de nuevo."

    return {
        "success": True,
        "response": full_response,
        "tools_used": tools_used,
        "events_found": all_events,
        "session_id": session_id or f"chat_{user_id}_{int(time.time())}",
    }
