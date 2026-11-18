"""API JSON des interlocuteurs et des attentes."""
from flask import Blueprint, jsonify, request

from models import person as person_model
from models.person import PersonError

api_person_bp = Blueprint("api_person", __name__, url_prefix="/api")


@api_person_bp.get("/people")
def list_people():
    """Retourne les interlocuteurs et leurs deux compteurs."""
    return jsonify({"people": person_model.list_people()})


@api_person_bp.get("/people/<int:person_id>")
def get_person(person_id):
    """Retourne un interlocuteur, ses attentes et les taches qui lui sont dues."""
    personne = person_model.get_person(person_id)
    if personne is None:
        return jsonify({"error": "Interlocuteur introuvable."}), 404

    return jsonify(
        {
            "person": personne,
            "waiting": person_model.list_waiting(status="open", person_id=person_id),
            "owed": person_model.owed_tasks(person_id),
        }
    )


@api_person_bp.post("/people")
def create_person():
    """Cree un interlocuteur."""
    try:
        personne = person_model.create_person(request.get_json(silent=True) or {})
    except PersonError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"person": personne}), 201


@api_person_bp.patch("/people/<int:person_id>")
def update_person(person_id):
    """Met a jour un interlocuteur."""
    try:
        personne = person_model.update_person(person_id, request.get_json(silent=True) or {})
    except PersonError as error:
        return jsonify({"error": str(error)}), 400

    if personne is None:
        return jsonify({"error": "Interlocuteur introuvable."}), 404

    return jsonify({"person": personne})


@api_person_bp.post("/people/<int:person_id>/merge")
def merge_person(person_id):
    """Absorbe un doublon (merge_id) dans cet interlocuteur, puis le supprime."""
    payload = request.get_json(silent=True) or {}
    try:
        merge_id = int(payload.get("merge_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Interlocuteur a fusionner (merge_id) attendu."}), 400

    try:
        personne = person_model.merge_people(person_id, merge_id)
    except PersonError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"person": personne})


@api_person_bp.delete("/people/<int:person_id>")
def delete_person(person_id):
    """Supprime un interlocuteur et ses attentes. Les taches sont detachees."""
    if not person_model.delete_person(person_id):
        return jsonify({"error": "Interlocuteur introuvable."}), 404
    return jsonify({"ok": True})


@api_person_bp.get("/waiting")
def list_waiting():
    """Retourne les attentes, la plus ancienne d'abord."""
    statut = (request.args.get("status") or "open").strip()
    if statut == "all":
        statut = None

    try:
        attentes = person_model.list_waiting(status=statut)
    except PersonError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"waiting": attentes})


@api_person_bp.post("/waiting")
def create_waiting():
    """Enregistre une attente : quelque chose qu'on me doit."""
    try:
        attente = person_model.create_waiting(request.get_json(silent=True) or {})
    except PersonError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"waiting": attente}), 201


@api_person_bp.patch("/waiting/<int:waiting_id>")
def update_waiting(waiting_id):
    """Met a jour une attente : sujet, echeance ou statut."""
    try:
        attente = person_model.update_waiting(waiting_id, request.get_json(silent=True) or {})
    except PersonError as error:
        return jsonify({"error": str(error)}), 400

    if attente is None:
        return jsonify({"error": "Attente introuvable."}), 404

    return jsonify({"waiting": attente})


@api_person_bp.delete("/waiting/<int:waiting_id>")
def delete_waiting(waiting_id):
    """Supprime une attente."""
    if not person_model.delete_waiting(waiting_id):
        return jsonify({"error": "Attente introuvable."}), 404
    return jsonify({"ok": True})
