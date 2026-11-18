"""Synchronisation des agendas externes publies en ICS (Outlook).

Sens unique : l'agenda externe fait foi, NOMAIA en garde une copie en lecture
seule sur une fenetre glissante. Chaque synchronisation remplace, dans une
seule transaction, tout ce que la source avait importe sur la fenetre : une
reunion supprimee dans Outlook disparait, une reunion deplacee bouge.

Les repetitions sont developpees par recurring-ical-events, pas par le moteur
de l'agenda : la grammaire RRULE d'Outlook (exceptions, occurrences deplacees,
fuseaux Windows) est bien plus riche que les series quotidiennes et
hebdomadaires de NOMAIA. Chaque occurrence devient une ligne simple.

Un echec (reseau, flux illisible) est enregistre sur la source et laisse les
lignes precedentes intactes : une coupure ne doit pas vider l'agenda.
"""
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from models import calendar_source as source_model
from models import get_db, setting

logger = logging.getLogger("nomaia.calendar_sync")

# Fenetre synchronisee : assez de passe pour le bilan de la semaine, assez
# d'avenir pour les echeances lointaines du plan.
WINDOW_PAST_DAYS = 7
WINDOW_FUTURE_DAYS = 60

FETCH_TIMEOUT_S = 20
MAX_FEED_BYTES = 5 * 1024 * 1024

STAMP_FORMAT = "%Y-%m-%d %H:%M"

# Statuts Outlook (X-MICROSOFT-CDO-BUSYSTATUS) qui ne bloquent aucun temps.
FREE_STATUSES = ("FREE", "WORKINGELSEWHERE")
OUT_OF_OFFICE = "OOF"


class SyncError(RuntimeError):
    """Flux injoignable ou illisible."""


def window(today=None):
    """Retourne la fenetre synchronisee (premier jour, dernier jour inclus)."""
    today = today or date.today()
    return today - timedelta(days=WINDOW_PAST_DAYS), today + timedelta(days=WINDOW_FUTURE_DAYS)


# --- Telechargement -------------------------------------------------------------


def fetch(url):
    """Telecharge un flux ICS.

    L'URL n'apparait dans aucun message d'erreur : seul l'hote est cite.

    :return: contenu brut du flux
    :raises SyncError: reseau en echec, reponse HTTP en erreur, flux trop lourd
    """
    hote = urlsplit(url).netloc
    requete = urllib.request.Request(url, headers={"User-Agent": "NOMAIA/1.0"})
    try:
        with urllib.request.urlopen(requete, timeout=FETCH_TIMEOUT_S) as reponse:
            contenu = reponse.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise SyncError(f"{hote} a repondu HTTP {error.code}.")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SyncError(f"{hote} injoignable : {getattr(error, 'reason', error)}.")

    if len(contenu) > MAX_FEED_BYTES:
        raise SyncError("Flux trop lourd (5 Mo maximum).")
    return contenu


# --- Analyse --------------------------------------------------------------------


def _local(moment):
    """Ramene un datetime ICS en heure locale naive, format de stockage de NOMAIA.

    Un horaire sans fuseau (« flottant ») est deja local par definition.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.replace(second=0, microsecond=0)


def _text(component, key):
    """Lit une propriete texte, vide si absente."""
    valeur = component.get(key)
    return str(valeur).strip() if valeur is not None else ""


# Longueur maximale d'une description importee, alignee sur les notes saisies.
MAX_NOTES_CHARS = 2000


def _description(component):
    """Lit la description d'un evenement : ordre du jour, lien de visio.

    Outlook l'emplit souvent de lignes vides et d'espaces insecables ; elles
    sont resserrees pour que la fiche reste lisible.
    """
    lignes = [ligne.replace("\u00a0", " ").rstrip() for ligne in _text(component, "DESCRIPTION").splitlines()]
    resserrees = []
    for ligne in lignes:
        if ligne or (resserrees and resserrees[-1]):
            resserrees.append(ligne)
    texte = "\n".join(resserrees).strip()
    return texte[:MAX_NOTES_CHARS] or None


def _split_by_day(debut, fin):
    """Coupe un intervalle aux minuits : l'agenda ne range qu'un jour par ligne.

    Une reunion de 23 h a 1 h, rangee sur son seul premier jour, ne retirerait
    rien a la matinee du lendemain.
    """
    morceaux = []
    curseur = debut
    while curseur < fin:
        minuit = datetime.combine(curseur.date() + timedelta(days=1), datetime.min.time())
        borne = min(fin, minuit)
        if borne > curseur:
            morceaux.append((curseur, borne))
        curseur = borne
    return morceaux


def parse(payload, start_day, end_day):
    """Extrait d'un flux ICS les occurrences d'une periode.

    :param payload: contenu brut du flux
    :param start_day: premier jour inclus
    :param end_day: dernier jour inclus
    :return: (evenements, absences journee entiere), listes de dictionnaires
    :raises SyncError: flux illisible
    """
    import icalendar
    import recurring_ical_events

    try:
        calendrier = icalendar.Calendar.from_ical(payload)
        occurrences = recurring_ical_events.of(calendrier).between(
            start_day, end_day + timedelta(days=1)
        )
    except Exception as error:
        raise SyncError(f"Flux ICS illisible : {type(error).__name__}.")

    evenements = []
    absences = []

    for composant in occurrences:
        if _text(composant, "STATUS").upper() == "CANCELLED":
            continue

        statut = _text(composant, "X-MICROSOFT-CDO-BUSYSTATUS").upper()
        transparent = _text(composant, "TRANSP").upper() == "TRANSPARENT"
        if statut in FREE_STATUSES or (transparent and statut != OUT_OF_OFFICE):
            continue

        titre = _text(composant, "SUMMARY") or "(sans titre)"
        uid = _text(composant, "UID") or titre
        debut = composant.get("DTSTART").dt
        fin_brute = composant.get("DTEND")
        fin = fin_brute.dt if fin_brute is not None else None

        # Journee entiere : DTSTART est une date, DTEND exclusive.
        if not isinstance(debut, datetime):
            if statut != OUT_OF_OFFICE:
                continue
            dernier = (fin - timedelta(days=1)) if isinstance(fin, date) and fin > debut else debut
            absences.append(
                {
                    "title": titre,
                    # Debut non borne : la suppression avant import vise
                    # toute absence qui touche la fenetre, elle revient entiere.
                    "start_day": debut,
                    "end_day": min(dernier, end_day),
                    "external_uid": f"{uid}|{debut.isoformat()}",
                }
            )
            continue

        debut_local = _local(debut)
        if isinstance(fin, datetime):
            fin_local = _local(fin)
        else:
            fin_local = debut_local + timedelta(minutes=30)
        if fin_local <= debut_local:
            continue
        # Commencee avant la fenetre : sa ligne d'un passage precedent est
        # conservee comme historique, la reimporter la doublerait.
        if debut_local.date() < start_day:
            continue

        kind = "absence" if statut == OUT_OF_OFFICE else "meeting"
        lieu = _text(composant, "LOCATION") or None
        notes = _description(composant)
        for morceau_debut, morceau_fin in _split_by_day(debut_local, fin_local):
            evenements.append(
                {
                    "title": titre,
                    "location": lieu,
                    "notes": notes,
                    "starts_at": morceau_debut,
                    "ends_at": morceau_fin,
                    "kind": kind,
                    "external_uid": f"{uid}|{morceau_debut.strftime(STAMP_FORMAT)}",
                }
            )

    return evenements, absences


# --- Remplacement ---------------------------------------------------------------


def _replace(source_id, evenements, absences, start_day):
    """Remplace dans une seule transaction ce que la source a importe.

    Le passe anterieur a la fenetre est conserve : il nourrit l'historique
    et ne peut plus changer cote Outlook de toute facon.

    Cles facultatives, posees par l'agenda Odoo : project_id sur un
    evenement ; kind ('leave' par defaut) et tentative sur une absence.
    """
    db = get_db()
    debut_texte = start_day.isoformat()
    ligne = db.execute("SELECT space FROM calendar_source WHERE id = ?", (source_id,)).fetchone()
    espace = ligne["space"] if ligne and ligne["space"] else "pro"
    try:
        db.execute(
            "DELETE FROM event WHERE source_id = ? AND starts_at >= ?",
            (source_id, debut_texte),
        )
        db.execute(
            "DELETE FROM day_off WHERE source_id = ? AND end_day >= ?",
            (source_id, debut_texte),
        )
        db.executemany(
            "INSERT INTO event (title, location, notes, starts_at, ends_at, kind, repeat,"
            " source_id, external_uid, project_id, space)"
            " VALUES (?, ?, ?, ?, ?, ?, 'none', ?, ?, ?, ?)",
            [
                (
                    item["title"][:200],
                    item["location"][:2000] if item["location"] else None,
                    item.get("notes"),
                    item["starts_at"].strftime(STAMP_FORMAT),
                    item["ends_at"].strftime(STAMP_FORMAT),
                    item["kind"],
                    source_id,
                    item["external_uid"],
                    item.get("project_id"),
                    espace,
                )
                for item in evenements
            ],
        )
        db.executemany(
            "INSERT INTO day_off (title, start_day, end_day, kind, source_id, tentative)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    item["title"][:200],
                    item["start_day"].isoformat(),
                    item["end_day"].isoformat(),
                    item.get("kind", "leave"),
                    source_id,
                    1 if item.get("tentative") else 0,
                )
                for item in absences
                if item["end_day"] >= item["start_day"]
            ],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Le cache de requete des jours de repos serait sinon perime.
    from models import day_off as day_off_model

    day_off_model._invalidate()


def sync_source(source_id, today=None):
    """Synchronise une source. Ne leve jamais : l'issue est renvoyee.

    :return: {'ok': bool, 'count': nombre importe, 'error': message ou None}
    """
    source = source_model.get_source(source_id)
    if source is not None and source["kind"] == source_model.KIND_ODOO:
        # Import local : odoo_calendar s'appuie sur ce module.
        from services import odoo_calendar

        return odoo_calendar.sync_source(source_id, today)

    maintenant = datetime.now().strftime(STAMP_FORMAT)
    url = source_model.get_url(source_id)
    if not url:
        erreur = "Adresse du calendrier absente ou illisible : saisis-la a nouveau."
        source_model.record_sync(source_id, maintenant, error=erreur)
        return {"ok": False, "count": 0, "error": erreur}

    premier, dernier = window(today)
    try:
        evenements, absences = parse(fetch(url), premier, dernier)
        _replace(source_id, evenements, absences, premier)
    except SyncError as error:
        logger.warning("Synchro agenda %s en echec : %s", source_id, error)
        source_model.record_sync(source_id, maintenant, error=str(error))
        return {"ok": False, "count": 0, "error": str(error)}
    except Exception as error:  # noqa: BLE001 - le battement ne doit pas tomber
        logger.exception("Synchro agenda %s : erreur inattendue.", source_id)
        message = f"Erreur inattendue : {type(error).__name__}."
        source_model.record_sync(source_id, maintenant, error=message)
        return {"ok": False, "count": 0, "error": message}

    total = len(evenements) + len(absences)
    source_model.record_sync(source_id, maintenant, count=total)
    logger.info("Agenda %s synchronise : %s element(s).", source_id, total)
    return {"ok": True, "count": total, "error": None}


def sync_due(now=None):
    """Synchronise les sources actives dont le dernier passage est trop ancien.

    Appele a chaque battement de coeur : le cout d'un tour sans rien a faire
    est une lecture de reglage et une requete.

    :return: nombre de sources synchronisees
    """
    now = now or datetime.now()
    intervalle = timedelta(minutes=max(setting.get_int("calendar_sync_min"), 5))
    faites = 0

    for source in source_model.list_sources():
        if not source["enabled"]:
            continue
        dernier = source["last_sync_at"]
        if dernier:
            try:
                if now - datetime.strptime(dernier, STAMP_FORMAT) < intervalle:
                    continue
            except ValueError:
                pass
        sync_source(source["id"], now.date())
        faites += 1

    return faites
