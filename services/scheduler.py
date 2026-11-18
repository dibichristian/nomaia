"""Moteur de priorisation : attribue un score a chaque tache ouverte.

Le score sert a ordonner la journee. Il reste volontairement lisible : chaque
composante est explicable a l'utilisateur dans l'interface.
"""
from datetime import date, datetime

from services import workdays

# Poids de la priorite declaree (P1 = 120, P2 = 90, P3 = 60, P4 = 30).
PRIORITY_WEIGHT = 30

# Points d'urgence selon le nombre de jours restants avant l'echeance.
URGENCY_BY_DELAY = {
    0: 100,   # aujourd'hui
    1: 70,    # demain
    2: 45,
    3: 30,
    4: 20,
    5: 15,
}
URGENCY_LATE_BASE = 130      # deja en retard
URGENCY_LATE_PER_DAY = 12    # aggravation par jour de retard
URGENCY_LATE_MAX = 200
URGENCY_NONE = 5             # tache sans echeance

# Une tache courte passe devant a score egal : elle libere l'esprit vite.
QUICK_WIN_MAX_MIN = 30
QUICK_WIN_BONUS = 12

# Une tache issue de l'IA non relue passe apres : elle merite une verification.
REVIEW_PENALTY = 15

# Preference horaire selon l'energie demandee par la tache.
ENERGY_BONUS = {
    "morning": {"high": 25, "med": 0, "low": -15},
    "afternoon": {"high": -15, "med": 0, "low": 10},
}


def _days_until(deadline, today):
    """Retourne le delai avant une echeance ISO, en jours ouvres.

    Un week-end ou un conge ne laisse aucun temps pour avancer : une echeance
    lundi vue vendredi est due au prochain jour travaille, pas dans trois jours.
    """
    try:
        target = datetime.strptime(deadline, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return workdays.working_days_until(target, today)


def urgency(task, today=None):
    """Calcule la composante d'urgence liee a l'echeance."""
    today = today or date.today()
    delay = _days_until(task.get("deadline"), today)

    if delay is None:
        return URGENCY_NONE
    if delay < 0:
        return min(URGENCY_LATE_BASE + abs(delay) * URGENCY_LATE_PER_DAY, URGENCY_LATE_MAX)
    return URGENCY_BY_DELAY.get(delay, max(10 - delay, 0))


def score(task, today=None):
    """Note une tache : plus le score est haut, plus elle passe tot.

    :param task: dictionnaire de tache (format models.task)
    :param today: date de reference, pour les tests
    :return: score entier
    """
    total = (5 - int(task.get("priority") or 3)) * PRIORITY_WEIGHT
    total += urgency(task, today)

    if int(task.get("estimate_min") or 0) <= QUICK_WIN_MAX_MIN:
        total += QUICK_WIN_BONUS

    if task.get("needs_review"):
        total -= REVIEW_PENALTY

    return total


def explain(task, today=None):
    """Detaille le score d'une tache, pour affichage dans l'interface."""
    return {
        "priorite": (5 - int(task.get("priority") or 3)) * PRIORITY_WEIGHT,
        "urgence": urgency(task, today),
        "tache_courte": QUICK_WIN_BONUS
        if int(task.get("estimate_min") or 0) <= QUICK_WIN_MAX_MIN
        else 0,
        "a_verifier": -REVIEW_PENALTY if task.get("needs_review") else 0,
        "total": score(task, today),
    }


def rank(tasks, window=None, today=None):
    """Trie des taches par score decroissant.

    :param window: 'morning' ou 'afternoon' pour ponderer selon l'energie
        demandee ; None pour un classement neutre.
    """
    bonuses = ENERGY_BONUS.get(window, {})

    return sorted(
        tasks,
        key=lambda task: (
            -(score(task, today) + bonuses.get(task.get("energy") or "med", 0)),
            task.get("deadline") or "9999-99-99",
            task.get("id") or 0,
        ),
    )
