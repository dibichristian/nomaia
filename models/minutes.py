"""Comptes rendus de reunion, d'intervention et de formation (tables minutes et minutes_item)."""
import re
from datetime import date, datetime

from models import get_db
from models import space as space_model

KINDS = ("meeting", "intervention", "training")
STATUSES = ("draft", "final")
ITEM_TYPES = ("decision", "action", "request")
TEXT_FIELDS = ("title", "attendees", "objectives", "discussed", "topics", "followups", "notes")
MAX_TEXT = 20000

# Libelles des champs, repris dans les messages du detecteur de secrets.
FIELD_LABELS = {
    "title": "Titre", "attendees": "Participants", "objectives": "Objectifs", "discussed": "Points discutés",
    "topics": "Sujets enseignés", "followups": "Points à revoir", "notes": "Notes", "text": "Élément",
    "owner": "Responsable",
}


class MinutesError(ValueError):
    """Saisie de compte rendu invalide."""


def _now():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _check_date(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise MinutesError(f"{field} : date AAAA-MM-JJ attendue.") from None


def _clean(data, partial):
    """Valeurs controlees d'une creation (partial=False) ou d'une modification."""
    values = {}
    for field in TEXT_FIELDS:
        if field in data:
            text = str(data[field] or "").strip() if field == "title" else str(data[field] or "")
            if len(text) > MAX_TEXT:
                raise MinutesError(f"{field} : texte trop long.")
            values[field] = text
    if "title" in values and not values["title"]:
        raise MinutesError("Le titre est obligatoire.")
    if "kind" in data:
        if data["kind"] not in KINDS:
            raise MinutesError("Type inconnu : meeting, intervention ou training.")
        values["kind"] = data["kind"]
    if "status" in data:
        if data["status"] not in STATUSES:
            raise MinutesError("Statut inconnu : draft ou final.")
        values["status"] = data["status"]
    if "held_on" in data:
        values["held_on"] = _check_date(data["held_on"], "held_on")
        if not values["held_on"]:
            raise MinutesError("La date de la séance est obligatoire.")
    if "project_id" in data:
        project_id = data["project_id"] or None
        if project_id is not None:
            if get_db().execute("SELECT 1 FROM project WHERE id = ?", (int(project_id),)).fetchone() is None:
                raise MinutesError("Projet introuvable.")
            project_id = int(project_id)
        values["project_id"] = project_id
    if data.get("space"):
        try:
            values["space"] = space_model.clean(data["space"])
        except space_model.SpaceError as error:
            raise MinutesError(str(error)) from None
    if values.get("project_id"):
        # Un compte rendu rattache a un projet en prend l'espace.
        values["space"] = space_model.for_record(project_id=values["project_id"])
    if not partial:
        for required in ("title", "held_on"):
            if required not in values:
                raise MinutesError(f"Champ obligatoire : {required}.")
    return values


def _minutes_dict(row):
    return dict(row) if row else None


def get(minutes_id):
    """Compte rendu avec ses elements, ou None."""
    row = get_db().execute(
        "SELECT m.*, p.name AS project_name, p.color AS project_color FROM minutes m"
        " LEFT JOIN project p ON p.id = m.project_id WHERE m.id = ?",
        (minutes_id,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["items"] = list_items(minutes_id)
    return data


def list_minutes(project_id=None, limit=200, space=None):
    """Comptes rendus du plus recent au plus ancien, avec le nombre d'elements par type.

    :param space: convention de models/space.py
    """
    sql = (
        "SELECT m.id, m.title, m.kind, m.held_on, m.status, m.project_id, m.event_id, m.updated_at,"
        " m.space,"
        " p.name AS project_name, p.color AS project_color,"
        " SUM(i.type = 'decision') AS decisions, SUM(i.type = 'action') AS actions,"
        " SUM(i.type = 'action' AND i.task_id IS NULL) AS actions_open"
        " FROM minutes m LEFT JOIN project p ON p.id = m.project_id"
        " LEFT JOIN minutes_item i ON i.minutes_id = m.id"
    )
    filtre, params = space_model.clause("m.space", space)
    sql += " WHERE 1 = 1" + filtre
    if project_id:
        sql += " AND m.project_id = ?"
        params.append(project_id)
    sql += " GROUP BY m.id ORDER BY m.held_on DESC, m.id DESC LIMIT ?"
    params.append(limit)
    rows = get_db().execute(sql, params).fetchall()
    return [{**dict(row), "decisions": row["decisions"] or 0, "actions": row["actions"] or 0,
             "actions_open": row["actions_open"] or 0} for row in rows]


def create(data):
    """Cree un compte rendu ; renvoie le compte rendu complet."""
    values = _clean(data, partial=False)
    values.setdefault("kind", "meeting")
    values.setdefault("space", space_model.for_new())
    if data.get("event_id"):
        values["event_id"] = int(data["event_id"])
    if data.get("starts_at"):
        values["starts_at"] = str(data["starts_at"])[:16]
    columns = ", ".join(values)
    db = get_db()
    cursor = db.execute(
        f"INSERT INTO minutes ({columns}) VALUES ({', '.join('?' * len(values))})", list(values.values())
    )
    db.commit()
    return get(cursor.lastrowid)


def for_session(event_id, held_on):
    """Compte rendu existant d'une seance, ou None."""
    row = get_db().execute(
        "SELECT id FROM minutes WHERE event_id = ? AND held_on = ?", (event_id, held_on)
    ).fetchone()
    return get(row["id"]) if row else None


def create_from_event(event_id, held_on=None, kind=None):
    """Compte rendu d'une seance d'un evenement de l'agenda ; l'existant est renvoye s'il y en a un.

    :return: (compte rendu, cree) — cree vaut False si la seance avait deja le sien
    """
    event = get_db().execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise MinutesError("Événement introuvable.")
    day = _check_date(held_on, "held_on") or event["starts_at"][:10]
    existing = for_session(event_id, day)
    if existing:
        return existing, False
    time = event["starts_at"][11:16] if len(event["starts_at"]) >= 16 else ""
    return create({
        "event_id": event_id, "title": event["title"], "held_on": day, "project_id": event["project_id"],
        "starts_at": f"{day} {time}".strip(), "kind": kind or "meeting", "notes": "",
        "space": event["space"],
    }), True


def update(minutes_id, data):
    """Modifie un compte rendu (sauvegarde automatique) ; renvoie le compte rendu complet ou None."""
    if get_db().execute("SELECT 1 FROM minutes WHERE id = ?", (minutes_id,)).fetchone() is None:
        return None
    values = _clean(data, partial=True)
    if values:
        values["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        db = get_db()
        db.execute(f"UPDATE minutes SET {assignments} WHERE id = ?", [*values.values(), minutes_id])
        db.commit()
    return get(minutes_id)


def delete(minutes_id):
    """Supprime un compte rendu et ses elements ; les taches et entrees de registre creees restent."""
    db = get_db()
    cursor = db.execute("DELETE FROM minutes WHERE id = ?", (minutes_id,))
    db.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------- Elements

def list_items(minutes_id):
    rows = get_db().execute(
        "SELECT i.*, t.status AS task_status, t.title AS task_title FROM minutes_item i"
        " LEFT JOIN task t ON t.id = i.task_id WHERE i.minutes_id = ? ORDER BY i.position, i.id",
        (minutes_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_item(item_id):
    row = get_db().execute("SELECT * FROM minutes_item WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def _clean_item(data, partial):
    values = {}
    if "type" in data or not partial:
        if data.get("type") not in ITEM_TYPES:
            raise MinutesError("Type d'élément inconnu : decision, action ou request.")
        values["type"] = data["type"]
    if "text" in data or not partial:
        text = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
        if not text:
            raise MinutesError("Le texte de l'élément est obligatoire.")
        values["text"] = text[:2000]
    if "owner" in data:
        values["owner"] = str(data["owner"] or "").strip()[:200]
    if "due" in data:
        values["due"] = _check_date(data["due"], "due")
    return values


def validate_item(data):
    """Controle un element avant ecriture (sans rien enregistrer) ; leve MinutesError."""
    return _clean_item(data, partial=False)


def add_item(minutes_id, data):
    if get_db().execute("SELECT 1 FROM minutes WHERE id = ?", (minutes_id,)).fetchone() is None:
        return None
    values = _clean_item(data, partial=False)
    db = get_db()
    values["minutes_id"] = minutes_id
    values["position"] = db.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM minutes_item WHERE minutes_id = ?", (minutes_id,)
    ).fetchone()[0]
    cursor = db.execute(
        f"INSERT INTO minutes_item ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    db.execute("UPDATE minutes SET updated_at = ? WHERE id = ?", (_now(), minutes_id))
    db.commit()
    return get_item(cursor.lastrowid)


def update_item(item_id, data):
    if get_item(item_id) is None:
        return None
    values = _clean_item(data, partial=True)
    if values:
        db = get_db()
        db.execute(f"UPDATE minutes_item SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                   [*values.values(), item_id])
        db.commit()
    return get_item(item_id)


def delete_item(item_id):
    db = get_db()
    cursor = db.execute("DELETE FROM minutes_item WHERE id = ?", (item_id,))
    db.commit()
    return cursor.rowcount > 0


def link_item(item_id, task_id=None, note_id=None):
    """Memorise la tache ou l'entree de registre nee d'un element."""
    db = get_db()
    if task_id is not None:
        db.execute("UPDATE minutes_item SET task_id = ? WHERE id = ?", (task_id, item_id))
    if note_id is not None:
        db.execute("UPDATE minutes_item SET note_id = ? WHERE id = ?", (note_id, item_id))
    db.commit()
