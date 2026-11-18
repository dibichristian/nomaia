"""API JSON de l'agenda : reunions, creneaux bloques et absences."""
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from models import event as event_model
from models.event import EventError
from services import planner

api_event_bp = Blueprint("api_event", __name__, url_prefix="/api")

# Largeur maximale d'une periode consultable. Au-dela, l'expansion des series
# repetitives produit des milliers d'occurrences pour un affichage que
# personne ne lit.
MAX_RANGE_DAYS = 120

# Duree minimale d'un trou exploitable, quand l'appelant n'en impose pas.
DEFAULT_FREE_MIN = 30


def _day_arg(nom, defaut):
    """Lit une date en parametre d'URL, avec repli."""
    brut = (request.args.get(nom) or "").strip()
    if not brut:
        return defaut
    try:
        return datetime.strptime(brut, "%Y-%m-%d").date()
    except ValueError:
        raise EventError(f"Parametre {nom} attendu au format AAAA-MM-JJ.")


@api_event_bp.get("/events")
def list_events():
    """Retourne les evenements d'une periode, repetitions developpees."""
    aujourd_hui = date.today()
    lundi = aujourd_hui - timedelta(days=aujourd_hui.weekday())

    try:
        debut = _day_arg("start", lundi)
        fin = _day_arg("end", lundi + timedelta(days=6))

        if fin < debut:
            raise EventError("La fin de periode precede le debut.")
        if (fin - debut).days > MAX_RANGE_DAYS:
            raise EventError(f"Periode trop large ({MAX_RANGE_DAYS} jours au maximum).")

        evenements = event_model.list_events(debut, fin)
    except EventError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(
        {"events": evenements, "start": debut.isoformat(), "end": fin.isoformat()}
    )


@api_event_bp.post("/events")
def create_event():
    """Cree un evenement."""
    try:
        evenement = event_model.create_event(request.get_json(silent=True) or {})
    except EventError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"event": evenement}), 201


@api_event_bp.patch("/events/<int:event_id>")
def update_event(event_id):
    """Met a jour un evenement."""
    try:
        evenement = event_model.update_event(event_id, request.get_json(silent=True) or {})
    except EventError as error:
        return jsonify({"error": str(error)}), 400

    if evenement is None:
        return jsonify({"error": "Evenement introuvable."}), 404

    return jsonify({"event": evenement})


@api_event_bp.delete("/events/<int:event_id>")
def delete_event(event_id):
    """Supprime un evenement et toute sa serie."""
    try:
        trouve = event_model.delete_event(event_id)
    except EventError as error:
        return jsonify({"error": str(error)}), 400
    if not trouve:
        return jsonify({"error": "Evenement introuvable."}), 404
    return jsonify({"ok": True})


@api_event_bp.post("/events/<int:event_id>/cancel")
def cancel_occurrence(event_id):
    """Annule une seule occurrence d'une serie repetitive."""
    charge = request.get_json(silent=True) or {}

    try:
        trouve = event_model.cancel_occurrence(event_id, charge.get("day"))
    except EventError as error:
        return jsonify({"error": str(error)}), 400

    if not trouve:
        return jsonify({"error": "Evenement introuvable."}), 404

    return jsonify({"ok": True})


@api_event_bp.get("/events/free")
def free_slots():
    """Retourne les trous exploitables d'une journee, agenda deduit."""
    try:
        jour = _day_arg("date", date.today())
    except EventError as error:
        return jsonify({"error": str(error)}), 400

    minimum = request.args.get("min_minutes", default=DEFAULT_FREE_MIN, type=int)
    creneaux = planner.free_slots(jour, min_minutes=max(minimum, 0))

    return jsonify(
        {
            "date": jour.isoformat(),
            "slots": creneaux,
            "free_min": sum(item["duration_min"] for item in creneaux),
        }
    )
