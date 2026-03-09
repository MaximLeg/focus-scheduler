#!/usr/bin/env python3
"""
scheduler/main.py — avec gestion de la récurrence
"""

import os, json, time, logging
from datetime import date, datetime, timedelta

import schedule, requests
from pywebpush import webpush, WebPushException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("focus")

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
#  SUPABASE
# ══════════════════════════════════════════════════════════════════════════════

def get_all_subscriptions():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/push_subscriptions", headers=HEADERS, params={"select": "*"})
    r.raise_for_status()
    return r.json()

def get_tasks_for_user(user_id):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "user_id": f"eq.{user_id}", "done": "eq.false"})
    r.raise_for_status()
    return r.json()

def get_all_recurring_tasks():
    """Récupère toutes les tâches récurrentes (tous utilisateurs)."""
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "recurrence": "not.is.null"})
    r.raise_for_status()
    return r.json()

def get_done_today_for_user(user_id):
    today = date.today().isoformat()
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "user_id": f"eq.{user_id}", "done": "eq.true", "done_at": f"gte.{today}T00:00:00"})
    r.raise_for_status()
    return r.json()

def create_task(task_data: dict):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS, json=task_data)
    r.raise_for_status()

def update_task(task_id: str, data: dict):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/tasks",
        headers={**HEADERS, "Prefer": "return=minimal"},
        params={"id": f"eq.{task_id}"}, json=data)
    r.raise_for_status()

# ══════════════════════════════════════════════════════════════════════════════
#  RÉCURRENCE
# ══════════════════════════════════════════════════════════════════════════════

def next_occurrence(deadline_str: str, recurrence: str) -> str | None:
    """Calcule la prochaine date selon la récurrence."""
    d = date.fromisoformat(deadline_str)
    if recurrence == "daily":
        d += timedelta(days=1)
    elif recurrence == "weekdays":
        d += timedelta(days=1)
        while d.weekday() >= 5:  # 5=sam, 6=dim
            d += timedelta(days=1)
    elif recurrence == "weekly":
        d += timedelta(weeks=1)
    elif recurrence == "biweekly":
        d += timedelta(weeks=2)
    elif recurrence == "monthly":
        month = d.month + 1
        year  = d.year + (month > 12)
        month = month if month <= 12 else 1
        import calendar
        day = min(d.day, calendar.monthrange(year, month)[1])
        d = d.replace(year=year, month=month, day=day)
    elif recurrence == "yearly":
        d = d.replace(year=d.year + 1)
    else:
        return None
    return d.isoformat()


def renew_recurring_tasks():
    """
    Chaque matin : pour chaque tâche récurrente cochée la veille,
    crée une nouvelle occurrence avec la prochaine date ET remet done=False
    sur la tâche parente pour la journée suivante.
    """
    log.info("🔁 Renouvellement des tâches récurrentes...")
    today     = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    tasks = get_all_recurring_tasks()
    renewed = 0

    for task in tasks:
        recurrence = task.get("recurrence")
        deadline   = task.get("deadline", "")
        done       = task.get("done", False)

        if not recurrence or not deadline:
            continue

        next_date = next_occurrence(deadline, recurrence)
        if not next_date:
            continue

        # Si la deadline est passée (hier ou avant) et la tâche est cochée
        # → renouveler pour la prochaine occurrence
        if deadline <= yesterday and done:
            update_task(task["id"], {
                "deadline": next_date,
                "done":     False,
                "done_at":  None,
            })
            log.info(f"  ↻ '{task['title']}' → {next_date}")
            renewed += 1

        # Si la deadline est passée mais pas cochée (oubliée)
        # → on avance quand même à la prochaine date sans marquer comme fait
        elif deadline < today and not done:
            update_task(task["id"], {"deadline": next_date})
            log.info(f"  → '{task['title']}' avancé à {next_date} (non cochée)")
            renewed += 1

    log.info(f"🔁 {renewed} tâche(s) récurrente(s) renouvelée(s)")


# ══════════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def send_push(sub, title, body, tag="focus", urgent=False):
    payload = json.dumps({"title": title, "body": body, "tag": tag, "urgent": urgent})
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_EMAIL},
        )
        log.info(f"✅ → {sub['endpoint'][:50]}...")
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            requests.delete(f"{SUPABASE_URL}/rest/v1/push_subscriptions",
                headers=HEADERS, params={"endpoint": f"eq.{sub['endpoint']}"})
        else:
            log.error(f"❌ {e}")
    except Exception as e:
        log.error(f"❌ {e}")


def morning_notification():
    """🌅 8h00 — renouvelle les récurrences PUIS envoie le résumé."""
    renew_recurring_tasks()  # ← toujours en premier

    log.info("🌅 Notifications du matin...")
    subs  = get_all_subscriptions()
    today = date.today().isoformat()

    for sub in subs:
        tasks        = get_tasks_for_user(sub["user_id"])
        today_tasks  = [t for t in tasks if t.get("deadline") == today]
        urgent_tasks = [t for t in tasks if t.get("urgency") == "urgent"]

        if not tasks:
            send_push(sub, "🌅 Bonne journée !", "Aucune tâche en cours. Profitez-en ! ✨", tag="morning")
            continue

        parts = []
        if today_tasks:  parts.append(f"{len(today_tasks)} tâche(s) aujourd'hui")
        if urgent_tasks: parts.append(f"{len(urgent_tasks)} urgente(s)")
        if urgent_tasks: parts += [f"• {t['title']}" for t in urgent_tasks[:3]]

        send_push(sub, "🌅 Votre journée commence", "\n".join(parts), tag="morning")


def urgent_hourly_reminder():
    """🔴 Rappel horaire — uniquement urgentes du jour, non terminées."""
    log.info("🔴 Rappels urgents horaires...")
    subs  = get_all_subscriptions()
    today = date.today().isoformat()

    for sub in subs:
        tasks = get_tasks_for_user(sub["user_id"])
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
    log.info("🌙 Récap du soir...")
    subs = get_all_subscriptions()

    for sub in subs:
        pending    = get_tasks_for_user(sub["user_id"])
        done_today = get_done_today_for_user(sub["user_id"])

        if done_today:
            title = "🌙 Récap de journée — Bien joué ! ✨"
            body  = f"{len(done_today)} tâche(s) accomplies.\n{len(pending)} en attente pour demain. Bonne nuit !"
        else:
            title = "🌙 Récap de journée"
            body  = f"{len(pending)} tâche(s) en attente. Demain sera parfait ! 💪" if pending else "Rien en attente. Excellent ! 🏆"

        send_push(sub, title, body, tag="evening")


# ══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

def setup_schedule():
    schedule.every().day.at("08:00").do(morning_notification)
    schedule.every().day.at("22:00").do(evening_recap)
    for hour in range(9, 22):
        schedule.every().day.at(f"{hour:02d}:00").do(urgent_hourly_reminder)

    log.info("📅 Scheduler configuré : 08:00 matin | 09-21:00 urgents | 22:00 soir")


if __name__ == "__main__":
    log.info("🚀 focus. scheduler démarré")
    setup_schedule()
    while True:
        schedule.run_pending()
        time.sleep(30)
