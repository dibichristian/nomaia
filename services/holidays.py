"""Jours feries par pays, importes dans les jours de repos.

Seuls les feries calculables sont couverts : dates fixes et fetes mobiles
derivees de Paques. Les fetes musulmanes (Tabaski, Aid el-Fitr, Maouloud...)
dependent de l'observation de la lune et sont annoncees par decret quelques
jours avant : les calculer donnerait une date fausse une annee sur deux. Elles
se saisissent a la main, comme un conge.

Ajouter un pays revient a ajouter une entree a COUNTRIES.
"""
import logging
from datetime import date, timedelta

from models import day_off as day_off_model
from models import setting

logger = logging.getLogger("nomaia.holidays")

# Fetes mobiles chretiennes, en jours apres le dimanche de Paques.
EASTER_MONDAY = (1, "Lundi de Pâques")
ASCENSION = (39, "Ascension")
WHIT_MONDAY = (50, "Lundi de Pentecôte")

COUNTRIES = {
    "CI": {
        "label": "Côte d'Ivoire",
        "fixed": (
            (1, 1, "Jour de l'An"),
            (5, 1, "Fête du Travail"),
            (8, 7, "Fête de l'Indépendance"),
            (8, 15, "Assomption"),
            (11, 1, "Toussaint"),
            (11, 15, "Journée nationale de la Paix"),
            (12, 25, "Noël"),
        ),
        "easter": (EASTER_MONDAY, ASCENSION, WHIT_MONDAY),
    },
    "SN": {
        "label": "Sénégal",
        "fixed": (
            (1, 1, "Jour de l'An"),
            (4, 4, "Fête de l'Indépendance"),
            (5, 1, "Fête du Travail"),
            (8, 15, "Assomption"),
            (11, 1, "Toussaint"),
            (12, 25, "Noël"),
        ),
        "easter": (EASTER_MONDAY, ASCENSION, WHIT_MONDAY),
    },
    "FR": {
        "label": "France",
        "fixed": (
            (1, 1, "Jour de l'An"),
            (5, 1, "Fête du Travail"),
            (5, 8, "Victoire 1945"),
            (7, 14, "Fête nationale"),
            (8, 15, "Assomption"),
            (11, 1, "Toussaint"),
            (11, 11, "Armistice 1918"),
            (12, 25, "Noël"),
        ),
        "easter": (EASTER_MONDAY, ASCENSION, WHIT_MONDAY),
    },
}


def easter_sunday(year):
    """Retourne le dimanche de Paques gregorien (algorithme de Meeus/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - notation de l'algorithme
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holidays_for(country, year):
    """Retourne les feries calculables d'un pays pour une annee.

    :param country: code pays de COUNTRIES
    :return: liste triee de tuples (date, intitule) ; vide si pays inconnu
    """
    regles = COUNTRIES.get(country)
    if not regles:
        return []

    feries = [(date(year, mois, jour), titre) for mois, jour, titre in regles["fixed"]]
    paques = easter_sunday(year)
    feries += [(paques + timedelta(days=ecart), titre) for ecart, titre in regles["easter"]]
    return sorted(feries)


def import_year(year, country=None):
    """Importe les feries d'une annee. Les jours deja presents sont laisses.

    :param country: code pays, defaut le reglage holiday_country
    :return: nombre de feries ajoutes
    """
    country = country if country is not None else setting.get("holiday_country")
    ajoutes = 0
    for jour, titre in holidays_for(country, year):
        if day_off_model.add_holiday(titre, jour, country):
            ajoutes += 1
    return ajoutes


def _imported_keys():
    """Retourne les couples pays:annee deja importes automatiquement."""
    brut = setting.get("holidays_imported") or ""
    return {cle for cle in brut.split(",") if cle}


def ensure_imported(today=None):
    """Importe une seule fois les feries de l'annee en cours et de la suivante.

    L'annee suivante est couverte des maintenant : une echeance de janvier vue
    en decembre doit deja sauter le Jour de l'An. Un ferie supprime a la main
    n'est jamais reimporte, puisque l'annee est marquee comme faite.

    Changer de pays retire les feries futurs de l'ancien avant d'importer.

    :return: nombre de feries ajoutes
    """
    today = today or date.today()
    country = setting.get("holiday_country") or ""
    if country not in COUNTRIES:
        return 0

    faites = _imported_keys()
    a_faire = [annee for annee in (today.year, today.year + 1) if f"{country}:{annee}" not in faites]
    if not a_faire:
        return 0

    retires = day_off_model.purge_foreign_holidays(country, today)
    if retires:
        logger.info("%s ferie(s) d'un autre pays retire(s).", retires)

    ajoutes = 0
    for annee in a_faire:
        ajoutes += import_year(annee, country)
        faites.add(f"{country}:{annee}")

    setting.set_value("holidays_imported", ",".join(sorted(faites)))
    if ajoutes:
        logger.info("%s jour(s) ferie(s) importe(s) pour %s.", ajoutes, country)
    return ajoutes
