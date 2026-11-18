"""Tableau de bord des projets : vue d'ensemble et fiche d'un projet.

Les chiffres viennent de models.project.board_rows (une requete pour tous les
projets) ; ce module en tire l'avancement, le reste a faire et les signaux
d'alerte, avec des regles simples et lisibles plutot qu'un score opaque.
"""
from datetime import date, datetime, timedelta

from models import event as event_model
from models import journal
from models import person as person_model
from models import phase as phase_model
from models import project as project_model
from models import setting, stats
from models import task as task_model
from models.task import VALID_SEVERITY
from services import project_register

# Echeance consideree comme proche, en jours.
DEADLINE_SOON_DAYS = 7

# En dessous de cet avancement, une echeance proche devient un signal.
SOON_PROGRESS_PCT = 70

# Taches terminees affichees dans la fiche d'un projet.
DONE_SHOWN = 20

# Semaines d'activite affichees dans la fiche d'un projet.
ACTIVITY_WEEKS = 6

# Horizon des rendez-vous a venir dans la fiche d'un projet, en jours.
EVENTS_DAYS = 30

# Au-dela de cette attente, une action du client devient un signal (jours).
CLIENT_WAIT_DAYS = 14

# Statuts affiches d'une tache, dans l'ordre des legendes.
TASK_STATES = ("done", "doing", "todo", "late", "blocked")


def _week_bounds(today):
    """Lundi et dimanche de la semaine de today, en ISO."""
    lundi = today - timedelta(days=today.weekday())
    return lundi.isoformat(), (lundi + timedelta(days=6)).isoformat()


def _progress(row):
    """Avancement pondere par les estimations, en pourcentage entier."""
    if row["est_total"]:
        return min(100, round(row["est_done"] / row["est_total"] * 100))
    return 100 if row["done_count"] and not row["open_count"] else 0


# --- Pilotage par etapes ------------------------------------------------------


def task_state(task, today_iso):
    """Statut affiche d'une tache, derive de ses champs.

    Le statut stocke reste 'todo' / 'done' (planificateur, Telegram, MCP en
    dependent) ; le pilotage a besoin d'un cran de plus. Ordre de priorite :
    terminee, bloquee, en retard (echeance passee), en cours (avancement
    declare ou temps chronometre), a faire.

    :param task: dictionnaire portant status, blocked, deadline, progress_pct,
        tracked_min
    :param today_iso: date du jour au format ISO
    :return: une valeur de TASK_STATES
    """
    if task["status"] == "done":
        return "done"
    if task.get("blocked"):
        return "blocked"
    if task.get("deadline") and task["deadline"] < today_iso:
        return "late"
    if task.get("progress_pct") or task.get("tracked_min"):
        return "doing"
    return "todo"


def _state_counts(tasks, today_iso):
    """Nombre de taches par statut affiche."""
    counts = {state: 0 for state in TASK_STATES}
    for task in tasks:
        counts[task_state(task, today_iso)] += 1
    return counts


def _quality_counts(tasks):
    """Anomalies ouvertes par gravite et livrables attendus ou recus."""
    anomalies = {severity: 0 for severity in VALID_SEVERITY}
    anomalies["fixed"] = 0
    livrables = {"awaited": 0, "received": 0}
    for task in tasks:
        kind = task.get("kind")
        if kind == "anomaly":
            if task["status"] == "done":
                anomalies["fixed"] += 1
            else:
                anomalies[task.get("severity") or task_model.DEFAULT_SEVERITY] += 1
        elif kind == "deliverable":
            livrables["received" if task["status"] == "done" else "awaited"] += 1
    anomalies["open"] = sum(anomalies[severity] for severity in VALID_SEVERITY)
    return anomalies, livrables


def _tasks_progress(tasks):
    """Avancement pondere par les estimations d'un lot de taches, ou None si vide."""
    total = sum(task["estimate_min"] for task in tasks)
    if not total:
        return None
    fait = sum(
        task["estimate_min"] if task["status"] == "done"
        else task["estimate_min"] * (task["progress_pct"] or 0) / 100
        for task in tasks
    )
    return min(100, round(fait / total * 100))


def _phase_view(phase, tasks, today):
    """Etape enrichie : avancement effectif, statut effectif, compteurs.

    L'avancement vient des taches de l'etape quand elle en a, de la saisie
    sinon ; une etape terminee vaut 100 %. Une etape « a venir » deja
    entamee passe « en cours ».
    """
    avancement = _tasks_progress(tasks)
    if phase["status"] == "done":
        avancement = 100
    elif avancement is None:
        avancement = phase["progress_pct"]

    statut = phase["status"]
    if statut == "todo" and avancement > 0:
        statut = "doing"

    jours = None
    if phase["planned_date"] and statut != "done":
        jours = (date.fromisoformat(phase["planned_date"]) - today).days

    return {
        "id": phase["id"],
        "name": phase["name"],
        "stage": phase["stage"],
        "sequence": phase["sequence"],
        "weight": phase["weight"],
        "status": statut,
        "stored_status": phase["status"],
        "progress_pct": avancement,
        "manual_progress": phase["progress_pct"],
        "has_tasks": bool(tasks),
        "planned_date": phase["planned_date"],
        "done_date": phase["done_date"],
        "days_left": jours,
        "task_total": len(tasks),
        "task_done": sum(1 for task in tasks if task["status"] == "done"),
        "states": _state_counts(tasks, today.isoformat()),
    }


def _phases_progress(phases):
    """Avancement global : somme des poids multiplies par l'avancement des etapes."""
    poids = sum(phase["weight"] for phase in phases)
    if poids:
        return round(sum(phase["weight"] * phase["progress_pct"] for phase in phases) / poids)
    return round(sum(phase["progress_pct"] for phase in phases) / len(phases))


def _milestone(phase, date_key):
    """Etape reduite a un jalon : nom, date, delai."""
    if phase is None:
        return None
    return {
        "id": phase["id"],
        "name": phase["name"],
        "date": phase[date_key],
        "days_left": phase["days_left"] if date_key == "planned_date" else None,
        "status": phase["status"],
        "progress_pct": phase["progress_pct"],
    }


def _pilot(phases, tasks, today):
    """Lecture d'un projet par ses etapes (regles de pilotage).

    - etape en cours : premiere etape en cours ou bloquee, a defaut la
      premiere a venir ;
    - dernier jalon atteint : derniere etape terminee ;
    - prochain jalon : premiere etape non terminee, avec sa date prevue.

    :param phases: etapes du projet dans l'ordre
    :param tasks: taches feuilles du projet (phase_model.board_tasks)
    :return: dictionnaire de pilotage
    """
    jour = today.isoformat()
    par_etape = {}
    for task in tasks:
        par_etape.setdefault(task["phase_id"], []).append(task)

    vues = [_phase_view(phase, par_etape.get(phase["id"], []), today) for phase in phases]
    en_cours = next((vue for vue in vues if vue["status"] in ("doing", "blocked")), None)
    en_cours = en_cours or next((vue for vue in vues if vue["status"] == "todo"), None)
    terminees = [vue for vue in vues if vue["status"] == "done"]
    ouvertes = [task for task in tasks if task["status"] != "done"]
    limite_client = (today - timedelta(days=CLIENT_WAIT_DAYS)).isoformat()
    cote_client = [task for task in ouvertes if task.get("side") in ("client", "both")]
    anomalies, livrables = _quality_counts(tasks)

    return {
        "phases": vues,
        "phase_progress": _phases_progress(vues) if vues else None,
        "current_phase": _milestone(en_cours, "planned_date"),
        "last_milestone": _milestone(terminees[-1] if terminees else None, "done_date"),
        "next_milestone": _milestone(
            next((vue for vue in vues if vue["status"] != "done"), None), "planned_date"
        ),
        "task_states": _state_counts(tasks, jour),
        "blocked_count": sum(1 for task in ouvertes if task.get("blocked")),
        "waiting_client": len(cote_client),
        "waiting_client_old": sum(
            1 for task in cote_client if (task.get("created_at") or "")[:10] < limite_client
        ),
        "due_soon": sum(
            1 for task in ouvertes
            if task["deadline"]
            and jour <= task["deadline"] <= (today + timedelta(days=DEADLINE_SOON_DAYS)).isoformat()
        ),
        "anomalies": anomalies,
        "deliverables": livrables,
    }


def phase_pilot(project_id, today=None):
    """Pilotage par etapes d'un seul projet (etapes enrichies, etape en cours, jalons)."""
    return _pilot(
        phase_model.list_phases(project_id),
        phase_model.board_tasks(project_id),
        today or date.today(),
    )


# Champs d'une etape qu'un assistant (chat, session Claude) peut modifier :
# la structure (nom, ordre, poids) reste a la main de l'utilisateur.
ASSISTANT_PHASE_FIELDS = ("status", "planned_date", "done_date", "progress_pct")

PHASE_STATUS_LABELS = {"todo": "à venir", "doing": "en cours", "blocked": "bloquée", "done": "terminée"}


def _phase_change_text(avant, apres):
    """Phrase de journal decrivant ce qui a change sur une etape, ou None."""
    morceaux = []
    if apres["status"] != avant["status"]:
        texte = PHASE_STATUS_LABELS[apres["status"]]
        if apres["status"] == "done" and apres["done_date"]:
            texte += f" le {apres['done_date']}"
        morceaux.append(texte)
    if apres["planned_date"] != avant["planned_date"]:
        morceaux.append(f"prévue le {apres['planned_date']}" if apres["planned_date"] else "date prévue retirée")
    if apres["status"] == avant["status"] == "done" and apres["done_date"] != avant["done_date"]:
        morceaux.append(f"terminée le {apres['done_date']}")
    if apres["progress_pct"] != avant["progress_pct"]:
        morceaux.append(f"avancement {apres['progress_pct']} %")
    if not morceaux:
        return None
    texte = f"Étape {apres['sequence']}. {apres['name']} : " + ", ".join(morceaux) + "."
    if apres.get("cascaded"):
        texte += f" {apres['cascaded']} étape(s) précédente(s) terminée(s) au passage."
    return texte


def change_phase(project_id, ref, data, source=None):
    """Fait avancer une etape a la demande d'un assistant, et le note au journal.

    :param ref: numero ou nom de l'etape
    :param data: champs parmi ASSISTANT_PHASE_FIELDS
    :param source: origine de la ligne de journal ('claude', 'chat')
    :return: etape a jour, 'cascaded' compris
    :raises ValidationError: etape introuvable, champ inconnu, avancement
        saisi sur une etape mesuree par ses taches
    """
    inconnus = set(data) - set(ASSISTANT_PHASE_FIELDS)
    if inconnus:
        raise task_model.ValidationError(
            "Champs d'etape modifiables : " + ", ".join(ASSISTANT_PHASE_FIELDS) + "."
        )
    if not data:
        raise task_model.ValidationError(
            "Rien a modifier : fournir status, planned_date, done_date ou progress_pct."
        )
    avant = phase_model.find_phase(project_id, ref)
    if "progress_pct" in data and phase_model.has_tasks(avant["id"]):
        raise task_model.ValidationError(
            "Cette etape a des taches : son avancement se calcule a partir d'elles."
        )
    apres = phase_model.update_phase(avant["id"], data)
    texte = _phase_change_text(avant, apres)
    if texte:
        journal.add_entry(texte, project_id=project_id, kind="progress", source=source)
    return apres


def _pilot_signals(pilot):
    """Signaux tires des etapes et des statuts de taches."""
    signaux = []
    for phase in pilot["phases"]:
        if phase["status"] == "blocked":
            signaux.append({"level": "danger", "label": f"étape bloquée : {phase['name']}"})

    jalon = pilot["next_milestone"]
    if jalon and jalon["days_left"] is not None and jalon["days_left"] < 0:
        signaux.append(
            {"level": "danger", "label": f"jalon « {jalon['name']} » dépassé de {-jalon['days_left']} j"}
        )

    if pilot["anomalies"]["critical"]:
        signaux.append(
            {"level": "danger", "label": f"{pilot['anomalies']['critical']} anomalie(s) critique(s) ouverte(s)"}
        )
    if pilot["blocked_count"]:
        signaux.append({"level": "warn", "label": f"{pilot['blocked_count']} tâche(s) bloquée(s)"})
    if pilot["waiting_client_old"]:
        signaux.append(
            {
                "level": "warn",
                "label": f"{pilot['waiting_client_old']} action(s) client en attente depuis plus de {CLIENT_WAIT_DAYS} j",
            }
        )
    return signaux


def _last_activity(row):
    """Derniere trace de vie du projet : tache, session de temps ou note."""
    traces = [row.get(cle) for cle in ("last_task_at", "last_session_at", "last_note_at")]
    traces = [trace for trace in traces if trace]
    return max(traces)[:10] if traces else None


def _signals(row, progress, last_activity, today):
    """Signaux d'alerte d'un projet, du plus grave au plus leger.

    :return: liste de {'level': 'danger'|'warn', 'label'}
    """
    signaux = []
    if row["status"] == "done":
        return signaux

    if row["deadline"] and row["open_count"]:
        jours = (date.fromisoformat(row["deadline"]) - today).days
        if jours < 0:
            signaux.append({"level": "danger", "label": f"échéance dépassée de {-jours} j"})
        elif jours <= DEADLINE_SOON_DAYS and progress < SOON_PROGRESS_PCT:
            signaux.append({"level": "warn", "label": f"échéance dans {jours} j, {progress} % fait"})

    if row["late_count"]:
        signaux.append({"level": "danger", "label": f"{row['late_count']} tâche(s) en retard"})

    if row["status"] == "active" and row["open_count"] and last_activity:
        inactif = (today - date.fromisoformat(last_activity)).days
        if inactif >= setting.get_int("project_idle_days"):
            signaux.append({"level": "warn", "label": f"à l'arrêt depuis {inactif} j"})

    return signaux


def _card(row, today, phases=(), tasks=()):
    """Projet enrichi pour l'affichage : avancement, reste, etapes, signaux.

    Un projet decoupe en etapes est mesure par ses etapes ponderees ; sans
    etape, par ses taches ponderees par les estimations.

    :param phases: etapes du projet dans l'ordre
    :param tasks: taches feuilles du projet (phase_model.board_tasks)
    """
    pilot = _pilot(list(phases), list(tasks), today)
    progress = pilot["phase_progress"] if phases else _progress(row)
    last_activity = _last_activity(row)
    signaux = _signals(row, progress, last_activity, today)
    if row["status"] != "done":
        signaux += _pilot_signals(pilot)
        signaux.sort(key=lambda signal: signal["level"] != "danger")
    return {
        "id": row["id"],
        "name": row["name"],
        "color": row["color"],
        "status": row["status"],
        "deadline": row["deadline"],
        "days_left": (date.fromisoformat(row["deadline"]) - today).days if row["deadline"] else None,
        "open_count": row["open_count"],
        "done_count": row["done_count"],
        "late_count": row["late_count"],
        "progress_pct": progress,
        "remaining_min": round(row["remaining_min"]),
        "estimated_min": row["est_total"],
        "tracked_min": row["tracked_min"],
        "tracked_week": row["tracked_week"],
        "done_week": row["done_week"],
        "last_activity": last_activity,
        "idle_days": (today - date.fromisoformat(last_activity)).days if last_activity else None,
        "signals": signaux,
        "weather": row.get("weather"),
        "weather_note": row.get("weather_note"),
        "go_live": row.get("go_live"),
        "manager_id": row.get("manager_id"),
        "odoo_version": row.get("odoo_version"),
        "client": row.get("client"),
        "phases": pilot["phases"],
        "current_phase": pilot["current_phase"],
        "last_milestone": pilot["last_milestone"],
        "next_milestone": pilot["next_milestone"],
        "task_states": pilot["task_states"],
        "blocked_count": pilot["blocked_count"],
        "waiting_client": pilot["waiting_client"],
        "due_soon": pilot["due_soon"],
        "anomalies": pilot["anomalies"],
        "deliverables": pilot["deliverables"],
    }


def _group_by_project(rows):
    """Range des lignes portant project_id dans un dictionnaire par projet."""
    groupes = {}
    for row in rows:
        groupes.setdefault(row["project_id"], []).append(row)
    return groupes


def overview(today=None, space=None):
    """Vue d'ensemble : indicateurs globaux et une carte par projet.

    :param space: 'pro' ou 'perso' pour n'afficher que cet espace (cartes et
        indicateurs) ; None pour tous

    Trois requetes en tout, quel que soit le nombre de projets : les chiffres
    des projets, toutes les etapes, toutes les taches feuilles.
    """
    today = today or date.today()
    debut, fin = _week_bounds(today)
    etapes = _group_by_project(phase_model.list_phases())
    taches = _group_by_project(phase_model.board_tasks())
    cartes = [
        _card(row, today, etapes.get(row["id"], ()), taches.get(row["id"], ()))
        for row in project_model.board_rows(today.isoformat(), debut, fin, space=space)
    ]
    semaine = stats.period_summary(debut, fin, space=space)
    actifs = [carte for carte in cartes if carte["status"] == "active"]

    etats = {state: sum(carte["task_states"][state] for carte in actifs) for state in TASK_STATES}
    return {
        "week_start": debut,
        "week_end": fin,
        "kpis": {
            "active_projects": len(actifs),
            "at_risk": sum(1 for carte in actifs if carte["signals"]),
            "open_tasks": semaine["open_tasks"],
            "late_tasks": semaine["late_tasks"],
            "done_week": semaine["done"],
            "tracked_week": semaine["tracked_min"],
            "due_soon": sum(carte["due_soon"] for carte in actifs),
            "blocked": sum(carte["blocked_count"] for carte in actifs),
            "waiting_client": sum(carte["waiting_client"] for carte in actifs),
            "anomalies_open": sum(carte["anomalies"]["open"] for carte in actifs),
            "anomalies_critical": sum(carte["anomalies"]["critical"] for carte in actifs),
            "deliverables_awaited": sum(carte["deliverables"]["awaited"] for carte in actifs),
            "task_states": etats,
        },
        "projects": cartes,
    }


def _task_row(task, today_iso):
    """Tache reduite a ce qu'affiche la fiche projet."""
    return {
        "state": task_state(task, today_iso),
        "phase_id": task.get("phase_id"),
        "blocked": task.get("blocked") or 0,
        "side": task.get("side"),
        "kind": task.get("kind") or "task",
        "severity": task.get("severity"),
        "module": task.get("module"),
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "priority": task["priority"],
        "deadline": task["deadline"],
        "estimate_min": task["estimate_min"],
        "tracked_min": task.get("tracked_min") or 0,
        "progress_pct": task.get("progress_pct") or 0,
        "parent_id": task.get("parent_id"),
        "position": task.get("position") or 0,
        "person_name": task.get("person_name"),
        "source": task.get("source"),
        "done_at": task.get("done_at"),
    }


def _anomaly_rows(rows):
    """Anomalies du projet : ouvertes d'abord, de la plus grave a la plus legere."""
    rang = {severity: index for index, severity in enumerate(VALID_SEVERITY)}
    return sorted(
        (ligne for ligne in rows if ligne["kind"] == "anomaly"),
        key=lambda ligne: (
            ligne["status"] == "done",
            rang.get(ligne["severity"], len(rang)),
            -ligne["id"],
        ),
    )


def _deliverable_rows(rows):
    """Livrables du projet : attendus d'abord, par echeance, puis recus."""
    return sorted(
        (ligne for ligne in rows if ligne["kind"] == "deliverable"),
        key=lambda ligne: (ligne["status"] == "done", ligne["deadline"] or "9999", ligne["id"]),
    )


def detail(project_id, today=None):
    """Fiche complete d'un projet, ou None s'il n'existe pas."""
    today = today or date.today()
    debut, fin = _week_bounds(today)
    rows = project_model.board_rows(today.isoformat(), debut, fin, project_id=project_id)
    if not rows:
        return None

    taches = task_model.list_tasks(project_id=project_id)
    parents = {task["id"]: task for task in taches}
    jour = today.isoformat()
    en_retard, a_faire, terminees, a_surveiller = [], [], [], []
    par_etape = {}
    for task in taches:
        ligne = _task_row(task, jour)
        parent = parents.get(task.get("parent_id"))
        ligne["parent_title"] = parent["title"] if parent else None
        # Une sous-tache sans etape se range dans celle de sa tache parente.
        if not ligne["phase_id"] and parent:
            ligne["phase_id"] = parent.get("phase_id")
        par_etape.setdefault(ligne["phase_id"], []).append(ligne)

        if task["status"] == "done":
            terminees.append(ligne)
        elif task["deadline"] and task["deadline"] < jour:
            en_retard.append(ligne)
        else:
            a_faire.append(ligne)

        if ligne["state"] in ("late", "blocked") or (
            task["status"] != "done" and ligne["side"] in ("client", "both")
        ):
            a_surveiller.append(ligne)
    terminees.sort(key=lambda ligne: ligne["done_at"] or "", reverse=True)

    carte = _card(
        rows[0], today, phase_model.list_phases(project_id), phase_model.board_tasks(project_id)
    )
    for etape in carte["phases"]:
        etape["tasks"] = par_etape.get(etape["id"], [])
    connues = {etape["id"] for etape in carte["phases"]}
    sans_etape = [
        ligne for cle, lignes in par_etape.items() if cle not in connues for ligne in lignes
    ]
    lignes = [ligne for groupe in par_etape.values() for ligne in groupe]

    personnes = person_model.list_people(project_id=project_id)
    attentes = [
        attente
        for personne in personnes
        for attente in person_model.list_waiting(status="open", person_id=personne["id"])
    ]
    rendez_vous = [
        evenement
        for evenement in event_model.list_events(today, today + timedelta(days=EVENTS_DAYS))
        if evenement.get("project_id") == project_id
    ]
    depuis = (today - timedelta(weeks=ACTIVITY_WEEKS)).isoformat()

    return {
        "project": carte,
        "tasks": {
            "late": en_retard,
            "open": a_faire,
            "done": terminees[:DONE_SHOWN],
            "done_total": len(terminees),
            "unphased": sans_etape,
            "watch": a_surveiller,
            "anomalies": _anomaly_rows(lignes),
            "deliverables": _deliverable_rows(lignes),
        },
        "register": project_register.register_view(journal.list_register(project_id)),
        "journal": journal.list_for_project(project_id),
        "people": personnes,
        "waiting": attentes,
        "events": rendez_vous,
        "activity": project_model.weekly_activity(project_id, depuis),
        "orphan_count": project_model.count_orphan_matches(project_id),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
    }
