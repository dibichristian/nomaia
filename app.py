"""Point d'entree de l'application NOMAIA (serveur local uniquement)."""
from flask import Flask, jsonify, request

import logging

import config
from controllers.api_agent import api_agent_bp
from controllers.api_attachment import api_attachment_bp
from controllers.api_calendar import api_calendar_bp
from controllers.api_capture import api_capture_bp
from controllers.api_chat import api_chat_bp
from controllers.api_day_off import api_day_off_bp
from controllers.api_event import api_event_bp
from controllers.api_knowledge import api_knowledge_bp
from controllers.api_live import api_live_bp
from controllers.api_mail import api_mail_bp
from controllers.api_minutes import api_minutes_bp
from controllers.api_odoo import api_odoo_bp
from controllers.api_person import api_person_bp
from controllers.api_plan import api_plan_bp
from controllers.api_project import api_project_bp
from controllers.api_review import api_review_bp
from controllers.api_search import api_search_bp
from controllers.api_settings import api_settings_bp
from controllers.api_task import api_task_bp
from controllers.main import main_bp
from controllers.security import attach_cookie, check_request_origin
from models import backup_db, close_db, init_schema, setting, vacuum
from services.heartbeat import Heartbeat
from services.telegram import TelegramBridge
from services.worker import TriageWorker


# Routes dont une ecriture change ce que l'utilisateur verra sur son telephone.
# Un changement de reglage, lui, ne concerne pas l'instantane epingle.
DIGEST_PATHS = (
    "/api/tasks",
    "/api/projects",
    "/api/capture",
    "/api/proposal",
    "/api/events",
    "/api/waiting",
    "/api/agent",
    "/api/mail",
)


def flag_telegram_digest(response):
    """Marque l'instantane Telegram a rafraichir apres une ecriture reussie.

    Aucun appel reseau ici : la requete HTTP doit rester instantanee. Le
    battement de coeur pousse la mise a jour dans les secondes qui suivent.
    """
    if (
        request.method in ("POST", "PATCH", "PUT", "DELETE")
        and response.status_code < 400
        and request.path.startswith(DIGEST_PATHS)
    ):
        from flask import current_app

        bridge = current_app.extensions.get("telegram")
        if bridge:
            bridge.mark_digest_stale()
    return response


def create_app(start_worker=True):
    """Construit et configure l'application Flask.

    :param start_worker: demarre le thread de triage (desactive pour les tests)
    """
    config.ensure_data_dirs()
    # Meme journal qu'en lance par tray.py : un echec de moteur doit laisser sa
    # trace complete dans le fichier, pas seulement dans une console fermee
    # depuis longtemps. basicConfig ne fait rien si tray.py l'a deja appele.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
        filename=str(config.LOG_PATH),
        encoding="utf-8",
    )

    app = Flask(
        __name__,
        static_folder=str(config.STATIC_DIR),
        template_folder=str(config.TEMPLATE_DIR),
    )
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_IMAGE_BYTES + 1024 * 1024

    config.ensure_data_dirs()

    init_schema()
    backup_db()

    with app.app_context():
        # Pieces jointes dont la cible a ete supprimee : fichiers effaces.
        from models import attachment as attachment_model

        attachment_model.sweep_orphans()
        # Bases anterieures : les secrets encore en clair sont chiffres ici.
        encrypted = setting.encrypt_legacy_secrets()
        setting.ensure_access_key()

    if encrypted:
        # Hors contexte applicatif : VACUUM exige une connexion sans
        # transaction ouverte. Purge les anciennes valeurs en clair.
        vacuum()
        logging.getLogger("nomaia").info(
            "Secrets chiffres au repos et pages liberees purgees : %s",
            ", ".join(encrypted),
        )

    @app.context_processor
    def shell_context():
        """Nom affiche en bas de la barre laterale, et ses initiales."""
        name = (setting.get("display_name") or "").strip() or "NOMAIA"
        initials = "".join(word[0] for word in name.split()[:2]).upper()
        return {
            "display_name": name,
            "display_initials": initials,
            "space_filter": setting.space_filter() or "all",
            "create_space": setting.create_space(),
        }

    # Controle d'acces et garde-fou CSRF, avant toute route.
    app.before_request(check_request_origin)
    app.after_request(attach_cookie)
    app.after_request(flag_telegram_digest)
    app.teardown_appcontext(close_db)
    app.register_blueprint(main_bp)
    app.register_blueprint(api_task_bp)
    app.register_blueprint(api_agent_bp)
    app.register_blueprint(api_attachment_bp)
    app.register_blueprint(api_capture_bp)
    app.register_blueprint(api_calendar_bp)
    app.register_blueprint(api_chat_bp)
    app.register_blueprint(api_day_off_bp)
    app.register_blueprint(api_event_bp)
    app.register_blueprint(api_knowledge_bp)
    app.register_blueprint(api_live_bp)
    app.register_blueprint(api_mail_bp)
    app.register_blueprint(api_minutes_bp)
    app.register_blueprint(api_odoo_bp)
    app.register_blueprint(api_person_bp)
    app.register_blueprint(api_plan_bp)
    app.register_blueprint(api_project_bp)
    app.register_blueprint(api_review_bp)
    app.register_blueprint(api_search_bp)
    app.register_blueprint(api_settings_bp)

    # La passerelle est enregistree avant le worker : celui-ci lui renvoie
    # le resultat de chaque triage issu du telephone.
    bridge = TelegramBridge(app)
    app.extensions["telegram"] = bridge

    worker = TriageWorker(app)
    app.extensions["triage_worker"] = worker

    heartbeat = Heartbeat(app)
    app.extensions["heartbeat"] = heartbeat

    if start_worker:
        worker.start()
        bridge.start()
        heartbeat.start()

    @app.errorhandler(404)
    def not_found(error):
        """Reponse JSON pour les routes API inexistantes."""
        return jsonify({"error": "Ressource introuvable."}), 404

    @app.errorhandler(413)
    def too_large(error):
        """Fichier depose plus lourd que la limite autorisee."""
        limite = config.MAX_IMAGE_BYTES // (1024 * 1024)
        return jsonify({"error": f"Fichier trop lourd ({limite} Mo maximum)."}), 413

    return app


if __name__ == "__main__":
    # L'application n'est JAMAIS construite a l'import : tray.py importe ce
    # module, et une seconde instance lancerait un deuxieme worker et un
    # deuxieme sondeur Telegram, qui se voleraient les messages du bot.
    #
    # waitress plutot que le serveur de developpement de Flask : meme serveur
    # qu'en usage normal via tray.py, donc meme comportement.
    from waitress import serve

    serve(create_app(), host=config.HOST, port=config.PORT, threads=8, ident="NOMAIA")
