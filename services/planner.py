"""Plan du jour : place les taches classees dans les creneaux disponibles.

Le plan n'est pas stocke : il est recalcule a chaque affichage a partir des
taches ouvertes. Une tache cochee disparait du plan, le reste remonte.
"""
from datetime import date, datetime, timedelta

from models import event as event_model
from models import plan_day as plan_day_model
from models import session as session_model
from models import setting
from models import space as space_model
from models import task as task_model
from services import scheduler, workdays

# Duree minimale d'un creneau exploitable : en dessous, on ne commence rien.
MIN_SLOT_MIN = 15

# Etiquette des fenetres reservees a l'espace perso (soir, jours non ouvres),
# actives seulement si le reglage perso_hours_enabled vaut '1'. Sans lui,
# toutes les fenetres acceptent les deux espaces, comme avant.
PERSO_WINDOW = "perso"


def _parse_time(value, fallback):
    """Convertit 'HH:MM' en objet time, avec repli sur une valeur sure."""
    try:
        return datetime.strptime(value, "%H:%M").time()
    except (TypeError, ValueError):
        return datetime.strptime(fallback, "%H:%M").time()


def _windows(day):
    """Retourne les fenetres de travail de la journee (matin, apres-midi).

    Un jour non ouvre n'en a aucune : c'est l'unique point de controle, d'ou
    decoulent la capacite nulle du plan et l'absence de trous libres.

    Avec les horaires perso actives, s'ajoute une fenetre etiquetee
    PERSO_WINDOW : le soir des jours ouvres, la journee des jours non ouvres.

    :return: liste de tuples (debut, fin, etiquette)
    """
    if not workdays.is_working_day(day):
        return _perso_windows(day, working=False)

    start = _parse_time(setting.get("work_start"), "08:30")
    end = _parse_time(setting.get("work_end"), "18:00")
    lunch_start = _parse_time(setting.get("lunch_start"), "12:30")
    lunch_end = _parse_time(setting.get("lunch_end"), "13:30")

    morning = (datetime.combine(day, start), datetime.combine(day, lunch_start), "morning")
    afternoon = (datetime.combine(day, lunch_end), datetime.combine(day, end), "afternoon")

    fenetres = [window for window in (morning, afternoon) if window[1] > window[0]]
    return fenetres + _perso_windows(day, working=True)


def perso_hours_enabled():
    """Indique si l'espace perso a ses propres horaires (soir et jours non ouvres)."""
    return setting.get("perso_hours_enabled") == "1"


def _perso_windows(day, working):
    """Fenetre reservee aux taches perso, ou aucune si l'option est coupee."""
    if not perso_hours_enabled():
        return []
    if working:
        debut = _parse_time(setting.get("perso_start"), "19:00")
        fin = _parse_time(setting.get("perso_end"), "22:00")
    else:
        debut = _parse_time(setting.get("perso_offday_start"), "09:00")
        fin = _parse_time(setting.get("perso_offday_end"), "18:00")
    fenetre = (datetime.combine(day, debut), datetime.combine(day, fin), PERSO_WINDOW)
    return [fenetre] if fenetre[1] > fenetre[0] else []


def window_accepts(label, task):
    """Indique si une tache peut etre placee dans une fenetre.

    Horaires perso actives : les fenetres de travail sont reservees au pro, la
    fenetre perso au perso. Sinon, toute fenetre accepte toute tache.
    """
    if not perso_hours_enabled():
        return True
    return (task.get("space") == "perso") == (label == PERSO_WINDOW)


def _free_windows(day):
    """Retourne les fenetres de travail restantes une fois l'agenda retire.

    Une reunion de 14 h a 16 h coupe l'apres-midi en deux fragments, qui
    gardent l'etiquette de leur fenetre mere : le bonus d'energie matin ou
    apres-midi continue donc de s'appliquer. Les fragments trop courts pour
    commencer quoi que ce soit sont ecartes ici plutot que de polluer le
    placement.

    :return: (fenetres libres, minutes retirees par l'agenda)
    """
    fenetres = _windows(day)
    occupe = event_model.busy_intervals(day)

    if not occupe:
        return fenetres, 0

    brut = sum(int((fin - debut).total_seconds() // 60) for debut, fin, _ in fenetres)
    libres = [
        (debut, fin, etiquette)
        for debut, fin, etiquette in event_model.subtract(fenetres, occupe)
        if (fin - debut).total_seconds() // 60 >= MIN_SLOT_MIN
    ]
    net = sum(int((fin - debut).total_seconds() // 60) for debut, fin, _ in libres)

    # Le temps perdu compte les fragments trop courts : ils sont bel et bien
    # indisponibles, les ignorer gonflerait artificiellement la capacite.
    return libres, max(0, brut - net)


def _padded_duration(task, buffer_pct):
    """Ajoute la marge de securite a la duree estimee d'une tache."""
    raw = int(task.get("estimate_min") or 30)
    return max(MIN_SLOT_MIN, round(raw * (1 + buffer_pct / 100)))


def _eligible(tasks, day, horizon_days):
    """Retient les taches a planifier : en retard, du jour, ou proches.

    Les taches sans echeance entrent aussi : ce sont elles qui remplissent
    les creneaux restants une fois les urgences placees.
    """
    limit = (day + timedelta(days=horizon_days)).isoformat()
    retained = []

    # Une tache detaillee en etapes est planifiee par ses etapes : la placer
    # aussi compterait deux fois le meme travail.
    parents = {task.get("parent_id") for task in tasks if task["status"] == "todo"}

    for task in tasks:
        if task["status"] != "todo" or task["id"] in parents:
            continue
        if task["deadline"] and task["deadline"] > limit:
            continue
        retained.append(task)

    return retained


def _blocked_by_sibling(task, pending, placed_ids):
    """Indique qu'une etape anterieure de la meme capture n'est pas encore placee.

    L'IA emet les taches d'une capture dans l'ordre logique (reproduire, puis
    corriger, puis tester) : leur ordre de creation fait donc foi. Sans cette
    regle, le score seul pourrait faire tester un correctif avant de l'ecrire.
    """
    capture_id = task.get("capture_id")
    if not capture_id:
        return False

    return any(
        other["id"] < task["id"]
        and other["id"] not in placed_ids
        and other.get("capture_id") == capture_id
        for other in pending
    )


def free_minutes(day, after=None):
    """Retourne le temps de travail libre d'une journee, agenda deduit.

    :param day: journee examinee
    :param after: instant a partir duquel compter ; le temps deja ecoule de
        la journee en cours n'est plus disponible
    :return: minutes libres, fragments trop courts exclus
    """
    fenetres, _ = _free_windows(day)
    total = 0
    for debut, fin, _etiquette in fenetres:
        if after is not None:
            debut = max(debut, after)
        if fin > debut:
            total += int((fin - debut).total_seconds() // 60)
    return total


def padded_duration(task):
    """Duree a reserver pour une tache : estimation, marge comprise."""
    return _padded_duration(task, setting.get_float("plan_buffer_pct"))


def free_slots(day=None, min_minutes=30):
    """Retourne les trous exploitables d'une journee, agenda deduit.

    Repond a la question « quand suis-je libre ? » sans rien planifier : ce
    sont les fenetres de travail restantes, pas les creneaux du plan.

    :param day: journee a examiner (defaut : aujourd'hui)
    :param min_minutes: duree en dessous de laquelle un trou est ignore
    :return: liste de {'start', 'end', 'duration_min', 'window'}
    """
    day = day or date.today()
    fenetres, _ = _free_windows(day)

    creneaux = []
    for debut, fin, etiquette in fenetres:
        duree = int((fin - debut).total_seconds() // 60)
        if duree < min_minutes:
            continue
        creneaux.append(
            {
                "start": debut.strftime("%H:%M"),
                "end": fin.strftime("%H:%M"),
                "duration_min": duree,
                "window": etiquette,
            }
        )

    return creneaux


def timeline(plan):
    """Fusionne creneaux de travail et evenements en un seul fil horaire.

    L'interface et le point du matin doivent montrer la journee telle qu'elle
    se deroule : une reunion entre deux taches, pas deux listes separees.

    :param plan: retour de build_plan
    :return: liste d'entrees {'kind': 'task'|'event', 'start', 'end', ...} triees
    """
    entrees = []

    for creneau in plan.get("slots", []):
        entrees.append({**creneau, "kind": "task"})

    for evenement in plan.get("events", []):
        entrees.append(
            {
                "kind": "event",
                "start": evenement["starts_at"][11:16],
                "end": evenement["ends_at"][11:16],
                "event": evenement,
            }
        )

    return sorted(entrees, key=lambda entree: entree["start"])


def build_plan(day=None):
    """Construit le plan d'une journee.

    :param day: date a planifier (defaut : aujourd'hui)
    :return: dictionnaire {date, slots, overflow, capacity_min, planned_min, load_pct}
    """
    day = day or date.today()
    buffer_pct = setting.get_float("plan_buffer_pct")
    horizon = setting.get_int("plan_horizon_days")

    # Tous les espaces : le temps est le meme. L'affichage filtre ensuite
    # (controllers/api_plan.py) en grisant les creneaux de l'autre espace.
    open_tasks = task_model.list_tasks(status="todo", space=space_model.ALL)
    pending = _eligible(open_tasks, day, horizon)
    placed_ids = set()
    slots = []
    capacity_min = 0

    # La capacite du jour est ce qui reste apres l'agenda. Sans cette
    # soustraction, NOMAIA promettait des heures deja prises et annoncait une
    # charge fausse, qui contaminait l'alerte de surcharge et le point du matin.
    free_windows, busy_min = _free_windows(day)
    events = event_model.list_for_day(day, space_model.ALL)

    # Les taches epinglees passent d'abord, dans l'ordre choisi : c'est le
    # choix de l'utilisateur, il prime sur le score, l'horizon et l'ordre des
    # etapes d'une capture.
    cursors = [window_start for window_start, _end, _label in free_windows]
    pinned = _pinned(open_tasks, day)
    for task in pinned:
        duration = _padded_duration(task, buffer_pct)
        for index, (_start, window_end, label) in enumerate(free_windows):
            if not window_accepts(label, task):
                continue
            if int((window_end - cursors[index]).total_seconds() // 60) < duration:
                continue
            slots.append(_slot(task, cursors[index], duration, label, day, pinned=True))
            placed_ids.add(task["id"])
            cursors[index] += timedelta(minutes=duration)
            break

    pending = pending + [
        task for task in pinned if task["id"] not in {item["id"] for item in pending}
    ]

    for index, (window_start, window_end, label) in enumerate(free_windows):
        cursor = cursors[index]
        capacity_min += int((window_end - window_start).total_seconds() // 60)

        # Une tache est placee a la fois : liberer une etape peut debloquer la
        # suivante de la meme capture, donc le classement est refait a chaque tour.
        while True:
            remaining = int((window_end - cursor).total_seconds() // 60)
            if remaining < MIN_SLOT_MIN:
                break

            available = [
                task
                for task in pending
                if task["id"] not in placed_ids
                and window_accepts(label, task)
                and not _blocked_by_sibling(task, pending, placed_ids)
            ]

            chosen = None
            # La fenetre perso n'a pas de bonus d'energie : classement neutre.
            rang = None if label == PERSO_WINDOW else label
            for task in scheduler.rank(available, rang, day):
                if _padded_duration(task, buffer_pct) <= remaining:
                    chosen = task
                    break

            if chosen is None:
                break

            duration = _padded_duration(chosen, buffer_pct)
            slots.append(_slot(chosen, cursor, duration, label, day))
            placed_ids.add(chosen["id"])
            cursor += timedelta(minutes=duration)

    slots.sort(key=lambda slot: slot["start"])
    overflow = scheduler.rank(
        [task for task in pending if task["id"] not in placed_ids], None, day
    )
    planned_min = sum(slot["duration_min"] for slot in slots)

    return {
        "date": day.isoformat(),
        "slots": slots,
        "overflow": overflow,
        "capacity_min": capacity_min,
        "planned_min": planned_min,
        "load_pct": round(planned_min / capacity_min * 100) if capacity_min else 0,
        "buffer_pct": buffer_pct,
        "events": events,
        "busy_min": busy_min,
        "day_off": workdays.day_off_reason(day),
        "pinned_count": len(pinned),
    }


def _pinned(open_tasks, day):
    """Taches ouvertes epinglees sur ce jour, dans l'ordre choisi.

    Une tache decoupee en etapes ouvertes cede sa place a ses etapes, dans
    leur ordre : la placer en bloc compterait deux fois le meme travail.
    """
    jour = day.isoformat()
    etapes = {}
    for task in open_tasks:
        if task.get("parent_id"):
            etapes.setdefault(task["parent_id"], []).append(task)

    epinglees = sorted(
        (task for task in open_tasks if task.get("pinned_day") == jour),
        key=lambda task: (task.get("pinned_order") or 0, task["id"]),
    )
    ordre, vus = [], set()
    for task in epinglees:
        remplacement = sorted(
            etapes.get(task["id"], []), key=lambda etape: (etape.get("position") or 0, etape["id"])
        ) or [task]
        for item in remplacement:
            if item["id"] not in vus:
                vus.add(item["id"])
                ordre.append(item)
    return ordre


def _slot(task, start, duration, label, day, pinned=False):
    """Construit un creneau du plan pour une tache placee a partir de start."""
    end = start + timedelta(minutes=duration)
    return {
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "duration_min": duration,
        "window": label,
        "task": task,
        "score": scheduler.score(task, day),
        "energy_bonus": 0 if pinned else scheduler.ENERGY_BONUS.get(label, {}).get(
            task.get("energy") or "med", 0
        ),
        "pinned": pinned,
    }


def day_review(day):
    """Vue d'un jour passe : ce qui a ete boucle, et ce qu'on a pu oublier de cocher.

    Planifier un jour ecoule n'a pas de sens : le plan recalcule y placerait
    les taches ouvertes d'aujourd'hui.

    :return: {date, past, done, open, events, done_min}
    """
    jour = day.isoformat()
    done = task_model.list_done_on(jour)
    return {
        "date": jour,
        "past": True,
        "done": done,
        "open": task_model.list_open_on(jour),
        "events": event_model.list_for_day(day, space_model.ALL),
        "done_min": sum(int(task.get("estimate_min") or 0) for task in done),
        "tracked_min": sum(int(task.get("tracked_min") or 0) for task in done),
        "day_off": workdays.day_off_reason(day),
    }


def _window_of(moment, windows):
    """Periode (matin, apres-midi) a laquelle rattacher un instant de la journee.

    Regle : la premiere fenetre qui n'est pas encore finie a cet instant.
    Avant le debut de journee, c'est donc le matin ; pendant la pause
    dejeuner, l'apres-midi (le travail coche a la reprise s'y lit) ; apres la
    fin de journee, la derniere fenetre. Jour non ouvre : aucune periode.

    :param moment: datetime de l'instant a ranger
    :param windows: retour de _windows(day)
    :return: 'morning', 'afternoon' ou None
    """
    if not windows:
        return None
    for _start, end, label in windows:
        if moment < end:
            return label
    return windows[-1][2]


def done_today(day):
    """Taches terminees ce jour-la, avec l'heure et le temps passe ce jour-la.

    Sert a afficher, sur demande, le travail deja fait dans le fil du plan.
    Le temps suivi de list_done_on couvre toute la vie de la tache : ici on
    ne compte que les sessions terminees du jour, lues en une seule requete.

    :param day: date examinee (le plan ne l'appelle que pour aujourd'hui)
    :return: liste de taches enrichies de 'at' (HH:MM : debut de la premiere
        session du jour, sinon heure de cloture), 'tracked_today_min' et
        'window', triee par heure
    """
    jour = day.isoformat()
    first_start, tracked = {}, {}
    # Sessions deja triees par debut croissant : la premiere vue est la plus tot.
    for item in session_model.list_sessions(jour, jour):
        first_start.setdefault(item["task_id"], item["started_at"])
        tracked[item["task_id"]] = tracked.get(item["task_id"], 0) + int(item["duration_min"] or 0)

    windows = _windows(day)
    result = []
    for task in task_model.list_done_on(jour):
        moment = datetime.fromisoformat(first_start.get(task["id"]) or task["done_at"])
        result.append(
            {
                **task,
                "at": moment.strftime("%H:%M"),
                "tracked_today_min": tracked.get(task["id"], 0),
                "window": _window_of(moment, windows),
            }
        )
    return sorted(result, key=lambda task: task["at"])


def minutes_by_space(tasks):
    """Duree reservee (marge comprise) de taches, repartie par espace.

    :param tasks: taches (dictionnaires portant 'space')
    :return: {'pro': minutes, 'perso': minutes}
    """
    result = dict.fromkeys(space_model.SPACES, 0)
    for task in tasks:
        if task.get("space") in result:
            result[task["space"]] += padded_duration(task)
    return result


def planned_by_space(plan):
    """Minutes planifiees d'un plan (retour de build_plan), reparties par espace.

    :return: {'pro': minutes, 'perso': minutes}
    """
    result = dict.fromkeys(space_model.SPACES, 0)
    for slot in plan.get("slots", []):
        space = (slot.get("task") or {}).get("space")
        if space in result:
            result[space] += slot["duration_min"]
    return result


def day_progress(day=None, space=None):
    """Avancement d'un jour, affiche par l'anneau du logo.

    Minutes des taches terminees ce jour-la, rapportees a ces minutes plus
    celles encore planifiees : les deux sont comptees avec la marge du plan,
    comme la jauge « Planifie » de la page Plan. Un jour passe n'est pas
    replanifie : on le rapporte a la charge notee ce jour-la (models.plan_day).

    :param day: date examinee (defaut : aujourd'hui)
    :param space: 'pro', 'perso', ou None pour les deux espaces
    :return: {date, space, pct (0-100, None si aucun plan n'a ete note ce
        jour passe), done_min, planned_min (minutes encore planifiees),
        by_space (False si le jour passe n'a que son total note : le
        chiffre porte alors sur les deux espaces)}
    """
    today = date.today()
    day = day or today
    done = task_model.list_done_on(day.isoformat())
    result = {"date": day.isoformat(), "space": space, "by_space": True}
    if day < today:
        result.update(_past_progress(day, space, done))
        return result

    plan = build_plan(day)
    if space is None:
        done_min, planned_min = sum(padded_duration(task) for task in done), plan["planned_min"]
    else:
        done_min, planned_min = minutes_by_space(done)[space], planned_by_space(plan)[space]
    total = done_min + planned_min
    result.update(pct=round(done_min / total * 100) if total else 0, done_min=done_min, planned_min=planned_min)
    return result


def _past_progress(day, space, done):
    """Avancement d'un jour passe : fait / charge notee ce jour-la.

    La charge notee comprend deja les taches bouclees au moment de la trace ;
    celles bouclees ensuite (ou hors plan) l'augmentent d'autant, pour ne
    jamais depasser 100 %.

    :return: {pct, done_min, planned_min, by_space}
    """
    key = day.isoformat()
    record = plan_day_model.between(key, key).get(key)
    detail = record.get(f"planned_{space}_min") if record and space else None
    by_space = space is not None and detail is not None
    if by_space:
        done_min, planned = minutes_by_space(done)[space], detail
    else:
        done_min = sum(padded_duration(task) for task in done)
        planned = record["planned_min"] if record else None
    if planned is None:
        return {"pct": None, "done_min": done_min, "planned_min": None, "by_space": by_space or space is None}
    total = max(planned, done_min)
    return {
        "pct": round(done_min / total * 100) if total else 0,
        "done_min": done_min,
        "planned_min": total - done_min,
        "by_space": by_space or space is None,
    }
