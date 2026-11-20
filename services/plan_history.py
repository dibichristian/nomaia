"""Historique du plan du jour : heures planifiees et heures suivies, jour par jour.

Le planifie vient de la trace laissee chaque jour par le plan (models.plan_day) ;
le suivi vient des sessions chronometrees ou declarees. Un jour sans trace de
plan reste sans planifie : on ne reconstitue pas un chiffre qu'on n'a pas vu.
"""
from datetime import date, timedelta

from models import get_db
from models import plan_day as plan_day_model
from models import session as session_model
from services import planner, workdays

# Au-dela, une periode libre est ramenee a ses derniers jours (un an).
MAX_DAYS = 366


def tracked_by_day(start_day, end_day):
    """Minutes suivies par jour sur une periode, en une seule requete.

    Une session est rattachee au jour de son debut. La session en cours
    compte pour aujourd'hui si aujourd'hui est dans la periode.

    :param start_day: date de debut (incluse)
    :param end_day: date de fin (incluse)
    :return: {jour ISO: minutes}
    """
    rows = get_db().execute(
        "SELECT date(started_at) AS day, COALESCE(SUM(duration_min), 0) AS minutes"
        " FROM session WHERE ended_at IS NOT NULL AND date(started_at) BETWEEN ? AND ?"
        " GROUP BY date(started_at)",
        (start_day.isoformat(), end_day.isoformat()),
    ).fetchall()
    result = {row["day"]: int(row["minutes"]) for row in rows}

    today = date.today()
    running = session_model.running()
    if running and start_day <= today <= end_day and running["started_at"][:10] == today.isoformat():
        key = today.isoformat()
        result[key] = result.get(key, 0) + int(running["elapsed_min"])
    return result


def tracked_on(day):
    """Minutes suivies un jour donne, session en cours comprise si c'est aujourd'hui."""
    return tracked_by_day(day, day).get(day.isoformat(), 0)


def record_today(plan, done_tasks):
    """Note la charge planifiee du jour a partir du plan calcule.

    La charge = plan restant + duree reservee des taches bouclees ce jour-la :
    cocher une tache la fait sortir du plan sans faire baisser la charge.
    Un jour de repos sans travail fait n'est pas note.

    :param plan: retour de planner.build_plan(aujourd'hui)
    :param done_tasks: taches bouclees aujourd'hui (planner.done_today)
    """
    done_min = sum(planner.padded_duration(task) for task in done_tasks)
    if plan.get("day_off") and not done_min:
        return
    # Meme calcul par espace : l'anneau du logo relit un jour passe espace par espace.
    planned, done = planner.planned_by_space(plan), planner.minutes_by_space(done_tasks)
    by_space = {space: planned[space] + done[space] for space in planned}
    plan_day_model.record(plan["date"], plan["planned_min"] + done_min, plan["capacity_min"], by_space)


def history(start, end, today=None):
    """Heures planifiees et suivies d'une periode, jour par jour, dans l'ordre.

    Les jours a venir sont ecartes (rien de fait, rien de note). Une periode
    plus longue que MAX_DAYS est ramenee a ses MAX_DAYS derniers jours.
    Les jours non ouvres n'apparaissent que s'ils portent du planifie ou du suivi.

    :param start: premier jour de la periode (date)
    :param end: dernier jour de la periode (date)
    :param today: date de reference (defaut : aujourd'hui)
    :return: {start, end, truncated, days: [{date, planned_min, tracked_min,
        capacity_min, working}], totals: {planned_min, tracked_min,
        tracked_on_planned_min, planned_days, days}}
    """
    today = today or date.today()
    end = min(end, today)
    truncated = (end - start).days + 1 > MAX_DAYS
    if truncated:
        start = end - timedelta(days=MAX_DAYS - 1)

    planned = plan_day_model.between(start.isoformat(), end.isoformat())
    tracked = tracked_by_day(start, end) if start <= end else {}

    rows = []
    for offset in range(max((end - start).days + 1, 0)):
        day = start + timedelta(days=offset)
        key = day.isoformat()
        working = workdays.is_working_day(day)
        record = planned.get(key)
        tracked_min = tracked.get(key, 0)
        if not working and record is None and not tracked_min:
            continue
        rows.append({
            "date": key,
            "planned_min": record["planned_min"] if record else None,
            # Sans trace, la capacite se recalcule (horaires et agenda actuels).
            "capacity_min": record["capacity_min"] if record else planner.free_minutes(day),
            "tracked_min": tracked_min,
            "working": working,
        })

    with_plan = [row for row in rows if row["planned_min"] is not None]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "truncated": truncated,
        "days": rows,
        "totals": {
            "planned_min": sum(row["planned_min"] for row in with_plan),
            "tracked_min": sum(row["tracked_min"] for row in rows),
            # Suivi des seuls jours planifies : base honnete du ratio suivi / planifie.
            "tracked_on_planned_min": sum(row["tracked_min"] for row in with_plan),
            "planned_days": len(with_plan),
            "days": len(rows),
        },
    }
