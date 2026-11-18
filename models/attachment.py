"""Pieces jointes : fichiers rattaches a une tache, un projet, un compte rendu ou une note.

Les fichiers vivent dans public/uploads/attachments sous un nom aleatoire ;
le nom d'origine n'est garde qu'en base. Seuls les images et les PDF sont
servis pour affichage : tout autre type est force en telechargement, afin
qu'un fichier HTML ou SVG depose ne s'execute jamais dans l'interface.
"""
import mimetypes
import uuid
from pathlib import Path

import config
from models import get_db

TARGETS = {"task": "task", "project": "project", "minutes": "minutes", "note": "kb_note"}
MAX_BYTES = config.MAX_IMAGE_BYTES
INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf")
MAX_PER_TARGET = 50


class AttachmentError(ValueError):
    """Depot de piece jointe refuse."""


def folder():
    path = Path(config.UPLOAD_DIR) / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_target(target_type, target_id):
    table = TARGETS.get(target_type)
    if table is None:
        raise AttachmentError("Cible inconnue : task, project, minutes ou note.")
    try:
        target_id = int(target_id)
    except (TypeError, ValueError):
        raise AttachmentError("Identifiant de cible invalide.") from None
    if get_db().execute(f"SELECT 1 FROM {table} WHERE id = ?", (target_id,)).fetchone() is None:
        raise AttachmentError("Élément introuvable.")
    return target_id


def _clean_filename(name):
    """Nom d'affichage sur : sans chemin ni caracteres de controle, 150 caracteres au plus."""
    base = Path(str(name or "fichier")).name.replace("\\", "_")
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"/|?*').strip(" .")
    return (base or "fichier")[:150]


def list_for(target_type, target_id):
    rows = get_db().execute(
        "SELECT id, filename, content_type, size, created_at FROM attachment"
        " WHERE target_type = ? AND target_id = ? ORDER BY id DESC",
        (target_type, int(target_id)),
    ).fetchall()
    return [dict(row) for row in rows]


def get(attachment_id):
    row = get_db().execute("SELECT * FROM attachment WHERE id = ?", (attachment_id,)).fetchone()
    return dict(row) if row else None


def save(target_type, target_id, filename, stream, content_type=None):
    """Enregistre un fichier depose ; renvoie la piece jointe.

    :raises AttachmentError: cible inconnue, fichier vide ou trop lourd, trop de fichiers
    """
    target_id = _check_target(target_type, target_id)
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM attachment WHERE target_type = ? AND target_id = ?",
                       (target_type, target_id)).fetchone()[0]
    if count >= MAX_PER_TARGET:
        raise AttachmentError(f"{MAX_PER_TARGET} pièces jointes au plus par élément.")
    data = stream.read(MAX_BYTES + 1)
    if not data:
        raise AttachmentError("Fichier vide.")
    if len(data) > MAX_BYTES:
        raise AttachmentError(f"Fichier trop lourd ({MAX_BYTES // (1024 * 1024)} Mo maximum).")
    name = _clean_filename(filename)
    guessed = mimetypes.guess_type(name)[0]
    kind = (content_type or guessed or "application/octet-stream").split(";")[0].strip().lower()[:100]
    suffix = Path(name).suffix.lower()[:10] if Path(name).suffix.isascii() else ""
    stored = uuid.uuid4().hex + suffix
    (folder() / stored).write_bytes(data)
    cursor = db.execute(
        "INSERT INTO attachment (target_type, target_id, filename, stored_name, content_type, size)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (target_type, target_id, name, stored, kind, len(data)),
    )
    db.commit()
    return {key: value for key, value in get(cursor.lastrowid).items() if key != "stored_name"}


def delete(attachment_id):
    item = get(attachment_id)
    if item is None:
        return False
    db = get_db()
    db.execute("DELETE FROM attachment WHERE id = ?", (attachment_id,))
    db.commit()
    (folder() / item["stored_name"]).unlink(missing_ok=True)
    return True


def is_inline(item):
    """Affichable dans le navigateur (image, PDF) ; sinon telechargement force."""
    return item["content_type"] in INLINE_TYPES


def sweep_orphans():
    """Efface les fichiers sans ligne en base (cible supprimee) ; renvoie leur nombre."""
    known = {row[0] for row in get_db().execute("SELECT stored_name FROM attachment")}
    removed = 0
    for path in folder().iterdir():
        if path.is_file() and path.name not in known:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
