#!/usr/bin/env python3
"""Test rápido del flujo de eventos - verificación en tiempo real."""
import asyncio
import json
import time
import sys
import os

sys.path.insert(0, '/home/sam/ai_system')
os.chdir('/home/sam/ai_system')

from orchestrator import orchestrator, get_camera_config

async def test():
    user_id = "moXcjYsfYogCFfvHq0TmadF8ytt2"
    camera_id = "cam_1779756401"
    
    print("=" * 60)
    print("TEST RÁPIDO: Flujo de eventos")
    print("=" * 60)
    
    # 1. Verificar config
    cfg = get_camera_config(user_id, camera_id)
    print(f"\n[1] Config: zone={cfg.get('zone')}, rules={len(cfg.get('rules', []))}")
    
    # 2. Cargar imagen de prueba
    events_dir = f"/home/sam/storage/users/{user_id}/cameras/{camera_id}/events"
    jpgs = sorted([f for f in os.listdir(events_dir) if f.endswith('.jpg')]) if os.path.exists(events_dir) else []
    
    if jpgs:
        with open(f"{events_dir}/{jpgs[0]}", 'rb') as f:
            frame_bytes = f.read()
        print(f"[2] Frame de prueba: {len(frame_bytes)} bytes")
    else:
        from PIL import Image
        import io
        img = Image.new('RGB', (640, 480), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        frame_bytes = buf.getvalue()
        print(f"[2] Frame dummy: {len(frame_bytes)} bytes")
    
    # 3. Llenar grid
    print("\n[3] Llenando grid...")
    for i in range(16):
        result = orchestrator.add_frame(
            frame_bytes, camera_id, user_id,
            yolo_count=1,
            vigilance_prompt=cfg.get('vigilance_prompt', ''),
            vigilance_rules='\n'.join(cfg.get('rules', []))
        )
        if result['grid_full']:
            print(f"  Grid LLENO en frame {i+1}!")
            break
    
    # 4. Esperar procesamiento
    print("\n[4] Esperando Qwen (60s)...")
    for i in range(12):
        await asyncio.sleep(5)
        pending = orchestrator.grid.get_frame_count()
        print(f"  {(i+1)*5}s: grid={pending} frames")
        if pending == 0:
            print("  → Grid procesado!")
            break
    
    # 5. Verificar eventos nuevos
    print("\n[5] Eventos nuevos:")
    if os.path.exists(events_dir):
        events = sorted(
            [f for f in os.listdir(events_dir) if f.endswith('.json')],
            key=lambda x: os.path.getmtime(f"{events_dir}/{x}"),
            reverse=True
        )
        
        new_events = []
        for fname in events[:5]:
            with open(f"{events_dir}/{fname}") as f:
                ev = json.load(f)
            ts = ev.get('timestamp', 0)
            # Eventos de los últimos 5 minutos
            if ts > time.time() - 300:
                new_events.append(ev)
        
        if new_events:
            for ev in new_events:
                ts = ev.get('timestamp', 0)
                qa = ev.get('metadata', {}).get('qwen_analysis', '')[:100]
                desc = ev.get('description', '')[:60]
                print(f"  ✅ {ev.get('event_id')[-12:]} | {desc} | qwen: {qa}")
        else:
            print("  No hay eventos nuevos en los últimos 5 min")
            
        # Verificar el último evento
        with open(f"{events_dir}/{events[0]}") as f:
            latest = json.load(f)
        ts = latest.get('timestamp', 0)
        qa = latest.get('metadata', {}).get('qwen_analysis', '')[:80]
        desc = latest.get('description', '')[:60]
        print(f"\n[6] Último evento global:")
        print(f"  {latest.get('event_id')} | ts={time.strftime('%H:%M:%S', time.localtime(ts))}")
        print(f"  desc: {desc}")
        print(f"  qwen: {qa}")
    
    # 6. Estado final
    print(f"\n[7] Estado final: grid={orchestrator.grid.get_frame_count()} frames")
    print("=" * 60)

asyncio.run(test())
