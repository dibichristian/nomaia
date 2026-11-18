"""Captures brutes, propositions de l'IA et fil de dialogue."""
import json
from datetime import datetime

from models import get_db
from models import space as space_model

VALID_SOURCES = ("web", "telegram", "email", "outlook")
VALID_STATUS = ("new", "processing", "waiting", "done", "failed")

# Nombre d'echecs moteur toleres sur une meme capture avant abandon. Le
# compteur ne mesure QUE les pannes du moteur : une reponse de l'utilisateur remet
# le budget a zero (voir requeue), sinon un dialogue de quatre questions
# condamnerait une capture parfaitement saine.
MAX_ATTEMPTS = 4


def create_capture(raw_text=None, image_path=None, source="web", space=None):
    """Enregistre une entree brute a traiter. Retourne la capture creee.

    :param space: espace impose (mail Outlook : 'pro') ; sinon celui du
        perimetre ou du selecteur (models/space.for_new). Le tri la deplace si
        ses taches tombent dans un projet de l'autre espace.
    """
    text = (raw_text or "").strip() or None
    if not text and not image_path:
        raise ValueError("Une capture doit contenir du texte ou une image.")
    if source not in VALID_SOURCES:
        raise ValueError("Source de capture inconnue.")

    db = get_db()
    cursor = db.execute(
        "INSERT INTO capture (raw_text, image_path, source, space) VALUES (?, ?, ?, ?)",
        (text, image_path, source, space_model.for_new(space)),
    )
    db.commit()
    return get_capture(cursor.lastrowid)


# Duree de vie d'un lien message Telegram -> capture. Au-dela, repondre a un
# vieux message n'a plus de sens : la capture est close depuis longtemps.
TELEGRAM_LINK_DAYS = 60


def link_telegram_message(message_id, capture_id):
    """Associe un message Telegram envoye a la capture dont il parle.

    Purge au passage les liens expires : la table reste minuscule sans tache
    de menage dediee.
    """
    if not message_id:
        return
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO telegram_link (message_id, capture_id) VALUES (?, ?)",
        (int(message_id), capture_id),
    )
    db.execute(
        "DELETE FROM telegram_link WHERE created_at < datetime('now', 'localtime', ?)",
        (f"-{TELEGRAM_LINK_DAYS} days",),
    )
    db.commit()


def capture_for_telegram_message(message_id):
    """Retourne la capture liee a un message Telegram, ou None."""
    if not message_id:
        return None
    row = get_db().execute(
        "SELECT capture_id FROM telegram_link WHERE message_id = ?", (int(message_id),)
    ).fetchone()
    return get_capture(row["capture_id"]) if row else None


def get_capture(capture_id):
    """Retourne une capture, ou None."""
    row = get_db().execute("SELECT * FROM capture WHERE id = ?", (capture_id,)).fetchone()
    return dict(row) if row else None


def list_captures(limit=30, open_only=False, space=None):
    """Liste les captures recentes, la plus recente en premier.

    :param open_only: ne garde que les entrees a traiter (statut autre que
        'done'), anciennes comprises
    :param space: convention de models/space.py
    """
    filtre, valeurs = space_model.clause("space", space)
    where = " WHERE 1 = 1" + (" AND status <> 'done'" if open_only else "") + filtre
    rows = get_db().execute(
        "SELECT * FROM capture" + where + " ORDER BY id DESC LIMIT ?", (*valeurs, limit)
    ).fetchall()
    return [dict(row) for row in rows]


def count_open_captures(space=None):
    """Nombre d'entrees a traiter (statut autre que 'done') : badge du menu et cloche."""
    filtre, valeurs = space_model.clause("space", space)
    return get_db().execute(
        "SELECT COUNT(*) FROM capture WHERE status <> 'done'" + filtre, valeurs
    ).fetchone()[0]


def set_space(capture_id, space):
    """Range une capture dans un espace (le tri la suit vers ses taches)."""
    db = get_db()
    db.execute("UPDATE capture SET space = ? WHERE id = ?", (space_model.clean(space), capture_id))
    db.commit()


def claim_next_capture():
    """Reserve la plus ancienne capture en attente et la passe en 'processing'.

    L'UPDATE conditionne sur status = 'new' : deux workers ne peuvent pas
    prendre la meme ligne.
    """
    db = get_db()
    row = db.execute(
        "SELECT id FROM capture WHERE status = 'new' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row is None:
        return None

    cursor = db.execute(
        "UPDATE capture SET status = 'processing', attempts = attempts + 1"
        " WHERE id = ? AND status = 'new'",
        (row["id"],),
    )
    db.commit()
    if cursor.rowcount == 0:
        return None
    return get_capture(row["id"])


def set_status(capture_id, status, error=None):
    """Met a jour l'etat d'une capture."""
    if status not in VALID_STATUS:
        raise ValueError("Statut de capture invalide.")

    processed_at = None
    if status in ("done", "failed"):
        processed_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    db = get_db()
    db.execute(
        "UPDATE capture SET status = ?, error = ?, processed_at = ? WHERE id = ?",
        (status, error, processed_at, capture_id),
    )
    if status == "done":
        # Une capture close n'attend plus de validation : ses propositions
        # restees en attente sont rejetees, sinon elles gonfleraient les compteurs.
        _reject_pending_proposals(db, capture_id)
    db.commit()


def requeue(capture_id, reset_attempts=False):
    """Remet une capture dans la file.

    :param reset_attempts: remet le compteur d'echecs a zero. A utiliser quand
        la relance vient d'une action humaine (reponse a une question) et non
        d'une panne du moteur : le dialogue ne doit pas epuiser le budget de
        tentatives.
    """
    db = get_db()
    if reset_attempts:
        db.execute(
            "UPDATE capture SET status = 'new', error = NULL, attempts = 0 WHERE id = ?",
            (capture_id,),
        )
    else:
        db.execute(
            "UPDATE capture SET status = 'new', error = NULL WHERE id = ?", (capture_id,)
        )
    db.commit()


def reset_stuck_captures():
    """Repasse en file les captures laissees en 'processing' par un arret brutal."""
    db = get_db()
    cursor = db.execute(
        "UPDATE capture SET status = 'new' WHERE status = 'processing' AND attempts < ?",
        (MAX_ATTEMPTS,),
    )
    db.execute(
        "UPDATE capture SET status = 'failed', error = 'Trop de tentatives.'"
        " WHERE status = 'processing' AND attempts >= ?",
        (MAX_ATTEMPTS,),
    )
    db.commit()
    return cursor.rowcount


# --- Propositions -----------------------------------------------------------


def create_proposal(capture_id, payload):
    """Enregistre la sortie structuree de l'IA pour une capture.

    Les propositions precedentes encore en attente sont rejetees : seule la
    derniere analyse d'une capture reste a valider.
    """
    db = get_db()
    _reject_pending_proposals(db, capture_id)
    cursor = db.execute(
        "INSERT INTO proposal (capture_id, summary, confidence, payload_json)"
        " VALUES (?, ?, ?, ?)",
        (
            capture_id,
            payload.get("summary"),
            float(payload.get("confidence") or 0),
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    db.commit()
    return get_proposal(cursor.lastrowid)


def _reject_pending_proposals(db, capture_id):
    """Rejette les propositions encore en attente d'une capture (sans commit)."""
    db.execute(
        "UPDATE proposal SET status = 'rejected' WHERE capture_id = ? AND status = 'pending'",
        (capture_id,),
    )


def get_proposal(proposal_id):
    """Retourne une proposition avec sa charge utile deserialisee."""
    row = get_db().execute(
        "SELECT * FROM proposal WHERE id = ?", (proposal_id,)
    ).fetchone()
    return _proposal_to_dict(row) if row else None


def latest_proposal(capture_id):
    """Retourne la derniere proposition liee a une capture."""
    row = get_db().execute(
        "SELECT * FROM proposal WHERE capture_id = ? ORDER BY id DESC LIMIT 1",
        (capture_id,),
    ).fetchone()
    return _proposal_to_dict(row) if row else None


def latest_proposals(capture_ids):
    """Derniere proposition de chaque capture, en une requete.

    :return: dictionnaire {capture_id: proposition} ; une capture sans
        proposition est absente
    """
    ids = [int(capture_id) for capture_id in capture_ids]
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    rows = get_db().execute(
        "SELECT * FROM proposal WHERE id IN ("
        " SELECT MAX(id) FROM proposal WHERE capture_id IN (" + marks + ") GROUP BY capture_id)",
        ids,
    ).fetchall()
    return {row["capture_id"]: _proposal_to_dict(row) for row in rows}


def list_pending_proposals(space=None):
    """Liste les propositions en attente de validation humaine (espace de leur capture)."""
    filtre, valeurs = space_model.clause("c.space", space)
    rows = get_db().execute(
        "SELECT p.* FROM proposal p JOIN capture c ON c.id = p.capture_id"
        " WHERE p.status = 'pending'" + filtre + " ORDER BY p.id DESC",
        valeurs,
    ).fetchall()
    return [_proposal_to_dict(row) for row in rows]


def count_pending_proposals(space=None):
    """Nombre de propositions en attente de validation (badge du menu)."""
    filtre, valeurs = space_model.clause("c.space", space)
    return get_db().execute(
        "SELECT COUNT(*) FROM proposal p JOIN capture c ON c.id = p.capture_id"
        " WHERE p.status = 'pending'" + filtre,
        valeurs,
    ).fetchone()[0]


def set_proposal_status(proposal_id, status):
    """Marque une proposition comme appliquee ou rejetee."""
    if status not in ("pending", "applied", "rejected"):
        raise ValueError("Statut de proposition invalide.")
    db = get_db()
    db.execute("UPDATE proposal SET status = ? WHERE id = ?", (status, proposal_id))
    db.commit()


def _proposal_to_dict(row):
    """Convertit une ligne proposal en dictionnaire, payload JSON inclus."""
    data = dict(row)
    try:
        data["payload"] = json.loads(data.pop("payload_json"))
    except (TypeError, ValueError):
        data["payload"] = {}
    return data


# --- Dialogue ---------------------------------------------------------------


def add_message(capture_id, role, content):
    """Ajoute un tour de dialogue ('nomaia' ou 'user')."""
    if role not in ("nomaia", "user"):
        raise ValueError("Role de message invalide.")
    content = (content or "").strip()
    if not content:
        raise ValueError("Le message est vide.")

    db = get_db()
    db.execute(
        "INSERT INTO message (capture_id, role, content) VALUES (?, ?, ?)",
        (capture_id, role, content),
    )
    db.commit()


def list_messages(capture_id):
    """Retourne le fil de dialogue d'une capture, dans l'ordre chronologique."""
    rows = get_db().execute(
        "SELECT * FROM message WHERE capture_id = ? ORDER BY id ASC", (capture_id,)
    ).fetchall()
    return [dict(row) for row in rows]
