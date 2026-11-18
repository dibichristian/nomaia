"""Rechargement a chaud : /api/revision, relu par les pages ouvertes toutes les 10 s."""
from flask import Blueprint, jsonify

from models import revision

api_live_bp = Blueprint("api_live", __name__, url_prefix="/api")


@api_live_bp.get("/revision")
def get_revision():
    """Revision courante des donnees : la page se recharge quand elle change.

    Protegee par la cle d'acces de l'interface, comme toute route hors /static/
    et /api/agent/ (voir controllers.security).
    """
    return jsonify({"rev": revision.current()})
