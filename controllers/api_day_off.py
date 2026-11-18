"""API JSON des jours de repos : conges, feries et calendrier ouvre."""
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from models import day_off as day_off_model
from models import setting
from models.day_off import DayOffError
from services import holidays, workdays

api_day_off_bp = Blueprint("api_day_off", __name__, url_prefix="/api")

# Largeur maximale d'une periode consultable : une annee et quelques jours,
# de quoi afficher un calendrier annuel sans produire un calendrier sans fin.
MAX_RANGE_DAYS = 400


def _day_arg(nom, defaut):
    """Lit une date en parametre d'URL, avec repli."""
    brut = (request.args.get(nom) or "").strip()
    if not brut:
        return defaut
    try:
        return datetime.strptime(brut, "%Y-%m-%d").date()
    except ValueError:
        raise DayOffError(f"Parametre {nom} attendu au format AAAA-MM-JJ.")


@api_day_off_bp.get("/days-off")
def list_days_off():
    """Retourne les jours de repos d'une periode et le calendrier jour par jour.

    Periode par defaut : l'annee civile en cours.
    """
    annee = date.today().year

    try:
        debut = _day_arg("start", date(annee, 1, 1))
        fin = _day_arg("end", date(annee, 12, 31))

        if fin < debut:
            raise DayOffError("La fin de periode precede le debut.")
        if (fin - debut).days > MAX_RANGE_DAYS:
            raise DayOffError(f"Periode trop large ({MAX_RANGE_DAYS} jours au maximum).")

        entrees = day_off_model.list_between(debut, fin)
    except DayOffError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(
        {
            "start": debut.isoformat(),
            "end": fin.isoformat(),
            "days_off": entrees,
            "calendar": workdays.calendar(debut, fin),
        }
    )


@api_day_off_bp.post("/days-off")
def create_day_off():
    """Cree un conge ou un ferie saisi a la main."""
    try:
        entree = day_off_model.create_day_off(request.get_json(silent=True) or {})
    except DayOffError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify({"day_off": entree}), 201


@api_day_off_bp.patch("/days-off/<int:day_off_id>")
def update_day_off(day_off_id):
    """Met a jour un jour de repos."""
    try:
        entree = day_off_model.update_day_off(day_off_id, request.get_json(silent=True) or {})
    except DayOffError as error:
        return jsonify({"error": str(error)}), 400

    if entree is None:
        return jsonify({"error": "Jour de repos introuvable."}), 404

    return jsonify({"day_off": entree})


@api_day_off_bp.delete("/days-off/<int:day_off_id>")
def delete_day_off(day_off_id):
    """Supprime un jour de repos."""
    try:
        trouve = day_off_model.delete_day_off(day_off_id)
    except DayOffError as error:
        return jsonify({"error": str(error)}), 400
    if not trouve:
        return jsonify({"error": "Jour de repos introuvable."}), 404
    return jsonify({"ok": True})


@api_day_off_bp.post("/days-off/holidays")
def import_holidays():
    """Importe les feries d'une annee pour le pays configure.

    Sert a reprendre un ferie supprime par erreur : l'import automatique,
    lui, ne repasse jamais sur une annee deja faite.
    """
    charge = request.get_json(silent=True) or {}
    pays = setting.get("holiday_country") or ""
    if pays not in holidays.COUNTRIES:
        return jsonify({"error": "Aucun pays configure pour les feries."}), 400

    try:
        annee = int(charge.get("year") or date.today().year)
    except (TypeError, ValueError):
        return jsonify({"error": "Annee invalide."}), 400
    if not 2000 <= annee <= 2100:
        return jsonify({"error": "Annee hors plage (2000 a 2100)."}), 400

    ajoutes = holidays.import_year(annee, pays)
    return jsonify({"added": ajoutes, "country": pays, "year": annee})
