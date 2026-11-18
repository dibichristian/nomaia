"""API JSON de l'import Odoo : etat, test de connexion, synchronisation."""
from flask import Blueprint, jsonify

from models import setting
from services import odoo_sync

api_odoo_bp = Blueprint("api_odoo", __name__, url_prefix="/api/odoo")


@api_odoo_bp.get("")
def get_status():
    """Etat de l'import : connexion renseignee et bilan de la derniere synchro."""
    return jsonify(
        {
            "configured": bool(setting.get("odoo_url") and setting.get_secret("odoo_api_key")),
            "last_sync": setting.get("odoo_last_sync") or None,
            "last_report": odoo_sync.last_report(),
        }
    )


@api_odoo_bp.post("/test")
def test_connection():
    """Verifie la connexion et compte les taches ouvertes assignees."""
    try:
        return jsonify({"ok": True, **odoo_sync.test_connection()})
    except odoo_sync.OdooSyncError as error:
        return jsonify({"ok": False, "error": str(error)}), 502


@api_odoo_bp.post("/sync")
def sync():
    """Importe les taches Odoo assignees, avec leur hierarchie."""
    try:
        return jsonify({"ok": True, "report": odoo_sync.sync()})
    except odoo_sync.OdooSyncError as error:
        return jsonify({"ok": False, "error": str(error)}), 502
