"""Proactivite : reperer ce qui va deraper avant que ce soit fait.

Les rendez-vous quotidiens constatent (retards, derive). Ce module anticipe :

- les echeances que le temps libre restant ne permet plus de tenir ;
- le contexte d'une reunion qui approche (ce que je dois, ce qu'on me doit) ;
- les taches qui dorment, les validations oubliees, les projets a l'arret.

Chaque fonction ne fait que detecter : l'envoi, le moment et le silence entre
deux alertes sont decides par le battement de coeur. Les detecteurs restent
ainsi testables un par un, sans horloge ni Telegram.
"""
import re
import unicodedata
from datetime import date, datetime, timedelta

from models import get_db
from models import person as person_model
from models import project as project_model
from models import setting
from models import task as task_model
from services import planner, workdays

# Reste a faire minimal d'une tache dont le temps chronometre depasse deja
# l'estimation : elle n'est pas finie, donc il reste quelque chose.
MIN_REMAINING_MIN = 15

# Nombre maximal d'echeances en danger remontees : au-dela, le probleme n'est
# plus une echeance mais la charge entiere, et l'alerte de surcharge le dit.
MAX_RISKS = 3

# Taille minimale d'un nom de projet ou d'interlocuteur cherche dans le titre
# d'une reunion : « RH » ou « Al » se retrouveraient dans n'importe quel mot.
MIN_NAME_CHARS = 3

# Taches du projet citees dans la preparation d'une reunion : dues sous peu.
MEETING_DUE_DAYS = 3
MAX_CONTEXT_ITEMS = 3


# --- Echeances en danger --------------------------------------------------------


def horizon_end(today, working_days):
    """Retourne le jour atteint apres `working_days` jours ouvres."""
    jour = today
    restants = working_days
    # Garde-fou : un calendrier entierement chome ne doit pas boucler.
    for _ in range(working_days * 7 + 14):
        if restants <= 0:
            break
        jour += timedelta(days=1)
        if workdays.is_working_day(jour, today):
            restants -= 1
    return jour


def remaining_minutes(task):
    """Reste a faire d'une tache : duree reservee moins le temps deja passe."""
    reserve = planner.padded_duration(task)
    passe = int(task.get("tracked_min") or 0)
    return max(reserve - passe, MIN_REMAINING_MIN)


def deadlines_at_risk(now=None):
    """Retourne les echeances que le temps libre ne permet plus de tenir.

    Le calcul est cumule, echeance par echeance : deux taches de trois heures
    dues jeudi tiennent chacune seule dans quatre heures libres, pas ensemble.
    Le temps libre est celui du planificateur, donc apres reunions (Outlook
    compris), conges, feries et week-ends ; pour aujourd'hui, il ne compte
    que ce qui reste apres `now`.

    :return: liste de {deadline, tasks, work_min, free_min, deficit_min,
        days_left}, la plus proche d'abord
    """
    now = now or datetime.now()
    today = now.date()
    fin = horizon_end(today, max(setting.get_int("risk_horizon_days"), 1))

    taches = [
        tache
        for tache in task_model.list_tasks(status="todo")
        if tache["deadline"] and today.isoformat() <= tache["deadline"] <= fin.isoformat()
    ]
    if not taches:
        return []

    par_echeance = {}
    for tache in taches:
        par_echeance.setdefault(tache["deadline"], []).append(tache)

    libre_par_jour = {}

    def libre(jour):
        if jour not in libre_par_jour:
            libre_par_jour[jour] = planner.free_minutes(jour, after=now if jour == today else None)
        return libre_par_jour[jour]

    risques = []
    travail = 0
    for echeance in sorted(par_echeance):
        cible = datetime.strptime(echeance, "%Y-%m-%d").date()
        du_jour = par_echeance[echeance]
        travail += sum(remaining_minutes(tache) for tache in du_jour)
        disponible = sum(
            libre(today + timedelta(days=ecart)) for ecart in range((cible - today).days + 1)
        )
        if travail > disponible:
            risques.append(
                {
                    "deadline": echeance,
                    "tasks": [
                        {"id": tache["id"], "title": tache["title"], "priority": tache["priority"]}
                        for tache in du_jour
                    ],
                    "work_min": travail,
                    "free_min": disponible,
                    "deficit_min": travail - disponible,
                    "days_left": workdays.working_days_until(cible, today),
                }
            )
        if len(risques) >= MAX_RISKS:
            break

    return risques


# --- Preparation de reunion -------------------------------------------------------


def _fold(texte):
    """Minuscules sans accents, pour comparer un titre et un nom."""
    decompose = unicodedata.normalize("NFKD", texte or "")
    return "".join(car for car in decompose if not unicodedata.combining(car)).casefold()


def _names_in(titre, noms):
    """Retourne les noms presents dans un titre, en mot entier."""
    titre_plie = _fold(titre)
    trouves = []
    for nom in noms:
        plie = _fold(nom).strip()
        if len(plie) < MIN_NAME_CHARS:
            continue
        if re.search(rf"(?<!\w){re.escape(plie)}(?!\w)", titre_plie):
            trouves.append(nom)
    return trouves


def meeting_context(event, today=None):
    """Retourne ce qu'il faut avoir en tete avant une reunion.

    Le flux Outlook publie ne porte pas les participants : le projet et les
    interlocuteurs sont donc retrouves par le projet rattache, ou par leur nom
    cite dans le titre (« Point ALPHA », « Call Paul »).

    :return: {'project', 'people', 'i_owe', 'they_owe', 'due_soon'} ; listes
        vides si rien n'est reconnu
    """
    today = today or date.today()
    titre = event.get("title") or ""

    projets = project_model.list_projects("active")
    projet = None
    if event.get("project_id"):
        projet = next((p for p in projets if p["id"] == event["project_id"]), None)
    if projet is None:
        cites = _names_in(titre, [p["name"] for p in projets])
        if cites:
            projet = next(p for p in projets if p["name"] == max(cites, key=len))

    personnes = person_model.list_people()
    noms_cites = set(_names_in(titre, [p["name"] for p in personnes]))
    concernees = [p for p in personnes if p["name"] in noms_cites]

    je_dois = []
    on_me_doit = []
    for personne in concernees:
        for tache in person_model.owed_tasks(personne["id"]):
            je_dois.append({**tache, "person_name": personne["name"]})
        on_me_doit.extend(person_model.list_waiting(status="open", person_id=personne["id"]))

    bientot = []
    if projet is not None:
        limite = (today + timedelta(days=MEETING_DUE_DAYS)).isoformat()
        deja = {tache["id"] for tache in je_dois}
        bientot = [
            tache
            for tache in task_model.list_tasks(status="todo", project_id=projet["id"])
            if tache["deadline"] and tache["deadline"] <= limite and tache["id"] not in deja
        ]

    return {
        "project": projet["name"] if projet else None,
        "people": [p["name"] for p in concernees],
        "i_owe": je_dois[:MAX_CONTEXT_ITEMS],
        "they_owe": on_me_doit[:MAX_CONTEXT_ITEMS],
        "due_soon": bientot[:MAX_CONTEXT_ITEMS],
    }


# --- Menage -------------------------------------------------------------------------


def dormant_tasks(today=None):
    """Retourne les taches qui dorment : sans echeance, jamais commencees, anciennes.

    Sans echeance, une tache n'est jamais en retard : rien ne la signale, elle
    reste en bas de liste indefiniment. La plus ancienne d'abord.
    """
    today = today or date.today()
    limite = (today - timedelta(days=setting.get_int("dormant_after_days"))).isoformat()

    dormantes = [
        tache
        for tache in task_model.list_tasks(status="todo")
        if not tache["deadline"]
        and not tache.get("tracked_min")
        and (tache["created_at"] or "")[:10] <= limite
    ]
    dormantes.sort(key=lambda tache: tache["created_at"] or "")
    return dormantes


def forgotten_validations(now=None):
    """Retourne ce que le triage attend de moi depuis trop longtemps.

    :return: {'proposals': [...], 'questions': [...]} ; une question est datee
        de son dernier message, pas de la capture
    """
    now = now or datetime.now()
    limite = (now - timedelta(hours=setting.get_int("validation_after_hours"))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db = get_db()

    propositions = db.execute(
        "SELECT p.id, p.capture_id, p.summary, p.created_at FROM proposal p"
        " WHERE p.status = 'pending' AND p.created_at < ? ORDER BY p.created_at",
        (limite,),
    ).fetchall()

    questions = db.execute(
        "SELECT c.id, c.raw_text, COALESCE(MAX(m.created_at), c.created_at) AS asked_at"
        " FROM capture c LEFT JOIN message m ON m.capture_id = c.id"
        " WHERE c.status = 'waiting'"
        " GROUP BY c.id HAVING asked_at < ? ORDER BY asked_at",
        (limite,),
    ).fetchall()

    return {
        "proposals": [dict(row) for row in propositions],
        "questions": [dict(row) for row in questions],
    }


def idle_projects(today=None):
    """Retourne les projets actifs sans aucune activite recente.

    L'activite est la plus recente des dates : creation du projet, creation
    ou cloture d'une tache, session chronometree. Un projet sans tache ouverte
    n'est pas signale : il est probablement fini, pas oublie.
    """
    today = today or date.today()
    limite = (today - timedelta(days=setting.get_int("project_idle_days"))).isoformat()

    rows = get_db().execute(
        "SELECT p.id, p.name,"
        " MAX(p.created_at,"
        "     COALESCE((SELECT MAX(MAX(t.created_at, COALESCE(t.done_at, '')))"
        "               FROM task t WHERE t.project_id = p.id), ''),"
        "     COALESCE((SELECT MAX(s.started_at) FROM session s"
        "               JOIN task t ON t.id = s.task_id WHERE t.project_id = p.id), '')"
        " ) AS last_activity,"
        " (SELECT COUNT(*) FROM task t WHERE t.project_id = p.id AND t.status = 'todo')"
        "   AS open_count"
        " FROM project p WHERE p.status = 'active'",
    ).fetchall()

    return [
        dict(row)
        for row in rows
        if row["open_count"] and (row["last_activity"] or "")[:10] < limite
    ]
