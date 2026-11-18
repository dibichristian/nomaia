"""Journal de travail : notes, avancement et temps declares sur une tache ou un projet.

Une ligne de journal dit ce qui s'est passe, pas ce qui reste a faire :

- **note**     : un commentaire libre (decision, blocage, contexte) ;
- **progress** : un point d'avancement, avec le pourcentage atteint ;
- **work**     : du travail fait, avec sa duree ;
- **decision**, **scope**, **dependency** : le registre du projet (decision
  actee, changement de perimetre, dependance). Un changement de perimetre ou
  une dependance a suivre porte sur une tache de suivi, dont le statut dit
  s'il est regle.

La source distingue ce que l'utilisateur a saisi (`NULL`) de ce qu'une session
Claude a declare (`claude`). Le bilan s'appuie sur ce journal : c'est lui qui
donne un suivi reel, la ou le chronometre est souvent oublie.

Table `note` reutilisee : elle existait sans etre exploitee, et ses colonnes
(tache, contenu, date) sont exactement le coeur d'une ligne de journal.
"""
from models import get_db
from models import space as space_model
from services import secrets_guard

VALID_KINDS = ("note", "progress", "work", "decision", "scope", "dependency")

# Types d'entree qui forment le registre d'un projet.
REGISTER_KINDS = ("decision", "scope", "dependency")

MAX_CONTENT_CHARS = 4000
MAX_SOURCE_CHARS = 40


class JournalError(ValueError):
    """Ligne de journal refusee."""


def _entry_dict(row):
    """Convertit une ligne de note en dictionnaire serialisable."""
    data = dict(row)
    data.setdefault("task_title", None)
    data.setdefault("project_name", None)
    return data


def add_entry(content, task_id=None, project_id=None, kind="note", minutes=None, source=None):
    """Ajoute une ligne au journal d'une tache ou d'un projet.

    :param content: texte de la ligne, obligatoire
    :param task_id: tache concernee
    :param project_id: projet concerne ; deduit de la tache s'il est absent
    :param kind: 'note', 'progress', 'work' ou un type du registre
    :param minutes: duree du travail (kind 'work')
    :param source: origine ('claude'), None pour une saisie a la main
    :return: ligne creee
    :raises JournalError: contenu vide, type inconnu ou aucune cible
    """
    texte = (content or "").strip()[:MAX_CONTENT_CHARS]
    if not texte:
        raise JournalError("Le contenu de la note est obligatoire.")
    if kind not in VALID_KINDS:
        raise JournalError("Type de note inconnu : " + ", ".join(VALID_KINDS) + ".")
    if task_id is None and project_id is None:
        raise JournalError("Une note porte sur une tache ou sur un projet.")
    secret = secrets_guard.find_secret(texte)
    if secret:
        raise JournalError(
            f"La note semble contenir un secret en clair ({secret}) : remplace-le par *** et note"
            " plutot ou il est range."
        )

    db = get_db()
    if task_id is not None and project_id is None:
        row = db.execute("SELECT project_id FROM task WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise JournalError(f"Tache {task_id} introuvable.")
        project_id = row["project_id"]

    # Une ligne de journal suit sa tache (ou son projet) : jamais d'espace propre.
    espace = space_model.for_record(task_id=task_id) if task_id is not None \
        else space_model.for_record(project_id=project_id)
    curseur = db.execute(
        "INSERT INTO note (task_id, project_id, content, kind, minutes, source, space)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            project_id,
            texte,
            kind,
            int(minutes) if minutes else None,
            (source or "").strip()[:MAX_SOURCE_CHARS] or None,
            espace,
        ),
    )
    db.commit()
    return get_entry(curseur.lastrowid)


def get_entry(entry_id):
    """Retourne une ligne de journal, ou None."""
    row = get_db().execute(
        "SELECT n.*, t.title AS task_title, p.name AS project_name"
        " FROM note n LEFT JOIN task t ON t.id = n.task_id"
        " LEFT JOIN project p ON p.id = n.project_id"
        " WHERE n.id = ?",
        (entry_id,),
    ).fetchone()
    return _entry_dict(row) if row else None


def list_for_task(task_id, limit=50):
    """Journal d'une tache, le plus recent d'abord."""
    rows = get_db().execute(
        "SELECT n.*, NULL AS task_title, NULL AS project_name FROM note n"
        " WHERE n.task_id = ? ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return [_entry_dict(row) for row in rows]


def list_for_project(project_id, limit=80):
    """Journal d'un projet : ses commentaires et ceux de ses taches."""
    rows = get_db().execute(
        "SELECT n.*, t.title AS task_title, NULL AS project_name"
        " FROM note n LEFT JOIN task t ON t.id = n.task_id"
        " WHERE n.project_id = ? ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [_entry_dict(row) for row in rows]


def list_register(project_id, limit=60):
    """Registre d'un projet (decisions, perimetre, dependances), le plus recent d'abord.

    Chaque entree porte le statut de sa tache de suivi eventuelle.
    """
    marques = ", ".join("?" for _ in REGISTER_KINDS)
    rows = get_db().execute(
        "SELECT n.*, t.title AS task_title, t.status AS task_status, NULL AS project_name"
        " FROM note n LEFT JOIN task t ON t.id = n.task_id"
        f" WHERE n.project_id = ? AND n.kind IN ({marques})"
        " ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
        (project_id, *REGISTER_KINDS, limit),
    ).fetchall()
    return [_entry_dict(row) for row in rows]


def list_between(start_date, end_date, source=None, limit=60, space=None):
    """Lignes d'une periode (dates ISO incluses), filtrees par source au besoin.

    Le projet d'une ligne sans projet est celui, actuel, de sa tache ou de la
    tache parente : une tache rattachee apres coup colore aussi son journal.
    """
    clause = "AND n.source = ?" if source else ""
    filtre, valeurs = space_model.clause("n.space", space)
    clause += filtre
    parametres = [start_date, end_date] + ([source] if source else []) + valeurs + [limit]
    rows = get_db().execute(
        "SELECT n.*, t.title AS task_title, p.name AS project_name, p.color AS project_color"
        " FROM note n LEFT JOIN task t ON t.id = n.task_id"
        " LEFT JOIN task parent ON parent.id = t.parent_id"
        " LEFT JOIN project p ON p.id = COALESCE(n.project_id, t.project_id, parent.project_id)"
        f" WHERE date(n.created_at) BETWEEN ? AND ? {clause}"
        " ORDER BY n.created_at ASC, n.id ASC LIMIT ?",
        parametres,
    ).fetchall()
    return [_entry_dict(row) for row in rows]


def delete_entry(entry_id):
    """Supprime une ligne de journal. Retourne False si elle n'existait pas."""
    db = get_db()
    curseur = db.execute("DELETE FROM note WHERE id = ?", (entry_id,))
    db.commit()
    return curseur.rowcount > 0
