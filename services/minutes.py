"""Comptes rendus : conversions des elements en taches et en entrees de registre.

Une action devient une tache du projet (echeance et responsable repris), une
decision s'inscrit au registre du projet. Chaque conversion n'a lieu qu'une
fois : l'element garde le lien vers ce qu'il a produit.
"""
from models import minutes as minutes_model
from models import task as task_model
from models.minutes import MinutesError
from services import project_register

TASK_SOURCE = "compte-rendu"


def _context(minutes):
    """Rappel de l'origine, ajoute a la tache ou a l'entree de registre."""
    return f"compte rendu « {minutes['title']} » du {minutes['held_on']}"


def item_to_task(item_id):
    """Cree la tache d'une action (ou d'une demande du client) ; renvoie la tache.

    :raises MinutesError: element introuvable, deja converti, ou decision
    """
    item = minutes_model.get_item(item_id)
    if item is None:
        raise MinutesError("Élément introuvable.")
    if item["type"] == "decision":
        raise MinutesError("Une décision s'inscrit au registre, elle ne devient pas une tâche.")
    if item["task_id"]:
        raise MinutesError("Cet élément a déjà sa tâche.")
    minutes = minutes_model.get(item["minutes_id"])
    description = "Issue du " + _context(minutes) + (f". Responsable : {item['owner']}." if item["owner"] else ".")
    task = task_model.create_task(
        {
            "title": item["text"][:200],
            "description": description,
            "project_id": minutes["project_id"],
            "deadline": item["due"],
            "side": "client" if item["type"] == "request" else None,
            # Sans projet, la tache reste dans l'espace du compte rendu.
            **({} if minutes["project_id"] else {"space": minutes["space"]}),
        },
        source=TASK_SOURCE,
    )
    minutes_model.link_item(item_id, task_id=task["id"])
    return task


def item_to_register(item_id):
    """Inscrit une decision au registre du projet du compte rendu ; renvoie l'entree.

    :raises MinutesError: element introuvable, deja inscrit, pas une decision, ou compte rendu sans projet
    """
    item = minutes_model.get_item(item_id)
    if item is None:
        raise MinutesError("Élément introuvable.")
    if item["type"] != "decision":
        raise MinutesError("Seule une décision s'inscrit au registre.")
    if item["note_id"]:
        raise MinutesError("Cette décision est déjà au registre.")
    minutes = minutes_model.get(item["minutes_id"])
    if not minutes["project_id"]:
        raise MinutesError("Rattachez d'abord le compte rendu à un projet.")
    entry = project_register.add_record(
        minutes["project_id"], "decision", f"{item['text']} ({_context(minutes)})", source=TASK_SOURCE
    )
    minutes_model.link_item(item_id, note_id=entry["id"])
    return entry


def finalize(minutes_id):
    """Valide le compte rendu ; renvoie ce qui reste a convertir (actions sans tache, decisions hors registre)."""
    minutes = minutes_model.update(minutes_id, {"status": "final"})
    if minutes is None:
        return None
    pending_actions = [i for i in minutes["items"] if i["type"] == "action" and not i["task_id"]]
    pending_decisions = [i for i in minutes["items"] if i["type"] == "decision" and not i["note_id"]]
    return {"minutes": minutes, "pending_actions": pending_actions, "pending_decisions": pending_decisions}
