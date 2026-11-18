"""Operations ouvertes aux sessions Claude Code (serveur MCP local).

Une session Claude qui travaille sur un vrai sujet relie ce travail a une
tache NOMAIA : elle la retrouve ou la cree, note son avancement et le temps
passe. Le bilan s'appuie alors sur du travail declare au fil de l'eau, et non
sur un chronometre que l'on oublie.

Ce module ne connait ni HTTP ni MCP : il valide et ecrit. Le controleur
/api/agent/ l'expose, le serveur MCP appelle ce controleur.
"""
import re
import unicodedata
from datetime import date, datetime

from flask import g, has_app_context

from models import get_db, journal, knowledge
from models import minutes as minutes_model
from models import person as person_model
from models import phase as phase_model
from models import project as project_model
from models import space as space_model
from models import session as session_model
from models import task as task_model
from models.journal import JournalError
from models.knowledge import KnowledgeError
from models.minutes import MinutesError
from models.task import VALID_KIND, VALID_SEVERITY, VALID_SIDE, ValidationError
from services import minutes as minutes_service
from services import progress_report as report_service
from services import project_board, project_register, secrets_guard
from services.secrets_guard import SecretDetected

SOURCE = "claude"

# Duree maximale declaree en une fois : au-dela, c'est une erreur de saisie
# (heures prises pour des minutes), pas une session de travail.
MAX_WORK_MIN = 600

# Temps declare plausible : au plus le temps ecoule depuis la creation de la
# tache (ou depuis la derniere declaration sur elle), plus cette marge. Au-dela
# la duree est enregistree quand meme, mais la session est avertie : une
# estimation de l'effort humain gonflait le bilan (x2 a x7 constates).
ELAPSED_MARGIN_RATIO = 1.2
ELAPSED_MARGIN_MIN = 15

VALID_STATUS_FILTERS = ("todo", "done", "all")

# Statuts tels que les sessions les ecrivent spontanement : ramenes aux deux
# statuts de NOMAIA (une tache est ouverte ou terminee) plutot que refuses.
STATUS_ALIASES = {
    "open": "todo", "opened": "todo", "in_progress": "todo", "doing": "todo", "pending": "todo",
    "started": "todo", "wip": "todo", "a_faire": "todo", "en_cours": "todo", "ouverte": "todo",
    "reopen": "todo", "reopened": "todo",
    "closed": "done", "close": "done", "completed": "done", "complete": "done", "finished": "done",
    "resolved": "done", "fait": "done", "faite": "done", "termine": "done", "terminee": "done",
    "tout": "all", "toutes": "all", "any": "all",
}


class AgentError(ValueError):
    """Demande d'une session Claude refusee, avec un message actionnable."""


def _space():
    """Espace de la cle presentee par la session ('pro' ou 'perso').

    Pose par controllers/security.py ; None hors d'une requete /api/agent/
    (appel interne ou test) : aucune restriction alors.
    """
    return g.get("agent_space") if has_app_context() else None


def _phase_labels(project_id=None):
    """Libelles des etapes, par identifiant : '5. Recette'."""
    return {
        phase["id"]: f"{phase['sequence']}. {phase['name']}"
        for phase in phase_model.list_phases(project_id)
    }


def _brief(task, phases=None):
    """Tache reduite aux champs utiles a une session Claude.

    :param phases: libelles des etapes deja charges (evite une requete par tache)
    """
    if phases is None and task.get("phase_id"):
        phases = _phase_labels(task.get("project_id"))
    return {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "priority": task["priority"],
        "deadline": task["deadline"],
        "project": task.get("project_name"),
        "progress_pct": task.get("progress_pct") or 0,
        "estimate_min": task["estimate_min"],
        "tracked_min": task.get("tracked_min") or 0,
        "source": task.get("source"),
        "phase": (phases or {}).get(task.get("phase_id")),
        "blocked": bool(task.get("blocked")),
        "side": task.get("side"),
        "kind": task.get("kind") or "task",
        "severity": task.get("severity"),
        "module": task.get("module"),
        "person": task.get("person_name"),
    }


def _task_or_error(task_id):
    """Retourne la tache, ou leve une AgentError lisible."""
    try:
        task = task_model.get_task(int(task_id))
    except (TypeError, ValueError):
        raise AgentError("Identifiant de tache attendu (task_id).")
    espace = _space()
    if task is None or (espace and task.get("space") != espace):
        # Une tache de l'autre espace est traitee comme absente : la session
        # n'apprend meme pas qu'elle existe.
        raise AgentError(f"Tache {task_id} introuvable : appeler list_tasks pour les identifiants.")
    return task


def _fold(text):
    """Minuscules sans accents, ponctuation reduite a des espaces : base des comparaisons souples."""
    decompose = unicodedata.normalize("NFKD", str(text or ""))
    sans_accents = "".join(car for car in decompose if not unicodedata.combining(car)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", sans_accents).strip()


def _status(value):
    """Statut normalise (todo, done, all...) : synonymes et accents toleres."""
    texte = _fold(value).replace(" ", "_")
    return STATUS_ALIASES.get(texte, texte)


def _fuzzy_project(texte):
    """Projet designe a peu pres : accents, casse et ponctuation ignores, nom partiel.

    Un nom partiel (« Nordik » pour « NORDIK EUROPA ») est accepte s'il ne
    designe qu'un seul projet ; sinon l'erreur liste les candidats.
    """
    cherche = _fold(texte)
    if not cherche:
        return None
    projets = project_model.list_projects(status=None, space=_space())
    exacts =[p for p in projets if _fold(p["name"]) == cherche]
    if exacts:
        return exacts[0]
    # Mots entiers : « sun » ne designe pas « NORDIK EUROPA », « nordik » oui ;
    # et « paie ALPHA » designe « ALPHA » (le nom du projet est dans le texte).
    cherche_mots = f" {cherche} "
    proches = [p for p in projets
               if cherche_mots in f" {_fold(p['name'])} " or f" {_fold(p['name'])} " in cherche_mots]
    actifs = [p for p in proches if p.get("status") != "done"] or proches
    if len(actifs) == 1:
        return actifs[0]
    if len(actifs) > 1:
        noms = ", ".join(p["name"] for p in actifs)
        raise AgentError(f"Projet '{texte}' ambigu : {noms}. Preciser le nom.")
    return None


def _project_id(name):
    """Resout un nom de projet ; None si absent. Leve une erreur si inconnu ou ambigu."""
    texte = (name or "").strip()
    if not texte:
        return None
    projet = project_model.find_by_name(texte, space=_space()) or _fuzzy_project(texte)
    if projet is None:
        connus = ", ".join(
            p["name"] for p in project_model.list_projects(status="active", space=_space())
        )
        raise AgentError(f"Projet '{texte}' inconnu. Projets actifs : {connus or 'aucun'}.")
    return projet["id"]


def _phase_id(project_id, phase):
    """Resout une etape du projet par son numero (1, 2...) ou son nom.

    :return: identifiant de l'etape, None pour retirer l'etape (chaine vide)
    :raises AgentError: tache sans projet, etape inconnue ou ambigue
    """
    texte = str(phase).strip()
    if not texte:
        return None
    if project_id is None:
        raise AgentError("La tache n'a pas de projet : une etape se range dans un projet.")
    try:
        return phase_model.find_phase(project_id, texte)["id"]
    except ValidationError as error:
        raise AgentError(str(error))


def _side(side):
    """Valide « qui doit agir » : us, client, both, ou chaine vide pour effacer."""
    valeur = str(side).strip().lower()
    if valeur and valeur not in VALID_SIDE:
        raise AgentError("side attendu : us, client ou both (chaine vide pour effacer).")
    return valeur or None


def _choice(value, valid, field):
    """Valide une valeur parmi une liste fermee ; chaine vide vaut None."""
    texte = str(value).strip().lower()
    if texte and texte not in valid:
        raise AgentError(f"{field} attendu : {', '.join(valid)}.")
    return texte or None


def list_tasks(status="todo", project=None, query=None, limit=30, kind=None):
    """Liste les taches, filtrees par statut, projet, nature et mots du titre.

    :param kind: 'task', 'anomaly' ou 'deliverable' ; None pour toutes
    :return: liste de taches compactes
    """
    status = _status(status or "todo")
    if status not in VALID_STATUS_FILTERS:
        raise AgentError("status attendu : todo, done ou all.")
    nature = _choice(kind, VALID_KIND, "kind") if kind else None

    project_id = _project_id(project)
    taches = task_model.list_tasks(
        status=None if status == "all" else status, project_id=project_id, space=_space()
    )

    if nature:
        taches = [task for task in taches if (task.get("kind") or "task") == nature]
    mots = [mot for mot in (query or "").lower().split() if mot]
    if mots:
        taches = [
            task for task in taches
            if all(mot in (task["title"] + " " + (task["description"] or "")).lower() for mot in mots)
        ]
    try:
        plafond = max(1, min(int(limit or 30), 100))
    except (TypeError, ValueError):
        raise AgentError("limit attendu : un entier entre 1 et 100.")
    etapes = _phase_labels(project_id)
    return [_brief(task, etapes) for task in taches[:plafond]]


def get_task(task_id):
    """Detail d'une tache : description, etapes et journal recent."""
    task = _task_or_error(task_id)
    detail = _brief(task)
    detail["description"] = task["description"]
    detail["steps"] = [_brief(step) for step in task_model.list_subtasks(task["id"])]
    detail["journal"] = [
        {
            "at": entry["created_at"],
            "kind": entry["kind"],
            "minutes": entry["minutes"],
            "source": entry["source"],
            "content": entry["content"],
        }
        for entry in journal.list_for_task(task["id"], limit=15)
    ]
    return detail


def create_task(title, description=None, project=None, priority=3, estimate_min=60,
                deadline=None, parent_id=None, force=False, phase=None, blocked=False,
                side=None, kind=None, severity=None, module=None, person=None):
    """Cree une tache au nom d'une session Claude.

    Avant de creer, cherche une tache ouverte au titre proche : la session doit
    reprendre l'existante plutot que d'ouvrir un doublon. `force` passe outre.

    :param phase: etape du projet, par numero ou par nom (projet obligatoire)
    :param blocked: tache bloquee des sa creation
    :param side: qui doit agir : us, client ou both (client par defaut pour un livrable)
    :param kind: 'task', 'anomaly' (bug remonte par le client) ou 'deliverable'
    :param severity: gravite d'une anomalie : critical, major (defaut), minor
    :param module: module concerne par une anomalie (Ventes, Paie...)
    :param person: a qui je dois cette tache (contact cree s'il est inconnu)
    :return: {'created': tache} ou {'similar': [taches proches]}
    """
    if not force:
        proches = task_model.similar_open_tasks(title or "", space=_space())
        if proches:
            return {
                "created": None,
                "similar": [_brief(task) for task in proches],
                "hint": "Des taches proches existent : reprends-en une (update_task), "
                        "ou rappelle create_task avec force=true.",
            }

    project_id = _project_id(project)
    if parent_id is not None:
        _task_or_error(parent_id)  # parente invisible depuis cet espace : refusee
    espace = {"space": _space()} if _space() else {}
    try:
        task = task_model.create_task(
            {
                **espace,
                "title": title,
                "description": description,
                "project_id": project_id,
                "priority": priority,
                "estimate_min": estimate_min,
                "deadline": deadline,
                "parent_id": parent_id,
                "phase_id": _phase_id(project_id, phase) if phase is not None else None,
                "blocked": blocked,
                "side": _side(side) if side is not None else None,
                "kind": _choice(kind, VALID_KIND, "kind") if kind else None,
                "severity": _choice(severity, VALID_SEVERITY, "severity") if severity else None,
                "module": module,
                "person_id": _person_id(person, project_id) if person else None,
            },
            source=SOURCE,
        )
    except ValidationError as error:
        raise AgentError(str(error))
    journal.add_entry("Tache ouverte par une session Claude.", task_id=task["id"], source=SOURCE)
    return {"created": _brief(task)}


def _framing_values(task, deadline, priority, project, phase, blocked, side, kind, severity, module, person=None):
    """Champs de cadrage fournis par la session, traduits en colonnes de tache.

    None laisse le champ inchange ; une chaine vide efface l'echeance, le
    projet, l'etape ou « qui doit agir ». L'etape se cherche dans le nouveau
    projet quand les deux changent ensemble.
    """
    valeurs = {}
    if deadline is not None:
        valeurs["deadline"] = str(deadline).strip() or None
    if priority is not None:
        valeurs["priority"] = priority
    project_id = task.get("project_id")
    if project is not None:
        project_id = valeurs["project_id"] = _project_id(project)
    if phase is not None:
        valeurs["phase_id"] = _phase_id(project_id, phase)
    if blocked is not None:
        valeurs["blocked"] = bool(blocked)
    if side is not None:
        valeurs["side"] = _side(side)
    if kind is not None:
        valeurs["kind"] = _choice(kind, VALID_KIND, "kind") or "task"
    if severity is not None:
        valeurs["severity"] = _choice(severity, VALID_SEVERITY, "severity")
    if module is not None:
        valeurs["module"] = module
    if person is not None:
        valeurs["person_id"] = _person_id(person, project_id) if str(person).strip() else None
    return valeurs


def _framing_changes(avant, apres):
    """Libelles des champs de cadrage reellement modifies, pour le journal."""
    changements = []
    if apres["deadline"] != avant["deadline"]:
        changements.append(f"echeance {apres['deadline'] or 'retiree'}")
    if apres["priority"] != avant["priority"]:
        changements.append(f"priorite {apres['priority']}")
    if apres["project_id"] != avant["project_id"]:
        changements.append(f"projet {apres.get('project_name') or 'retire'}")
    if apres.get("phase_id") != avant.get("phase_id"):
        libelle = _phase_labels(apres["project_id"]).get(apres["phase_id"]) if apres.get("phase_id") else None
        changements.append(f"etape {libelle or 'retiree'}")
    if bool(apres.get("blocked")) != bool(avant.get("blocked")):
        changements.append("bloquee" if apres.get("blocked") else "debloquee")
    if apres.get("side") != avant.get("side"):
        changements.append(f"qui doit agir {apres.get('side') or 'efface'}")
    if (apres.get("kind") or "task") != (avant.get("kind") or "task"):
        changements.append(f"nature {apres.get('kind') or 'task'}")
    if apres.get("severity") != avant.get("severity") and apres.get("severity"):
        changements.append(f"gravite {apres['severity']}")
    if apres.get("module") != avant.get("module"):
        changements.append(f"module {apres.get('module') or 'retire'}")
    if apres.get("person_id") != avant.get("person_id"):
        changements.append(f"pour {apres.get('person_name') or 'personne'}")
    return changements


def update_task(task_id, progress_pct=None, note=None, status=None, deadline=None,
                priority=None, project=None, phase=None, blocked=None, side=None,
                kind=None, severity=None, module=None, person=None):
    """Note l'avancement d'une tache et ajuste son cadrage.

    Un champ absent (None) reste inchange. Chaque changement de cadrage est
    trace dans le journal de la tache, pour que l'utilisateur voie ce qu'une
    session a modifie.

    :param status: 'done' pour clore, 'todo' pour rouvrir
    :param deadline: echeance AAAA-MM-JJ, chaine vide pour la retirer
    :param priority: 1 critique ... 4 basse
    :param project: nom exact du projet, chaine vide pour le retirer ; la
        tache perd son etape en changeant de projet, sauf etape fournie
    :param phase: etape du projet, par numero ou par nom
    :param blocked: True pour bloquer, False pour debloquer
    :param side: qui doit agir : us, client ou both
    :param kind: 'task', 'anomaly' ou 'deliverable'
    :param severity: gravite d'une anomalie : critical, major, minor
    :param module: module concerne par une anomalie
    :param person: a qui je dois cette tache ; chaine vide pour retirer
    :return: tache a jour
    """
    task = _task_or_error(task_id)
    cadrage = (deadline, priority, project, phase, blocked, side, kind, severity, module, person)
    if progress_pct is None and not note and not status and all(champ is None for champ in cadrage):
        raise AgentError(
            "Rien a mettre a jour : fournir progress_pct, note, status, deadline,"
            " priority, project, phase, blocked, side, kind, severity, module ou person."
        )
    statut = _status(status) if status else None
    if statut and statut not in ("done", "todo"):
        raise AgentError("status attendu : done ou todo.")

    avant = task
    valeurs = _framing_values(task, *cadrage)
    if progress_pct is not None:
        valeurs["progress_pct"] = progress_pct
    try:
        if valeurs:
            task = task_model.update_task(task["id"], valeurs)
    except ValidationError as error:
        raise AgentError(str(error))

    if statut:
        if task["status"] != statut:
            task = task_model.toggle_task(task["id"])
        if statut == "done" and (task.get("progress_pct") or 0) < 100:
            task = task_model.update_task(task["id"], {"progress_pct": 100})

    morceaux = []
    if note and note.strip():
        morceaux.append(note.strip())
    elif progress_pct is not None:
        morceaux.append(f"Avancement : {task['progress_pct']} %.")
    changements = _framing_changes(avant, task)
    if changements:
        morceaux.append("Cadrage : " + ", ".join(changements) + ".")
    if morceaux:
        journal.add_entry(" ".join(morceaux), task_id=task["id"], kind="progress", source=SOURCE)

    return _brief(task_model.get_task(task["id"]))


def log_work(task_id, minutes, summary):
    """Declare du temps passe sur une tache, avec ce qui a ete fait.

    Le temps devient une session terminee : il compte dans le temps suivi,
    le bilan et le calibrage des estimations, comme un chronometre.
    """
    task = _task_or_error(task_id)
    try:
        duree = int(minutes)
    except (TypeError, ValueError):
        raise AgentError("minutes attendu : un nombre entier.")
    if duree < 1 or duree > MAX_WORK_MIN:
        raise AgentError(f"minutes doit rester entre 1 et {MAX_WORK_MIN}.")
    texte = (summary or "").strip()
    if not texte:
        raise AgentError("summary obligatoire : une phrase sur ce qui a ete fait.")

    avertissement = _elapsed_warning(task, duree)
    session_model.record(task["id"], duree, source=SOURCE)
    journal.add_entry(texte, task_id=task["id"], kind="work", minutes=duree, source=SOURCE)
    resultat = _brief(task_model.get_task(task["id"]))
    if avertissement:
        resultat["warning"] = avertissement
    return resultat


def _elapsed_warning(task, minutes, now=None):
    """Avertissement si la duree declaree depasse le temps reellement ecoule.

    Reference : la derniere declaration de temps sur la tache, sinon sa
    creation. Non bloquant : une session qui reprend une tache ancienne, ou
    dont le travail a commence avant la tache, reste libre de sa declaration.

    :return: message, ou None si la duree est plausible
    """
    now = now or datetime.now()
    db = get_db()
    derniere = db.execute(
        "SELECT MAX(ended_at) FROM session WHERE task_id = ? AND ended_at IS NOT NULL", (task["id"],)
    ).fetchone()[0]
    reference = derniere or task.get("created_at")
    try:
        depuis = datetime.strptime(str(reference)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    ecoule = max(0, (now - depuis).total_seconds() / 60)
    plafond = ecoule * ELAPSED_MARGIN_RATIO + ELAPSED_MARGIN_MIN
    if minutes <= plafond:
        return None
    return (
        f"Duree enregistree ({minutes} min) superieure au temps ecoule depuis "
        f"{'la derniere declaration' if derniere else 'la creation de la tache'} (~{round(ecoule)} min)."
        " Declare le temps reellement ecoule a l'horloge, pas une estimation de l'effort ;"
        " corrige si besoin a la prochaine declaration."
    )


def add_record(project, kind, content, follow_up=False):
    """Inscrit une decision, un changement de perimetre ou une dependance au registre.

    :param project: nom exact du projet
    :param kind: 'decision', 'scope' ou 'dependency'
    :param follow_up: ouvre une tache de suivi (perimetre, dependance)
    :return: entree creee et tache de suivi eventuelle
    """
    if not (project or "").strip():
        raise AgentError("project obligatoire : nom exact du projet (list_projects).")
    project_id = _project_id(project)
    nature = _choice(kind or "", journal.REGISTER_KINDS, "kind")
    if not nature:
        raise AgentError("kind obligatoire : decision, scope ou dependency.")
    try:
        entree = project_register.add_record(
            project_id, nature, content, follow_up=bool(follow_up), source=SOURCE
        )
    except (JournalError, ValidationError) as error:
        raise AgentError(str(error))
    return {
        "id": entree["id"],
        "kind": entree["kind"],
        "content": entree["content"],
        "follow_up_task": _brief(entree["task"]) if entree["task"] else None,
    }


def update_phase(project, phase, status=None, planned_date=None, done_date=None, progress_pct=None):
    """Fait avancer une etape du projet : statut, dates, avancement saisi.

    Terminer une etape clot les etapes precedentes restees ouvertes. Le
    changement est trace dans le journal du projet.

    :param project: nom exact du projet
    :param phase: numero ou nom de l'etape
    :param status: 'todo', 'doing', 'blocked' ou 'done'
    :return: etape a jour, avec le nombre d'etapes closes au passage
    """
    if not (project or "").strip():
        raise AgentError("project obligatoire : nom exact du projet (list_projects).")
    project_id = _project_id(project)
    data = {
        cle: valeur
        for cle, valeur in (
            ("status", status), ("planned_date", planned_date),
            ("done_date", done_date), ("progress_pct", progress_pct),
        )
        if valeur is not None
    }
    try:
        return project_board.change_phase(project_id, phase, data, source=SOURCE)
    except ValidationError as error:
        raise AgentError(str(error))


def list_phases(project):
    """Etapes d'un projet, pour savoir ou ranger une tache.

    :param project: nom exact du projet
    :return: {'project', 'current': numero de l'etape en cours, 'phases': [...]}
    """
    if not (project or "").strip():
        raise AgentError("project obligatoire : nom exact du projet (list_projects).")
    project_id = _project_id(project)
    pilotage = project_board.phase_pilot(project_id)
    courante = pilotage["current_phase"]
    return {
        "project": project.strip(),
        "current": next(
            (vue["sequence"] for vue in pilotage["phases"] if courante and vue["id"] == courante["id"]),
            None,
        ),
        "phases": [
            {
                "sequence": vue["sequence"],
                "name": vue["name"],
                "stage": vue["stage"],
                "status": vue["status"],
                "progress_pct": vue["progress_pct"],
                "planned_date": vue["planned_date"],
                "tasks_open": vue["task_total"] - vue["task_done"],
            }
            for vue in pilotage["phases"]
        ],
    }


def list_projects():
    """Projets actifs ou en pause, avec leurs taches ouvertes."""
    return [
        {
            "id": projet["id"],
            "name": projet["name"],
            "status": projet["status"],
            "deadline": projet["deadline"],
            "open_tasks": projet.get("open_count", 0),
        }
        for projet in project_model.list_with_stats(None, space=_space())
        if projet["status"] != "done"
    ]


def _project_brief(project_id):
    """Projet reduit aux champs de list_projects, taches ouvertes comprises."""
    projet = next(p for p in project_model.list_with_stats(None) if p["id"] == project_id)
    return {
        "id": projet["id"],
        "name": projet["name"],
        "status": projet["status"],
        "deadline": projet["deadline"],
        "open_tasks": projet.get("open_count", 0),
    }


def create_project(name, deadline=None):
    """Cree un projet au nom d'une session Claude, sauf s'il existe deja.

    Un projet du meme nom, casse ignoree et quel que soit son statut, est
    renvoye tel quel : la session doit l'utiliser plutot que d'ouvrir un doublon.

    :param name: nom du projet
    :param deadline: echeance AAAA-MM-JJ, facultative
    :return: {'created': True, 'project': projet} ou {'created': False, 'existing': projet}
    :raises AgentError: nom vide ou trop long, echeance mal formee
    """
    echeance = _report_date(deadline, "deadline")
    # Tous les espaces : un nom deja pris de l'autre cote est refuse, pas ignore.
    existant = project_model.find_by_name(name, space=space_model.ALL)
    if existant is not None:
        if _space() and existant["space"] != _space():
            raise AgentError(
                "Un projet de ce nom existe deja dans l'autre espace : choisis un autre nom."
            )
        return {
            "created": False,
            "existing": _project_brief(existant["id"]),
            "hint": "Aucun projet cree : un projet de ce nom existe deja, utilise-le.",
        }
    texte = (name or "").strip()
    try:
        projet = project_model.create_project(
            texte,
            color=project_model.suggest_color(texte),
            deadline=echeance.isoformat() if echeance else None,
            space=_space() or project_model.DEFAULT_SPACE,
        )
    except project_model.ProjectError as error:
        raise AgentError(str(error))
    return {"created": True, "project": _project_brief(projet["id"])}


def _report_date(value, field):
    """Date AAAA-MM-JJ d'une periode de rapport, ou None si absente."""
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise AgentError(f"{field} attendue au format AAAA-MM-JJ.")


def progress_report(manager=None, start=None, end=None):
    """Rapport d'avancement d'un chef de projet, pret a publier.

    Sans chef de projet, retourne la liste des chefs de projet connus : la
    session choisit, puis rappelle.

    :param manager: nom (ou alias) du contact chef de projet
    :param start: debut AAAA-MM-JJ (defaut : il y a six jours)
    :param end: fin AAAA-MM-JJ (defaut : aujourd'hui)
    :return: {'managers': [...]} ou {'report': {...}, 'message': texte WhatsApp}
    """
    if _space() == "perso":
        # Le rapport agrege les projets d'un chef de projet client : il
        # n'existe que cote pro, et l'exposer ici ferait fuir l'espace pro.
        raise AgentError("Le rapport d'avancement ne concerne que l'espace pro.")
    if not (manager or "").strip():
        return {"managers": report_service.list_managers()}

    chef = person_model.find_by_name(manager)
    if chef is None:
        noms = ", ".join(item["name"] for item in report_service.list_managers()) or "aucun"
        raise AgentError(f"Chef de projet « {manager} » introuvable. Chefs de projet connus : {noms}.")

    debut, fin = _report_date(start, "start"), _report_date(end, "end")
    if (debut is None) != (fin is None):
        raise AgentError("start et end vont ensemble : fournir les deux, ou aucun.")
    if debut and fin < debut:
        raise AgentError("end precede start.")

    try:
        rapport = report_service.build(chef["id"], debut, fin)
    except report_service.ReportError as error:
        raise AgentError(str(error))
    return {"report": rapport, "message": report_service.as_whatsapp(rapport)}


def set_report_url(manager, url):
    """Retient le lien du tableau de bord publie pour un chef de projet.

    Le lien est ensuite ajoute au message WhatsApp du rapport.

    :param manager: nom (ou alias) du contact chef de projet
    :param url: lien https du tableau de bord, chaine vide pour l'effacer
    :return: {'manager', 'report_url'}
    """
    chef = person_model.find_by_name(manager)
    if chef is None:
        raise AgentError(f"Chef de projet « {manager} » introuvable.")
    try:
        chef = person_model.update_person(chef["id"], {"report_url": url})
    except person_model.PersonError as error:
        raise AgentError(str(error))
    return {"manager": chef["name"], "report_url": chef["report_url"]}


# ---------------------------------------------------------------- Notes et comptes rendus
#
# Une session Claude ecrit une note ou un compte rendu seulement a la demande
# de l'utilisateur (consigne du serveur MCP). La suppression reste dans
# l'interface : elle est irreversible.

NOTE_FIELDS = ("title", "content", "category", "odoo_version", "module", "tags", "pinned")
MINUTES_FIELDS = ("title", "kind", "held_on", "attendees", "objectives", "discussed", "topics", "followups", "notes")
ITEM_FIELDS = ("type", "text", "owner", "due")

# Elements ajoutes en un appel : au-dela, la session recopie un document au
# lieu d'en tirer les decisions et les actions.
MAX_MINUTES_ITEMS = 50


def _record_id(value, field):
    """Identifiant entier, ou AgentError lisible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AgentError(f"Identifiant attendu ({field}).")


def _with_project(data, fields):
    """Champs fournis parmi ceux autorises ; le projet (nom) devient project_id, chaine vide le retire."""
    values = {key: data[key] for key in fields if data.get(key) is not None}
    if data.get("project") is not None:
        values["project_id"] = _project_id(data["project"])
    return values


def _note_brief(note, with_content=False):
    """Note reduite aux champs utiles ; le contenu seulement a la lecture."""
    brief = {
        "id": note["id"],
        "title": note["title"],
        "category": note["category"],
        "project": note.get("project_name"),
        "odoo_version": note.get("odoo_version") or None,
        "module": note.get("module") or None,
        "tags": note.get("tags") or [],
        "pinned": bool(note.get("pinned")),
        "source_task_id": note.get("source_task_id"),
        "updated_at": note.get("updated_at"),
    }
    if with_content:
        brief["content"] = note.get("content") or ""
    return brief


def _note_or_error(note_id):
    """Retourne la note, ou leve une AgentError lisible."""
    note = knowledge.get(_record_id(note_id, "note_id"))
    # Une note de l'autre espace est traitee comme absente (meme regle que les taches).
    if note is None or not space_model.visible(note.get("space"), _space()):
        raise AgentError(f"Note {note_id} introuvable : nomaia_search la retrouve par ses mots.")
    return note


def get_note(note_id):
    """Note complete, contenu Markdown compris."""
    return _note_brief(_note_or_error(note_id), with_content=True)


def create_note(data):
    """Cree une note de la base de connaissances.

    :param data: title (obligatoire), content, category, project (nom), odoo_version, module, tags, pinned
    :return: {'created': note sans son contenu}
    """
    try:
        note = knowledge.create(_with_project(data, NOTE_FIELDS))
    except KnowledgeError as error:
        raise AgentError(str(error))
    return {"created": _note_brief(note)}


def update_note(note_id, data, append=False):
    """Modifie les champs fournis d'une note ; les autres restent inchanges.

    :param append: ajoute content a la fin du contenu actuel au lieu de le remplacer
    :return: {'updated': note sans son contenu}
    """
    note = _note_or_error(note_id)
    values = _with_project(data, NOTE_FIELDS)
    if append and values.get("content"):
        values["content"] = f"{(note['content'] or '').rstrip()}\n\n{values['content'].strip()}".lstrip()
    if not values:
        raise AgentError("Rien a modifier : fournir au moins un champ (title, content, category, project...).")
    try:
        note = knowledge.update(note["id"], values)
    except KnowledgeError as error:
        raise AgentError(str(error))
    return {"updated": _note_brief(note)}


def _minutes_brief(minutes):
    """Compte rendu et ses elements, reduits aux champs utiles."""
    brief = {key: minutes.get(key) for key in ("id", "status", *MINUTES_FIELDS)}
    brief["project"] = minutes.get("project_name")
    brief["items"] = [
        {
            "id": item["id"], "type": item["type"], "text": item["text"], "owner": item.get("owner") or None,
            "due": item.get("due"), "task_id": item.get("task_id"), "task_status": item.get("task_status"),
            "registered": bool(item.get("note_id")),
        }
        for item in minutes.get("items") or []
    ]
    return brief


def _minutes_or_error(minutes_id):
    """Retourne le compte rendu, ou leve une AgentError lisible."""
    minutes = minutes_model.get(_record_id(minutes_id, "minutes_id"))
    if minutes is None or not space_model.visible(minutes.get("space"), _space()):
        raise AgentError(f"Compte rendu {minutes_id} introuvable : nomaia_search le retrouve par ses mots.")
    return minutes


def _check_secrets(values):
    """Refuse un secret en clair : la route de l'interface le controle, le modele non."""
    try:
        secrets_guard.check({minutes_model.FIELD_LABELS.get(key, key): value
                             for key, value in values.items() if isinstance(value, str)})
    except SecretDetected as error:
        raise AgentError(str(error))


def _items(items):
    """Elements a ajouter, tous controles avant la moindre ecriture."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise AgentError("items attendu : liste d'objets {type, text, owner, due}.")
    if len(items) > MAX_MINUTES_ITEMS:
        raise AgentError(f"{MAX_MINUTES_ITEMS} elements au plus par appel.")
    cleaned = []
    for position, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise AgentError(f"Element {position} : objet {{type, text, owner, due}} attendu.")
        values = {key: item[key] for key in ITEM_FIELDS if item.get(key) is not None}
        _check_secrets(values)
        try:
            minutes_model.validate_item(values)
        except MinutesError as error:
            raise AgentError(f"Element {position} : {error}")
        cleaned.append(values)
    return cleaned


def _apply_status(minutes_id, status):
    """Valide (final) ou rouvre (draft) le compte rendu ; renvoie son etat et ce qui reste a convertir."""
    if status == "final":
        result = minutes_service.finalize(minutes_id)
        return {
            "minutes": _minutes_brief(result["minutes"]),
            "pending_actions": [item["text"] for item in result["pending_actions"]],
            "pending_decisions": [item["text"] for item in result["pending_decisions"]],
        }
    if status == "draft":
        minutes_model.update(minutes_id, {"status": "draft"})
    return {"minutes": _minutes_brief(minutes_model.get(minutes_id))}


def get_minutes(minutes_id):
    """Compte rendu complet : sections et elements (decisions, actions, demandes)."""
    return _minutes_brief(_minutes_or_error(minutes_id))


def create_minutes(data):
    """Cree un compte rendu de reunion, d'intervention ou de formation, elements compris.

    :param data: title (obligatoire), held_on (defaut aujourd'hui), kind, project (nom), sections,
                 items [{type, text, owner, due}], status ('final' pour le valider d'emblee)
    :return: {'minutes'} et, pour un compte rendu valide, 'pending_actions' et 'pending_decisions'
    """
    values = _with_project(data, MINUTES_FIELDS)
    values.setdefault("held_on", date.today().isoformat())
    status = _choice(data.get("status") or "", minutes_model.STATUSES, "status")
    items = _items(data.get("items"))
    _check_secrets(values)
    try:
        minutes = minutes_model.create(values)
        for item in items:
            minutes_model.add_item(minutes["id"], item)
    except MinutesError as error:
        raise AgentError(str(error))
    return _apply_status(minutes["id"], status)


def update_minutes(minutes_id, data):
    """Modifie les sections fournies, ajoute des elements, valide ou rouvre le compte rendu.

    Une section fournie remplace l'ancienne ; les champs omis restent inchanges.

    :param data: champs de create_minutes, add_items a la place de items
    :return: meme forme que create_minutes
    """
    minutes = _minutes_or_error(minutes_id)
    values = _with_project(data, MINUTES_FIELDS)
    status = _choice(data.get("status") or "", minutes_model.STATUSES, "status")
    items = _items(data.get("add_items"))
    if not (values or items or status):
        raise AgentError("Rien a modifier : fournir une section, add_items ou status.")
    _check_secrets(values)
    try:
        if values:
            minutes_model.update(minutes["id"], values)
        for item in items:
            minutes_model.add_item(minutes["id"], item)
    except MinutesError as error:
        raise AgentError(str(error))
    return _apply_status(minutes["id"], status)


# ---------------------------------------------------------------- Contacts et attentes
#
# Une demande d'un client ou d'un collegue devient une tache « pour » lui
# (person) ; ce qu'on attend de quelqu'un devient une attente, que les
# relances automatiques suivent. Le carnet de contacts se complete au fil du
# travail, sans doublon : un nom deja connu (ou un alias) met a jour la fiche.

WAITING_ALIASES = {
    "recu": "received", "recue": "received", "received": "received", "done": "received", "ok": "received",
    "abandonne": "dropped", "abandonnee": "dropped", "dropped": "dropped", "cancel": "dropped",
    "cancelled": "dropped", "annule": "dropped", "annulee": "dropped",
    "ouverte": "open", "open": "open", "en_attente": "open", "all": "all", "tout": "all", "toutes": "all",
}


def _find_person(name):
    """Contact designe par son nom : exact ou alias, sinon nom partiel s'il est unique.

    « Hugo » retrouve « Hugo Lambert » s'il est le seul a porter ce nom ;
    s'ils sont plusieurs, l'erreur liste les candidats.
    """
    texte = (name or "").strip()
    if not texte:
        return None
    exact = person_model.find_by_name(texte)
    if exact:
        return exact
    cherche = f" {_fold(texte)} "
    proches = [
        personne for personne in person_model.list_people()
        if any(cherche in f" {_fold(nom)} " for nom in [personne["name"], *personne["aliases"]])
    ]
    if len(proches) > 1:
        noms = ", ".join(personne["name"] for personne in proches)
        raise AgentError(f"Contact '{texte}' ambigu : {noms}. Preciser le nom.")
    return proches[0] if proches else None


def _person_id(name, project_id=None):
    """Identifiant du contact, cree s'il est inconnu ; rattache au projet fourni (en plus des siens)."""
    personne = _find_person(name)
    if personne is not None and project_id:
        person_model.add_project(personne["id"], project_id)
    if personne is None:
        try:
            personne = person_model.create_person({"name": str(name).strip(), "project_id": project_id})
        except person_model.PersonError as error:
            raise AgentError(str(error))
    return personne["id"]


def _person_brief(personne):
    """Contact reduit aux champs utiles a une session Claude."""
    return {
        "id": personne["id"],
        "name": personne["name"],
        "role": personne.get("role"),
        "project": personne.get("project_name"),
        "projects": [projet["name"] for projet in personne.get("projects") or []],
        "email": personne.get("email"),
        "phone": personne.get("phone"),
        "aliases": personne.get("aliases") or [],
        "i_owe": personne.get("owed_count", 0),
        "they_owe": personne.get("waiting_count", 0),
    }


def _waiting_brief(attente):
    """Attente reduite aux champs utiles a une session Claude."""
    return {key: attente.get(key) for key in (
        "id", "person_name", "subject", "since", "due", "status", "task_id", "task_title", "days_open", "overdue",
    )}


def list_people(query=None, project=None):
    """Contacts, avec ce que je leur dois (taches) et ce qu'ils me doivent (attentes).

    :param query: mots a retrouver dans le nom, un alias, le role ou un projet
    :param project: ne garde que les contacts rattaches a ce projet
    """
    personnes = person_model.list_people(project_id=_project_id(project) if project else None)
    cherche = _fold(query)
    if cherche:
        personnes = [
            personne for personne in personnes
            if all(mot in _fold(" ".join([personne["name"], *personne["aliases"], personne.get("role") or "",
                                          *[projet["name"] for projet in personne["projects"]]]))
                   for mot in cherche.split())
        ]
    return {"people": [_person_brief(personne) for personne in personnes]}


def save_person(name, role=None, project=None, email=None, phone=None, notes=None, new_name=None,
                remove_project=None):
    """Cree un contact ou complete sa fiche s'il existe deja (nom ou alias).

    Seuls les champs fournis changent ; les notes s'ajoutent a celles de la
    fiche au lieu de les remplacer. new_name renomme (l'ancien nom reste un alias).
    Un contact peut suivre plusieurs projets : project en ajoute un,
    remove_project en retire un.

    :return: {'person', 'created'}
    """
    if not (name or "").strip():
        raise AgentError("name obligatoire : nom du contact.")
    data = {key: value for key, value in (("role", role), ("email", email), ("phone", phone)) if value is not None}
    ajout = _project_id(project) if project else None
    retrait = _project_id(remove_project) if remove_project else None
    if new_name is not None:
        data["name"] = new_name
    existant = _find_person(name)
    try:
        if existant is None:
            data.setdefault("name", name)
            data["notes"] = notes
            personne, cree = person_model.create_person(data), True
        else:
            if notes and notes.strip():
                anciennes = (existant.get("notes") or "").strip()
                data["notes"] = (anciennes + "\n" + notes.strip()).strip()
            personne, cree = person_model.update_person(existant["id"], data), False
        if ajout:
            person_model.add_project(personne["id"], ajout)
        if retrait:
            person_model.remove_project(personne["id"], retrait)
    except person_model.PersonError as error:
        raise AgentError(str(error))
    # Relue dans la liste : elle porte les compteurs (ce que je dois, ce qu'on me doit).
    personne = next((p for p in person_model.list_people() if p["id"] == personne["id"]), personne)
    return {"person": _person_brief(personne), "created": cree}


def _waiting_status(value, allow_all=False):
    """Statut d'attente normalise (recu, abandonne... toleres)."""
    texte = _fold(value).replace(" ", "_")
    statut = WAITING_ALIASES.get(texte, texte)
    valides = (*person_model.VALID_STATUS, "all") if allow_all else person_model.VALID_STATUS
    if statut not in valides:
        raise AgentError(f"status attendu : {', '.join(valides)}.")
    return statut


def list_waiting(person=None, status="open"):
    """Ce que j'attends des autres, la plus ancienne d'abord."""
    statut = _waiting_status(status or "open", allow_all=True)
    person_id = None
    if person:
        personne = _find_person(person)
        if personne is None:
            raise AgentError(f"Contact '{person}' inconnu : nomaia_list_people donne les noms.")
        person_id = personne["id"]
    try:
        attentes = person_model.list_waiting(status=None if statut == "all" else statut, person_id=person_id)
    except person_model.PersonError as error:
        raise AgentError(str(error))
    return {"waiting": [_waiting_brief(attente) for attente in attentes]}


def add_waiting(person, subject, due=None, task_id=None, project=None):
    """Enregistre ce que quelqu'un me doit ; les relances automatiques le suivent.

    Le contact est cree s'il est inconnu. Une attente ouverte au meme sujet
    pour la meme personne est renvoyee telle quelle plutot que doublee.

    :return: {'waiting', 'created'}
    """
    if not (person or "").strip():
        raise AgentError("person obligatoire : qui doit quelque chose.")
    if not (subject or "").strip():
        raise AgentError("subject obligatoire : ce qui est attendu.")
    if task_id is not None:
        _task_or_error(task_id)
    person_id = _person_id(person, _project_id(project) if project else None)
    sujet = _fold(subject)
    for attente in person_model.list_waiting(status="open", person_id=person_id):
        if _fold(attente["subject"]) == sujet:
            return {"waiting": _waiting_brief(attente), "created": False}
    try:
        attente = person_model.create_waiting(
            {"person_id": person_id, "subject": subject, "due": due, "task_id": task_id}
        )
    except person_model.PersonError as error:
        raise AgentError(str(error))
    return {"waiting": _waiting_brief(attente), "created": True}


def resolve_waiting(waiting_id, status="received"):
    """Clot une attente : recue (received) ou abandonnee (dropped)."""
    attente = person_model.get_waiting(_record_id(waiting_id, "waiting_id"))
    if attente is None:
        raise AgentError(f"Attente {waiting_id} introuvable : nomaia_list_waiting donne les identifiants.")
    statut = _waiting_status(status or "received")
    if statut == "open":
        raise AgentError("status attendu : received ou dropped.")
    attente = person_model.update_waiting(attente["id"], {"status": statut})
    return {"waiting": _waiting_brief(attente)}
