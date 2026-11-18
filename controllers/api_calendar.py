"""API JSON des agendas externes : sources ICS, agenda Odoo et synchronisation."""
from flask import Blueprint, jsonify, request

from models import calendar_source as source_model
from models.calendar_source import SourceError
from services import calendar_sync, odoo_calendar

api_calendar_bp = Blueprint("api_calendar", __name__, url_prefix="/api")


@api_calendar_bp.get("/calendar-sources")
def list_sources():
    """Retourne les sources, URL masquees.

    La source Odoo porte ses quatre imports (options) ; odoo_available dit si
    l'agenda Odoo peut etre ajoute : connexion renseignee et aucune source Odoo.
    """
    sources = source_model.list_sources()
    a_odoo = False
    for source in sources:
        if source["kind"] == source_model.KIND_ODOO:
            source["options"] = odoo_calendar.options()
            a_odoo = True
    return jsonify(
        {"sources": sources, "odoo_available": not a_odoo and odoo_calendar.is_configured()}
    )


@api_calendar_bp.post("/calendar-sources")
def create_source():
    """Cree une source puis la synchronise aussitot.

    Une adresse fausse se voit ainsi a l'enregistrement, pas un quart d'heure
    plus tard dans un journal que personne ne lit. {"kind": "odoo"} relie
    l'agenda Odoo, sans adresse : la connexion est celle des taches Odoo.
    """
    charge = request.get_json(silent=True) or {}
    try:
        if charge.get("kind") == source_model.KIND_ODOO:
            if not odoo_calendar.is_configured():
                raise SourceError("Connexion Odoo non renseignee (Config > Connexions).")
            source = source_model.create_odoo_source(charge.get("name") or "Odoo")
        else:
            source = source_model.create_source(charge)
    except SourceError as error:
        return jsonify({"error": str(error)}), 400

    issue = calendar_sync.sync_source(source["id"])
    return jsonify({"source": source_model.get_source(source["id"]), "sync": issue}), 201


@api_calendar_bp.patch("/calendar-sources/<int:source_id>")
def update_source(source_id):
    """Met a jour le nom, l'adresse ou l'activation d'une source."""
    charge = request.get_json(silent=True) or {}
    try:
        source = source_model.update_source(source_id, charge)
    except SourceError as error:
        return jsonify({"error": str(error)}), 400

    if source is None:
        return jsonify({"error": "Calendrier introuvable."}), 404

    # Nouvelle adresse ou reactivation : l'effet doit se voir tout de suite.
    issue = None
    if source["enabled"] and ((charge.get("url") or "").strip() or "enabled" in charge):
        issue = calendar_sync.sync_source(source_id)
        source = source_model.get_source(source_id)

    return jsonify({"source": source, "sync": issue})


@api_calendar_bp.delete("/calendar-sources/<int:source_id>")
def delete_source(source_id):
    """Supprime une source et tout ce qu'elle a importe."""
    if not source_model.delete_source(source_id):
        return jsonify({"error": "Calendrier introuvable."}), 404
    return jsonify({"ok": True})


@api_calendar_bp.post("/calendar-sources/<int:source_id>/sync")
def sync_source(source_id):
    """Synchronise une source a la demande."""
    source = source_model.get_source(source_id)
    if source is None:
        return jsonify({"error": "Calendrier introuvable."}), 404
    if not source["enabled"]:
        # Reimporter une source desactivee contredirait sa desactivation.
        return jsonify({"error": "Calendrier desactive : reactive-le d'abord."}), 400

    issue = calendar_sync.sync_source(source_id)
    return jsonify({"source": source_model.get_source(source_id), "sync": issue})
