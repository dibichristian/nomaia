"""Detecteur de secrets saisis en clair (mots de passe, cles d'API, cles privees).

Une note technique ou un compte rendu finit toujours par recevoir un mot de
passe admin Odoo colle « pour plus tard ». NOMAIA refuse de l'enregistrer :
il faut noter OU le secret est range, pas le secret lui-meme.

Les valeurs de remplacement evidentes (***, <mot de passe>, ${VAR},
os.environ[...], changeme...) sont acceptees.
"""
import re

_KEY = r"(?:password|passwd|pwd|mot[ _-]?de[ _-]?passe|mdp|secret|api[_-]?key|access[_-]?key|token|private[_-]?key)"
# (?<![a-z]) plutot que \b : « admin_passwd » ou « db_password » doivent etre reconnus.
_ASSIGNMENT = re.compile(rf"(?i)(?<![a-z]){_KEY}(?![a-z])[\"']?\s*[:=]\s*[\"']?([^\s\"',;]+)")
_PLACEHOLDER = re.compile(
    r"(?i)^(?:\*+|x{3,}|\.{3,}|<.*|\$\{?[\w.]+\}?|%\(?\w+\)?s?|\{\{.*\}\}|false|true|none|null"
    r"|changeme|change-?moi|redacted|masqu[eé]|secret|password|vide|empty|os\.environ.*|env\(.*|getenv.*)$"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# Formats de jetons reconnaissables a leur prefixe.
_TOKENS = re.compile(
    r"\b(?:sk-ant-[\w-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|github_pat_\w{30,}"
    r"|AKIA[0-9A-Z]{16}|xox[abprs]-[\w-]{10,}|dt_[\w-]{40,})\b"
)


class SecretDetected(ValueError):
    """Un texte contient un secret en clair."""


def find_secret(text):
    """Premier secret trouve dans le texte (description courte), ou None."""
    if not text:
        return None
    text = str(text)
    if _PRIVATE_KEY.search(text):
        return "une clé privée"
    token = _TOKENS.search(text)
    if token:
        return f"une clé d'API ({token.group(0)[:6]}…)"
    for match in _ASSIGNMENT.finditer(text):
        value = match.group(1)
        if len(value) >= 3 and not _PLACEHOLDER.match(value):
            return f"« {match.group(0).split(value)[0].strip()} … »"
    return None


# Bloc complet d'une cle privee ; sans ligne de fin, tout le reste du texte.
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
)

MASK = "***"


def mask_secrets(text):
    """Remplace par *** les secrets d'un texte venu d'ailleurs (mail lu).

    Un texte qu'on ne saisit pas soi-meme ne peut pas etre refuse : on le garde,
    mais sans la valeur du secret. Memes motifs que find_secret().

    :param text: texte a nettoyer
    :return: (texte masque, nombre de secrets masques)
    """
    if not text:
        return text, 0
    text = str(text)
    compte = 0

    text, trouves = _PRIVATE_KEY_BLOCK.subn(MASK + " (clé privée masquée)", text)
    compte += trouves
    text, trouves = _TOKENS.subn(MASK, text)
    compte += trouves

    def _masquer(match):
        nonlocal compte
        value = match.group(1)
        if len(value) < 3 or _PLACEHOLDER.match(value):
            return match.group(0)
        compte += 1
        debut = match.start(1) - match.start(0)
        fin = match.end(1) - match.start(0)
        return match.group(0)[:debut] + MASK + match.group(0)[fin:]

    text = _ASSIGNMENT.sub(_masquer, text)
    return text, compte


def check(fields):
    """Refuse l'enregistrement si un champ contient un secret.

    :param fields: dictionnaire {libelle du champ: texte}
    :raises SecretDetected: message pret a afficher
    """
    for label, value in fields.items():
        hit = find_secret(value) if isinstance(value, str) else None
        if hit:
            raise SecretDetected(
                f"« {label} » semble contenir un secret en clair ({hit}). Remplacez la valeur par *** et notez"
                " plutôt où le secret est rangé (gestionnaire de mots de passe, coffre du client…)."
            )
