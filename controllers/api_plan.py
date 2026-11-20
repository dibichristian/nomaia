"""API JSON du plan du jour et des reglages d'horaires."""
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from models import plan_day as plan_day_model
from models import setting
from models import space as space_model
from models import task as task_model
from models.task import ValidationError
from services import day_close, foresight, notifications, plan_history, plan_kpis, plan_side, planner, scheduler
from services.day_close import DayCloseError

api_plan_bp = Blueprint("api_plan", __name__, url_prefix="/api")

# Reglages modifiables depuis l'interface du plan.
PLAN_SETTINGS = (
    "work_start",
    "work_end",
    "lunch_start",
    "lunch_end",
    "plan_buffer_pct",
    "plan_horizon_days",
)


@api_plan_bp.get("/plan")
def get_plan():
    """Retourne le plan d'une journee (par defaut aujourd'hui)."""
    raw_day = request.args.get("date")
    if raw_day:
        try:
            day = datetime.strptime(raw_day, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Date attendue au format AAAA-MM-JJ."}), 400
    else:
        day = date.today()

    # Jour ecoule : on ne planifie pas le passe, on montre ce qui a ete fait
    # et ce qu'on a pu oublier de cocher.
    if day < date.today():
        review = planner.day_review(day)
        review["settings"] = {key: setting.get(key) for key in PLAN_SETTINGS}
        review.update(_real_load(day))
        return jsonify(_in_scope(review))

    plan = planner.build_plan(day)
    plan["settings"] = {key: setting.get(key) for key in PLAN_SETTINGS}
    # Les echeances en danger ne valent que vues d'aujourd'hui : le temps
    # libre se compte a partir de maintenant.
    plan["risks"] = (
        foresight.deadlines_at_risk()
        if day == date.today() and setting.get("foresight_enabled") == "1"
        else []
    )
    # Travail deja fait, affiche sur demande dans le fil : aujourd'hui seulement
    # (le passe a sa propre vue, le futur n'a rien de fait).
    if day == date.today():
        plan["done_today"] = planner.done_today(day)
        # Trace du planifie : sans elle, l'historique perdrait la charge du jour.
        plan_history.record_today(plan, plan["done_today"])
        plan["tracked_day_min"] = plan_history.tracked_on(day)
    return jsonify(_in_scope(plan))



@api_plan_bp.get("/day/progress")
def get_day_progress():
    """Avancement d'un jour (?date=, defaut aujourd'hui) dans l'espace affiche : l'anneau du logo."""
    raw_day = request.args.get("date")
    try:
        day = datetime.strptime(raw_day, "%Y-%m-%d").date() if raw_day else None
    except ValueError:
        return jsonify({"error": "Date attendue au format AAAA-MM-JJ."}), 400
    return jsonify(planner.day_progress(day, space_model.scope()))

def _masked_label(value):
    """Libelle d'un creneau de l'autre espace : le temps est pris, le detail cache."""
    return "Occupé · " + space_model.LABELS.get(value, value)


def _in_scope(plan):
    """Montre un plan calcule sur les deux espaces dans le perimetre choisi.

    Le plan est construit sur tout (le temps est le meme pour les deux espaces) ;
    ici, les creneaux et rendez-vous de l'autre espace gardent leur place mais
    perdent leur detail (`masked`), et les listes de taches (debordement, fait,
    oublie d'un jour passe) ne montrent que l'espace visible. La charge et la
    capacite restent globales : le temps est le meme pour les deux espaces.
    """
    espace = space_model.scope()
    if espace is None:
        return plan
    visible = lambda item: space_model.visible(item.get("space"), espace)  # noqa: E731

    creneaux = []
    for slot in plan.get("slots", []):
        tache = slot["task"]
        if visible(tache):
            creneaux.append(slot)
            continue
        creneaux.append({
            **{cle: slot[cle] for cle in ("start", "end", "duration_min") if cle in slot},
            "masked": True,
            "task": {"id": None, "title": _masked_label(tache.get("space")), "priority": 4,
                     "space": tache.get("space"), "estimate_min": tache.get("estimate_min")},
        })
    plan["slots"] = creneaux
    plan["events"] = [
        item if visible(item) else {
            **{cle: item[cle] for cle in ("starts_at", "ends_at", "kind") if cle in item},
            "id": None, "title": _masked_label(item.get("space")), "space": item.get("space"),
            "masked": True, "location": None, "project_name": None,
        }
        for item in plan.get("events", [])
    ]
    for cle in ("overflow", "done_today", "done", "open"):
        # Listes de taches : plan du jour (debordement, fait) et jour passe (fait, oublie).
        if cle in plan:
            plan[cle] = [tache for tache in plan[cle] if visible(tache)]
    return plan


def _real_load(day):
    """Temps reel d'un jour passe et capacite de reference pour la barre de charge.

    La capacite vient de la trace du plan si elle existe, sinon elle est
    recalculee avec les horaires et l'agenda actuels.
    """
    record = plan_day_model.between(day.isoformat(), day.isoformat()).get(day.isoformat())
    return {
        "tracked_day_min": plan_history.tracked_on(day),
        "planned_day_min": record["planned_min"] if record else None,
        "day_capacity_min": record["capacity_min"] if record else planner.free_minutes(day),
    }


@api_plan_bp.errorhandler(ValidationError)
def handle_validation_error(error):
    """Transforme une erreur de validation en reponse HTTP 400."""
    return jsonify({"error": str(error)}), 400


def _plan_day(payload):
    """Jour vise par une requete d'epinglage : celui envoye, sinon aujourd'hui."""
    return str(payload.get("date") or date.today().isoformat())


@api_plan_bp.post("/plan/order")
def set_order():
    """Fixe l'ordre des taches d'un jour (glisser-deposer dans le plan)."""
    payload = request.get_json(silent=True) or {}
    ids = payload.get("task_ids")
    if not isinstance(ids, list):
        return jsonify({"error": "Liste ordonnee attendue (task_ids)."}), 400
    return jsonify({"pinned": task_model.pin_tasks(_plan_day(payload), ids)})


@api_plan_bp.delete("/plan/order")
def reset_order():
    """Rend tout le jour au classement automatique."""
    jour = request.args.get("date") or date.today().isoformat()
    return jsonify({"pinned": task_model.pin_tasks(jour, [])})


@api_plan_bp.post("/plan/pin/<int:task_id>")
def pin_task(task_id):
    """Epingle une tache : en tete (« maintenant ») ou a la fin du jour."""
    payload = request.get_json(silent=True) or {}
    first = payload.get("position", "now") != "end"
    task = task_model.pin_task(task_id, _plan_day(payload), first=first)
    if task is None:
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify(task)


@api_plan_bp.delete("/plan/pin/<int:task_id>")
def unpin_task(task_id):
    """Rend une tache au classement automatique."""
    if not task_model.unpin_task(task_id):
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify({"ok": True})


@api_plan_bp.get("/plan/why/<int:task_id>")
def explain_task(task_id):
    """Detaille le calcul du score d'une tache."""
    from models import task as task_model

    task = task_model.get_task(task_id)
    if task is None:
        return jsonify({"error": "Tache introuvable."}), 404
    return jsonify({"task_id": task_id, "detail": scheduler.explain(task)})


@api_plan_bp.patch("/plan/settings")
def update_settings():
    """Met a jour les horaires de travail et la marge de planification."""
    payload = request.get_json(silent=True) or {}

    for key, value in payload.items():
        if key not in PLAN_SETTINGS:
            return jsonify({"error": f"Reglage inconnu : {key}"}), 400

        if key.endswith(("_start", "_end")):
            try:
                datetime.strptime(str(value), "%H:%M")
            except ValueError:
                return jsonify({"error": f"{key} attend une heure au format HH:MM."}), 400
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} attend un nombre."}), 400
            if number < 0 or number > 100:
                return jsonify({"error": f"{key} doit rester entre 0 et 100."}), 400

        setting.set_value(key, value)

    return jsonify({key: setting.get(key) for key in PLAN_SETTINGS})


@api_plan_bp.get("/day/recap")
def day_recap():
    """Recapitulatif d'une journee (?date=) et note « pour demain » si elle existe."""
    try:
        return jsonify(day_close.recap(request.args.get("date")))
    except DayCloseError as error:
        return jsonify({"error": str(error)}), 400


@api_plan_bp.post("/day/close")
def day_close_route():
    """Cloture la journee : enregistre la note « pour demain »."""
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(day_close.close(data.get("date"), data.get("tomorrow")))
    except DayCloseError as error:
        return jsonify({"error": str(error)}), 400


@api_plan_bp.get("/day/morning")
def day_morning():
    """Note du soir precedent, a relire en debut de journee (ou null)."""
    return jsonify({"note": day_close.morning_note(request.args.get("date"))})


@api_plan_bp.get("/plan/side")
def get_plan_side():
    """Indicateurs du jour et colonne laterale du plan (agenda, signaux projets, boite de reception)."""
    raw_day = request.args.get("date")
    try:
        day = datetime.strptime(raw_day, "%Y-%m-%d").date() if raw_day else None
    except ValueError:
        return jsonify({"error": "Date attendue au format AAAA-MM-JJ."}), 400
    return jsonify(plan_side.side(day))


@api_plan_bp.get("/plan/kpi/<kind>")
def get_plan_kpi(kind):
    """Detail d'un indicateur du plan (tracked, late, client) au jour ?date=."""
    if kind not in plan_kpis.KINDS:
        return jsonify({"error": f"Indicateur inconnu : {kind}."}), 404
    raw_day = request.args.get("date")
    try:
        day = datetime.strptime(raw_day, "%Y-%m-%d").date() if raw_day else date.today()
    except ValueError:
        return jsonify({"error": "Date attendue au format AAAA-MM-JJ."}), 400
    return jsonify({"kind": kind, "day": day.isoformat(), **plan_kpis.detail(kind, day)})


@api_plan_bp.get("/notifications")
def get_notifications():
    """Cloche de l'en-tete : reunions imminentes, echeances du jour, retards, relances, propositions."""
    return jsonify({"items": notifications.current()})
