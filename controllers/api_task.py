"""API JSON des taches et du chronometre."""
from flask import Blueprint, jsonify, request

from models import journal
from models import session as session_model
from models import setting
from models import task as task_model
from models.journal import JournalError
from models.task import ValidationError

api_task_bp = Blueprint("api_task", __name__, url_prefix="/api/tasks")


@api_task_bp.errorhandler(ValidationError)
def handle_validation_error(error):
    """Transforme une erreur de validation en reponse HTTP 400."""
    return jsonify({"error": str(error)}), 400


def _payload():
    """Retourne le corps JSON de la requete, ou un dictionnaire vide."""
    return request.get_json(silent=True) or {}


def _project_filter():
    """Lit le filtre projet de la requete.

    Trois cas : absent (aucun filtre), 'none' (taches sans projet), ou un
    identifiant. Une valeur non numerique est ignoree plutot que refusee : un
    filtre corrompu ne doit pas vider le tableau de bord sans explication.
    """
    brut = (request.args.get("project_id") or "").strip()
    if not brut:
        return None
    if brut == task_model.NO_PROJECT:
        return task_model.NO_PROJECT
    return int(brut) if brut.isdigit() else None


@api_task_bp.get("")
def list_tasks():
    """Liste les taches avec les compteurs du tableau de bord.

    `space=current` restreint liste et compteurs a l'espace choisi dans
    l'interface ; sans ce parametre, tous les espaces.
    """
    status = request.args.get("status") or None
    space = setting.resolve_space(request.args.get("space"))
    return jsonify(
        {
            "tasks": task_model.list_tasks(
                status=status, project_id=_project_filter(), space=space
            ),
            "stats": task_model.stats(space=space),
        }
    )


@api_task_bp.get("/<int:task_id>")
def get_task(task_id):
    """Retourne une tache seule, avec ses etapes (fenetres de modification et de detail)."""
    task = task_model.get_task(task_id)
    if task is None:
        return jsonify({"error": "Tache introuvable."}), 404
    task["subtasks"] = task_model.list_subtasks(task_id)
    task["journal"] = journal.list_for_task(task_id, limit=20)
    return jsonify(task)


@api_task_bp.post("/<int:task_id>/journal")
def add_journal_entry(task_id):
    """Ajoute une note au journal d'une tache, avec un avancement eventuel."""
    if task_model.get_task(task_id) is None:
        return jsonify({"error": "Tache introuvable."}), 404
    data = _payload()
    if not (data.get("content") or "").strip():
        return jsonify({"error": "Le contenu de la note est obligatoire."}), 400
    if data.get("progress_pct") not in (None, ""):
        task_model.update_task(task_id, {"progress_pct": data.get("progress_pct")})
    try:
        entree = journal.add_entry(
            data.get("content"),
            task_id=task_id,
            kind="progress" if data.get("progress_pct") not in (None, "") else "note",
        )
    except JournalError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(entree), 201


@api_task_bp.post("")
def create_task():
    """Cree une tache."""
    return jsonify(task_model.create_task(_payload())), 201


@api_task_bp.patch("/<int:task_id>")
def update_task(task_id):
    """Met a jour partiellement une tache."""
    task = task_model.update_task(task_id, _payload())
    if task is None:
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify(task)


@api_task_bp.post("/<int:task_id>/toggle")
def toggle_task(task_id):
    """Bascule l'etat d'une tache entre a faire et terminee."""
    task = task_model.toggle_task(task_id)
    if task is None:
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify(task)


@api_task_bp.post("/<int:task_id>/done-on")
def complete_on(task_id):
    """Marque une tache terminee a une date passee (case oubliee)."""
    task = task_model.complete_on(task_id, _payload().get("day"))
    if task is None:
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify(task)


@api_task_bp.delete("/<int:task_id>")
def delete_task(task_id):
    """Supprime definitivement une tache."""
    if not task_model.delete_task(task_id):
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify({"deleted": task_id})


@api_task_bp.get("/timer")
def get_timer():
    """Retourne la session de travail en cours, ou null."""
    return jsonify({"session": session_model.running()})


@api_task_bp.post("/<int:task_id>/timer/start")
def start_timer(task_id):
    """Demarre le chronometre sur une tache (arrete la session precedente)."""
    if task_model.get_task(task_id) is None:
        return jsonify({"error": "Tache introuvable."}), 404

    session_model.start(task_id)
    return jsonify({"session": session_model.running()}), 201


@api_task_bp.post("/timer/stop")
def stop_timer():
    """Arrete le chronometre en cours."""
    duration = session_model.stop()
    if duration is None:
        return jsonify({"error": "Aucun chronometre en cours."}), 409
    return jsonify({"duration_min": duration})
