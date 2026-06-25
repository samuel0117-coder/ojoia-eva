#!/usr/bin/env python3
"""Test completo del flujo de eventos con el nuevo prompt en español."""
import asyncio
import json
import time
import sys
import os

sys.path.insert(0, '/home/sam/ai_system')
os.chdir('/home/sam/ai_system')

from orchestrator import orchestrator, get_camera_config, QwenOrchestrator

async def test_flow():
    user_id = "moXcjYsfYogCFfvHq0TmadF8ytt2"
    camera_id = "cam_1779756401"
    
    print("=" * 60)
    print("TEST: Flujo completo de eventos")
    print("=" * 60)
    
    # 1. Verificar config de la cámara
    print("\n[1] Config de cámara:")
    cfg = get_camera_config(user_id, camera_id)
    print(f"  zone: {cfg.get('zone')}")
    print(f"  rules: {cfg.get('rules', [])}")
    print(f"  rules_es: {cfg.get('rules_es', [])}")
    print(f"  scanner_question: {cfg.get('scanner_question', '')}")
    print(f"  system_prompt: {cfg.get('vigilance_prompt', '')[:100]}")
    
    # 2. Cargar una imagen de prueba
    print("\n[2] Cargando imagen de prueba...")
    # Buscar un frame existente o crear uno
    test_img_path = None
    events_dir = f"/home/sam/storage/users/{user_id}/cameras/{camera_id}/events"
    if os.path.exists(events_dir):
        jpgs = [f for f in os.listdir(events_dir) if f.endswith('.jpg')]
        if jpgs:
            test_img_path = f"{events_dir}/{jpgs[0]}"
    
    if not test_img_path or not os.path.exists(test_img_path):
        print("  No hay frames de prueba. Creando imagen dummy...")
        from PIL import Image
        import io
        img = Image.new('RGB', (640, 480), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        frame_bytes = buf.getvalue()
    else:
        print(f"  Usando: {test_img_path}")
        with open(test_img_path, 'rb') as f:
            frame_bytes = f.read()
    
    print(f"  Tamaño: {len(frame_bytes)} bytes")
    
    # 3. Simular 16 frames (llenar el grid)
    print("\n[3] Llenando grid con 16 frames...")
    for i in range(16):
        result = orchestrator.add_frame(
            frame_bytes, camera_id, user_id,
            yolo_count=1,
            vigilance_prompt=cfg.get('vigilance_prompt', ''),
            vigilance_rules='\n'.join(cfg.get('rules', []))
        )
        if i < 3 or i == 15:
            print(f"  Frame {i+1}: count={result['frame_count']}, full={result['grid_full']}")
    
    # 4. Esperar a que process_grid termine
    print("\n[4] Esperando análisis de Qwen (hasta 60s)...")
    await asyncio.sleep(30)
    
    # 5. Verificar si se creó un evento
    print("\n[5] Verificando eventos guardados:")
    if os.path.exists(events_dir):
        events = sorted(
            [f for f in os.listdir(events_dir) if f.endswith('.json')],
            key=lambda x: os.path.getmtime(f"{events_dir}/{x}"),
            reverse=True
        )
        print(f"  Total eventos: {len(events)}")
        
        for fname in events[:3]:
            with open(f"{events_dir}/{fname}") as f:
                ev = json.load(f)
            ts = ev.get('timestamp', 0)
            desc = ev.get('description', '')[:80]
            qa = ev.get('metadata', {}).get('qwen_analysis', '')[:80]
            print(f"\n  Evento: {ev.get('event_id')}")
            print(f"  ts: {time.strftime('%H:%M:%S', time.localtime(ts))}")
            print(f"  type: {ev.get('event_type')}")
            print(f"  desc: {desc}")
            print(f"  qwen: {qa}")
    
    print("\n" + "=" * 60)
    print("TEST COMPLETADO")
    print("=" * 60)

asyncio.run(test_flow())
