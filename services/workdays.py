"""Calendrier de travail : jours ouvres, jours de repos, delais en jours ouvres.

Un jour est ouvre s'il fait partie des jours de la semaine travailles
(reglage work_days) et qu'aucun conge ni ferie ne le couvre.

Hors contexte applicatif (tests unitaires purs du score), tous les jours sont
consideres ouvres : le calcul retombe alors exactement sur les jours
calendaires, comportement anterieur.
"""
from datetime import timedelta

from flask import g, has_app_context

from models import day_off as day_off_model
from models import setting

DEFAULT_WORK_DAYS = frozenset({0, 1, 2, 3, 4})

# Demi-largeur de la fenetre chargee en une requete autour du jour de
# reference. Au-dela, le score d'urgence est sature (plus de points avant dix
# jours, plafond de retard atteint en une semaine) : compter en jours ouvres
# ne changerait rien, on compte donc en jours calendaires.
WINDOW_DAYS = 62

WEEKEND_LABEL = "Week-end"
NON_WORKING_LABEL = "Jour non ouvré"

_WEEKDAYS_KEY = "workdays_weekdays"


def parse_work_days(value):
    """Convertit le reglage '0,1,2,3,4' en ensemble de jours (lundi = 0).

    Une valeur illisible retombe sur lundi-vendredi plutot que de rendre
    toute la semaine chomee.
    """
    try:
        jours = frozenset(int(part) for part in str(value or "").split(",") if part.strip())
    except ValueError:
        return DEFAULT_WORK_DAYS
    jours = frozenset(jour for jour in jours if 0 <= jour <= 6)
    return jours or DEFAULT_WORK_DAYS


def work_weekdays():
    """Retourne les jours de la semaine travailles, lus une fois par requete."""
    if not has_app_context():
        return frozenset(range(7))
    if _WEEKDAYS_KEY not in g:
        g.setdefault(_WEEKDAYS_KEY, parse_work_days(setting.get("work_days")))
    return g.get(_WEEKDAYS_KEY)


def _window(reference):
    """Jours de repos autour d'une date, charges en une seule requete."""
    marge = timedelta(days=WINDOW_DAYS)
    return day_off_model.off_map(reference - marge, reference + marge)


def _reason(day, carte):
    """Motif de repos d'un jour, d'apres une carte de jours de repos chargee."""
    entree = carte.get(day)
    if entree:
        return entree["title"]
    if day.weekday() not in work_weekdays():
        return WEEKEND_LABEL if day.weekday() >= 5 else NON_WORKING_LABEL
    return None


def calendar(start_day, end_day):
    """Retourne le statut de chaque jour d'une periode, en une seule requete.

    :return: liste de {date, working, reason}
    """
    carte = day_off_model.off_map(start_day, end_day) if has_app_context() else {}
    jours = []
    jour = start_day
    while jour <= end_day:
        motif = _reason(jour, carte) if has_app_context() else None
        jours.append({"date": jour.isoformat(), "working": motif is None, "reason": motif})
        jour += timedelta(days=1)
    return jours


def day_off_reason(day, reference=None):
    """Retourne le motif pour lequel un jour n'est pas travaille, ou None.

    :param day: jour examine
    :param reference: jour autour duquel charger le calendrier ; partager la
        meme reference entre plusieurs appels evite une requete par jour
    :return: intitule du conge ou du ferie, 'Week-end', ou None si ouvre
    """
    if not has_app_context():
        return None

    reference = reference or day
    if abs((day - reference).days) > WINDOW_DAYS:
        reference = day

    return _reason(day, _window(reference))


def is_working_day(day, reference=None):
    """Indique si un jour est travaille."""
    return day_off_reason(day, reference) is None


def working_days_until(target, today):
    """Compte le delai jusqu'a une echeance, en jours ouvres.

    Vendredi pour une echeance lundi donne 1 et non 3. Une echeance passee
    donne un nombre negatif, au minimum -1 : une tache due samedi et vue
    dimanche est en retard meme si aucun jour ouvre ne s'est ecoule.

    :return: entier signe ; jours calendaires au-dela de WINDOW_DAYS
    """
    ecart = (target - today).days
    if ecart == 0 or abs(ecart) > WINDOW_DAYS or not has_app_context():
        return ecart

    debut = today if ecart > 0 else target
    ouvres = sum(
        1
        for decalage in range(1, abs(ecart) + 1)
        if is_working_day(debut + timedelta(days=decalage), today)
    )

    if ecart > 0:
        return ouvres
    return -max(1, ouvres)
