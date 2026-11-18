"""Registre d'un projet : decisions, changements de perimetre, dependances.

Une entree du registre est une ligne du journal du projet, typee. Une decision
actee n'appelle rien de plus. Un changement de perimetre ou une dependance
doit etre arbitre ou leve : l'entree peut alors ouvrir une tache de suivi,
dont le statut dit si le point est regle. Pas de statut saisi a la main qui
deviendrait faux faute d'etre tenu a jour.
"""
from models import journal
from models import task as task_model
from models.journal import JournalError

# Types d'entree qui peuvent ouvrir une tache de suivi, et le verbe du titre.
FOLLOW_UP_TITLES = {
    "scope": "Arbitrer : ",
    "dependency": "Lever la dépendance : ",
}

# Longueur maximale du titre d'une tache de suivi.
FOLLOW_UP_TITLE_CHARS = 120


def _follow_up_title(kind, content):
    """Titre de la tache de suivi : verbe + premiere ligne de l'entree, tronquee."""
    ligne = content.strip().splitlines()[0].strip()
    titre = FOLLOW_UP_TITLES[kind] + ligne
    if len(titre) > FOLLOW_UP_TITLE_CHARS:
        titre = titre[: FOLLOW_UP_TITLE_CHARS - 1].rstrip() + "…"
    return titre


def add_record(project_id, kind, content, follow_up=False, source=None):
    """Ajoute une entree au registre du projet.

    :param kind: 'decision', 'scope' ou 'dependency'
    :param follow_up: ouvre une tache de suivi (perimetre et dependance seulement)
    :param source: origine ('claude'), None pour une saisie a la main
    :return: entree creee, avec la tache de suivi eventuelle sous 'task'
    :raises JournalError: type hors registre ou contenu vide
    """
    if kind not in journal.REGISTER_KINDS:
        raise JournalError("Type de registre inconnu : decision, scope ou dependency.")
    if not (content or "").strip():
        raise JournalError("Le contenu de l'entree est obligatoire.")

    tache = None
    if follow_up and kind in FOLLOW_UP_TITLES:
        tache = task_model.create_task(
            {"title": _follow_up_title(kind, content), "project_id": project_id},
            source=source,
        )
    entree = journal.add_entry(
        content,
        task_id=tache["id"] if tache else None,
        project_id=project_id,
        kind=kind,
        source=source,
    )
    entree["task"] = tache
    return entree


def record_status(entry):
    """Statut d'une entree du registre, deduit et non saisi.

    :return: 'acted' (decision), 'open' ou 'settled' (tache de suivi ouverte
        ou close), 'noted' (sans suivi)
    """
    if entry["kind"] == "decision":
        return "acted"
    if entry.get("task_id"):
        return "settled" if entry.get("task_status") == "done" else "open"
    return "noted"


def register_view(entries):
    """Entrees du registre pretes a afficher, statut deduit compris."""
    return [
        {
            "id": entry["id"],
            "kind": entry["kind"],
            "content": entry["content"],
            "created_at": entry["created_at"],
            "source": entry.get("source"),
            "task_id": entry.get("task_id"),
            "task_title": entry.get("task_title"),
            "status": record_status(entry),
        }
        for entry in entries
    ]
