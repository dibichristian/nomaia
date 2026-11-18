"""Agenda Odoo en lecture seule : conges, feries de la societe et reunions.

Meme contrat que les agendas ICS (services/calendar_sync.py) : Odoo fait foi,
rien n'y est jamais ecrit, et chaque synchronisation remplace dans une seule
transaction ce que la source a importe depuis le debut de la fenetre
(calendar_sync.window, J-7 a J+60). La connexion est celle des taches Odoo
(odoo_sync.build_client).

Ce qui est lu, un search_read par modele :

- hr.leave de mon employe : valides (state 'validate') et, si le reglage le
  demande, en attente ('confirm', 'validate1'). Refuses et annules ignores ;
- resource.calendar.leaves sans ressource (feries, fermetures) du calendrier
  de travail de mon employe ou sans calendrier, pour sa societe ;
- calendar.event ou mon partenaire est invite, hors invitations declinees
  (calendar.attendee).

Transformation :

- conge a la journee : jour de repos (dates locales) ;
- conge valide a la demi-journee ou a l'heure : absence horaire ;
- conge en attente, quelle que soit sa duree : jour de repos 'tentative' sur
  ses journees. Il est affiche mais ne bloque rien (day_off.off_map l'ignore) :
  une absence horaire, elle, retirerait du temps au plan ;
- ferie : jour de repos 'holiday', sauf les dates deja couvertes par un ferie
  d'une autre origine (pays importe, saisie, autre agenda) ;
- reunion : evenement 'meeting', projet devine d'apres le titre. Une reunion
  « journee entiere » occupe les horaires de travail regles de chaque jour.
  Une reunion deja presente ailleurs (Outlook, saisie) a la meme heure de
  debut avec un titre equivalent n'est pas importee une seconde fois.

Les datetimes Odoo sont en UTC : ils sont ramenes dans le fuseau de
l'utilisateur Odoo (res.users.tz), a defaut dans celui du poste.
"""
import logging
import re
import unicodedata
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models import calendar_source as source_model
from models import event as event_model
from models import get_db, setting
from services import calendar_sync, odoo_sync
from services.odoo_client import OdooRpcError

logger = logging.getLogger("nomaia.odoo_calendar")

LEAVE_MODEL = "hr.leave"
HOLIDAY_MODEL = "resource.calendar.leaves"
MEETING_MODEL = "calendar.event"
ATTENDEE_MODEL = "calendar.attendee"
EMPLOYEE_MODEL = "hr.employee"

VALIDATED_STATES = ("validate",)
PENDING_STATES = ("confirm", "validate1")

# Champs souhaites ; seuls ceux que la version d'Odoo connait sont demandes.
LEAVE_FIELDS = (
    "holiday_status_id", "name", "state", "date_from", "date_to",
    "request_date_from", "request_date_to", "request_unit_half",
    "request_unit_hours", "number_of_days",
)
HOLIDAY_FIELDS = ("name", "date_from", "date_to", "time_type", "calendar_id", "company_id")
MEETING_FIELDS = (
    "name", "start", "stop", "allday", "start_date", "stop_date", "location", "description",
    "res_model",
)

ODOO_STAMP = "%Y-%m-%d %H:%M:%S"
DAY_FORMAT = "%Y-%m-%d"

# Reglage (models/setting.py) de chaque import.
OPTION_KEYS = {
    "leaves": "odoo_cal_leaves",
    "pending": "odoo_cal_pending",
    "holidays": "odoo_cal_holidays",
    "meetings": "odoo_cal_meetings",
}

# Nom de projet trop court pour etre devine sans faux positif (« IT », « RH »).
MIN_PROJECT_NAME_CHARS = 3

DEFAULT_LEAVE_TITLE = "Congé"
DEFAULT_HOLIDAY_TITLE = "Fermeture"


def is_configured():
    """Indique si la connexion Odoo des taches est renseignee."""
    return bool(setting.get("odoo_url") and setting.get_secret("odoo_api_key"))


def options():
    """Retourne l'etat des quatre imports : {leaves, pending, holidays, meetings}."""
    return {nom: setting.get(cle) == "1" for nom, cle in OPTION_KEYS.items()}


# --- Conversions ----------------------------------------------------------------


def _user_zone(name):
    """Fuseau de l'utilisateur Odoo, ou None pour celui du poste."""
    if not name:
        return None
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _to_local(value, zone):
    """Convertit un datetime Odoo (texte UTC) en heure locale naive, a la minute.

    :return: datetime, ou None si la valeur est absente ou illisible
    """
    if not value:
        return None
    try:
        moment = datetime.strptime(str(value)[:19], ODOO_STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    local = moment.astimezone(zone) if zone else moment.astimezone()
    return local.replace(tzinfo=None, second=0, microsecond=0)


def _parse_day(value):
    """Convertit une date Odoo ('AAAA-MM-JJ') en date, None si absente."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], DAY_FORMAT).date()
    except ValueError:
        return None


def _last_day(debut, fin):
    """Dernier jour couvert par un intervalle local.

    Une fin a minuit pile appartient a la veille : sinon une fermeture qui
    s'arrete a 00:00 deborderait sur le lendemain.
    """
    if fin > debut and fin.time() == time(0, 0):
        return (fin - timedelta(days=1)).date()
    return fin.date()


def _m2o_id(value):
    """Identifiant d'un many2one lu par RPC ([id, nom] ou False)."""
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return None


def _m2o_name(value):
    """Libelle d'un many2one lu par RPC ([id, nom] ou False)."""
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1]).strip()
    return ""


def _fold(text):
    """Texte sans accents ni casse, espaces resserres : base des comparaisons."""
    decompose = unicodedata.normalize("NFKD", str(text or ""))
    sans_accents = "".join(car for car in decompose if not unicodedata.combining(car))
    return " ".join(sans_accents.casefold().split())


def _parse_hour(value, fallback):
    """Lit un horaire 'HH:MM' de reglage, avec repli."""
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        return datetime.strptime(fallback, "%H:%M").time()


# --- Lecture Odoo ---------------------------------------------------------------


def _available(client, model, wanted):
    """Ne garde que les champs que la version d'Odoo connait sur ce modele."""
    connus = client.fields_get(model, attributes=("type",)) or {}
    return [champ for champ in wanted if champ in connus]


def _fetch(client, premier, dernier, opts):
    """Lit dans Odoo ce que les reglages demandent.

    Les bornes sont elargies d'un jour de chaque cote : le filtre exact se
    fait apres conversion en heure locale.

    :return: {zone, leaves, holidays, meetings, declined}
    """
    uid = client.authenticate()
    utilisateur = (client.read("res.users", [uid], ["partner_id", "tz", "company_id"]) or [{}])[0]
    partenaire = _m2o_id(utilisateur.get("partner_id"))
    debut = (premier - timedelta(days=1)).strftime(DAY_FORMAT) + " 00:00:00"
    fin = (dernier + timedelta(days=2)).strftime(DAY_FORMAT) + " 00:00:00"

    lu = {
        "zone": _user_zone(utilisateur.get("tz")),
        "leaves": [],
        "holidays": [],
        "meetings": [],
        "declined": set(),
    }

    etats = []
    if opts["leaves"]:
        etats.extend(VALIDATED_STATES)
    if opts["pending"]:
        etats.extend(PENDING_STATES)
    if etats:
        lu["leaves"] = client.search_read(
            LEAVE_MODEL,
            [
                ("employee_id.user_id", "=", uid),
                ("state", "in", etats),
                ("date_from", "<", fin),
                ("date_to", ">=", debut),
            ],
            _available(client, LEAVE_MODEL, LEAVE_FIELDS),
        )

    if opts["holidays"]:
        lu["holidays"] = _fetch_holidays(client, uid, utilisateur, debut, fin)

    if opts["meetings"] and partenaire:
        reunions = client.search_read(
            MEETING_MODEL,
            [("partner_ids", "in", [partenaire]), ("start", "<", fin), ("stop", ">=", debut)],
            _available(client, MEETING_MODEL, MEETING_FIELDS),
        )
        # Odoo cree une reunion « X en conge » pour chaque conge (res_model
        # hr.leave) : le conge est deja importe comme jour de repos.
        reunions = [row for row in reunions if row.get("res_model") != LEAVE_MODEL]
        lu["meetings"] = reunions
        if reunions:
            declinees = client.search_read(
                ATTENDEE_MODEL,
                [
                    ("partner_id", "=", partenaire),
                    ("event_id", "in", [row["id"] for row in reunions]),
                    ("state", "=", "declined"),
                ],
                ["event_id"],
            )
            lu["declined"] = {_m2o_id(row.get("event_id")) for row in declinees}

    return lu


def _fetch_holidays(client, uid, utilisateur, debut, fin):
    """Feries et fermetures du calendrier de travail de mon employe.

    Sans fiche employe, seuls les feries sans calendrier de la societe de
    l'utilisateur sont retenus.
    """
    employes = client.search_read(
        EMPLOYEE_MODEL, [("user_id", "=", uid)], ["resource_calendar_id", "company_id"]
    )
    calendriers = sorted(
        {_m2o_id(row.get("resource_calendar_id")) for row in employes} - {None}
    )
    societes = sorted({_m2o_id(row.get("company_id")) for row in employes} - {None})
    if not societes and _m2o_id(utilisateur.get("company_id")):
        societes = [_m2o_id(utilisateur.get("company_id"))]

    champs = _available(client, HOLIDAY_MODEL, HOLIDAY_FIELDS)
    domaine = [("resource_id", "=", False), ("date_from", "<", fin), ("date_to", ">=", debut)]
    if calendriers:
        domaine += ["|", ("calendar_id", "=", False), ("calendar_id", "in", calendriers)]
    else:
        domaine.append(("calendar_id", "=", False))
    if societes and "company_id" in champs:
        domaine += ["|", ("company_id", "=", False), ("company_id", "in", societes)]
    return client.search_read(HOLIDAY_MODEL, domaine, champs)


# --- Transformation -------------------------------------------------------------


def _leave_items(rows, zone, premier, dernier):
    """Conges Odoo vers evenements (absences horaires) et jours de repos.

    :return: (evenements, absences)
    """
    evenements = []
    absences = []
    for row in rows:
        debut = _to_local(row.get("date_from"), zone)
        fin = _to_local(row.get("date_to"), zone)
        if debut is None or fin is None or fin < debut:
            continue

        en_attente = row.get("state") in PENDING_STATES
        titre = _m2o_name(row.get("holiday_status_id")) or (row.get("name") or "").strip()
        titre = titre or DEFAULT_LEAVE_TITLE
        if "request_unit_half" in row or "request_unit_hours" in row:
            partiel = bool(row.get("request_unit_half") or row.get("request_unit_hours"))
        else:
            # Champs absents de cette version : la duree en jours tranche.
            partiel = 0 < float(row.get("number_of_days") or 1) < 1

        # Un conge valide a l'heure retire du temps au plan ; en attente, il
        # n'est qu'affiche, sur sa journee entiere (voir l'en-tete du module).
        if partiel and not en_attente:
            for morceau_debut, morceau_fin in calendar_sync._split_by_day(debut, fin):
                if not premier <= morceau_debut.date() <= dernier:
                    continue
                evenements.append(
                    {
                        "title": titre,
                        "location": None,
                        "notes": None,
                        "starts_at": morceau_debut,
                        "ends_at": morceau_fin,
                        "kind": "absence",
                        "external_uid": f"odoo-leave:{row['id']}|"
                        f"{morceau_debut.strftime(calendar_sync.STAMP_FORMAT)}",
                    }
                )
            continue

        premier_jour = _parse_day(row.get("request_date_from")) or debut.date()
        dernier_jour = _parse_day(row.get("request_date_to")) or _last_day(debut, fin)
        dernier_jour = max(dernier_jour, premier_jour)
        if dernier_jour < premier or premier_jour > dernier:
            continue
        absences.append(
            {
                "title": titre,
                "start_day": premier_jour,
                "end_day": dernier_jour,
                "kind": "leave",
                "tentative": en_attente,
            }
        )
    return evenements, absences


def _holidays_elsewhere(source_id, premier_jour, dernier_jour):
    """Dates deja couvertes par un ferie d'une autre origine que cette source."""
    rows = get_db().execute(
        "SELECT start_day, end_day FROM day_off WHERE kind = 'holiday'"
        " AND (source_id IS NULL OR source_id != ?) AND start_day <= ? AND end_day >= ?",
        (source_id, dernier_jour.isoformat(), premier_jour.isoformat()),
    ).fetchall()
    couverts = set()
    for row in rows:
        jour = max(premier_jour, _parse_day(row["start_day"]))
        fin = min(dernier_jour, _parse_day(row["end_day"]))
        while jour <= fin:
            couverts.add(jour)
            jour += timedelta(days=1)
    return couverts


def _uncovered_runs(debut, fin, couverts):
    """Decoupe une plage de dates en sous-plages hors des dates couvertes."""
    plages = []
    jour = debut
    while jour <= fin:
        if jour in couverts:
            jour += timedelta(days=1)
            continue
        depart = jour
        while jour + timedelta(days=1) <= fin and jour + timedelta(days=1) not in couverts:
            jour += timedelta(days=1)
        plages.append((depart, jour))
        jour += timedelta(days=1)
    return plages


def _holiday_items(source_id, rows, zone, premier, dernier):
    """Feries Odoo vers jours de repos, sans doubler un ferie deja present."""
    plages = []
    for row in rows:
        # 'other' : temps compte comme travaille (formation...), pas un repos.
        if row.get("time_type") == "other":
            continue
        debut = _to_local(row.get("date_from"), zone)
        fin = _to_local(row.get("date_to"), zone)
        if debut is None or fin is None or fin < debut:
            continue
        dernier_jour = _last_day(debut, fin)
        if dernier_jour < premier or debut.date() > dernier:
            continue
        titre = (row.get("name") or "").strip() or DEFAULT_HOLIDAY_TITLE
        plages.append((debut.date(), dernier_jour, titre))

    if not plages:
        return []

    couverts = _holidays_elsewhere(
        source_id, min(p[0] for p in plages), max(p[1] for p in plages)
    )
    absences = []
    for debut, fin, titre in sorted(plages):
        for depart, arrivee in _uncovered_runs(debut, fin, couverts):
            absences.append(
                {"title": titre, "start_day": depart, "end_day": arrivee, "kind": "holiday"}
            )
            # Deux feries Odoo le meme jour (global et calendrier) : un seul.
            jour = depart
            while jour <= arrivee:
                couverts.add(jour)
                jour += timedelta(days=1)
    return absences


def _project_matchers():
    """Motifs des projets NOMAIA non termines, du nom le plus long au plus court."""
    rows = get_db().execute("SELECT id, name FROM project WHERE status != 'done'").fetchall()
    motifs = []
    for row in rows:
        nom = _fold(row["name"])
        if len(nom) < MIN_PROJECT_NAME_CHARS:
            continue
        motifs.append(
            (len(nom), row["id"], re.compile(r"(?<!\w)" + re.escape(nom) + r"(?!\w)"))
        )
    motifs.sort(key=lambda motif: (-motif[0], motif[1]))
    return motifs


def guess_project(title, matchers):
    """Projet dont le nom figure dans le titre, en mot entier ; le plus long gagne.

    :param matchers: motifs prepares par _project_matchers
    :return: identifiant du projet, ou None
    """
    titre = _fold(title)
    for _longueur, project_id, motif in matchers:
        if motif.search(titre):
            return project_id
    return None


def _same_title(a, b):
    """Titres equivalents : sans casse ni accents, l'un contenu dans l'autre."""
    gauche, droite = _fold(a), _fold(b)
    return bool(gauche and droite) and (gauche in droite or droite in gauche)


def _events_elsewhere(source_id, premier, dernier):
    """Titres des evenements d'autres origines, par heure de debut."""
    autres = {}
    for evenement in event_model.list_events(premier, dernier, "all"):
        if evenement["source_id"] == source_id:
            continue
        autres.setdefault(evenement["starts_at"], []).append(evenement["title"])
    return autres


def _meeting_slots(row, zone, premier, dernier, heures):
    """Creneaux locaux occupes par une reunion, coupes par jour."""
    if row.get("allday"):
        debut = _to_local(row.get("start"), zone)
        fin = _to_local(row.get("stop"), zone)
        premier_jour = _parse_day(row.get("start_date")) or (debut.date() if debut else None)
        dernier_jour = _parse_day(row.get("stop_date")) or (fin.date() if fin else premier_jour)
        if premier_jour is None:
            return []
        creneaux = []
        jour = max(premier_jour, premier)
        while jour <= min(max(dernier_jour, premier_jour), dernier):
            ouverture = datetime.combine(jour, heures[0])
            fermeture = datetime.combine(jour, heures[1])
            if fermeture > ouverture:
                creneaux.append((ouverture, fermeture))
            jour += timedelta(days=1)
        return creneaux

    debut = _to_local(row.get("start"), zone)
    fin = _to_local(row.get("stop"), zone)
    if debut is None or fin is None or fin <= debut:
        return []
    return [
        (morceau_debut, morceau_fin)
        for morceau_debut, morceau_fin in calendar_sync._split_by_day(debut, fin)
        if premier <= morceau_debut.date() <= dernier
    ]


def _meeting_items(source_id, rows, declined, zone, premier, dernier):
    """Reunions Odoo vers evenements 'meeting', sans les doublons d'autres agendas."""
    if not rows:
        return []
    heures = (
        _parse_hour(setting.get("work_start"), "08:30"),
        _parse_hour(setting.get("work_end"), "18:00"),
    )
    autres = _events_elsewhere(source_id, premier, dernier)
    motifs = _project_matchers()

    evenements = []
    for row in rows:
        if row["id"] in declined:
            continue
        titre = (row.get("name") or "").strip() or "(sans titre)"
        notes = odoo_sync.html_to_text(row.get("description"))
        lieu = (row.get("location") or "").strip() or None
        projet = guess_project(titre, motifs)
        for debut, fin in _meeting_slots(row, zone, premier, dernier, heures):
            cle = debut.strftime(calendar_sync.STAMP_FORMAT)
            if any(_same_title(titre, autre) for autre in autres.get(cle, ())):
                continue
            evenements.append(
                {
                    "title": titre,
                    "location": lieu,
                    "notes": notes[: calendar_sync.MAX_NOTES_CHARS] if notes else None,
                    "starts_at": debut,
                    "ends_at": fin,
                    "kind": "meeting",
                    "external_uid": f"odoo:{row['id']}",
                    "project_id": projet,
                }
            )
    return evenements


def build_items(source_id, lu, premier, dernier):
    """Transforme la lecture Odoo en lignes a importer.

    :return: (evenements, absences) au format de calendar_sync._replace
    """
    zone = lu["zone"]
    evenements, absences = _leave_items(lu["leaves"], zone, premier, dernier)
    absences += _holiday_items(source_id, lu["holidays"], zone, premier, dernier)
    evenements += _meeting_items(
        source_id, lu["meetings"], lu["declined"], zone, premier, dernier
    )
    return evenements, absences


# --- Synchronisation ------------------------------------------------------------


def sync_source(source_id, today=None, client=None):
    """Synchronise la source Odoo. Ne leve jamais : l'issue est renvoyee.

    Une erreur Odoo (connexion, modele absent, droits) est enregistree sur la
    source et laisse les lignes precedentes intactes. Tous les imports coupes,
    la fenetre est simplement videe.

    :param client: client Odoo (injecte par les tests), construit sinon
    :return: {'ok': bool, 'count': nombre importe, 'error': message ou None}
    """
    maintenant = datetime.now().strftime(calendar_sync.STAMP_FORMAT)
    premier, dernier = calendar_sync.window(today)
    opts = options()
    try:
        lu = {"zone": None, "leaves": [], "holidays": [], "meetings": [], "declined": set()}
        if any(opts.values()):
            lu = _fetch(client or odoo_sync.build_client(), premier, dernier, opts)
        evenements, absences = build_items(source_id, lu, premier, dernier)
        calendar_sync._replace(source_id, evenements, absences, premier)
    except (odoo_sync.OdooSyncError, OdooRpcError) as error:
        message = str(error) if isinstance(error, odoo_sync.OdooSyncError) else f"Odoo : {error}"
        logger.warning("Synchro agenda Odoo %s en echec : %s", source_id, message)
        source_model.record_sync(source_id, maintenant, error=message)
        return {"ok": False, "count": 0, "error": message}
    except Exception as error:  # noqa: BLE001 - le battement ne doit pas tomber
        logger.exception("Synchro agenda Odoo %s : erreur inattendue.", source_id)
        message = f"Erreur inattendue : {type(error).__name__}."
        source_model.record_sync(source_id, maintenant, error=message)
        return {"ok": False, "count": 0, "error": message}

    total = len(evenements) + len(absences)
    source_model.record_sync(source_id, maintenant, count=total)
    logger.info("Agenda Odoo %s synchronise : %s element(s).", source_id, total)
    return {"ok": True, "count": total, "error": None}
