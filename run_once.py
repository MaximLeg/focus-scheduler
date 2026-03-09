#!/usr/bin/env python3
"""
scheduler/run_once.py
Exécuté par GitHub Actions toutes les heures.
Détermine quelle(s) action(s) lancer selon l'heure actuelle (UTC+2 Paris).
"""

import os, json, logging, calendar
from datetime import date, datetime, timedelta, timezone

import requests
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

# Heure locale Paris (UTC+1 hiver, UTC+2 été)
# GitHub Actions tourne en UTC — on ajoute le décalage manuellement
def paris_hour():
    utc_now = datetime.now(timezone.utc)
    # Calcul DST simplifié : UTC+2 de fin mars à fin octobre, UTC+1 sinon
    month = utc_now.month
    offset = 2 if 3 < month < 11 or (month == 3 and utc_now.day >= 25) or (month == 10 and utc_now.day < 25) else 1
    return (utc_now.hour + offset) % 24

# ── Supabase helpers ──────────────────────────────────────────────────────────

def get_all_subscriptions():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/push_subscriptions", headers=HEADERS, params={"select": "*"})
    r.raise_for_status(); return r.json()

def get_pending_tasks(user_id):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "user_id": f"eq.{user_id}", "done": "eq.false", "recurrence": "is.null"})
    r.raise_for_status(); return r.json()

def get_recurring_tasks(user_id):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "user_id": f"eq.{user_id}", "recurrence": "not.is.null"})
    r.raise_for_status(); return r.json()

def get_completions_for_date(user_id, date_str):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/task_completions", headers=HEADERS,
        params={"select": "task_id", "user_id": f"eq.{user_id}", "completed_date": f"eq.{date_str}"})
    r.raise_for_status()
    return {row["task_id"] for row in r.json()}

def get_all_recurring_tasks():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "recurrence": "not.is.null"})
    r.raise_for_status(); return r.json()

def update_task(task_id, data):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/tasks",
        headers={**HEADERS, "Prefer": "return=minimal"},
        params={"id": f"eq.{task_id}"}, json=data)
    r.raise_for_status()

def get_done_today(user_id):
    today = date.today().isoformat()
    r = requests.get(f"{SUPABASE_URL}/rest/v1/tasks", headers=HEADERS,
        params={"select": "*", "user_id": f"eq.{user_id}", "done": "eq.true", "done_at": f"gte.{today}T00:00:00"})
    r.raise_for_status(); return r.json()

# ── Récurrence ────────────────────────────────────────────────────────────────

def next_occurrence(deadline_str, recurrence):
    d = date.fromisoformat(deadline_str)
    if   recurrence == "daily":    d += timedelta(days=1)
    elif recurrence == "weekdays":
        d += timedelta(days=1)
        while d.weekday() >= 5: d += timedelta(days=1)
    elif recurrence == "weekly":   d += timedelta(weeks=1)
    elif recurrence == "biweekly": d += timedelta(weeks=2)
    elif recurrence == "monthly":
        month = d.month % 12 + 1
        year  = d.year + (1 if d.month == 12 else 0)
        day   = min(d.day, calendar.monthrange(year, month)[1])
        d     = d.replace(year=year, month=month, day=day)
    elif recurrence == "yearly":   d = d.replace(year=d.year + 1)
    else: return None
    return d.isoformat()

def renew_recurring_tasks():
    log.info("🔁 Renouvellement récurrences...")
    today = date.today().isoformat()
    tasks = get_all_recurring_tasks()
    renewed = 0
    for task in tasks:
        deadline   = task.get("deadline", "")
        recurrence = task.get("recurrence")
        if not deadline or not recurrence or deadline >= today:
            continue
        next_date = next_occurrence(deadline, recurrence)
        if not next_date: continue
        update_task(task["id"], {"deadline": next_date, "done": False, "done_at": None})
        log.info(f"  ↻ '{task['title']}' → {next_date}")
        renewed += 1
    log.info(f"🔁 {renewed} renouvelée(s)")

# ── Notifications ─────────────────────────────────────────────────────────────

def send_push(sub, title, body, tag="focus", urgent=False):
    payload = json.dumps({"title": title, "body": body, "tag": tag, "urgent": urgent})
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=payload, vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_EMAIL},
        )
        log.info(f"  ✅ notif envoyée")
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            requests.delete(f"{SUPABASE_URL}/rest/v1/push_subscriptions",
                headers=HEADERS, params={"endpoint": f"eq.{sub['endpoint']}"})
        else: log.error(f"  ❌ {e}")
    except Exception as e:
        log.error(f"  ❌ {e}")

def morning_notification():
    renew_recurring_tasks()
    log.info("🌅 Notification matin...")
    subs  = get_all_subscriptions()
    today = date.today().isoformat()
    for sub in subs:
        uid          = sub["user_id"]
        pending      = get_pending_tasks(uid)
        recurring    = get_recurring_tasks(uid)
        done_today   = get_completions_for_date(uid, today)
        recur_today  = [t for t in recurring if t.get("deadline") == today and t["id"] not in done_today]
        all_today    = [t for t in pending if t.get("deadline") == today] + recur_today
        urgent_total = [t for t in all_today if t.get("urgency") == "urgent"]

        if not all_today and not pending:
            send_push(sub, "🌅 Bonne journée !", "Aucune tâche. Profitez-en ! ✨", tag="morning")
            continue
        parts = []
        if all_today:    parts.append(f"{len(all_today)} tâche(s) aujourd'hui")
        if urgent_total: parts.append(f"{len(urgent_total)} urgente(s)")
        if urgent_total: parts += [f"• {t['title']}" for t in urgent_total[:3]]
        send_push(sub, "🌅 Votre journée commence", "\n".join(parts), tag="morning")

def urgent_hourly_reminder():
    log.info("🔴 Rappels urgents...")
    subs  = get_all_subscriptions()
    today = date.today().isoformat()
    for sub in subs:
        uid          = sub["user_id"]
        done_today   = get_completions_for_date(uid, today)
        pending      = get_pending_tasks(uid)
        recurring    = get_recurring_tasks(uid)
        urgent_plain = [t for t in pending if t.get("urgency") == "urgent" and t.get("deadline") == today]
        urgent_recur = [t for t in recurring if t.get("urgency") == "urgent" and t.get("deadline") == today and t["id"] not in done_today]
        urgent_today = urgent_plain + urgent_recur
        if not urgent_today: continue
        nb    = len(urgent_today)
        title = f"🔴 {nb} urgence{' restante' if nb==1 else 's restantes'} aujourd'hui"
        body  = "\n".join(f"• {t['title']}" for t in urgent_today)
        send_push(sub, title, body, tag="urgent-reminder", urgent=True)

def evening_recap():
    log.info("🌙 Récap soir...")
    subs  = get_all_subscriptions()
    today = date.today().isoformat()
    for sub in subs:
        uid        = sub["user_id"]
        done_plain = get_done_today(uid)
        done_recur = get_completions_for_date(uid, today)
        total_done = len(done_plain) + len(done_recur)
        pending    = get_pending_tasks(uid)
        if total_done:
            title = "🌙 Bien joué aujourd'hui ! ✨"
            body  = f"{total_done} tâche(s) accomplies.\n{len(pending)} en attente pour demain."
        else:
            title = "🌙 Récap de journée"
            body  = f"{len(pending)} tâche(s) en attente. Demain sera parfait ! 💪" if pending else "Rien en attente. Excellent ! 🏆"
        send_push(sub, title, body, tag="evening")

# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    hour = paris_hour()
    log.info(f"⏰ Heure Paris : {hour}h")

    if hour == 8:
        morning_notification()
    elif hour == 22:
        evening_recap()
    elif 9 <= hour <= 21:
        urgent_hourly_reminder()
    else:
        log.info("💤 Hors plage horaire, rien à faire.")
