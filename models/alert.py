"""Memoire des alertes proactives : qui a ete signale, et quand.

Une alerte repetee chaque jour devient un bruit qu'on apprend a ignorer, et
avec elle les alertes qui comptent. Chaque sujet signale porte une cle
(« dormant:task:12 », « urgent:task:7:2026-10-08 ») et la date de son dernier
envoi ; un delai de silence propre a chaque type d'alerte decide du suivant.
"""
from datetime import datetime, timedelta

from models import get_db

STAMP_FORMAT = "%Y-%m-%d %H:%M"


def sent_within(key, days, now=None):
    """Indique si une cle a ete signalee il y a moins de `days` jours.

    :param days: delai de silence ; None signifie « une seule fois, jamais plus »
    """
    row = get_db().execute("SELECT sent_at FROM alert_log WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False
    if days is None:
        return True

    now = now or datetime.now()
    try:
        envoi = datetime.strptime(row["sent_at"], STAMP_FORMAT)
    except ValueError:
        return False
    return now - envoi < timedelta(days=days)


def filter_quiet(items, key_of, days, now=None):
    """Garde les elements dont la cle n'est pas en periode de silence."""
    return [item for item in items if not sent_within(key_of(item), days, now)]


def mark_sent(keys, now=None):
    """Enregistre l'envoi d'un lot de cles."""
    if not keys:
        return
    moment = (now or datetime.now()).strftime(STAMP_FORMAT)
    db = get_db()
    db.executemany(
        "INSERT INTO alert_log (key, sent_at) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET sent_at = excluded.sent_at",
        [(key, moment) for key in keys],
    )
    db.commit()
