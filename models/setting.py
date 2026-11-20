"""Reglages persistants (cle / valeur) avec valeurs par defaut.

Les reglages listes dans SECRET_KEYS sont chiffres au repos (voir models.secret)
et ne doivent etre lus que par get_secret().
"""
import re
import secrets as secrets_module
from datetime import datetime, timedelta

from models import get_db, secret

# Appairage d'un appareil (telephone) : duree de validite du code, essais
# faux toleres, alphabet sans caractere ambigu a la lecture.
PAIRING_TTL_S = 600
PAIRING_MAX_FAILURES = 5
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Reglages chiffres en base et effaces des sauvegardes.
SECRET_KEYS = (
    "telegram_token",
    "access_key",
    "anthropic_api_key",
    "gemini_api_key",
    "agent_key",
    "agent_key_perso",
    "pairing_code",
    "odoo_api_key",
)

DEFAULTS = {
    # Cle des sessions Claude Code (serveur MCP local), distincte de la cle de
    # l'interface : elle n'ouvre que /api/agent/, et se revoque seule.
    "agent_key": "",
    # Cle des sessions Claude de l'espace perso : meme portee que agent_key
    # (qui reste celle de l'espace pro), mais limitee aux projets et taches perso.
    "agent_key_perso": "",
    # Espace affiche dans l'interface (selecteur de la barre du haut) : all,
    # pro ou perso. Global plutot que par appareil : poste et telephone
    # montrent le meme perimetre.
    "space_filter": "all",
    # Espace des nouveaux elements quand le selecteur est sur « Tout ».
    "create_space": "pro",
    # Couleurs de chaque espace (debut et fin du degrade), injectees dans
    # layout.html en variables CSS --space-<espace>-from / -to. Memes valeurs
    # que les defauts declares dans static/css/shell.css.
    "space_pro_color_from": "#6366f1",
    "space_pro_color_to": "#22d3ee",
    "space_perso_color_from": "#f59e0b",
    "space_perso_color_to": "#ec4899",
    # Horaires propres a l'espace perso (services/planner.py) : coupes par
    # defaut, les deux espaces partagent alors les horaires de travail.
    "perso_hours_enabled": "0",
    "perso_start": "19:00",
    "perso_end": "22:00",
    "perso_offday_start": "09:00",
    "perso_offday_end": "18:00",
    # Fournisseur de modele : claude_cli, anthropic, gemini ou ollama.
    "llm_provider": "claude_cli",
    # Modele retenu pour chaque fournisseur, memorise separement afin qu'une
    # bascule d'un fournisseur a l'autre n'efface pas le choix precedent.
    "model_claude_cli": "sonnet",
    "model_anthropic": "claude-sonnet-5",
    "model_gemini": "gemini-3.8-flash",
    "model_ollama": "qwen2.5vl:7b",
    # Cles d'acces aux interfaces de programmation (chiffrees au repos).
    "anthropic_api_key": "",
    "gemini_api_key": "",
    # Fournisseur de la conversation, separe de celui du triage : un tour de
    # chat enchaine jusqu'a chat_max_steps appels, ce que le CLI ne supporte
    # pas economiquement.
    #
    # Vide par defaut, donc le chat retombe sur llm_provider : il fonctionne
    # des la premiere ouverture, quitte a etre lent, et la page affiche un
    # avertissement. Nommer ici un fournisseur a cle casserait le chat sur
    # toute installation qui n'a pas cette cle.
    "chat_provider": "",
    # Tours de dialogue rappeles au modele a chaque message.
    "chat_history_turns": "12",
    # Appels d'outils autorises pour repondre a un seul message. Au-dela,
    # NOMAIA repond avec ce qu'il a plutot que de boucler indefiniment.
    "chat_max_steps": "5",
    # Message Telegram libre : "1" ouvre la conversation, "0" conserve le
    # depot de capture comme unique comportement.
    "telegram_chat_mode": "1",
    # Longueur au-dela de laquelle un message Telegram part au triage plutot
    # qu'a la conversation : un collage de plusieurs paragraphes est un depot.
    "telegram_capture_chars": "400",
    # Vocaux Telegram : transcrits sur le poste, puis traites comme un texte.
    "voice_enabled": "1",
    # Taille du modele Whisper : tiny, base ou small.
    "voice_model": "small",
    # Langue forcee ("fr") : la detection automatique se trompe sur les
    # vocaux courts. Vide = detection automatique.
    "voice_language": "fr",
    # Duree maximale d'un vocal accepte, en secondes : au-dela, la transcription
    # sur CPU bloquerait la reception des autres messages trop longtemps.
    "voice_max_s": "300",
    # Nombre de taches ouvertes injectees en contexte de triage : chaque tache
    # coute une quinzaine de jetons, d'ou un plafond bas par defaut.
    "context_task_limit": "12",
    # Nombre de tours de dialogue rappeles lors d'une relance de triage.
    "context_history_turns": "6",
    # Chemin force vers l'executable claude (vide = detection automatique).
    "claude_path": "",
    # Dossier de configuration de Claude Code, quand plusieurs comptes cohabitent
    # sur la machine. Lance depuis l'icone, NOMAIA n'herite pas de la variable
    # CLAUDE_CONFIG_DIR du terminal et retomberait sur le compte par defaut,
    # dont la session a pu expirer. Vide = comportement herite.
    "claude_config_dir": "",
    "llm_timeout_s": "90",
    # Cle d'acces a l'interface, generee au premier demarrage.
    "access_key": "",
    # Adresse HTTPS par laquelle le telephone joint NOMAIA (Tailscale serve),
    # ex. https://pc.tail1234.ts.net. Vide = acces local seulement.
    "mobile_origin": "",
    # Code d'appairage d'un appareil : usage unique, duree courte, et efface
    # apres quelques essais faux.
    "pairing_code": "",
    "pairing_expires": "",
    "pairing_failures": "0",
    # Passerelle Telegram (jeton renseigne en base, jamais dans le code).
    "telegram_token": "",
    "telegram_chat_id": "",
    "telegram_offset": "0",
    "telegram_pair_code": "",
    "telegram_waiting_capture": "",
    # Conversation du chat suivie par Telegram (/conv, /newconv). Vide : la
    # conversation courante, celle active en dernier.
    "telegram_conversation_id": "",
    # Instantane epingle dans le chat : seule facon de consulter ses taches
    # depuis le telephone quand le PC est eteint.
    "telegram_pinned_digest": "1",
    "telegram_digest_message_id": "",
    # Horaires de travail utilises pour construire le plan du jour.
    "work_start": "08:30",
    "work_end": "18:00",
    "lunch_start": "12:30",
    "lunch_end": "13:30",
    # Jours de la semaine travailles, lundi = 0. Un jour hors de cette liste a
    # une capacite nulle et ne declenche aucun message proactif.
    "work_days": "0,1,2,3,4",
    # Pays dont les feries calculables sont importes (CI, SN, FR ; vide = aucun).
    "holiday_country": "CI",
    # Couples pays:annee deja importes : un ferie supprime a la main ne
    # revient pas au passage suivant.
    "holidays_imported": "",
    # Intervalle entre deux relectures des agendas externes (ICS), en minutes.
    # Outlook ne republie son flux que toutes les 15 a 30 min : plus court ne
    # sert a rien.
    "calendar_sync_min": "15",
    # Proactivite : NOMAIA signale ce qui va deraper avant que ce soit fait.
    # "0" coupe toutes les alertes anticipees (les points matin et soir
    # restent, sans leurs nouveaux blocs).
    "foresight_enabled": "1",
    # Horizon, en jours ouvres, sur lequel les echeances sont surveillees.
    "risk_horizon_days": "10",
    # Anciennete d'une tache sans echeance ni temps passe avant d'etre dite
    # dormante.
    "dormant_after_days": "21",
    # Inactivite d'un projet actif avant d'etre signale.
    "project_idle_days": "14",
    # Delai avant de rappeler une proposition ou une question du triage.
    "validation_after_hours": "24",
    # Derniere verification des echeances en danger pour l'alerte immediate.
    "foresight_last_check": "",
    # Semaine ISO du dernier bloc « Menage de la semaine ».
    "foresight_last_weekly": "",
    # Marge ajoutee a chaque estimation (les estimations sont toujours optimistes).
    "plan_buffer_pct": "15",
    # Les taches dues au-dela de cet horizon ne sont pas planifiees aujourd'hui.
    "plan_horizon_days": "3",
    # Autorise le triage a creer un projet absent quand il identifie un client
    # ou un module ("0" pour n'accepter que les projets crees a la main).
    "auto_create_project": "1",
    # Autorise le triage a identifier les interlocuteurs et ce qu'ils doivent.
    # "0" revient exactement au comportement anterieur : aucune personne creee,
    # aucune attente enregistree.
    "extract_people": "1",
    # Anciennete a partir de laquelle une attente est signalee le matin.
    # Nom affiche en bas de la barre laterale (initiales dans la pastille).
    "display_name": "",
    "nudge_after_days": "3",
    # Silence entre deux signalements d'une meme attente : sans lui, la relance
    # repete la meme ligne chaque matin jusqu'a devenir un bruit.
    "nudge_quiet_days": "3",
    # Rendez-vous quotidiens : NOMAIA pousse le plan le matin et le point le
    # soir, sur Telegram et en bulle Windows.
    "heartbeat_enabled": "1",
    "morning_brief_at": "08:30",
    "evening_brief_at": "18:00",
    # Injecte dans le prompt de triage l'ecart mesure entre duree estimee et
    # duree reellement chronometree ("0" pour laisser l'IA estimer a l'aveugle).
    "estimate_calibration": "1",
    # Retard au-dela duquel une tache est signalee comme derivante.
    "drift_alert_days": "3",
    # Rappel avant chaque reunion de l'agenda, sur Telegram et en bulle.
    "event_reminder_enabled": "1",
    "event_reminder_min": "10",
    # Debut de la derniere occurrence deja annoncee : evite de renvoyer un
    # rappel apres un redemarrage.
    "event_reminder_watermark": "",
    # Date du dernier rendez-vous tenu : evite de rejouer le meme message a
    # chaque redemarrage de la journee.
    "heartbeat_last_morning": "",
    "heartbeat_last_evening": "",
    # Base Odoo dont les taches assignees sont importees (lecture seule, voir
    # services/odoo_sync.py). json2 : cle API seule ; jsonrpc : login + cle.
    "odoo_url": "",
    "odoo_db": "",
    "odoo_login": "",
    "odoo_protocol": "json2",
    "odoo_api_key": "",
    # Derniere synchronisation : horodatage et bilan JSON.
    "odoo_last_sync": "",
    "odoo_last_report": "",
    # Agenda Odoo (voir services/odoo_calendar.py) : ce que la source Odoo
    # importe. Conges valides, conges en attente (affiches, non bloquants),
    # feries et fermetures de la societe, reunions.
    "odoo_cal_leaves": "1",
    "odoo_cal_pending": "1",
    "odoo_cal_holidays": "1",
    "odoo_cal_meetings": "1",
    # Mails Outlook lus par Claude Code via le connecteur Microsoft 365 du
    # compte Claude (voir services/mail_reader.py). "1" : lecture toutes les
    # heures pendant les heures de travail. Rien n'est stocke ici pour l'acces.
    "mail_enabled": "0",
    # Debut du dernier passage reussi : borne « recus depuis » du suivant.
    "mail_last_seen": "",
    # Dernier passage (reussi ou non), mails importes, derniere erreur.
    "mail_last_run": "",
    "mail_last_count": "0",
    "mail_last_error": "",
    # Seuil au-dessus duquel NOMAIA cree les taches sans validation.
    "auto_apply_confidence": "0.8",
    # Seuil en dessous duquel NOMAIA ne propose rien sans relecture humaine.
    "manual_confidence": "0.5",
}


def get(key, default=None):
    """Retourne la valeur d'un reglage (base, puis defaut integre, puis argument)."""
    row = get_db().execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    return DEFAULTS.get(key, default)


SPACE_FILTERS = ("all", "pro", "perso")


def space_filter():
    """Espace choisi dans l'interface : 'pro', 'perso', ou None pour tous.

    Une valeur inconnue vaut 'tous' : un reglage corrompu ne doit pas vider
    les listes sans explication.
    """
    valeur = get("space_filter")
    return valeur if valeur in ("pro", "perso") else None


def create_space():
    """Espace des elements crees quand le selecteur montre les deux espaces."""
    valeur = get("create_space")
    return valeur if valeur in ("pro", "perso") else "pro"


# Couleur d'espace acceptee : #rrggbb strict. La valeur finit dans une balise
# <style> : tout autre format est refuse plutot qu'echappe.
SPACE_COLOR_PATTERN = re.compile(r"^#[0-9a-f]{6}$")

# Variable CSS -> reglage portant la couleur.
SPACE_COLOR_SETTINGS = {
    "--space-pro-from": "space_pro_color_from",
    "--space-pro-to": "space_pro_color_to",
    "--space-perso-from": "space_perso_color_from",
    "--space-perso-to": "space_perso_color_to",
}


def is_space_color(value):
    """Indique si une valeur est une couleur #rrggbb (minuscules)."""
    return isinstance(value, str) and bool(SPACE_COLOR_PATTERN.fullmatch(value))


def space_colors():
    """Couleurs des espaces, par variable CSS (--space-pro-from, ...).

    Une valeur en base hors format (base modifiee a la main) retombe sur son
    defaut : rien d'autre qu'un #rrggbb n'atteint la balise <style>.

    :return: dict variable CSS -> couleur #rrggbb
    """
    couleurs = {}
    for variable, key in SPACE_COLOR_SETTINGS.items():
        valeur = get(key)
        couleurs[variable] = valeur if is_space_color(valeur) else DEFAULTS[key]
    return couleurs


def resolve_space(raw):
    """Traduit le parametre `space` d'une requete en filtre d'espace.

    Le filtre est opt-in : sans parametre, aucune restriction (chat, plan,
    agent continuent de tout voir).

    :param raw: 'current' (espace choisi dans l'interface), 'pro', 'perso'
        ou rien
    :return: 'pro', 'perso' ou None
    """
    valeur = (raw or "").strip().lower()
    if valeur == "current":
        return space_filter()
    return valeur if valeur in ("pro", "perso") else None


def get_float(key):
    """Retourne un reglage numerique, ou son defaut si la valeur est illisible."""
    try:
        return float(get(key))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def get_int(key):
    """Retourne un reglage entier, ou son defaut si la valeur est illisible."""
    return int(get_float(key))


def set_value(key, value):
    """Ecrit un reglage."""
    db = get_db()
    db.execute(
        "INSERT INTO setting (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()


def all_settings():
    """Retourne les reglages non secrets : defauts surcharges par la base."""
    values = {key: value for key, value in DEFAULTS.items() if key not in SECRET_KEYS}
    for row in get_db().execute("SELECT key, value FROM setting"):
        if row["key"] not in SECRET_KEYS:
            values[row["key"]] = row["value"]
    return values


def get_secret(key):
    """Lit un reglage secret en le dechiffrant."""
    return secret.unprotect(get(key)) or ""


def set_secret(key, value):
    """Ecrit un reglage secret sous forme chiffree."""
    set_value(key, secret.protect(value))


def ensure_access_key():
    """Retourne la cle d'acces, en la creant au premier appel.

    La cle protege l'interface des autres comptes Windows de la machine :
    127.0.0.1 leur est accessible, le fichier de base ne l'est pas.
    """
    key = get_secret("access_key")
    if not key:
        key = secrets_module.token_urlsafe(32)
        set_secret("access_key", key)
    return key


# Espace de travail -> reglage portant la cle des sessions Claude de cet
# espace. 'agent_key' reste la cle pro : un lien deja enregistre continue de
# fonctionner apres la mise a jour.
AGENT_KEY_SETTINGS = {"pro": "agent_key", "perso": "agent_key_perso"}


def _agent_key_setting(space):
    """Nom du reglage de la cle d'un espace ; leve ValueError si l'espace est inconnu."""
    try:
        return AGENT_KEY_SETTINGS[space]
    except KeyError:
        raise ValueError("Espace inconnu : pro ou perso.")


def get_agent_key(space="pro"):
    """Retourne la cle des sessions Claude d'un espace, ou une chaine vide si absente."""
    return get_secret(_agent_key_setting(space))


def regenerate_agent_key(space="pro"):
    """Tire une nouvelle cle des sessions Claude d'un espace : l'ancienne cesse aussitot de marcher."""
    key = secrets_module.token_urlsafe(32)
    set_secret(_agent_key_setting(space), key)
    return key


def issue_pairing_code(ttl_s=PAIRING_TTL_S):
    """Tire un code d'appairage d'appareil, qui remplace le precedent.

    :return: code de 8 caracteres, sans caractere ambigu (0/O, 1/I)
    """
    code = "".join(secrets_module.choice(PAIRING_ALPHABET) for _ in range(8))
    set_secret("pairing_code", code)
    set_value("pairing_expires", (datetime.now() + timedelta(seconds=ttl_s)).isoformat())
    set_value("pairing_failures", "0")
    return code


def _clear_pairing_code():
    """Efface le code d'appairage en cours."""
    set_secret("pairing_code", "")
    set_value("pairing_expires", "")
    set_value("pairing_failures", "0")


def consume_pairing_code(provided):
    """Verifie un code d'appairage saisi sur un appareil.

    Un code juste est efface aussitot : il ne sert qu'une fois. Un code faux
    compte comme un essai, et le code en cours est efface au bout de
    PAIRING_MAX_FAILURES essais : le deviner par force brute est impossible.

    :return: True si le code est valide
    """
    expected = get_secret("pairing_code")
    if not expected:
        return False
    try:
        expired = datetime.fromisoformat(get("pairing_expires")) < datetime.now()
    except (TypeError, ValueError):
        expired = True
    if expired:
        _clear_pairing_code()
        return False

    normalized = "".join(char for char in str(provided or "").upper() if char.isalnum())
    if secrets_module.compare_digest(normalized.encode("utf-8"), expected.encode("utf-8")):
        _clear_pairing_code()
        return True

    failures = get_int("pairing_failures") + 1
    if failures >= PAIRING_MAX_FAILURES:
        _clear_pairing_code()
    else:
        set_value("pairing_failures", failures)
    return False


def encrypt_legacy_secrets():
    """Chiffre les secrets encore stockes en clair (bases anterieures)."""
    encrypted = []
    for key in SECRET_KEYS:
        value = get(key)
        if value and not secret.is_protected(value):
            set_value(key, secret.protect(value))
            encrypted.append(key)
    return encrypted
