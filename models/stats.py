"""Agregats statistiques sur une periode : activite, temps suivi, repartition."""
from models import get_db
from models import space as space_model

# Nombre minimal de taches chronometrees avant d'oser corriger une estimation.
# En dessous, le facteur mesure du bruit : une seule tache oubliee sous le
# chronometre suffirait a fausser toutes les estimations suivantes.
CALIBRATION_MIN_TASKS = 4

# Profondeur de l'historique retenu. Au-dela, le facteur decrit un utilisateur
# qui n'existe plus : outils, clients et habitudes ont change.
CALIBRATION_WINDOW_DAYS = 90

# Bornes de bon sens sur le facteur. Un ratio de 12 vient toujours d'un
# chronometre laisse tourner toute la nuit, jamais d'une vraie sous-estimation.
# Elles s'appliquent d'abord tache par tache : sans ce filtre en amont, un seul
# chronometre oublie ferait sortir la moyenne des bornes et priverait le triage
# de tout calibrage. Elles restent ensuite en garde-fou sur l'agregat.
CALIBRATION_MIN_RATIO = 0.4
CALIBRATION_MAX_RATIO = 4.0


def _src(table, space=None, alias=None):
    """Source SQL d'une table restreinte a l'espace du perimetre.

    Toutes les fonctions de ce module prennent `space` (convention de
    models/space.py : None = perimetre de la requete). La valeur inseree est
    toujours un membre de space_model.SPACES, jamais une saisie.

    :param table: 'task', 'capture' ou 'note' (colonne space), ou 'session'
        (espace de sa tache)
    :param alias: nom de la source dans la requete (defaut : la table)
    """
    alias = alias or table
    espace = space_model.resolve(space)
    if espace not in space_model.SPACES:
        return f"{table} AS {alias}"
    if table == "session":
        filtre = f"task_id IN (SELECT id FROM task WHERE space = '{espace}')"
    else:
        filtre = f"space = '{espace}'"
    return f"(SELECT * FROM {table} WHERE {filtre}) AS {alias}"


def period_summary(start_date, end_date, space=None):
    """Compteurs d'une periode (dates ISO incluses).

    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    task, session, capture = _src("task", space), _src("session", space), _src("capture", space)
    row = get_db().execute(
        "SELECT"
        f"  (SELECT COUNT(*) FROM {task} WHERE date(created_at) BETWEEN ? AND ?) AS created,"
        f"  (SELECT COUNT(*) FROM {task} WHERE status = 'done'"
        "     AND date(done_at) BETWEEN ? AND ?) AS done,"
        f"  (SELECT COALESCE(SUM(estimate_min), 0) FROM {task} WHERE status = 'done'"
        "     AND date(done_at) BETWEEN ? AND ?) AS estimated_min,"
        f"  (SELECT COALESCE(SUM(duration_min), 0) FROM {session}"
        "     WHERE ended_at IS NOT NULL AND date(started_at) BETWEEN ? AND ?) AS tracked_min,"
        f"  (SELECT COUNT(*) FROM {capture} WHERE date(created_at) BETWEEN ? AND ?) AS captures,"
        f"  (SELECT COUNT(*) FROM {task} WHERE status = 'todo') AS open_tasks,"
        f"  (SELECT COUNT(*) FROM {task} WHERE status = 'todo'"
        "     AND deadline < date('now', 'localtime')) AS late_tasks",
        (start_date, end_date) * 5,
    ).fetchone()
    return dict(row)


def agent_work(start_date, end_date, space=None):
    """Travail declare par les sessions Claude sur une periode.

    :return: {'minutes', 'entries', 'tasks_created', 'tasks_done', 'tasks_worked'}
    """
    task, session, note = _src("task", space), _src("session", space), _src("note", space)
    row = get_db().execute(
        "SELECT"
        f"  (SELECT COALESCE(SUM(duration_min), 0) FROM {session} WHERE source = 'claude'"
        "     AND date(started_at) BETWEEN ? AND ?) AS minutes,"
        f"  (SELECT COUNT(*) FROM {note} WHERE source = 'claude'"
        "     AND date(created_at) BETWEEN ? AND ?) AS entries,"
        f"  (SELECT COUNT(*) FROM {task} WHERE source = 'claude'"
        "     AND date(created_at) BETWEEN ? AND ?) AS tasks_created,"
        f"  (SELECT COUNT(*) FROM {task} WHERE source = 'claude' AND status = 'done'"
        "     AND date(done_at) BETWEEN ? AND ?) AS tasks_done,"
        f"  (SELECT COUNT(DISTINCT task_id) FROM {note} WHERE source = 'claude'"
        "     AND date(created_at) BETWEEN ? AND ?) AS tasks_worked",
        (start_date, end_date) * 5,
    ).fetchone()
    return dict(row)


def done_by_day(start_date, end_date, space=None):
    """Nombre de taches terminees par jour."""
    rows = get_db().execute(
        f"SELECT date(done_at) AS day, COUNT(*) AS count FROM {_src('task', space)}"
        " WHERE status = 'done' AND date(done_at) BETWEEN ? AND ?"
        " GROUP BY day ORDER BY day",
        (start_date, end_date),
    ).fetchall()
    return [dict(row) for row in rows]


def tracked_by_project(start_date, end_date, space=None):
    """Temps suivi par projet sur la periode.

    Une sous-tache sans projet compte pour le projet de sa tache parente.
    La ligne « Sans projet » porte un project_id nul.
    """
    rows = get_db().execute(
        "SELECT p.id AS project_id,"
        "  COALESCE(p.name, 'Sans projet') AS project,"
        "  COALESCE(p.color, '#6b7280') AS color,"
        "  SUM(s.duration_min) AS minutes"
        f" FROM {_src('session', space, 's')}"
        " JOIN task t ON t.id = s.task_id"
        " LEFT JOIN task parent ON parent.id = t.parent_id"
        " LEFT JOIN project p ON p.id = COALESCE(t.project_id, parent.project_id)"
        " WHERE s.ended_at IS NOT NULL AND date(s.started_at) BETWEEN ? AND ?"
        " GROUP BY p.id ORDER BY minutes DESC",
        (start_date, end_date),
    ).fetchall()
    return [dict(row) for row in rows]


def unassigned_tracked_tasks(start_date, end_date, limit=50, space=None):
    """Taches chronometrees sur la periode sans projet, ni elles ni leur parente.

    Ce sont elles qui forment la barre « Sans projet » de tracked_by_project.

    :return: liste de {'id', 'title', 'minutes'}, les plus longues d'abord
    """
    rows = get_db().execute(
        "SELECT t.id, t.title, SUM(s.duration_min) AS minutes"
        f" FROM {_src('session', space, 's')}"
        " JOIN task t ON t.id = s.task_id"
        " LEFT JOIN task parent ON parent.id = t.parent_id"
        " WHERE s.ended_at IS NOT NULL AND date(s.started_at) BETWEEN ? AND ?"
        "   AND t.project_id IS NULL AND parent.project_id IS NULL"
        " GROUP BY t.id ORDER BY minutes DESC, t.id LIMIT ?",
        (start_date, end_date, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def done_by_priority(start_date, end_date, space=None):
    """Repartition des taches terminees par priorite."""
    rows = get_db().execute(
        f"SELECT priority, COUNT(*) AS count FROM {_src('task', space)}"
        " WHERE status = 'done' AND date(done_at) BETWEEN ? AND ?"
        " GROUP BY priority ORDER BY priority",
        (start_date, end_date),
    ).fetchall()
    return [dict(row) for row in rows]


def done_tasks(start_date, end_date, limit=40, space=None):
    """Liste des taches terminees sur la periode."""
    rows = get_db().execute(
        "SELECT t.id, t.title, t.priority, t.estimate_min, t.done_at,"
        "  p.name AS project_name,"
        "  COALESCE((SELECT SUM(duration_min) FROM session WHERE task_id = t.id), 0) AS tracked_min"
        f" FROM {_src('task', space, 't')} LEFT JOIN project p ON p.id = t.project_id"
        " WHERE t.status = 'done' AND date(t.done_at) BETWEEN ? AND ?"
        " ORDER BY t.done_at ASC LIMIT ?",
        (start_date, end_date, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def capture_breakdown(start_date, end_date, space=None):
    """Repartit les captures de la periode selon ce qu'elles ont produit."""
    row = get_db().execute(
        "SELECT COUNT(*) AS total,"
        "  SUM(CASE WHEN EXISTS (SELECT 1 FROM task WHERE task.capture_id = capture.id)"
        "      THEN 1 ELSE 0 END) AS with_tasks,"
        "  SUM(CASE WHEN status = 'waiting' THEN 1 ELSE 0 END) AS waiting,"
        "  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed"
        f" FROM {_src('capture', space)} WHERE date(created_at) BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()

    data = {key: (value or 0) for key, value in dict(row).items()}
    data["without_tasks"] = data["total"] - data["with_tasks"]
    return data


def estimation_gap(start_date, end_date, space=None):
    """Compare estime et reel sur les taches terminees ayant du temps suivi.

    :return: {'tasks': n, 'estimated_min': x, 'tracked_min': y, 'ratio': y/x}
    """
    row = get_db().execute(
        "SELECT COUNT(*) AS tasks,"
        "  COALESCE(SUM(t.estimate_min), 0) AS estimated_min,"
        "  COALESCE(SUM(s.total), 0) AS tracked_min"
        f" FROM {_src('task', space, 't')}"
        " JOIN (SELECT task_id, SUM(duration_min) AS total FROM session"
        "        WHERE ended_at IS NOT NULL GROUP BY task_id) s ON s.task_id = t.id"
        " WHERE t.status = 'done' AND date(t.done_at) BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchone()

    data = dict(row)
    data["ratio"] = (
        round(data["tracked_min"] / data["estimated_min"], 2)
        if data["estimated_min"]
        else None
    )
    return data


def _calibration_rows(window_days, space=None):
    """Retourne le cumul estime / chronometre par projet sur la fenetre.

    Seules les taches terminees ET chronometrees comptent : une tache close
    sans chronometre ne dit rien du temps qu'elle a reellement pris.
    """
    return get_db().execute(
        "SELECT t.project_id, p.name AS project,"
        "  COUNT(*) AS tasks,"
        "  SUM(t.estimate_min) AS estimated_min,"
        "  SUM(s.total) AS tracked_min"
        f" FROM {_src('task', space, 't')}"
        " JOIN (SELECT task_id, SUM(duration_min) AS total FROM session"
        "        WHERE ended_at IS NOT NULL GROUP BY task_id) s ON s.task_id = t.id"
        " LEFT JOIN project p ON p.id = t.project_id"
        " WHERE t.status = 'done' AND t.estimate_min > 0 AND s.total > 0"
        "   AND s.total >= t.estimate_min * ? AND s.total <= t.estimate_min * ?"
        "   AND date(t.done_at) >= date('now', 'localtime', ?)"
        " GROUP BY t.project_id, p.name",
        (CALIBRATION_MIN_RATIO, CALIBRATION_MAX_RATIO, f"-{int(window_days)} day"),
    ).fetchall()


def _ratio(tracked_min, estimated_min):
    """Facteur reel / estime, borne, ou None si la mesure n'est pas exploitable."""
    if not estimated_min:
        return None
    brut = tracked_min / estimated_min
    if brut < CALIBRATION_MIN_RATIO or brut > CALIBRATION_MAX_RATIO:
        return None
    return round(brut, 2)


def estimation_calibration(
    min_tasks=CALIBRATION_MIN_TASKS, window_days=CALIBRATION_WINDOW_DAYS, space=None
):
    """Mesure l'ecart systematique entre duree estimee et duree reelle.

    Un facteur de 1.4 signifie que les taches prennent 40 % de plus que prevu.
    C'est cette derive que le triage injecte dans son prompt pour cesser de
    repeter la meme erreur d'estimation a chaque nouvelle tache.

    :param min_tasks: taille minimale d'echantillon, par projet comme au global
    :param window_days: profondeur de l'historique, en jours
    :return: {'global': {...} | None, 'projects': [...], 'window_days': n}
    """
    lignes = [dict(row) for row in _calibration_rows(window_days, space)]

    total_tasks = sum(ligne["tasks"] for ligne in lignes)
    total_estimated = sum(ligne["estimated_min"] for ligne in lignes)
    total_tracked = sum(ligne["tracked_min"] for ligne in lignes)

    globale = None
    if total_tasks >= min_tasks:
        ratio = _ratio(total_tracked, total_estimated)
        if ratio is not None:
            globale = {
                "tasks": total_tasks,
                "estimated_min": total_estimated,
                "tracked_min": total_tracked,
                "ratio": ratio,
            }

    projets = []
    for ligne in lignes:
        if not ligne["project_id"] or ligne["tasks"] < min_tasks:
            continue
        ratio = _ratio(ligne["tracked_min"], ligne["estimated_min"])
        if ratio is None:
            continue
        projets.append(
            {
                "project_id": ligne["project_id"],
                "project": ligne["project"],
                "tasks": ligne["tasks"],
                "estimated_min": ligne["estimated_min"],
                "tracked_min": ligne["tracked_min"],
                "ratio": ratio,
            }
        )

    projets.sort(key=lambda item: -item["tasks"])
    return {"global": globale, "projects": projets, "window_days": window_days}


def project_activity(start_date, end_date, space=None):
    """Mouvements de chaque projet sur une periode (dates ISO incluses).

    Quatre requetes groupees, quel que soit le nombre de projets.

    :return: {project_id: {'tasks_done', 'phases_done', 'anomalies_opened',
        'anomalies_fixed', 'deliverables_received', 'register_entries'}}
    """
    db = get_db()
    activite = {}

    def ajoute(rows):
        for row in rows:
            ligne = activite.setdefault(
                row["project_id"],
                {
                    "tasks_done": 0,
                    "phases_done": 0,
                    "anomalies_opened": 0,
                    "anomalies_fixed": 0,
                    "deliverables_received": 0,
                    "register_entries": 0,
                },
            )
            for cle in row.keys():
                if cle != "project_id":
                    ligne[cle] += row[cle] or 0

    ajoute(db.execute(
        "SELECT project_id,"
        "  SUM(CASE WHEN status = 'done' AND date(done_at) BETWEEN ? AND ?"
        "      AND kind = 'task' THEN 1 ELSE 0 END) AS tasks_done,"
        "  SUM(CASE WHEN kind = 'anomaly' AND date(created_at) BETWEEN ? AND ?"
        "      THEN 1 ELSE 0 END) AS anomalies_opened,"
        "  SUM(CASE WHEN kind = 'anomaly' AND status = 'done'"
        "      AND date(done_at) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS anomalies_fixed,"
        "  SUM(CASE WHEN kind = 'deliverable' AND status = 'done'"
        "      AND date(done_at) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS deliverables_received"
        f" FROM {_src('task', space)} WHERE project_id IS NOT NULL GROUP BY project_id",
        (start_date, end_date) * 4,
    ).fetchall())
    ajoute(db.execute(
        "SELECT project_id, COUNT(*) AS phases_done FROM project_phase"
        " WHERE status = 'done' AND done_date BETWEEN ? AND ? GROUP BY project_id",
        (start_date, end_date),
    ).fetchall())
    ajoute(db.execute(
        f"SELECT project_id, COUNT(*) AS register_entries FROM {_src('note', space)}"
        " WHERE project_id IS NOT NULL AND kind IN ('decision', 'scope', 'dependency')"
        "   AND date(created_at) BETWEEN ? AND ? GROUP BY project_id",
        (start_date, end_date),
    ).fetchall())

    # Un projet sans aucun mouvement n'a rien a dire sur la periode.
    return {
        project_id: ligne
        for project_id, ligne in activite.items()
        if any(ligne.values())
    }
