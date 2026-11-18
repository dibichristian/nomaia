"""Suivi du temps passe : sessions de travail sur une tache.

Une seule session peut etre ouverte a la fois : demarrer un chronometre
arrete automatiquement le precedent.
"""
from datetime import datetime, timedelta

from models import get_db


def _now():
    """Horodatage local a la seconde, format SQLite."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def running():
    """Retourne la session en cours, ou None."""
    row = get_db().execute(
        "SELECT s.*, t.title AS task_title, t.priority AS task_priority"
        " FROM session s JOIN task t ON t.id = s.task_id"
        " WHERE s.ended_at IS NULL ORDER BY s.id DESC LIMIT 1"
    ).fetchone()

    if row is None:
        return None

    data = dict(row)
    started = datetime.fromisoformat(data["started_at"])
    data["elapsed_min"] = int((datetime.now() - started).total_seconds() // 60)
    return data


def start(task_id):
    """Demarre le chronometre sur une tache, en fermant la session precedente."""
    stop()

    db = get_db()
    cursor = db.execute(
        "INSERT INTO session (task_id, started_at) VALUES (?, ?)", (task_id, _now())
    )
    db.commit()
    return cursor.lastrowid


def stop():
    """Arrete la session en cours. Retourne la duree en minutes, ou None."""
    db = get_db()
    row = db.execute(
        "SELECT id, started_at FROM session WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if row is None:
        return None

    ended = datetime.now().replace(microsecond=0)
    started = datetime.fromisoformat(row["started_at"])
    # Une session est comptee au moins une minute : un clic n'est pas zero.
    duration = max(1, int((ended - started).total_seconds() // 60))

    db.execute(
        "UPDATE session SET ended_at = ?, duration_min = ? WHERE id = ?",
        (ended.isoformat(sep=" "), duration, row["id"]),
    )
    db.commit()
    return duration


def record(task_id, minutes, source=None):
    """Enregistre apres coup une session de travail terminee.

    Sert au temps declare par une session Claude : il n'y a pas de
    chronometre, seulement une duree. La session se termine maintenant et
    commence `minutes` plus tot, pour tomber dans la bonne journee du bilan.

    :return: identifiant de la session creee
    """
    fin = datetime.now().replace(microsecond=0)
    debut = fin - timedelta(minutes=int(minutes))
    db = get_db()
    curseur = db.execute(
        "INSERT INTO session (task_id, started_at, ended_at, duration_min, source)"
        " VALUES (?, ?, ?, ?, ?)",
        (task_id, debut.isoformat(sep=" "), fin.isoformat(sep=" "), int(minutes), source),
    )
    db.commit()
    return curseur.lastrowid


def tracked_for_task(task_id):
    """Retourne le temps total suivi sur une tache, en minutes."""
    row = get_db().execute(
        "SELECT COALESCE(SUM(duration_min), 0) AS total FROM session WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    return row["total"]


def list_sessions(start_date, end_date):
    """Liste les sessions terminees d'une periode (dates ISO incluses)."""
    rows = get_db().execute(
        "SELECT s.*, t.title AS task_title, t.project_id, p.name AS project_name"
        " FROM session s"
        " JOIN task t ON t.id = s.task_id"
        " LEFT JOIN project p ON p.id = t.project_id"
        " WHERE s.ended_at IS NOT NULL AND date(s.started_at) BETWEEN ? AND ?"
        " ORDER BY s.started_at ASC",
        (start_date, end_date),
    ).fetchall()
    return [dict(row) for row in rows]
