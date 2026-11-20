"""Acces a la base SQLite : connexion, schema, sauvegarde.

La connexion est portee par le contexte d'application Flask (`g`) afin
d'ouvrir un seul curseur par requete HTTP.
"""
import sqlite3
from datetime import datetime

from flask import g

import config

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS project (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    color       TEXT    NOT NULL DEFAULT '#6366f1',
    status      TEXT    NOT NULL DEFAULT 'active',
    deadline    TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS task (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER REFERENCES project(id) ON DELETE SET NULL,
    parent_id     INTEGER REFERENCES task(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL,
    description   TEXT,
    priority      INTEGER NOT NULL DEFAULT 3,
    estimate_min  INTEGER NOT NULL DEFAULT 30,
    deadline      TEXT,
    energy        TEXT    NOT NULL DEFAULT 'med',
    status        TEXT    NOT NULL DEFAULT 'todo',
    position      INTEGER NOT NULL DEFAULT 0,
    capture_id    INTEGER REFERENCES capture(id) ON DELETE SET NULL,
    person_id     INTEGER REFERENCES person(id) ON DELETE SET NULL,
    needs_review  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    done_at       TEXT
);

CREATE TABLE IF NOT EXISTS project_phase (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name          TEXT    NOT NULL,
    stage         TEXT,
    sequence      INTEGER NOT NULL DEFAULT 0,
    weight        INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'todo',
    progress_pct  INTEGER NOT NULL DEFAULT 0,
    planned_date  TEXT,
    done_date     TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS capture (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL DEFAULT 'web',
    raw_text     TEXT,
    image_path   TEXT,
    status       TEXT    NOT NULL DEFAULT 'new',
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS proposal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id   INTEGER NOT NULL REFERENCES capture(id) ON DELETE CASCADE,
    summary      TEXT,
    confidence   REAL    NOT NULL DEFAULT 0,
    payload_json TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id INTEGER NOT NULL REFERENCES capture(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS session (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    started_at   TEXT    NOT NULL,
    ended_at     TEXT,
    duration_min INTEGER
);

CREATE TABLE IF NOT EXISTS note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER REFERENCES task(id) ON DELETE CASCADE,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS review (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT    NOT NULL,
    period_end   TEXT    NOT NULL,
    payload_json TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT    NOT NULL,
    model         TEXT,
    purpose       TEXT    NOT NULL DEFAULT 'triage',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS person (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES project(id) ON DELETE SET NULL,
    name       TEXT    NOT NULL,
    role       TEXT,
    email      TEXT,
    phone      TEXT,
    notes      TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Projets d'un interlocuteur : un client ou un collegue intervient souvent
-- sur plusieurs projets. person.project_id reste son projet principal, et
-- figure toujours ici (voir _migrate).
CREATE TABLE IF NOT EXISTS person_project (
    person_id  INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, project_id)
);

CREATE TABLE IF NOT EXISTS waiting (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES task(id) ON DELETE SET NULL,
    subject       TEXT    NOT NULL,
    since         TEXT    NOT NULL,
    due           TEXT,
    status        TEXT    NOT NULL DEFAULT 'open',
    last_nudge_at TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER REFERENCES project(id) ON DELETE SET NULL,
    title        TEXT    NOT NULL,
    location     TEXT,
    starts_at    TEXT    NOT NULL,
    ends_at      TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'meeting',
    repeat       TEXT    NOT NULL DEFAULT 'none',
    repeat_until TEXT,
    notes        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS event_exception (
    event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    day      TEXT    NOT NULL,
    PRIMARY KEY (event_id, day)
);

-- Journees entieres non travaillees : conges et feries. Distinct de 'event' :
-- une plage de plusieurs jours n'a ni heure ni repetition, et casserait le
-- filtre sur starts_at comme le plafond de 24 h des evenements.
-- country : pays du ferie importe automatiquement, NULL pour une saisie.
CREATE TABLE IF NOT EXISTS day_off (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    start_day  TEXT    NOT NULL,
    end_day    TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'leave',
    country    TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Agendas externes relus en ICS (Outlook publie). L'URL est un secret :
-- quiconque la possede lit l'agenda, elle est donc chiffree et purgee des
-- sauvegardes comme les autres cles.
CREATE TABLE IF NOT EXISTS calendar_source (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_sync_at TEXT,
    last_error   TEXT,
    last_count   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Memoire des alertes proactives : une cle par sujet signale (tache et
-- echeance, proposition, projet), avec la date du dernier envoi. C'est elle
-- qui empeche NOMAIA de repeter la meme alerte jusqu'a devenir du bruit.
CREATE TABLE IF NOT EXISTS alert_log (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);

-- Messages Telegram envoyes a propos d'une capture. Repondre a l'un d'eux
-- (fonction « Repondre » de Telegram) adresse la reponse a cette capture,
-- meme si d'autres questions sont arrivees entre-temps.
CREATE TABLE IF NOT EXISTS telegram_link (
    message_id INTEGER PRIMARY KEY,
    capture_id INTEGER NOT NULL REFERENCES capture(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Conversations du chat : chacune porte son fil et son plan en attente.
CREATE TABLE IF NOT EXISTS conversation (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT,
    archived   INTEGER NOT NULL DEFAULT 0,
    -- Rang d'activite croissant : ordonne les conversations sans dependre
    -- de l'horloge, trop grossiere sous Windows pour departager deux ecritures.
    activity   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS chat_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    tools_json TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS chat_plan (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    steps_json   TEXT    NOT NULL,
    reply        TEXT,
    status       TEXT    NOT NULL DEFAULT 'pending',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Projets Odoo rattaches a un projet NOMAIA : plusieurs projets Odoo (une
-- integration et ses evolutions) peuvent alimenter un meme projet ici.
CREATE TABLE IF NOT EXISTS project_odoo_link (
    odoo_id    INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE
);

-- Mails Outlook deja lus par services/mail_reader.py : un mail vu n'est
-- jamais redepose, meme si sa capture a ete supprimee depuis.
CREATE TABLE IF NOT EXISTS mail_seen (
    message_id TEXT    PRIMARY KEY,
    capture_id INTEGER REFERENCES capture(id) ON DELETE SET NULL,
    seen_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Compte rendu d'une reunion, d'une intervention ou d'une formation. Rattache
-- a une seance d'un evenement de l'agenda (event_id + held_on : un evenement
-- repete a un compte rendu par seance), mais garde sa propre copie du titre,
-- de la date et du projet : il survit a l'evenement (agenda Outlook resynchronise).
CREATE TABLE IF NOT EXISTS minutes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER REFERENCES event(id) ON DELETE SET NULL,
    project_id  INTEGER REFERENCES project(id) ON DELETE SET NULL,
    kind        TEXT    NOT NULL DEFAULT 'meeting',
    title       TEXT    NOT NULL,
    held_on     TEXT    NOT NULL,
    starts_at   TEXT,
    attendees   TEXT    NOT NULL DEFAULT '',
    objectives  TEXT    NOT NULL DEFAULT '',
    discussed   TEXT    NOT NULL DEFAULT '',
    topics      TEXT    NOT NULL DEFAULT '',
    followups   TEXT    NOT NULL DEFAULT '',
    notes       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'draft',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Elements d'un compte rendu : decision (inscrite au registre du projet),
-- action (transformee en tache), demande du client. Chaque conversion n'a
-- lieu qu'une fois : task_id et note_id la memorisent.
CREATE TABLE IF NOT EXISTS minutes_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    minutes_id  INTEGER NOT NULL REFERENCES minutes(id) ON DELETE CASCADE,
    type        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    owner       TEXT    NOT NULL DEFAULT '',
    due         TEXT,
    task_id     INTEGER REFERENCES task(id) ON DELETE SET NULL,
    note_id     INTEGER REFERENCES note(id) ON DELETE SET NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Base de connaissances : notes techniques en Markdown (solutions, extraits de
-- code, procedures). Une anomalie resolue avec une solution produit sa note
-- (source_task_id, unique : une note par anomalie, tenue a jour).
CREATE TABLE IF NOT EXISTS kb_note (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    content        TEXT    NOT NULL DEFAULT '',
    category       TEXT    NOT NULL DEFAULT 'other',
    project_id     INTEGER REFERENCES project(id) ON DELETE SET NULL,
    odoo_version   TEXT    NOT NULL DEFAULT '',
    module         TEXT    NOT NULL DEFAULT '',
    tags           TEXT    NOT NULL DEFAULT '',
    pinned         INTEGER NOT NULL DEFAULT 0,
    source_task_id INTEGER REFERENCES task(id) ON DELETE SET NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Cloture de journee : la note « pour demain » ecrite le soir, relue le lendemain
-- matin sur le plan du jour et reprise par le bilan. Le recapitulatif lui-meme
-- est calcule (taches terminees, temps suivi, journal), jamais ressaisi.
CREATE TABLE IF NOT EXISTS day_note (
    day         TEXT    PRIMARY KEY,
    tomorrow    TEXT    NOT NULL DEFAULT '',
    closed_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Charge planifiee d'une journee, notee quand le plan du jour est affiche. Le
-- plan est recalcule a chaque lecture : sans cette trace, le planifie d'un jour
-- passe serait perdu. planned_min = plan restant + taches bouclees ce jour-la,
-- pour que cocher une tache ne fasse pas baisser la charge du jour.
CREATE TABLE IF NOT EXISTS plan_day (
    day          TEXT    PRIMARY KEY,
    planned_min  INTEGER NOT NULL DEFAULT 0,
    capacity_min INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Pieces jointes : un fichier rattache a une tache (livrable recu), un projet,
-- un compte rendu ou une note. Fichier sur disque dans public/uploads/attachments
-- sous un nom aleatoire ; le nom d'origine n'est garde qu'ici.
CREATE TABLE IF NOT EXISTS attachment (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type   TEXT    NOT NULL,
    target_id     INTEGER NOT NULL,
    filename      TEXT    NOT NULL,
    stored_name   TEXT    NOT NULL UNIQUE,
    content_type  TEXT    NOT NULL DEFAULT 'application/octet-stream',
    size          INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_attachment_target ON attachment(target_type, target_id);

-- Revision des donnees affichees : une ligne unique dont le compteur est
-- incremente par triggers a chaque ecriture (voir models/revision.py). Les
-- pages ouvertes le relisent pour se recharger sans F5.
CREATE TABLE IF NOT EXISTS data_revision (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    rev INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO data_revision (id, rev) VALUES (1, 0);

-- Une cible supprimee emporte ses pieces jointes (les fichiers orphelins sont
-- effaces au demarrage suivant, voir models/attachment.py).
CREATE TRIGGER IF NOT EXISTS attachment_task_ad AFTER DELETE ON task
BEGIN DELETE FROM attachment WHERE target_type = 'task' AND target_id = old.id; END;
CREATE TRIGGER IF NOT EXISTS attachment_project_ad AFTER DELETE ON project
BEGIN DELETE FROM attachment WHERE target_type = 'project' AND target_id = old.id; END;
CREATE TRIGGER IF NOT EXISTS attachment_minutes_ad AFTER DELETE ON minutes
BEGIN DELETE FROM attachment WHERE target_type = 'minutes' AND target_id = old.id; END;
CREATE TRIGGER IF NOT EXISTS attachment_kb_ad AFTER DELETE ON kb_note
BEGIN DELETE FROM attachment WHERE target_type = 'note' AND target_id = old.id; END;

CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_source ON kb_note(source_task_id) WHERE source_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kb_project ON kb_note(project_id, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_minutes_session ON minutes(event_id, held_on) WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_minutes_project ON minutes(project_id, held_on);
CREATE INDEX IF NOT EXISTS idx_minutes_item    ON minutes_item(minutes_id, position);

CREATE INDEX IF NOT EXISTS idx_task_status     ON task(status);
CREATE INDEX IF NOT EXISTS idx_task_deadline   ON task(deadline);
CREATE INDEX IF NOT EXISTS idx_task_project    ON task(project_id);
CREATE INDEX IF NOT EXISTS idx_session_task    ON session(task_id);
CREATE INDEX IF NOT EXISTS idx_capture_status  ON capture(status);
CREATE INDEX IF NOT EXISTS idx_proposal_status ON proposal(status, capture_id);
CREATE INDEX IF NOT EXISTS idx_message_capture ON message(capture_id);
CREATE INDEX IF NOT EXISTS idx_session_open    ON session(ended_at);
CREATE INDEX IF NOT EXISTS idx_chat_created    ON chat_message(created_at);
CREATE INDEX IF NOT EXISTS idx_chat_plan_status ON chat_plan(status);
CREATE INDEX IF NOT EXISTS idx_event_start     ON event(starts_at);
CREATE INDEX IF NOT EXISTS idx_event_repeat    ON event(repeat);
CREATE INDEX IF NOT EXISTS idx_day_off_period  ON day_off(start_day, end_day);
CREATE INDEX IF NOT EXISTS idx_person_project  ON person(project_id);
CREATE INDEX IF NOT EXISTS idx_person_project_link ON person_project(project_id);
CREATE INDEX IF NOT EXISTS idx_waiting_status  ON waiting(status, person_id);
CREATE INDEX IF NOT EXISTS idx_review_period   ON review(period_start);
CREATE INDEX IF NOT EXISTS idx_usage_date      ON usage(created_at);
CREATE INDEX IF NOT EXISTS idx_phase_project   ON project_phase(project_id, sequence);
"""

# Colonnes ajoutees apres la mise en service : appliquees par _migrate().
MIGRATIONS = (
    ("task", "capture_id", "INTEGER REFERENCES capture(id) ON DELETE SET NULL"),
    ("task", "needs_review", "INTEGER NOT NULL DEFAULT 0"),
    # A qui cette tache est due. Une promesse a tenir est une tache avec un
    # destinataire, pas une entite parallele qu'il faudrait synchroniser.
    ("task", "person_id", "INTEGER REFERENCES person(id) ON DELETE SET NULL"),
    # Provenance d'un evenement ou d'un jour de repos importe d'un agenda
    # externe. Une ligne qui porte une source est en lecture seule : elle est
    # remplacee a chaque synchronisation. Supprimer la source supprime ses
    # lignes.
    ("event", "source_id", "INTEGER REFERENCES calendar_source(id) ON DELETE CASCADE"),
    ("event", "external_uid", "TEXT"),
    ("day_off", "source_id", "INTEGER REFERENCES calendar_source(id) ON DELETE CASCADE"),
    # Nature d'un agenda externe : 'ics' (flux publie, URL chiffree) ou 'odoo'
    # (connexion des reglages Odoo, URL vide ; voir services/odoo_calendar.py).
    ("calendar_source", "kind", "TEXT NOT NULL DEFAULT 'ics'"),
    # Conge en attente de validation (importe d'Odoo) : affiche dans l'agenda,
    # mais ne retire aucune capacite (ignore par day_off.off_map).
    ("day_off", "tentative", "INTEGER NOT NULL DEFAULT 0"),
    # Autres noms sous lesquels un interlocuteur a ete cite (un par ligne).
    # Alimente par la fusion et le renommage : sans eux, la capture suivante
    # recreerait le doublon qu'on vient de supprimer.
    ("person", "aliases", "TEXT"),
    # Ordre impose a la main dans le plan d'un jour. Une tache epinglee passe
    # avant le classement automatique, dans l'ordre choisi ; l'epingle d'un
    # jour passe est simplement ignoree.
    ("task", "pinned_day", "TEXT"),
    ("task", "pinned_order", "INTEGER"),
    # Avancement declare (0 a 100) et origine d'une tache : 'claude' pour une
    # tache ouverte par une session Claude Code, NULL pour le reste.
    ("task", "progress_pct", "INTEGER NOT NULL DEFAULT 0"),
    ("task", "source", "TEXT"),
    # Journal de travail (voir models/journal.py) : la table note existante
    # gagne un projet, un type, une duree et une source.
    ("note", "project_id", "INTEGER REFERENCES project(id) ON DELETE SET NULL"),
    ("note", "kind", "TEXT NOT NULL DEFAULT 'note'"),
    ("note", "minutes", "INTEGER"),
    ("note", "source", "TEXT"),
    # Origine d'une session de temps : NULL pour le chronometre, 'claude' pour
    # un temps declare par une session Claude.
    ("session", "source", "TEXT"),
    # Conversations separees : chaque message et chaque plan appartient a une
    # conversation. Les messages anterieurs sont rattaches a une conversation
    # « d'origine » par _migrate, sans rien perdre.
    ("chat_message", "conversation_id", "INTEGER REFERENCES conversation(id) ON DELETE CASCADE"),
    ("chat_plan", "conversation_id", "INTEGER REFERENCES conversation(id) ON DELETE CASCADE"),
    # Pilotage des projets (voir models/phase.py) : une tache se range dans une
    # etape du projet, peut etre bloquee et attendre un camp ('us', 'client',
    # 'both' ; NULL vaut 'us'). Le projet porte sa meteo et sa date de bascule.
    ("task", "phase_id", "INTEGER REFERENCES project_phase(id) ON DELETE SET NULL"),
    ("task", "blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("task", "side", "TEXT"),
    ("project", "weather", "TEXT"),
    ("project", "weather_note", "TEXT"),
    ("project", "go_live", "TEXT"),
    # Nature d'une tache : 'task', 'anomaly' (bug remonte par le client, avec
    # sa gravite et son module) ou 'deliverable' (livrable attendu du client).
    # Une anomalie ou un livrable reste une tache : meme statut derive, meme
    # etape, memes outils pour les sessions Claude.
    ("task", "kind", "TEXT NOT NULL DEFAULT 'task'"),
    ("task", "severity", "TEXT"),
    ("task", "module", "TEXT"),
    # Chef de projet a qui part le rapport d'avancement (voir
    # services/progress_report.py), et lien du tableau de bord qu'on lui partage.
    ("project", "manager_id", "INTEGER REFERENCES person(id) ON DELETE SET NULL"),
    ("person", "report_url", "TEXT"),
    # Identifiant de la tache ou du projet dans la base Odoo dont il est
    # importe (voir services/odoo_sync.py). NULL pour ce qui est cree ici.
    # project.odoo_id n'est plus lu : remplace par project_odoo_link, qui
    # admet plusieurs projets Odoo par projet. Conserve pour la reprise.
    ("task", "odoo_id", "INTEGER"),
    ("project", "odoo_id", "INTEGER"),
    # Anomalie : cause trouvee et solution appliquee (une anomalie resolue
    # avec une solution produit sa note dans la base de connaissances).
    ("task", "cause", "TEXT"),
    ("task", "solution", "TEXT"),
    # Nombre de fois ou l'echeance a ete repoussee : signal d'une tache qui traine.
    ("task", "deadline_shifts", "INTEGER NOT NULL DEFAULT 0"),
    # Contexte Odoo du projet : version et client (filtres de la base de connaissances).
    ("project", "odoo_version", "TEXT"),
    ("project", "client", "TEXT"),
    # Sources cliquables d'une reponse du chat (voir services/chat_sources.py).
    ("chat_message", "sources_json", "TEXT"),
    # Espace de travail : 'pro' ou 'perso'. Une tache rattachee a un projet
    # prend l'espace du projet (models/task.py) ; une tache sans projet garde
    # le sien. Chaque cle des sessions Claude est liee a un espace et ne voit
    # que lui (controllers/security.py, services/agent.py). Tout l'existant
    # bascule en 'pro'.
    ("project", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("task", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    # Separation complete des espaces (models/space.py) : chaque donnee
    # metier porte son espace. Contacts et notes techniques admettent
    # 'commun' (visibles des deux espaces). Un bilan enregistre garde le
    # perimetre sur lequel il a ete calcule ('all' = les deux espaces).
    # La reprise des donnees existantes est faite par SPACE_BACKFILL.
    ("capture", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("conversation", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("event", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("calendar_source", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("note", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("minutes", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("waiting", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("person", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("kb_note", "space", "TEXT NOT NULL DEFAULT 'pro'"),
    ("review", "space", "TEXT NOT NULL DEFAULT 'all'"),
    # Charge planifiee par espace (anneau du logo sur un jour passe). NULL pour
    # les jours notes avant : seul le total est alors connu.
    ("plan_day", "planned_pro_min", "INTEGER"),
    ("plan_day", "planned_perso_min", "INTEGER"),
)

# Reprise des donnees au moment ou la colonne space apparait (une seule fois :
# un espace modifie ensuite a la main n'est jamais recalcule). Principe : une
# donnee suit son projet ou sa tache ; sans attache, elle reste en 'pro', sauf
# contacts et notes techniques, jusqu'ici partages, qui passent en 'commun'
# pour rester visibles des deux cotes.
SPACE_BACKFILL = {
    "capture": (
        # Une capture dont toutes les taches sont perso est perso.
        "UPDATE capture SET space = 'perso' WHERE EXISTS"
        " (SELECT 1 FROM task WHERE task.capture_id = capture.id)"
        " AND NOT EXISTS (SELECT 1 FROM task WHERE task.capture_id = capture.id"
        " AND task.space <> 'perso')",
    ),
    "event": (
        "UPDATE event SET space = (SELECT space FROM project WHERE project.id = event.project_id)"
        " WHERE project_id IN (SELECT id FROM project)",
    ),
    "note": (
        "UPDATE note SET space = COALESCE("
        " (SELECT space FROM task WHERE task.id = note.task_id),"
        " (SELECT space FROM project WHERE project.id = note.project_id), 'pro')",
    ),
    "minutes": (
        "UPDATE minutes SET space = COALESCE("
        " (SELECT space FROM project WHERE project.id = minutes.project_id),"
        " (SELECT p.space FROM event e JOIN project p ON p.id = e.project_id"
        "  WHERE e.id = minutes.event_id), 'pro')",
    ),
    "person": (
        # Projets tous dans un meme espace : cet espace ; sinon (aucun projet,
        # ou projets des deux espaces) : commun.
        "UPDATE person SET space = COALESCE((SELECT MIN(p.space) FROM person_project pp"
        " JOIN project p ON p.id = pp.project_id WHERE pp.person_id = person.id"
        " HAVING COUNT(DISTINCT p.space) = 1), 'commun')",
    ),
    "waiting": (
        "UPDATE waiting SET space = COALESCE("
        " (SELECT space FROM task WHERE task.id = waiting.task_id),"
        " (SELECT NULLIF(space, 'commun') FROM person WHERE person.id = waiting.person_id),"
        " 'pro')",
    ),
    "kb_note": (
        "UPDATE kb_note SET space = COALESCE("
        " (SELECT space FROM project WHERE project.id = kb_note.project_id),"
        " (SELECT space FROM task WHERE task.id = kb_note.source_task_id), 'commun')",
    ),
}

# Ordre de reprise : les contacts avant les attentes, qui en dependent.
SPACE_BACKFILL_ORDER = ("capture", "event", "note", "minutes", "person", "waiting", "kb_note")


def _migrate(conn):
    """Ajoute les colonnes manquantes sur une base creee par une version anterieure."""
    nouvelles = set()
    for table, column, definition in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            nouvelles.add((table, column))

    for table in SPACE_BACKFILL_ORDER:
        if (table, "space") in nouvelles:
            for sql in SPACE_BACKFILL[table]:
                conn.execute(sql)

    # Index sur des colonnes ajoutees par migration : ils ne peuvent pas vivre
    # dans SCHEMA, execute avant que la colonne existe sur une base ancienne.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_source ON event(source_id, starts_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_note_task ON note(task_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_note_project ON note(project_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_phase ON task(phase_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_space ON task(space, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_capture_space ON capture(space, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_space ON event(space, starts_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_person_space ON person(space)")
    # Unicite de l'import Odoo : une tache Odoo n'existe qu'une fois ici.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_odoo ON task(odoo_id) WHERE odoo_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_odoo ON project(odoo_id)"
        " WHERE odoo_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_odoo_link ON project_odoo_link(project_id)"
    )
    # Reprise des liens portes par project.odoo_id, vide ensuite : la colonne
    # ne doit plus recreer un lien deplace par une fusion.
    conn.execute(
        "INSERT OR IGNORE INTO project_odoo_link (odoo_id, project_id)"
        " SELECT odoo_id, id FROM project WHERE odoo_id IS NOT NULL"
    )
    conn.execute("UPDATE project SET odoo_id = NULL WHERE odoo_id IS NOT NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_conversation ON chat_message(conversation_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_plan_conversation ON chat_plan(conversation_id, status)"
    )
    # Chaque re-triage d'une capture creait une proposition sans clore les
    # precedentes : des propositions restaient 'pending' sur des captures
    # closes et gonflaient les compteurs. Le modele les rejette desormais
    # (models/capture.py) ; ce nettoyage rattrape les donnees existantes.
    conn.execute(
        "UPDATE proposal SET status = 'rejected' WHERE status = 'pending'"
        " AND capture_id IN (SELECT id FROM capture WHERE status = 'done')"
    )
    # Le projet principal d'un interlocuteur figure toujours parmi ses projets :
    # reprend les rattachements anterieurs a la table person_project, sans doublon.
    conn.execute(
        "INSERT OR IGNORE INTO person_project (person_id, project_id)"
        " SELECT id, project_id FROM person WHERE project_id IS NOT NULL"
    )
    _attach_orphan_chat(conn)


def _attach_orphan_chat(conn):
    """Rattache l'historique du fil unique d'avant a une conversation d'origine.

    Ne fait rien une fois fait : seuls des messages sans conversation (bases
    anterieures aux conversations separees) declenchent la creation.
    """
    orphelins = conn.execute(
        "SELECT COUNT(*) FROM chat_message WHERE conversation_id IS NULL"
    ).fetchone()[0]
    if not orphelins:
        return
    dernier = conn.execute("SELECT MAX(created_at) FROM chat_message").fetchone()[0]
    curseur = conn.execute(
        "INSERT INTO conversation (title, updated_at) VALUES (?, ?)",
        ("Conversation d'origine", dernier or datetime.now().replace(microsecond=0).isoformat(sep=" ")),
    )
    conn.execute(
        "UPDATE chat_message SET conversation_id = ? WHERE conversation_id IS NULL",
        (curseur.lastrowid,),
    )
    conn.execute(
        "UPDATE chat_plan SET conversation_id = ? WHERE conversation_id IS NULL",
        (curseur.lastrowid,),
    )


# Attente maximale sur une base verrouillee, en millisecondes. Trois ecrivains
# se partagent le fichier : les threads waitress, le worker de triage et le
# sondeur Telegram. Sans ce delai, une ecriture concurrente leve aussitot
# "database is locked" au lieu de patienter le temps de la transaction voisine.
BUSY_TIMEOUT_MS = 5000


def _connect(db_path=None):
    """Ouvre une connexion SQLite configuree pour un acces concurrent.

    WAL autorise les lectures pendant une ecriture : sans lui, le worker de
    triage bloque l'interface a chaque tache creee. Le mode est une propriete
    du fichier, pose une fois et conservee ensuite.

    :param db_path: base a ouvrir (defaut : la base de l'application)
    :return: connexion sqlite3 avec row_factory et cles etrangeres actives
    """
    conn = sqlite3.connect(db_path or config.DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL plutot que FULL : en WAL, la base reste coherente apres une coupure
    # de courant, seule la toute derniere transaction peut etre perdue.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """Retourne la connexion SQLite de la requete courante, ouverte si besoin."""
    if "db" not in g:
        g.db = _connect()
    return g.db


def close_db(exception=None):
    """Ferme la connexion en fin de requete (branchee sur teardown_appcontext)."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema():
    """Cree les tables et index manquants. Idempotent."""
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        # Import local : models.search lit get_db dans ce module.
        from models.revision import ensure_triggers
        from models.search import ensure_index

        ensure_index(conn)
        ensure_triggers(conn)
        conn.commit()
    finally:
        conn.close()


# Les secrets sont effaces des sauvegardes : une copie de la base ne doit
# transporter aucun secret hors du poste, meme chiffre.
SECRET_SETTINGS = (
    "telegram_token",
    "access_key",
    "anthropic_api_key",
    "gemini_api_key",
    "agent_key",
    "agent_key_perso",
    "pairing_code",
    "odoo_api_key",
)


def _strip_secrets(db_path):
    """Efface les secrets d'une copie de la base.

    La copie repasse en journal classique : sans cela, l'effacement laisserait
    un fichier -wal a cote de la sauvegarde, et la restaurer sans lui rendrait
    les secrets effaces.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.executemany(
            "UPDATE setting SET value = '' WHERE key = ?",
            [(key,) for key in SECRET_SETTINGS],
        )
        conn.execute("UPDATE calendar_source SET url = ''")
        conn.commit()
    finally:
        conn.close()


def vacuum():
    """Reecrit la base pour purger les pages liberees.

    Indispensable apres avoir chiffre un secret : SQLite laisse l'ancienne
    valeur en clair dans les pages liberees tant que le fichier n'est pas
    reecrit, et une simple recherche binaire la retrouverait.
    """
    conn = _connect()
    try:
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def backup_db():
    """Copie la base dans public/backups puis purge les sauvegardes excedentaires.

    La copie passe par l'API backup() de SQLite et non par une copie de
    fichier : en WAL, les dernieres transactions vivent dans le fichier -wal,
    qu'une copie brute laisserait de cote. Le resultat serait une sauvegarde
    amputee, voire illisible si l'ecriture tombait au milieu.

    Le jeton Telegram est retire de la copie : restaurer une sauvegarde impose
    donc de saisir a nouveau le jeton, ce qui est le prix d'une copie sans secret.
    """
    if not config.DB_PATH.exists():
        return None

    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = config.BACKUP_DIR / f"nomaia_{stamp}.db"

    source = _connect()
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    _strip_secrets(target)

    backups = sorted(config.BACKUP_DIR.glob("nomaia_*.db"), reverse=True)
    for old in backups[config.BACKUP_KEEP:]:
        old.unlink(missing_ok=True)

    return target
