"""Recherche universelle : resultats de l'index mis en forme pour la palette et le MCP.

Chaque resultat porte un libelle, une ligne de contexte et l'adresse ou
l'ouvrir. Les details (projet, statut, date) sont lus en une requete par
nature, jamais une par resultat.
"""
from models import get_db
from models import search as search_model
from models import space as space_model

STATUS_LABELS = {"todo": "à faire", "done": "terminée"}
KIND_LABELS = {
    "tache": "Tâche", "projet": "Projet", "journal": "Journal", "contact": "Contact", "evenement": "Événement",
    "cr": "Compte rendu", "cr_element": "Compte rendu", "note": "Note",
}
ITEM_LABELS = {"decision": "Décision", "action": "Action", "request": "Demande"}


def _rows(sql, ids):
    """Lignes indexees par id pour une liste d'identifiants."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {row["id"]: dict(row) for row in get_db().execute(sql.format(ids=marks), list(ids)).fetchall()}


def _clean_snippet(snippet):
    """Extrait du corps, avec les crochets de mise en evidence retires (le client surligne lui-meme)."""
    return " ".join((snippet or "").replace("[", "").replace("]", "").split())


def _hidden(kind, item_id, space, rows_by_kind):
    """Indique si l'element est hors du perimetre (les elements communs restent visibles)."""
    lignes = rows_by_kind.get(kind)
    if not lignes or item_id not in lignes:
        return False
    return not space_model.visible(lignes[item_id]["space"], space)


def search(text, limit=20, space=None):
    """Resultats mis en forme : {kind, kind_label, id, label, sub, url, project}.

    :param text: saisie libre
    :param limit: nombre maximal de resultats
    :param space: convention de models/space.py : les elements de l'autre
        espace sont ecartes, toutes natures confondues (contacts et notes
        communs restent visibles)
    """
    space = space_model.resolve(space)
    hits = search_model.search(text, limit=limit)
    ids = {}
    for hit in hits:
        ids.setdefault(hit["kind"], []).append(hit["id"])

    tasks = _rows(
        "SELECT t.id, t.title, t.status, t.deadline, t.kind, t.project_id, t.space, p.name AS project"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id WHERE t.id IN ({ids})",
        ids.get("tache"),
    )
    projects = _rows("SELECT id, name, status, deadline, space FROM project WHERE id IN ({ids})", ids.get("projet"))
    notes = _rows(
        "SELECT n.id, n.content, n.kind, n.created_at, n.task_id, n.project_id,"
        " coalesce(p.name, tp.name) AS project, coalesce(n.project_id, t.project_id) AS any_project_id,"
        " t.title AS task_title, n.space"
        " FROM note n LEFT JOIN task t ON t.id = n.task_id LEFT JOIN project p ON p.id = n.project_id"
        " LEFT JOIN project tp ON tp.id = t.project_id WHERE n.id IN ({ids})",
        ids.get("journal"),
    )
    people = _rows(
        "SELECT pe.id, pe.name, pe.role, pe.space,"
        " (SELECT group_concat(p.name, ', ') FROM person_project pp"
        "   JOIN project p ON p.id = pp.project_id WHERE pp.person_id = pe.id) AS project"
        " FROM person pe WHERE pe.id IN ({ids})",
        ids.get("contact"),
    )
    events = _rows(
        "SELECT e.id, e.title, e.starts_at, e.location, e.space, p.name AS project FROM event e"
        " LEFT JOIN project p ON p.id = e.project_id WHERE e.id IN ({ids})",
        ids.get("evenement"),
    )

    minutes = _rows(
        "SELECT m.id, m.title, m.held_on, m.kind, m.space, p.name AS project FROM minutes m"
        " LEFT JOIN project p ON p.id = m.project_id WHERE m.id IN ({ids})",
        ids.get("cr"),
    )
    items = _rows(
        "SELECT i.id, i.type, i.text, i.minutes_id, m.title, m.held_on, m.space, p.name AS project"
        " FROM minutes_item i"
        " JOIN minutes m ON m.id = i.minutes_id LEFT JOIN project p ON p.id = m.project_id WHERE i.id IN ({ids})",
        ids.get("cr_element"),
    )

    kb = _rows(
        "SELECT k.id, k.title, k.category, k.odoo_version, k.module, k.space, p.name AS project"
        " FROM kb_note k"
        " LEFT JOIN project p ON p.id = k.project_id WHERE k.id IN ({ids})",
        ids.get("note"),
    )

    rows_by_kind = {"tache": tasks, "projet": projects, "journal": notes, "contact": people,
                    "evenement": events, "cr": minutes, "cr_element": items, "note": kb}
    results = []
    for hit in hits:
        kind, item_id = hit["kind"], hit["id"]
        result = {"kind": kind, "kind_label": KIND_LABELS[kind], "id": item_id}
        if space and _hidden(kind, item_id, space, rows_by_kind):
            continue
        lignes = rows_by_kind.get(kind) or {}
        if item_id in lignes:
            result["space"] = lignes[item_id]["space"]
        if kind == "tache" and item_id in tasks:
            t = tasks[item_id]
            details = [t["project"] or "Sans projet", STATUS_LABELS.get(t["status"], t["status"])]
            if t["kind"] == "anomaly":
                details.insert(0, "anomalie")
            elif t["kind"] == "deliverable":
                details.insert(0, "livrable")
            if t["deadline"]:
                details.append("échéance " + t["deadline"])
            result.update(label=t["title"], sub=" · ".join(details), project=t["project"],
                          url=f"/plan?tache={item_id}", done=t["status"] == "done")
        elif kind == "projet" and item_id in projects:
            p = projects[item_id]
            result.update(label=p["name"], sub="Projet · " + (p["status"] or ""), project=p["name"],
                          url=f"/projets/{item_id}")
        elif kind == "journal" and item_id in notes:
            n = notes[item_id]
            context = n["task_title"] or n["project"] or "Journal"
            result.update(label=_clean_snippet(hit["snippet"]) or n["content"][:120],
                          sub=f"{context} · {(n['created_at'] or '')[:10]}", project=n["project"],
                          url=(f"/projets/{n['any_project_id']}" if n["any_project_id"]
                               else f"/plan?tache={n['task_id']}" if n["task_id"] else "/taches"))
        elif kind == "contact" and item_id in people:
            pe = people[item_id]
            result.update(label=pe["name"], sub=" · ".join(x for x in (pe["role"], pe["project"]) if x) or "Contact",
                          project=pe["project"], url=f"/contacts?personne={item_id}")
        elif kind == "evenement" and item_id in events:
            e = events[item_id]
            day = (e["starts_at"] or "")[:10]
            result.update(label=e["title"], sub=" · ".join(x for x in ((e["starts_at"] or "")[:16].replace("T", " "),
                                                                          e["location"], e["project"]) if x),
                          project=e["project"], url=f"/agenda?date={day}" if day else "/agenda")
        elif kind == "cr" and item_id in minutes:
            m = minutes[item_id]
            result.update(label=m["title"], sub=" · ".join(x for x in (m["held_on"], m["project"]) if x),
                          project=m["project"], url=f"/comptes-rendus/{item_id}")
        elif kind == "cr_element" and item_id in items:
            i = items[item_id]
            result.update(label=i["text"], sub=f"{ITEM_LABELS.get(i['type'], 'Élément')} · {i['title']} · {i['held_on']}",
                          project=i["project"], url=f"/comptes-rendus/{i['minutes_id']}")
        elif kind == "note" and item_id in kb:
            k = kb[item_id]
            details = [k["module"], f"Odoo {k['odoo_version']}" if k["odoo_version"] else "", k["project"]]
            result.update(label=k["title"], sub=" · ".join(x for x in details if x) or "Base de connaissances",
                          project=k["project"], url=f"/notes/{item_id}")
        else:
            continue  # element supprime entre l'index et la lecture : ignore
        results.append(result)
    return results
