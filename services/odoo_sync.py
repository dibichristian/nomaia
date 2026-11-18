"""Import en lecture seule des taches Odoo (project.task) assignees a l'utilisateur.

Methode reprise d'odoo_migrator :

- appariement par cle : l'identifiant Odoo, memorise sur la tache et le
  projet. La synchro se rejoue sans jamais creer de doublon ;
- parent ecrit en deuxieme passe : toutes les taches existent avant qu'on
  les rattache, quel que soit l'ordre de lecture ;
- lecture par lots.

Perimetre : les taches ouvertes assignees a l'utilisateur et rattachees a un
projet (les taches privees, comme l'accueil d'Odoo, n'interessent pas le
pilotage), plus leurs ascendants (une sous-tache a moi sous une tache d'un
collegue garde son arbre complet).

Partage des champs : Odoo fait foi pour le titre, la description,
l'echeance, le projet, l'ordre et le parent. Priorite et estimation ne sont
posees qu'a la creation : NOMAIA les ajuste ensuite. Etape, avancement,
epingle, notes et temps passe restent propres a NOMAIA.

Statut : une tache close dans Odoo (terminee, annulee, archivee, supprimee
ou sortie du perimetre) est close ici, avec une note qui dit pourquoi. Une
tache close ici n'est jamais rouverte par la synchro.
"""
import html
import json
import logging
import re
import threading
from datetime import datetime

from models import journal, setting
from models import project as project_model
from models import task as task_model
from services.odoo_client import PROTOCOLS, OdooClient, OdooConfig, OdooRpcError

logger = logging.getLogger("nomaia.odoo")

TASK_MODEL = "project.task"

# Etats clos d'une tache Odoo 17 a 19 (project.models.project_task.CLOSED_STATES).
CLOSED_STATES = ("1_done", "1_canceled")

TASK_FIELDS = [
    "name", "description", "parent_id", "project_id", "state", "date_deadline",
    "priority", "allocated_hours", "sequence", "active",
]

# Priorite Odoo vers NOMAIA (1 critique ... 4 basse). '0' est la valeur par
# defaut d'Odoo : elle vaut « normale », pas « basse ».
# Odoo 19 : 0 basse, 1 moyenne, 2 haute, 3 urgente.
PRIORITY_MAP = {"0": 3, "1": 3, "2": 2, "3": 1}
# Odoo 17 et 18 : 0 normale, 1 haute (l'etoile).
PRIORITY_MAP_STARRED = {"0": 3, "1": 2}

# Profondeur maximale remontee pour les ascendants : garde-fou contre une
# boucle si la base renvoyait un arbre incoherent.
MAX_ANCESTOR_DEPTH = 10

SOURCE = "odoo"

# Une seule synchro a la fois : deux clics rapproches creeraient les memes
# taches deux fois avant que l'index unique ne refuse la seconde.
_lock = threading.Lock()


class OdooSyncError(Exception):
    """Synchro impossible : configuration incomplete ou serveur en erreur."""


# ----------------------------------------------------------------------
# Connexion
# ----------------------------------------------------------------------
def build_client():
    """Construit le client Odoo a partir des reglages.

    :raises OdooSyncError: reglage manquant
    """
    url = (setting.get("odoo_url") or "").strip()
    database = (setting.get("odoo_db") or "").strip()
    protocol = setting.get("odoo_protocol") or "json2"
    login = (setting.get("odoo_login") or "").strip()
    secret = setting.get_secret("odoo_api_key")

    manquants = [label for label, valeur in (
        ("adresse", url), ("base", database), ("cle API", secret),
    ) if not valeur]
    if protocol == "jsonrpc" and not login:
        manquants.append("login")
    if protocol not in PROTOCOLS:
        raise OdooSyncError(f"Protocole Odoo inconnu : {protocol}")
    if manquants:
        raise OdooSyncError("Connexion Odoo incomplete : " + ", ".join(manquants) + ".")

    return OdooClient(OdooConfig(
        url=url, database=database, secret=secret, protocol=protocol, login=login,
    ))


def test_connection(client=None):
    """Verifie la connexion et retourne l'utilisateur Odoo et ses taches ouvertes.

    :return: {"user": nom, "login": login, "open_tasks": nombre}
    :raises OdooSyncError: connexion refusee
    """
    client = client or build_client()
    try:
        uid = client.authenticate()
        user = client.user_info()
        ouvertes = client.search(TASK_MODEL, _mine_domain(uid, _task_schema(client)))
    except OdooRpcError as error:
        raise OdooSyncError(str(error)) from error
    return {"user": user.get("name"), "login": user.get("login"), "open_tasks": len(ouvertes)}


# ----------------------------------------------------------------------
# Lecture Odoo
# ----------------------------------------------------------------------
def _task_schema(client):
    """Lit ce qui varie d'une version d'Odoo a l'autre sur project.task.

    Lu sur le serveur plutot que deduit du numero de version : une base 18
    n'a pas de modeles de tache (is_template, apparu en 19) et n'a que deux
    priorites.

    :return: {"has_template": bool, "priority_map": {cle Odoo: priorite NOMAIA}}
    """
    champs = client.fields_get(TASK_MODEL)
    priorites = {cle for cle, _libelle in (champs.get("priority") or {}).get("selection") or []}
    return {
        "has_template": "is_template" in champs,
        "priority_map": PRIORITY_MAP if len(priorites) > 2 else PRIORITY_MAP_STARRED,
    }


def _mine_domain(uid, schema):
    """Taches ouvertes assignees a l'utilisateur, dans un projet, hors modeles de tache."""
    domaine = [
        ("user_ids", "in", [uid]),
        ("state", "not in", list(CLOSED_STATES)),
        ("project_id", "!=", False),
    ]
    if schema["has_template"]:
        domaine.append(("is_template", "=", False))
    return domaine


def _m2o_id(value):
    """Identifiant d'un many2one lu par RPC ([id, nom] ou False)."""
    return value[0] if value else None


def _fetch_scope(client, uid, schema, warnings):
    """Lit les taches du perimetre : les miennes ouvertes et leurs ascendants.

    :return: {odoo_id: enregistrement Odoo}
    """
    records = {}
    for lot in client.iter_read(TASK_MODEL, _mine_domain(uid, schema), TASK_FIELDS):
        records.update({row["id"]: row for row in lot})

    # Ascendants : archives compris, sinon une tache parente archivee
    # laisserait sa sous-tache orpheline.
    demandes = set()
    for _niveau in range(MAX_ANCESTOR_DEPTH):
        manquants = {
            _m2o_id(row["parent_id"]) for row in records.values()
        } - set(records) - demandes - {None}
        if not manquants:
            break
        demandes |= manquants
        records.update({row["id"]: row for row in _read_by_ids(client, manquants, TASK_FIELDS)})

    # Tache parente illisible (droits) : ses sous-taches restent a la racine.
    illisibles = demandes - set(records)
    if illisibles:
        warnings.append(
            f"{len(illisibles)} tache(s) parente(s) illisible(s) dans Odoo :"
            " leurs sous-taches sont importees a la racine."
        )
    return records


def _read_by_ids(client, ids, fields):
    """Lit des taches par identifiant, archives comprises.

    search_read plutot que read : un identifiant supprime ou interdit est
    simplement absent du resultat, la ou read ferait echouer tout le lot.
    """
    return client.search_read(
        TASK_MODEL, [("id", "in", sorted(ids))], fields, context={"active_test": False}
    )


# ----------------------------------------------------------------------
# Conversion
# ----------------------------------------------------------------------
_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n\s*\n+")


def html_to_text(value):
    """Convertit la description HTML d'Odoo en texte brut lisible."""
    if not value:
        return None
    texte = _BLOCK_TAGS.sub("\n", str(value))
    texte = html.unescape(_TAGS.sub("", texte))
    texte = _BLANK_LINES.sub("\n\n", texte).strip()
    return texte or None


def _synced_values(record, project_id):
    """Champs dont Odoo fait foi, au format de models.task."""
    return {
        "title": (record.get("name") or "").strip()[:300] or "(sans titre)",
        "description": html_to_text(record.get("description")),
        "deadline": (record.get("date_deadline") or "")[:10] or None,
        "project_id": project_id,
        "position": int(record.get("sequence") or 0),
    }


def _initial_values(record, priority_map):
    """Champs poses a la creation seulement : NOMAIA les ajuste ensuite."""
    heures = float(record.get("allocated_hours") or 0)
    valeurs = {"priority": priority_map.get(str(record.get("priority")), 3)}
    if heures > 0:
        valeurs["estimate_min"] = max(5, min(1440, round(heures * 60)))
    return valeurs


def _closure_reason(record):
    """Raison de cloture d'une tache Odoo, ou None si elle reste ouverte."""
    if record is None:
        return "supprimee ou inaccessible dans Odoo"
    if record.get("state") == "1_done":
        return "terminee dans Odoo"
    if record.get("state") == "1_canceled":
        return "annulee dans Odoo"
    if not record.get("active", True):
        return "archivee dans Odoo"
    return None


# ----------------------------------------------------------------------
# Ecriture NOMAIA
# ----------------------------------------------------------------------
def _resolve_projects(records, report):
    """Fait correspondre chaque projet Odoo a un projet NOMAIA, cree au besoin.

    Apparie par lien Odoo (pose a l'import ou deplace par une fusion), puis
    par nom (un projet saisi a la main avant l'import est rattache, pas
    double). Un projet NOMAIA peut recevoir plusieurs projets Odoo.

    :return: {odoo_project_id: nomaia_project_id}
    """
    projets = {}
    for record in records.values():
        if record.get("project_id"):
            projets.setdefault(record["project_id"][0], record["project_id"][1])

    correspondance = {}
    for odoo_id, nom in projets.items():
        projet = project_model.find_by_odoo_id(odoo_id)
        if projet is None:
            # Odoo est la base professionnelle : ses projets vivent dans l'espace pro.
            projet = project_model.find_by_name(nom, space="pro")
            if projet is None:
                projet = project_model.create_project(
                    nom, color=project_model.suggest_color(nom), space="pro"
                )
                report["projects_created"] += 1
            project_model.link_odoo(projet["id"], odoo_id)
        correspondance[odoo_id] = projet["id"]
    return correspondance


def _close(task_id, reason, now):
    """Clot une tache NOMAIA et note pourquoi dans son journal."""
    task_model.complete_on(task_id, now.date().isoformat(), now.strftime("%H:%M"))
    journal.add_entry(f"Close par la synchro Odoo : {reason}.", task_id=task_id, source=SOURCE)


def _upsert_tasks(records, projects, index, priority_map, report, now):
    """Passe 1 : cree ou met a jour chaque tache, sans son parent.

    :return: {odoo_id: nomaia_task_id} pour tout le perimetre
    """
    ids = {}
    for odoo_id, record in records.items():
        valeurs = _synced_values(record, projects.get(_m2o_id(record.get("project_id"))))
        existante = index.get(odoo_id)

        if existante is None:
            sans_projet = {} if valeurs.get("project_id") else {"space": "pro"}
            creee = task_model.create_task(
                {**valeurs, **_initial_values(record, priority_map), **sans_projet}, source=SOURCE
            )
            task_model.link_odoo(creee["id"], odoo_id)
            ids[odoo_id] = creee["id"]
            report["created"] += 1
            raison = _closure_reason(record)
            if raison:
                # Ascendant deja clos dans Odoo : importe pour l'arbre, clos aussitot.
                _close(creee["id"], raison, now)
            continue

        ids[odoo_id] = existante["id"]
        changements = {cle: val for cle, val in valeurs.items() if existante.get(cle) != val}
        if changements:
            task_model.update_task(existante["id"], changements)
            report["updated"] += 1
        raison = _closure_reason(record)
        if raison and existante["status"] != "done":
            _close(existante["id"], raison, now)
            report["closed"] += 1
    return ids


def _link_parents(records, ids, report):
    """Passe 2 : rattache chaque tache a sa tache parente Odoo."""
    actuels = {row["id"]: row["parent_id"] for row in task_model.odoo_index().values()}
    for odoo_id, record in records.items():
        voulu = ids.get(_m2o_id(record.get("parent_id")))
        if actuels.get(ids[odoo_id]) != voulu:
            task_model.update_task(ids[odoo_id], {"parent_id": voulu})
            report["reparented"] += 1


def _close_out_of_scope(client, records, index, report, now):
    """Clot les taches importees encore ouvertes ici mais sorties du perimetre.

    Relit chacune dans Odoo (archives comprises) pour donner la vraie raison.
    """
    restantes = {
        odoo_id: task for odoo_id, task in index.items()
        if odoo_id not in records and task["status"] != "done"
    }
    if not restantes:
        return
    lues = {
        row["id"]: row
        for row in _read_by_ids(client, restantes, ["state", "active", "project_id"])
    }
    for odoo_id, task in restantes.items():
        lue = lues.get(odoo_id)
        raison = _closure_reason(lue) or (
            "tache sans projet dans Odoo" if not lue.get("project_id")
            else "sortie de ton perimetre (reassignee)"
        )
        _close(task["id"], raison, now)
        report["closed"] += 1


def sync(client=None, now=None):
    """Importe les taches Odoo assignees a l'utilisateur, avec leur hierarchie.

    :param client: client Odoo (injecte par les tests), construit sinon
    :param now: horodatage de reference, pour les tests
    :return: bilan {created, updated, closed, reparented, projects_created,
        total, warnings, synced_at}
    :raises OdooSyncError: configuration incomplete, serveur en erreur ou
        synchro deja en cours
    """
    if not _lock.acquire(blocking=False):
        raise OdooSyncError("Une synchronisation Odoo est deja en cours.")
    try:
        client = client or build_client()
        now = now or datetime.now().replace(microsecond=0)
        report = {
            "created": 0, "updated": 0, "closed": 0, "reparented": 0,
            "projects_created": 0, "total": 0, "warnings": [],
        }
        try:
            uid = client.authenticate()
            schema = _task_schema(client)
            records = _fetch_scope(client, uid, schema, report["warnings"])
            index = task_model.odoo_index()
            projets = _resolve_projects(records, report)
            ids = _upsert_tasks(records, projets, index, schema["priority_map"], report, now)
            _link_parents(records, ids, report)
            _close_out_of_scope(client, records, index, report, now)
        except OdooRpcError as error:
            raise OdooSyncError(f"Odoo : {error}") from error

        report["total"] = len(records)
        report["synced_at"] = now.isoformat(sep=" ")
        setting.set_value("odoo_last_sync", report["synced_at"])
        setting.set_value("odoo_last_report", json.dumps(report, ensure_ascii=False))
        logger.info("Synchro Odoo : %s", report)
        return report
    finally:
        _lock.release()


def last_report():
    """Retourne le bilan de la derniere synchro, ou None."""
    brut = setting.get("odoo_last_report")
    try:
        return json.loads(brut) if brut else None
    except ValueError:
        return None
