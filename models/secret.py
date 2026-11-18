"""Chiffrement des secrets au repos via DPAPI (Windows).

DPAPI lie le chiffrement a la session Windows de l'utilisateur : une base
copiee, synchronisee sur un cloud ou recuperee sur un disque vole ne peut pas
etre dechiffree ailleurs. Un programme lance sous le meme compte, lui, le peut
toujours : c'est une protection contre la fuite du fichier, pas contre un
processus local.
"""
import base64
import logging

logger = logging.getLogger("nomaia.secret")

try:
    import win32crypt
except ImportError:  # hors Windows ou pywin32 absent
    win32crypt = None

# Marqueur en tete de valeur chiffree : permet de reconnaitre les valeurs
# heritees d'une version anterieure, encore en clair.
PREFIX = "dpapi:"
DESCRIPTION = "NOMAIA"


def available():
    """Indique si le chiffrement est utilisable sur ce poste."""
    return win32crypt is not None


def is_protected(value):
    """Indique si une valeur est deja chiffree."""
    return bool(value) and str(value).startswith(PREFIX)


def protect(value):
    """Chiffre une valeur. Retourne la valeur inchangee si DPAPI est indisponible."""
    if not value or is_protected(value):
        return value

    if not available():
        logger.warning("DPAPI indisponible : le secret reste en clair en base.")
        return value

    blob = win32crypt.CryptProtectData(
        str(value).encode("utf-8"), DESCRIPTION, None, None, None, 0
    )
    return PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect(value):
    """Dechiffre une valeur. Une valeur en clair est retournee telle quelle."""
    if not is_protected(value):
        return value

    if not available():
        logger.error("Secret chiffre mais DPAPI indisponible : valeur illisible.")
        return ""

    try:
        blob = base64.b64decode(str(value)[len(PREFIX):])
        _, clear = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return clear.decode("utf-8")
    except Exception:
        # Base chiffree sous une autre session Windows : la valeur est perdue,
        # mais l'application doit continuer a demarrer.
        logger.error("Dechiffrement impossible : secret cree sous une autre session.")
        return ""
