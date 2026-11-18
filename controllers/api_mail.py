"""API JSON de la lecture des mails Outlook (via le connecteur Claude)."""
from flask import Blueprint, jsonify

from services import mail_reader

api_mail_bp = Blueprint("api_mail", __name__, url_prefix="/api/mail")


@api_mail_bp.get("/status")
def get_status():
    """Etat de la lecture : activation, dernier passage, derniere erreur."""
    return jsonify(mail_reader.status())


@api_mail_bp.post("/read")
def read_now():
    """Lit les mails tout de suite et rend le nombre d'entrees deposees.

    Appel long (jusqu'a quelques minutes) : la requete attend la fin du passage.
    """
    resultat = mail_reader.read_mails()
    if resultat.get("refused"):
        return jsonify(resultat), 400
    if resultat.get("busy"):
        return jsonify(resultat), 409
    if not resultat.get("ok"):
        return jsonify(resultat), 502
    return jsonify(resultat)
