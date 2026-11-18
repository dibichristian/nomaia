"""Agenda : reunions, creneaux bloques, absences et evenements personnels.

Un evenement occupe du temps sans etre une tache : il ne se coche pas, ne se
chronometre pas, ne se score pas. Sa seule influence sur le reste de NOMAIA est
de retirer du temps a la journee, ce qui rend la charge du plan enfin exacte.

Les repetitions ne sont jamais materialisees en base : une serie vit dans une
seule ligne et ses occurrences sont calculees a la demande sur la periode
demandee. Materialiser imposerait de decider jusqu'a quand, puis de rattraper
chaque fois que l'horizon avance.
"""
import re
from datetime import date, datetime, timedelta

from models import get_db
from models import space as space_model

# personal : sport, famille, medecin. Rappele comme une reunion, mais ne
# retire du temps que s'il tombe pendant les heures de travail.
VALID_KIND = ("meeting", "focus", "absence", "personal")
VALID_REPEAT = ("none", "daily", "weekly")

# Format de stockage, aligne sur le reste du projet : heure locale en texte,
# ni fuseau ni UTC. NOMAIA tourne sur un poste unique.
STAMP_FORMAT = "%Y-%m-%d %H:%M"
DAY_FORMAT = "%Y-%m-%d"

STAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 2000

# Duree maximale d'un evenement. Au-dela, c'est une absence de plusieurs jours,
# qui se saisit comme une repetition quotidienne et non comme un bloc unique :
# un bloc de trois jours fausserait la soustraction fenetre par fenetre.
MAX_DURATION_MIN = 24 * 60

# Garde-fou d'expansion : une serie quotidienne sur dix ans demandee par erreur
# ne doit pas produire des milliers de lignes.
MAX_OCCURRENCES = 500


class EventError(ValueError):
    """Donnee d'evenement refusee."""


def _refuse_if_imported(row):
    """Refuse toute ecriture sur un evenement importe d'un agenda externe.

    La prochaine synchronisation ecraserait la modification sans prevenir :
    mieux vaut dire tout de suite ou corriger.

    :raises EventError: l'evenement porte une source
    """
    if row is not None and row["source_id"] is not None:
        raise EventError(
            "Evenement importe d'un agenda externe : modifie-le a la source (Outlook ou Odoo)."
        )


def _parse_stamp(value, champ):
    """Convertit un horodatage texte en datetime.

    :raises EventError: format invalide
    """
    texte = (value or "").strip()
    if not STAMP_PATTERN.match(texte):
        raise EventError(f"{champ} attendu au format AAAA-MM-JJ HH:MM.")
    return datetime.strptime(texte.replace("T", " ")[:16], STAMP_FORMAT)


def _parse_day(value, champ):
    """Convertit une date texte en date. Retourne None si la valeur est vide."""
    texte = (value or "").strip()
    if not texte:
        return None
    if not DAY_PATTERN.match(texte):
        raise EventError(f"{champ} attendu au format AAAA-MM-JJ.")
    return datetime.strptime(texte, DAY_FORMAT).date()


def _clean(data, partial=False):
    """Valide et normalise les champs d'un evenement.

    :param partial: n'exige aucun champ, pour une mise a jour partielle
    :raises EventError: champ obligatoire absent ou valeur refusee
    """
    champs = {}

    if "title" in data or not partial:
        titre = (data.get("title") or "").strip()
        if not titre:
            raise EventError("Le titre est obligatoire.")
        champs["title"] = titre[:MAX_TITLE_CHARS]

    if "starts_at" in data or not partial:
        champs["starts_at"] = _parse_stamp(data.get("starts_at"), "La date de debut")

    if "ends_at" in data or not partial:
        champs["ends_at"] = _parse_stamp(data.get("ends_at"), "La date de fin")

    if "kind" in data or not partial:
        kind = (data.get("kind") or "meeting").strip()
        if kind not in VALID_KIND:
            raise EventError(f"Type inconnu : {kind}.")
        champs["kind"] = kind

    if "repeat" in data or not partial:
        repeat = (data.get("repeat") or "none").strip()
        if repeat not in VALID_REPEAT:
            raise EventError(f"Repetition inconnue : {repeat}.")
        champs["repeat"] = repeat

    if "repeat_until" in data:
        jour = _parse_day(data.get("repeat_until"), "La fin de repetition")
        champs["repeat_until"] = jour.isoformat() if jour else None

    for nom in ("location", "notes"):
        if nom in data:
            valeur = (data.get(nom) or "").strip()
            champs[nom] = valeur[:MAX_TEXT_CHARS] or None

    if "project_id" in data:
        brut = data.get("project_id")
        champs["project_id"] = int(brut) if brut not in (None, "", "none") else None

    if data.get("space"):
        try:
            champs["space"] = space_model.clean(data.get("space"))
        except space_model.SpaceError as error:
            raise EventError(str(error)) from None

    return champs


def _apply_space(champs, existant=None):
    """Un evenement rattache a un projet en prend l'espace ; sinon il garde le
    sien, ou recoit celui du perimetre a la creation."""
    projet = champs.get("project_id", existant["project_id"] if existant else None)
    if projet:
        row = get_db().execute("SELECT space FROM project WHERE id = ?", (projet,)).fetchone()
        if row:
            champs["space"] = row["space"]
            return
    if existant is None and "space" not in champs:
        champs["space"] = space_model.for_new()


def _check_period(champs, existant=None):
    """Verifie la coherence des bornes d'un evenement.

    Sur une mise a jour partielle, les bornes absentes sont reprises de la
    ligne existante : sans cela, changer la seule heure de fin echapperait au
    controle et permettrait une fin avant le debut.
    """
    debut = champs.get("starts_at")
    fin = champs.get("ends_at")

    if debut is None and existant:
        debut = _parse_stamp(existant["starts_at"], "La date de debut")
    if fin is None and existant:
        fin = _parse_stamp(existant["ends_at"], "La date de fin")

    if debut is None or fin is None:
        return

    if fin <= debut:
        raise EventError("La fin doit suivre le debut.")

    duree = (fin - debut).total_seconds() / 60
    if duree > MAX_DURATION_MIN:
        raise EventError(
            "Un evenement ne peut pas depasser 24 h."
            " Une absence de plusieurs jours se saisit en repetition quotidienne."
        )

    repeat = champs.get("repeat")
    if repeat is None and existant:
        repeat = existant["repeat"]

    jusqu_a = champs.get("repeat_until")
    if jusqu_a is None and existant:
        jusqu_a = existant["repeat_until"]

    if repeat and repeat != "none" and jusqu_a:
        if datetime.strptime(jusqu_a, DAY_FORMAT).date() < debut.date():
            raise EventError("La fin de repetition precede le premier evenement.")


def _to_dict(row, day=None):
    """Convertit une ligne en dictionnaire serialisable.

    :param day: jour de l'occurrence a rendre ; les horaires sont alors
        reportes sur ce jour et 'is_occurrence' passe a vrai
    """
    debut = datetime.strptime(row["starts_at"], STAMP_FORMAT)
    fin = datetime.strptime(row["ends_at"], STAMP_FORMAT)
    occurrence = False

    if day is not None and day != debut.date():
        decalage = day - debut.date()
        debut = debut + decalage
        fin = fin + decalage
        occurrence = True

    return {
        "id": row["id"],
        "title": row["title"],
        "location": row["location"],
        "starts_at": debut.strftime(STAMP_FORMAT),
        "ends_at": fin.strftime(STAMP_FORMAT),
        "kind": row["kind"],
        "repeat": row["repeat"],
        "repeat_until": row["repeat_until"],
        "notes": row["notes"],
        "project_id": row["project_id"],
        "project_name": row["project_name"] if "project_name" in row.keys() else None,
        "space": row["space"] if "space" in row.keys() else space_model.DEFAULT,
        "is_occurrence": occurrence,
        # Importe d'un agenda externe : remplace a chaque synchronisation,
        # donc jamais modifiable ici.
        "source_id": row["source_id"],
        "source_name": row["source_name"] if "source_name" in row.keys() else None,
        "read_only": row["source_id"] is not None,
    }


# --- Expansion des repetitions ------------------------------------------------


def _occurrence_days(row, start_day, end_day, exceptions):
    """Retourne les jours ou une serie a lieu, dans la periode demandee.

    :param row: ligne d'evenement
    :param start_day: premier jour inclus
    :param end_day: dernier jour inclus
    :param exceptions: jours annules de cette serie, en texte ISO
    """
    premier = datetime.strptime(row["starts_at"], STAMP_FORMAT).date()
    repeat = row["repeat"]

    if repeat == "none":
        jours = [premier] if start_day <= premier <= end_day else []
        return [jour for jour in jours if jour.isoformat() not in exceptions]

    pas = timedelta(days=1 if repeat == "daily" else 7)

    borne = end_day
    if row["repeat_until"]:
        fin_serie = datetime.strptime(row["repeat_until"], DAY_FORMAT).date()
        borne = min(borne, fin_serie)

    if borne < premier or borne < start_day:
        return []

    # On saute directement a la premiere occurrence visible plutot que de
    # parcourir depuis l'origine : une serie hebdomadaire vieille de deux ans
    # coute sinon cent tours de boucle pour afficher une semaine.
    if premier >= start_day:
        courant = premier
    else:
        ecart = (start_day - premier).days
        sauts = ecart // pas.days
        courant = premier + pas * sauts
        if courant < start_day:
            courant += pas

    jours = []
    while courant <= borne and len(jours) < MAX_OCCURRENCES:
        if courant.isoformat() not in exceptions:
            jours.append(courant)
        courant += pas

    return jours


def _exceptions_by_event(event_ids):
    """Retourne les occurrences annulees, groupees par evenement."""
    if not event_ids:
        return {}

    marques = ",".join("?" for _ in event_ids)
    rows = get_db().execute(
        f"SELECT event_id, day FROM event_exception WHERE event_id IN ({marques})",
        tuple(event_ids),
    ).fetchall()

    groupes = {}
    for row in rows:
        groupes.setdefault(row["event_id"], set()).add(row["day"])
    return groupes


def list_events(start_day, end_day, space=None):
    """Retourne les evenements d'une periode, repetitions comprises.

    Les occurrences sont calculees ici : la base ne contient qu'une ligne par
    serie. Le resultat est trie par debut.

    :param start_day: date de debut incluse
    :param end_day: date de fin incluse
    :param space: convention de models/space.py
    :return: liste de dictionnaires d'evenements
    """
    if end_day < start_day:
        raise EventError("La fin de periode precede le debut.")

    fin_texte = (end_day + timedelta(days=1)).isoformat()
    filtre, valeurs = space_model.clause("e.space", space)

    # Une serie commencee avant la periode peut y avoir des occurrences : on ne
    # peut donc filtrer sur starts_at que pour les evenements non repetitifs.
    rows = get_db().execute(
        "SELECT e.*, p.name AS project_name, s.name AS source_name"
        " FROM event e LEFT JOIN project p ON p.id = e.project_id"
        " LEFT JOIN calendar_source s ON s.id = e.source_id"
        " WHERE ((e.repeat = 'none' AND e.starts_at >= ? AND e.starts_at < ?)"
        "    OR (e.repeat != 'none' AND e.starts_at < ?"
        "        AND (e.repeat_until IS NULL OR e.repeat_until >= ?)))" + filtre,
        (start_day.isoformat(), fin_texte, fin_texte, start_day.isoformat(), *valeurs),
    ).fetchall()

    exceptions = _exceptions_by_event([row["id"] for row in rows])

    evenements = []
    for row in rows:
        for jour in _occurrence_days(row, start_day, end_day, exceptions.get(row["id"], set())):
            evenements.append(_to_dict(row, day=jour))

    return sorted(evenements, key=lambda item: item["starts_at"])


def list_for_day(day, space=None):
    """Retourne les evenements d'une seule journee."""
    return list_events(day, day, space)


# --- Temps occupe ---------------------------------------------------------------


def busy_intervals(day):
    """Retourne les intervalles occupes d'une journee, fusionnes et tries.

    La fusion est indispensable : deux reunions qui se recouvrent, soustraites
    l'une puis l'autre, retireraient deux fois le temps commun et produiraient
    une capacite fausse, voire negative.

    :param day: journee a examiner
    :return: liste de tuples (debut, fin) en datetime, sans chevauchement
    """
    bruts = []
    # Tous les espaces : une reunion perso occupe aussi le temps de travail.
    for evenement in list_for_day(day, space_model.ALL):
        debut = datetime.strptime(evenement["starts_at"], STAMP_FORMAT)
        fin = datetime.strptime(evenement["ends_at"], STAMP_FORMAT)
        if fin > debut:
            bruts.append((debut, fin))

    return merge_intervals(bruts)


def merge_intervals(intervals):
    """Fusionne des intervalles qui se chevauchent ou se touchent."""
    fusionnes = []
    for debut, fin in sorted(intervals):
        if fusionnes and debut <= fusionnes[-1][1]:
            fusionnes[-1] = (fusionnes[-1][0], max(fusionnes[-1][1], fin))
        else:
            fusionnes.append((debut, fin))
    return fusionnes


def subtract(windows, busy):
    """Retire des intervalles occupes a des fenetres de disponibilite.

    :param windows: liste de tuples (debut, fin, etiquette)
    :param busy: intervalles occupes, deja fusionnes
    :return: fenetres restantes, meme forme, etiquette conservee
    """
    restantes = []

    for debut, fin, etiquette in windows:
        curseur = debut
        for occupe_debut, occupe_fin in busy:
            if occupe_fin <= curseur or occupe_debut >= fin:
                continue
            if occupe_debut > curseur:
                restantes.append((curseur, occupe_debut, etiquette))
            curseur = max(curseur, occupe_fin)
            if curseur >= fin:
                break
        if curseur < fin:
            restantes.append((curseur, fin, etiquette))

    return restantes


# --- CRUD -----------------------------------------------------------------------


def get_event(event_id):
    """Retourne un evenement par son identifiant, ou None."""
    row = get_db().execute(
        "SELECT e.*, p.name AS project_name, s.name AS source_name"
        " FROM event e LEFT JOIN project p ON p.id = e.project_id"
        " LEFT JOIN calendar_source s ON s.id = e.source_id"
        " WHERE e.id = ?",
        (event_id,),
    ).fetchone()
    return _to_dict(row) if row else None


def create_event(data):
    """Cree un evenement et le retourne.

    :param data: champs de l'evenement
    :raises EventError: donnee refusee
    """
    champs = _clean(data)
    _check_period(champs)
    _apply_space(champs)

    db = get_db()
    curseur = db.execute(
        "INSERT INTO event"
        " (project_id, title, location, starts_at, ends_at, kind, repeat, repeat_until, notes,"
        " space) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            champs.get("project_id"),
            champs["title"],
            champs.get("location"),
            champs["starts_at"].strftime(STAMP_FORMAT),
            champs["ends_at"].strftime(STAMP_FORMAT),
            champs["kind"],
            champs["repeat"],
            champs.get("repeat_until"),
            champs.get("notes"),
            champs["space"],
        ),
    )
    db.commit()
    return get_event(curseur.lastrowid)


def update_event(event_id, data):
    """Met a jour un evenement. Retourne la version enregistree, ou None."""
    existant = get_db().execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    if existant is None:
        return None
    _refuse_if_imported(existant)

    champs = _clean(data, partial=True)
    if not champs:
        return get_event(event_id)

    _check_period(champs, existant)
    _apply_space(champs, existant)

    colonnes = []
    valeurs = []
    for nom, valeur in champs.items():
        colonnes.append(f"{nom} = ?")
        valeurs.append(valeur.strftime(STAMP_FORMAT) if isinstance(valeur, datetime) else valeur)

    valeurs.append(event_id)
    db = get_db()
    db.execute(f"UPDATE event SET {', '.join(colonnes)} WHERE id = ?", valeurs)

    # Deplacer une serie invalide ses annulations : elles portaient sur des
    # jours qui n'existent plus dans la nouvelle cadence.
    if "starts_at" in champs or "repeat" in champs:
        db.execute("DELETE FROM event_exception WHERE event_id = ?", (event_id,))

    db.commit()
    return get_event(event_id)


def delete_event(event_id):
    """Supprime un evenement et toute sa serie. Retourne True si une ligne partait.

    :raises EventError: evenement importe d'un agenda externe
    """
    _refuse_if_imported(
        get_db().execute("SELECT source_id FROM event WHERE id = ?", (event_id,)).fetchone()
    )
    db = get_db()
    curseur = db.execute("DELETE FROM event WHERE id = ?", (event_id,))
    db.commit()
    return curseur.rowcount > 0


def cancel_occurrence(event_id, day):
    """Annule une seule occurrence d'une serie.

    :param day: jour annule, au format AAAA-MM-JJ
    :return: True si l'annulation est enregistree, False si l'evenement est absent
    :raises EventError: jour invalide, ou evenement sans repetition
    """
    jour = _parse_day(day, "Le jour a annuler")
    if jour is None:
        raise EventError("Le jour a annuler est obligatoire.")

    row = get_db().execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return False
    _refuse_if_imported(row)

    if row["repeat"] == "none":
        raise EventError(
            "Cet evenement n'a pas de repetition : supprime-le au lieu d'annuler un jour."
        )

    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO event_exception (event_id, day) VALUES (?, ?)",
        (event_id, jour.isoformat()),
    )
    db.commit()
    return True


def next_occurrences(after, until):
    """Retourne les occurrences commencant dans un intervalle, triees.

    Sert au rappel avant reunion : le battement de coeur demande ce qui
    commence entre son dernier passage et l'horizon du rappel.

    :param after: datetime exclu
    :param until: datetime inclus
    :return: liste de dictionnaires d'evenements
    """
    if until < after:
        return []

    evenements = list_events(after.date(), until.date(), space_model.ALL)
    retenus = []

    for evenement in evenements:
        debut = datetime.strptime(evenement["starts_at"], STAMP_FORMAT)
        if after < debut <= until:
            retenus.append(evenement)

    return retenus


def stats_for_day(day=None):
    """Retourne le temps occupe d'une journee, en minutes."""
    day = day or date.today()
    occupe = busy_intervals(day)
    return sum(int((fin - debut).total_seconds() // 60) for debut, fin in occupe)
