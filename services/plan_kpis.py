"""Indicateurs du plan du jour, vus a une date donnee, et leur detail.

Chaque indicateur se lit pour le jour affiche, passe compris :
- temps suivi : sessions de ce jour-la (session en cours comprise aujourd'hui) ;
- en retard : taches dont l'echeance etait depassee et qui restaient ouvertes
  a la fin de ce jour-la ;
- attente client : taches cote client ouvertes ce jour-la, et attentes encore
  ouvertes nees avant ou ce jour-la.

Limites assumees : le cote (client, nous) d'une tache est celui d'aujourd'hui,
et une attente close n'a pas de date de cloture, elle ne compte donc plus
dans le passe.
"""
from datetime import date, datetime

from models import get_db
from models import session as session_model
from services import plan_history

KINDS = ("tracked", "late", "client")

# Ouvert « a la fin du jour » : pas encore cree apres ce jour, et pas termine
# avant ou ce jour-la.
_OPEN_ON = (
    "date(t.created_at) <= :day"
    " AND (t.status <> 'done' OR date(t.done_at) > :day)"
)


def _days_between(start_iso, day):
    """Jours ecoules entre une date ISO (date ou date-heure) et le jour vu."""
    if not start_iso:
        return None
    return max(0, (day - date.fromisoformat(str(start_iso)[:10])).days)


def _late_rows(day):
    """Taches en retard a la fin du jour vu, la plus en retard d'abord."""
    return get_db().execute(
        "SELECT t.id, t.title, t.deadline, t.priority, p.name AS project_name"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        f" WHERE t.deadline IS NOT NULL AND t.deadline < :day AND {_OPEN_ON}"
        " ORDER BY t.deadline, t.priority",
        {"day": day.isoformat()},
    ).fetchall()


def _client_rows(day):
    """Ce qui attend le client au jour vu : taches cote client puis attentes ouvertes."""
    db = get_db()
    tasks = db.execute(
        "SELECT t.id, t.title, t.created_at, p.name AS project_name, pe.name AS person_name"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " LEFT JOIN person pe ON pe.id = t.person_id"
        f" WHERE t.side IN ('client', 'both') AND {_OPEN_ON}"
        " ORDER BY t.created_at",
        {"day": day.isoformat()},
    ).fetchall()
    waits = db.execute(
        "SELECT w.id, w.subject, w.since, w.task_id, pe.name AS person_name"
        " FROM waiting w JOIN person pe ON pe.id = w.person_id"
        " WHERE w.status = 'open' AND date(w.since) <= :day"
        " ORDER BY w.since",
        {"day": day.isoformat()},
    ).fetchall()
    return tasks, waits


def kpis(day):
    """Valeurs des indicateurs du jour vu (le planifie vient du plan lui-meme).

    :param day: date affichee dans le plan
    :return: {tracked_min (None pour un jour a venir), late_count,
        client: {count, oldest_days}}
    """
    tasks, waits = _client_rows(day)
    starts = [row["created_at"] for row in tasks] + [row["since"] for row in waits]
    oldest = min((str(value)[:10] for value in starts if value), default=None)
    return {
        "tracked_min": plan_history.tracked_on(day) if day <= date.today() else None,
        "late_count": len(_late_rows(day)),
        "client": {"count": len(tasks) + len(waits), "oldest_days": _days_between(oldest, day)},
    }


def _tracked_detail(day):
    """Sessions du jour vu, dans l'ordre, session en cours comprise aujourd'hui."""
    items = [
        {
            "task_id": row["task_id"],
            "title": row["task_title"],
            "project_name": row["project_name"],
            "start": row["started_at"][11:16],
            "end": (row["ended_at"] or "")[11:16],
            "minutes": int(row["duration_min"] or 0),
            "running": False,
        }
        for row in session_model.list_sessions(day.isoformat(), day.isoformat())
    ]
    running = session_model.running()
    if running and day == date.today() and running["started_at"][:10] == day.isoformat():
        items.append({
            "task_id": running["task_id"],
            "title": running["task_title"],
            "project_name": None,
            "start": running["started_at"][11:16],
            "end": datetime.now().strftime("%H:%M"),
            "minutes": int(running["elapsed_min"]),
            "running": True,
        })
    return {"items": items, "total_min": sum(item["minutes"] for item in items)}


def _late_detail(day):
    """Taches en retard au jour vu, avec le retard en jours."""
    return {
        "items": [
            {
                "task_id": row["id"],
                "title": row["title"],
                "project_name": row["project_name"],
                "priority": row["priority"],
                "deadline": row["deadline"],
                "days_late": _days_between(row["deadline"], day),
            }
            for row in _late_rows(day)
        ]
    }


def _client_detail(day):
    """Taches cote client et attentes ouvertes au jour vu, avec leur anciennete."""
    tasks, waits = _client_rows(day)
    items = [
        {
            "kind": "task",
            "task_id": row["id"],
            "title": row["title"],
            "person_name": row["person_name"],
            "project_name": row["project_name"],
            "since": str(row["created_at"])[:10],
            "days": _days_between(row["created_at"], day),
        }
        for row in tasks
    ] + [
        {
            "kind": "waiting",
            "task_id": row["task_id"],
            "title": row["subject"],
            "person_name": row["person_name"],
            "project_name": None,
            "since": str(row["since"])[:10],
            "days": _days_between(row["since"], day),
        }
        for row in waits
    ]
    items.sort(key=lambda item: item["since"])
    return {"items": items}


def detail(kind, day):
    """Detail d'un indicateur au jour vu.

    :param kind: 'tracked', 'late' ou 'client'
    :param day: date affichee dans le plan
    :raises ValueError: indicateur inconnu
    """
    if kind == "tracked":
        return _tracked_detail(day)
    if kind == "late":
        return _late_detail(day)
    if kind == "client":
        return _client_detail(day)
    raise ValueError(f"Indicateur inconnu : {kind}.")
