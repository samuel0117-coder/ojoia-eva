#!/usr/bin/env python3
"""Test de alerta: fuerza una violación para verificar notificación push."""
import asyncio
import json
import time
import sys
import os

sys.path.insert(0, '/home/sam/ai_system')
os.chdir('/home/sam/ai_system')

from orchestrator import orchestrator, get_camera_config, save_event_to_disk, send_fcm_notification, update_camera_metrics
import datetime

async def test_alert():
    user_id = "moXcjYsfYogCFfvHq0TmadF8ytt2"
    camera_id = "cam_1779756401"
    
    print("=" * 60)
    print("TEST: Alerta con notificación push")
    print("=" * 60)
    
    # 1. Cargar imagen
    events_dir = f"/home/sam/storage/users/{user_id}/cameras/{camera_id}/events"
    jpgs = sorted([f for f in os.listdir(events_dir) if f.endswith('.jpg')]) if os.path.exists(events_dir) else []
    
    if jpgs:
        with open(f"{events_dir}/{jpgs[0]}", 'rb') as f:
            frame_bytes = f.read()
    else:
        from PIL import Image
        import io
        img = Image.new('RGB', (640, 480), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        frame_bytes = buf.getvalue()
    
    cfg = get_camera_config(user_id, camera_id)
    
    # 2. Llenar grid
    print("\n[1] Llenando grid...")
    for i in range(16):
        result = orchestrator.add_frame(
            frame_bytes, camera_id, user_id,
            yolo_count=1,
            vigilance_prompt=cfg.get('vigilance_prompt', ''),
            vigilance_rules='\n'.join(cfg.get('rules', []))
        )
        if result['grid_full']:
            print(f"  Grid LLENO en frame {i+1}")
            break
    
    # 3. Esperar procesamiento
    print("\n[2] Esperando Qwen...")
    await asyncio.sleep(30)
    
    # 4. Verificar evento creado
    print("\n[3] Verificando evento:")
    if os.path.exists(events_dir):
        events = sorted(
            [f for f in os.listdir(events_dir) if f.endswith('.json')],
            key=lambda x: os.path.getmtime(f"{events_dir}/{x}"),
            reverse=True
        )
        
        # Buscar evento nuevo (últimos 2 minutos)
        for fname in events[:10]:
            with open(f"{events_dir}/{fname}") as f:
                ev = json.load(f)
            ts = ev.get('timestamp', 0)
            if ts > time.time() - 120:
                qa = ev.get('metadata', {}).get('qwen_analysis', 'N/A')[:80]
                desc = ev.get('description', '')[:60]
                eid = ev.get('event_id', '')[-12:]
                etype = ev.get('event_type', '')
                print(f"  ✅ {eid} | type={etype} | desc={desc}")
                print(f"  qwen: {qa}")
    
    # 5. Verificar estado de notificaciones
    print(f"\n[4] Cooldown state:")
    print(f"  Last notifications: {orchestrator._last_notification_ts}")
    for cam_key, ts in orchestrator._last_notification_ts.items():
        remaining = 300 - (time.time() - ts)
        print(f"  {cam_key}: last notif {remaining:.0f}s ago")
    
    # 6. ¿Se envió FCM?
    print(f"\n[5] Revisar logs de FCM...")
    import subprocess
    result = subprocess.run(['tail', '-20', '/home/sam/ai_system/api_eva.log'], capture_output=True, text=True)
    for line in result.stdout.split('\n'):
        if 'FCM' in line or 'fcm' in line.lower() or 'push' in line.lower() or 'notification' in line.lower():
            print(f"  {line}")
    
    print("\n" + "=" * 60)

asyncio.run(test_alert())
