"""Revision des donnees : compteur unique incremente a chaque ecriture affichable.

Les pages ouvertes relisent ce compteur (/api/revision) et se rechargent
quand il a bouge. Il est tenu par des triggers SQL, donc incremente quel que
soit l'ecrivain : interface, session Claude, Telegram, triage, battement de
coeur, synchronisations Odoo et Outlook.

Seules les tables dont le contenu s'affiche sont suivies. Restent hors suivi :
- setting : reglages, mais aussi horodatages techniques ecrits par le
  battement de coeur a chaque tour ; les pages se rechargeraient sans cesse ;
- usage : une ligne par appel au modele, lue seulement par Config ;
- search_fts, search_meta : index derive, deja nourri par les tables suivies ;
- alert_log, telegram_link, mail_seen : memoire technique des alertes, du bot
  et du lecteur de mails ; ce qu'ils produisent passe par des tables suivies ;
- message : dialogue de triage, affiche par la seule boite de reception, qui
  a son propre sondage ;
- calendar_source : son horodatage change a chaque synchronisation, meme sans
  nouveaute ; les evenements importes sont, eux, suivis ;
- project_odoo_link : lien d'import jamais affiche, ecrit avec ses taches.

Ajouter une table : la declarer dans TRACKED_TABLES. En retirer une : ses
triggers sont supprimes au demarrage suivant.
"""
from models import get_db

TRACKED_TABLES = (
    "project",
    "project_phase",
    "task",
    "session",
    "note",
    "capture",
    "proposal",
    "review",
    "person",
    "person_project",
    "waiting",
    "event",
    "event_exception",
    "day_off",
    "day_note",
    "conversation",
    "chat_message",
    "chat_plan",
    "minutes",
    "minutes_item",
    "kb_note",
    "attachment",
)

TRIGGER_PREFIX = "revision_"
BUMP = "UPDATE data_revision SET rev = rev + 1 WHERE id = 1;"


def ensure_triggers(conn):
    """Cree les triggers de revision manquants et retire ceux des tables plus suivies.

    Idempotente : appelee par init_schema a chaque demarrage.
    """
    existing = conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger' AND name GLOB ?",
        (TRIGGER_PREFIX + "*",),
    ).fetchall()
    for name, table in existing:
        if table not in TRACKED_TABLES:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")

    for table in TRACKED_TABLES:
        for suffix, event in (("ai", "INSERT"), ("au", "UPDATE"), ("ad", "DELETE")):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {TRIGGER_PREFIX}{table}_{suffix}"
                f" AFTER {event} ON {table} BEGIN {BUMP} END"
            )


def current():
    """Revision courante des donnees (0 sur une base neuve)."""
    row = get_db().execute("SELECT rev FROM data_revision WHERE id = 1").fetchone()
    return row["rev"] if row else 0
