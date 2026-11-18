"""Journees non travaillees : conges et jours feries.

Un jour de repos n'a ni heure ni repetition : c'est une plage de dates
entieres. Il ne retire pas du temps a la journee comme une reunion, il la
supprime : capacite nulle, aucun message proactif, et il ne compte pas dans
le delai restant avant une echeance.

Les feries importes portent leur pays (colonne country) : changer de pays
retire les feries futurs de l'ancien sans toucher aux saisies manuelles.

Un conge en attente de validation (colonne tentative, importe d'Odoo) est
affiche mais ne supprime rien : off_map l'ignore, la journee reste ouvree.
"""
import re
from datetime import datetime, timedelta

from flask import g, has_app_context

from models import get_db

VALID_KIND = ("leave", "holiday")

DAY_FORMAT = "%Y-%m-%d"
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_TITLE_CHARS = 200

# Plage maximale d'un seul conge. Au-dela, c'est une erreur de saisie (annee
# tapee de travers) plutot qu'un conge reel.
MAX_SPAN_DAYS = 366

# Cle du cache de requete : le score d'urgence interroge le calendrier pour
# chaque tache, une requete par tache serait un N+1.
_CACHE_KEY = "day_off_cache"


class DayOffError(ValueError):
    """Donnee de jour de repos refusee."""


def _refuse_if_imported(row):
    """Refuse l'ecriture d'un jour de repos importe d'un agenda externe.

    :raises DayOffError: la ligne porte une source
    """
    if row is not None and row["source_id"] is not None:
        raise DayOffError(
            "Absence importee d'un agenda externe : modifie-la a la source (Outlook ou Odoo)."
        )


def _parse_day(value, champ):
    """Convertit une date texte en date.

    :raises DayOffError: valeur absente ou mal formee
    """
    texte = (value or "").strip() if isinstance(value, str) else ""
    if not DAY_PATTERN.match(texte):
        raise DayOffError(f"{champ} attendu au format AAAA-MM-JJ.")
    try:
        return datetime.strptime(texte, DAY_FORMAT).date()
    except ValueError:
        raise DayOffError(f"{champ} n'est pas une date valide.")


def _clean(data, partial=False):
    """Valide et normalise les champs d'un jour de repos.

    :param partial: n'exige aucun champ, pour une mise a jour partielle
    :raises DayOffError: champ obligatoire absent ou valeur refusee
    """
    champs = {}

    if "title" in data or not partial:
        titre = (data.get("title") or "").strip()
        if not titre:
            raise DayOffError("Le titre est obligatoire.")
        champs["title"] = titre[:MAX_TITLE_CHARS]

    if "start_day" in data or not partial:
        champs["start_day"] = _parse_day(data.get("start_day"), "Le premier jour")

    # Un seul jour se saisit sans fin : la fin reprend alors le debut.
    if data.get("end_day") not in (None, ""):
        champs["end_day"] = _parse_day(data.get("end_day"), "Le dernier jour")
    elif not partial:
        champs["end_day"] = champs["start_day"]

    if "kind" in data or not partial:
        kind = (data.get("kind") or "leave").strip()
        if kind not in VALID_KIND:
            raise DayOffError(f"Type inconnu : {kind}. Attendu leave ou holiday.")
        champs["kind"] = kind

    return champs


def _check_span(debut, fin):
    """Verifie la coherence d'une plage de dates."""
    if fin < debut:
        raise DayOffError("Le dernier jour precede le premier.")
    if (fin - debut).days >= MAX_SPAN_DAYS:
        raise DayOffError(f"Un conge ne peut pas depasser {MAX_SPAN_DAYS} jours.")


def _to_dict(row):
    """Convertit une ligne en dictionnaire serialisable."""
    debut = datetime.strptime(row["start_day"], DAY_FORMAT).date()
    fin = datetime.strptime(row["end_day"], DAY_FORMAT).date()
    return {
        "id": row["id"],
        "title": row["title"],
        "start_day": row["start_day"],
        "end_day": row["end_day"],
        "kind": row["kind"],
        "country": row["country"],
        "days": (fin - debut).days + 1,
        "source_id": row["source_id"],
        "read_only": row["source_id"] is not None,
        "tentative": bool(row["tentative"]),
    }


def _invalidate():
    """Vide le cache de requete apres une ecriture."""
    if has_app_context():
        g.pop(_CACHE_KEY, None)


# --- Lecture ------------------------------------------------------------------


def list_between(start_day, end_day):
    """Retourne les jours de repos qui touchent une periode, tries par debut.

    :param start_day: date de debut incluse
    :param end_day: date de fin incluse
    """
    if end_day < start_day:
        raise DayOffError("La fin de periode precede le debut.")

    rows = get_db().execute(
        "SELECT * FROM day_off WHERE start_day <= ? AND end_day >= ?"
        " ORDER BY start_day, id",
        (end_day.isoformat(), start_day.isoformat()),
    ).fetchall()
    return [_to_dict(row) for row in rows]


def off_map(start_day, end_day):
    """Retourne, jour par jour, le repos qui couvre chaque date de la periode.

    Un jour couvert par deux entrees (ferie pendant un conge) garde la
    premiere : le motif affiche importe peu, le jour est chome de toute facon.
    Un conge en attente (tentative) est ignore : il ne bloque pas le plan.

    :return: dictionnaire {date: jour de repos}
    """
    cache = g.setdefault(_CACHE_KEY, {}) if has_app_context() else {}
    cle = (start_day, end_day)
    if cle in cache:
        return cache[cle]

    carte = {}
    for entree in list_between(start_day, end_day):
        if entree["tentative"]:
            continue
        jour = max(start_day, datetime.strptime(entree["start_day"], DAY_FORMAT).date())
        fin = min(end_day, datetime.strptime(entree["end_day"], DAY_FORMAT).date())
        while jour <= fin:
            carte.setdefault(jour, entree)
            jour += timedelta(days=1)

    cache[cle] = carte
    return carte


def get_day_off(day_off_id):
    """Retourne un jour de repos par son identifiant, ou None."""
    row = get_db().execute("SELECT * FROM day_off WHERE id = ?", (day_off_id,)).fetchone()
    return _to_dict(row) if row else None


# --- Ecriture -----------------------------------------------------------------


def create_day_off(data, country=None):
    """Cree un jour de repos et le retourne.

    :param data: champs title, start_day, end_day (facultatif), kind
    :param country: pays d'un ferie importe, None pour une saisie
    :raises DayOffError: donnee refusee
    """
    champs = _clean(data)
    _check_span(champs["start_day"], champs["end_day"])

    db = get_db()
    curseur = db.execute(
        "INSERT INTO day_off (title, start_day, end_day, kind, country) VALUES (?, ?, ?, ?, ?)",
        (
            champs["title"],
            champs["start_day"].isoformat(),
            champs["end_day"].isoformat(),
            champs["kind"],
            country,
        ),
    )
    db.commit()
    _invalidate()
    return get_day_off(curseur.lastrowid)


def update_day_off(day_off_id, data):
    """Met a jour un jour de repos. Retourne la version enregistree, ou None."""
    existant = get_db().execute("SELECT * FROM day_off WHERE id = ?", (day_off_id,)).fetchone()
    if existant is None:
        return None
    _refuse_if_imported(existant)

    champs = _clean(data, partial=True)
    if not champs:
        return get_day_off(day_off_id)

    debut = champs.get("start_day") or datetime.strptime(existant["start_day"], DAY_FORMAT).date()
    fin = champs.get("end_day") or datetime.strptime(existant["end_day"], DAY_FORMAT).date()
    _check_span(debut, fin)

    colonnes = []
    valeurs = []
    for nom, valeur in champs.items():
        colonnes.append(f"{nom} = ?")
        valeurs.append(valeur.isoformat() if hasattr(valeur, "isoformat") else valeur)

    valeurs.append(day_off_id)
    db = get_db()
    db.execute(f"UPDATE day_off SET {', '.join(colonnes)} WHERE id = ?", valeurs)
    db.commit()
    _invalidate()
    return get_day_off(day_off_id)


def delete_day_off(day_off_id):
    """Supprime un jour de repos. Retourne True si une ligne partait.

    :raises DayOffError: jour de repos importe d'un agenda externe
    """
    _refuse_if_imported(
        get_db().execute("SELECT source_id FROM day_off WHERE id = ?", (day_off_id,)).fetchone()
    )
    db = get_db()
    curseur = db.execute("DELETE FROM day_off WHERE id = ?", (day_off_id,))
    db.commit()
    _invalidate()
    return curseur.rowcount > 0


def add_holiday(title, day, country):
    """Enregistre un ferie importe, sauf s'il existe deja ce jour-la.

    :param day: date du ferie
    :param country: code pays de la source
    :return: True si le ferie est ajoute, False s'il etait deja present
    """
    db = get_db()
    deja = db.execute(
        "SELECT 1 FROM day_off WHERE kind = 'holiday' AND start_day = ? AND end_day = ?",
        (day.isoformat(), day.isoformat()),
    ).fetchone()
    if deja:
        return False

    db.execute(
        "INSERT INTO day_off (title, start_day, end_day, kind, country)"
        " VALUES (?, ?, ?, 'holiday', ?)",
        (title[:MAX_TITLE_CHARS], day.isoformat(), day.isoformat(), country),
    )
    db.commit()
    _invalidate()
    return True


def purge_foreign_holidays(country, from_day):
    """Retire les feries importes d'un autre pays, a partir d'un jour donne.

    Le passe n'est pas reecrit : un ferie deja chome reste dans l'historique.
    Les saisies manuelles (country NULL) ne sont jamais touchees.

    :return: nombre de lignes supprimees
    """
    db = get_db()
    curseur = db.execute(
        "DELETE FROM day_off WHERE country IS NOT NULL AND country != ? AND start_day >= ?",
        (country or "", from_day.isoformat()),
    )
    db.commit()
    _invalidate()
    return curseur.rowcount
