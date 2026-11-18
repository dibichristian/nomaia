"""Recherche universelle : /api/search (palette Ctrl+K)."""
from flask import Blueprint, jsonify, request

from services import search as search_service

api_search_bp = Blueprint("api_search", __name__, url_prefix="/api")

MAX_LIMIT = 50


@api_search_bp.get("/search")
def search():
    """Taches, projets, journal, contacts et evenements correspondant a q (deux caracteres au moins)."""
    text = (request.args.get("q") or "").strip()[:200]
    try:
        limit = max(1, min(MAX_LIMIT, int(request.args.get("limit") or 20)))
    except ValueError:
        limit = 20
    return jsonify({"query": text, "results": search_service.search(text, limit=limit)})
