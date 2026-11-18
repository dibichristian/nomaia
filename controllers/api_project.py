"""API JSON des projets."""
from flask import Blueprint, jsonify, request

from models import journal
from models import phase as phase_model
from models import project as project_model
from models import setting
from models.journal import JournalError
from models.project import ProjectError
from models.task import ValidationError
from services import project_board, project_register, project_resume
from services.llm import LLMError

api_project_bp = Blueprint("api_project", __name__, url_prefix="/api/projects")


@api_project_bp.errorhandler(ProjectError)
@api_project_bp.errorhandler(ValidationError)
def handle_project_error(error):
    """Transforme une erreur de validation en reponse HTTP 400."""
    return jsonify({"error": str(error)}), 400


def _project_phase(project_id, phase_id):
    """Retourne l'etape si elle appartient bien au projet, sinon None."""
    etape = phase_model.get_phase(phase_id)
    if etape is None or etape["project_id"] != project_id:
        return None
    return etape


@api_project_bp.get("")
def list_projects():
    """Liste les projets avec leurs compteurs et leur temps suivi."""
    status = request.args.get("status") or None
    projects = project_model.list_with_stats(
        status, setting.resolve_space(request.args.get("space"))
    )

    # Nombre de taches orphelines que chaque projet pourrait recuperer :
    # l'interface propose le rattrapage seulement quand il y a de quoi.
    for project in projects:
        project["orphan_count"] = project_model.count_orphan_matches(project["id"])

    return jsonify({"projects": projects})


@api_project_bp.post("")
def create_project():
    """Cree un projet.

    `attach_matching` rattache dans la foulee les taches ouvertes sans projet
    qui citent son nom : un projet est presque toujours cree apres les taches
    qu'il concerne.
    """
    payload = request.get_json(silent=True) or {}
    project = project_model.create_project(
        payload.get("name"),
        payload.get("color"),
        payload.get("deadline"),
        payload.get("space") or None,
    )

    if payload.get("attach_matching"):
        project["attached"] = project_model.attach_orphan_tasks(project["id"])

    return jsonify(project), 201


@api_project_bp.get("/board")
def board():
    """Tableau de bord : indicateurs et une carte par projet.

    `space=current` restreint cartes et indicateurs a l'espace choisi.
    """
    return jsonify(
        project_board.overview(space=setting.resolve_space(request.args.get("space")))
    )


@api_project_bp.get("/<int:project_id>/detail")
def detail(project_id):
    """Fiche complete d'un projet : taches, journal, contacts, agenda, activite."""
    fiche = project_board.detail(project_id)
    if fiche is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(fiche)


@api_project_bp.get("/<int:project_id>/resume")
def resume(project_id):
    """« Ou en etais-je ? » : synthese de reprise du projet, a partir des seules donnees enregistrees."""
    synthese = project_resume.resume(project_id)
    if synthese is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(synthese)


@api_project_bp.post("/<int:project_id>/resume/summary")
def resume_summary(project_id):
    """Resume de reprise redige par l'IA, sources citees (facultatif)."""
    try:
        resume_ia = project_resume.summarize(project_id)
    except LLMError as error:
        return jsonify({"error": f"Résumé indisponible : {error}"}), 503
    if resume_ia is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(resume_ia)


@api_project_bp.post("/<int:project_id>/journal")
def add_comment(project_id):
    """Ajoute un commentaire, ou une entree du registre, au journal du projet.

    Corps : content, kind ('note' par defaut, ou 'decision', 'scope',
    'dependency'), follow_up (ouvre une tache de suivi pour un changement de
    perimetre ou une dependance).
    """
    if project_model.get_project(project_id) is None:
        return jsonify({"error": "Projet introuvable."}), 404
    data = request.get_json(silent=True) or {}
    kind = data.get("kind") or "note"
    try:
        if kind == "note":
            entree = journal.add_entry(data.get("content"), project_id=project_id)
        else:
            entree = project_register.add_record(
                project_id, kind, data.get("content"), follow_up=bool(data.get("follow_up"))
            )
    except JournalError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(entree), 201


@api_project_bp.delete("/<int:project_id>/journal/<int:entry_id>")
def delete_comment(project_id, entry_id):
    """Supprime une ligne du journal du projet."""
    entree = journal.get_entry(entry_id)
    if entree is None or entree["project_id"] != project_id:
        return jsonify({"error": "Note introuvable."}), 404
    journal.delete_entry(entry_id)
    return jsonify({"deleted": entry_id})


@api_project_bp.post("/<int:project_id>/attach")
def attach_tasks(project_id):
    """Rattache a ce projet les taches ouvertes sans projet qui citent son nom."""
    if project_model.get_project(project_id) is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify({"attached": project_model.attach_orphan_tasks(project_id)})


@api_project_bp.get("/<int:project_id>/phases")
def list_phases(project_id):
    """Etapes du projet, dans l'ordre."""
    if project_model.get_project(project_id) is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify({"phases": phase_model.list_phases(project_id)})


@api_project_bp.post("/<int:project_id>/phases")
def create_phase(project_id):
    """Ajoute une etape au projet."""
    if project_model.get_project(project_id) is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(phase_model.create_phase(project_id, request.get_json(silent=True) or {})), 201


@api_project_bp.post("/<int:project_id>/phases/template")
def apply_phase_template(project_id):
    """Cree les etapes du gabarit generique sur un projet sans etape."""
    if project_model.get_project(project_id) is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify({"phases": phase_model.apply_template(project_id)}), 201


@api_project_bp.patch("/<int:project_id>/phases/<int:phase_id>")
def update_phase(project_id, phase_id):
    """Met a jour une etape du projet."""
    if _project_phase(project_id, phase_id) is None:
        return jsonify({"error": "Etape introuvable."}), 404
    return jsonify(phase_model.update_phase(phase_id, request.get_json(silent=True) or {}))


@api_project_bp.delete("/<int:project_id>/phases/<int:phase_id>")
def delete_phase(project_id, phase_id):
    """Supprime une etape. Ses taches restent dans le projet, sans etape."""
    if _project_phase(project_id, phase_id) is None:
        return jsonify({"error": "Etape introuvable."}), 404
    phase_model.delete_phase(phase_id)
    return jsonify({"deleted": phase_id})


@api_project_bp.patch("/<int:project_id>")
def update_project(project_id):
    """Met a jour un projet."""
    project = project_model.update_project(project_id, request.get_json(silent=True) or {})
    if project is None:
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify(project)


@api_project_bp.post("/<int:project_id>/merge")
def merge_project(project_id):
    """Absorbe un projet en double (merge_id) dans celui-ci, puis le supprime."""
    payload = request.get_json(silent=True) or {}
    try:
        merge_id = int(payload.get("merge_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Projet a fusionner manquant."}), 400
    return jsonify(project_model.merge_projects(project_id, merge_id))


@api_project_bp.delete("/<int:project_id>")
def delete_project(project_id):
    """Supprime un projet. Ses taches sont conservees et detachees."""
    if not project_model.delete_project(project_id):
        return jsonify({"error": "Projet introuvable."}), 404
    return jsonify({"deleted": project_id})
