"""API JSON de la boite de reception : captures, dialogue, propositions."""
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import config
from models import capture as capture_model
from services import llm, mail_parse, triage

api_capture_bp = Blueprint("api_capture", __name__, url_prefix="/api")

# Liste de la boite de reception : taille d'une page et plafond, le sondage
# relisant toute la liste affichee a chaque passage.
LIST_PAGE = 30
LIST_MAX_LIMIT = 300


def _save_image(storage):
    """Enregistre une image de capture et retourne son chemin absolu.

    :raises ValueError: extension refusee ou fichier trop lourd
    """
    name = secure_filename(storage.filename or "capture.png")
    extension = Path(name).suffix.lower()
    if extension not in config.ALLOWED_IMAGE_EXT:
        raise ValueError(f"Format d'image non accepte : {extension or 'inconnu'}.")

    storage.stream.seek(0, 2)
    size = storage.stream.tell()
    storage.stream.seek(0)
    if size > config.MAX_IMAGE_BYTES:
        raise ValueError("Image trop lourde (10 Mo maximum).")
    if size == 0:
        raise ValueError("Image vide.")

    folder = config.UPLOAD_DIR / datetime.now().strftime("%Y-%m")
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = folder / f"{stamp}_{uuid.uuid4().hex[:8]}{extension}"
    storage.save(target)
    return str(target)


@api_capture_bp.post("/capture")
def create_capture():
    """Depose une entree brute : texte, image, ou les deux.

    Accepte du JSON (`{"text": "..."}`) ou un envoi multipart avec un champ
    `image`. Retourne immediatement : le triage se fait en arriere-plan.
    """
    image_path = None

    if request.files.get("image"):
        try:
            image_path = _save_image(request.files["image"])
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        raw_text = request.form.get("text")
        source = request.form.get("source") or "web"
        space = request.form.get("space") or None
    else:
        payload = request.get_json(silent=True) or {}
        raw_text = payload.get("text")
        source = payload.get("source") or "web"
        space = payload.get("space") or None

    try:
        # Meme convention que sur Telegram : /mail devant un texte colle.
        mots = (raw_text or "").split(None, 1)
        if not image_path and mots and mots[0].lower() == "/mail":
            raw_text = mail_parse.pasted_text(mots[1] if len(mots) > 1 else "")
            source = "email"
        capture = capture_model.create_capture(
            raw_text=raw_text, image_path=image_path, source=source, space=space
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(capture), 201


@api_capture_bp.post("/capture/eml")
def create_mail_capture():
    """Depose un mail exporte en .eml (champ multipart `eml`).

    Un champ `text` facultatif porte une precision de l'utilisateur. Le mail est
    lu en memoire et seul son texte est conserve : le fichier n'est pas garde.
    """
    storage = request.files.get("eml")
    if storage is None or not mail_parse.is_eml(storage.filename, storage.mimetype):
        return jsonify({"error": "Joins un fichier .eml."}), 400

    try:
        mail = mail_parse.parse_eml(storage.read(mail_parse.MAX_EML_BYTES + 1))
        capture = capture_model.create_capture(
            raw_text=mail_parse.capture_text(mail, request.form.get("text")),
            source="email",
            space=request.form.get("space") or None,
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(capture), 201


@api_capture_bp.get("/capture")
def list_captures():
    """Liste les captures recentes avec leur derniere proposition.

    Parametres : `limit` (30 par defaut, plafonne a LIST_MAX_LIMIT) et
    `filter` ('open' : entrees a traiter seulement, anciennes comprises ;
    sinon toutes). `has_more` signale des entrees au-dela de la limite.
    """
    try:
        limit = int(request.args.get("limit") or LIST_PAGE)
    except ValueError:
        limit = LIST_PAGE
    limit = max(1, min(limit, LIST_MAX_LIMIT))
    open_only = request.args.get("filter") == "open"

    # Une ligne de plus que demande : sa presence dit s'il reste des entrees.
    rows = capture_model.list_captures(limit=limit + 1, open_only=open_only)
    captures = rows[:limit]
    proposals = capture_model.latest_proposals(item["id"] for item in captures)
    for item in captures:
        item["proposal"] = proposals.get(item["id"])
    return jsonify(
        {
            "captures": captures,
            "limit": limit,
            "has_more": len(rows) > limit,
            "open_count": capture_model.count_open_captures(),
        }
    )


@api_capture_bp.get("/capture/<int:capture_id>")
def get_capture(capture_id):
    """Retourne le fil complet d'une capture : dialogue, proposition, taches."""
    thread = triage.capture_thread(capture_id)
    if thread is None:
        return jsonify({"error": "Capture introuvable."}), 404
    return jsonify(thread)


@api_capture_bp.post("/capture/<int:capture_id>/answer")
def answer_capture(capture_id):
    """Enregistre une reponse aux questions de NOMAIA et relance le triage."""
    if capture_model.get_capture(capture_id) is None:
        return jsonify({"error": "Capture introuvable."}), 404

    answer = (request.get_json(silent=True) or {}).get("answer")
    try:
        triage.answer_capture(capture_id, answer)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(triage.capture_thread(capture_id))


@api_capture_bp.post("/capture/<int:capture_id>/dismiss")
def dismiss_capture(capture_id):
    """Classe une entree sans suite : rien n'est cree, sa proposition eventuelle est rejetee.

    Seule issue pour une entree sans proposition de taches (salutation, question
    restee ouverte) autre que repondre. L'entree reste lisible dans « Tout ».
    """
    capture = capture_model.get_capture(capture_id)
    if capture is None:
        return jsonify({"error": "Capture introuvable."}), 404
    if capture["status"] not in ("waiting", "failed"):
        return jsonify({"error": "Seule une entree en attente ou en echec se classe sans suite."}), 409

    capture_model.add_message(capture_id, "user", "Classée sans suite.")
    capture_model.set_status(capture_id, "done")
    return jsonify(triage.capture_thread(capture_id))


@api_capture_bp.post("/proposal/<int:proposal_id>/apply")
def apply_proposal(proposal_id):
    """Valide manuellement une proposition et cree les taches correspondantes."""
    proposal = capture_model.get_proposal(proposal_id)
    if proposal is None:
        return jsonify({"error": "Proposition introuvable."}), 404
    if proposal["status"] != "pending":
        return jsonify({"error": "Proposition deja traitee."}), 409

    tasks = triage.apply_proposal(proposal, needs_review=False)
    capture_model.set_status(proposal["capture_id"], "done")
    return jsonify({"tasks": tasks})


@api_capture_bp.post("/proposal/<int:proposal_id>/reject")
def reject_proposal(proposal_id):
    """Rejette une proposition sans creer de tache."""
    proposal = capture_model.get_proposal(proposal_id)
    if proposal is None:
        return jsonify({"error": "Proposition introuvable."}), 404

    capture_model.set_proposal_status(proposal_id, "rejected")
    capture_model.set_status(proposal["capture_id"], "done")
    return jsonify({"rejected": proposal_id})


@api_capture_bp.get("/status")
def status():
    """Etat du moteur de langage et du worker de triage."""
    worker = current_app.extensions.get("triage_worker")
    bridge = current_app.extensions.get("telegram")
    heartbeat = current_app.extensions.get("heartbeat")

    telegram_state = {"ok": False, "detail": "Passerelle absente."}
    if bridge:
        telegram_state = bridge.check()
        telegram_state["running"] = bridge.is_running()

    return jsonify(
        {
            "llm": llm.check_backend(),
            "worker_running": bool(worker and worker.is_running()),
            "worker_error": worker.last_error if worker else None,
            "telegram": telegram_state,
            "heartbeat_running": bool(heartbeat and heartbeat.is_running()),
            # Entrees a traiter (captures non closes), pas les propositions.
            "inbox_pending": capture_model.count_open_captures(),
        }
    )


@api_capture_bp.post("/window/show")
def show_window():
    """Ramene la fenetre native au premier plan.

    Appelee par une seconde instance de NOMAIA : plutot que de demarrer un
    deuxieme serveur, elle reveille la fenetre de celle qui tourne deja.
    """
    show = current_app.extensions.get("show_window")
    if show is None:
        return jsonify({"error": "Aucune fenetre native disponible."}), 409

    show()
    return jsonify({"shown": True})


@api_capture_bp.get("/uploads/<path:relative_path>")
def serve_upload(relative_path):
    """Sert une image de capture depuis public/uploads."""
    return send_from_directory(config.UPLOAD_DIR, relative_path)
