"""CRUD des projets et indicateurs par projet."""
import re

from models import get_db
from models import space as space_model

VALID_STATUS = ("active", "paused", "done")

# Espace de travail d'un projet ; ses taches le suivent (models/task.py).
VALID_SPACE = ("pro", "perso")
DEFAULT_SPACE = "pro"

# Meteo du projet, de la plus sombre a la plus claire.
VALID_WEATHER = ("stormy", "rainy", "cloudy", "fair", "sunny")

# La couleur est injectee dans un attribut style : seul l'hexadecimal est admis,
# sinon une valeur du type 'red;background:url(...)' declencherait une requete
# vers un serveur tiers a l'affichage de la page.
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#6366f1"

# Teintes attribuees aux projets crees automatiquement par le triage. Sans
# elles, tous les projets deduits par NOMAIA sortiraient de la meme couleur et
# la pastille de la liste ne distinguerait plus rien.
PALETTE = (
    "#6366f1",
    "#ef4444",
    "#f59e0b",
    "#10b981",
    "#06b6d4",
    "#8b5cf6",
    "#ec4899",
    "#84cc16",
)


def suggest_color(name):
    """Choisit une couleur stable pour un nom de projet.

    Deterministe : le meme nom rend toujours la meme teinte, y compris apres
    une suppression suivie d'une recreation.
    """
    name = (name or "").strip().lower()
    if not name:
        return DEFAULT_COLOR
    return PALETTE[sum(name.encode("utf-8")) % len(PALETTE)]


class ProjectError(ValueError):
    """Donnee de projet invalide : remontee en HTTP 400 par le controleur."""


def _clean_name(name):
    """Valide un nom de projet."""
    name = (name or "").strip()
    if not name:
        raise ProjectError("Le nom du projet est obligatoire.")
    if len(name) > 120:
        raise ProjectError("Le nom ne peut pas depasser 120 caracteres.")
    return name


def _clean_color(color):
    """Valide une couleur hexadecimale. Retourne la couleur par defaut si vide."""
    color = (color or "").strip() or DEFAULT_COLOR
    if not COLOR_PATTERN.match(color):
        raise ProjectError("La couleur doit etre au format hexadecimal, par exemple #6366f1.")
    return color.lower()


def _clean_manager(value):
    """Valide le chef de projet : un interlocuteur existant, ou None pour retirer."""
    if value in (None, "", "none"):
        return None
    try:
        person_id = int(value)
    except (TypeError, ValueError):
        raise ProjectError("Chef de projet invalide.")
    if get_db().execute("SELECT 1 FROM person WHERE id = ?", (person_id,)).fetchone() is None:
        raise ProjectError("Chef de projet introuvable dans les contacts.")
    return person_id


def list_by_manager(person_id):
    """Projets non termines suivis par un chef de projet, par nom."""
    rows = get_db().execute(
        "SELECT * FROM project WHERE manager_id = ? AND status != 'done' ORDER BY name ASC",
        (person_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def clean_space(space):
    """Valide un espace de travail ; leve ProjectError s'il est inconnu."""
    valeur = (space or "").strip().lower()
    if valeur not in VALID_SPACE:
        raise ProjectError("Espace invalide : pro ou perso.")
    return valeur


def list_projects(status="active", space=None):
    """Liste les projets, filtres par statut ('active' par defaut, None pour tout).

    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    sql = "SELECT * FROM project WHERE 1 = 1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    filtre, valeurs = space_model.clause("space", space)
    sql += filtre
    params.extend(valeurs)
    sql += " ORDER BY name ASC"
    return [dict(row) for row in get_db().execute(sql, params).fetchall()]


def list_with_stats(status=None, space=None):
    """Liste les projets avec leurs compteurs de taches et leur temps suivi.

    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    sql = [
        "SELECT p.*,",
        "  COALESCE(t.open_count, 0)    AS open_count,",
        "  COALESCE(t.done_count, 0)    AS done_count,",
        "  COALESCE(t.open_estimate, 0) AS open_estimate_min,",
        "  COALESCE(s.tracked, 0)       AS tracked_min",
        "FROM project p",
        "LEFT JOIN (",
        "  SELECT project_id,",
        "    SUM(CASE WHEN status = 'todo' THEN 1 ELSE 0 END) AS open_count,",
        "    SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count,",
        "    SUM(CASE WHEN status = 'todo' THEN estimate_min ELSE 0 END) AS open_estimate",
        "  FROM task GROUP BY project_id",
        ") t ON t.project_id = p.id",
        "LEFT JOIN (",
        "  SELECT task.project_id, SUM(session.duration_min) AS tracked",
        "  FROM session JOIN task ON task.id = session.task_id",
        "  WHERE session.ended_at IS NOT NULL GROUP BY task.project_id",
        ") s ON s.project_id = p.id",
    ]
    sql.append("WHERE 1 = 1")
    params = []

    if status:
        sql.append("AND p.status = ?")
        params.append(status)
    filtre, valeurs = space_model.clause("p.space", space)
    if filtre:
        sql.append(filtre)
        params.extend(valeurs)

    sql.append("ORDER BY p.status ASC, p.name ASC")
    rows = get_db().execute(" ".join(sql), params).fetchall()
    return [dict(row) for row in rows]


# Taches comptees dans l'avancement d'un projet : une tache detaillee en
# etapes est mesuree par ses etapes, la compter aussi doublerait son poids.
_LEAF_TASKS = "task WHERE id NOT IN (SELECT parent_id FROM task WHERE parent_id IS NOT NULL)"


def board_rows(today, week_start, week_end, project_id=None, space=None):
    """Chiffres de pilotage de chaque projet, en une seule requete.

    L'avancement est pondere par les estimations : une tache de 4 h terminee
    pese plus qu'une tache de 15 min. Une tache ouverte compte pour la part
    declaree de son avancement (progress_pct).

    :param today: date ISO du jour (retards)
    :param week_start: debut ISO de la semaine en cours
    :param week_end: fin ISO de la semaine en cours
    :param project_id: restreint a un projet
    :param space: convention de models/space.py (None = perimetre de la requete)
    :return: liste de dictionnaires, un par projet
    """
    sql = [
        "SELECT p.*,",
        "  COALESCE(t.open_count, 0) AS open_count,",
        "  COALESCE(t.done_count, 0) AS done_count,",
        "  COALESCE(t.late_count, 0) AS late_count,",
        "  COALESCE(t.est_total, 0) AS est_total,",
        "  COALESCE(t.est_done, 0) AS est_done,",
        "  COALESCE(t.est_left, 0) AS remaining_min,",
        "  COALESCE(t.done_week, 0) AS done_week,",
        "  t.last_task_at,",
        "  COALESCE(s.tracked_total, 0) AS tracked_min,",
        "  COALESCE(s.tracked_week, 0) AS tracked_week,",
        "  s.last_session_at,",
        "  n.last_note_at",
        "FROM project p",
        "LEFT JOIN (",
        "  SELECT project_id,",
        "    SUM(status = 'todo') AS open_count,",
        "    SUM(status = 'done') AS done_count,",
        "    SUM(status = 'todo' AND deadline < ?) AS late_count,",
        "    SUM(estimate_min) AS est_total,",
        "    SUM(CASE WHEN status = 'done' THEN estimate_min",
        "        ELSE estimate_min * progress_pct / 100.0 END) AS est_done,",
        "    SUM(CASE WHEN status = 'todo' THEN estimate_min * (100 - progress_pct) / 100.0",
        "        ELSE 0 END) AS est_left,",
        "    SUM(status = 'done' AND date(done_at) BETWEEN ? AND ?) AS done_week,",
        "    MAX(MAX(created_at), COALESCE(MAX(done_at), '')) AS last_task_at",
        f"  FROM {_LEAF_TASKS} GROUP BY project_id",
        ") t ON t.project_id = p.id",
        "LEFT JOIN (",
        "  SELECT task.project_id,",
        "    SUM(session.duration_min) AS tracked_total,",
        "    SUM(CASE WHEN date(session.started_at) BETWEEN ? AND ?",
        "        THEN session.duration_min ELSE 0 END) AS tracked_week,",
        "    MAX(session.ended_at) AS last_session_at",
        "  FROM session JOIN task ON task.id = session.task_id",
        "  WHERE session.ended_at IS NOT NULL GROUP BY task.project_id",
        ") s ON s.project_id = p.id",
        "LEFT JOIN (",
        "  SELECT project_id, MAX(created_at) AS last_note_at FROM note",
        "  WHERE project_id IS NOT NULL GROUP BY project_id",
        ") n ON n.project_id = p.id",
    ]
    params = [today, week_start, week_end, week_start, week_end]
    conditions = []
    if project_id is not None:
        conditions.append("p.id = ?")
        params.append(project_id)
    filtre, valeurs = space_model.clause("p.space", space)
    if filtre:
        conditions.append(filtre[len(" AND "):])
        params.extend(valeurs)
    if conditions:
        sql.append("WHERE " + " AND ".join(conditions))
    sql.append("ORDER BY CASE p.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, p.name")
    rows = get_db().execute(" ".join(sql), params).fetchall()
    return [dict(row) for row in rows]


def weekly_activity(project_id, since):
    """Taches terminees et temps suivi par semaine depuis une date ISO.

    :return: liste de {'week': 'AAAA-MM-JJ' (lundi), 'done', 'minutes'}
    """
    lundi = "date({col}, '-' || ((CAST(strftime('%w', {col}) AS INTEGER) + 6) % 7) || ' days')"
    terminees = get_db().execute(
        f"SELECT {lundi.format(col='done_at')} AS week, COUNT(*) AS done FROM task"
        " WHERE project_id = ? AND status = 'done' AND date(done_at) >= ? GROUP BY week",
        (project_id, since),
    ).fetchall()
    temps = get_db().execute(
        f"SELECT {lundi.format(col='s.started_at')} AS week, SUM(s.duration_min) AS minutes"
        " FROM session s JOIN task t ON t.id = s.task_id"
        " WHERE t.project_id = ? AND s.ended_at IS NOT NULL AND date(s.started_at) >= ?"
        " GROUP BY week",
        (project_id, since),
    ).fetchall()

    semaines = {}
    for row in terminees:
        semaines.setdefault(row["week"], {"week": row["week"], "done": 0, "minutes": 0})["done"] = row["done"]
    for row in temps:
        semaines.setdefault(row["week"], {"week": row["week"], "done": 0, "minutes": 0})["minutes"] = row["minutes"]
    return [semaines[cle] for cle in sorted(semaines)]


def get_project(project_id):
    """Retourne un projet, ou None."""
    row = get_db().execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def find_by_odoo_id(odoo_id):
    """Retourne le projet NOMAIA alimente par ce projet Odoo, ou None."""
    row = get_db().execute(
        "SELECT p.* FROM project p JOIN project_odoo_link l ON l.project_id = p.id"
        " WHERE l.odoo_id = ?",
        (odoo_id,),
    ).fetchone()
    return dict(row) if row else None


def has_odoo_link(project_id):
    """Indique si le projet est deja alimente par au moins un projet Odoo."""
    return get_db().execute(
        "SELECT 1 FROM project_odoo_link WHERE project_id = ? LIMIT 1", (project_id,)
    ).fetchone() is not None


def link_odoo(project_id, odoo_id):
    """Rattache un projet Odoo a un projet NOMAIA (remplace un lien anterieur)."""
    db = get_db()
    db.execute(
        "INSERT INTO project_odoo_link (odoo_id, project_id) VALUES (?, ?)"
        " ON CONFLICT(odoo_id) DO UPDATE SET project_id = excluded.project_id",
        (odoo_id, project_id),
    )
    db.commit()


# Informations du projet reprises du projet absorbe quand le projet garde ne
# les porte pas.
MERGE_FILLED_FIELDS = ("deadline", "go_live", "manager_id", "weather", "weather_note")


def merge_projects(keep_id, merge_id):
    """Absorbe un projet en double dans un autre, puis le supprime.

    Le projet garde conserve son nom, ses etapes et ses informations ; celles
    qu'il n'a pas sont reprises du doublon. Taches, journal, contacts,
    evenements et liens Odoo du doublon passent au projet garde. Les etapes du
    doublon sont supprimees : ses taches arrivent sans etape, a ranger.

    :param keep_id: projet conserve
    :param merge_id: projet absorbe puis supprime
    :return: projet garde a jour
    :raises ProjectError: projet introuvable ou identique
    """
    if keep_id == merge_id:
        raise ProjectError("Un projet ne peut pas etre fusionne avec lui-meme.")
    garde = get_project(keep_id)
    doublon = get_project(merge_id)
    if garde is None or doublon is None:
        raise ProjectError("Projet introuvable.")

    db = get_db()
    try:
        # Taches d'abord detachees des etapes du doublon, qui vont disparaitre.
        db.execute(
            "UPDATE task SET phase_id = NULL, project_id = ?, space = ? WHERE project_id = ?",
            (keep_id, garde["space"], merge_id),
        )
        for table in ("note", "person", "event"):
            db.execute(
                f"UPDATE {table} SET project_id = ? WHERE project_id = ?", (keep_id, merge_id)
            )
        db.execute(
            "UPDATE project_odoo_link SET project_id = ? WHERE project_id = ?", (keep_id, merge_id)
        )
        # Les interlocuteurs du doublon suivent le projet garde (sinon la
        # suppression du doublon les detacherait en cascade).
        db.execute(
            "INSERT OR IGNORE INTO person_project (person_id, project_id)"
            " SELECT person_id, ? FROM person_project WHERE project_id = ?",
            (keep_id, merge_id),
        )
        manquants = {
            champ: doublon[champ]
            for champ in MERGE_FILLED_FIELDS
            if not garde.get(champ) and doublon.get(champ)
        }
        if manquants:
            db.execute(
                f"UPDATE project SET {', '.join(f'{champ} = ?' for champ in manquants)} WHERE id = ?",
                [*manquants.values(), keep_id],
            )
        db.execute("DELETE FROM project_phase WHERE project_id = ?", (merge_id,))
        db.execute("DELETE FROM project WHERE id = ?", (merge_id,))
        db.execute(
            "INSERT INTO note (project_id, content, kind) VALUES (?, ?, 'note')",
            (keep_id, f"Projet « {doublon['name']} » fusionne ici."),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_project(keep_id)


def find_by_name(name, space=None):
    """Retourne le projet portant exactement ce nom, casse ignoree, ou None.

    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    name = (name or "").strip()
    if not name:
        return None

    sql = "SELECT * FROM project WHERE lower(name) = lower(?)"
    params = [name]
    filtre, valeurs = space_model.clause("space", space)
    sql += filtre
    params.extend(valeurs)
    row = get_db().execute(sql + " LIMIT 1", params).fetchone()
    return dict(row) if row else None


def _like_pattern(value):
    """Echappe les jokers SQL d'un texte destine a une clause LIKE."""
    for caractere in ("!", "%", "_"):
        value = value.replace(caractere, "!" + caractere)
    return "%" + value + "%"


def attach_orphan_tasks(project_id):
    """Rattache a un projet les taches ouvertes sans projet qui le citent.

    Un projet est souvent cree apres coup, une fois que les taches existent
    deja : sans ce rattrapage, les taches deduites avant sa creation restent
    orphelines pour toujours et n'apparaissent dans aucun bilan.

    La recherche porte sur le titre et la description, et ne touche jamais une
    tache deja rattachee ni une tache terminee.

    :return: nombre de taches rattachees
    """
    projet = get_project(project_id)
    if projet is None:
        return 0

    motif = _like_pattern(projet["name"])
    db = get_db()
    cursor = db.execute(
        "UPDATE task SET project_id = ?, space = ?"
        " WHERE project_id IS NULL AND status = 'todo'"
        "   AND (title LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!')",
        (project_id, projet["space"], motif, motif),
    )
    db.commit()
    return cursor.rowcount


def count_orphan_matches(project_id):
    """Compte les taches qu'un rattachement prendrait, sans rien modifier."""
    projet = get_project(project_id)
    if projet is None:
        return 0

    motif = _like_pattern(projet["name"])
    row = get_db().execute(
        "SELECT COUNT(*) AS total FROM task"
        " WHERE project_id IS NULL AND status = 'todo'"
        "   AND (title LIKE ? ESCAPE '!' OR description LIKE ? ESCAPE '!')",
        (motif, motif),
    ).fetchone()
    return row["total"]


def create_project(name, color="#6366f1", deadline=None, space=None):
    """Cree un projet et retourne sa representation.

    :param space: espace de travail du projet ; par defaut celui du perimetre
        (models/space.for_new)
    """
    db = get_db()
    cursor = db.execute(
        "INSERT INTO project (name, color, deadline, space) VALUES (?, ?, ?, ?)",
        (_clean_name(name), _clean_color(color), deadline or None,
         clean_space(space or space_model.for_new())),
    )
    db.commit()
    return get_project(cursor.lastrowid)


def update_project(project_id, data):
    """Met a jour un projet. Retourne None s'il n'existe pas."""
    if get_project(project_id) is None:
        return None

    values = {}
    if "name" in data:
        values["name"] = _clean_name(data["name"])
    if "color" in data:
        values["color"] = _clean_color(data["color"])
    if "deadline" in data:
        values["deadline"] = (data["deadline"] or "").strip() or None
    if "status" in data:
        if data["status"] not in VALID_STATUS:
            raise ProjectError("Statut de projet invalide.")
        values["status"] = data["status"]
    if "weather" in data:
        weather = data["weather"] or None
        if weather is not None and weather not in VALID_WEATHER:
            raise ProjectError("Meteo de projet invalide.")
        values["weather"] = weather
    if "weather_note" in data:
        note = (data["weather_note"] or "").strip()
        if len(note) > 1000:
            raise ProjectError("Le commentaire de meteo ne peut pas depasser 1000 caracteres.")
        values["weather_note"] = note or None
    if "go_live" in data:
        values["go_live"] = (data["go_live"] or "").strip() or None
    if "manager_id" in data:
        values["manager_id"] = _clean_manager(data["manager_id"])
    # Contexte Odoo : version (17.0, 18.0...) et client, libres et facultatifs.
    for field, size in (("odoo_version", 20), ("client", 120)):
        if field in data:
            values[field] = (data[field] or "").strip()[:size] or None
    if "space" in data:
        values["space"] = clean_space(data["space"])

    if not values:
        return get_project(project_id)

    assignments = ", ".join(f"{column} = ?" for column in values)
    db = get_db()
    db.execute(
        f"UPDATE project SET {assignments} WHERE id = ?",
        list(values.values()) + [project_id],
    )
    if "space" in values:
        # Les taches suivent l'espace de leur projet, sous-taches comprises.
        db.execute("UPDATE task SET space = ? WHERE project_id = ?", (values["space"], project_id))
        space_model.cascade_project(db, project_id, values["space"])
    db.commit()
    return get_project(project_id)


def delete_project(project_id):
    """Supprime un projet. Ses taches sont conservees, simplement detachees.

    Le journal des taches reste avec elles ; seuls les commentaires propres au
    projet (sans tache) partent avec lui. Fait ici et non par la cle etrangere :
    une base migree depuis une version anterieure peut porter un ON DELETE
    CASCADE que SQLite ne permet pas de modifier.
    """
    db = get_db()
    db.execute(
        "UPDATE note SET project_id = NULL WHERE project_id = ? AND task_id IS NOT NULL",
        (project_id,),
    )
    db.execute("DELETE FROM note WHERE project_id = ? AND task_id IS NULL", (project_id,))
    # Les etapes partent avec le projet ; ses taches, conservees, les perdent.
    db.execute("UPDATE task SET phase_id = NULL WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM project_phase WHERE project_id = ?", (project_id,))
    cursor = db.execute("DELETE FROM project WHERE id = ?", (project_id,))
    db.commit()
    return cursor.rowcount > 0
