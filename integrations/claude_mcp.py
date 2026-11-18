"""Serveur MCP local : relie les sessions Claude Code aux taches NOMAIA.

Lance par Claude Code (transport stdio), il traduit les appels d'outils en
requetes vers l'API locale de NOMAIA (/api/agent/). Bibliotheque standard
uniquement : il tourne avec n'importe quel Python 3.8+ du poste, sans
installation.

Enregistrement, une seule fois (commande affichee dans NOMAIA > Config) :

    claude mcp add --scope user nomaia -e NOMAIA_AGENT_KEY=<cle> -- python <chemin>/claude_mcp.py

Variables d'environnement :
    NOMAIA_AGENT_KEY  cle des sessions Claude (obligatoire)
    NOMAIA_URL        adresse de NOMAIA (defaut http://127.0.0.1:5555)

Le flux stdout ne transporte QUE le protocole JSON-RPC ; tout diagnostic part
sur stderr, sans quoi Claude Code recevrait des messages illisibles.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER_NAME = "nomaia"
SERVER_VERSION = "1.8.1"

# Versions du protocole comprises. Celle demandee par le client est reprise si
# elle figure ici, sinon la plus recente est proposee.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

DEFAULT_URL = "http://127.0.0.1:5555"
HTTP_TIMEOUT_S = 15

# Consigne remise a Claude Code a la connexion : c'est elle qui decide quand
# une session ecrit dans NOMAIA. Une tache par question posee noierait le
# suivi ; aucune tache pour un vrai travail le rendrait inutile.
INSTRUCTIONS = """NOMAIA est le gestionnaire de taches personnel de l'utilisateur. Ces outils relient le travail de cette session a ses taches, pour un suivi reel de l'avancement.

Quand cette session fait un VRAI travail (code ecrit ou corrige, analyse livree, document produit) :
1. Au debut, cherche la tache correspondante (nomaia_list_tasks avec query). Si elle existe, reprends-la ; sinon cree-la (nomaia_create_task) avec un titre court a l'infinitif et le projet s'il est connu (nomaia_list_projects).
2. Aux etapes cles, note l'avancement (nomaia_update_task : progress_pct et une phrase). Si la tache se bloque, attend le client, ou change d'echeance, de priorite, de projet ou d'etape du projet, renseigne-le aussi avec nomaia_update_task (nomaia_list_phases donne les etapes). Quand une etape du projet est atteinte ou finie, mets-la a jour (nomaia_update_phase) : terminer une etape termine aussi les precedentes.
4. Un bug remonte par le client est une tache kind=anomaly (gravite, module) ; un fichier ou un PV attendu du client est une tache kind=deliverable. Une decision actee, un changement de perimetre ou une dependance va au registre du projet (nomaia_add_record).
5. Interlocuteurs : une demande faite par un client ou un collegue est une tache pour lui (person = son nom) ; ce que l'utilisateur attend de quelqu'un (fichier, validation, acces, reponse) est une attente (nomaia_add_waiting), suivie par les relances ; une attente satisfaite se clot (nomaia_resolve_waiting). Un contact nouveau ou une coordonnee apprise en cours de travail va dans sa fiche (nomaia_save_person).
3. En fin de travail, declare le temps passe (nomaia_log_work : temps REELLEMENT ECOULE a l'horloge depuis le debut du travail ou la derniere declaration, jamais une estimation de l'effort humain equivalent ; et un resume) et clos la tache si elle est terminee (status=done).

Notes de la base de connaissances et comptes rendus : cree-les ou modifie-les (nomaia_create_note, nomaia_update_note, nomaia_create_minutes, nomaia_update_minutes) SEULEMENT quand l'utilisateur le demande. Avant d'en creer un, cherche s'il existe deja (nomaia_search) et complete-le plutot que d'ouvrir un doublon.

Ne cree PAS de tache pour une simple question, une explication ou une exploration sans livrable. Ne cree un projet (nomaia_create_project) que si l'utilisateur le demande, ou si aucun projet existant (nomaia_list_projects) ne correspond au travail en cours. Si l'utilisateur demande ses taches, utilise nomaia_list_tasks. Si NOMAIA est injoignable, poursuis le travail sans bloquer et signale-le une seule fois."""

KIND_HELP = "task ; anomaly = bug remonte par le client ; deliverable = fichier ou PV attendu du client"

PERSON_HELP = "a qui je dois cette tache (client ou collegue qui l'a demandee) ; contact cree s'il est inconnu"

# Champs communs a la creation et a la modification des notes et comptes rendus.
NOTE_PROPERTIES = {
    "title": {"type": "string", "description": "200 caracteres max"},
    "content": {"type": "string", "description": "Markdown"},
    "category": {
        "type": "string",
        "enum": ["python", "xml", "js_owl", "sql", "config", "bug_solution", "deployment", "functional", "other"],
    },
    "project": {"type": "string", "description": "nom exact du projet"},
    "odoo_version": {"type": "string", "description": "ex. 17.0"},
    "module": {"type": "string", "description": "module Odoo concerne"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "pinned": {"type": "boolean"},
}

MINUTES_PROPERTIES = {
    "title": {"type": "string"},
    "held_on": {"type": "string", "description": "AAAA-MM-JJ, defaut aujourd'hui a la creation"},
    "kind": {"type": "string", "enum": ["meeting", "intervention", "training"], "description": "defaut meeting"},
    "project": {"type": "string", "description": "nom exact du projet"},
    "attendees": {"type": "string", "description": "participants"},
    "objectives": {"type": "string"},
    "discussed": {"type": "string", "description": "points discutes"},
    "topics": {"type": "string", "description": "sujets enseignes (formation)"},
    "followups": {"type": "string", "description": "points a revoir (intervention, formation)"},
    "notes": {"type": "string"},
    "status": {"type": "string", "enum": ["draft", "final"]},
}

MINUTES_ITEMS = {
    "type": "array",
    "description": "decisions, actions et demandes du client, 50 au plus",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["decision", "action", "request"]},
            "text": {"type": "string"},
            "owner": {"type": "string", "description": "responsable"},
            "due": {"type": "string", "description": "AAAA-MM-JJ"},
        },
        "required": ["type", "text"],
    },
}

TOOLS = [
    {
        "name": "nomaia_list_tasks",
        "description": "Liste les taches de l'utilisateur (ouvertes par defaut), filtrables par projet et par mots du titre.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["todo", "done", "all"], "description": "defaut todo"},
                "project": {"type": "string", "description": "nom exact du projet"},
                "query": {"type": "string", "description": "mots a retrouver dans le titre ou la description"},
                "kind": {"type": "string", "enum": ["task", "anomaly", "deliverable"], "description": "nature des taches"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "nomaia_get_task",
        "description": "Detail d'une tache : description, etapes et journal d'avancement recent.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "nomaia_create_task",
        "description": (
            "Cree une tache pour le travail en cours. Si une tache au titre proche existe,"
            " rien n'est cree et les taches proches sont renvoyees : reprends-en une, ou"
            " rappelle avec force=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "verbe a l'infinitif + objet, 80 caracteres max"},
                "description": {"type": "string"},
                "project": {"type": "string", "description": "nom exact d'un projet existant"},
                "priority": {"type": "integer", "minimum": 1, "maximum": 4, "description": "1 critique ... 4 basse, defaut 3"},
                "estimate_min": {"type": "integer", "minimum": 5, "maximum": 1440},
                "deadline": {"type": "string", "description": "AAAA-MM-JJ"},
                "parent_id": {"type": "integer", "description": "tache parente si c'est une etape"},
                "phase": {"type": "string", "description": "etape du projet : numero ('5') ou nom (nomaia_list_phases)"},
                "blocked": {"type": "boolean", "description": "tache bloquee"},
                "side": {"type": "string", "enum": ["us", "client", "both"], "description": "qui doit agir"},
                "kind": {"type": "string", "enum": ["task", "anomaly", "deliverable"], "description": KIND_HELP},
                "severity": {"type": "string", "enum": ["critical", "major", "minor"], "description": "gravite d'une anomalie, defaut major"},
                "module": {"type": "string", "description": "module concerne par une anomalie (Ventes, RH / Paie...)"},
                "person": {"type": "string", "description": PERSON_HELP},
                "force": {"type": "boolean"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "nomaia_update_task",
        "description": (
            "Note l'avancement d'une tache (pourcentage, commentaire, cloture status=done) et ajuste"
            " son cadrage : echeance, priorite, projet, etape, blocage, qui doit agir. Un champ omis"
            " reste inchange ; une chaine vide efface deadline, project, phase ou side."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "progress_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                "note": {"type": "string", "description": "ce qui a avance, decide ou bloque"},
                "status": {"type": "string", "enum": ["done", "todo"]},
                "deadline": {"type": "string", "description": "AAAA-MM-JJ"},
                "priority": {"type": "integer", "minimum": 1, "maximum": 4, "description": "1 critique ... 4 basse"},
                "project": {"type": "string", "description": "nom exact du projet ; la tache perd son etape sauf phase fournie"},
                "phase": {"type": "string", "description": "etape du projet : numero ('5') ou nom (nomaia_list_phases)"},
                "blocked": {"type": "boolean", "description": "true bloque, false debloque"},
                "side": {"type": "string", "enum": ["us", "client", "both", ""], "description": "qui doit agir"},
                "kind": {"type": "string", "enum": ["task", "anomaly", "deliverable"], "description": KIND_HELP},
                "severity": {"type": "string", "enum": ["critical", "major", "minor"], "description": "gravite d'une anomalie"},
                "module": {"type": "string", "description": "module concerne par une anomalie"},
                "person": {"type": "string", "description": PERSON_HELP + " ; chaine vide pour retirer"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "nomaia_log_work",
        "description": (
            "Declare le temps passe sur une tache et ce qui a ete fait. Compte dans le bilan de l'utilisateur."
            " minutes = temps reellement ecoule a l'horloge, pas l'effort estime ; un avertissement"
            " (warning) signale une duree superieure au temps ecoule depuis la tache ou la derniere declaration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "minutes": {"type": "integer", "minimum": 1, "maximum": 600},
                "summary": {"type": "string", "description": "une ou deux phrases sur le travail fait"},
            },
            "required": ["task_id", "minutes", "summary"],
        },
    },
    {
        "name": "nomaia_list_projects",
        "description": "Liste les projets actifs ou en pause, avec leurs taches ouvertes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "nomaia_search",
        "description": (
            "Recherche plein texte dans NOMAIA : taches, projets, journal (notes, travail, decisions),"
            " contacts, evenements de l'agenda, comptes rendus et notes de la base de connaissances."
            " Accents et casse ignores, mots en prefixe. Utile pour retrouver une decision, un echange"
            " ou le contexte d'un sujet. kind=note : id pour nomaia_get_note ; kind=cr ou cr_element :"
            " l'identifiant du compte rendu est la fin de l'url (/comptes-rendus/<id>)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "mots a chercher (deux caracteres au moins)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "nomaia_create_project",
        "description": (
            "Cree un projet. Si un projet du meme nom existe deja (casse ignoree), rien n'est"
            " cree et le projet existant est renvoye : utilise-le."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "nom du projet, 120 caracteres max"},
                "deadline": {"type": "string", "description": "AAAA-MM-JJ"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "nomaia_add_record",
        "description": (
            "Inscrit au registre d'un projet une decision actee, un changement de perimetre (scope)"
            " ou une dependance. follow_up=true ouvre une tache de suivi pour un point a arbitrer ou a lever."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "nom exact du projet"},
                "kind": {"type": "string", "enum": ["decision", "scope", "dependency"]},
                "content": {"type": "string", "description": "objet, en une ou deux phrases"},
                "follow_up": {"type": "boolean", "description": "scope ou dependency seulement"},
            },
            "required": ["project", "kind", "content"],
        },
    },
    {
        "name": "nomaia_update_phase",
        "description": (
            "Fait avancer une etape d'un projet : statut, date prevue, date de fin, avancement."
            " Terminer une etape termine aussi les etapes precedentes encore ouvertes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "nom exact du projet"},
                "phase": {"type": "string", "description": "numero ('5') ou nom de l'etape"},
                "status": {"type": "string", "enum": ["todo", "doing", "blocked", "done"]},
                "planned_date": {"type": "string", "description": "AAAA-MM-JJ, chaine vide pour retirer"},
                "done_date": {"type": "string", "description": "AAAA-MM-JJ, defaut aujourd'hui si done"},
                "progress_pct": {"type": "integer", "minimum": 0, "maximum": 100, "description": "seulement si l'etape n'a pas de taches"},
            },
            "required": ["project", "phase"],
        },
    },
    {
        "name": "nomaia_list_phases",
        "description": "Etapes d'un projet (numero, nom, statut, avancement) et etape en cours : ou ranger une tache.",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "nom exact du projet"}},
            "required": ["project"],
        },
    },
    {
        "name": "nomaia_progress_report",
        "description": (
            "Rapport d'avancement destine a un chef de projet : tous ses projets en cours,"
            " chiffres calcules par NOMAIA (avancement, etapes, fait, a venir, anomalies,"
            " livrables client, registre) et message WhatsApp pret a relire. Sans manager,"
            " liste les chefs de projet. Sert a publier ou mettre a jour son tableau de bord."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "manager": {"type": "string", "description": "nom du contact chef de projet"},
                "start": {"type": "string", "description": "AAAA-MM-JJ, defaut il y a six jours"},
                "end": {"type": "string", "description": "AAAA-MM-JJ, defaut aujourd'hui"},
            },
        },
    },
    {
        "name": "nomaia_set_report_url",
        "description": (
            "Retient le lien du tableau de bord publie pour un chef de projet ; il est ajoute"
            " au message WhatsApp du rapport. Chaine vide pour l'effacer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "manager": {"type": "string", "description": "nom du contact chef de projet"},
                "url": {"type": "string", "description": "lien https du tableau de bord"},
            },
            "required": ["manager", "url"],
        },
    },
    {
        "name": "nomaia_list_people",
        "description": (
            "Contacts de l'utilisateur (clients, collegues) : role, projet, ce qu'il leur doit (i_owe :"
            " taches) et ce qu'ils lui doivent (they_owe : attentes). query filtre par nom, alias, role ou projet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string", "description": "contacts rattaches a ce projet"},
            },
        },
    },
    {
        "name": "nomaia_save_person",
        "description": (
            "Cree un contact ou complete sa fiche si le nom (ou un alias) est deja connu : seuls les"
            " champs fournis changent, les notes s'ajoutent. new_name renomme (l'ancien nom reste un alias)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "nom du contact (exact, alias ou nom partiel unique)"},
                "role": {"type": "string", "description": "ex. chef de projet, comptable, DSI"},
                "project": {"type": "string", "description": "projet a ajouter (un contact peut en suivre plusieurs)"},
                "remove_project": {"type": "string", "description": "projet a retirer de sa fiche"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "notes": {"type": "string", "description": "ajoutees aux notes existantes"},
                "new_name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "nomaia_list_waiting",
        "description": "Ce que l'utilisateur attend des autres (attentes ouvertes par defaut), la plus ancienne d'abord, avec le retard.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "restreindre a un contact"},
                "status": {"type": "string", "enum": ["open", "received", "dropped", "all"]},
            },
        },
    },
    {
        "name": "nomaia_add_waiting",
        "description": (
            "Enregistre ce que quelqu'un doit a l'utilisateur (fichier, validation, acces, reponse)."
            " Les relances automatiques de NOMAIA le suivent. Contact cree s'il est inconnu ; une"
            " attente ouverte au meme sujet n'est pas doublee."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "qui doit quelque chose"},
                "subject": {"type": "string", "description": "ce qui est attendu, en une phrase"},
                "due": {"type": "string", "description": "AAAA-MM-JJ, date promise"},
                "task_id": {"type": "integer", "description": "tache bloquee par cette attente"},
                "project": {"type": "string", "description": "projet du contact s'il est cree"},
            },
            "required": ["person", "subject"],
        },
    },
    {
        "name": "nomaia_resolve_waiting",
        "description": "Clot une attente : received (recu) ou dropped (abandonne). Identifiant via nomaia_list_waiting.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "waiting_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["received", "dropped"], "description": "defaut received"},
            },
            "required": ["waiting_id"],
        },
    },
    {
        "name": "nomaia_get_note",
        "description": "Lit une note de la base de connaissances, contenu Markdown compris (id via nomaia_search).",
        "inputSchema": {
            "type": "object",
            "properties": {"note_id": {"type": "integer"}},
            "required": ["note_id"],
        },
    },
    {
        "name": "nomaia_create_note",
        "description": (
            "Cree une note de la base de connaissances (solution, extrait de code, procedure)."
            " Seulement a la demande de l'utilisateur. Un secret en clair est refuse : ecrire *** et"
            " ou le secret est range."
        ),
        "inputSchema": {
            "type": "object",
            "properties": NOTE_PROPERTIES,
            "required": ["title"],
        },
    },
    {
        "name": "nomaia_update_note",
        "description": (
            "Modifie une note : seuls les champs fournis changent. append=true ajoute content a la"
            " fin de la note au lieu de la remplacer. project vide detache la note de son projet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_id": {"type": "integer"},
                **NOTE_PROPERTIES,
                "append": {"type": "boolean", "description": "ajouter content a la fin, defaut false"},
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "nomaia_get_minutes",
        "description": (
            "Lit un compte rendu : sections et elements (decisions, actions, demandes), avec la tache"
            " creee pour chaque action. Identifiant via nomaia_search (fin de l'url)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"minutes_id": {"type": "integer"}},
            "required": ["minutes_id"],
        },
    },
    {
        "name": "nomaia_create_minutes",
        "description": (
            "Cree un compte rendu de reunion, d'intervention ou de formation, avec ses elements."
            " Seulement a la demande de l'utilisateur. status=final le valide et renvoie les actions et"
            " decisions restant a transformer en taches ou en registre (dans l'interface)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **MINUTES_PROPERTIES,
                "items": MINUTES_ITEMS,
            },
            "required": ["title"],
        },
    },
    {
        "name": "nomaia_update_minutes",
        "description": (
            "Modifie un compte rendu : une section fournie remplace l'ancienne, les champs omis"
            " restent inchanges. add_items ajoute des elements ; status=final valide, draft rouvre."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes_id": {"type": "integer"},
                **MINUTES_PROPERTIES,
                "add_items": MINUTES_ITEMS,
            },
            "required": ["minutes_id"],
        },
    },
]


def log(message):
    """Diagnostic sur stderr : stdout est reserve au protocole."""
    sys.stderr.write(f"[nomaia-mcp] {message}\n")
    sys.stderr.flush()


class NomaiaUnavailable(RuntimeError):
    """NOMAIA ne repond pas, ou refuse la requete."""


def call_nomaia(method, path, payload=None, query=None):
    """Appelle l'API /api/agent/ de NOMAIA et retourne le JSON recu.

    :raises NomaiaUnavailable: cle absente, NOMAIA arrete ou erreur renvoyee
    """
    cle = os.environ.get("NOMAIA_AGENT_KEY", "").strip()
    if not cle:
        raise NomaiaUnavailable(
            "NOMAIA_AGENT_KEY absente : genere la cle dans NOMAIA > Config > Connexions > Sessions Claude."
        )

    url = os.environ.get("NOMAIA_URL", DEFAULT_URL).rstrip("/") + "/api/agent" + path
    if query:
        filtres = {key: value for key, value in query.items() if value not in (None, "")}
        if filtres:
            url += "?" + urllib.parse.urlencode(filtres)

    corps = json.dumps(payload).encode("utf-8") if payload is not None else None
    requete = urllib.request.Request(url, data=corps, method=method)
    requete.add_header("Authorization", "Bearer " + cle)
    requete.add_header("Accept", "application/json")
    if corps is not None:
        requete.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(requete, timeout=HTTP_TIMEOUT_S) as reponse:
            return json.loads(reponse.read().decode("utf-8"))
    except urllib.error.HTTPError as erreur:
        try:
            detail = json.loads(erreur.read().decode("utf-8")).get("error")
        except ValueError:
            detail = None
        raise NomaiaUnavailable(detail or f"NOMAIA a repondu HTTP {erreur.code}.")
    except (urllib.error.URLError, OSError) as erreur:
        raise NomaiaUnavailable(f"NOMAIA injoignable ({erreur}). Est-il lance ?")


def run_tool(name, arguments):
    """Execute un outil et retourne le resultat JSON de NOMAIA."""
    args = arguments or {}
    if name == "nomaia_list_tasks":
        return call_nomaia("GET", "/tasks", query={
            "status": args.get("status"), "project": args.get("project"),
            "q": args.get("query"), "limit": args.get("limit"), "kind": args.get("kind"),
        })
    if name == "nomaia_get_task":
        return call_nomaia("GET", f"/tasks/{int(args['task_id'])}")
    if name == "nomaia_create_task":
        return call_nomaia("POST", "/tasks", payload=args)
    if name == "nomaia_update_task":
        task_id = int(args.pop("task_id"))
        return call_nomaia("POST", f"/tasks/{task_id}/progress", payload=args)
    if name == "nomaia_log_work":
        task_id = int(args.pop("task_id"))
        return call_nomaia("POST", f"/tasks/{task_id}/work", payload=args)
    if name == "nomaia_list_projects":
        return call_nomaia("GET", "/projects")
    if name == "nomaia_create_project":
        return call_nomaia("POST", "/projects", payload=args)
    if name == "nomaia_search":
        return call_nomaia("GET", "/search", query={"q": args.get("query"), "limit": args.get("limit")})
    if name == "nomaia_add_record":
        return call_nomaia("POST", "/records", payload=args)
    if name == "nomaia_update_phase":
        return call_nomaia("POST", "/phases/update", payload=args)
    if name == "nomaia_list_phases":
        return call_nomaia("GET", "/phases", query={"project": args["project"]})
    if name == "nomaia_progress_report":
        return call_nomaia("GET", "/progress-report", query={
            "manager": args.get("manager"), "start": args.get("start"), "end": args.get("end"),
        })
    if name == "nomaia_set_report_url":
        return call_nomaia("POST", "/progress-report/url", payload=args)
    if name == "nomaia_list_people":
        return call_nomaia("GET", "/people", query={"q": args.get("query"), "project": args.get("project")})
    if name == "nomaia_save_person":
        return call_nomaia("POST", "/people", payload=args)
    if name == "nomaia_list_waiting":
        return call_nomaia("GET", "/waiting", query={"person": args.get("person"), "status": args.get("status")})
    if name == "nomaia_add_waiting":
        return call_nomaia("POST", "/waiting", payload=args)
    if name == "nomaia_resolve_waiting":
        waiting_id = int(args.pop("waiting_id"))
        return call_nomaia("POST", f"/waiting/{waiting_id}/resolve", payload=args)
    if name == "nomaia_get_note":
        return call_nomaia("GET", f"/notes/{int(args['note_id'])}")
    if name == "nomaia_create_note":
        return call_nomaia("POST", "/notes", payload=args)
    if name == "nomaia_update_note":
        note_id = int(args.pop("note_id"))
        return call_nomaia("PATCH", f"/notes/{note_id}", payload=args)
    if name == "nomaia_get_minutes":
        return call_nomaia("GET", f"/minutes/{int(args['minutes_id'])}")
    if name == "nomaia_create_minutes":
        return call_nomaia("POST", "/minutes", payload=args)
    if name == "nomaia_update_minutes":
        minutes_id = int(args.pop("minutes_id"))
        return call_nomaia("PATCH", f"/minutes/{minutes_id}", payload=args)
    raise KeyError(name)


def _result(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code, message):
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle(message):
    """Traite un message JSON-RPC. Retourne la reponse, ou None pour une notification."""
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None:
        # Notification (initialized, cancelled...) : jamais de reponse.
        return None

    if method == "initialize":
        demandee = params.get("protocolVersion")
        version = demandee if demandee in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(message_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        })

    if method == "ping":
        return _result(message_id, {})

    if method == "tools/list":
        return _result(message_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        if name not in {tool["name"] for tool in TOOLS}:
            return _error(message_id, -32602, f"Outil inconnu : {name}")
        try:
            data = run_tool(name, dict(params.get("arguments") or {}))
            texte, en_erreur = json.dumps(data, ensure_ascii=False, indent=1), False
        except NomaiaUnavailable as erreur:
            texte, en_erreur = str(erreur), True
        except (KeyError, TypeError, ValueError) as erreur:
            texte, en_erreur = f"Arguments invalides : {erreur}", True
        return _result(message_id, {
            "content": [{"type": "text", "text": texte}],
            "isError": en_erreur,
        })

    return _error(message_id, -32601, f"Methode non prise en charge : {method}")


def main():
    """Boucle stdio : un message JSON par ligne, une reponse par ligne."""
    entree = sys.stdin.buffer
    sortie = sys.stdout.buffer
    log("serveur demarre")
    for ligne in entree:
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            message = json.loads(ligne.decode("utf-8"))
        except ValueError:
            reponse = _error(None, -32700, "JSON illisible.")
        else:
            if not isinstance(message, dict):
                reponse = _error(None, -32600, "Requete JSON-RPC attendue (objet).")
                sortie.write(json.dumps(reponse).encode("utf-8") + b"\n")
                sortie.flush()
                continue
            try:
                reponse = handle(message)
            except Exception as erreur:  # noqa: BLE001 - le serveur ne doit jamais tomber
                log(f"erreur inattendue : {erreur!r}")
                reponse = _error(message.get("id"), -32603, "Erreur interne du serveur NOMAIA.")
        if reponse is not None:
            sortie.write(json.dumps(reponse, ensure_ascii=False).encode("utf-8") + b"\n")
            sortie.flush()


if __name__ == "__main__":
    main()
