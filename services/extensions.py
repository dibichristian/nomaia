"""Chargement d'une extension locale facultative (paquet `perso/`).

Une installation peut ajouter un paquet `perso/` a la racine du programme
pour y loger son code propre (blueprints, gabarits, reglages d'affichage)
sans retoucher les fichiers communs. Contrat : le paquet expose une fonction
`register(app)`, appelee une seule fois a la construction de l'application.

Sans paquet `perso/`, rien ne se passe. Une extension cassee est journalisee
avec sa trace complete, sans empecher NOMAIA de demarrer.
"""
import importlib
import logging
from pathlib import Path

import config

logger = logging.getLogger("nomaia.extensions")

DEFAULT_MODULE = "perso"


def _is_local(module):
    """Indique si le module provient du dossier du programme.

    Ecarte un paquet homonyme installe ailleurs (site-packages) : seul le
    dossier perso/ livre avec NOMAIA a le droit de s'y brancher.
    """
    location = getattr(module, "__file__", None)
    if not location:
        return False
    try:
        Path(location).resolve().relative_to(Path(config.RESOURCE_DIR).resolve())
    except ValueError:
        return False
    return True


def load_extensions(app, module_name=DEFAULT_MODULE):
    """Importe l'extension locale et lui confie l'application.

    Les routes declarees par l'extension passent par les memes filtres
    before_request que le reste : cle d'acces et garde-fou anti-CSRF.

    :param app: application Flask en construction
    :param module_name: nom du paquet a charger (parametre pour les tests)
    :return: True si l'extension a ete chargee, False sinon
    """
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        # Seule l'absence du paquet lui-meme est normale : une dependance
        # manquante a l'interieur de l'extension est une vraie erreur.
        if error.name == module_name:
            return False
        logger.exception("Extension %s : import impossible.", module_name)
        return False
    except Exception:
        logger.exception("Extension %s : import impossible.", module_name)
        return False

    if not _is_local(module):
        logger.warning(
            "Extension %s ignoree : %s n'est pas dans le dossier du programme.",
            module_name, getattr(module, "__file__", None),
        )
        return False

    register = getattr(module, "register", None)
    if not callable(register):
        logger.warning("Extension %s : aucune fonction register(app), ignoree.", module_name)
        return False

    try:
        register(app)
    except Exception:
        logger.exception("Extension %s : echec de register(app).", module_name)
        return False

    logger.info("Extension %s chargee.", module_name)
    return True
