"""API des sessions Claude Code : /api/agent/.

Appelee par le serveur MCP local (integrations/claude_mcp.py), jamais par le
navigateur. L'acces se fait par la cle des sessions (en-tete Authorization),
verifiee dans controllers/security.py, et non par le cookie de l'interface.
"""
from flask import Blueprint, g, jsonify, request

from models.journal import JournalError
from services import agent
from services import search as search_service
from services.agent import AgentError

api_agent_bp = Blueprint("api_agent", __name__, url_prefix="/api/agent")


@api_agent_bp.errorhandler(AgentError)
@api_agent_bp.errorhandler(JournalError)
def handle_agent_error(error):
    """Erreur de saisie : renvoyee telle quelle, pour que la session corrige."""
    return jsonify({"error": str(error)}), 400


def _payload():
    """Corps JSON de la requete, ou dictionnaire vide."""
    return request.get_json(silent=True) or {}


@api_agent_bp.get("/tasks")
def list_tasks():
    """Taches filtrees par statut, projet et mots du titre."""
    return jsonify(
        {
            "tasks": agent.list_tasks(
                status=request.args.get("status") or "todo",
                project=request.args.get("project"),
                query=request.args.get("q"),
                limit=request.args.get("limit") or 30,
                kind=request.args.get("kind"),
            )
        }
    )


@api_agent_bp.get("/tasks/<int:task_id>")
def get_task(task_id):
    """Detail d'une tache, etapes et journal compris."""
    return jsonify(agent.get_task(task_id))


@api_agent_bp.post("/tasks")
def create_task():
    """Cree une tache, sauf si une tache proche existe deja (force pour passer outre)."""
    data = _payload()
    resultat = agent.create_task(
        title=data.get("title"),
        description=data.get("description"),
        project=data.get("project"),
        priority=data.get("priority", 3),
        estimate_min=data.get("estimate_min", 60),
        deadline=data.get("deadline"),
        parent_id=data.get("parent_id"),
        force=bool(data.get("force")),
        phase=data.get("phase"),
        blocked=bool(data.get("blocked")),
        side=data.get("side"),
        kind=data.get("kind"),
        severity=data.get("severity"),
        module=data.get("module"),
        person=data.get("person"),
    )
    return jsonify(resultat), 201 if resultat.get("created") else 200


@api_agent_bp.post("/tasks/<int:task_id>/progress")
def update_task(task_id):
    """Avancement, commentaire, cloture ou cadrage d'une tache."""
    data = _payload()
    return jsonify(
        agent.update_task(
            task_id,
            progress_pct=data.get("progress_pct"),
            note=data.get("note"),
            status=data.get("status"),
            deadline=data.get("deadline"),
            priority=data.get("priority"),
            project=data.get("project"),
            phase=data.get("phase"),
            blocked=data.get("blocked"),
            side=data.get("side"),
            kind=data.get("kind"),
            severity=data.get("severity"),
            module=data.get("module"),
            person=data.get("person"),
        )
    )


@api_agent_bp.post("/tasks/<int:task_id>/work")
def log_work(task_id):
    """Temps passe sur une tache, avec ce qui a ete fait."""
    data = _payload()
    return jsonify(agent.log_work(task_id, data.get("minutes"), data.get("summary")))


@api_agent_bp.get("/search")
def search():
    """Recherche plein texte (taches, projets, journal, contacts, evenements, comptes rendus, notes).

    kind et url servent a la session : l'element d'un compte rendu porte son
    propre identifiant, celui du compte rendu se lit dans l'url.
    """
    text = (request.args.get("q") or "").strip()[:200]
    if len(text) < 2:
        raise AgentError("Indique au moins deux caracteres a chercher.")
    try:
        limit = max(1, min(50, int(request.args.get("limit") or 15)))
    except ValueError:
        limit = 15
    results = [
        {key: item.get(key) for key in ("kind", "kind_label", "id", "label", "sub", "url")}
        for item in search_service.search(text, limit=limit, space=g.get("agent_space"))
    ]
    return jsonify({"query": text, "results": results})


@api_agent_bp.get("/people")
def list_people():
    """Contacts, avec ce que je leur dois et ce qu'ils me doivent."""
    return jsonify(agent.list_people(request.args.get("q"), request.args.get("project")))


@api_agent_bp.post("/people")
def save_person():
    """Cree un contact ou complete sa fiche (nom ou alias deja connu)."""
    data = _payload()
    resultat = agent.save_person(
        data.get("name"), role=data.get("role"), project=data.get("project"), email=data.get("email"),
        phone=data.get("phone"), notes=data.get("notes"), new_name=data.get("new_name"),
        remove_project=data.get("remove_project"),
    )
    return jsonify(resultat), 201 if resultat["created"] else 200


@api_agent_bp.get("/waiting")
def list_waiting():
    """Ce que j'attends des autres."""
    return jsonify(agent.list_waiting(request.args.get("person"), request.args.get("status") or "open"))


@api_agent_bp.post("/waiting")
def add_waiting():
    """Enregistre ce que quelqu'un me doit (contact cree s'il est inconnu)."""
    data = _payload()
    resultat = agent.add_waiting(
        data.get("person"), data.get("subject"), due=data.get("due"),
        task_id=data.get("task_id"), project=data.get("project"),
    )
    return jsonify(resultat), 201 if resultat["created"] else 200


@api_agent_bp.post("/waiting/<int:waiting_id>/resolve")
def resolve_waiting(waiting_id):
    """Clot une attente : recue ou abandonnee."""
    return jsonify(agent.resolve_waiting(waiting_id, _payload().get("status") or "received"))


@api_agent_bp.get("/notes/<int:note_id>")
def get_note(note_id):
    """Note de la base de connaissances, contenu compris."""
    return jsonify(agent.get_note(note_id))


@api_agent_bp.post("/notes")
def create_note():
    """Cree une note de la base de connaissances."""
    return jsonify(agent.create_note(_payload())), 201


@api_agent_bp.patch("/notes/<int:note_id>")
def update_note(note_id):
    """Modifie les champs fournis d'une note ; append=true ajoute le contenu a la fin."""
    data = _payload()
    return jsonify(agent.update_note(note_id, data, append=bool(data.get("append"))))


@api_agent_bp.get("/minutes/<int:minutes_id>")
def get_minutes(minutes_id):
    """Compte rendu avec ses elements."""
    return jsonify(agent.get_minutes(minutes_id))


@api_agent_bp.post("/minutes")
def create_minutes():
    """Cree un compte rendu, elements compris."""
    return jsonify(agent.create_minutes(_payload())), 201


@api_agent_bp.patch("/minutes/<int:minutes_id>")
def update_minutes(minutes_id):
    """Modifie les sections fournies, ajoute des elements, valide ou rouvre."""
    return jsonify(agent.update_minutes(minutes_id, _payload()))


@api_agent_bp.get("/projects")
def list_projects():
    """Projets actifs ou en pause."""
    return jsonify({"projects": agent.list_projects()})


@api_agent_bp.post("/projects")
def create_project():
    """Cree un projet, sauf si un projet du meme nom existe deja (renvoye alors)."""
    data = _payload()
    resultat = agent.create_project(data.get("name"), deadline=data.get("deadline"))
    return jsonify(resultat), 201 if resultat["created"] else 200


@api_agent_bp.post("/records")
def add_record():
    """Decision, changement de perimetre ou dependance inscrit au registre d'un projet."""
    data = _payload()
    resultat = agent.add_record(
        data.get("project"), data.get("kind"), data.get("content"), follow_up=data.get("follow_up")
    )
    return jsonify(resultat), 201


@api_agent_bp.post("/phases/update")
def update_phase():
    """Statut, dates ou avancement d'une etape ; terminer clot les precedentes."""
    data = _payload()
    return jsonify(
        agent.update_phase(
            data.get("project"),
            data.get("phase"),
            status=data.get("status"),
            planned_date=data.get("planned_date"),
            done_date=data.get("done_date"),
            progress_pct=data.get("progress_pct"),
        )
    )


@api_agent_bp.get("/phases")
def list_phases():
    """Etapes d'un projet, etape en cours comprise."""
    return jsonify(agent.list_phases(request.args.get("project")))


@api_agent_bp.get("/progress-report")
def progress_report():
    """Rapport d'avancement d'un chef de projet, ou liste des chefs de projet."""
    return jsonify(
        agent.progress_report(
            request.args.get("manager"), request.args.get("start"), request.args.get("end")
        )
    )


@api_agent_bp.post("/progress-report/url")
def set_report_url():
    """Retient le lien du tableau de bord publie pour un chef de projet."""
    data = _payload()
    return jsonify(agent.set_report_url(data.get("manager"), data.get("url")))
