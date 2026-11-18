"""Interlocuteurs et attentes : qui gravite autour du travail, et qui doit quoi.

Deux directions, volontairement modelisees differemment :

- **Je dois** quelque chose a quelqu'un : c'est une tache, avec un destinataire
  (`task.person_id`). Une table parallele doublonnerait les taches et imposerait
  de les tenir synchronisees.
- **On me doit** quelque chose : ce n'est pas mon travail, donc pas une tache.
  C'est une attente (`waiting`), qui ne se coche pas mais se relance.

Le second cas est le trou que ce module comble : sans lui, des specs attendues
depuis huit jours n'existent nulle part.
"""
import re
from datetime import date, datetime

from models import get_db
from models import space as space_model

VALID_STATUS = ("open", "received", "dropped")

DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_NAME_CHARS = 120
MAX_SUBJECT_CHARS = 300
MAX_TEXT_CHARS = 2000
MAX_URL_CHARS = 500

# Nombre d'alias retenus par interlocuteur. Au-dela, la liste ne sert plus a
# reconnaitre quelqu'un, elle temoigne d'une fusion faite a tort.
MAX_ALIASES = 20


class PersonError(ValueError):
    """Donnee d'interlocuteur ou d'attente refusee."""


def _parse_day(value, champ):
    """Convertit une date texte en date, ou None si la valeur est vide."""
    texte = (value or "").strip()
    if not texte:
        return None
    if not DAY_PATTERN.match(texte):
        raise PersonError(f"{champ} attendu au format AAAA-MM-JJ.")
    return datetime.strptime(texte, "%Y-%m-%d").date()


def _clean_text(value, limite):
    """Normalise un champ texte facultatif."""
    texte = (value or "").strip()
    return texte[:limite] or None


# --- Interlocuteurs -------------------------------------------------------------


def _clean_report_url(value):
    """Lien du tableau de bord partage : https uniquement, sinon refuse.

    Le lien part tel quel dans un message WhatsApp : un autre schema
    (javascript:, file:) n'a rien a y faire.
    """
    lien = (value or "").strip()
    if not lien:
        return None
    if not lien.startswith("https://") or len(lien) > MAX_URL_CHARS or any(c.isspace() for c in lien):
        raise PersonError("Le lien du tableau de bord doit commencer par https://.")
    return lien


def _split_aliases(value):
    """Convertit la colonne aliases (un nom par ligne) en liste."""
    return [ligne.strip() for ligne in (value or "").splitlines() if ligne.strip()]


def _as_alias_list(value):
    """Accepte une liste, ou un texte separe par des virgules ou des lignes."""
    if isinstance(value, str):
        return value.replace(",", "\n").splitlines()
    return list(value or [])


def _join_aliases(names, own_name):
    """Normalise une liste d'alias pour la colonne aliases.

    Retire les vides, les doublons (casse ignoree) et le nom propre de
    l'interlocuteur, qui n'a pas a etre son propre alias.

    :param names: liste de noms, ou texte separe par des virgules ou des lignes
    :param own_name: nom principal de l'interlocuteur
    :return: texte a enregistrer, ou None si aucun alias
    """
    vus = {(own_name or "").strip().casefold()}
    retenus = []
    for nom in _as_alias_list(names):
        texte = str(nom or "").strip()[:MAX_NAME_CHARS]
        cle = texte.casefold()
        if texte and cle not in vus:
            vus.add(cle)
            retenus.append(texte)
    return "\n".join(retenus[:MAX_ALIASES]) or None



def _person_dict(row):
    """Convertit une ligne de person en dictionnaire serialisable."""
    cles = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "email": row["email"],
        "phone": row["phone"],
        "notes": row["notes"],
        "aliases": _split_aliases(row["aliases"]) if "aliases" in cles else [],
        "report_url": row["report_url"] if "report_url" in cles else None,
        "project_id": row["project_id"],
        "space": row["space"] if "space" in cles else space_model.DEFAULT,
        "project_name": row["project_name"] if "project_name" in cles else None,
        "projects": [],
        "waiting_count": row["waiting_count"] if "waiting_count" in cles else 0,
        "owed_count": row["owed_count"] if "owed_count" in cles else 0,
    }


def _with_projects(people):
    """Ajoute a chaque interlocuteur la liste de ses projets, en une requete."""
    if not people:
        return people
    ids = [personne["id"] for personne in people]
    rows = get_db().execute(
        "SELECT pp.person_id, pr.id, pr.name, pr.color FROM person_project pp"
        " JOIN project pr ON pr.id = pp.project_id"
        f" WHERE pp.person_id IN ({', '.join('?' * len(ids))})"
        " ORDER BY pr.name COLLATE NOCASE",
        ids,
    ).fetchall()
    par_personne = {}
    for row in rows:
        par_personne.setdefault(row["person_id"], []).append(
            {"id": row["id"], "name": row["name"], "color": row["color"]}
        )
    for personne in people:
        projets = par_personne.get(personne["id"], [])
        # Le projet principal d'abord : c'est lui qu'affichent les vues compactes.
        projets.sort(key=lambda projet: projet["id"] != personne["project_id"])
        personne["projects"] = projets
    return people


def list_people(project_id=None, space=None):
    """Retourne les interlocuteurs, avec ce qu'on leur doit et ce qu'ils doivent.

    Les deux compteurs et les projets sont lus en deux requetes : les remonter
    par interlocuteur produirait une lecture par ligne.

    :param project_id: ne garde que les interlocuteurs rattaches a ce projet
    :param space: convention de models/space.py ; un contact 'commun' est
        visible de partout, ses compteurs ne comptent que l'espace visible
    """
    filtre, parametres = " WHERE 1 = 1", []
    if project_id is not None:
        filtre += " AND p.id IN (SELECT person_id FROM person_project WHERE project_id = ?)"
        parametres.append(project_id)
    personnes, valeurs_p = space_model.clause("p.space", space, shared=True)
    attentes, valeurs_w = space_model.clause("w.space", space)
    taches, valeurs_t = space_model.clause("t.space", space)
    rows = get_db().execute(
        "SELECT p.*, pr.name AS project_name,"
        " (SELECT COUNT(*) FROM waiting w"
        "   WHERE w.person_id = p.id AND w.status = 'open'" + attentes + ") AS waiting_count,"
        " (SELECT COUNT(*) FROM task t"
        "   WHERE t.person_id = p.id AND t.status = 'todo'" + taches + ") AS owed_count"
        " FROM person p LEFT JOIN project pr ON pr.id = p.project_id"
        + filtre + personnes + " ORDER BY p.name COLLATE NOCASE",
        valeurs_w + valeurs_t + parametres + valeurs_p,
    ).fetchall()
    return _with_projects([_person_dict(row) for row in rows])


def get_person(person_id):
    """Retourne un interlocuteur par son identifiant, ou None."""
    row = get_db().execute(
        "SELECT p.*, pr.name AS project_name"
        " FROM person p LEFT JOIN project pr ON pr.id = p.project_id"
        " WHERE p.id = ?",
        (person_id,),
    ).fetchone()
    return _with_projects([_person_dict(row)])[0] if row else None


def find_by_name(name, space=None):
    """Retrouve un interlocuteur par son nom, sans tenir compte de la casse.

    Sert au triage et au chat, qui citent un prenom et non un identifiant.
    Entre deux correspondances, le nom le plus court gagne : « Paul » doit
    l'emporter sur « Paul Martin » quand c'est « Paul » qui est cite.

    A defaut de nom exact, un alias suffit : c'est ce qui empeche une fusion
    d'etre defaite par la capture suivante qui cite l'ancien nom.

    :param space: convention de models/space.py (contacts communs compris)
    """
    texte = (name or "").strip()
    if not texte:
        return None

    filtre, valeurs = space_model.clause("p.space", space, shared=True)
    db = get_db()
    row = db.execute(
        "SELECT p.*, pr.name AS project_name"
        " FROM person p LEFT JOIN project pr ON pr.id = p.project_id"
        " WHERE p.name = ? COLLATE NOCASE" + filtre +
        " ORDER BY LENGTH(p.name), p.id LIMIT 1",
        (texte, *valeurs),
    ).fetchone()
    if row:
        return _with_projects([_person_dict(row)])[0]

    # Comparaison en Python : les alias tiennent dans une colonne texte, et
    # le carnet compte quelques dizaines de personnes, pas des milliers.
    cible = texte.casefold()
    candidats = db.execute(
        "SELECT p.*, pr.name AS project_name"
        " FROM person p LEFT JOIN project pr ON pr.id = p.project_id"
        " WHERE p.aliases IS NOT NULL AND p.aliases != ''" + filtre +
        " ORDER BY LENGTH(p.name), p.id",
        valeurs,
    ).fetchall()
    for candidat in candidats:
        if any(alias.casefold() == cible for alias in _split_aliases(candidat["aliases"])):
            return _with_projects([_person_dict(candidat)])[0]
    return None


def _project_ids(values):
    """Identifiants de projet distincts et valides, dans l'ordre fourni."""
    ids = []
    for value in values or []:
        if value in (None, "", "none"):
            continue
        try:
            identifiant = int(value)
        except (TypeError, ValueError):
            raise PersonError(f"Projet invalide : {value}.") from None
        if identifiant not in ids:
            ids.append(identifiant)
    if ids:
        connus = {row[0] for row in get_db().execute(
            f"SELECT id FROM project WHERE id IN ({', '.join('?' * len(ids))})", ids)}
        inconnus = [identifiant for identifiant in ids if identifiant not in connus]
        if inconnus:
            raise PersonError(f"Projet introuvable : {inconnus[0]}.")
    return ids


def add_project(person_id, project_id, commit=True):
    """Rattache un interlocuteur a un projet de plus ; devient son principal s'il n'en a pas."""
    db = get_db()
    db.execute("INSERT OR IGNORE INTO person_project (person_id, project_id) VALUES (?, ?)",
               (person_id, project_id))
    db.execute("UPDATE person SET project_id = ? WHERE id = ? AND project_id IS NULL", (project_id, person_id))
    if commit:
        db.commit()


def remove_project(person_id, project_id, commit=True):
    """Detache un interlocuteur d'un projet ; s'il etait principal, un autre de ses projets le remplace."""
    db = get_db()
    db.execute("DELETE FROM person_project WHERE person_id = ? AND project_id = ?", (person_id, project_id))
    db.execute(
        "UPDATE person SET project_id = (SELECT MIN(project_id) FROM person_project WHERE person_id = ?)"
        " WHERE id = ? AND project_id = ?",
        (person_id, person_id, project_id),
    )
    if commit:
        db.commit()


def set_projects(person_id, project_ids, commit=True):
    """Remplace l'ensemble des projets d'un interlocuteur.

    Le principal est garde s'il reste dans l'ensemble, sinon c'est le premier fourni.
    """
    ids = _project_ids(project_ids)
    db = get_db()
    db.execute("DELETE FROM person_project WHERE person_id = ?", (person_id,))
    db.executemany("INSERT INTO person_project (person_id, project_id) VALUES (?, ?)",
                   [(person_id, identifiant) for identifiant in ids])
    actuel = db.execute("SELECT project_id FROM person WHERE id = ?", (person_id,)).fetchone()
    if actuel is not None and actuel[0] not in ids:
        db.execute("UPDATE person SET project_id = ? WHERE id = ?", (ids[0] if ids else None, person_id))
    if commit:
        db.commit()


def create_person(data):
    """Cree un interlocuteur et le retourne.

    :raises PersonError: nom absent
    """
    nom = (data.get("name") or "").strip()
    if not nom:
        raise PersonError("Le nom est obligatoire.")

    projet = data.get("project_id")
    db = get_db()
    espace = _space_for_new(data.get("space"), projet)
    curseur = db.execute(
        "INSERT INTO person (name, role, email, phone, notes, project_id, space)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            nom[:MAX_NAME_CHARS],
            _clean_text(data.get("role"), MAX_NAME_CHARS),
            _clean_text(data.get("email"), MAX_NAME_CHARS),
            _clean_text(data.get("phone"), MAX_NAME_CHARS),
            _clean_text(data.get("notes"), MAX_TEXT_CHARS),
            int(projet) if projet not in (None, "", "none") else None,
            espace,
        ),
    )
    nouveau = curseur.lastrowid
    if "project_ids" in data:
        set_projects(nouveau, data.get("project_ids"), commit=False)
    elif projet not in (None, "", "none"):
        add_project(nouveau, int(projet), commit=False)
    db.commit()
    return get_person(nouveau)


def _space_for_new(explicit, project_id=None):
    """Espace d'un contact cree : explicite (commun admis), sinon celui de son
    projet, sinon celui du perimetre."""
    if explicit:
        try:
            return space_model.clean(explicit, shared=True)
        except space_model.SpaceError as error:
            raise PersonError(str(error)) from None
    if project_id not in (None, "", "none"):
        row = get_db().execute("SELECT space FROM project WHERE id = ?", (int(project_id),)).fetchone()
        if row:
            return row["space"]
    return space_model.for_new()


def get_or_create(name, project_id=None, role=None, space=None):
    """Retourne l'interlocuteur portant ce nom, en le creant au besoin.

    Utilise par le triage : il cite un nom, pas un identifiant, et creer le
    doublon d'un interlocuteur existant rendrait les relances incompletes.

    :param space: espace vise (celui de la capture triee) ; sans lui, le
        perimetre de la requete. Un contact de l'autre espace n'est jamais
        rattache : un homonyme est cree dans l'espace vise (contacts communs
        exceptes), comme un partenaire propre a une societe dans Odoo.
    """
    existant = find_by_name(name, space=space)
    if existant:
        # Cite sur un autre projet : il y intervient aussi, sans quitter les precedents.
        if project_id and project_id not in [projet["id"] for projet in existant["projects"]]:
            add_project(existant["id"], project_id)
            existant = get_person(existant["id"])
        return existant
    return create_person({"name": name, "project_id": project_id, "role": role, "space": space})


def update_person(person_id, data):
    """Met a jour un interlocuteur. Retourne la version enregistree, ou None.

    Un renommage garde l'ancien nom en alias : les captures qui le citent
    encore retrouvent la fiche au lieu d'en creer une nouvelle.
    """
    actuel = get_person(person_id)
    if actuel is None:
        return None

    champs = {}
    nom_final = actuel["name"]
    if "name" in data:
        nom = (data.get("name") or "").strip()
        if not nom:
            raise PersonError("Le nom est obligatoire.")
        champs["name"] = nom_final = nom[:MAX_NAME_CHARS]

    renomme = nom_final.casefold() != actuel["name"].casefold()
    if "aliases" in data or renomme:
        alias = _as_alias_list(data["aliases"] if "aliases" in data else actuel["aliases"])
        if renomme:
            alias.append(actuel["name"])
        champs["aliases"] = _join_aliases(alias, nom_final)

    for nom_champ, limite in (
        ("role", MAX_NAME_CHARS),
        ("email", MAX_NAME_CHARS),
        ("phone", MAX_NAME_CHARS),
        ("notes", MAX_TEXT_CHARS),
    ):
        if nom_champ in data:
            champs[nom_champ] = _clean_text(data.get(nom_champ), limite)

    if "report_url" in data:
        champs["report_url"] = _clean_report_url(data.get("report_url"))

    if data.get("space"):
        champs["space"] = _space_for_new(data.get("space"))

    # project_ids remplace l'ensemble des projets ; project_id seul change le
    # principal sans retirer les autres (None detache du principal).
    projets = _project_ids(data.get("project_ids")) if "project_ids" in data else None
    principal = None
    if "project_id" in data and projets is None:
        brut = data.get("project_id")
        principal = _project_ids([brut])[0] if brut not in (None, "", "none") else None

    if not champs and projets is None and "project_id" not in data:
        return get_person(person_id)

    db = get_db()
    if champs:
        colonnes = ", ".join(f"{nom} = ?" for nom in champs)
        db.execute(f"UPDATE person SET {colonnes} WHERE id = ?", list(champs.values()) + [person_id])
    if projets is not None:
        set_projects(person_id, projets, commit=False)
    elif "project_id" in data:
        if principal is None:
            if actuel["project_id"]:
                remove_project(person_id, actuel["project_id"], commit=False)
        else:
            db.execute("INSERT OR IGNORE INTO person_project (person_id, project_id) VALUES (?, ?)",
                       (person_id, principal))
            db.execute("UPDATE person SET project_id = ? WHERE id = ?", (principal, person_id))
    db.commit()
    return get_person(person_id)


def delete_person(person_id):
    """Supprime un interlocuteur et ses attentes.

    Les taches qui lui etaient dues ne sont pas perdues : elles sont
    simplement detachees, comme a la suppression d'un projet.
    """
    db = get_db()
    curseur = db.execute("DELETE FROM person WHERE id = ?", (person_id,))
    db.commit()
    return curseur.rowcount > 0


def merge_people(keep_id, merge_id):
    """Absorbe un interlocuteur en double dans celui qu'on garde.

    Rien ne se perd : taches dues et attentes passent sur la fiche gardee, les
    champs vides y sont completes, les notes mises bout a bout, et le nom
    absorbe devient un alias. Tout se fait dans une seule transaction.

    :param keep_id: interlocuteur conserve
    :param merge_id: interlocuteur absorbe puis supprime
    :return: interlocuteur conserve, a jour
    :raises PersonError: identifiants identiques ou interlocuteur introuvable
    """
    if int(keep_id) == int(merge_id):
        raise PersonError("Un interlocuteur ne peut pas etre fusionne avec lui-meme.")

    garde = get_person(keep_id)
    absorbe = get_person(merge_id)
    if garde is None or absorbe is None:
        raise PersonError("Interlocuteur introuvable.")

    champs = {}
    for champ in ("role", "email", "phone", "project_id", "report_url"):
        if not garde[champ] and absorbe[champ]:
            champs[champ] = absorbe[champ]

    if absorbe["notes"]:
        notes = f"{garde['notes']}\n{absorbe['notes']}" if garde["notes"] else absorbe["notes"]
        champs["notes"] = notes[:MAX_TEXT_CHARS]

    champs["aliases"] = _join_aliases(
        garde["aliases"] + [absorbe["name"]] + absorbe["aliases"], garde["name"]
    )

    colonnes = ", ".join(f"{nom} = ?" for nom in champs)
    db = get_db()
    try:
        db.execute("UPDATE task SET person_id = ? WHERE person_id = ?", (keep_id, merge_id))
        db.execute("UPDATE waiting SET person_id = ? WHERE person_id = ?", (keep_id, merge_id))
        db.execute("UPDATE project SET manager_id = ? WHERE manager_id = ?", (keep_id, merge_id))
        db.execute(
            "INSERT OR IGNORE INTO person_project (person_id, project_id)"
            " SELECT ?, project_id FROM person_project WHERE person_id = ?",
            (keep_id, merge_id),
        )
        db.execute(
            f"UPDATE person SET {colonnes} WHERE id = ?", list(champs.values()) + [keep_id]
        )
        db.execute("DELETE FROM person WHERE id = ?", (merge_id,))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_person(keep_id)


def owed_tasks(person_id, status="todo"):
    """Retourne les taches dues a un interlocuteur : ce que je lui dois."""
    clause = "AND t.status = ?" if status else ""
    parametres = [person_id] + ([status] if status else [])

    rows = get_db().execute(
        "SELECT t.id, t.title, t.priority, t.deadline, t.status, pr.name AS project_name"
        " FROM task t LEFT JOIN project pr ON pr.id = t.project_id"
        f" WHERE t.person_id = ? {clause}"
        " ORDER BY CASE WHEN t.deadline IS NULL THEN 1 ELSE 0 END, t.deadline, t.priority",
        parametres,
    ).fetchall()

    return [dict(row) for row in rows]


# --- Attentes -------------------------------------------------------------------


def _waiting_dict(row, today=None):
    """Convertit une ligne de waiting, avec l'anciennete et le retard calcules."""
    today = today or date.today()
    cles = row.keys()

    jours = 0
    try:
        depuis = datetime.strptime(row["since"], "%Y-%m-%d").date()
        jours = (today - depuis).days
    except (TypeError, ValueError):
        pass

    return {
        "id": row["id"],
        "space": row["space"] if "space" in cles else space_model.DEFAULT,
        "person_id": row["person_id"],
        "person_name": row["person_name"] if "person_name" in cles else None,
        "subject": row["subject"],
        "since": row["since"],
        "due": row["due"],
        "status": row["status"],
        "task_id": row["task_id"],
        "task_title": row["task_title"] if "task_title" in cles else None,
        "days_open": max(0, jours),
        "overdue": bool(row["due"] and row["due"] < today.isoformat()),
    }


def _waiting_query(clause="", parametres=()):
    """Lit des attentes avec le nom de la personne et celui de la tache bloquee.

    :param clause: commence par WHERE (le filtre d'espace y est ajoute par les appelants)
    """
    return get_db().execute(
        "SELECT w.*, p.name AS person_name, t.title AS task_title"
        " FROM waiting w"
        " JOIN person p ON p.id = w.person_id"
        " LEFT JOIN task t ON t.id = w.task_id"
        f" {clause}"
        " ORDER BY w.since, w.id",
        parametres,
    ).fetchall()


def list_waiting(status="open", person_id=None, space=None):
    """Retourne les attentes, la plus ancienne d'abord.

    :param status: 'open', 'received', 'dropped', ou None pour tout
    :param person_id: restreint a un interlocuteur
    :param space: convention de models/space.py
    """
    conditions = []
    parametres = []
    filtre, valeurs = space_model.clause("w.space", space)
    if filtre:
        conditions.append(filtre[len(" AND "):])
        parametres.extend(valeurs)

    if status:
        if status not in VALID_STATUS:
            raise PersonError(f"Statut inconnu : {status}.")
        conditions.append("w.status = ?")
        parametres.append(status)

    if person_id is not None:
        conditions.append("w.person_id = ?")
        parametres.append(person_id)

    clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return [_waiting_dict(row) for row in _waiting_query(clause, tuple(parametres))]


def get_waiting(waiting_id):
    """Retourne une attente par son identifiant, ou None."""
    rows = _waiting_query("WHERE w.id = ?", (waiting_id,))
    return _waiting_dict(rows[0]) if rows else None


def create_waiting(data):
    """Enregistre une attente : quelque chose qu'on me doit.

    :raises PersonError: interlocuteur absent ou sujet vide
    """
    person_id = data.get("person_id")
    if person_id in (None, "", "none"):
        raise PersonError("L'interlocuteur est obligatoire.")

    person_id = int(person_id)
    if get_person(person_id) is None:
        raise PersonError(f"Interlocuteur {person_id} introuvable.")

    sujet = (data.get("subject") or "").strip()
    if not sujet:
        raise PersonError("Le sujet de l'attente est obligatoire.")

    depuis = _parse_day(data.get("since"), "La date de depart")
    echeance = _parse_day(data.get("due"), "L'echeance attendue")

    if depuis is None:
        # Saisie apres coup : « il devait me l'envoyer vendredi, on est mardi ».
        # L'attente a commence au plus tard a l'echeance promise, sinon son
        # anciennete repartirait de zero a chaque enregistrement tardif.
        depuis = min(echeance, date.today()) if echeance else date.today()
    elif echeance and echeance < depuis:
        # Depart explicite et posterieur a l'echeance : la saisie se contredit.
        raise PersonError("L'echeance precede le depart de l'attente.")

    tache = data.get("task_id")
    tache = int(tache) if tache not in (None, "", "none") else None
    db = get_db()
    # Espace : celui de la tache bloquee, sinon celui du contact s'il n'est
    # pas commun, sinon l'espace demande ou celui du perimetre.
    ligne = db.execute("SELECT space FROM task WHERE id = ?", (tache,)).fetchone() if tache else None
    contact = db.execute("SELECT space FROM person WHERE id = ?", (person_id,)).fetchone()
    if ligne:
        espace = ligne["space"]
    elif contact and contact["space"] in space_model.SPACES:
        espace = contact["space"]
    else:
        espace = space_model.for_new(data.get("space"))
    curseur = db.execute(
        "INSERT INTO waiting (person_id, task_id, subject, since, due, space)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            person_id,
            tache,
            sujet[:MAX_SUBJECT_CHARS],
            depuis.isoformat(),
            echeance.isoformat() if echeance else None,
            espace,
        ),
    )
    db.commit()
    return get_waiting(curseur.lastrowid)


def update_waiting(waiting_id, data):
    """Met a jour une attente. Retourne la version enregistree, ou None."""
    existant = get_waiting(waiting_id)
    if existant is None:
        return None

    champs = {}

    if "subject" in data:
        sujet = (data.get("subject") or "").strip()
        if not sujet:
            raise PersonError("Le sujet de l'attente est obligatoire.")
        champs["subject"] = sujet[:MAX_SUBJECT_CHARS]

    if "status" in data:
        statut = (data.get("status") or "").strip()
        if statut not in VALID_STATUS:
            raise PersonError(f"Statut inconnu : {statut}.")
        champs["status"] = statut

    if "due" in data:
        echeance = _parse_day(data.get("due"), "L'echeance attendue")
        champs["due"] = echeance.isoformat() if echeance else None

    if "task_id" in data:
        tache = data.get("task_id")
        champs["task_id"] = int(tache) if tache not in (None, "", "none") else None

    if not champs:
        return existant

    colonnes = ", ".join(f"{nom} = ?" for nom in champs)
    db = get_db()
    db.execute(f"UPDATE waiting SET {colonnes} WHERE id = ?", list(champs.values()) + [waiting_id])
    db.commit()
    return get_waiting(waiting_id)


def delete_waiting(waiting_id):
    """Supprime une attente. Retourne True si une ligne partait."""
    db = get_db()
    curseur = db.execute("DELETE FROM waiting WHERE id = ?", (waiting_id,))
    db.commit()
    return curseur.rowcount > 0


def mark_nudged(waiting_ids, moment=None):
    """Note que ces attentes viennent d'etre signalees.

    Sans cette marque, la relance du matin repeterait la meme ligne chaque
    jour jusqu'a ce qu'elle soit recue : au bout d'une semaine, le message
    devient un bruit qu'on ne lit plus.
    """
    if not waiting_ids:
        return 0

    horodatage = (moment or datetime.now()).strftime("%Y-%m-%d %H:%M")
    marques = ",".join("?" for _ in waiting_ids)

    db = get_db()
    curseur = db.execute(
        f"UPDATE waiting SET last_nudge_at = ? WHERE id IN ({marques})",
        [horodatage] + list(waiting_ids),
    )
    db.commit()
    return curseur.rowcount


def stale_waiting(min_days, quiet_days, today=None):
    """Retourne les attentes qui meritent une relance.

    :param min_days: anciennete minimale avant de signaler quoi que ce soit
    :param quiet_days: delai de silence entre deux signalements d'une meme attente
    :param today: jour de reference
    """
    today = today or date.today()
    retenues = []

    for attente in list_waiting(status="open"):
        en_retard = attente["overdue"]
        if attente["days_open"] < min_days and not en_retard:
            continue

        row = get_db().execute(
            "SELECT last_nudge_at FROM waiting WHERE id = ?", (attente["id"],)
        ).fetchone()
        dernier = row["last_nudge_at"] if row else None

        if dernier:
            try:
                depuis = (today - datetime.strptime(dernier[:10], "%Y-%m-%d").date()).days
                if depuis < quiet_days:
                    continue
            except ValueError:
                pass

        retenues.append(attente)

    retenues.sort(key=lambda item: -item["days_open"])
    return retenues
