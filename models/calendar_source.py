"""Agendas externes relus en ICS : une ligne par flux publie.

L'URL d'un flux publie Outlook donne acces a tout l'agenda, sans mot de
passe : c'est un secret. Elle est chiffree au repos (DPAPI), jamais renvoyee
par l'API (seulement un indice), jamais ecrite au journal, et purgee des
sauvegardes.

Une source de nature 'odoo' n'a pas d'URL (colonne vide) : sa connexion est
celle des reglages Odoo (Config > Connexions > Taches Odoo). Une seule
source Odoo peut exister.
"""
from urllib.parse import urlsplit

from models import get_db, secret

MAX_NAME_CHARS = 80
MAX_URL_CHARS = 2000

KIND_ICS = "ics"
KIND_ODOO = "odoo"


class SourceError(ValueError):
    """Donnee de source d'agenda refusee."""


def normalize_url(value):
    """Valide une URL de flux ICS et la ramene en https.

    webcal:// est le schema d'abonnement propose par Outlook : c'est du https
    sous un autre nom. Le http en clair est refuse, l'URL etant un secret.

    :raises SourceError: URL absente, trop longue ou d'un autre schema
    """
    texte = (value or "").strip()
    if not texte:
        raise SourceError("L'adresse du calendrier est obligatoire.")
    if len(texte) > MAX_URL_CHARS:
        raise SourceError("Adresse du calendrier trop longue.")

    if texte.lower().startswith("webcal://"):
        texte = "https://" + texte[len("webcal://"):]

    morceaux = urlsplit(texte)
    if morceaux.scheme.lower() != "https" or not morceaux.netloc:
        raise SourceError("Adresse attendue en https:// ou webcal://.")
    return texte


def url_hint(url):
    """Indice affichable d'une URL secrete : l'hote et ses derniers caracteres."""
    if not url:
        return ""
    return f"{urlsplit(url).netloc}/…{url[-6:]}"


def _to_dict(row):
    """Convertit une ligne en dictionnaire serialisable, URL masquee."""
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"] or KIND_ICS,
        "enabled": bool(row["enabled"]),
        "url_hint": url_hint(secret.unprotect(row["url"])),
        "last_sync_at": row["last_sync_at"],
        "last_error": row["last_error"],
        "last_count": row["last_count"],
        "space": row["space"] if "space" in row.keys() else "pro",
    }


def _clean_name(value):
    """Valide le nom affiche d'une source."""
    nom = (value or "").strip()
    if not nom:
        raise SourceError("Le nom du calendrier est obligatoire.")
    return nom[:MAX_NAME_CHARS]


# --- Lecture ------------------------------------------------------------------


def list_sources():
    """Retourne toutes les sources, URL masquees."""
    rows = get_db().execute("SELECT * FROM calendar_source ORDER BY id").fetchall()
    return [_to_dict(row) for row in rows]


def get_source(source_id):
    """Retourne une source, URL masquee, ou None."""
    row = get_db().execute("SELECT * FROM calendar_source WHERE id = ?", (source_id,)).fetchone()
    return _to_dict(row) if row else None


def get_url(source_id):
    """Retourne l'URL dechiffree d'une source, ou une chaine vide."""
    row = get_db().execute(
        "SELECT url FROM calendar_source WHERE id = ?", (source_id,)
    ).fetchone()
    return secret.unprotect(row["url"]) if row else ""


def odoo_source():
    """Retourne la source Odoo, ou None s'il n'y en a pas."""
    row = get_db().execute(
        "SELECT * FROM calendar_source WHERE kind = ? ORDER BY id LIMIT 1", (KIND_ODOO,)
    ).fetchone()
    return _to_dict(row) if row else None


def enabled_ids():
    """Retourne les identifiants des sources actives."""
    rows = get_db().execute("SELECT id FROM calendar_source WHERE enabled = 1").fetchall()
    return [row["id"] for row in rows]


# --- Ecriture -----------------------------------------------------------------


def create_source(data):
    """Cree une source et la retourne.

    :param data: champs name et url
    :raises SourceError: donnee refusee
    """
    nom = _clean_name(data.get("name"))
    url = normalize_url(data.get("url"))

    db = get_db()
    curseur = db.execute(
        "INSERT INTO calendar_source (name, url) VALUES (?, ?)",
        (nom, secret.protect(url)),
    )
    db.commit()
    return get_source(curseur.lastrowid)


def create_odoo_source(name="Odoo"):
    """Cree la source de l'agenda Odoo et la retourne.

    :raises SourceError: une source Odoo existe deja
    """
    if odoo_source() is not None:
        raise SourceError("L'agenda Odoo est deja relie.")

    db = get_db()
    curseur = db.execute(
        "INSERT INTO calendar_source (name, url, kind) VALUES (?, '', ?)",
        (_clean_name(name), KIND_ODOO),
    )
    db.commit()
    return get_source(curseur.lastrowid)


def update_source(source_id, data):
    """Met a jour une source. Retourne la version enregistree, ou None.

    Une URL vide est ignoree : le formulaire ne renvoie jamais l'URL secrete,
    un champ laisse vide signifie « inchange », pas « efface ».
    """
    existante = get_source(source_id)
    if existante is None:
        return None

    champs = {}
    if "name" in data:
        champs["name"] = _clean_name(data.get("name"))
    # Une source Odoo n'a pas d'URL : sa connexion vient des reglages Odoo.
    if existante["kind"] != KIND_ODOO and (data.get("url") or "").strip():
        champs["url"] = secret.protect(normalize_url(data.get("url")))
    if "enabled" in data:
        champs["enabled"] = 1 if data.get("enabled") in (True, 1, "1", "true") else 0
    if data.get("space"):
        # Espace des evenements importes ; l'agenda Odoo reste professionnel.
        espace = (data.get("space") or "").strip().lower()
        if espace not in ("pro", "perso") or (existante["kind"] == KIND_ODOO and espace != "pro"):
            raise SourceError("Espace invalide pour ce calendrier.")
        champs["space"] = espace

    if champs:
        colonnes = ", ".join(f"{nom} = ?" for nom in champs)
        db = get_db()
        db.execute(
            f"UPDATE calendar_source SET {colonnes} WHERE id = ?",
            (*champs.values(), source_id),
        )
        if "space" in champs:
            # Les evenements deja importes changent d'espace avec leur calendrier.
            db.execute("UPDATE event SET space = ? WHERE source_id = ?", (champs["space"], source_id))
        # Desactiver retire ce que la source a importe : laisser des reunions
        # figees, que plus rien ne met a jour, bloquerait du temps a tort.
        # La reactivation les reimporte.
        if champs.get("enabled") == 0:
            _purge_imported(db, source_id)
        db.commit()
    return get_source(source_id)


def _purge_imported(db, source_id):
    """Retire tout ce qu'une source a importe (transaction de l'appelant).

    La suppression est explicite : la cascade de cle etrangere ne s'applique
    qu'aux colonnes creees avec elle, pas forcement a celles ajoutees par
    migration sur une base ancienne.
    """
    db.execute("DELETE FROM event WHERE source_id = ?", (source_id,))
    db.execute("DELETE FROM day_off WHERE source_id = ?", (source_id,))


def delete_source(source_id):
    """Supprime une source et tout ce qu'elle a importe.

    :return: True si une source partait
    """
    db = get_db()
    _purge_imported(db, source_id)
    curseur = db.execute("DELETE FROM calendar_source WHERE id = ?", (source_id,))
    db.commit()
    return curseur.rowcount > 0


def record_sync(source_id, when, count=None, error=None):
    """Enregistre l'issue d'une synchronisation.

    Un echec ne touche pas au compteur : les lignes precedentes sont gardees,
    le nombre affiche doit donc rester le leur.
    """
    db = get_db()
    if error is None:
        db.execute(
            "UPDATE calendar_source SET last_sync_at = ?, last_error = NULL, last_count = ?"
            " WHERE id = ?",
            (when, count or 0, source_id),
        )
    else:
        db.execute(
            "UPDATE calendar_source SET last_sync_at = ?, last_error = ? WHERE id = ?",
            (when, error[:300], source_id),
        )
    db.commit()
