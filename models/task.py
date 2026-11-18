"""CRUD et validation des taches."""
from datetime import date, datetime

from models import get_db, knowledge
from models import space as space_model
from services import secrets_guard

VALID_STATUS = ("todo", "done")
VALID_ENERGY = ("low", "med", "high")

# Camp qui doit agir : nous, le client, ou les deux. Vide vaut 'us'.
VALID_SIDE = ("us", "client", "both")

# Nature d'une tache : tache ordinaire, anomalie remontee par le client, ou
# livrable attendu du client (fichier de donnees, PV a signer).
VALID_KIND = ("task", "anomaly", "deliverable")

# Gravite d'une anomalie, de la plus grave a la plus legere.
VALID_SEVERITY = ("critical", "major", "minor")
DEFAULT_SEVERITY = "major"

# Espace de travail : une tache rattachee a un projet suit l'espace du projet,
# une sous-tache sans projet celui de sa parente (voir _apply_space).
VALID_SPACE = ("pro", "perso")

# Champs modifiables via l'API (liste blanche : protege des injections de colonnes).
EDITABLE_FIELDS = (
    "title",
    "description",
    "priority",
    "estimate_min",
    "deadline",
    "energy",
    "project_id",
    "parent_id",
    "position",
    "capture_id",
    "person_id",
    "needs_review",
    "progress_pct",
    "phase_id",
    "blocked",
    "side",
    "kind",
    "severity",
    "module",
    "cause",
    "solution",
    "space",
)


class ValidationError(ValueError):
    """Donnee de tache invalide : remontee en HTTP 400 par le controleur."""


def _parse_date(value, field):
    """Valide une date ISO 'AAAA-MM-JJ'. Retourne la chaine normalisee ou None."""
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValidationError(f"Le champ {field} doit etre une date au format AAAA-MM-JJ.")


def _parse_int(value, field, minimum=None, maximum=None, default=None):
    """Valide un entier optionnel avec bornes basse et haute."""
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Le champ {field} doit etre un nombre entier.")
    if minimum is not None and number < minimum:
        raise ValidationError(f"Le champ {field} doit etre superieur ou egal a {minimum}.")
    if maximum is not None and number > maximum:
        raise ValidationError(f"Le champ {field} doit etre inferieur ou egal a {maximum}.")
    return number


def _clean(data, partial=False):
    """Normalise et valide les donnees d'une tache.

    :param data: donnees brutes issues du client
    :param partial: si True, seuls les champs presents sont traites (PATCH)
    :return: dictionnaire colonne -> valeur pret pour l'ecriture SQL
    """
    cleaned = {}

    if "title" in data or not partial:
        title = (data.get("title") or "").strip()
        if not title:
            raise ValidationError("Le titre est obligatoire.")
        if len(title) > 300:
            raise ValidationError("Le titre ne peut pas depasser 300 caracteres.")
        cleaned["title"] = title

    if "description" in data:
        description = (data.get("description") or "").strip()
        cleaned["description"] = description or None

    if "priority" in data or not partial:
        cleaned["priority"] = _parse_int(
            data.get("priority"), "priorite", minimum=1, maximum=4, default=3
        )

    if "estimate_min" in data or not partial:
        cleaned["estimate_min"] = _parse_int(
            data.get("estimate_min"), "duree", minimum=5, maximum=1440, default=30
        )

    if "deadline" in data or not partial:
        cleaned["deadline"] = _parse_date(data.get("deadline"), "echeance")

    if "energy" in data or not partial:
        energy = data.get("energy") or "med"
        if energy not in VALID_ENERGY:
            raise ValidationError("Niveau d'energie invalide.")
        cleaned["energy"] = energy

    if "project_id" in data:
        cleaned["project_id"] = _parse_int(data.get("project_id"), "projet", minimum=1)

    if "parent_id" in data:
        cleaned["parent_id"] = _parse_int(data.get("parent_id"), "tache parente", minimum=1)

    if "position" in data:
        cleaned["position"] = _parse_int(data.get("position"), "position", default=0)

    if "capture_id" in data:
        cleaned["capture_id"] = _parse_int(data.get("capture_id"), "capture", minimum=1)

    if "person_id" in data:
        # A qui la tache est due. Une promesse a tenir est une tache avec un
        # destinataire, pas une entite parallele.
        cleaned["person_id"] = _parse_int(data.get("person_id"), "interlocuteur", minimum=1)

    if "needs_review" in data:
        cleaned["needs_review"] = 1 if data.get("needs_review") else 0

    if "progress_pct" in data:
        cleaned["progress_pct"] = _parse_int(
            data.get("progress_pct"), "avancement", minimum=0, maximum=100, default=0
        )

    if "phase_id" in data:
        cleaned["phase_id"] = _parse_int(data.get("phase_id"), "etape du projet", minimum=1)

    if "blocked" in data:
        cleaned["blocked"] = 1 if data.get("blocked") else 0

    if "side" in data:
        side = data.get("side") or None
        if side is not None and side not in VALID_SIDE:
            raise ValidationError("Camp invalide : us, client ou both.")
        cleaned["side"] = side

    if "kind" in data:
        kind = data.get("kind") or "task"
        if kind not in VALID_KIND:
            raise ValidationError("Nature invalide : task, anomaly ou deliverable.")
        cleaned["kind"] = kind

    if "severity" in data:
        severity = data.get("severity") or None
        if severity is not None and severity not in VALID_SEVERITY:
            raise ValidationError("Gravite invalide : critical, major ou minor.")
        cleaned["severity"] = severity

    if "module" in data:
        module = (data.get("module") or "").strip()
        if len(module) > 80:
            raise ValidationError("Le module ne peut pas depasser 80 caracteres.")
        cleaned["module"] = module or None

    # Anomalie : cause trouvee et solution appliquee. Jamais de secret en clair.
    for field, label in (("cause", "La cause"), ("solution", "La solution")):
        if field in data:
            text = (data.get(field) or "").strip()
            if len(text) > 20000:
                raise ValidationError(f"{label} est trop longue.")
            secret = secrets_guard.find_secret(text)
            if secret:
                raise ValidationError(
                    f"{label} semble contenir un secret en clair ({secret}) : remplace-le par ***"
                    " et note plutot ou il est range."
                )
            cleaned[field] = text or None

    if "space" in data:
        space = (data.get("space") or "").strip().lower()
        if space not in VALID_SPACE:
            raise ValidationError("Espace invalide : pro ou perso.")
        cleaned["space"] = space

    return cleaned


def _apply_space(values, current=None):
    """Aligne l'espace de la tache sur celui de son projet, ou de sa tache parente.

    Une tache sans projet ni parente garde l'espace fourni (a la creation,
    celui du perimetre : models/space.for_new). Un espace explicite contraire
    a celui du projet est
    refuse plutot qu'ignore : l'appelant doit changer l'espace du projet.

    :param values: colonnes nettoyees, completees en place
    :param current: tache existante (mise a jour), None a la creation
    :raises ValidationError: espace demande different de celui du projet
    """
    current = current or {}
    project_id = values.get("project_id", current.get("project_id"))
    parent_id = values.get("parent_id", current.get("parent_id"))
    touche = "space" in values or "project_id" in values or "parent_id" in values
    if not touche:
        return

    db = get_db()
    if project_id:
        row = db.execute("SELECT space FROM project WHERE id = ?", (project_id,)).fetchone()
        origine = row["space"] if row else None
        libelle = "son projet"
    elif parent_id:
        row = db.execute("SELECT space FROM task WHERE id = ?", (parent_id,)).fetchone()
        origine = row["space"] if row else None
        libelle = "sa tache parente"
    else:
        return

    if origine is None:
        return
    if "space" in values and values["space"] != origine:
        raise ValidationError(
            f"La tache suit l'espace de {libelle} ({origine}) : change plutot l'espace du projet."
        )
    values["space"] = origine


def _check_kind(values, current=None):
    """Garde gravite, module et camp coherents avec la nature de la tache.

    Une anomalie a toujours une gravite (majeure a defaut) ; une tache qui
    n'est pas une anomalie n'en a pas. Un livrable nouveau est attendu du
    client, sauf camp fourni.

    :param values: valeurs nettoyees, modifiees sur place
    :param current: tache avant modification (None a la creation)
    """
    current = current or {}
    kind = values.get("kind", current.get("kind") or "task")
    if kind == "anomaly":
        if not values.get("severity", current.get("severity")):
            values["severity"] = DEFAULT_SEVERITY
    else:
        if values.get("severity") or current.get("severity"):
            values["severity"] = None
        if values.get("module") or current.get("module"):
            values["module"] = None
    if kind == "deliverable" and kind != current.get("kind") and not values.get("side", current.get("side")):
        values["side"] = "client"


def _check_phase(values, current=None):
    """Garde l'etape coherente avec le projet de la tache.

    Une etape appartient a un projet : la rattacher a une tache d'un autre
    projet la ferait compter ailleurs. Une tache qui change de projet perd son
    etape, sauf si une etape du nouveau projet est fournie en meme temps.

    :param values: valeurs nettoyees, modifiees sur place
    :param current: tache avant modification (None a la creation)
    """
    project_id = values.get("project_id", (current or {}).get("project_id"))

    if values.get("phase_id"):
        row = get_db().execute(
            "SELECT project_id FROM project_phase WHERE id = ?", (values["phase_id"],)
        ).fetchone()
        if row is None:
            raise ValidationError("Etape du projet introuvable.")
        if row["project_id"] != project_id:
            raise ValidationError("Cette etape appartient a un autre projet.")
    elif current and "project_id" in values and values["project_id"] != current.get("project_id"):
        values["phase_id"] = None


def _bucket(row):
    """Classe une tache pour l'affichage : late, today, soon, someday ou done."""
    if row["status"] == "done":
        return "done"
    if not row["deadline"]:
        return "someday"
    today = date.today().isoformat()
    if row["deadline"] < today:
        return "late"
    if row["deadline"] == today:
        return "today"
    return "soon"


def to_dict(row):
    """Convertit une ligne SQLite en dictionnaire serialisable en JSON."""
    data = dict(row)
    data["bucket"] = _bucket(row)
    return data


# Valeur de filtre demandant les taches rattachees a aucun projet.
NO_PROJECT = "none"


def list_tasks(status=None, project_id=None, space=None):
    """Liste les taches triees par echeance puis priorite.

    :param status: 'todo', 'done', ou None pour tout retourner
    :param project_id: identifiant de projet, ou NO_PROJECT pour les taches
        orphelines, ou None pour ne pas filtrer
    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    sql = [
        "SELECT t.*, p.name AS project_name, p.color AS project_color,"
        " pe.name AS person_name,"
        " COALESCE((SELECT SUM(duration_min) FROM session"
        "   WHERE task_id = t.id AND ended_at IS NOT NULL), 0) AS tracked_min",
        "FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " LEFT JOIN person pe ON pe.id = t.person_id",
        "WHERE 1 = 1",
    ]
    params = []

    if status:
        if status not in VALID_STATUS:
            raise ValidationError("Statut invalide.")
        sql.append("AND t.status = ?")
        params.append(status)

    if project_id == NO_PROJECT:
        sql.append("AND t.project_id IS NULL")
    elif project_id:
        sql.append("AND t.project_id = ?")
        params.append(project_id)

    filtre, valeurs = space_model.clause("t.space", space)
    if filtre:
        sql.append(filtre)
        params.extend(valeurs)

    sql.append(
        "ORDER BY t.status ASC,"
        " CASE WHEN t.deadline IS NULL THEN 1 ELSE 0 END, t.deadline ASC,"
        " t.priority ASC, t.position ASC, t.id ASC"
    )

    rows = get_db().execute(" ".join(sql), params).fetchall()
    return [to_dict(row) for row in rows]


def get_task(task_id):
    """Retourne une tache par identifiant, ou None si elle n'existe pas."""
    row = get_db().execute(
        "SELECT t.*, p.name AS project_name, p.color AS project_color,"
        " pe.name AS person_name,"
        " COALESCE((SELECT SUM(duration_min) FROM session"
        "   WHERE task_id = t.id AND ended_at IS NOT NULL), 0) AS tracked_min"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " LEFT JOIN person pe ON pe.id = t.person_id"
        " WHERE t.id = ?",
        (task_id,),
    ).fetchone()
    return to_dict(row) if row else None


def list_subtasks(parent_id):
    """Retourne les etapes d'une tache (sous-taches), dans l'ordre de creation."""
    rows = get_db().execute(
        "SELECT t.*, p.name AS project_name, p.color AS project_color,"
        " NULL AS person_name, 0 AS tracked_min"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " WHERE t.parent_id = ? ORDER BY t.position ASC, t.id ASC",
        (parent_id,),
    ).fetchall()
    return [to_dict(row) for row in rows]


def odoo_index():
    """Retourne les taches importees d'Odoo, indexees par identifiant Odoo.

    :return: {odoo_id: tache} avec les seules colonnes utiles a la synchro
    """
    rows = get_db().execute(
        "SELECT id, odoo_id, title, description, deadline, project_id, parent_id,"
        " position, status FROM task WHERE odoo_id IS NOT NULL"
    ).fetchall()
    return {row["odoo_id"]: dict(row) for row in rows}


def link_odoo(task_id, odoo_id):
    """Rattache une tache a son identifiant Odoo (hors liste blanche : la synchro seule l'ecrit)."""
    db = get_db()
    db.execute("UPDATE task SET odoo_id = ? WHERE id = ?", (odoo_id, task_id))
    db.commit()


def list_tasks_by_capture(capture_id):
    """Retourne les taches issues d'une capture donnee."""
    rows = get_db().execute(
        "SELECT t.*, p.name AS project_name, p.color AS project_color,"
        " pe.name AS person_name,"
        " COALESCE((SELECT SUM(duration_min) FROM session"
        "   WHERE task_id = t.id AND ended_at IS NOT NULL), 0) AS tracked_min"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " LEFT JOIN person pe ON pe.id = t.person_id"
        " WHERE t.capture_id = ? ORDER BY t.id ASC",
        (capture_id,),
    ).fetchall()
    return [to_dict(row) for row in rows]


def create_task(data, source=None):
    """Cree une tache et retourne sa representation complete.

    :param source: origine de la tache ('claude' pour une session Claude Code)
    """
    values = _clean(data)
    _check_phase(values)
    _check_kind(values)
    _apply_space(values)
    if "space" not in values:
        values["space"] = space_model.for_new()
    if source:
        values["source"] = str(source)[:40]
    columns = list(values.keys())
    placeholders = ", ".join("?" for _ in columns)

    db = get_db()
    cursor = db.execute(
        f"INSERT INTO task ({', '.join(columns)}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )
    db.commit()
    return get_task(cursor.lastrowid)


def update_task(task_id, data):
    """Met a jour partiellement une tache. Retourne None si elle n'existe pas."""
    current = get_task(task_id)
    if current is None:
        return None

    values = {
        key: value
        for key, value in _clean(data, partial=True).items()
        if key in EDITABLE_FIELDS
    }
    _check_phase(values, current)
    _check_kind(values, current)
    _apply_space(values, current)
    if not values:
        return get_task(task_id)
    # Echeance repoussee (et non avancee ni posee) : compteur de reports.
    old_deadline = current.get("deadline")
    new_deadline = values.get("deadline")
    if old_deadline and new_deadline and new_deadline > old_deadline:
        values["deadline_shifts"] = (current.get("deadline_shifts") or 0) + 1

    assignments = ", ".join(f"{column} = ?" for column in values)
    db = get_db()
    db.execute(
        f"UPDATE task SET {assignments} WHERE id = ?",
        list(values.values()) + [task_id],
    )
    if values.get("space") and values["space"] != current.get("space"):
        # Les sous-taches sans projet suivent l'espace de leur parente.
        db.execute(
            "UPDATE task SET space = ? WHERE parent_id = ? AND project_id IS NULL",
            (values["space"], task_id),
        )
        space_model.cascade_task(db, task_id, values["space"])
    knowledge.ensure_solution_note(task_id)
    db.commit()
    return get_task(task_id)


def toggle_task(task_id):
    """Bascule une tache entre 'todo' et 'done'. Retourne None si introuvable."""
    task = get_task(task_id)
    if task is None:
        return None

    if task["status"] == "done":
        new_status, done_at = "todo", None
    else:
        new_status = "done"
        done_at = datetime.now().replace(microsecond=0).isoformat(sep=" ")

    db = get_db()
    db.execute(
        "UPDATE task SET status = ?, done_at = ? WHERE id = ?",
        (new_status, done_at, task_id),
    )
    knowledge.ensure_solution_note(task_id)
    db.commit()
    return get_task(task_id)


def complete_on(task_id, day, at_time="18:00"):
    """Marque une tache terminee a une date passee : la case oubliee.

    La date de fin compte pour le bilan et le calibrage : cocher aujourd'hui
    une tache faite mardi la rangerait dans la mauvaise journee.

    :param day: date ISO 'AAAA-MM-JJ' de realisation
    :param at_time: heure retenue, 'HH:MM' (fin de journee par defaut)
    :return: tache a jour, ou None si introuvable
    :raises ValidationError: date invalide ou future
    """
    jour = _parse_date(day, "jour")
    if jour is None:
        raise ValidationError("Le jour de realisation est obligatoire.")
    if jour > date.today().isoformat():
        raise ValidationError("Une tache ne peut pas etre terminee dans le futur.")
    if get_task(task_id) is None:
        return None

    db = get_db()
    db.execute(
        "UPDATE task SET status = 'done', done_at = ? WHERE id = ?",
        (f"{jour} {at_time}:00", task_id),
    )
    knowledge.ensure_solution_note(task_id)
    db.commit()
    return get_task(task_id)


def list_done_on(day):
    """Retourne les taches terminees un jour donne, dans l'ordre de cloture."""
    rows = get_db().execute(
        "SELECT t.*, p.name AS project_name, p.color AS project_color,"
        " pe.name AS person_name,"
        " COALESCE((SELECT SUM(duration_min) FROM session"
        "   WHERE task_id = t.id AND ended_at IS NOT NULL), 0) AS tracked_min"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " LEFT JOIN person pe ON pe.id = t.person_id"
        " WHERE t.status = 'done' AND date(t.done_at) = ?"
        " ORDER BY t.done_at ASC, t.id ASC",
        (day,),
    ).fetchall()
    return [to_dict(row) for row in rows]


def list_open_on(day, limit=60):
    """Retourne les taches ouvertes qui existaient deja ce jour-la.

    Sert au rattrapage d'un jour passe : une tache creee apres ne peut pas
    avoir ete faite ce jour-la.
    """
    rows = get_db().execute(
        "SELECT t.*, p.name AS project_name, p.color AS project_color,"
        " pe.name AS person_name, 0 AS tracked_min"
        " FROM task t LEFT JOIN project p ON p.id = t.project_id"
        " LEFT JOIN person pe ON pe.id = t.person_id"
        " WHERE t.status = 'todo' AND date(t.created_at) <= ?"
        " ORDER BY t.priority ASC,"
        " CASE WHEN t.deadline IS NULL THEN 1 ELSE 0 END, t.deadline ASC, t.id ASC"
        " LIMIT ?",
        (day, limit),
    ).fetchall()
    return [to_dict(row) for row in rows]


def pinned_ids(day):
    """Retourne les taches ouvertes epinglees sur un jour, dans l'ordre choisi."""
    rows = get_db().execute(
        "SELECT id FROM task WHERE pinned_day = ? AND status = 'todo'"
        " ORDER BY pinned_order ASC, id ASC",
        (day,),
    ).fetchall()
    return [row["id"] for row in rows]


def pin_tasks(day, task_ids):
    """Fixe l'ordre des taches d'un jour : elles passent avant le reste.

    Remplace l'epinglage existant du jour : la liste recue est l'ordre voulu,
    complet. Les identifiants inconnus ou deja termines sont ignores.

    :param day: date ISO du plan
    :param task_ids: identifiants dans l'ordre souhaite
    :return: identifiants effectivement epingles, dans l'ordre
    """
    jour = _parse_date(day, "jour")
    if jour is None:
        raise ValidationError("Le jour du plan est obligatoire.")

    ordre = []
    for brut in task_ids or []:
        identifiant = _parse_int(brut, "tache", minimum=1)
        if identifiant not in ordre:
            ordre.append(identifiant)

    db = get_db()
    try:
        db.execute(
            "UPDATE task SET pinned_day = NULL, pinned_order = NULL WHERE pinned_day = ?",
            (jour,),
        )
        db.executemany(
            "UPDATE task SET pinned_day = ?, pinned_order = ? WHERE id = ? AND status = 'todo'",
            [(jour, index, identifiant) for index, identifiant in enumerate(ordre)],
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return pinned_ids(jour)


def pin_task(task_id, day, first=True):
    """Epingle une tache sur un jour, en tete (« maintenant ») ou en fin.

    :return: tache a jour, ou None si introuvable
    """
    task = get_task(task_id)
    if task is None:
        return None
    if task["status"] != "todo":
        raise ValidationError("Une tache terminee ne se planifie plus.")

    jour = _parse_date(day, "jour")
    ordre = [identifiant for identifiant in pinned_ids(jour) if identifiant != task_id]
    ordre = [task_id] + ordre if first else ordre + [task_id]
    pin_tasks(jour, ordre)
    return get_task(task_id)


def unpin_task(task_id):
    """Rend une tache au classement automatique. Retourne False si introuvable."""
    db = get_db()
    curseur = db.execute(
        "UPDATE task SET pinned_day = NULL, pinned_order = NULL WHERE id = ?", (task_id,)
    )
    db.commit()
    return curseur.rowcount > 0


def delete_task(task_id):
    """Supprime une tache et ses sous-taches. Retourne True si une ligne a ete supprimee."""
    db = get_db()
    cursor = db.execute("DELETE FROM task WHERE id = ?", (task_id,))
    db.commit()
    return cursor.rowcount > 0


# Mots vides ecartes du rapprochement : presents partout, ils feraient passer
# deux taches sans rapport pour des doublons.
STOPWORDS = frozenset(
    """a au aux avec ce ces dans de des du elle en et eux il je la le les leur
    lui ma mais me meme mes moi mon ne nos notre nous on ou par pas pour qu que
    qui sa se ses son sur ta te tes toi ton tu un une vos votre vous y
    pour faire mettre voir sujet tache probleme point""".split()
)

# Proportion de mots communs a partir de laquelle deux taches sont rapprochees.
# 0.6 tient la route en pratique : en dessous, tout se ressemble.
DUPLICATE_THRESHOLD = 0.6

# Longueur minimale d'un mot retenu pour le rapprochement.
MIN_WORD_CHARS = 3


def _signature(task):
    """Retourne les mots significatifs du titre d'une tache."""
    import re

    mots = re.findall(r"[a-z0-9]+", (task.get("title") or "").lower())
    return {mot for mot in mots if len(mot) >= MIN_WORD_CHARS and mot not in STOPWORDS}


def _similarity(gauche, droite):
    """Proportion de mots communs entre deux jeux de mots (indice de Jaccard)."""
    if not gauche or not droite:
        return 0.0
    return len(gauche & droite) / len(gauche | droite)


def similar_open_tasks(title, threshold=0.5, limit=5, space=None):
    """Taches ouvertes dont le titre ressemble a celui fourni.

    Permet a une session Claude de reprendre une tache existante au lieu
    d'ouvrir un doublon.

    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    cible = _signature({"title": title})
    proches = []
    for task in list_tasks(status="todo", space=space):
        score = _similarity(cible, _signature(task))
        if score >= threshold:
            proches.append((score, task))
    proches.sort(key=lambda item: -item[0])
    return [task for _score, task in proches[:limit]]


def find_duplicates(threshold=DUPLICATE_THRESHOLD, project_id=None):
    """Retourne les groupes de taches ouvertes qui se recouvrent.

    Le rapprochement porte sur les mots du titre, pas sur le texte entier :
    deux formulations d'un meme travail partagent leur vocabulaire, pas leur
    ponctuation. Rien n'est fusionne ici, seulement propose.

    :param threshold: proportion de mots communs exigee, 0 a 1
    :param project_id: restreint le rapprochement a un projet
    :return: liste de groupes, chacun trie du plus ancien au plus recent
    """
    taches = [
        tache
        for tache in list_tasks(status="todo")
        if project_id is None or tache["project_id"] == project_id
    ]

    signatures = {tache["id"]: _signature(tache) for tache in taches}
    par_id = {tache["id"]: tache for tache in taches}

    # Union-find rudimentaire : deux taches proches rejoignent le meme groupe,
    # et la transitivite regroupe A-B et B-C en un seul trio.
    parent = {tache["id"]: tache["id"] for tache in taches}

    def racine(identifiant):
        while parent[identifiant] != identifiant:
            parent[identifiant] = parent[parent[identifiant]]
            identifiant = parent[identifiant]
        return identifiant

    for index, gauche in enumerate(taches):
        for droite in taches[index + 1 :]:
            if _similarity(signatures[gauche["id"]], signatures[droite["id"]]) < threshold:
                continue
            racine_gauche, racine_droite = racine(gauche["id"]), racine(droite["id"])
            if racine_gauche != racine_droite:
                parent[racine_droite] = racine_gauche

    groupes = {}
    for identifiant in parent:
        groupes.setdefault(racine(identifiant), []).append(par_id[identifiant])

    return [
        sorted(membres, key=lambda item: item["id"])
        for membres in groupes.values()
        if len(membres) > 1
    ]


def merge_tasks(keep_id, merge_ids):
    """Fusionne des taches dans une seule. Retourne la tache conservee.

    Le point delicat est le temps chronometre : les sessions des taches
    absorbees sont reattribuees a celle qu'on garde. Sans cela, fusionner
    effacerait du travail reellement fait, ce qui fausserait le calibrage des
    estimations et le bilan, les deux fonctions qui reposent dessus.

    Les autres champs sont arbitres par le plus contraignant : l'echeance la
    plus proche, la priorite la plus haute, et toute valeur renseignee l'emporte
    sur une valeur absente.

    :param keep_id: tache conservee
    :param merge_ids: taches absorbees puis supprimees
    :raises ValidationError: tache introuvable, ou keep_id present dans merge_ids
    """
    gardee = get_task(keep_id)
    if gardee is None:
        raise ValidationError(f"Tache {keep_id} introuvable.")

    cibles = []
    for identifiant in merge_ids:
        if identifiant == keep_id:
            raise ValidationError("Une tache ne peut pas etre fusionnee avec elle-meme.")
        absorbee = get_task(identifiant)
        if absorbee is None:
            raise ValidationError(f"Tache {identifiant} introuvable.")
        cibles.append(absorbee)

    if not cibles:
        return gardee

    champs = {}
    descriptions = [(gardee.get("description") or "").strip()]

    for absorbee in cibles:
        texte = (absorbee.get("description") or "").strip()
        if texte and texte not in descriptions:
            descriptions.append(texte)

        # L'echeance la plus proche : c'est la plus contraignante des deux.
        if absorbee["deadline"] and (
            not champs.get("deadline", gardee["deadline"])
            or absorbee["deadline"] < champs.get("deadline", gardee["deadline"])
        ):
            champs["deadline"] = absorbee["deadline"]

        # La priorite la plus haute, donc le plus petit nombre.
        if absorbee["priority"] < champs.get("priority", gardee["priority"]):
            champs["priority"] = absorbee["priority"]

        for nom in ("project_id", "person_id"):
            if not champs.get(nom, gardee.get(nom)) and absorbee.get(nom):
                champs[nom] = absorbee[nom]

    descriptions = [texte for texte in descriptions if texte]
    if len(descriptions) > 1:
        champs["description"] = "\n\n".join(descriptions)

    db = get_db()
    identifiants = [absorbee["id"] for absorbee in cibles]
    marques = ",".join("?" for _ in identifiants)

    # Le temps passe suit la tache conservee.
    db.execute(
        f"UPDATE session SET task_id = ? WHERE task_id IN ({marques})",
        [keep_id] + identifiants,
    )
    # Les notes aussi : elles portent du contexte ecrit a la main.
    db.execute(
        f"UPDATE note SET task_id = ? WHERE task_id IN ({marques})",
        [keep_id] + identifiants,
    )
    # Les attentes bloquees par une tache absorbee pointent vers la survivante.
    db.execute(
        f"UPDATE waiting SET task_id = ? WHERE task_id IN ({marques})",
        [keep_id] + identifiants,
    )
    # Les sous-taches sont rattachees plutot que supprimees en cascade.
    db.execute(
        f"UPDATE task SET parent_id = ? WHERE parent_id IN ({marques})",
        [keep_id] + identifiants,
    )
    db.execute(f"DELETE FROM task WHERE id IN ({marques})", identifiants)
    db.commit()

    if champs:
        update_task(keep_id, champs)

    return get_task(keep_id)


def stats(space=None):
    """Compteurs affiches en tete du tableau de bord.

    :param space: convention de models/space.py (None = perimetre de la requete)
    """
    today = date.today().isoformat()
    filtre, valeurs = space_model.clause("space", space)
    filtre = filtre.replace(" AND ", " WHERE ", 1)
    row = get_db().execute(
        "SELECT"
        "  COUNT(*) FILTER (WHERE status = 'todo') AS todo_count,"
        "  COUNT(*) FILTER (WHERE status = 'todo' AND deadline < ?) AS late_count,"
        "  COUNT(*) FILTER (WHERE status = 'todo' AND deadline = ?) AS today_count,"
        "  COALESCE(SUM(estimate_min) FILTER"
        "    (WHERE status = 'todo' AND deadline IS NOT NULL AND deadline <= ?), 0) AS today_load,"
        "  COUNT(*) FILTER (WHERE status = 'done' AND date(done_at) = ?) AS done_today"
        " FROM task" + filtre,
        (today, today, today, today, *valeurs),
    ).fetchone()
    return dict(row)
