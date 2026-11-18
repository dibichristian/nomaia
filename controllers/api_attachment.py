"""Pieces jointes : /api/attachments (depot, liste, ouverture, suppression)."""
from flask import Blueprint, jsonify, request, send_from_directory

from models import attachment as attachment_model
from models.attachment import AttachmentError

api_attachment_bp = Blueprint("api_attachment", __name__, url_prefix="/api/attachments")


@api_attachment_bp.errorhandler(AttachmentError)
def handle_error(error):
    return jsonify({"error": str(error)}), 400


@api_attachment_bp.get("")
def list_attachments():
    """Pieces jointes d'un element : ?target=task|project|minutes|note&id=<id>."""
    target = request.args.get("target", "")
    if target not in attachment_model.TARGETS:
        raise AttachmentError("Cible inconnue : task, project, minutes ou note.")
    return jsonify({"attachments": attachment_model.list_for(target, request.args.get("id", type=int) or 0)})


@api_attachment_bp.post("")
def upload():
    """Depot multipart : champs target, id et file."""
    file = request.files.get("file")
    if file is None:
        raise AttachmentError("Aucun fichier envoyé.")
    item = attachment_model.save(request.form.get("target"), request.form.get("id"), file.filename,
                                 file.stream, file.mimetype)
    return jsonify(item), 201


@api_attachment_bp.get("/<int:attachment_id>/file")
def open_file(attachment_id):
    """Sert le fichier : affiche une image ou un PDF, force le telechargement de tout le reste."""
    item = attachment_model.get(attachment_id)
    if item is None:
        return jsonify({"error": "Pièce jointe introuvable."}), 404
    inline = attachment_model.is_inline(item) and request.args.get("download") is None
    response = send_from_directory(
        attachment_model.folder(), item["stored_name"],
        mimetype=item["content_type"] if inline else "application/octet-stream",
        as_attachment=not inline, download_name=item["filename"],
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@api_attachment_bp.delete("/<int:attachment_id>")
def delete(attachment_id):
    if attachment_model.delete(attachment_id):
        return jsonify({"deleted": attachment_id})
    return jsonify({"error": "Pièce jointe introuvable."}), 404
