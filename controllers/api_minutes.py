"""Comptes rendus de reunion, d'intervention et de formation : /api/minutes."""
from flask import Blueprint, jsonify, request

from models import minutes as minutes_model
from models.journal import JournalError
from models.minutes import MinutesError
from models.task import ValidationError
from services import minutes as minutes_service
from services import secrets_guard
from services.secrets_guard import SecretDetected

api_minutes_bp = Blueprint("api_minutes", __name__, url_prefix="/api/minutes")


@api_minutes_bp.errorhandler(MinutesError)
@api_minutes_bp.errorhandler(JournalError)
@api_minutes_bp.errorhandler(ValidationError)
@api_minutes_bp.errorhandler(SecretDetected)
def handle_error(error):
    """Saisie invalide : message renvoye tel quel."""
    return jsonify({"error": str(error)}), 400


def _payload():
    """Corps JSON ; tout texte contenant un secret en clair est refuse avant enregistrement."""
    data = request.get_json(silent=True) or {}
    secrets_guard.check({minutes_model.FIELD_LABELS.get(key, key): value
                         for key, value in data.items() if isinstance(value, str)})
    return data


def _not_found():
    return jsonify({"error": "Compte rendu introuvable."}), 404


@api_minutes_bp.get("")
def list_minutes():
    """Comptes rendus, du plus recent au plus ancien (filtre ?project_id=)."""
    project_id = request.args.get("project_id", type=int)
    return jsonify({"minutes": minutes_model.list_minutes(project_id=project_id)})


@api_minutes_bp.post("")
def create():
    """Cree un compte rendu ; avec event_id, celui de la seance (l'existant est renvoye s'il y en a un)."""
    data = _payload()
    if data.get("event_id"):
        minutes, created = minutes_model.create_from_event(int(data["event_id"]), data.get("held_on"), data.get("kind"))
        return jsonify(minutes), 201 if created else 200
    return jsonify(minutes_model.create(data)), 201


@api_minutes_bp.get("/<int:minutes_id>")
def get(minutes_id):
    minutes = minutes_model.get(minutes_id)
    return jsonify(minutes) if minutes else _not_found()


@api_minutes_bp.patch("/<int:minutes_id>")
def update(minutes_id):
    """Modification partielle (sauvegarde automatique de l'editeur)."""
    minutes = minutes_model.update(minutes_id, _payload())
    return jsonify(minutes) if minutes else _not_found()


@api_minutes_bp.delete("/<int:minutes_id>")
def delete(minutes_id):
    return jsonify({"deleted": minutes_id}) if minutes_model.delete(minutes_id) else _not_found()


@api_minutes_bp.post("/<int:minutes_id>/finalize")
def finalize(minutes_id):
    """Valide le compte rendu et signale les actions et decisions restant a convertir."""
    result = minutes_service.finalize(minutes_id)
    return jsonify(result) if result else _not_found()


@api_minutes_bp.post("/<int:minutes_id>/items")
def add_item(minutes_id):
    item = minutes_model.add_item(minutes_id, _payload())
    return (jsonify(item), 201) if item else _not_found()


@api_minutes_bp.patch("/items/<int:item_id>")
def update_item(item_id):
    item = minutes_model.update_item(item_id, _payload())
    return jsonify(item) if item else (jsonify({"error": "Élément introuvable."}), 404)


@api_minutes_bp.delete("/items/<int:item_id>")
def delete_item(item_id):
    if minutes_model.delete_item(item_id):
        return jsonify({"deleted": item_id})
    return jsonify({"error": "Élément introuvable."}), 404


@api_minutes_bp.post("/items/<int:item_id>/task")
def item_to_task(item_id):
    """Transforme une action (ou une demande) en tache du projet."""
    return jsonify(minutes_service.item_to_task(item_id)), 201


@api_minutes_bp.post("/items/<int:item_id>/register")
def item_to_register(item_id):
    """Inscrit une decision au registre du projet."""
    return jsonify(minutes_service.item_to_register(item_id)), 201
