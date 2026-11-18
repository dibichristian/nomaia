"""Charge planifiee enregistree jour par jour (historique du plan du jour).

Le plan est un calcul, pas une donnee : cette table garde, pour chaque jour
ouvert dans le plan, la charge planifiee et la capacite du moment.
"""
from models import get_db


def record(day, planned_min, capacity_min):
    """Enregistre (ou remplace) la charge planifiee d'un jour.

    :param day: date ISO AAAA-MM-JJ
    :param planned_min: minutes planifiees (plan restant + taches bouclees)
    :param capacity_min: minutes disponibles ce jour-la, agenda deduit
    """
    db = get_db()
    db.execute(
        "INSERT INTO plan_day (day, planned_min, capacity_min, updated_at)"
        " VALUES (?, ?, ?, datetime('now', 'localtime'))"
        " ON CONFLICT(day) DO UPDATE SET planned_min = excluded.planned_min,"
        " capacity_min = excluded.capacity_min, updated_at = excluded.updated_at",
        (day, int(planned_min), int(capacity_min)),
    )
    db.commit()


def between(start_day, end_day):
    """Charges planifiees d'une periode (dates ISO incluses), indexees par jour.

    :return: {jour ISO: {planned_min, capacity_min}}
    """
    rows = get_db().execute(
        "SELECT day, planned_min, capacity_min FROM plan_day WHERE day BETWEEN ? AND ?",
        (start_day, end_day),
    ).fetchall()
    return {row["day"]: {"planned_min": row["planned_min"], "capacity_min": row["capacity_min"]} for row in rows}
