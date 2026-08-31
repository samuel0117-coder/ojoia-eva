#!/usr/bin/env python3
"""C5 — Test de carga del pipeline de ingesta de OjoIA.

Simula N cámaras sintéticas publicando frames JPEG contra /ingest/frame y mide:
- latencia p50/p95/p99 del ingest
- códigos HTTP (200 / 429 rate-limited / errores)
- drops de FRAME_QUEUE (via /health)

Uso:
    python3 scripts/load_test_ingest.py --url http://127.0.0.1:8005 \
        --cameras 10 --fps 1 --duration 20

Requiere una imagen JPEG cualquiera (se genera una sintética si falta PIL no).
La cámara usa id "LOADTEST-<i>" y user "loadtest"; limpiar con --clean.
"""
import argparse, asyncio, base64, io, json, statistics, time, os, sys

try:
    import httpx
except ImportError:
    print("pip install httpx"); sys.exit(1)


def synth_jpeg(seed: int = 0) -> bytes:
    """JPEG 320x240 sintético (gris con variación por seed)."""
    try:
        from PIL import Image
        img = Image.new("RGB", (320, 240), (seed % 255, 100, 100))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return buf.getvalue()
    except ImportError:
        # fallback: JPEG 1x1 mínimo embebido
        return base64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////"
            "////////////////////////////////////////////2wBDAf//////////////"
            "//////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB"
            "/8QAFAABAAAAAAAAAAAAAAAAAAAAA//EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAU"
            "AQEAAAAAAAAAAAAAAAAAAAAE/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQAC"
            "EQMRAD8AnwD/2Q==")


async def camera_loop(client, url, cam_id, user, key, fps, duration, results):
    frame = synth_jpeg(hash(cam_id) % 255)
    interval = 1.0 / fps
    end = time.time() + duration
    while time.time() < end:
        t0 = time.time()
        try:
            r = await client.post(
                f"{url}/ingest/frame",
                files={"image": ("frame.jpg", frame, "image/jpeg")},
                data={"camera_id": cam_id, "user_id": user},
                headers={"X-Camera-Key": key} if key else {},
            )
            results.append((time.time() - t0, r.status_code))
        except Exception as e:
            results.append((time.time() - t0, -1))
        elapsed = time.time() - t0
        await asyncio.sleep(max(0, interval - elapsed))


async def run(url, cameras, fps, duration, user, key):
    results = []
    limits = httpx.Limits(max_connections=cameras * 2)
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        h0 = (await client.get(f"{url}/health")).json()
        tasks = [camera_loop(client, url, f"LOADTEST-{i:03d}", user, key, fps, duration, results)
                 for i in range(cameras)]
        await asyncio.gather(*tasks)
        h1 = (await client.get(f"{url}/health")).json()
    return results, h0, h1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8005")
    ap.add_argument("--cameras", type=int, default=10)
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--user", default="loadtest")
    ap.add_argument("--key", default="")
    ap.add_argument("--clean", action="store_true", help="borrar datos del user loadtest")
    args = ap.parse_args()

    if args.clean:
        import shutil
        p = f"/home/sam/storage/users/{args.user}"
        if os.path.exists(p):
            shutil.rmtree(p)
            print(f"limpiado {p}")
        return

    print(f"▶ {args.cameras} cámaras × {args.fps} fps × {args.duration}s contra {args.url}")
    results, h0, h1 = asyncio.run(
        run(args.url, args.cameras, args.fps, args.duration, args.user, args.key))

    lats = sorted(l for l, _ in results)
    codes = {}
    for _, c in results:
        codes[c] = codes.get(c, 0) + 1
    pct = lambda p: lats[int(len(lats) * p)] * 1000 if lats else 0
    print(f"\n═══ RESULTADOS ═══")
    print(f"requests totales : {len(results)}  ({len(results)/args.duration:.1f} req/s)")
    print(f"latencia p50     : {pct(.5):.0f} ms")
    print(f"latencia p95     : {pct(.95):.0f} ms")
    print(f"latencia p99     : {pct(.99):.0f} ms")
    print(f"HTTP codes       : {codes}")
    q0, q1 = h0.get("frame_queue", {}), h1.get("frame_queue", {})
    print(f"queue drops      : {q0.get('drops', 0)} → {q1.get('drops', 0)}")
    print(f"rate-limit drops : {q0.get('rate_limit_drops', 0)} → {q1.get('rate_limit_drops', 0)}")


if __name__ == "__main__":
    main()
