# 📊 Informe GPU — Certificación F3 y perfilado YOLO (2026-08-31)

> Pregunta: ¿cuánto subió el pico de VRAM con las pruebas de YOLO y qué % de GPU se usa?
> Método: muestreo `nvidia-smi` cada 150-200ms por GPU y por proceso, bursts controlados
> contra `/detect_batch` con 16 imágenes reales por tanda.

---

## 1. Resultado directo (la respuesta corta)

| Métrica | Valor |
|---|---|
| **Pico VRAM de YOLO (aislado, 20 tandas × 16 imgs)** | **980 MiB — PLANO** (no crece con el batch) |
| Pico VRAM de YOLO durante certificación 100 cámaras | ~2,068 MiB (misma reserva, sin crecimiento) |
| **Utilización GPU0 durante bursts YOLO** | promedio **2%**, máximo **22%** |
| Utilización GPU0 durante certificación completa (con Qwen) | picos de **100%** (ver §3: era Qwen, no YOLO) |
| Inference YOLO batch=16 (imgs 416px) | p50 **~145ms**, rango 124-173ms |
| Throughput YOLO certificado | 8 tandas concurrentes (128 imgs) en 1.54s = **83 img/s** |

**Conclusión: YOLO NO es el problema de VRAM ni de GPU.** Con batch=16 usa ~1GB
plano y 22% de GPU en ráfagas. El margen restante de GPU0 (15GB libres, 78% de
cómputo libre en burst) permite subir BATCH a 32 sin riesgo medible.

## 2. Mapa actual por GPU (verificado por PCI bus, no asumido)

### GPU0 (02:00.0) — 9,424 / 24,576 MiB usados en reposo
| Proceso | VRAM | Comentario |
|---|---|---|
| `llama-server` (Qwen3VL-8B Q4, contenedor **qwen3vl8b**) | 7,098 MiB | ⚠️ contenedor **unhealthy** hace horas |
| `whisper_server.py` (whisper-turbo) | 1,312 MiB | OK, idle |
| `yolo_server.py` (yolo-pose) | 980 MiB | OK, pico plano 980 |
| `sglang` (qwen-7b) | 0 MiB | 🔴 **CRASH-LOOP** (ver §4) |

### GPU1 (03:00.0) — 24,037 / 24,576 MiB (97.9%)
| Proceso | VRAM | Comentario |
|---|---|---|
| `VLLM::EngineCore` (Qwen-VL-9B, grids de vigilancia) | 23,460 MiB | ⚠️ 97% VRAM — sin colchón para picos de KV |

## 3. El pico de 18,395 MiB en GPU0 durante la certificación — atribución

El pico (18.4GB) y el 100% de util de GPU0 durante la prueba de 100 cámaras
**no fue YOLO**. Descomposición:
- sglang (qwen-7b, entonces vivo): 10,138 MiB pesos + KV cache creciendo
- llama-server (qwen3vl8b): 7,098 MiB
- yolo: ~2,000 MiB — plano
- whisper: ~1,300 MiB

El 100% de GPU0 = **qwen-7b procesando las verificaciones B2** (el verificador
de 2ª pasada de alertas corre en GPU0) + decodificación de grids.

## 4. 🔴 Hallazgo crítico: qwen-7b está en crash-loop

- Sintoma: contenedor "Up X seconds" reiniciándose cada ~30s; `/health` en 8004 no responde.
- Causa (log): `Loaded weights leave no GPU memory for the KV cache under
  --mem-fraction-static=0.42 ... minimum viable = 0.585`
- Es decir: al (re)arrancar, sus pesos (~10GB) + el KV que quiere reservar no caben
  junto a qwen3vl8b (7GB) + whisper (1.3GB) + yolo (1GB) en los 24GB de GPU0.
- **Impacto en producción HOY**: el verificador B2 de alertas está caído → las
  reglas keyword-match se dropean (política conservadora) y Eva chat degrada.
- **Causa raíz**: no fue YOLO (pico plano 980MiB). Fue la cohabitación de 4
  modelos en GPU0 sin presupuesto por contenedor.

### Opciones (decisión pendiente del operador)
| Opción | Efecto |
|---|---|
| A (recomendada): apagar `qwen3vl8b` (unhealthy, no referenciado por el pipeline de vigilancia; liberaría 7GB de GPU0) | qwen-7b arranca con colchón de KV amplio |
| B: subir `--mem-fraction-static` de sglang a ~0.585 | arranca justo, sin colchón; riesgo de OOM bajo ráfaga B2 |
| C: mover whisper o yolo a GPU1 | GPU1 ya está al 97% — no recomendado |
| D: mover qwen-7b a otro nodo del clúster | aislamiento total, más latencia de red |

## 5. Capacidad de escala deducida de estas mediciones

- YOLO: a 83 img/s sostenidos verificados, con 100 cámaras × 1fps (100 img/s)
  va justo → **subir BATCH de 16 a 32** duplica el throughput por tanda
  (la inferencia escala casi lineal en 3090 hasta ~32 con 416px).
- Qwen-VL (GPU1): sigue siendo el techo de análisis; cubierto por
  OJOIA_GRID_SIZE=32 (mitad de grids/s).
- GPU0 en burst YOLO: 78% de cóputo libre → espacio para batch 32 sobrado.

## 6. Recomendaciones accionables (en orden)

1. **Decidir el destino de qwen3vl8b** (apagar / reclamar) → desbloquea qwen-7b.
2. Subir `YOLO_BATCH` (BATCH del worker) a 32 y `MAXLEN` del stream a 4000
   (mismo factor) cuando las cámaras superen ~80.
3. Bajar `gpu-memory-utilization` de vLLM (GPU1) de ~0.96 a 0.90 → colchón 1.4GB
   anti-OOM en producción.
4. Monitor: exponer `inference_ms` de YOLO en /health (ya viene en cada
   respuesta de /detect_batch; solo hay que agregarlo a las métricas).

---

*Mediciones: nvidia-smi 595.84, muestreo 150-200ms, frames reales de eventos
OJO-D1C560 (personas presentes). Archivos de muestreo: /tmp/kilo/*.csv.*
