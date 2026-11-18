"""Base de connaissances : notes techniques en Markdown (table kb_note).

Classees par projet, version d'Odoo, module, categorie et etiquettes. Une
anomalie resolue avec une solution produit sa note « Solution », tenue a
jour si la solution change ensuite (ensure_solution_note, appelee par
models.task a chaque passage a « terminee »).
"""
import re
from datetime import datetime

from models import get_db
from models import space as space_model
from services import secrets_guard

CATEGORIES = ("python", "xml", "js_owl", "sql", "config", "bug_solution", "deployment", "functional", "other")
CATEGORY_LABELS = {
    "python": "Python", "xml": "XML / vues", "js_owl": "JS / Owl", "sql": "SQL", "config": "Configuration",
    "bug_solution": "Solution d'anomalie", "deployment": "Déploiement", "functional": "Fonctionnel", "other": "Autre",
}
MAX_CONTENT = 100_000

# Modele propose pour noter un deploiement (la fiche « Deploiements » de DevTrack, en plus leger).
DEPLOYMENT_TEMPLATE = """## Contexte
Environnement : test / préproduction / production
Version d'Odoo :
Modules :

## Changements
-

## Vérifications
- [ ] Mise à jour des modules sans erreur
- [ ] Tests fonctionnels principaux
- [ ] Sauvegarde de la base avant mise à jour

## Résultat et incidents
"""


class KnowledgeError(ValueError):
    """Saisie de note invalide."""


def _now():
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _tags(value):
    """Etiquettes normalisees : minuscules, sans doublon, separees par des virgules."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    seen = []
    for item in items:
        tag = re.sub(r"\s+", " ", str(item)).strip().lower()[:40]
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen[:20])


def _clean(data, partial):
    values = {}
    if "title" in data or not partial:
        title = str(data.get("title") or "").strip()
        if not title:
            raise KnowledgeError("Le titre est obligatoire.")
        values["title"] = title[:200]
    if "content" in data:
        content = str(data.get("content") or "")
        if len(content) > MAX_CONTENT:
            raise KnowledgeError("Note trop longue.")
        values["content"] = content
    if "category" in data:
        if data["category"] not in CATEGORIES:
            raise KnowledgeError("Catégorie inconnue.")
        values["category"] = data["category"]
    for field, size in (("odoo_version", 20), ("module", 120)):
        if field in data:
            values[field] = str(data.get(field) or "").strip()[:size]
    if "tags" in data:
        values["tags"] = _tags(data["tags"])
    if "pinned" in data:
        values["pinned"] = 1 if data["pinned"] else 0
    if "project_id" in data:
        project_id = data.get("project_id") or None
        if project_id is not None and get_db().execute(
            "SELECT 1 FROM project WHERE id = ?", (int(project_id),)
        ).fetchone() is None:
            raise KnowledgeError("Projet introuvable.")
        values["project_id"] = int(project_id) if project_id else None
    if data.get("space"):
        try:
            values["space"] = space_model.clean(data["space"], shared=True)
        except space_model.SpaceError as error:
            raise KnowledgeError(str(error)) from None
    if values.get("project_id"):
        # Une note rattachee a un projet en prend l'espace.
        values["space"] = space_model.for_record(project_id=values["project_id"])
    try:
        secrets_guard.check({"Titre": values.get("title"), "Contenu": values.get("content")})
    except secrets_guard.SecretDetected as error:
        raise KnowledgeError(str(error)) from None
    return values


def _note_dict(row):
    data = dict(row)
    data["tags"] = [tag for tag in (data.get("tags") or "").split(",") if tag]
    data["category_label"] = CATEGORY_LABELS.get(data["category"], data["category"])
    return data


def get(note_id):
    row = get_db().execute(
        "SELECT k.*, p.name AS project_name, p.color AS project_color, t.title AS source_task_title"
        " FROM kb_note k LEFT JOIN project p ON p.id = k.project_id LEFT JOIN task t ON t.id = k.source_task_id"
        " WHERE k.id = ?",
        (note_id,),
    ).fetchone()
    return _note_dict(row) if row else None


def list_notes(project_id=None, category=None, tag=None, odoo_version=None, limit=300, space=None):
    """Notes epinglees d'abord, puis les plus recemment modifiees (contenu non renvoye).

    :param space: convention de models/space.py (notes communes comprises)
    """
    sql = (
        "SELECT k.id, k.title, k.category, k.project_id, k.odoo_version, k.module, k.tags, k.pinned,"
        " k.space,"
        " k.source_task_id, k.updated_at, substr(k.content, 1, 220) AS excerpt,"
        " p.name AS project_name, p.color AS project_color"
        " FROM kb_note k LEFT JOIN project p ON p.id = k.project_id WHERE 1 = 1"
    )
    filtre, params = space_model.clause("k.space", space, shared=True)
    sql += filtre
    if project_id:
        sql += " AND k.project_id = ?"
        params.append(project_id)
    if category:
        sql += " AND k.category = ?"
        params.append(category)
    if odoo_version:
        sql += " AND k.odoo_version = ?"
        params.append(odoo_version)
    if tag:
        sql += " AND (',' || k.tags || ',') LIKE ?"
        params.append(f"%,{tag.strip().lower()},%")
    sql += " ORDER BY k.pinned DESC, k.updated_at DESC, k.id DESC LIMIT ?"
    params.append(limit)
    return [_note_dict(row) for row in get_db().execute(sql, params).fetchall()]


def facets(space=None):
    """Valeurs disponibles pour les filtres : categories utilisees, versions, etiquettes."""
    db = get_db()
    filtre, valeurs = space_model.clause("space", space, shared=True)
    categories = {row[0]: row[1] for row in db.execute(
        "SELECT category, COUNT(*) FROM kb_note WHERE 1 = 1" + filtre + " GROUP BY category", valeurs)}
    versions = [row[0] for row in db.execute(
        "SELECT DISTINCT odoo_version FROM kb_note WHERE odoo_version <> ''" + filtre
        + " ORDER BY odoo_version DESC", valeurs)]
    counts = {}
    for (tags,) in db.execute("SELECT tags FROM kb_note WHERE tags <> ''" + filtre, valeurs):
        for tag in tags.split(","):
            counts[tag] = counts.get(tag, 0) + 1
    return {
        "categories": [{"key": key, "label": CATEGORY_LABELS[key], "count": categories.get(key, 0)}
                       for key in CATEGORIES],
        "versions": versions,
        "tags": sorted(counts, key=lambda tag: (-counts[tag], tag))[:40],
    }


def create(data):
    values = _clean(data, partial=False)
    values.setdefault("space", space_model.for_new(shared=True))
    db = get_db()
    cursor = db.execute(
        f"INSERT INTO kb_note ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )
    db.commit()
    return get(cursor.lastrowid)


def update(note_id, data):
    if get(note_id) is None:
        return None
    values = _clean(data, partial=True)
    if values:
        values["updated_at"] = _now()
        db = get_db()
        db.execute(f"UPDATE kb_note SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
                   [*values.values(), note_id])
        db.commit()
    return get(note_id)


def delete(note_id):
    db = get_db()
    cursor = db.execute("DELETE FROM kb_note WHERE id = ?", (note_id,))
    db.commit()
    return cursor.rowcount > 0


def ensure_solution_note(task_id):
    """Cree ou met a jour la note « Solution » d'une anomalie resolue ; renvoie son id ou None.

    Sans effet si la tache n'est pas une anomalie terminee avec une solution.
    Appelee sans commit propre : le commit de l'appelant l'emporte.
    """
    db = get_db()
    task = db.execute(
        "SELECT t.*, p.name AS project_name FROM task t LEFT JOIN project p ON p.id = t.project_id WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    if task is None or task["kind"] != "anomaly" or task["status"] != "done" or not (task["solution"] or "").strip():
        return None
    parts = []
    if (task["description"] or "").strip():
        parts.append("## Symptôme\n" + task["description"].strip())
    if (task["cause"] or "").strip():
        parts.append("## Cause\n" + task["cause"].strip())
    parts.append("## Solution\n" + task["solution"].strip())
    content = "\n\n".join(parts)
    title = f"Solution : {task['title']}"[:200]
    existing = db.execute("SELECT id FROM kb_note WHERE source_task_id = ?", (task_id,)).fetchone()
    if existing:
        db.execute("UPDATE kb_note SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                   (title, content, _now(), existing["id"]))
        return existing["id"]
    cursor = db.execute(
        "INSERT INTO kb_note (title, content, category, project_id, module, tags, source_task_id,"
        " space) VALUES (?, ?, 'bug_solution', ?, ?, 'anomalie', ?, ?)",
        (title, content, task["project_id"], task["module"] or "", task_id, task["space"]),
    )
    return cursor.lastrowid
