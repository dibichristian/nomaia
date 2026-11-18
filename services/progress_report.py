"""Rapport d'avancement destine a un chef de projet : tous ses projets, une periode.

Le rapport sort de la base, pas de l'IA : chaque chiffre est calcule, aucun
n'est redige. Il alimente l'apercu de la page Bilan, le message WhatsApp et le
tableau de bord publie sur claude.ai (outil MCP nomaia_progress_report).

Il ne montre que ce qui regarde le projet. Restent dehors : descriptions des
taches, temps chronometre, journal de travail et commentaires internes,
taches sans projet. Le chef de projet lit un etat d'avancement, pas le
carnet de bord de l'utilisateur.
"""
from datetime import date, datetime, timedelta

from models import journal
from models import person as person_model
from models import project as project_model
from models import task as task_model
from models.task import VALID_SEVERITY
from services import project_board, project_register

# Periode par defaut : les sept derniers jours, aujourd'hui compris. Un point
# envoye un lundi matin couvre ainsi la semaine ecoulee, et non un lundi vide.
DEFAULT_DAYS = 7

# Horizon de la rubrique « a venir », en jours a partir d'aujourd'hui.
UPCOMING_DAYS = 14

# Plafonds par rubrique : au-dela, le rapport ne se lit plus d'un coup d'oeil.
MAX_DONE = 15
MAX_UPCOMING = 10
MAX_ANOMALIES = 10
MAX_REGISTER = 8
MAX_REGISTER_CHARS = 240

# Au-dela, le message WhatsApp resume les etapes terminees au lieu de les citer.
MAX_PHASES_NAMED = 3

SEVERITY_LABELS = {"critical": "critique", "major": "majeure", "minor": "mineure"}


class ReportError(ValueError):
    """Rapport impossible : chef de projet introuvable ou sans projet."""


def default_bounds(today=None):
    """Les sept derniers jours, aujourd'hui compris."""
    today = today or date.today()
    return today - timedelta(days=DEFAULT_DAYS - 1), today


def list_managers():
    """Chefs de projet : contacts qui suivent au moins un projet non termine.

    :return: liste de {id, name, role, phone, report_url, projects: [noms]}
    """
    chefs = []
    for personne in person_model.list_people():
        projets = project_model.list_by_manager(personne["id"])
        if projets:
            chefs.append(
                {
                    "id": personne["id"],
                    "name": personne["name"],
                    "role": personne["role"],
                    "phone": personne["phone"],
                    "report_url": personne["report_url"],
                    "projects": [projet["name"] for projet in projets],
                }
            )
    return chefs


def _in_period(value, start_iso, end_iso):
    """Vrai si la date (ou le moment) ISO tombe dans la periode."""
    return bool(value) and start_iso <= value[:10] <= end_iso


def _title(task, parents):
    """Titre d'une tache, precede de celui de sa tache parente pour une etape."""
    parent = parents.get(task.get("parent_id"))
    return f"{parent['title']} › {task['title']}" if parent else task["title"]


def _split_ids(tasks):
    """Identifiants des taches decoupees en etapes : leurs etapes parlent pour elles."""
    return {task["parent_id"] for task in tasks if task.get("parent_id")}


def _done_rows(tasks, parents, start_iso, end_iso):
    """Taches ordinaires terminees sur la periode, la plus recente d'abord."""
    decoupees = _split_ids(tasks)
    lignes = [
        {"title": _title(task, parents), "done_at": task["done_at"][:10]}
        for task in tasks
        if task["status"] == "done"
        and (task.get("kind") or "task") == "task"
        and task["id"] not in decoupees
        and _in_period(task["done_at"], start_iso, end_iso)
    ]
    lignes.sort(key=lambda ligne: ligne["done_at"], reverse=True)
    return lignes


def _upcoming_rows(tasks, parents, today):
    """Taches ordinaires ouvertes en retard, bloquees ou dues sous UPCOMING_DAYS jours."""
    jour = today.isoformat()
    limite = (today + timedelta(days=UPCOMING_DAYS)).isoformat()
    decoupees = _split_ids(tasks)
    lignes = []
    for task in tasks:
        if task["status"] == "done" or task["id"] in decoupees:
            continue
        if (task.get("kind") or "task") != "task":
            continue
        if not (task.get("blocked") or (task["deadline"] and task["deadline"] <= limite)):
            continue
        lignes.append(
            {
                "title": _title(task, parents),
                "deadline": task["deadline"],
                "state": project_board.task_state(task, jour),
                "side": task.get("side") or "us",
                "progress_pct": task.get("progress_pct") or 0,
            }
        )
    lignes.sort(key=lambda ligne: (ligne["deadline"] or "9999", ligne["title"]))
    return lignes


def _anomalies(tasks, today, start_iso, end_iso):
    """Anomalies ouvertes (les plus graves d'abord) et mouvements de la periode."""
    jour = today.isoformat()
    rang = {severity: index for index, severity in enumerate(VALID_SEVERITY)}
    anomalies = [task for task in tasks if task.get("kind") == "anomaly"]
    ouvertes = [task for task in anomalies if task["status"] != "done"]
    ouvertes.sort(
        key=lambda task: (rang.get(task.get("severity"), len(rang)), task["created_at"])
    )
    comptes = {severity: 0 for severity in VALID_SEVERITY}
    for task in ouvertes:
        comptes[task.get("severity") or task_model.DEFAULT_SEVERITY] += 1

    return {
        "open_count": len(ouvertes),
        "by_severity": comptes,
        "opened_in_period": sum(
            1 for task in anomalies if _in_period(task["created_at"], start_iso, end_iso)
        ),
        "fixed_in_period": sum(
            1 for task in anomalies
            if task["status"] == "done" and _in_period(task["done_at"], start_iso, end_iso)
        ),
        "open": [
            {
                "title": task["title"],
                "severity": task.get("severity") or task_model.DEFAULT_SEVERITY,
                "module": task.get("module"),
                "state": project_board.task_state(task, jour),
                "reported_on": task["created_at"][:10],
            }
            for task in ouvertes[:MAX_ANOMALIES]
        ],
    }


def _deliverables(tasks, today, start_iso, end_iso):
    """Livrables attendus du client (par echeance) et recus sur la periode."""
    jour = today.isoformat()
    livrables = [task for task in tasks if task.get("kind") == "deliverable"]
    attendus = sorted(
        (task for task in livrables if task["status"] != "done"),
        key=lambda task: task["deadline"] or "9999",
    )
    return {
        "awaited": [
            {
                "title": task["title"],
                "deadline": task["deadline"],
                "late": bool(task["deadline"] and task["deadline"] < jour),
            }
            for task in attendus
        ],
        "received": [
            {"title": task["title"], "done_at": task["done_at"][:10]}
            for task in livrables
            if task["status"] == "done" and _in_period(task["done_at"], start_iso, end_iso)
        ],
    }


def _register_rows(project_id, start_iso, end_iso):
    """Decisions, changements de perimetre et dependances inscrits sur la periode."""
    entrees = [
        entree
        for entree in project_register.register_view(journal.list_register(project_id))
        if _in_period(entree["created_at"], start_iso, end_iso)
    ]
    return [
        {
            "kind": entree["kind"],
            "content": entree["content"][:MAX_REGISTER_CHARS],
            "status": entree["status"],
            "date": entree["created_at"][:10],
        }
        for entree in entrees[:MAX_REGISTER]
    ]


def _phase_rows(card):
    """Frise des etapes : ce qu'il faut pour la dessiner, rien de plus."""
    return [
        {
            "sequence": phase["sequence"],
            "name": phase["name"],
            "status": phase["status"],
            "progress_pct": phase["progress_pct"],
            "planned_date": phase["planned_date"],
            "done_date": phase["done_date"],
        }
        for phase in card["phases"]
    ]


def _project_section(card, today, start_iso, end_iso):
    """Section du rapport pour un projet."""
    tasks = task_model.list_tasks(project_id=card["id"])
    parents = {task["id"]: task for task in tasks}
    phases = _phase_rows(card)
    return {
        "id": card["id"],
        "name": card["name"],
        "color": card["color"],
        "status": card["status"],
        "weather": card["weather"],
        "weather_note": card["weather_note"],
        "progress_pct": card["progress_pct"],
        "deadline": card["deadline"],
        "go_live": card["go_live"],
        "current_phase": card["current_phase"],
        "next_milestone": card["next_milestone"],
        "last_milestone": card["last_milestone"],
        "phases": phases,
        "phases_done": [
            {"name": phase["name"], "done_date": phase["done_date"]}
            for phase in phases
            if phase["status"] == "done" and _in_period(phase["done_date"], start_iso, end_iso)
        ],
        "task_states": card["task_states"],
        "done": _done_rows(tasks, parents, start_iso, end_iso)[:MAX_DONE],
        "upcoming": _upcoming_rows(tasks, parents, today)[:MAX_UPCOMING],
        "waiting_client": card["waiting_client"],
        "anomalies": _anomalies(tasks, today, start_iso, end_iso),
        "deliverables": _deliverables(tasks, today, start_iso, end_iso),
        "register": _register_rows(card["id"], start_iso, end_iso),
        "attention": [signal["label"] for signal in card["signals"]],
    }


def _summary(sections):
    """Indicateurs de tete du rapport, tous projets confondus."""
    total = len(sections)
    return {
        "projects": total,
        "avg_progress": round(sum(s["progress_pct"] for s in sections) / total) if total else 0,
        "at_risk": sum(1 for s in sections if s["attention"]),
        "tasks_done": sum(len(s["done"]) for s in sections),
        "phases_done": sum(len(s["phases_done"]) for s in sections),
        "anomalies_open": sum(s["anomalies"]["open_count"] for s in sections),
        "anomalies_critical": sum(s["anomalies"]["by_severity"]["critical"] for s in sections),
        "anomalies_fixed": sum(s["anomalies"]["fixed_in_period"] for s in sections),
        "deliverables_awaited": sum(len(s["deliverables"]["awaited"]) for s in sections),
        "deliverables_late": sum(
            1 for s in sections for item in s["deliverables"]["awaited"] if item["late"]
        ),
    }


def build(manager_id, start_date=None, end_date=None, today=None):
    """Rapport d'avancement de tous les projets non termines d'un chef de projet.

    :param manager_id: identifiant du contact chef de projet
    :param start_date: debut de periode (defaut : il y a six jours)
    :param end_date: fin de periode (defaut : aujourd'hui)
    :return: {'manager', 'period_start', 'period_end', 'generated_at',
        'summary', 'projects'}
    :raises ReportError: contact introuvable ou sans projet suivi
    """
    today = today or date.today()
    if start_date is None or end_date is None:
        start_date, end_date = default_bounds(today)

    chef = person_model.get_person(manager_id)
    if chef is None:
        raise ReportError("Chef de projet introuvable.")
    ids = {projet["id"] for projet in project_model.list_by_manager(chef["id"])}
    if not ids:
        raise ReportError(
            f"{chef['name']} ne suit aucun projet en cours : choisis-le comme chef de projet"
            " dans la fiche d'un projet (Modifier)."
        )

    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()
    sections = [
        _project_section(carte, today, start_iso, end_iso)
        for carte in project_board.overview(today)["projects"]
        if carte["id"] in ids
    ]
    return {
        "manager": {
            "id": chef["id"],
            "name": chef["name"],
            "role": chef["role"],
            "phone": chef["phone"],
            "report_url": chef["report_url"],
        },
        "period_start": start_iso,
        "period_end": end_iso,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "summary": _summary(sections),
        "projects": sections,
    }


def _short_date(iso):
    """AAAA-MM-JJ en JJ/MM."""
    return f"{iso[8:10]}/{iso[5:7]}" if iso else ""


def _project_message(section):
    """Bloc WhatsApp d'un projet : avancement, faits de la periode, points d'attention."""
    lignes = [f"*{section['name']}* — {section['progress_pct']} %"]

    etat = []
    if section["current_phase"]:
        etat.append(f"étape en cours : {section['current_phase']['name']}")
    jalon = section["next_milestone"]
    if jalon and jalon["date"]:
        etat.append(f"prochain jalon « {jalon['name']} » le {_short_date(jalon['date'])}")
    if etat:
        # Majuscule initiale seulement : capitalize() mettrait le nom de l'etape en minuscules.
        texte = ", ".join(etat)
        lignes.append(texte[0].upper() + texte[1:])

    faits = []
    etapes = section["phases_done"]
    if len(etapes) > MAX_PHASES_NAMED:
        faits.append(f"{len(etapes)} étapes terminées, jusqu'à « {etapes[-1]['name']} »")
    elif etapes:
        faits.append("étape(s) terminée(s) : " + ", ".join(phase["name"] for phase in etapes))
    if section["done"]:
        faits.append(f"{len(section['done'])} tâche(s) terminée(s)")
    if section["anomalies"]["fixed_in_period"]:
        faits.append(f"{section['anomalies']['fixed_in_period']} anomalie(s) corrigée(s)")
    if section["deliverables"]["received"]:
        faits.append(f"{len(section['deliverables']['received'])} livrable(s) reçu(s)")
    lignes.append("Fait : " + ("; ".join(faits) if faits else "pas d'avancée notable sur la période"))

    attention = []
    comptes = section["anomalies"]["by_severity"]
    if section["anomalies"]["open_count"]:
        detail = ", ".join(
            f"{comptes[severity]} {SEVERITY_LABELS[severity]}(s)"
            for severity in VALID_SEVERITY
            if comptes[severity]
        )
        attention.append(f"{section['anomalies']['open_count']} anomalie(s) ouverte(s) ({detail})")
    attendus = section["deliverables"]["awaited"]
    if attendus:
        en_retard = sum(1 for item in attendus if item["late"])
        attention.append(
            f"{len(attendus)} livrable(s) client attendu(s)"
            + (f", dont {en_retard} en retard" if en_retard else "")
        )
    attention += [label for label in section["attention"] if "anomalie" not in label]
    if attention:
        lignes.append("Attention : " + "; ".join(attention))
    return "\n".join(lignes)


def as_whatsapp(report):
    """Message WhatsApp court : un bloc par projet et le lien du tableau de bord.

    Mise en forme WhatsApp : *gras*. Le texte est relu par l'utilisateur avant
    envoi, il n'est jamais envoye automatiquement.
    """
    prenom = report["manager"]["name"].split()[0]
    blocs = [
        f"Bonjour {prenom},",
        f"Point d'avancement du {_short_date(report['period_start'])}"
        f" au {_short_date(report['period_end'])} :",
    ]
    blocs += [_project_message(section) for section in report["projects"]]
    if report["manager"]["report_url"]:
        blocs.append(f"Tableau de bord détaillé : {report['manager']['report_url']}")
    return "\n\n".join(blocs)
