#!/usr/bin/python3
"""Actualiza schema herramientas Eva v2"""

import os

filepath = '/opt/ojoia/code/eva_v2.py'
schema_start_line = None

# Leer líneas
with open(filepath, 'r') as f:
    lines = f.readlines()

# Buscar schema inicio
for i, line in enumerate(lines):
    if 'f"- get_activity_summary: Resume la actividad del día' in line:
        schema_start_line = i
        break

if schema_start_line and schema_start_line > 0:
    # Nuevas líneas
    new_lines = [
        '# ====== HERRAMIENTAS ACTUALIZADAS ======
',
        '    f"- get_activity_summary: Resume actividad hoy (análisis totales, personas máx/min, alertas)\n" +\n',
        '    f"- search_events: Busca **eventos** usando palabras clave o rango horario\n" +\n',
        '    f"- find_anomalies: Eventos **sospechosos** priorizados (severidad media/alta)\n" +\n', 
        '    f"- latest_events: Últimos eventos procesados \\\\ última hora\n" +\n',
        '    f"- count_people: **Conteo exacto** de personas en cámara (usar: hoy/ayer/timestamp)\n" +\n',
        '    f"- count_kids: Niños detectados (medida DFPF < 0.85, ROI cabeza grande)\n" +\n',
        '    f"- is_open_hours: Determina horario negocio **según schedule.json**\n" +\n', 
        '    f"- list_employees: **Empleados ACTIVOS** registrados (face_id, nombre, rol)\n"'
    ]
    # Reemplazar
    lines[schema_start_line:schema_start_line+4] = new_lines

    with open(filepath, 'w') as f:
        f.writelines(lines)
    print(f"✅ Schema actualizado en línea {schema_start_line+1}")
else:
    print("❌ No encontrado patrón -- verificar líneas exactas")

# Mostrar nuevas herramientas
print("\\n=== HERRAMIENTAS AHORA ===")
for i, line in enumerate(lines):
    if 'count_people' in line or 'is_open_hours' in line:
        print(line.strip())
