"""Sources cliquables des reponses du chat.

Pendant un tour, le chat lit la base par ses outils. Les elements lus que la
reponse cite (par leur titre, leur nom ou « #id ») deviennent des liens sous
la reponse : on ouvre la tache, le projet ou la fiche dont NOMAIA parle.

Les sources sont tirees des resultats d'outils, jamais du texte du modele :
un lien pointe toujours vers un element qui existe. Le filtre « cite dans la
reponse » ecarte le reste de ce qui a ete lu (une liste de 30 taches pour
n'en retenir que deux).
"""
import re
import unicodedata

from models import task as task_model

MAX_SOURCES = 8

# Longueur retenue d'un titre pour le reconnaitre dans la reponse : le modele
# abrege souvent les titres longs, et certains outils les tronquent deja.
MATCH_PREFIX = 40
MIN_MATCH = 5

KIND_LABELS = {"task": "Tâche", "project": "Projet", "person": "Contact", "event": "Agenda", "conversation": "Conversation"}

# Cle de la liste qui contient l'element -> nature des objets {id, name}.
NAMED_KINDS = {"projects": "project", "people": "person", "person": "person"}


def _fold(text):
    """Minuscules sans accents, espaces reduits : base de la comparaison."""
    decompose = unicodedata.normalize("NFKD", str(text or ""))
    sans_accents = "".join(car for car in decompose if not unicodedata.combining(car)).casefold()
    return re.sub(r"\s+", " ", sans_accents).strip()


def _url(kind, item_id, day=None):
    if kind == "task":
        return f"/plan?tache={item_id}"
    if kind == "project":
        return f"/projets/{item_id}"
    if kind == "person":
        return f"/contacts?personne={item_id}"
    if kind == "conversation":
        return f"/chat?c={item_id}"
    return f"/agenda?date={day}" if day else "/agenda"


def _source(kind, item_id, label, day=None):
    label = str(label or "").strip().rstrip("…").strip()
    return {"kind": kind, "kind_label": KIND_LABELS[kind], "id": item_id, "label": label, "url": _url(kind, item_id, day)}


def _walk(value, parent=None):
    """Elements reconnaissables d'un resultat d'outil, a toute profondeur."""
    if isinstance(value, list):
        for item in value:
            yield from _walk(item, parent)
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("conversation_id"), int) and value.get("title"):
        yield _source("conversation", value["conversation_id"], value["title"])
    elif isinstance(value.get("id"), int) and value.get("title"):
        if value.get("starts_at"):
            yield _source("event", value["id"], value["title"], str(value["starts_at"])[:10])
        else:
            yield _source("task", value["id"], value["title"])
    elif isinstance(value.get("id"), int) and value.get("name") and parent in NAMED_KINDS:
        yield _source(NAMED_KINDS[parent], value["id"], value["name"])
    for key, child in value.items():
        if isinstance(child, (dict, list)):
            yield from _walk(child, key)


def _cited(source, reply):
    """La reponse cite-t-elle l'element : « #id » pour une tache, sinon son titre."""
    if source["kind"] == "task" and re.search(rf"#{source['id']}\b", reply):
        return True
    label = _fold(source["label"])[:MATCH_PREFIX].strip()
    return len(label) >= MIN_MATCH and label in _fold(reply)


def _unique(sources):
    vus, uniques = set(), []
    for source in sources:
        cle = (source["kind"], source["id"])
        if cle not in vus:
            vus.add(cle)
            uniques.append(source)
    return uniques


def from_results(results, reply):
    """Sources citees par la reponse, parmi les elements lus pendant le tour.

    :param results: resultats bruts des outils de lecture appeles
    :param reply: texte final de la reponse
    :return: liste de {kind, kind_label, id, label, url}, MAX_SOURCES au plus
    """
    if not reply:
        return []
    candidats = _unique(source for result in results for source in _walk(result))
    return [source for source in candidats if _cited(source, reply)][:MAX_SOURCES]


def _task_source(task_id):
    """Source d'une tache qui existe encore, sinon None."""
    try:
        task = task_model.get_task(int(task_id))
    except (TypeError, ValueError):
        return None
    return _source("task", task["id"], task["title"]) if task else None


def from_steps(applied):
    """Elements touches par un plan applique : taches modifiees ou creees, evenements poses.

    Une tache supprimee ou absorbee par une fusion n'a plus de lien : chaque
    tache est relue apres le plan.

    :param applied: liste de (etape, resultat de l'outil) pour les etapes reussies
    """
    sources = []
    for step, result in applied:
        args = step.get("args") or {}
        cible = args.get("keep_id") if step.get("tool") == "merge_tasks" else args.get("task_id")
        if cible is not None:
            sources.append(_task_source(cible))
        for source in _walk(result.get("data")):
            sources.append(_task_source(source["id"]) if source["kind"] == "task" else source)
    return _unique(source for source in sources if source)[:MAX_SOURCES]
