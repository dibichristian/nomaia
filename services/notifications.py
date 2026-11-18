"""Notifications de l'en-tete (cloche) : ce qui merite un coup d'oeil maintenant.

Reunions qui commencent bientot, echeances du jour, taches en retard,
entrees a traiter, relances depassees. Lu a la demande, rien n'est
stocke : la cloche reflete l'etat present (Telegram garde ses propres envois).
"""
from datetime import date, datetime, timedelta

from models import capture as capture_model
from models import event as event_model
from models import get_db
from models import person as person_model

SOON_MINUTES = 120


def current(now=None):
    """Liste de {level: danger|warn|info, text, url}, la plus urgente d'abord."""
    now = now or datetime.now()
    today = now.date()
    items = []
    for ev in event_model.list_events(today, today):
        try:
            start = datetime.fromisoformat(str(ev["starts_at"])[:16])
        except ValueError:
            continue
        if now <= start <= now + timedelta(minutes=SOON_MINUTES):
            minutes = int((start - now).total_seconds() // 60)
            items.append({"level": "warn" if minutes <= 30 else "info",
                          "text": f"{ev['title']} dans {minutes} min" if minutes else f"{ev['title']} commence",
                          "url": f"/agenda?date={today.isoformat()}"})
    db = get_db()
    due_today = db.execute(
        "SELECT id, title FROM task WHERE status <> 'done' AND deadline = ? ORDER BY priority LIMIT 5",
        (today.isoformat(),),
    ).fetchall()
    for row in due_today:
        items.append({"level": "warn", "text": f"Échéance aujourd'hui : {row['title']}", "url": f"/plan?tache={row['id']}"})
    late = db.execute(
        "SELECT COUNT(*) FROM task WHERE status <> 'done' AND deadline IS NOT NULL AND deadline < ?",
        (today.isoformat(),),
    ).fetchone()[0]
    if late:
        items.append({"level": "danger", "text": f"{late} tâche{'s' if late > 1 else ''} en retard", "url": "/taches"})
    overdue = [w for w in person_model.list_waiting(status="open") if w.get("overdue")]
    if overdue:
        items.append({"level": "warn", "text": f"{len(overdue)} relance{'s' if len(overdue) > 1 else ''} dépassée{'s' if len(overdue) > 1 else ''}",
                      "url": "/contacts"})
    pending = capture_model.count_open_captures()
    if pending:
        items.append({"level": "info", "text": f"{pending} entrée{'s' if pending > 1 else ''} à traiter", "url": "/"})
    order = {"danger": 0, "warn": 1, "info": 2}
    items.sort(key=lambda item: order[item["level"]])
    return items
