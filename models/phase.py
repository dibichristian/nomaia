"""Etapes d'un projet (jalons) : CRUD et gabarit generique.

Un projet se pilote par ses etapes, du recueil du besoin a la maintenance.
Chaque etape a un poids : l'avancement global d'un projet est la somme, etape par etape, du poids multiplie par l'avancement.
Les regles de calcul vivent dans services/project_board.py ; ce module ne fait
que lire et ecrire.
"""
import unicodedata
from datetime import date

from models import get_db
from models.task import ValidationError, _parse_date, _parse_int

# Statuts d'une etape : a venir, en cours, bloquee, terminee.
VALID_STATUS = ("todo", "doing", "blocked", "done")

# Gabarit generique : les etapes d'un projet type, groupees par grande etape,
# avec leur poids en points (total 100). Chaque projet peut ensuite ajouter,
# renommer ou supprimer ses etapes.
TEMPLATE = (
    ("Cadrage", "Recueil du besoin", 8),
    ("Cadrage", "Analyse et conception", 10),
    ("Réalisation", "Configuration", 12),
    ("Réalisation", "Développements", 15),
    ("Réalisation", "Reprise des données", 8),
    ("Validation", "Recette", 12),
    ("Validation", "Corrections", 6),
    ("Validation", "Formation des utilisateurs", 6),
    ("Déploiement", "Mise en production", 12),
    ("Déploiement", "Accompagnement au démarrage", 6),
    ("Support", "Maintenance et support", 5),
)


def _clean(data, partial=False):
    """Normalise et valide les donnees d'une etape.

    :param data: donnees brutes issues du client
    :param partial: si True, seuls les champs presents sont traites (PATCH)
    :return: dictionnaire colonne -> valeur pret pour l'ecriture SQL
    """
    cleaned = {}

    if "name" in data or not partial:
        name = (data.get("name") or "").strip()
        if not name:
            raise ValidationError("Le nom de l'etape est obligatoire.")
        if len(name) > 120:
            raise ValidationError("Le nom de l'etape ne peut pas depasser 120 caracteres.")
        cleaned["name"] = name

    if "stage" in data:
        cleaned["stage"] = (data.get("stage") or "").strip()[:60] or None

    if "sequence" in data:
        cleaned["sequence"] = _parse_int(data.get("sequence"), "ordre", minimum=0, default=0)

    if "weight" in data or not partial:
        cleaned["weight"] = _parse_int(data.get("weight"), "poids", minimum=0, maximum=100, default=0)

    if "status" in data or not partial:
        status = data.get("status") or "todo"
        if status not in VALID_STATUS:
            raise ValidationError("Statut d'etape invalide.")
        cleaned["status"] = status

    if "progress_pct" in data:
        cleaned["progress_pct"] = _parse_int(
            data.get("progress_pct"), "avancement", minimum=0, maximum=100, default=0
        )

    if "planned_date" in data:
        cleaned["planned_date"] = _parse_date(data.get("planned_date"), "date prevue")

    if "done_date" in data:
        cleaned["done_date"] = _parse_date(data.get("done_date"), "date de fin")

    return cleaned


def _close_dates(values, previous=None):
    """Tient la date de fin coherente avec le statut.

    Une etape terminee sans date recoit celle du jour ; une etape rouverte la
    perd, sinon elle compterait encore comme jalon atteint.
    """
    status = values.get("status")
    if status == "done" and not values.get("done_date") and not (previous or {}).get("done_date"):
        values["done_date"] = date.today().isoformat()
    elif status and status != "done":
        values["done_date"] = None
    return values


def list_phases(project_id=None):
    """Etapes d'un projet, ou de tous les projets, dans l'ordre du projet."""
    sql = "SELECT * FROM project_phase"
    params = []
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params.append(project_id)
    sql += " ORDER BY project_id, sequence, id"
    return [dict(row) for row in get_db().execute(sql, params).fetchall()]


def get_phase(phase_id):
    """Retourne une etape, ou None."""
    row = get_db().execute("SELECT * FROM project_phase WHERE id = ?", (phase_id,)).fetchone()
    return dict(row) if row else None


def create_phase(project_id, data):
    """Ajoute une etape en fin de projet (sauf ordre fourni)."""
    values = _close_dates(_clean(data))
    if "sequence" not in values:
        row = get_db().execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM project_phase WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        values["sequence"] = row[0]
    values["project_id"] = project_id

    columns = list(values)
    db = get_db()
    cursor = db.execute(
        f"INSERT INTO project_phase ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
        [values[column] for column in columns],
    )
    db.commit()
    return get_phase(cursor.lastrowid)


def _close_previous(db, phase):
    """Termine les etapes qui precedent une etape terminee.

    Une etape ne se termine pas avant celles qui la precedent : marquer la 5
    terminee clot aussi les 1 a 4 restees ouvertes, a la meme date.

    :return: nombre d'etapes closes au passage
    """
    cursor = db.execute(
        "UPDATE project_phase SET status = 'done', done_date = COALESCE(done_date, ?)"
        " WHERE project_id = ? AND sequence < ? AND status != 'done'",
        (phase["done_date"], phase["project_id"], phase["sequence"]),
    )
    return cursor.rowcount


def update_phase(phase_id, data):
    """Met a jour une etape. Retourne None si elle n'existe pas.

    Une etape qui passe a « terminee » clot les etapes precedentes encore
    ouvertes ; leur nombre est renvoye sous 'cascaded'.
    """
    previous = get_phase(phase_id)
    if previous is None:
        return None

    values = _close_dates(_clean(data, partial=True), previous)
    if not values:
        return previous

    assignments = ", ".join(f"{column} = ?" for column in values)
    db = get_db()
    db.execute(
        f"UPDATE project_phase SET {assignments} WHERE id = ?",
        list(values.values()) + [phase_id],
    )
    phase = get_phase(phase_id)
    closes = 0
    if phase["status"] == "done" and previous["status"] != "done":
        closes = _close_previous(db, phase)
    db.commit()
    phase["cascaded"] = closes
    return phase


def has_tasks(phase_id):
    """Vrai si au moins une tache est rangee dans l'etape."""
    row = get_db().execute("SELECT 1 FROM task WHERE phase_id = ? LIMIT 1", (phase_id,)).fetchone()
    return row is not None


def _fold(texte):
    """Texte compare sans casse ni accents : 'Recette' == 'recette'."""
    decompose = unicodedata.normalize("NFKD", str(texte or "").strip().casefold())
    return "".join(car for car in decompose if not unicodedata.combining(car))


def find_phase(project_id, ref):
    """Retrouve une etape du projet par son numero (1, 2...) ou son nom.

    Le nom se compare sans casse ni accents ; a defaut d'egalite, un morceau
    de nom suffit s'il ne designe qu'une etape.

    :param ref: numero ou nom de l'etape
    :return: etape trouvee
    :raises ValidationError: projet sans etape, etape inconnue ou ambigue
        (le message liste les etapes du projet)
    """
    texte = str(ref if ref is not None else "").strip()
    etapes = list_phases(project_id)
    if not etapes:
        raise ValidationError("Ce projet n'a pas encore d'etapes : elles se posent depuis la fiche projet.")
    if texte.isdigit():
        trouvees = [etape for etape in etapes if etape["sequence"] == int(texte)]
    else:
        cle = _fold(texte)
        trouvees = [etape for etape in etapes if cle and _fold(etape["name"]) == cle]
        trouvees = trouvees or [etape for etape in etapes if cle and cle in _fold(etape["name"])]
    if len(trouvees) != 1:
        connues = ", ".join(f"{etape['sequence']}. {etape['name']}" for etape in etapes)
        raise ValidationError(f"Etape '{texte}' introuvable ou ambigue. Etapes du projet : {connues}.")
    return trouvees[0]


def delete_phase(phase_id):
    """Supprime une etape. Ses taches sont conservees, simplement sans etape.

    Fait a la main et non par la cle etrangere : sur une base migree, la
    colonne task.phase_id est ajoutee par ALTER TABLE et l'application ne doit
    pas dependre de l'etat de PRAGMA foreign_keys.
    """
    db = get_db()
    db.execute("UPDATE task SET phase_id = NULL WHERE phase_id = ?", (phase_id,))
    cursor = db.execute("DELETE FROM project_phase WHERE id = ?", (phase_id,))
    db.commit()
    return cursor.rowcount > 0


def apply_template(project_id):
    """Cree les etapes du gabarit generique sur un projet sans etape.

    :return: etapes creees, dans l'ordre
    :raises ValidationError: le projet a deja des etapes
    """
    db = get_db()
    existing = db.execute(
        "SELECT COUNT(*) FROM project_phase WHERE project_id = ?", (project_id,)
    ).fetchone()[0]
    if existing:
        raise ValidationError("Ce projet a deja des etapes : le gabarit ne s'applique qu'a un projet vierge.")

    db.executemany(
        "INSERT INTO project_phase (project_id, stage, name, weight, sequence) VALUES (?, ?, ?, ?, ?)",
        [
            (project_id, stage, name, weight, index)
            for index, (stage, name, weight) in enumerate(TEMPLATE, start=1)
        ],
    )
    db.commit()
    return list_phases(project_id)


def board_tasks(project_id=None):
    """Taches feuilles reduites a ce qu'il faut pour le pilotage, en une requete.

    Une tache detaillee en sous-taches est mesuree par ses sous-taches : la
    compter aussi doublerait son poids (meme regle que project.board_rows).
    Une sous-tache sans etape prend celle de sa tache parente.

    :param project_id: restreint a un projet ; None pour toutes les taches
        rattachees a un projet
    :return: liste de dictionnaires
    """
    sql = [
        "SELECT t.id, t.project_id, COALESCE(t.phase_id, pt.phase_id) AS phase_id,",
        "  t.status, t.blocked, t.side, t.deadline, t.progress_pct, t.estimate_min,",
        "  t.created_at, t.kind, t.severity, COALESCE(s.tracked, 0) AS tracked_min",
        "FROM task t",
        "LEFT JOIN task pt ON pt.id = t.parent_id",
        "LEFT JOIN (",
        "  SELECT task_id, SUM(duration_min) AS tracked FROM session",
        "  WHERE ended_at IS NOT NULL GROUP BY task_id",
        ") s ON s.task_id = t.id",
        "WHERE t.project_id IS NOT NULL",
        "  AND t.id NOT IN (SELECT parent_id FROM task WHERE parent_id IS NOT NULL)",
    ]
    params = []
    if project_id is not None:
        sql.append("AND t.project_id = ?")
        params.append(project_id)
    return [dict(row) for row in get_db().execute(" ".join(sql), params).fetchall()]
