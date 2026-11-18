"""API JSON des statistiques et du bilan de periode."""
from datetime import datetime

from flask import Blueprint, jsonify, request

from services import plan_history, progress_report
from services import review as review_service
from services.llm import LLMError

api_review_bp = Blueprint("api_review", __name__, url_prefix="/api")


def _bounds():
    """Determine la periode demandee : une semaine, un mois, ou des dates libres.

    Parametres reconnus : start et end (AAAA-MM-JJ), sinon months_back
    (0 = mois en cours), sinon weeks_back (0 = semaine en cours, defaut).
    """
    raw_start = request.args.get("start") or (request.get_json(silent=True) or {}).get("start")
    raw_end = request.args.get("end") or (request.get_json(silent=True) or {}).get("end")

    if raw_start and raw_end:
        try:
            start = datetime.strptime(raw_start, "%Y-%m-%d").date()
            end = datetime.strptime(raw_end, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Dates attendues au format AAAA-MM-JJ.")
        if end < start:
            raise ValueError("La date de fin precede la date de debut.")
        return start, end

    months_back = request.args.get("months_back", type=int)
    if months_back is not None:
        return review_service.month_bounds(months_back=max(months_back, 0))

    weeks_back = request.args.get("weeks_back", default=0, type=int)
    return review_service.period_bounds(weeks_back=max(weeks_back, 0))


@api_review_bp.get("/stats")
def get_stats():
    """Retourne les indicateurs bruts d'une periode, sans appel a l'IA."""
    try:
        start, end = _bounds()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(review_service.gather(start, end))


@api_review_bp.get("/stats/hours")
def get_stats_hours():
    """Heures planifiees et suivies, jour par jour, sur la periode du bilan."""
    try:
        start, end = _bounds()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(plan_history.history(start, end))


@api_review_bp.get("/review")
def get_review():
    """Retourne le dernier bilan genere pour la periode demandee, ou None."""
    try:
        start, end = _bounds()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"review": review_service.get_for_period(start, end)})


@api_review_bp.get("/reviews")
def list_reviews():
    """Historique des bilans enregistres, le plus recent d'abord."""
    return jsonify({"reviews": review_service.list_reviews()})


@api_review_bp.get("/reviews/<int:review_id>")
def get_review_by_id(review_id):
    """Retourne un bilan de l'historique."""
    review = review_service.get_review(review_id)
    if review is None:
        return jsonify({"error": "Bilan introuvable."}), 404
    return jsonify({"review": review})


@api_review_bp.post("/review")
def create_review():
    """Genere un nouveau bilan via l'IA (appel long : 10 a 30 secondes)."""
    try:
        start, end = _bounds()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    try:
        return jsonify({"review": review_service.generate(start, end)}), 201
    except LLMError as error:
        return jsonify({"error": str(error)}), 503


@api_review_bp.get("/progress-report/managers")
def list_managers():
    """Chefs de projet : contacts qui suivent au moins un projet en cours."""
    return jsonify({"managers": progress_report.list_managers()})


@api_review_bp.get("/progress-report")
def get_progress_report():
    """Rapport d'avancement d'un chef de projet sur la periode, et son message WhatsApp."""
    manager_id = request.args.get("manager_id", type=int)
    if manager_id is None:
        return jsonify({"error": "Chef de projet attendu (manager_id)."}), 400
    try:
        start, end = _bounds()
        report = progress_report.build(manager_id, start, end)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"report": report, "message": progress_report.as_whatsapp(report)})
