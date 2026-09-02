#!/usr/bin/env python3
"""expirations_job.py — F-billing: dunning/vencimientos de planes OjoIA.

Corre diario (cron) o desde el panel (POST /admin/billing/run-expirations):

1. AVISO AL USUARIO (push FCM): a 3 días y a 1 día del vencimiento,
   y el primer día de gracia. Máx 1 push/día por usuario (registro en
   user.json.last_expiry_notice para no repetir).
2. PUSH AL ADMIN: resumen de quienes vencen en ≤3 días (para cobrar).
3. SUSPENSIÓN automática: al terminar la gracia → status=suspended
   (antes se calculaba on-the-fly pero el user.json quedaba 'active'
   para siempre — el estado real nunca se persistía).
4. RESUMEN final en stdout (lo lee el cron log / el endpoint).
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STORAGE_ROOT = Path(os.environ.get("OJOIA_STORAGE", "/home/sam/storage"))
NOTICE_DAYS = (3, 1)      # avisar al usuario a 3d y a 1d
GRACE_KEY = "grace_period_days"


def _push_user(user_id: str, title: str, body: str) -> bool:
    try:
        from orchestrator import send_fcm_notification
        import asyncio
        return asyncio.run(send_fcm_notification(title=title, body=body,
                                                 user_id=user_id))
    except Exception:
        return False


def _push_admin(title: str, body: str) -> bool:
    """Push a todos los tokens admin (admin_config.push_tokens)."""
    try:
        import requests
        cfg = json.loads((STORAGE_ROOT / "admin_config.json").read_text())
        server_key = cfg.get("fcm_server_key") or os.environ.get("FCM_SERVER_KEY")
        tokens = cfg.get("push_tokens") or []
        if not server_key or not tokens:
            return False
        for t in tokens[:5]:
            requests.post("https://fcm.googleapis.com/fcm/send",
                          json={"to": t, "notification": {"title": title, "body": body}},
                          headers={"Authorization": f"key={server_key}"}, timeout=8)
        return True
    except Exception:
        return False


def process_all() -> dict:
    now = time.time()
    summary = {"checked": 0, "notified_user": 0, "suspended": 0,
               "expiring_3d": [], "admin_notified": False}

    users_dir = STORAGE_ROOT / "users"
    if not users_dir.is_dir():
        return summary

    for udir in users_dir.iterdir():
        uf = udir / "user.json"
        if not uf.is_file():
            continue
        try:
            ud = json.loads(uf.read_text())
        except Exception:
            continue
        uid = udir.name
        summary["checked"] += 1

        plan_end = ud.get("plan_end", 0) or 0
        if not plan_end:
            continue  # sin vencimiento (free perpetuo o trial sin fin)
        days_left = int((plan_end - now) / 86400)
        status = ud.get("status", "active")

        # 1) avisos al usuario (3d/1d) — 1 por día máx. int() trunca (3d-2h=2),
        # así que avisamos por RANGO: <=3 significa "te quedan 3 días o menos",
        # y 1d cuando days_left==0 pero aún no vence.
        notify_reason = None
        if status == "active" and plan_end > now:
            if days_left <= 3 and days_left > 1:
                notify_reason = f"te quedan {days_left} día(s)"
            elif days_left <= 1:
                notify_reason = "vence mañana"
        if notify_reason:
            last = ud.get("last_expiry_notice", 0)
            if now - last > 20 * 3600:
                plan_name = ud.get("plan", "plan")
                _push_user(uid, "⚠️ Tu plan de OjoIA vence pronto",
                           f"{notify_reason} de tu plan {plan_name}. "
                           f"Renueva para no perder la vigilancia.")
                ud["last_expiry_notice"] = now
                summary["notified_user"] += 1

        # 2) en gracia → aviso único de gracia (plan vencido pero dentro
        # del período: days_left<=0 y aún no expira la gracia)
        if status == "active" and plan_end and plan_end < now:
            grace = ud.get(GRACE_KEY, 3)
            if now < plan_end + grace * 86400 and not ud.get("grace_notified"):
                _push_user(uid, "🔶 Plan vencido — período de gracia",
                           "Tu plan venció. Tienes pocos días de gracia antes de "
                           "suspender el servicio. Renueva cuanto antes.")
                ud["grace_notified"] = True
                summary["notified_user"] += 1

        # 3) suspensión real al acabar la gracia
        grace = ud.get(GRACE_KEY, 3)
        if status != "suspended" and plan_end and now > plan_end + grace * 86400:
            ud["status"] = "suspended"
            ud["suspended_at"] = int(now)
            ud["suspended_reason"] = "plan_expired"
            summary["suspended"] += 1

        # colección para el push admin
        if status == "active" and 0 <= days_left <= 3:
            summary["expiring_3d"].append({"user_id": uid, "days_left": days_left,
                                           "plan": ud.get("plan")})

        # persistir solo si cambió algo
        if ud != json.loads(uf.read_text()):
            tmp = uf.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(ud, indent=2, ensure_ascii=False))
            tmp.replace(uf)

    # push resumen al admin
    if summary["expiring_3d"]:
        names = ", ".join(f"{e['user_id']} ({e['days_left']}d)" for e in summary["expiring_3d"][:10])
        summary["admin_notified"] = _push_admin(
            "💳 Planes por vencer en OjoIA",
            f"{len(summary['expiring_3d'])} usuario(s) vencen en ≤3 días: {names}")
    return summary


def check_disk_alerts() -> dict:
    """HOT-COLD F3: alerta de disco al operador (llamado por el cron diario).
    Usa la misma lógica que /admin/disks/check-alerts (antispam 1h incluido)."""
    try:
        import requests
        ADMIN_TOKEN = os.environ.get("MEGAPANEL_TOKEN", "")
        if not ADMIN_TOKEN:
            return {"skipped": "sin token"}
        r = requests.post(
            "http://127.0.0.1:8005/admin/auth/login",
            json={"token": ADMIN_TOKEN}, timeout=10)
        sess = r.json().get("session_token", "")
        if not sess:
            return {"skipped": "login falló"}
        r2 = requests.post(
            "http://127.0.0.1:8005/admin/disks/check-alerts",
            headers={"Authorization": f"Bearer {sess}"}, timeout=30)
        return r2.json()
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    s = process_all()
    print(json.dumps(s, ensure_ascii=False, indent=1))
    # F3: chequeo de disco en el mismo ciclo diario del cron
    try:
        d = check_disk_alerts()
        print("[disk-alerts]", json.dumps(d, ensure_ascii=False)[:200])
    except Exception as e:
        print("[disk-alerts] error:", e)
