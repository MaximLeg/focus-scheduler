#!/usr/bin/env python3
"""
scheduler/main.py
─────────────────────────────────────────────────────────────────────────────
Scheduler de notifications — tourne sur Render (gratuit, sans CB)
Envoie les notifications Web Push via la librairie pywebpush

Installation :
    pip install -r requirements.txt

Générer les clés VAPID (à faire UNE SEULE FOIS) :
    python generate_vapid.py

Variables d'environnement à configurer sur Render :
    SUPABASE_URL        → ton URL Supabase
    SUPABASE_SERVICE_KEY → ta Service Role Key (Settings → API)
    VAPID_PRIVATE_KEY   → clé privée générée par generate_vapid.py
    VAPID_PUBLIC_KEY    → clé publique (à copier aussi dans src/supabase.js)
    VAPID_EMAIL         → mailto:ton@email.com
─────────────────────────────────────────────────────────────────────────────
"""

import os
import json
import time
import logging
from datetime import date, datetime, timezone

import schedule
import requests
from pywebpush import webpush, WebPushException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("focus")

# ── Config depuis les variables d'environnement ───────────────────────────────
SUPABASE_URL         = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
VAPID_PRIVATE_KEY    = os.environ["VAPID_PRIVATE_KEY"]
VAPID_PUBLIC_KEY     = os.environ["VAPID_PUBLIC_KEY"]
VAPID_EMAIL          = os.environ.get("VAPID_EMAIL", "mailto:focus@app.com")

HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
}


# ══════════════════════════════════════════════════════════════════════════════
#  ACCÈS SUPABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_all_subscriptions() -> list[dict]:
    """Récupère tous les abonnements push enregistrés."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/push_subscriptions",
        headers=HEADERS,
        params={"select": "*"}
    )
    r.raise_for_status()
    return r.json()


def get_tasks_for_user(user_id: str) -> list[dict]:
    """Récupère les tâches non terminées d'un utilisateur."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/tasks",
        headers=HEADERS,
        params={
            "select":  "*",
            "user_id": f"eq.{user_id}",
            "done":    "eq.false",
        }
    )
    r.raise_for_status()
    return r.json()


def get_done_today_for_user(user_id: str) -> list[dict]:
    """Récupère les tâches terminées aujourd'hui."""
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/tasks",
        headers=HEADERS,
        params={
            "select":  "*",
            "user_id": f"eq.{user_id}",
            "done":    "eq.true",
            "done_at": f"gte.{today}T00:00:00",
        }
    )
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════════════════════════
#  ENVOI D'UNE NOTIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def send_push(sub: dict, title: str, body: str, tag: str = "focus", urgent: bool = False):
    """Envoie une notification Web Push à un abonnement."""
    payload = json.dumps({
        "title":  title,
        "body":   body,
        "tag":    tag,
        "urgent": urgent,
    })
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {
                    "p256dh": sub["p256dh"],
                    "auth":   sub["auth"],
                },
            },
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_EMAIL},
        )
        log.info(f"✅ Notif envoyée → {sub['endpoint'][:50]}...")
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            # Abonnement expiré → on le supprime
            log.warning(f"🗑 Abonnement expiré, suppression...")
            requests.delete(
                f"{SUPABASE_URL}/rest/v1/push_subscriptions",
                headers=HEADERS,
                params={"endpoint": f"eq.{sub['endpoint']}"}
            )
        else:
            log.error(f"❌ WebPushException: {e}")
    except Exception as e:
        log.error(f"❌ Erreur inattendue: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  LES 3 NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def morning_notification():
    """🌅 8h00 — résumé de la journée."""
    log.info("🌅 Envoi notifications du matin...")
    subs = get_all_subscriptions()
    today = date.today().isoformat()

    for sub in subs:
        tasks        = get_tasks_for_user(sub["user_id"])
        today_tasks  = [t for t in tasks if t.get("deadline") == today]
        urgent_tasks = [t for t in tasks if t.get("urgency") == "urgent"]

        if not tasks:
            title = "🌅 Bonne journée !"
            body  = "Aucune tâche en cours. Profitez-en ! ✨"
        else:
            title = "🌅 Votre journée commence"
            parts = []
            if today_tasks:
                parts.append(f"{len(today_tasks)} tâche(s) pour aujourd'hui")
            if urgent_tasks:
                parts.append(f"{len(urgent_tasks)} urgente(s) :")
                parts += [f"• {t['title']}" for t in urgent_tasks[:3]]
            body = "\n".join(parts)

        send_push(sub, title, body, tag="morning")


def urgent_hourly_reminder():
    """🔴 Toutes les heures (9h→21h) — rappel des urgents à faire aujourd'hui."""
    log.info("🔴 Envoi rappels urgents horaires...")
    subs  = get_all_subscriptions()
    today = date.today().isoformat()

    for sub in subs:
        tasks = get_tasks_for_user(sub["user_id"])

        # Seulement les urgentes non terminées avec deadline aujourd'hui
        urgent_today = [
            t for t in tasks
            if t.get("urgency") == "urgent"
            and t.get("deadline") == today
            and not t.get("done")
        ]
        if not urgent_today:
            continue

        nb    = len(urgent_today)
        title = f"🔴 {nb} urgence{' restante' if nb == 1 else 's restantes'} aujourd'hui"
        body  = "\n".join(f"• {t['title']}" for t in urgent_today)
        send_push(sub, title, body, tag="urgent-reminder", urgent=True)


def evening_recap():
    """🌙 22h00 — récap motivant."""
    log.info("🌙 Envoi récap du soir...")
    subs = get_all_subscriptions()

    for sub in subs:
        pending    = get_tasks_for_user(sub["user_id"])
        done_today = get_done_today_for_user(sub["user_id"])

        if done_today:
            title = "🌙 Récap de journée — Bien joué ! ✨"
            body  = (f"{len(done_today)} tâche(s) accomplies aujourd'hui.\n"
                     f"{len(pending)} en attente pour demain. Bonne nuit !")
        else:
            title = "🌙 Récap de journée"
            body  = (f"{len(pending)} tâche(s) en attente. Demain sera parfait ! 💪"
                     if pending else "Rien en attente. Excellent travail ! 🏆")

        send_push(sub, title, body, tag="evening")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def setup_schedule():
    schedule.every().day.at("08:00").do(morning_notification)
    schedule.every().day.at("22:00").do(evening_recap)

    for hour in range(9, 22):
        schedule.every().day.at(f"{hour:02d}:00").do(urgent_hourly_reminder)

    log.info("📅 Scheduler configuré :")
    log.info("   🌅 08:00 — notification du matin")
    log.info("   🔴 09:00–21:00 — rappels urgents horaires")
    log.info("   🌙 22:00 — récap du soir")


if __name__ == "__main__":
    log.info("🚀 focus. scheduler démarré")
    setup_schedule()

    # Optionnel : envoie une notif de matin au démarrage si on est entre 8h et 9h
    now_hour = datetime.now().hour
    if now_hour == 8:
        morning_notification()

    while True:
        schedule.run_pending()
        time.sleep(30)
