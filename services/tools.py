"""Registre des outils appelables par un modele de langage.

Chaque outil expose une action simple sur la base NOMAIA : lire des taches,
consulter le plan du jour, creer ou decaler une tache, piloter le chronometre,
lire l'agenda et y poser un evenement, suivre les interlocuteurs et ce
qu'ils me doivent. Seul cancel_event retire quelque chose, et uniquement sur
demande explicite d'un evenement designe par identifiant.

Une distinction structure les outils d'interlocuteurs : ce que **je dois** a
quelqu'un est une tache avec un destinataire, ce qu'**on me doit** est une
attente (waiting), qui ne se coche pas mais se relance.

Les sorties repartent dans le prompt du modele : elles sont volontairement
compactes (quelques champs par tache, titres tronques) pour ne pas gaspiller
de jetons a chaque tour de dialogue.
"""
import inspect
import logging
import re
from datetime import date, datetime, time, timedelta

from models import chat as chat_model
from models import day_off as day_off_model
from models import event as event_model
from models import get_db
from models import person as person_model
from models import project as project_model
from models import session as session_model
from models import stats as stats_model
from models import task as task_model
from services import foresight, planner, project_board, workdays
from services import review as review_service

logger = logging.getLogger("nomaia.tools")

# Longueur des titres renvoyes au modele, alignee sur le contexte de triage.
TITLE_MAX = 70

# Bornes de bon sens sur les listes renvoyees : au-dela, le modele paie des
# jetons pour des lignes qu'il ne lira pas.
LIMIT_MAX = 100
POSTPONE_MAX_DAYS = 365

# Horizon maximal d'une lecture d'agenda : un mois suffit a toutes les
# questions courantes et borne le nombre de lignes renvoyees au modele.
EVENT_DAYS_MAX = 31

# Duree maximale d'un evenement, alignee sur models.event.MAX_DURATION_MIN.
EVENT_DURATION_MAX = 24 * 60

# Valeurs de statut tolerees en entree, ramenees aux deux statuts reels.
STATUS_ALIASES = {
    "todo": "todo",
    "open": "todo",
    "ouvert": "todo",
    "ouverte": "todo",
    "pending": "todo",
    "encours": "todo",
    "done": "done",
    "closed": "done",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "termine": "done",
    "terminee": "done",
}

# Statuts d'attente toleres en entree, ramenes aux trois statuts reels.
WAITING_STATUS_ALIASES = {
    "open": "open",
    "ouvert": "open",
    "ouverte": "open",
    "attente": "open",
    "en attente": "open",
    "pending": "open",
    "todo": "open",
    "received": "received",
    "recu": "received",
    "recue": "received",
    "reception": "received",
    "arrive": "received",
    "ok": "received",
    "done": "received",
    "closed": "received",
    "termine": "received",
    "terminee": "received",
    "dropped": "dropped",
    "drop": "dropped",
    "abandon": "dropped",
    "abandonne": "dropped",
    "abandonnee": "dropped",
    "annule": "dropped",
    "annulee": "dropped",
    "cancelled": "dropped",
    "canceled": "dropped",
    "perdu": "dropped",
}

# Statuts acceptes pour clore une attente : une attente ne se rouvre pas ici.
WAITING_RESOLVED = ("received", "dropped")

# Nombre de lignes detaillees dans la fiche d'un interlocuteur.
PERSON_DETAIL_MAX = 20

# Valeurs demandant explicitement l'absence de projet.
NO_PROJECT_WORDS = ("none", "null", "aucun", "sans projet", "sans")

# Types d'evenement toleres en entree, ramenes aux quatre types reels.
KIND_ALIASES = {
    "meeting": "meeting",
    "reunion": "meeting",
    "rdv": "meeting",
    "rendez-vous": "meeting",
    "rendez_vous": "meeting",
    "call": "meeting",
    "appel": "meeting",
    "focus": "focus",
    "concentration": "focus",
    "bloc": "focus",
    "deep work": "focus",
    "absence": "absence",
    "conge": "absence",
    "off": "absence",
    "indisponible": "absence",
    "personal": "personal",
    "perso": "personal",
    "personnel": "personal",
    "prive": "personal",
    "sport": "personal",
    "famille": "personal",
}

# Types de jour de repos toleres en entree.
DAY_OFF_KIND_ALIASES = {
    "leave": "leave",
    "conge": "leave",
    "conges": "leave",
    "vacances": "leave",
    "repos": "leave",
    "holiday": "holiday",
    "ferie": "holiday",
}

# Rythmes de repetition toleres en entree.
REPEAT_ALIASES = {
    "none": "none",
    "aucune": "none",
    "aucun": "none",
    "jamais": "none",
    "once": "none",
    "unique": "none",
    "daily": "daily",
    "quotidien": "daily",
    "quotidienne": "daily",
    "chaque jour": "daily",
    "tous les jours": "daily",
    "weekly": "weekly",
    "hebdo": "weekly",
    "hebdomadaire": "weekly",
    "chaque semaine": "weekly",
}

# Valeurs de 'day' reclamant la serie entiere plutot qu'une seule occurrence.
SERIES_WORDS = ("all", "tout", "toute", "toutes", "serie", "series", "*")

# Heure libre : 'HH:MM', 'HH:MM:SS', 'HHhMM' ou 'HHh'.
TIME_PATTERN = re.compile(r"^(\d{1,2})\s*[:hH]\s*(\d{2})?(?::\d{2})?$")
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Alias d'arguments toleres : un modele ecrit regulierement 'project_name' ou
# 'q' la ou l'outil attend 'project' ou 'query'. Chaque cle pointe vers une
# liste ordonnee de cibles possibles ; la premiere que la fonction accepte
# reellement l'emporte, ce qui permet a 'name' de viser 'title' sur
# create_task et 'project' sur les outils qui n'ont pas de titre.
ARG_ALIASES = {
    "project_name": ["project"],
    "projectname": ["project"],
    "project_title": ["project"],
    "projet": ["project"],
    "name": ["title", "project", "person"],
    "nom": ["title", "name", "project", "person"],
    "task": ["task_id"],
    "taskid": ["task_id"],
    "id": ["task_id", "event_id", "waiting_id"],
    "task_identifier": ["task_id"],
    "waiting": ["waiting_id"],
    "waitingid": ["waiting_id"],
    "wait_id": ["waiting_id"],
    "attente": ["waiting_id"],
    "event": ["event_id"],
    "eventid": ["event_id"],
    "evenement": ["event_id"],
    "titre": ["title"],
    "who": ["person", "name"],
    "qui": ["person", "name"],
    "personne": ["person", "name"],
    "contact": ["person", "name"],
    "person_name": ["person", "name"],
    "interlocuteur": ["person", "name"],
    "person": ["person", "name"],
    "sujet": ["subject"],
    "objet": ["subject"],
    "what": ["subject"],
    "quoi": ["subject"],
    "q": ["query"],
    "search": ["query"],
    "text": ["query"],
    "keyword": ["query"],
    "term": ["query"],
    "recherche": ["query"],
    "state": ["status"],
    "statut": ["status"],
    "max": ["limit"],
    "max_results": ["limit"],
    "count": ["limit"],
    "top": ["limit"],
    "n": ["limit"],
    "limite": ["limit"],
    "day": ["date", "deadline"],
    "date": ["date", "day", "deadline"],
    "jour": ["date", "day", "deadline"],
    "target_date": ["date", "day", "deadline"],
    "due": ["deadline"],
    "due_date": ["deadline", "due"],
    "echeance": ["deadline", "due"],
    "date_limite": ["deadline", "due"],
    "pour": ["due"],
    "week": ["weeks_back"],
    "weeks": ["weeks_back"],
    "weeks_ago": ["weeks_back"],
    "week_offset": ["weeks_back"],
    "semaine": ["weeks_back"],
    "estimate": ["estimate_min"],
    "duration": ["estimate_min", "duration_min", "min_minutes"],
    "duration_min": ["estimate_min", "min_minutes"],
    "minutes": ["estimate_min", "duration_min", "min_minutes"],
    "duree": ["estimate_min", "duration_min", "min_minutes"],
    "min_duration": ["min_minutes"],
    "duree_min": ["min_minutes"],
    "notes": ["description", "notes"],
    "note": ["description", "notes"],
    "description": ["description", "notes"],
    "desc": ["description", "notes"],
    "detail": ["description", "notes"],
    "details": ["description", "notes"],
    "priorite": ["priority"],
    "delay": ["days"],
    "days_count": ["days"],
    "nb_days": ["days"],
    "postpone_days": ["days"],
    "jours": ["days"],
    "energie": ["energy"],
    "start": ["starts_at"],
    "start_at": ["starts_at"],
    "startsat": ["starts_at"],
    "begin": ["starts_at"],
    "debut": ["starts_at"],
    "datetime": ["starts_at"],
    "when": ["starts_at"],
    "end": ["ends_at"],
    "end_at": ["ends_at"],
    "endsat": ["ends_at"],
    "fin": ["ends_at"],
    "heure": ["start_time"],
    "time": ["start_time"],
    "hour": ["start_time"],
    "lieu": ["location"],
    "place": ["location"],
    "salle": ["location"],
    "type": ["kind"],
    "event_type": ["kind"],
    "categorie": ["kind"],
    "recurrence": ["repeat"],
    "repetition": ["repeat"],
    "frequence": ["repeat"],
    "frequency": ["repeat"],
    "keep": ["keep_id"],
    "garder": ["keep_id"],
    "principale": ["keep_id"],
    "merge": ["merge_ids"],
    "merge_id": ["merge_ids"],
    "doublons": ["merge_ids"],
    "others": ["merge_ids"],
    "seuil": ["threshold"],
    "nouveau_nom": ["new_name"],
    "rename": ["new_name"],
    "until": ["repeat_until"],
    "repeat_end": ["repeat_until"],
    "jusqu_a": ["repeat_until"],
}


class ToolError(ValueError):
    """Erreur metier d'un outil : convertie en {'ok': False, 'error': ...}."""


# --- Helpers de normalisation -------------------------------------------------


def _truncate(text, length=TITLE_MAX):
    """Tronque un texte a la longueur voulue, en signalant la coupe."""
    text = (text or "").strip()
    return text if len(text) <= length else text[: length - 1] + "…"


def _as_int(value, label, minimum=None, maximum=None, default=None):
    """Convertit une valeur en entier borne, ou leve une ToolError lisible."""
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{label} doit etre un nombre entier.")
    if minimum is not None and number < minimum:
        number = minimum
    if maximum is not None and number > maximum:
        number = maximum
    return number


def _as_ratio(value, label, default):
    """Convertit une valeur en proportion entre 0 et 1, ou leve une ToolError.

    Un modele ecrit aussi bien 0.6 que '0,6' ou 60 (pour cent) : les trois
    formes donnent la meme proportion.
    """
    if value in (None, ""):
        return default
    texte = str(value).strip().replace(",", ".").rstrip("%").strip()
    try:
        nombre = float(texte)
    except (TypeError, ValueError):
        raise ToolError(f"{label} doit etre un nombre entre 0 et 1.")
    if nombre > 1:
        # 60 se lit 60 %, pas un seuil de 6000 % qui ne rapprocherait rien.
        nombre = nombre / 100
    if not 0 < nombre <= 1:
        raise ToolError(f"{label} doit etre un nombre entre 0 et 1.")
    return nombre


def _as_id_list(value, label):
    """Convertit une liste d'identifiants libre en entiers uniques et ordonnes.

    Un modele ecrit aussi bien [3, 7] que 3 ou '3,7' : les trois formes
    donnent la meme liste.

    :return: liste d'entiers, vide si rien d'exploitable n'est fourni
    """
    if value in (None, ""):
        return []

    if isinstance(value, (list, tuple, set)):
        brut = list(value)
    elif isinstance(value, str):
        brut = re.split(r"[\s,;]+", value.strip())
    else:
        brut = [value]

    identifiants = []
    for element in brut:
        if element in (None, ""):
            continue
        numero = _as_int(element, label, minimum=1)
        if numero is not None and numero not in identifiants:
            identifiants.append(numero)
    return identifiants


def _as_date(value, label):
    """Valide une date ISO 'AAAA-MM-JJ'. Retourne un objet date, ou None."""
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ToolError(f"{label} doit etre une date au format AAAA-MM-JJ.")


def _as_time(value, label):
    """Valide une heure libre ('14:00', '14h', '14:00:00') en objet time."""
    if value in (None, ""):
        return None
    correspondance = TIME_PATTERN.match(str(value).strip())
    if correspondance is None:
        raise ToolError(f"{label} doit porter une heure au format HH:MM.")
    heures = int(correspondance.group(1))
    minutes = int(correspondance.group(2) or 0)
    if heures > 23 or minutes > 59:
        raise ToolError(f"{label} doit porter une heure valide au format HH:MM.")
    return time(hour=heures, minute=minutes)


def _as_stamp(value, label, time_value=None, default_day=None):
    """Valide un horodatage 'AAAA-MM-JJ HH:MM' et retourne un datetime.

    Un modele ecrit mal les dates : on tolere le separateur 'T', une date
    seule completee par une heure fournie a part, et une heure seule
    completee par un jour de reference.

    :param time_value: heure a utiliser si la valeur ne porte qu'une date
    :param default_day: jour a utiliser si la valeur ne porte qu'une heure
    :return: objet datetime
    :raises ToolError: format inexploitable
    """
    texte = str(value or "").strip().replace("T", " ")
    if not texte:
        raise ToolError(f"{label} est obligatoire, au format AAAA-MM-JJ HH:MM.")

    morceaux = texte.split()
    heure_texte = morceaux[1] if len(morceaux) > 1 else None

    if DAY_PATTERN.match(morceaux[0]):
        jour = _as_date(morceaux[0], label)
    elif default_day is not None and heure_texte is None:
        jour, heure_texte = default_day, morceaux[0]
    else:
        raise ToolError(f"{label} doit suivre le format AAAA-MM-JJ HH:MM.")

    heure = _as_time(heure_texte, label) if heure_texte else _as_time(time_value, label)
    if heure is None:
        raise ToolError(f"{label} doit porter une heure, au format AAAA-MM-JJ HH:MM.")

    return datetime.combine(jour, heure)


def _as_kind(value, default="meeting"):
    """Ramene un type d'evenement libre a 'meeting', 'focus', 'absence' ou 'personal'."""
    if value in (None, ""):
        value = default
    normalized = str(value).strip().lower()
    if normalized in KIND_ALIASES:
        return KIND_ALIASES[normalized]
    raise ToolError("Type invalide : attendu meeting, focus, absence ou personal.")


def _as_repeat(value, default="none"):
    """Ramene une repetition libre a 'none', 'daily' ou 'weekly'."""
    if value in (None, ""):
        value = default
    normalized = str(value).strip().lower()
    if normalized in REPEAT_ALIASES:
        return REPEAT_ALIASES[normalized]
    raise ToolError("Repetition invalide : attendu none, daily ou weekly.")


def _as_status(value, default="todo"):
    """Ramene un statut libre a 'todo', 'done' ou None (tous les statuts)."""
    if value in (None, ""):
        value = default
    normalized = str(value).strip().lower()
    if normalized in ("all", "tout", "tous", "toutes", "any", "*"):
        return None
    if normalized in STATUS_ALIASES:
        return STATUS_ALIASES[normalized]
    raise ToolError("Statut invalide : attendu todo, done ou all.")


def _as_waiting_status(value, default="open", allow_all=True):
    """Ramene un statut d'attente libre a 'open', 'received', 'dropped' ou None.

    :param allow_all: autorise 'all', qui vaut « tous les statuts » (None)
    :raises ToolError: valeur hors des trois statuts reels
    """
    if value in (None, ""):
        value = default
    normalized = str(value).strip().lower()
    if allow_all and normalized in ("all", "tout", "tous", "toutes", "any", "*"):
        return None
    if normalized in WAITING_STATUS_ALIASES:
        return WAITING_STATUS_ALIASES[normalized]
    raise ToolError("Statut d'attente invalide : attendu open, received ou dropped.")


def _resolve_person(name):
    """Resout un nom d'interlocuteur en fiche complete.

    :param name: nom cherche, casse ignoree
    :return: dictionnaire d'interlocuteur
    :raises ToolError: nom vide ou interlocuteur inconnu
    """
    texte = str(name or "").strip()
    if not texte:
        raise ToolError("Le nom de l'interlocuteur est obligatoire.")

    person = person_model.find_by_name(texte)
    if person is None:
        raise ToolError(
            f"Interlocuteur '{_truncate(texte, 40)}' introuvable."
            " Appeler list_people pour les noms connus."
        )
    return person


def _is_no_project(value):
    """Indique qu'une valeur de projet demande explicitement 'aucun projet'."""
    return str(value).strip().lower() in NO_PROJECT_WORDS


def _resolve_project(name):
    """Resout un nom de projet en identifiant.

    :param name: nom du projet, casse ignoree
    :return: tuple (project_id ou None, message d'avertissement ou None)
    """
    project = project_model.find_by_name(name)
    if project is None:
        return None, f"Projet '{_truncate(str(name), 40)}' introuvable."
    return project["id"], None


def _like_pattern(value):
    """Echappe les jokers SQL d'un texte destine a une clause LIKE."""
    for caractere in ("!", "%", "_"):
        value = value.replace(caractere, "!" + caractere)
    return "%" + value + "%"


# --- Helpers de serialisation compacte ---------------------------------------


def _task_brief(row):
    """Reduit une tache aux champs utiles au modele.

    :param row: ligne de tache issue de models.task
    :return: dictionnaire de sept champs, titre tronque
    """
    return {
        "id": row["id"],
        "title": _truncate(row["title"]),
        "priority": row["priority"],
        "deadline": row["deadline"],
        "estimate_min": row["estimate_min"],
        "project": row.get("project_name"),
        "status": row["status"],
    }


def _project_brief(row):
    """Reduit un projet a ses compteurs utiles au modele."""
    return {
        "id": row["id"],
        "name": _truncate(row["name"], 60),
        "status": row["status"],
        "deadline": row["deadline"],
        "open_count": row["open_count"],
        "done_count": row["done_count"],
        "open_estimate_min": row["open_estimate_min"],
        "tracked_min": row["tracked_min"],
    }


def _event_brief(row):
    """Reduit un evenement aux champs utiles au modele.

    Le lieu n'apparait que s'il existe, et 'is_occurrence' que s'il est vrai :
    deux cles muettes par ligne d'agenda coutent cher sur un mois affiche.

    :param row: dictionnaire d'evenement issu de models.event
    :return: dictionnaire compact, titre tronque
    """
    brief = {
        "id": row["id"],
        "title": _truncate(row["title"]),
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "kind": row["kind"],
        "project": row.get("project_name"),
    }
    if row.get("location"):
        brief["location"] = _truncate(row["location"], 40)
    if row.get("is_occurrence"):
        brief["is_occurrence"] = True
    if row.get("source_name"):
        # Importe d'Outlook : le modele doit savoir qu'il ne peut pas l'annuler.
        brief["source"] = row["source_name"]
        brief["read_only"] = True
    return brief


def _person_brief(row):
    """Reduit un interlocuteur a ses deux compteurs et son rattachement.

    :param row: ligne d'interlocuteur issue de models.person
    :return: dictionnaire compact ; owed_count = ce que je lui dois,
        waiting_count = ce qu'il me doit
    """
    brief = {
        "id": row["id"],
        "name": _truncate(row["name"], 60),
        "role": row.get("role"),
        "project": row.get("project_name"),
        "owed_count": row.get("owed_count", 0),
        "waiting_count": row.get("waiting_count", 0),
    }
    # Un interlocuteur peut intervenir sur plusieurs projets : le principal
    # ci-dessus, tous ici des qu'il y en a plus d'un.
    if len(row.get("projects") or []) > 1:
        brief["projects"] = [projet["name"] for projet in row["projects"]]
    # Les alias n'apparaissent que s'il y en a : ils expliquent pourquoi un
    # nom cite retrouve une fiche portant un autre nom.
    if row.get("aliases"):
        brief["aliases"] = row["aliases"]
    return brief


def _waiting_brief(row):
    """Reduit une attente aux champs utiles au modele.

    'due', 'overdue' et 'task_title' n'apparaissent que s'ils disent quelque
    chose : sur une liste de relances, trois cles muettes par ligne coutent.

    :param row: dictionnaire d'attente issu de models.person
    :return: dictionnaire compact, sujet tronque
    """
    brief = {
        "id": row["id"],
        "person": row.get("person_name"),
        "subject": _truncate(row["subject"]),
        "days_open": row.get("days_open", 0),
    }
    if row.get("due"):
        brief["due"] = row["due"]
    if row.get("overdue"):
        brief["overdue"] = True
    if row.get("task_title"):
        brief["task_title"] = _truncate(row["task_title"], 40)
    return brief


def _owed_brief(row):
    """Reduit une tache due a un interlocuteur : c'est mon travail a moi.

    models.person.owed_tasks ne remonte pas tous les champs d'une tache :
    cette ligne compacte ne peut pas passer par _task_brief.
    """
    brief = {
        "id": row["id"],
        "title": _truncate(row["title"]),
        "priority": row["priority"],
    }
    if row.get("deadline"):
        brief["deadline"] = row["deadline"]
    if row.get("project_name"):
        brief["project"] = row["project_name"]
    return brief


# --- Outils de lecture --------------------------------------------------------


def _list_tasks(status="todo", project=None, limit=20):
    """Liste les taches ouvertes ou terminees, eventuellement d'un seul projet.

    :param status: 'todo', 'done' ou 'all' (defaut 'todo')
    :param project: nom du projet, 'none' pour les taches sans projet
    :param limit: nombre maximal de taches renvoyees (defaut 20)
    :return: {'total': n, 'count': m, 'tasks': [tache compacte, ...]}
    """
    status = _as_status(status)
    limit = _as_int(limit, "La limite", minimum=1, maximum=LIMIT_MAX, default=20)

    project_id = None
    if project not in (None, ""):
        if _is_no_project(project):
            project_id = task_model.NO_PROJECT
        else:
            project_id, warning = _resolve_project(project)
            if warning:
                raise ToolError(warning)

    rows = task_model.list_tasks(status=status, project_id=project_id)
    return {
        "total": len(rows),
        "count": min(len(rows), limit),
        "tasks": [_task_brief(row) for row in rows[:limit]],
    }


def _search_tasks(query, limit=10):
    """Recherche une tache par mot-cle dans son titre et sa description.

    Les taches ouvertes remontent avant les taches terminees.

    :param query: texte recherche, au moins deux caracteres
    :param limit: nombre maximal de resultats (defaut 10)
    :return: {'query': texte, 'count': n, 'tasks': [tache compacte, ...]}
    """
    query = (query or "").strip()
    if len(query) < 2:
        raise ToolError("La recherche demande au moins deux caracteres.")

    limit = _as_int(limit, "La limite", minimum=1, maximum=LIMIT_MAX, default=10)
    motif = _like_pattern(query)

    rows = get_db().execute(
        "SELECT t.*, p.name AS project_name"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " WHERE t.title LIKE ? ESCAPE '!' OR t.description LIKE ? ESCAPE '!'"
        " ORDER BY CASE WHEN t.status = 'todo' THEN 0 ELSE 1 END,"
        "   CASE WHEN t.deadline IS NULL THEN 1 ELSE 0 END, t.deadline ASC,"
        "   t.priority ASC, t.id DESC"
        " LIMIT ?",
        (motif, motif, limit),
    ).fetchall()

    tasks = [_task_brief(dict(row)) for row in rows]
    return {"query": _truncate(query, 60), "count": len(tasks), "tasks": tasks}


def _find_duplicates(project=None, threshold=task_model.DUPLICATE_THRESHOLD):
    """Repere les taches ouvertes qui se recouvrent, sans rien fusionner.

    Le rapprochement porte sur les mots des titres. N'avoir aucun doublon est
    une reponse normale : le retour porte alors count=0, ce n'est pas un echec.

    :param project: nom du projet auquel restreindre la recherche
    :param threshold: proportion de mots communs exigee, 0 a 1 (defaut 0.6)
    :return: {'threshold', 'count', 'groups': [{'count', 'tasks': [...]}, ...]}
    """
    seuil = _as_ratio(threshold, "Le seuil", task_model.DUPLICATE_THRESHOLD)

    project_id = None
    if project not in (None, "") and not _is_no_project(project):
        project_id, warning = _resolve_project(project)
        if warning:
            raise ToolError(warning)

    groupes = task_model.find_duplicates(threshold=seuil, project_id=project_id)
    return {
        "threshold": seuil,
        "count": len(groupes),
        "groups": [
            {"count": len(groupe), "tasks": [_task_brief(tache) for tache in groupe]}
            for groupe in groupes[:LIMIT_MAX]
        ],
    }


def _get_plan(date=None):
    """Retourne le plan de la journee : creneaux places et reste a caser.

    :param date: jour a planifier au format AAAA-MM-JJ (defaut aujourd'hui)
    :return: {'date', 'load_pct', 'capacity_min', 'planned_min', 'slots',
        'overflow_count', 'overflow'}
    """
    day = _as_date(date, "La date")
    plan = planner.build_plan(day)

    slots = [
        {
            "start": slot["start"],
            "end": slot["end"],
            "duration_min": slot["duration_min"],
            "task": _task_brief(slot["task"]),
        }
        for slot in plan["slots"]
    ]
    overflow = plan["overflow"]

    return {
        "date": plan["date"],
        "capacity_min": plan["capacity_min"],
        "planned_min": plan["planned_min"],
        "load_pct": plan["load_pct"],
        "slots": slots,
        "overflow_count": len(overflow),
        "overflow": [_task_brief(row) for row in overflow[:5]],
    }


def _project_status(name=None):
    """Retourne les compteurs des projets, ou d'un seul projet nomme.

    :param name: nom du projet cherche (defaut : tous les projets)
    :return: {'count': n, 'projects': [projet compact, ...]}
    """
    rows = project_model.list_with_stats()

    if name not in (None, ""):
        project_id, warning = _resolve_project(name)
        if warning:
            raise ToolError(warning)
        rows = [row for row in rows if row["id"] == project_id]

    return {"count": len(rows), "projects": [_project_brief(row) for row in rows]}


def _get_stats(weeks_back=0):
    """Retourne les compteurs d'une semaine : cree, termine, temps suivi.

    :param weeks_back: 0 pour la semaine en cours, 1 pour la precedente
    :return: {'period_start', 'period_end', ...compteurs de la periode}
    """
    weeks_back = _as_int(weeks_back, "Le nombre de semaines", minimum=0, maximum=520, default=0)
    start_date, end_date = review_service.period_bounds(weeks_back=weeks_back)

    summary = stats_model.period_summary(start_date.isoformat(), end_date.isoformat())
    summary["period_start"] = start_date.isoformat()
    summary["period_end"] = end_date.isoformat()
    return summary


def _current_timer():
    """Retourne le chronometre en cours, s'il y en a un.

    :return: {'running': False} ou {'running': True, 'task_id', 'title',
        'started_at', 'elapsed_min'}
    """
    running = session_model.running()
    if running is None:
        return {"running": False}

    return {
        "running": True,
        "task_id": running["task_id"],
        "title": _truncate(running["task_title"]),
        "started_at": running["started_at"],
        "elapsed_min": running["elapsed_min"],
    }


def _list_events(date=None, days=1):
    """Liste les evenements d'agenda d'une periode, repetitions developpees.

    Ce sont des reunions, creneaux bloques et absences, pas des taches : ils
    ne se cochent pas et ne se chronometrent pas.

    :param date: premier jour au format AAAA-MM-JJ (defaut aujourd'hui)
    :param days: nombre de jours couverts, 1 a 31 (defaut 1)
    :return: {'start', 'end', 'total', 'count', 'events': [evenement compact]}
    """
    # 'date' masque ici la classe datetime.date : on passe par datetime.now().
    day = _as_date(date, "La date") or datetime.now().date()
    days = _as_int(days, "Le nombre de jours", minimum=1, maximum=EVENT_DAYS_MAX, default=1)
    end_day = day + timedelta(days=days - 1)

    rows = event_model.list_events(day, end_day)
    result = {
        "start": day.isoformat(),
        "end": end_day.isoformat(),
        "total": len(rows),
        "count": min(len(rows), LIMIT_MAX),
        "events": [_event_brief(row) for row in rows[:LIMIT_MAX]],
    }

    # Les jours non travailles ne sont cites que s'il y en a : une semaine
    # ordinaire n'a pas a payer une cle vide.
    repos = [
        {"date": jour["date"], "reason": jour["reason"]}
        for jour in workdays.calendar(day, end_day)
        if not jour["working"]
    ]
    if repos:
        result["days_off"] = repos
    return result


def _find_free_slots(date=None, min_minutes=30):
    """Retourne les trous libres d'une journee, agenda deduit.

    Repond a « quand suis-je libre jeudi ? » : ce sont les fenetres de travail
    restantes, sans rien planifier dedans.

    :param date: jour examine au format AAAA-MM-JJ (defaut aujourd'hui)
    :param min_minutes: duree en dessous de laquelle un trou est ignore
    :return: {'date', 'count', 'free_min', 'slots': [{start, end, ...}]}
    """
    day = _as_date(date, "La date") or datetime.now().date()
    min_minutes = _as_int(
        min_minutes, "La duree minimale", minimum=5, maximum=EVENT_DURATION_MAX, default=30
    )

    slots = planner.free_slots(day, min_minutes)
    return {
        "date": day.isoformat(),
        "count": len(slots),
        "free_min": sum(slot["duration_min"] for slot in slots),
        "slots": slots,
    }


def _day_off_brief(row):
    """Reduit un jour de repos aux champs utiles au modele."""
    return {
        "id": row["id"],
        "title": _truncate(row["title"]),
        "start_day": row["start_day"],
        "end_day": row["end_day"],
        "kind": row["kind"],
        "days": row["days"],
        # Conge en attente de validation : n'est pas un jour chome.
        "tentative": bool(row.get("tentative")),
    }


def _create_day_off(title, start_day, end_day=None, kind="leave"):
    """Pose un conge ou un ferie : une ou plusieurs journees entieres chomees.

    Un jour de repos n'a pas d'heure : ce jour-la, rien n'est planifie et
    NOMAIA n'envoie ni plan, ni bilan, ni relance.

    :param title: motif, obligatoire (« Conges », « Tabaski »)
    :param start_day: premier jour AAAA-MM-JJ
    :param end_day: dernier jour inclus AAAA-MM-JJ, defaut le premier
    :param kind: leave (conge) ou holiday (ferie), defaut leave
    :return: {'day_off': jour de repos compact}
    """
    debut = _as_date(start_day, "Le premier jour")
    if debut is None:
        raise ToolError("Le premier jour est obligatoire.")
    fin = _as_date(end_day, "Le dernier jour") or debut

    normalized = str(kind or "leave").strip().lower()
    if normalized not in DAY_OFF_KIND_ALIASES:
        raise ToolError("Type invalide : attendu leave ou holiday.")

    entree = day_off_model.create_day_off(
        {
            "title": title,
            "start_day": debut.isoformat(),
            "end_day": fin.isoformat(),
            "kind": DAY_OFF_KIND_ALIASES[normalized],
        }
    )
    return {"day_off": _day_off_brief(entree)}


def _list_days_off(date=None, days=31):
    """Liste les conges et feries qui touchent une periode.

    :param date: premier jour AAAA-MM-JJ (defaut aujourd'hui)
    :param days: nombre de jours couverts, 1 a 366 (defaut 31)
    :return: {'start', 'end', 'count', 'days_off': [jour de repos compact]}
    """
    day = _as_date(date, "La date") or datetime.now().date()
    days = _as_int(days, "Le nombre de jours", minimum=1, maximum=366, default=31)
    end_day = day + timedelta(days=days - 1)

    rows = day_off_model.list_between(day, end_day)
    return {
        "start": day.isoformat(),
        "end": end_day.isoformat(),
        "count": len(rows),
        "days_off": [_day_off_brief(row) for row in rows[:LIMIT_MAX]],
    }


def _delete_day_off(day_off_id):
    """Supprime un conge ou un ferie.

    :param day_off_id: identifiant, tel que donne par list_days_off
    :return: {'day_off_id', 'title', 'deleted': True}
    """
    day_off_id = _as_int(day_off_id, "L'identifiant du jour de repos", minimum=1)
    if day_off_id is None:
        raise ToolError("L'identifiant du jour de repos est obligatoire.")

    entree = day_off_model.get_day_off(day_off_id)
    if entree is None:
        raise ToolError(f"Jour de repos {day_off_id} introuvable.")

    day_off_model.delete_day_off(day_off_id)
    return {"day_off_id": day_off_id, "title": _truncate(entree["title"]), "deleted": True}


def _get_risks():
    """Echeances que le temps libre restant ne permet plus de tenir.

    Repond a « suis-je dans les temps ? » : travail restant (marge comprise,
    temps deja chronometre deduit) compare au temps libre reel d'ici chaque
    echeance, apres reunions, conges et week-ends.

    :return: {'count', 'risks': [{deadline, tasks, work_min, free_min, ...}]}
    """
    risques = foresight.deadlines_at_risk()
    for risque in risques:
        for tache in risque["tasks"]:
            tache["title"] = _truncate(tache["title"])
    return {"count": len(risques), "risks": risques}


# Messages relus au plus dans une autre conversation : au-dela, l'outil coute
# plus de jetons qu'il n'apporte.
READ_CONVERSATION_MAX = 30


def _search_conversations(query):
    """Retrouve les autres conversations qui parlent d'un sujet.

    :param query: mots a retrouver (tous presents dans un meme message)
    :return: {'conversations': [{conversation_id, title, updated_at, excerpts}]}
    """
    texte = str(query or "").strip()
    if not texte:
        raise ToolError("Preciser les mots a chercher (query).")
    trouvees = chat_model.search(texte)
    return {"count": len(trouvees), "conversations": trouvees}


def _read_conversation(conversation_id, limit=12):
    """Relit les derniers messages d'une autre conversation.

    :param conversation_id: conversation a relire (voir search_conversations)
    :param limit: nombre de messages, 30 au plus
    :return: {'conversation', 'messages'}
    """
    identifiant = _as_int(conversation_id, "L'identifiant de conversation", minimum=1)
    if identifiant is None:
        raise ToolError("conversation_id obligatoire : appeler search_conversations.")
    conversation = chat_model.get_conversation(identifiant)
    if conversation is None:
        raise ToolError(f"Conversation {identifiant} introuvable.")
    nombre = _as_int(limit, "limit", minimum=1, maximum=READ_CONVERSATION_MAX, default=12)
    messages = chat_model.history(limit=nombre, conversation_id=identifiant)
    return {
        "conversation": {"id": identifiant, "title": conversation["title"]},
        "messages": [
            {"role": item["role"], "at": item["created_at"], "content": _truncate(item["content"], 600)}
            for item in messages
        ],
    }


def _list_people():
    """Liste les interlocuteurs avec leurs deux compteurs.

    owed_count compte les taches ouvertes que je dois a la personne,
    waiting_count les attentes ouvertes qu'elle me doit.

    :return: {'total': n, 'count': m, 'people': [interlocuteur compact, ...]}
    """
    rows = person_model.list_people()
    return {
        "total": len(rows),
        "count": min(len(rows), LIMIT_MAX),
        "people": [_person_brief(row) for row in rows[:LIMIT_MAX]],
    }


def _person_status(name):
    """Retourne la fiche d'un interlocuteur et les deux dettes en cours.

    'i_owe' liste mes taches ouvertes pour cette personne : c'est mon travail.
    'they_owe' liste les attentes ouvertes : ce qu'elle me doit, a relancer.

    :param name: nom de l'interlocuteur, casse ignoree
    :return: {'person', 'i_owe_count', 'i_owe', 'they_owe_count', 'they_owe'}
    """
    person = _resolve_person(name)

    fiche = {
        "id": person["id"],
        "name": _truncate(person["name"], 60),
        "role": person.get("role"),
        "project": person.get("project_name"),
        "projects": [projet["name"] for projet in person.get("projects") or []],
    }
    if person.get("email"):
        fiche["email"] = _truncate(person["email"], 60)
    if person.get("phone"):
        fiche["phone"] = _truncate(person["phone"], 40)

    owed = person_model.owed_tasks(person["id"])
    waiting = person_model.list_waiting(status="open", person_id=person["id"])

    return {
        "person": fiche,
        "i_owe_count": len(owed),
        "i_owe": [_owed_brief(row) for row in owed[:PERSON_DETAIL_MAX]],
        "they_owe_count": len(waiting),
        "they_owe": [_waiting_brief(row) for row in waiting[:PERSON_DETAIL_MAX]],
    }


def _list_waiting(person=None, status="open"):
    """Liste ce que j'attends des autres, la plus ancienne d'abord.

    Ce ne sont pas mes taches : une attente se relance, elle ne se coche pas.
    C'est l'outil de la question « je relance qui ? ».

    :param person: nom d'un interlocuteur, defaut tous
    :param status: 'open', 'received', 'dropped' ou 'all' (defaut 'open')
    :return: {'total': n, 'count': m, 'waiting': [attente compacte, ...]}
    """
    statut = _as_waiting_status(status)

    person_id = None
    if person not in (None, ""):
        person_id = _resolve_person(person)["id"]

    rows = person_model.list_waiting(status=statut, person_id=person_id)
    return {
        "total": len(rows),
        "count": min(len(rows), LIMIT_MAX),
        "waiting": [_waiting_brief(row) for row in rows[:LIMIT_MAX]],
    }


# Statuts d'etape toleres en entree, ramenes aux quatre statuts reels.
PHASE_STATUS_ALIASES = {
    "todo": "todo",
    "a venir": "todo",
    "avenir": "todo",
    "doing": "doing",
    "en cours": "doing",
    "encours": "doing",
    "blocked": "blocked",
    "bloque": "blocked",
    "bloquee": "blocked",
    "done": "done",
    "termine": "done",
    "terminee": "done",
    "fini": "done",
    "finie": "done",
}


def _project_or_error(name):
    """Identifiant d'un projet nomme, ou ToolError lisible."""
    if name in (None, ""):
        raise ToolError("Le nom du projet est obligatoire.")
    project_id, warning = _resolve_project(name)
    if warning:
        raise ToolError(warning)
    return project_id


def _phase_brief(phase):
    """Etape compacte : numero, nom, statut, avancement, dates."""
    return {
        "sequence": phase["sequence"],
        "name": _truncate(phase["name"]),
        "status": phase["status"],
        "progress_pct": phase["progress_pct"],
        "planned_date": phase["planned_date"],
        "done_date": phase["done_date"],
    }


def _list_phases(project):
    """Etapes d'un projet et etape en cours.

    :param project: nom du projet
    :return: {'current': numero de l'etape en cours, 'phases': [...]}
    """
    pilotage = project_board.phase_pilot(_project_or_error(project))
    courante = pilotage["current_phase"]
    return {
        "current": next(
            (vue["sequence"] for vue in pilotage["phases"] if courante and vue["id"] == courante["id"]),
            None,
        ),
        "phases": [_phase_brief(vue) for vue in pilotage["phases"]],
    }


# --- Outils d'ecriture --------------------------------------------------------


def _update_phase(project, phase, status=None, planned_date=None, done_date=None, progress_pct=None):
    """Fait avancer une etape : statut, date prevue, date de fin, avancement.

    Terminer une etape termine aussi les etapes precedentes encore ouvertes.

    :param project: nom du projet
    :param phase: numero ou nom de l'etape
    :return: {'phase': etape compacte, 'cascaded': etapes closes au passage}
    """
    project_id = _project_or_error(project)
    data = {}
    if status not in (None, ""):
        cle = str(status).strip().lower().replace("é", "e").replace("à", "a").replace("_", " ")
        if cle not in PHASE_STATUS_ALIASES:
            raise ToolError("Statut d'etape attendu : todo, doing, blocked ou done.")
        data["status"] = PHASE_STATUS_ALIASES[cle]
    if planned_date is not None:
        data["planned_date"] = None if _is_no_project(planned_date) else planned_date
    if done_date not in (None, ""):
        data["done_date"] = done_date
    if progress_pct not in (None, ""):
        data["progress_pct"] = progress_pct
    etape = project_board.change_phase(project_id, phase, data, source="chat")
    return {"phase": _phase_brief(etape), "cascaded": etape.get("cascaded", 0)}


def _create_task(
    title,
    description=None,
    priority=3,
    estimate_min=30,
    deadline=None,
    energy="med",
    project=None,
    person=None,
):
    """Cree une tache.

    Un projet introuvable n'echoue pas : la tache est creee sans projet et le
    retour porte une note explicite.

    :param title: intitule de la tache, obligatoire
    :param description: precisions facultatives
    :param priority: 1 (urgent) a 4 (confort), defaut 3
    :param estimate_min: duree estimee en minutes, defaut 30
    :param deadline: echeance AAAA-MM-JJ, facultative
    :param energy: 'low', 'med' ou 'high', defaut 'med'
    :param project: nom du projet de rattachement, facultatif
    :param person: nom de la personne a qui la tache est due, facultatif
    :return: {'task': tache compacte, 'note': avertissement eventuel}
    """
    data = {
        "title": title,
        "priority": priority,
        "estimate_min": estimate_min,
        "deadline": deadline,
        "energy": energy,
    }
    if description not in (None, ""):
        data["description"] = description

    note = None
    if project not in (None, "") and not _is_no_project(project):
        project_id, warning = _resolve_project(project)
        if project_id:
            data["project_id"] = project_id
        else:
            note = warning + " Tache creee sans projet."

    notes = [note] if note else []

    if person not in (None, ""):
        # get_or_create plutot qu'une resolution stricte : un destinataire
        # inconnu est le cas normal la premiere fois qu'on le cite.
        personne = person_model.get_or_create(str(person).strip())
        data["person_id"] = personne["id"]
        notes.append(f"Due a {personne['name']}.")

    task = task_model.create_task(data)
    result = {"task": _task_brief(task)}
    if notes:
        result["note"] = " ".join(notes)
    return result


def _update_task(
    task_id,
    title=None,
    description=None,
    priority=None,
    estimate_min=None,
    deadline=None,
    energy=None,
    project=None,
    person=None,
):
    """Modifie les champs fournis d'une tache existante.

    Seuls les champs transmis sont touches. 'deadline' ou 'project' a 'none'
    efface la valeur. Le statut ne se change pas ici : voir complete_task.

    :param task_id: identifiant de la tache
    :return: {'task': tache compacte, 'note': avertissement eventuel}
    """
    task_id = _as_int(task_id, "L'identifiant de tache", minimum=1)
    if task_id is None:
        raise ToolError("L'identifiant de tache est obligatoire.")

    data = {}
    if title not in (None, ""):
        data["title"] = title
    if description is not None:
        data["description"] = description
    if priority not in (None, ""):
        data["priority"] = priority
    if estimate_min not in (None, ""):
        data["estimate_min"] = estimate_min
    if energy not in (None, ""):
        data["energy"] = energy
    if deadline is not None:
        data["deadline"] = None if _is_no_project(deadline) else deadline

    note = None
    if project is not None:
        if _is_no_project(project):
            data["project_id"] = None
        else:
            project_id, warning = _resolve_project(project)
            if project_id:
                data["project_id"] = project_id
            else:
                note = warning + " Projet inchange."

    if person is not None:
        if _is_no_project(person):
            data["person_id"] = None
        else:
            personne = person_model.get_or_create(str(person).strip())
            data["person_id"] = personne["id"]

    if not data:
        raise ToolError("Aucun champ a modifier.")

    task = task_model.update_task(task_id, data)
    if task is None:
        raise ToolError(f"Tache {task_id} introuvable.")

    result = {"task": _task_brief(task)}
    if note:
        result["note"] = note
    return result


def _complete_task(task_id):
    """Marque une tache comme terminee.

    Refuse explicitement une tache deja terminee plutot que de la rouvrir.

    :param task_id: identifiant de la tache
    :return: {'task': tache compacte}
    """
    task_id = _as_int(task_id, "L'identifiant de tache", minimum=1)
    if task_id is None:
        raise ToolError("L'identifiant de tache est obligatoire.")

    task = task_model.get_task(task_id)
    if task is None:
        raise ToolError(f"Tache {task_id} introuvable.")
    if task["status"] == "done":
        raise ToolError(f"Tache {task_id} deja terminee.")

    return {"task": _task_brief(task_model.toggle_task(task_id))}


PIN_POSITIONS = ("now", "end", "none")


def _pin_task(task_id, position="now", day=None):
    """Place une tache dans le plan d'un jour, a la main.

    :param task_id: identifiant de la tache
    :param position: 'now' en tete du jour, 'end' a la suite des epinglees,
        'none' pour la rendre au classement automatique
    :param day: jour vise AAAA-MM-JJ (defaut : aujourd'hui)
    :return: {'task', 'position', 'day'}
    """
    task_id = _as_int(task_id, "L'identifiant de tache", minimum=1)
    if task_id is None:
        raise ToolError("L'identifiant de tache est obligatoire.")
    position = str(position or "now").strip().lower()
    if position not in PIN_POSITIONS:
        raise ToolError("position attendue : now, end ou none.")

    if position == "none":
        if not task_model.unpin_task(task_id):
            raise ToolError(f"Tache {task_id} introuvable.")
        return {"task": _task_brief(task_model.get_task(task_id)), "position": "none"}

    jour = (_as_date(day, "Le jour") or date.today()).isoformat()
    task = task_model.pin_task(task_id, jour, first=position == "now")
    if task is None:
        raise ToolError(f"Tache {task_id} introuvable.")
    return {"task": _task_brief(task), "position": position, "day": jour}


def _postpone_task(task_id, deadline=None, days=None):
    """Decale l'echeance d'une tache, a une date fixe ou d'un nombre de jours.

    Le decalage part de l'echeance actuelle, ou d'aujourd'hui si la tache n'en
    a pas encore.

    :param task_id: identifiant de la tache
    :param deadline: nouvelle echeance AAAA-MM-JJ
    :param days: nombre de jours de decalage, si deadline n'est pas fourni
    :return: {'task': tache compacte, 'previous_deadline': ancienne echeance}
    """
    task_id = _as_int(task_id, "L'identifiant de tache", minimum=1)
    if task_id is None:
        raise ToolError("L'identifiant de tache est obligatoire.")

    task = task_model.get_task(task_id)
    if task is None:
        raise ToolError(f"Tache {task_id} introuvable.")

    if deadline not in (None, ""):
        target = _as_date(deadline, "L'echeance")
    elif days not in (None, ""):
        shift = _as_int(
            days, "Le nombre de jours", minimum=-POSTPONE_MAX_DAYS, maximum=POSTPONE_MAX_DAYS
        )
        if not shift:
            raise ToolError("Le nombre de jours doit etre different de zero.")
        base = _as_date(task["deadline"], "L'echeance") or date.today()
        target = base + timedelta(days=shift)
    else:
        raise ToolError("Fournir une echeance (deadline) ou un nombre de jours (days).")

    updated = task_model.update_task(task_id, {"deadline": target.isoformat()})
    return {"task": _task_brief(updated), "previous_deadline": task["deadline"]}


def _merge_tasks(keep_id, merge_ids):
    """Fusionne des doublons dans une seule tache, puis supprime les absorbees.

    Operation destructive, mais sans perte de travail : le temps chronometre,
    les notes et les attentes des taches absorbees suivent la tache conservee.
    Le retour dit combien de taches ont ete absorbees et combien de minutes
    chronometrees ont ete recuperees.

    :param keep_id: tache conservee
    :param merge_ids: taches absorbees : liste, entier seul ou chaine '3,7'
    :return: {'task', 'absorbed_count', 'absorbed_ids', 'tracked_min_recovered',
        'tracked_min'}
    """
    keep_id = _as_int(keep_id, "L'identifiant de la tache conservee", minimum=1)
    if keep_id is None:
        raise ToolError("L'identifiant de la tache conservee (keep_id) est obligatoire.")

    cibles = _as_id_list(merge_ids, "L'identifiant d'une tache absorbee")
    if not cibles:
        raise ToolError("Preciser au moins une tache a fusionner (merge_ids).")
    if keep_id in cibles:
        raise ToolError("La tache conservee ne peut pas figurer dans merge_ids.")

    if task_model.get_task(keep_id) is None:
        raise ToolError(f"Tache {keep_id} introuvable.")

    # Le temps des absorbees se lit avant la fusion : apres, il est reporte sur
    # la tache conservee et on ne saurait plus dire ce qui a ete recupere.
    recupere = 0
    for identifiant in cibles:
        absorbee = task_model.get_task(identifiant)
        if absorbee is None:
            raise ToolError(f"Tache {identifiant} introuvable.")
        recupere += absorbee.get("tracked_min") or 0

    try:
        gardee = task_model.merge_tasks(keep_id, cibles)
    except task_model.ValidationError as erreur:
        raise ToolError(str(erreur))

    return {
        "task": _task_brief(gardee),
        "absorbed_count": len(cibles),
        "absorbed_ids": cibles,
        "tracked_min_recovered": recupere,
        "tracked_min": gardee.get("tracked_min") or 0,
    }


def _delete_task(task_id):
    """Supprime definitivement une tache.

    Le temps chronometre sur la tache part avec elle : sur un doublon, c'est
    merge_tasks qu'il faut preferer, qui conserve ce temps.

    :param task_id: identifiant de la tache
    :return: {'task_id', 'title', 'deleted', 'tracked_min'}
    """
    task_id = _as_int(task_id, "L'identifiant de tache", minimum=1)
    if task_id is None:
        raise ToolError("L'identifiant de tache est obligatoire.")

    task = task_model.get_task(task_id)
    if task is None:
        raise ToolError(f"Tache {task_id} introuvable.")

    task_model.delete_task(task_id)
    return {
        "task_id": task_id,
        "title": _truncate(task["title"]),
        "deleted": True,
        "tracked_min": task.get("tracked_min") or 0,
    }


def _start_timer(task_id):
    """Demarre le chronometre sur une tache, en fermant la session precedente.

    :param task_id: identifiant de la tache a chronometrer
    :return: {'session_id', 'task_id', 'title', 'stopped_previous_min'}
    """
    task_id = _as_int(task_id, "L'identifiant de tache", minimum=1)
    if task_id is None:
        raise ToolError("L'identifiant de tache est obligatoire.")

    task = task_model.get_task(task_id)
    if task is None:
        raise ToolError(f"Tache {task_id} introuvable.")

    previous = session_model.stop()
    session_id = session_model.start(task_id)
    return {
        "session_id": session_id,
        "task_id": task_id,
        "title": _truncate(task["title"]),
        "stopped_previous_min": previous,
    }


def _stop_timer():
    """Arrete le chronometre en cours et retourne la duree enregistree.

    :return: {'task_id', 'title', 'duration_min'}
    """
    running = session_model.running()
    if running is None:
        raise ToolError("Aucun chronometre en cours.")

    duration = session_model.stop()
    return {
        "task_id": running["task_id"],
        "title": _truncate(running["task_title"]),
        "duration_min": duration,
    }


def _create_event(
    title,
    starts_at,
    ends_at=None,
    duration_min=None,
    kind="meeting",
    location=None,
    project=None,
    repeat="none",
    repeat_until=None,
    notes=None,
    start_time=None,
):
    """Cree un evenement d'agenda : reunion, creneau bloque ou absence.

    Un projet introuvable n'echoue pas : l'evenement est cree sans projet et
    le retour porte une note explicite, comme pour une tache.

    :param title: intitule, obligatoire
    :param starts_at: debut 'AAAA-MM-JJ HH:MM', ou date seule avec start_time
    :param ends_at: fin 'AAAA-MM-JJ HH:MM', ou heure seule le meme jour
    :param duration_min: duree en minutes, a defaut de ends_at
    :param kind: meeting, focus ou absence (defaut meeting)
    :param location: lieu, facultatif
    :param project: nom du projet de rattachement, facultatif
    :param repeat: none, daily ou weekly (defaut none)
    :param repeat_until: dernier jour de la serie AAAA-MM-JJ, facultatif
    :param notes: precisions facultatives
    :param start_time: heure HH:MM si starts_at ne porte qu'une date
    :return: {'event': evenement compact, 'note': avertissement eventuel}
    """
    debut = _as_stamp(starts_at, "La date de debut", time_value=start_time)

    if ends_at not in (None, ""):
        fin = _as_stamp(ends_at, "La date de fin", default_day=debut.date())
    elif duration_min not in (None, ""):
        minutes = _as_int(
            duration_min, "La duree", minimum=1, maximum=EVENT_DURATION_MAX, default=60
        )
        fin = debut + timedelta(minutes=minutes)
    else:
        raise ToolError("Fournir une fin (ends_at) ou une duree en minutes (duration_min).")

    data = {
        "title": title,
        "starts_at": debut.strftime("%Y-%m-%d %H:%M"),
        "ends_at": fin.strftime("%Y-%m-%d %H:%M"),
        "kind": _as_kind(kind),
        "repeat": _as_repeat(repeat),
    }
    if location not in (None, ""):
        data["location"] = location
    if notes not in (None, ""):
        data["notes"] = notes
    if repeat_until not in (None, ""):
        jusqu_a = _as_date(repeat_until, "La fin de repetition")
        data["repeat_until"] = jusqu_a.isoformat()

    note = None
    if project not in (None, "") and not _is_no_project(project):
        project_id, warning = _resolve_project(project)
        if project_id:
            data["project_id"] = project_id
        else:
            note = warning + " Evenement cree sans projet."

    event = event_model.create_event(data)
    result = {"event": _event_brief(event)}
    if note:
        result["note"] = note
    return result


def _cancel_event(event_id, day=None):
    """Annule un evenement : un seul jour d'une serie, ou la serie entiere.

    Un evenement repetitif sans jour precise est refuse : supprimer la serie
    en croyant annuler un mardi est une erreur qu'on ne rattrape pas. Le
    modele doit alors choisir, avec un jour ou avec day='all'.

    :param event_id: identifiant de l'evenement
    :param day: jour annule AAAA-MM-JJ, ou 'all' pour toute la serie
    :return: {'event_id', 'title', 'scope', ...}
    """
    event_id = _as_int(event_id, "L'identifiant d'evenement", minimum=1)
    if event_id is None:
        raise ToolError("L'identifiant d'evenement est obligatoire.")

    event = event_model.get_event(event_id)
    if event is None:
        raise ToolError(f"Evenement {event_id} introuvable.")

    titre = _truncate(event["title"])
    toute_la_serie = str(day).strip().lower() in SERIES_WORDS if day not in (None, "") else False
    jour = None if toute_la_serie else _as_date(day, "Le jour")

    if event["repeat"] == "none":
        # Sans repetition, annuler l'unique occurrence revient a supprimer.
        if jour is not None and jour.isoformat() != event["starts_at"][:10]:
            raise ToolError(
                f"Evenement {event_id} n'a pas lieu le {jour.isoformat()}"
                f" mais le {event['starts_at'][:10]}."
            )
        event_model.delete_event(event_id)
        return {"event_id": event_id, "title": titre, "scope": "event", "deleted": True}

    if jour is None and not toute_la_serie:
        raise ToolError(
            f"Evenement {event_id} se repete ({event['repeat']}) : preciser day=AAAA-MM-JJ"
            " pour annuler cette seule occurrence, ou day='all' pour supprimer la serie."
        )

    if toute_la_serie:
        event_model.delete_event(event_id)
        return {"event_id": event_id, "title": titre, "scope": "series", "deleted": True}

    event_model.cancel_occurrence(event_id, jour.isoformat())
    return {
        "event_id": event_id,
        "title": titre,
        "scope": "occurrence",
        "cancelled_day": jour.isoformat(),
    }


def _add_waiting(person, subject, due=None, task_id=None):
    """Enregistre une attente : quelqu'un me doit quelque chose.

    A ne pas confondre avec une tache : si c'est moi qui dois quelque chose a
    quelqu'un, c'est create_task qu'il faut appeler.

    Un interlocuteur inconnu n'echoue pas : il est cree au passage et le
    retour le signale, comme une tache signale un projet introuvable.

    :param person: nom de l'interlocuteur, obligatoire
    :param subject: ce qui est attendu, obligatoire
    :param due: date esperee AAAA-MM-JJ, facultative
    :param task_id: tache bloquee par cette attente, facultative
    :return: {'waiting': attente compacte, 'note': avertissement eventuel}
    """
    nom = str(person or "").strip()
    if not nom:
        raise ToolError("Le nom de l'interlocuteur est obligatoire.")

    sujet = str(subject or "").strip()
    if not sujet:
        raise ToolError("Le sujet de l'attente est obligatoire.")

    data = {"subject": sujet}

    if due not in (None, ""):
        data["due"] = _as_date(due, "L'echeance attendue").isoformat()

    if task_id not in (None, ""):
        identifiant = _as_int(task_id, "L'identifiant de tache", minimum=1)
        if task_model.get_task(identifiant) is None:
            raise ToolError(f"Tache {identifiant} introuvable.")
        data["task_id"] = identifiant

    existant = person_model.find_by_name(nom)
    interlocuteur = existant or person_model.get_or_create(nom)
    data["person_id"] = interlocuteur["id"]

    waiting = person_model.create_waiting(data)
    result = {"waiting": _waiting_brief(waiting)}
    if existant is None:
        result["note"] = f"Interlocuteur '{_truncate(interlocuteur['name'], 40)}' cree."
    return result


def _resolve_waiting(waiting_id, status="received"):
    """Clot une attente : recue ou abandonnee.

    Une attente ne se rouvre pas ici : seuls 'received' et 'dropped' sont
    acceptes, pour eviter qu'un statut approximatif efface une relance.

    :param waiting_id: identifiant de l'attente
    :param status: 'received' (recu) ou 'dropped' (abandonne), defaut received
    :return: {'waiting': attente compacte, 'status': statut enregistre}
    """
    waiting_id = _as_int(waiting_id, "L'identifiant d'attente", minimum=1)
    if waiting_id is None:
        raise ToolError("L'identifiant d'attente est obligatoire.")

    statut = _as_waiting_status(status, default="received", allow_all=False)
    if statut not in WAITING_RESOLVED:
        raise ToolError(
            "Statut invalide : attendu received (recu) ou dropped (abandonne)."
            " Une attente ne se rouvre pas depuis cet outil."
        )

    attente = person_model.get_waiting(waiting_id)
    if attente is None:
        raise ToolError(f"Attente {waiting_id} introuvable.")
    if attente["status"] == statut:
        raise ToolError(f"Attente {waiting_id} deja marquee {statut}.")

    updated = person_model.update_waiting(waiting_id, {"status": statut})
    return {"waiting": _waiting_brief(updated), "status": statut}


def _merge_people(keep_id, merge_id):
    """Absorbe un interlocuteur en double dans celui qu'on garde.

    Taches dues et attentes suivent la fiche gardee ; le nom absorbe devient
    un alias, pour que le triage ne recree pas le doublon.

    :param keep_id: interlocuteur conserve
    :param merge_id: interlocuteur absorbe puis supprime
    :return: {'person', 'absorbed'}
    """
    keep_id = _as_int(keep_id, "L'identifiant de l'interlocuteur conserve", minimum=1)
    merge_id = _as_int(merge_id, "L'identifiant de l'interlocuteur absorbe", minimum=1)
    if keep_id is None or merge_id is None:
        raise ToolError("keep_id et merge_id sont obligatoires : appeler list_people pour les ids.")

    absorbe = person_model.get_person(merge_id)
    if absorbe is None:
        raise ToolError(f"Interlocuteur {merge_id} introuvable.")

    gardee = person_model.merge_people(keep_id, merge_id)
    return {"person": _person_brief(gardee), "absorbed": absorbe["name"]}


def _update_person(
    name,
    new_name=None,
    role=None,
    project=None,
    email=None,
    phone=None,
    notes=None,
):
    """Modifie la fiche d'un interlocuteur, champ par champ.

    Seuls les champs transmis sont touches, et 'none' efface un champ, y
    compris le rattachement a un projet.

    :param name: nom actuel de l'interlocuteur, obligatoire
    :param new_name: nouveau nom
    :param role: fonction ou role, 'none' pour l'effacer
    :param project: nom du projet de rattachement, ou 'none' pour le detacher
    :param email: adresse, 'none' pour l'effacer
    :param phone: telephone, 'none' pour l'effacer
    :param notes: precisions libres, 'none' pour les effacer
    :return: {'person': interlocuteur compact, 'note': avertissement eventuel}
    """
    person = _resolve_person(name)

    data = {}
    if new_name not in (None, ""):
        data["name"] = str(new_name).strip()

    for champ, valeur in (("role", role), ("email", email), ("phone", phone), ("notes", notes)):
        if valeur is not None:
            data[champ] = None if _is_no_project(valeur) else valeur

    note = None
    if project is not None:
        if _is_no_project(project):
            data["project_id"] = None
        else:
            project_id, warning = _resolve_project(project)
            if project_id:
                data["project_id"] = project_id
            else:
                note = warning + " Projet inchange."

    if not data:
        if note:
            raise ToolError(note)
        raise ToolError(
            "Aucun champ a modifier : fournir new_name, role, project, email, phone ou notes."
        )

    updated = person_model.update_person(person["id"], data)
    result = {"person": _person_brief(updated)}
    if note:
        result["note"] = note
    return result


def _update_waiting(waiting_id, subject=None, due=None):
    """Corrige une attente : son sujet, sa date esperee.

    Le statut ne se change pas ici : une attente se clot avec resolve_waiting.

    :param waiting_id: identifiant de l'attente
    :param subject: nouveau sujet
    :param due: nouvelle date esperee AAAA-MM-JJ, ou 'none' pour l'effacer
    :return: {'waiting': attente compacte}
    """
    waiting_id = _as_int(waiting_id, "L'identifiant d'attente", minimum=1)
    if waiting_id is None:
        raise ToolError("L'identifiant d'attente est obligatoire.")

    if person_model.get_waiting(waiting_id) is None:
        raise ToolError(f"Attente {waiting_id} introuvable.")

    data = {}
    if subject not in (None, ""):
        data["subject"] = str(subject).strip()
    if due is not None:
        echeance = None if _is_no_project(due) else _as_date(due, "L'echeance attendue")
        data["due"] = echeance.isoformat() if echeance else None

    if not data:
        raise ToolError(
            "Aucun champ a modifier : fournir un sujet (subject) ou une echeance (due)."
            " Pour clore une attente, utiliser resolve_waiting."
        )

    return {"waiting": _waiting_brief(person_model.update_waiting(waiting_id, data))}


# --- Catalogue ----------------------------------------------------------------

TOOLS = {
    "list_tasks": {
        "description": "Liste les taches, filtrable par statut et par projet.",
        "args": {
            "status": "todo|done|all, defaut todo",
            "project": "nom du projet, ou 'none' pour les taches sans projet",
            "limit": "nombre maximal de taches, defaut 20",
        },
        "writes": False,
        "handler": _list_tasks,
    },
    "search_tasks": {
        "description": "Cherche un mot-cle dans les titres et descriptions, ouvertes d'abord.",
        "args": {
            "query": "texte recherche, obligatoire",
            "limit": "nombre maximal de resultats, defaut 10",
        },
        "writes": False,
        "handler": _search_tasks,
    },
    "find_duplicates": {
        "description": (
            "Repere les taches ouvertes qui se recouvrent, sans rien fusionner."
            " count=0 signifie simplement qu'il n'y a pas de doublon."
        ),
        "args": {
            "project": "nom du projet auquel se limiter, defaut tous",
            "threshold": "proportion de mots communs exigee, 0 a 1, defaut 0.6",
        },
        "writes": False,
        "handler": _find_duplicates,
    },
    "get_plan": {
        "description": "Plan d'une journee : creneaux, charge, taches non casees.",
        "args": {"date": "AAAA-MM-JJ, defaut aujourd'hui"},
        "writes": False,
        "handler": _get_plan,
    },
    "project_status": {
        "description": "Compteurs des projets : taches ouvertes, terminees, temps suivi.",
        "args": {"name": "nom d'un projet, defaut tous les projets"},
        "writes": False,
        "handler": _project_status,
    },
    "list_phases": {
        "description": "Etapes d'un projet (numero, nom, statut, avancement, dates) et etape en cours.",
        "args": {"project": "nom du projet, obligatoire"},
        "writes": False,
        "handler": _list_phases,
    },
    "update_phase": {
        "description": (
            "Fait avancer une etape d'un projet : statut, date prevue, date de fin, avancement."
            " Terminer une etape termine aussi les etapes precedentes encore ouvertes."
        ),
        "args": {
            "project": "nom du projet, obligatoire",
            "phase": "numero ou nom de l'etape, obligatoire",
            "status": "todo|doing|blocked|done",
            "planned_date": "date prevue AAAA-MM-JJ, ou 'none'",
            "done_date": "date de fin reelle AAAA-MM-JJ, defaut aujourd'hui si done",
            "progress_pct": "avancement 0 a 100, seulement si l'etape n'a pas de taches",
        },
        "writes": True,
        "handler": _update_phase,
    },
    "get_stats": {
        "description": "Chiffres d'une semaine : creees, terminees, temps suivi, retards.",
        "args": {"weeks_back": "0 semaine en cours, 1 precedente, defaut 0"},
        "writes": False,
        "handler": _get_stats,
    },
    "current_timer": {
        "description": "Chronometre en cours, avec la tache et le temps ecoule.",
        "args": {},
        "writes": False,
        "handler": _current_timer,
    },
    "list_events": {
        "description": (
            "Agenda d'une periode : reunions, creneaux bloques, absences, perso,"
            " et days_off (jours non travailles). Un evenement n'est pas une tache."
        ),
        "args": {
            "date": "premier jour AAAA-MM-JJ, defaut aujourd'hui",
            "days": "nombre de jours couverts, 1 a 31, defaut 1",
        },
        "writes": False,
        "handler": _list_events,
    },
    "get_risks": {
        "description": (
            "Echeances en danger : travail restant superieur au temps libre"
            " d'ici l'echeance. Liste vide = tout tient."
        ),
        "args": {},
        "writes": False,
        "handler": _get_risks,
    },
    "list_days_off": {
        "description": "Conges et feries d'une periode, avec leur identifiant.",
        "args": {
            "date": "premier jour AAAA-MM-JJ, defaut aujourd'hui",
            "days": "nombre de jours couverts, 1 a 366, defaut 31",
        },
        "writes": False,
        "handler": _list_days_off,
    },
    "find_free_slots": {
        "description": "Trous libres d'une journee, agenda deduit : quand suis-je disponible.",
        "args": {
            "date": "AAAA-MM-JJ, defaut aujourd'hui",
            "min_minutes": "duree minimale d'un trou retenu, defaut 30",
        },
        "writes": False,
        "handler": _find_free_slots,
    },
    "search_conversations": {
        "description": (
            "Cherche dans les conversations du chat celles qui parlent d'un sujet,"
            " quand l'utilisateur fait reference a un echange passe ou a une autre conversation."
        ),
        "args": {"query": "mots a retrouver, obligatoire"},
        "writes": False,
        "handler": _search_conversations,
    },
    "read_conversation": {
        "description": "Relit les derniers messages d'une autre conversation (id via search_conversations).",
        "args": {
            "conversation_id": "identifiant, obligatoire",
            "limit": "nombre de messages, 12 par defaut, 30 au plus",
        },
        "writes": False,
        "handler": _read_conversation,
    },
    "list_people": {
        "description": (
            "Interlocuteurs connus, avec ce que je leur dois (taches)"
            " et ce qu'ils me doivent (attentes)."
        ),
        "args": {},
        "writes": False,
        "handler": _list_people,
    },
    "person_status": {
        "description": (
            "Fiche d'un interlocuteur : coordonnees, i_owe (mes taches pour lui)"
            " et they_owe (ce qu'il me doit)."
        ),
        "args": {"name": "nom de l'interlocuteur, obligatoire"},
        "writes": False,
        "handler": _person_status,
    },
    "list_waiting": {
        "description": (
            "Ce que j'attends des autres, plus ancienne d'abord : 'je relance qui ?'."
            " Une attente n'est pas ma tache, elle se relance."
        ),
        "args": {
            "person": "nom d'un interlocuteur, defaut tous",
            "status": "open|received|dropped|all, defaut open",
        },
        "writes": False,
        "handler": _list_waiting,
    },
    "create_task": {
        "description": "Cree une tache. Projet introuvable : creee sans projet, avec une note.",
        "args": {
            "title": "intitule, obligatoire",
            "description": "precisions, optionnel",
            "priority": "1 urgent a 4 confort, defaut 3",
            "estimate_min": "duree estimee en minutes, defaut 30",
            "deadline": "AAAA-MM-JJ, optionnel",
            "energy": "low|med|high, defaut med",
            "project": "nom du projet, optionnel",
            "person": "nom de la personne a qui JE dois cette tache, optionnel",
        },
        "writes": True,
        "handler": _create_task,
    },
    "update_task": {
        "description": "Modifie les champs fournis d'une tache ('none' efface deadline/project).",
        "args": {
            "task_id": "identifiant, obligatoire",
            "title": "nouvel intitule",
            "description": "nouvelle description",
            "priority": "1 a 4",
            "estimate_min": "duree estimee en minutes",
            "deadline": "AAAA-MM-JJ ou 'none'",
            "energy": "low|med|high",
            "project": "nom du projet ou 'none'",
            "person": "nom de la personne a qui JE dois cette tache, ou 'none'",
        },
        "writes": True,
        "handler": _update_task,
    },
    "complete_task": {
        "description": "Marque une tache terminee. Echoue si elle l'est deja.",
        "args": {"task_id": "identifiant, obligatoire"},
        "writes": True,
        "handler": _complete_task,
    },
    "pin_task": {
        "description": (
            "Place une tache dans le plan du jour a la main : 'now' la met en tete"
            " (je la fais maintenant), 'end' a la suite, 'none' la rend au classement auto."
        ),
        "args": {
            "task_id": "identifiant, obligatoire",
            "position": "now (defaut), end ou none",
            "day": "jour AAAA-MM-JJ, defaut aujourd'hui",
        },
        "writes": True,
        "handler": _pin_task,
    },
    "postpone_task": {
        "description": "Decale l'echeance d'une tache, a une date ou d'un nombre de jours.",
        "args": {
            "task_id": "identifiant, obligatoire",
            "deadline": "nouvelle echeance AAAA-MM-JJ",
            "days": "decalage en jours depuis l'echeance actuelle, sinon depuis aujourd'hui",
        },
        "writes": True,
        "handler": _postpone_task,
    },
    "merge_tasks": {
        "description": (
            "Fusionne des doublons dans une seule tache, puis supprime les absorbees."
            " Le temps chronometre, les notes et les attentes suivent la tache gardee."
        ),
        "args": {
            "keep_id": "identifiant de la tache conservee, obligatoire",
            "merge_ids": "taches absorbees : liste, entier seul ou '3,7', obligatoire",
        },
        "writes": True,
        "destructive": True,
        "handler": _merge_tasks,
    },
    "delete_task": {
        "description": (
            "Supprime definitivement une tache, avec le temps chronometre dessus."
            " Sur un doublon, preferer merge_tasks, qui conserve ce temps."
        ),
        "args": {"task_id": "identifiant, obligatoire"},
        "writes": True,
        "destructive": True,
        "handler": _delete_task,
    },
    "start_timer": {
        "description": "Demarre le chronometre sur une tache, en arretant le precedent.",
        "args": {"task_id": "identifiant, obligatoire"},
        "writes": True,
        "handler": _start_timer,
    },
    "stop_timer": {
        "description": "Arrete le chronometre en cours et enregistre la duree.",
        "args": {},
        "writes": True,
        "handler": _stop_timer,
    },
    "create_event": {
        "description": (
            "Pose un evenement horaire dans l'agenda (reunion, focus, absence, perso),"
            " pas une tache. Journee entiere de conge : create_day_off."
            " Projet introuvable : cree sans projet, avec une note."
        ),
        "args": {
            "title": "intitule, obligatoire",
            "starts_at": "debut AAAA-MM-JJ HH:MM, obligatoire",
            "ends_at": "fin AAAA-MM-JJ HH:MM, ou HH:MM le meme jour",
            "duration_min": "duree en minutes, a defaut de ends_at",
            "kind": "meeting|focus|absence|personal, defaut meeting",
            "location": "lieu, optionnel",
            "project": "nom du projet, optionnel",
            "repeat": "none|daily|weekly, defaut none",
            "repeat_until": "dernier jour de la serie AAAA-MM-JJ, optionnel",
            "notes": "precisions, optionnel",
            "start_time": "heure HH:MM si starts_at ne porte qu'une date",
        },
        "writes": True,
        "handler": _create_event,
    },
    "cancel_event": {
        "description": (
            "Annule un evenement : day=AAAA-MM-JJ pour un seul jour d'une serie,"
            " day='all' pour toute la serie. Une serie sans day est refusee."
        ),
        "args": {
            "event_id": "identifiant, obligatoire",
            "day": "jour annule AAAA-MM-JJ, ou 'all' pour la serie entiere",
        },
        "writes": True,
        "handler": _cancel_event,
    },
    "create_day_off": {
        "description": (
            "Pose un conge ou un ferie sur une ou plusieurs journees entieres :"
            " rien n'est planifie ces jours-la."
        ),
        "args": {
            "title": "motif, obligatoire",
            "start_day": "premier jour AAAA-MM-JJ, obligatoire",
            "end_day": "dernier jour inclus AAAA-MM-JJ, defaut le premier",
            "kind": "leave|holiday, defaut leave",
        },
        "writes": True,
        "handler": _create_day_off,
    },
    "delete_day_off": {
        "description": "Supprime un conge ou un ferie (identifiant via list_days_off).",
        "args": {"day_off_id": "identifiant, obligatoire"},
        "writes": True,
        "handler": _delete_day_off,
    },
    "add_waiting": {
        "description": (
            "Enregistre une attente : ON ME doit quelque chose ('Paul doit m'envoyer X')."
            " Si JE dois quelque chose a quelqu'un, c'est create_task."
            " Interlocuteur inconnu : cree au passage, avec une note."
        ),
        "args": {
            "person": "nom de l'interlocuteur attendu, obligatoire",
            "subject": "ce qui est attendu, obligatoire",
            "due": "date esperee AAAA-MM-JJ, optionnel",
            "task_id": "tache bloquee par cette attente, optionnel",
        },
        "writes": True,
        "handler": _add_waiting,
    },
    "resolve_waiting": {
        "description": (
            "Clot une attente : received (recu) ou dropped (abandonne)."
            " Aucun autre statut n'est accepte."
        ),
        "args": {
            "waiting_id": "identifiant de l'attente, obligatoire",
            "status": "received|dropped, defaut received",
        },
        "writes": True,
        "handler": _resolve_waiting,
    },
    "update_person": {
        "description": (
            "Modifie la fiche d'un interlocuteur designe par son nom"
            " ('none' efface un champ ou detache le projet)."
        ),
        "args": {
            "name": "nom actuel de l'interlocuteur, obligatoire",
            "new_name": "nouveau nom",
            "role": "fonction ou role, ou 'none'",
            "project": "nom du projet de rattachement, ou 'none'",
            "email": "adresse, ou 'none'",
            "phone": "telephone, ou 'none'",
            "notes": "precisions libres, ou 'none'",
        },
        "writes": True,
        "handler": _update_person,
    },
    "merge_people": {
        "description": (
            "Fusionne deux fiches d'un meme interlocuteur cite sous deux noms."
            " Taches et attentes suivent la fiche gardee, l'autre nom devient un alias."
        ),
        "args": {
            "keep_id": "identifiant de l'interlocuteur conserve, obligatoire",
            "merge_id": "identifiant de l'interlocuteur absorbe puis supprime, obligatoire",
        },
        "writes": True,
        "destructive": True,
        "handler": _merge_people,
    },
    "update_waiting": {
        "description": (
            "Corrige une attente : son sujet, sa date esperee."
            " Le statut ne se change pas ici : pour clore, utiliser resolve_waiting."
        ),
        "args": {
            "waiting_id": "identifiant de l'attente, obligatoire",
            "subject": "nouveau sujet",
            "due": "nouvelle date esperee AAAA-MM-JJ, ou 'none'",
        },
        "writes": True,
        "handler": _update_waiting,
    },
}


def describe():
    """Retourne le catalogue des outils en texte compact, pour le prompt systeme.

    :return: une ligne par outil, au format 'nom(arg1, arg2) - description'
    """
    lignes = []
    for name, spec in TOOLS.items():
        signature = ", ".join(spec["args"].keys())
        marque = " [ecrit]" if spec["writes"] else ""
        if spec.get("destructive"):
            marque += " [destructif, confirmation exigee]"
        lignes.append(f"{name}({signature}) - {spec['description']}{marque}")
    return "\n".join(lignes)


def _accepted_params(handler):
    """Retourne les noms d'arguments reellement acceptes par un handler."""
    return set(inspect.signature(handler).parameters)


def _normalize_args(args, handler):
    """Applique les alias connus et ecarte les arguments inconnus.

    :param args: arguments bruts proposes par le modele
    :param handler: fonction cible, dont la signature fait autorite
    :return: dictionnaire d'arguments acceptables par le handler
    """
    accepted = _accepted_params(handler)
    cleaned = {key: value for key, value in args.items() if key in accepted}

    for key, value in args.items():
        if key in accepted:
            continue
        for cible in ARG_ALIASES.get(str(key).strip().lower(), []):
            if cible in accepted and cible not in cleaned:
                cleaned[cible] = value
                break

    return cleaned


def execute(name, args):
    """Execute un outil du catalogue.

    :param name: nom de l'outil, tel que liste par describe()
    :param args: dictionnaire d'arguments ; les inconnus sont ignores
    :return: {'ok': True, 'data': <serialisable JSON>} ou
        {'ok': False, 'error': '<message court en francais>'}
    """
    spec = TOOLS.get(name)
    if spec is None:
        return {"ok": False, "error": f"Outil inconnu : {_truncate(str(name), 40)}."}

    if args in (None, ""):
        args = {}
    if not isinstance(args, dict):
        return {"ok": False, "error": "Les arguments doivent former un objet JSON."}

    handler = spec["handler"]
    try:
        cleaned = _normalize_args(args, handler)
        return {"ok": True, "data": handler(**cleaned)}
    except TypeError as erreur:
        # Argument obligatoire absent : le modele doit savoir lequel completer.
        logger.warning("Outil %s : arguments invalides (%s)", name, erreur)
        return {"ok": False, "error": f"Arguments invalides pour {name}."}
    except (
        ToolError,
        task_model.ValidationError,
        project_model.ProjectError,
        event_model.EventError,
        day_off_model.DayOffError,
        person_model.PersonError,
    ) as erreur:
        return {"ok": False, "error": str(erreur)}
    except Exception as erreur:  # noqa: BLE001 - un outil ne doit jamais remonter
        logger.exception("Outil %s en echec", name)
        return {"ok": False, "error": f"Echec de {name} : {type(erreur).__name__}."}
