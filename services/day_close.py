"""Cloture de journee, version legere : recapitulatif calcule et note « pour demain ».

Le recapitulatif vient de ce qui est deja enregistre (taches terminees,
sessions chronometrees, journal) : aucune double saisie. Seule la note du
soir est ecrite a la main ; elle est relue le lendemain matin.
"""
from datetime import date, datetime, timedelta

from models import get_db
from models import session as session_model
from services import secrets_guard

MAX_NOTE = 4000


class DayCloseError(ValueError):
    """Saisie de cloture invalide."""


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10]) if value else date.today()
    except ValueError:
        raise DayCloseError("Date AAAA-MM-JJ attendue.") from None


def recap(day=None):
    """Bilan d'une journee : taches terminees, temps suivi par projet, journal, note du soir."""
    jour = _day(day).isoformat()
    db = get_db()
    done = [dict(row) for row in db.execute(
        "SELECT t.id, t.title, p.name AS project_name FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " WHERE t.status = 'done' AND date(t.done_at) = ? ORDER BY t.done_at", (jour,)
    ).fetchall()]
    sessions = session_model.list_sessions(jour, jour)
    by_project = {}
    for item in sessions:
        name = item["project_name"] or "Sans projet"
        by_project[name] = by_project.get(name, 0) + (item["duration_min"] or 0)
    journal = [dict(row) for row in db.execute(
        "SELECT n.id, n.kind, n.content, n.minutes, n.source, t.title AS task_title FROM note n"
        " LEFT JOIN task t ON t.id = n.task_id WHERE date(n.created_at) = ? ORDER BY n.created_at", (jour,)
    ).fetchall()]
    note = db.execute("SELECT tomorrow, closed_at FROM day_note WHERE day = ?", (jour,)).fetchone()
    return {
        "day": jour,
        "done": done,
        "tracked_min": sum(by_project.values()),
        "tracked_by_project": [{"project": k, "minutes": v} for k, v in sorted(by_project.items(), key=lambda x: -x[1])],
        "journal": journal,
        "tomorrow": note["tomorrow"] if note else "",
        "closed_at": note["closed_at"] if note else None,
    }


def close(day, tomorrow):
    """Enregistre (ou remplace) la note « pour demain » du jour ; renvoie le recapitulatif."""
    jour = _day(day).isoformat()
    texte = (tomorrow or "").strip()
    if len(texte) > MAX_NOTE:
        raise DayCloseError("Note trop longue.")
    hit = secrets_guard.find_secret(texte)
    if hit:
        raise DayCloseError(f"La note semble contenir un secret en clair ({hit}).")
    db = get_db()
    db.execute(
        "INSERT INTO day_note (day, tomorrow, closed_at) VALUES (?, ?, ?)"
        " ON CONFLICT(day) DO UPDATE SET tomorrow = excluded.tomorrow, closed_at = excluded.closed_at",
        (jour, texte, datetime.now().replace(microsecond=0).isoformat(sep=" ")),
    )
    db.commit()
    return recap(jour)


def morning_note(day=None):
    """Note laissee lors de la derniere cloture precedant ce jour (le vendredi pour un lundi), ou None."""
    jour = _day(day)
    row = get_db().execute(
        "SELECT day, tomorrow FROM day_note WHERE day < ? AND day >= ? AND tomorrow <> ''"
        " ORDER BY day DESC LIMIT 1",
        (jour.isoformat(), (jour - timedelta(days=7)).isoformat()),
    ).fetchone()
    return {"day": row["day"], "text": row["tomorrow"]} if row else None


def notes_between(start, end):
    """Notes du soir d'une periode (pour le bilan)."""
    rows = get_db().execute(
        "SELECT day, tomorrow FROM day_note WHERE day BETWEEN ? AND ? AND tomorrow <> '' ORDER BY day",
        (str(start), str(end)),
    ).fetchall()
    return [dict(row) for row in rows]
