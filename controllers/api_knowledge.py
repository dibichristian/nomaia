"""Base de connaissances : /api/notes (notes techniques en Markdown)."""
from flask import Blueprint, jsonify, request

from models import knowledge
from models.knowledge import KnowledgeError

api_knowledge_bp = Blueprint("api_knowledge", __name__, url_prefix="/api/notes")


@api_knowledge_bp.errorhandler(KnowledgeError)
def handle_error(error):
    """Saisie invalide (dont secret en clair) : message renvoye tel quel."""
    return jsonify({"error": str(error)}), 400


def _payload():
    return request.get_json(silent=True) or {}


@api_knowledge_bp.get("")
def list_notes():
    """Notes filtrees par projet, categorie, etiquette ou version d'Odoo, avec les valeurs des filtres."""
    return jsonify({
        "notes": knowledge.list_notes(
            project_id=request.args.get("project_id", type=int),
            category=request.args.get("category") or None,
            tag=request.args.get("tag") or None,
            odoo_version=request.args.get("odoo_version") or None,
        ),
        "facets": knowledge.facets(),
        "deployment_template": knowledge.DEPLOYMENT_TEMPLATE,
    })


@api_knowledge_bp.post("")
def create():
    return jsonify(knowledge.create(_payload())), 201


@api_knowledge_bp.get("/<int:note_id>")
def get(note_id):
    note = knowledge.get(note_id)
    return jsonify(note) if note else (jsonify({"error": "Note introuvable."}), 404)


@api_knowledge_bp.patch("/<int:note_id>")
def update(note_id):
    note = knowledge.update(note_id, _payload())
    return jsonify(note) if note else (jsonify({"error": "Note introuvable."}), 404)


@api_knowledge_bp.delete("/<int:note_id>")
def delete(note_id):
    if knowledge.delete(note_id):
        return jsonify({"deleted": note_id})
    return jsonify({"error": "Note introuvable."}), 404
