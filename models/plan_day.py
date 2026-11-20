"""Charge planifiee enregistree jour par jour (historique du plan du jour).

Le plan est un calcul, pas une donnee : cette table garde, pour chaque jour
ouvert dans le plan, la charge planifiee et la capacite du moment.
"""
from models import get_db


def record(day, planned_min, capacity_min, planned_by_space=None):
    """Enregistre (ou remplace) la charge planifiee d'un jour.

    :param day: date ISO AAAA-MM-JJ
    :param planned_min: minutes planifiees (plan restant + taches bouclees)
    :param capacity_min: minutes disponibles ce jour-la, agenda deduit
    :param planned_by_space: detail de planned_min par espace
        ({'pro': minutes, 'perso': minutes}) ; None si inconnu
    """
    detail = planned_by_space or {}
    db = get_db()
    db.execute(
        "INSERT INTO plan_day (day, planned_min, capacity_min, planned_pro_min, planned_perso_min, updated_at)"
        " VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))"
        " ON CONFLICT(day) DO UPDATE SET planned_min = excluded.planned_min,"
        " capacity_min = excluded.capacity_min, planned_pro_min = excluded.planned_pro_min,"
        " planned_perso_min = excluded.planned_perso_min, updated_at = excluded.updated_at",
        (day, int(planned_min), int(capacity_min), _minutes(detail.get("pro")), _minutes(detail.get("perso"))),
    )
    db.commit()


def _minutes(value):
    """Minutes entieres, ou None si le detail est inconnu."""
    return None if value is None else int(value)


def between(start_day, end_day):
    """Charges planifiees d'une periode (dates ISO incluses), indexees par jour.

    :return: {jour ISO: {planned_min, capacity_min, planned_pro_min,
        planned_perso_min}} ; le detail par espace vaut None pour un jour
        note avant qu'il soit enregistre
    """
    rows = get_db().execute(
        "SELECT day, planned_min, capacity_min, planned_pro_min, planned_perso_min"
        " FROM plan_day WHERE day BETWEEN ? AND ?",
        (start_day, end_day),
    ).fetchall()
    return {
        row["day"]: {
            "planned_min": row["planned_min"],
            "capacity_min": row["capacity_min"],
            "planned_pro_min": row["planned_pro_min"],
            "planned_perso_min": row["planned_perso_min"],
        }
        for row in rows
    }
