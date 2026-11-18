"""Lecture des mails transferes a la main par l'utilisateur.

NOMAIA n'a aucun acces a la boite Outlook : l'utilisateur choisit chaque mail et
le depose, soit en fichier .eml (web ou Telegram), soit en texte colle apres
/mail. Ce module en tire un texte de capture lisible par le triage.

Seul le texte est retenu : les pieces jointes sont ignorees.
"""
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

# Un .eml sans pieces jointes depasse rarement quelques centaines de Ko ;
# au-dela, ce sont les pieces jointes qui pesent, et elles sont ignorees.
MAX_EML_BYTES = 10 * 1024 * 1024

# Corps conserve pour le triage. Au-dela, c'est presque toujours l'historique
# cite des echanges precedents, qui coute des jetons sans apporter d'action.
MAX_BODY_CHARS = 6000

EML_EXTENSION = ".eml"
EML_MIME = "message/rfc822"

# Balises HTML apres lesquelles le texte change de ligne.
_BLOCK_TAGS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}


class _TextExtractor(HTMLParser):
    """Extrait le texte visible d'un corps HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html):
    """Convertit un corps HTML en texte brut, lignes vides compactees."""
    extractor = _TextExtractor()
    extractor.feed(html or "")
    extractor.close()
    return _compact("".join(extractor.parts))


def _compact(text):
    """Retire les espaces de fin de ligne et les lignes vides en serie."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    compacted = []
    for line in lines:
        if not line and compacted and not compacted[-1]:
            continue
        compacted.append(line)
    return "\n".join(compacted).strip()


def _truncate(body):
    """Coupe le corps a MAX_BODY_CHARS en le signalant."""
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS].rstrip() + "\n[... suite du mail coupee]"


def is_eml(filename=None, mime_type=None):
    """Indique si un fichier depose est un mail .eml."""
    if (mime_type or "").lower() == EML_MIME:
        return True
    return (filename or "").lower().endswith(EML_EXTENSION)


def _body_text(message):
    """Retourne le corps texte d'un mail : texte brut, sinon HTML converti."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        # Jeu de caracteres inconnu ou mal declare : lecture tolerante.
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_subtype() == "html":
        return html_to_text(content)
    return _compact(content)


def _date_label(message):
    """Retourne la date d'envoi au format AAAA-MM-JJ HH:MM, ou None."""
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(raw)


def parse_eml(data):
    """Analyse un fichier .eml.

    :param data: contenu binaire du fichier
    :return: dict {sender, to, date, subject, body}
    :raises ValueError: fichier vide, trop lourd ou sans contenu exploitable
    """
    if not data:
        raise ValueError("Fichier mail vide.")
    if len(data) > MAX_EML_BYTES:
        raise ValueError("Mail trop lourd (10 Mo maximum).")

    message = BytesParser(policy=policy.default).parsebytes(data)
    mail = {
        "sender": str(message.get("From") or "").strip() or None,
        "to": str(message.get("To") or "").strip() or None,
        "date": _date_label(message),
        "subject": str(message.get("Subject") or "").strip() or None,
        "body": _truncate(_body_text(message)),
    }
    if not mail["body"] and not mail["subject"]:
        raise ValueError("Ce fichier ne contient aucun mail lisible.")
    return mail


def capture_text(mail, note=None):
    """Met en forme un mail analyse pour le triage.

    :param mail: dict rendu par parse_eml()
    :param note: precision libre de l'utilisateur, placee avant le mail
    """
    lines = []
    if note and note.strip():
        lines += [f"NOTE DE L'UTILISATEUR : {note.strip()}", ""]
    lines.append("MAIL TRANSFERE")
    for label, key in (("De", "sender"), ("A", "to"), ("Date", "date"), ("Objet", "subject")):
        if mail.get(key):
            lines.append(f"{label} : {mail[key]}")
    lines += ["", mail.get("body") or "(corps vide)"]
    return "\n".join(lines)


def pasted_text(text):
    """Met en forme un mail colle en texte brut (commande /mail).

    :raises ValueError: texte vide
    """
    body = _compact(text or "")
    if not body:
        raise ValueError("Colle le contenu du mail apres /mail.")
    return "MAIL TRANSFERE (texte colle)\n\n" + _truncate(body)
