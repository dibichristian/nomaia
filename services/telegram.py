"""Passerelle Telegram : depot de captures depuis le telephone.

Fonctionne en long polling (getUpdates) : aucun port a ouvrir, aucun nom de
domaine, rien a exposer sur Internet. Si le PC est eteint, Telegram conserve
les messages 24 h et NOMAIA les recupere au redemarrage.

Le jeton n'est jamais ecrit dans le code : il vit dans la table `setting`.
"""
import errno
import http.client
import json
import logging
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import config
from models import capture as capture_model
from models import chat as chat_model
from models import setting
from services import mail_parse, transcribe, triage

logger = logging.getLogger("nomaia.telegram")

API_ROOT = "https://api.telegram.org"
API_HOST = "api.telegram.org"

# Duree du long polling cote Telegram.
POLL_TIMEOUT_S = 25

# Sur certains reseaux, environ un tiers des ouvertures TLS vers l'IP de
# Telegram sont coupees (WinError 10054) ; une connexion etablie tient, elle.
# D'ou une connexion conservee entre les appels et des reprises rapides.
CALL_ATTEMPTS = 5
CALL_BACKOFF_S = 0.5

# PC hors ligne : attente croissante entre deux sondages, plafonnee.
OFFLINE_WAIT_MIN_S = 15
OFFLINE_WAIT_MAX_S = 60

# Erreurs signifiant l'absence de reseau (et non une coupure ponctuelle).
_OFFLINE_ERRNOS = {
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
    10050, 10051, 10065,  # WSAENETDOWN, WSAENETUNREACH, WSAEHOSTUNREACH
}

MAX_MESSAGE_CHARS = 3800

# Captures dont l'issue est renvoyee sur Telegram. Un mail, depose depuis le
# web ou depuis Telegram, attend toujours une validation : l'utilisateur en est
# prevenu sur son telephone dans les deux cas. Les questions, elles, partent
# quelle que soit la source : on doit pouvoir y repondre depuis le telephone.
NOTIFIED_SOURCES = ("telegram", "email")

# Rappel ajoute sous chaque message auquel on peut repondre.
REPLY_HINT = "Reponds a ce message (glisser vers la droite) pour continuer cette entree."

# Limite de telechargement imposee par l'API Bot de Telegram.
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Menu propose dans l'application Telegram. Declare au demarrage pour rester
# aligne sur les commandes reellement traitees par _handle_command().
BOT_COMMANDS = [
    {"command": "plan", "description": "Le plan de la journee"},
    {"command": "note", "description": "Deposer une entree a trier"},
    {"command": "mail", "description": "Deposer un mail colle (ou envoie le .eml)"},
    {"command": "mails", "description": "Lire mes mails Outlook maintenant"},
    {"command": "taches", "description": "Ce qu'il reste a faire"},
    {"command": "bilan", "description": "Bilan de la semaine"},
    {"command": "nouveau", "description": "Forcer une nouvelle entree"},
    {"command": "convs", "description": "Mes conversations recentes"},
    {"command": "conv", "description": "Reprendre une conversation : /conv 3"},
    {"command": "newconv", "description": "Nouvelle conversation"},
    {"command": "aide", "description": "Mode d'emploi"},
]

class TelegramOffline(RuntimeError):
    """Le PC n'a pas acces au reseau : inutile d'insister."""


def _is_offline_error(error):
    """Indique si une erreur reseau traduit une absence de connexion."""
    if isinstance(error, socket.gaierror):
        return True
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    return code in _OFFLINE_ERRNOS


# Codes d'appairage faux toleres avant de tirer un nouveau code.
PAIR_MAX_FAILURES = 5

HELP_TEXT = (
    "NOMAIA est a l'ecoute.\n\n"
    "Parle-moi normalement : je reponds, je cherche, je cree ou je decale "
    "une tache. Une capture d'ecran ou un long message transfere part au "
    "triage, qui en deduit les taches.\n\n"
    "/note <texte> - forcer le triage au lieu de la conversation\n"
    "/mail <texte> - deposer un mail colle ; un fichier .eml marche aussi\n"
    "/mails - lire mes mails Outlook maintenant (via Claude Code)\n"
    "/plan - le plan de la journee\n"
    "/taches - ce qu'il reste a faire\n"
    "/bilan - bilan de la semaine\n"
    "/nouveau - forcer une nouvelle entree au lieu de repondre a ma question\n"
    "/convs - mes conversations recentes ; /conv 3 en reprend une ; /newconv en ouvre une\n"
    "Repondre a un de mes messages (glisser vers la droite) continue l'entree dont il parle.\n"
    "/aide - ce message"
)


class TelegramBridge:
    """Thread de reception des messages Telegram."""

    def __init__(self, app):
        """:param app: application Flask, pour ouvrir un contexte par message"""
        self.app = app
        self._stop = threading.Event()
        self._thread = None
        # Une connexion HTTPS par thread, reutilisee d'un appel a l'autre.
        self._local = threading.local()
        self._offline_since = None
        self._bot_name = None
        self._checked_at = None
        self.last_error = None
        # Codes d'appairage faux depuis le dernier tirage.
        self._pair_failures = 0
        # Instantane epingle : marque a rafraichir, et date du dernier envoi.
        self._digest_stale = False
        self._digest_pushed_at = None

    # --- Cycle de vie ------------------------------------------------------

    def is_configured(self):
        """Indique si un jeton de bot est enregistre."""
        with self.app.app_context():
            return bool(setting.get_secret("telegram_token"))

    def start(self):
        """Demarre l'ecoute si un jeton est configure."""
        if self._thread and self._thread.is_alive():
            return
        if not self.is_configured():
            logger.info("Telegram non configure : passerelle inactive.")
            return

        with self.app.app_context():
            if not setting.get("telegram_chat_id"):
                if not setting.get("telegram_pair_code"):
                    self._new_pair_code()
                logger.warning(
                    "Telegram non appaire : envoie au bot /pair suivi du code"
                    " affiche dans la boite de reception de NOMAIA."
                )

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nomaia-telegram", daemon=True)
        self._thread.start()

    def stop(self):
        """Demande l'arret de l'ecoute."""
        self._stop.set()

    def is_running(self):
        """Indique si le thread d'ecoute tourne."""
        return bool(self._thread and self._thread.is_alive())

    # --- Appels API --------------------------------------------------------

    def _token(self):
        """Retourne le jeton du bot (contexte applicatif requis)."""
        return setting.get_secret("telegram_token")

    def _connection(self, timeout):
        """Retourne la connexion HTTPS du thread courant, ouverte a la demande."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection(API_HOST, timeout=timeout)
            self._local.conn = conn
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        return conn

    def _drop_connection(self):
        """Ferme la connexion du thread courant (morte ou douteuse)."""
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            conn.close()

    def _call(self, method, params=None, timeout=20, attempts=CALL_ATTEMPTS):
        """Appelle une methode de l'API Telegram et retourne son resultat.

        La connexion est conservee entre les appels : sur certains reseaux,
        l'ouverture TLS vers Telegram est coupee au hasard (WinError 10054),
        mais une connexion etablie tient. Une coupure, ou une connexion morte
        apres une mise en veille, est rejouee sur une connexion neuve. Une
        erreur HTTP explicite (jeton invalide, conflit de sondage) n'est
        jamais rejouee.

        :raises TelegramOffline: le PC n'a pas acces au reseau
        :raises RuntimeError: erreur HTTP, refus de l'API ou essais epuises
        """
        path = f"/bot{self._token()}/{method}"
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                conn = self._connection(timeout)
                conn.request("POST", path, body=data, headers=headers)
                response = conn.getresponse()
                # Lecture complete : condition pour reutiliser la connexion.
                raw = response.read()
            except (http.client.HTTPException, OSError) as error:
                self._drop_connection()
                if _is_offline_error(error):
                    raise TelegramOffline(f"Telegram {method} : reseau indisponible ({error})")
                last_error = error
                if attempt < attempts:
                    logger.debug("%s tentative %s echouee : %s", method, attempt, error)
                    self._stop.wait(CALL_BACKOFF_S)
                continue

            if response.status != 200:
                detail = raw.decode("utf-8", "replace")[:200]
                raise RuntimeError(f"Telegram {method} : HTTP {response.status} {detail}")
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError as error:
                raise RuntimeError(f"Telegram {method} : reponse illisible ({error})")
            if not body.get("ok"):
                raise RuntimeError(f"Telegram {method} a refuse : {body.get('description')}")
            return body.get("result")

        raise RuntimeError(f"Telegram {method} injoignable apres {attempts} essais : {last_error}")

    def send(self, chat_id, text):
        """Envoie un message texte, tronque a la limite Telegram.

        :return: identifiant du message envoye, ou None en cas d'echec
        """
        try:
            result = self._call(
                "sendMessage",
                {"chat_id": chat_id, "text": text[:MAX_MESSAGE_CHARS]},
            )
        except RuntimeError as error:
            self.last_error = str(error)
            logger.error("Envoi Telegram impossible : %s", error)
            return None
        return (result or {}).get("message_id")

    def _send_about(self, chat_id, capture_id, text):
        """Envoie un message lie a une capture : y repondre la poursuit."""
        message_id = self.send(chat_id, text)
        capture_model.link_telegram_message(message_id, capture_id)

    def check(self, max_age_s=60):
        """Verifie le jeton et retourne l'etat de la passerelle.

        Le nom du bot est mis en cache : l'interface interroge cet etat toutes
        les 20 secondes et il est inutile d'appeler Telegram a chaque fois.
        """
        if not setting.get_secret("telegram_token"):
            return {"ok": False, "detail": "Aucun jeton enregistre."}

        age = (datetime.now() - self._checked_at).total_seconds() if self._checked_at else None
        if self._bot_name is None or age is None or age > max_age_s:
            try:
                self._bot_name = (self._call("getMe") or {}).get("username")
                self._checked_at = datetime.now()
            except RuntimeError as error:
                return {"ok": False, "detail": str(error)}

        paired = bool(setting.get("telegram_chat_id"))
        return {
            "ok": True,
            "bot": self._bot_name,
            "paired": paired,
            "pair_code": None if paired else setting.get("telegram_pair_code"),
        }

    # --- Boucle de reception ----------------------------------------------

    def _publish_commands(self):
        """Declare le menu de commandes aupres de Telegram (echec sans gravite)."""
        try:
            with self.app.app_context():
                self._call("setMyCommands", {"commands": json.dumps(BOT_COMMANDS)})
            logger.info("Menu de commandes publie sur Telegram.")
        except RuntimeError as error:
            logger.warning("Menu de commandes non publie : %s", error)

    def _loop(self):
        """Recupere les messages en continu (long polling)."""
        self._publish_commands()

        while not self._stop.is_set():
            try:
                with self.app.app_context():
                    offset = setting.get_int("telegram_offset")
                    updates = self._call(
                        "getUpdates",
                        {"offset": offset, "timeout": POLL_TIMEOUT_S, "allowed_updates": '["message"]'},
                        timeout=POLL_TIMEOUT_S + 15,
                    )

                    for update in updates or []:
                        try:
                            message = update.get("message")
                            if message:
                                self._handle(message)
                        except Exception:
                            logger.exception(
                                "Message Telegram %s non traite.", update["update_id"]
                            )
                        finally:
                            # L'accuse de reception part meme en cas d'echec,
                            # sinon Telegram renvoie le meme message sans fin.
                            setting.set_value("telegram_offset", update["update_id"] + 1)

                self.last_error = None
                if self._offline_since is not None:
                    logger.info("Reseau retabli : ecoute Telegram reprise.")
                    self._offline_since = None
            except TelegramOffline as error:
                # PC sans reseau : un seul message, puis attente croissante.
                self.last_error = str(error)
                if self._offline_since is None:
                    self._offline_since = datetime.now()
                    logger.warning("Hors ligne : ecoute Telegram en pause (%s).", error)
                self._stop.wait(self._offline_wait())
            except RuntimeError as error:
                # Coupure reseau ou API indisponible : on patiente sans mourir.
                self.last_error = str(error)
                logger.warning("Ecoute Telegram interrompue : %s", error)
                self._stop.wait(15)
            except Exception as error:
                self.last_error = str(error)
                logger.exception("Erreur inattendue dans la passerelle Telegram.")
                self._stop.wait(15)

    def _offline_wait(self):
        """Delai avant le prochain sondage hors ligne : double, plafonne."""
        elapsed = (datetime.now() - self._offline_since).total_seconds()
        return min(max(OFFLINE_WAIT_MIN_S, elapsed), OFFLINE_WAIT_MAX_S)

    def _handle(self, message):
        """Traite un message recu : appairage, commande, ou nouvelle capture."""
        chat_id = str(message.get("chat", {}).get("id"))
        text = (message.get("text") or message.get("caption") or "").strip()
        owner = setting.get("telegram_chat_id")

        if not owner:
            self._try_pairing(chat_id, text)
            return

        if chat_id != owner:
            logger.warning("Message ignore : chat %s non autorise.", chat_id)
            return

        if text.startswith("/"):
            if self._handle_command(chat_id, text):
                return
            text = ""

        image_path = None
        if message.get("photo"):
            try:
                image_path = self._download_photo(message["photo"][-1]["file_id"])
            except RuntimeError as error:
                self.send(chat_id, f"Image non recuperee : {error}")
                return
        elif message.get("document"):
            document = message["document"]
            if mail_parse.is_eml(document.get("file_name"), document.get("mime_type")):
                self._deposer_eml(chat_id, document, text)
                return
            if (document.get("mime_type") or "").startswith("image/"):
                try:
                    image_path = self._download_photo(document["file_id"])
                except RuntimeError as error:
                    self.send(chat_id, f"Image non recuperee : {error}")
                    return
        elif message.get("voice") or message.get("audio"):
            transcrit = self._transcrire_vocal(chat_id, message.get("voice") or message["audio"])
            if transcrit is None:
                return
            # Une legende eventuelle precede la transcription : elle donne
            # souvent le contexte du vocal (client, projet).
            text = f"{text}\n{transcrit}" if text else transcrit

        if not text and not image_path:
            self.send(chat_id, "Rien d'exploitable dans ce message.")
            return

        if text and not image_path and self._answer_replied_capture(chat_id, message, text):
            return

        waiting_id = setting.get("telegram_waiting_capture")
        if waiting_id and text and not image_path:
            waiting = capture_model.get_capture(int(waiting_id))
            if waiting and waiting["status"] == "waiting":
                triage.answer_capture(waiting["id"], text)
                self.send(chat_id, "Compris, je reprends l'analyse.")
                return
            setting.set_value("telegram_waiting_capture", "")

        if self._va_en_conversation(text, image_path):
            self._repondre_en_conversation(chat_id, text)
            return

        capture = capture_model.create_capture(
            raw_text=text or None, image_path=image_path, source="telegram"
        )
        self.send(chat_id, "Bien recu, j'analyse.")
        logger.info("Capture %s creee depuis Telegram.", capture["id"])

    def _answer_replied_capture(self, chat_id, message, text):
        """Adresse une reponse Telegram a la capture du message cite.

        Passe avant la capture en attente implicite : quand plusieurs questions
        se croisent, seule la citation dit a laquelle on repond.

        :return: True si le message a ete consomme
        """
        cited = (message.get("reply_to_message") or {}).get("message_id")
        capture = capture_model.capture_for_telegram_message(cited)
        if capture is None:
            return False

        if capture["status"] in ("waiting", "failed"):
            triage.answer_capture(capture["id"], text)
            self.send(chat_id, "Compris, je reprends l'analyse.")
        elif capture["status"] in ("new", "processing"):
            self.send(chat_id, "Cette entree est deja en cours d'analyse, patiente un instant.")
        else:
            self.send(
                chat_id,
                "Cette entree est deja traitee. Modifie la tache dans NOMAIA,"
                " ou envoie une nouvelle entree.",
            )
        return True

    def _va_en_conversation(self, text, image_path):
        """Indique si un message doit etre traite comme une conversation.

        Une image releve toujours du triage : la conversation ne sait pas
        les lire. Un texte long aussi : un message client transfere est un
        depot, pas une question.
        """
        if image_path or not text:
            return False
        if setting.get("telegram_chat_mode") != "1":
            return False
        return len(text) <= setting.get_int("telegram_capture_chars")

    def _repondre_en_conversation(self, chat_id, text):
        """Fait repondre NOMAIA a un message libre. Appel long : 10 a 60 s.

        Un echec de moteur est annonce tel quel. Renvoyer le message au triage
        serait pire que rien : le triage cree des taches, il ne repond pas a une
        question, et une salutation y produit un « Aucune action identifiee »
        suivi d'une demande de validation. Deux messages de bruit au lieu d'une
        cause lisible.
        """
        from services import assistant

        try:
            resultat = assistant.reply_to(text, conversation_id=self._conversation_id())
        except Exception as error:
            logger.error("Conversation Telegram en echec : %s", error, exc_info=True)
            resultat = {"reply": None, "error": str(error)}

        if resultat.get("reply"):
            self.send(chat_id, resultat["reply"])
            return

        self.send(chat_id, self._motif_d_echec(resultat.get("error")))

    @staticmethod
    def _conversation_id():
        """Conversation suivie par Telegram : celle choisie, sinon la courante.

        Une conversation supprimee ou archivee depuis le PC n'est plus suivie :
        on retombe sur la courante plutot que d'ecrire dans un fil masque.
        """
        brut = setting.get("telegram_conversation_id")
        if brut:
            conversation = chat_model.get_conversation(int(brut))
            if conversation and not conversation["archived"]:
                return conversation["id"]
        identifiant = chat_model.current_id()
        setting.set_value("telegram_conversation_id", str(identifiant))
        return identifiant

    # Conversations listees par /convs : au-dela, la liste ne tient plus sur
    # un ecran de telephone.
    CONVERSATIONS_LISTED = 8

    def _list_conversations(self, chat_id):
        """Repond a /convs : les conversations recentes, la suivie marquee."""
        active = self._conversation_id()
        lignes = []
        for conversation in chat_model.list_conversations(limit=self.CONVERSATIONS_LISTED):
            marque = "> " if conversation["id"] == active else "  "
            lignes.append(
                f"{marque}{conversation['id']}. {conversation['title']}"
                f" ({conversation['updated_at'][:10]}, {conversation['message_count']} msg)"
            )
        self.send(
            chat_id,
            "Conversations recentes :\n" + "\n".join(lignes)
            + "\n\n/conv <numero> pour en reprendre une, /newconv pour en ouvrir une.",
        )

    def _switch_conversation(self, chat_id, argument):
        """Repond a /conv <n> : reprend une conversation et rappelle ou elle en etait."""
        try:
            conversation = chat_model.get_conversation(int(argument))
        except (TypeError, ValueError):
            conversation = None
        if conversation is None:
            self.send(chat_id, "Numero inconnu. /convs liste les conversations.")
            return
        if conversation["archived"]:
            chat_model.update_conversation(conversation["id"], archived=False)
        setting.set_value("telegram_conversation_id", str(conversation["id"]))

        rappel = chat_model.history(limit=2, conversation_id=conversation["id"])
        lignes = [
            ("Moi : " if item["role"] == "user" else "NOMAIA : ") + item["content"][:300]
            for item in rappel
        ]
        corps = "\n\n".join(lignes) if lignes else "(conversation vide)"
        self.send(chat_id, f"Conversation « {conversation['title']} » reprise.\n\n{corps}")

    def _new_conversation(self, chat_id, titre):
        """Repond a /newconv [titre] : ouvre une conversation et la suit."""
        conversation = chat_model.create_conversation(titre or None)
        setting.set_value("telegram_conversation_id", str(conversation["id"]))
        self.send(
            chat_id,
            f"Nouvelle conversation ouverte ({conversation['id']}). Je t'ecoute.",
        )

    @staticmethod
    def _motif_d_echec(erreur):
        """Traduit un echec de conversation en message actionnable.

        Un motif technique seul n'aide pas depuis un telephone : ce qui compte
        est de savoir ou aller le corriger.
        """
        detail = str(erreur or "").strip()
        repere = detail.lower()

        if "cle api" in repere or "aucune cle" in repere:
            return (
                "Je ne peux pas repondre : le moteur de conversation n'a pas de"
                " cle. Va dans Config > Intelligence > Conversation sur le PC.\n\n"
                "En attendant, /note <texte> depose une entree a trier."
            )

        if "503" in repere or "high demand" in repere or "overloaded" in repere:
            return (
                "Le moteur est surcharge en ce moment. Reessaie dans une"
                " minute, ou choisis un autre modele dans Config."
            )

        if "injoignable" in repere or "timeout" in repere:
            return "Moteur injoignable. Verifie la connexion, puis reessaie."

        return (
            "Je n'ai pas pu repondre : " + (detail[:200] or "cause inconnue")
            + "\n\n/note <texte> depose une entree a trier."
        )

    def _new_pair_code(self):
        """Tire un nouveau code d'appairage a six chiffres et remet les essais a zero."""
        code = f"{100000 + secrets.randbelow(900000)}"
        setting.set_value("telegram_pair_code", code)
        self._pair_failures = 0
        return code

    def _try_pairing(self, chat_id, text):
        """Associe le bot au premier chat qui fournit le bon code d'appairage.

        Apres PAIR_MAX_FAILURES codes faux, le code est remplace : deviner un
        code a six chiffres par essais successifs devient impraticable.
        """
        expected = setting.get("telegram_pair_code") or ""
        parts = text.split()

        if (
            expected
            and len(parts) == 2
            and parts[0] == "/pair"
            and secrets.compare_digest(parts[1].encode("utf-8"), expected.encode("utf-8"))
        ):
            setting.set_value("telegram_chat_id", chat_id)
            setting.set_value("telegram_pair_code", "")
            self._pair_failures = 0
            self.send(chat_id, "Appairage reussi.\n\n" + HELP_TEXT)
            logger.info("Telegram appaire avec le chat %s.", chat_id)
            return

        if parts[:1] == ["/pair"]:
            self._pair_failures += 1
            if self._pair_failures >= PAIR_MAX_FAILURES:
                self._new_pair_code()
                logger.warning("Telegram : trop de codes d'appairage faux, nouveau code tire.")
        self.send(chat_id, "Envoie /pair suivi du code affiche dans NOMAIA.")

    def _handle_command(self, chat_id, text):
        """Traite une commande. Retourne True si elle a ete consommee."""
        command = text.split()[0].lower().lstrip("/").split("@")[0]

        if command in ("start", "aide", "help"):
            self.send(chat_id, HELP_TEXT)
            return True

        if command == "plan":
            from services import heartbeat as heartbeat_service

            self.send(
                chat_id,
                heartbeat_service.morning_brief() or "Rien a planifier aujourd'hui.",
            )
            return True

        if command == "taches":
            self.send(chat_id, self._task_digest())
            return True

        if command == "bilan":
            self._send_review(chat_id)
            return True

        if command == "note":
            # Court-circuite la conversation : le texte part au triage.
            contenu = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if not contenu:
                self.send(chat_id, "Ecris ce que je dois trier apres /note.")
                return True
            capture = capture_model.create_capture(raw_text=contenu, source="telegram")
            self.send(chat_id, "Bien recu, j'analyse.")
            logger.info("Capture %s creee depuis Telegram (/note).", capture["id"])
            return True

        if command == "mail":
            contenu = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            try:
                texte = mail_parse.pasted_text(contenu)
            except ValueError as error:
                self.send(chat_id, str(error))
                return True
            capture = capture_model.create_capture(raw_text=texte, source="email")
            self.send(chat_id, "Mail recu, j'analyse. Rien ne sera cree sans ta validation.")
            logger.info("Capture %s creee depuis Telegram (/mail).", capture["id"])
            return True

        if command == "mails":
            self._read_mails(chat_id)
            return True

        if command == "convs":
            self._list_conversations(chat_id)
            return True

        if command == "conv":
            argument = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if not argument:
                self._list_conversations(chat_id)
            else:
                self._switch_conversation(chat_id, argument)
            return True

        if command == "newconv":
            titre = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            self._new_conversation(chat_id, titre)
            return True

        if command == "nouveau":
            setting.set_value("telegram_waiting_capture", "")
            self.send(chat_id, "D'accord, envoie la nouvelle entree.")
            return True

        return False

    def _read_mails(self, chat_id):
        """Lance une lecture des mails Outlook ; le resultat arrive a la fin.

        Un passage dure jusqu'a quelques minutes : il tourne dans son propre
        thread pour ne pas bloquer la reception des autres messages.
        """
        from services import mail_reader

        motif = mail_reader.refusal()
        if motif:
            self.send(chat_id, motif)
            return

        self.send(chat_id, "Je lis tes mails Outlook, reponse dans une a deux minutes.")
        thread = mail_reader.start_background(
            self.app, on_done=lambda resultat: self.send(chat_id, mail_reader.result_text(resultat))
        )
        if thread is None:
            self.send(chat_id, mail_reader.BUSY_MESSAGE)

    def _deposer_eml(self, chat_id, document, note):
        """Telecharge un fichier .eml et le depose comme capture de mail.

        :param note: legende eventuelle du fichier, transmise au triage
        """
        try:
            _, payload = self._fetch_file(document["file_id"], mail_parse.MAX_EML_BYTES)
            mail = mail_parse.parse_eml(payload)
        except (RuntimeError, ValueError) as error:
            self.send(chat_id, f"Mail non lu : {error}")
            return

        capture = capture_model.create_capture(
            raw_text=mail_parse.capture_text(mail, note), source="email"
        )
        objet = mail["subject"] or "sans objet"
        self.send(chat_id, f"Mail « {objet} » recu, j'analyse. Rien ne sera cree sans ta validation.")
        logger.info("Capture %s creee depuis un .eml Telegram.", capture["id"])

    def _send_review(self, chat_id):
        """Genere le bilan de la semaine et l'envoie. Appel long : 10 a 30 s."""
        from services import review as review_service
        from services.llm import LLMError

        self.send(chat_id, "Je regarde les chiffres de la semaine...")
        try:
            self.send(chat_id, review_service.as_text(review_service.generate()))
        except LLMError as error:
            self.send(chat_id, f"Bilan impossible pour l'instant : {error}")

    def _task_digest(self):
        """Construit le resume des taches en cours pour Telegram."""
        from models import task as task_model

        tasks = task_model.list_tasks(status="todo")
        if not tasks:
            return "Aucune tache en cours."

        lines = []
        for item in tasks[:15]:
            deadline = f" — {item['deadline']}" if item["deadline"] else ""
            lines.append(f"P{item['priority']} · {item['title']}{deadline}")

        remainder = len(tasks) - 15
        if remainder > 0:
            lines.append(f"… et {remainder} autre(s).")
        return "\n".join(lines)

    def _transcrire_vocal(self, chat_id, media):
        """Telecharge et transcrit un vocal. Retourne le texte, ou None.

        Chaque refus est annonce : un vocal ignore en silence laisserait croire
        que la consigne a ete prise en compte. L'audio est efface des la
        transcription faite, reussie ou non.
        """
        if setting.get("voice_enabled") != "1":
            self.send(chat_id, "Vocaux desactives. Active-les dans Config > Intelligence > Conversation.")
            return None

        limite = setting.get_int("voice_max_s")
        if int(media.get("duration") or 0) > limite:
            self.send(chat_id, f"Vocal trop long ({limite} s maximum). Coupe-le en plusieurs.")
            return None

        if not transcribe.is_available():
            self.send(chat_id, "Transcription indisponible sur le PC : faster-whisper absent.")
            return None

        # Indicateur natif d'activite : la transcription prend plusieurs
        # secondes, et un message d'attente de plus serait du bruit.
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, attempts=1)
        except RuntimeError:
            pass

        chemin = None
        try:
            chemin = self._download_voice(media["file_id"])
            texte = transcribe.transcribe(chemin)
        except (RuntimeError, transcribe.TranscriptionError) as error:
            logger.warning("Vocal non transcrit : %s", error)
            self.send(chat_id, f"Vocal non compris : {error}")
            return None
        finally:
            if chemin:
                Path(chemin).unlink(missing_ok=True)

        self.send(chat_id, f"Entendu : \"{texte}\"")
        return texte

    def _fetch_file(self, file_id, max_bytes):
        """Telecharge un fichier Telegram en memoire.

        :return: (chemin distant, contenu binaire)
        :raises RuntimeError: chemin absent, reseau en echec ou fichier trop lourd
        """
        info = self._call("getFile", {"file_id": file_id})
        remote_path = info.get("file_path")
        if not remote_path:
            raise RuntimeError("Chemin de fichier absent de la reponse Telegram.")

        url = f"{API_ROOT}/file/bot{self._token()}/{remote_path}"
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(str(error))

        if len(payload) > max_bytes:
            raise RuntimeError(f"Fichier trop lourd ({max_bytes // (1024 * 1024)} Mo maximum).")
        return remote_path, payload

    def _download_voice(self, file_id):
        """Telecharge un vocal Telegram dans public/uploads. Retourne son chemin."""
        remote_path, payload = self._fetch_file(file_id, MAX_VOICE_BYTES)

        folder = config.UPLOAD_DIR / "voice"
        folder.mkdir(parents=True, exist_ok=True)
        extension = Path(remote_path).suffix.lower() or ".ogg"
        target = folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_tg{extension}"
        target.write_bytes(payload)
        return str(target)

    def _download_photo(self, file_id):
        """Telecharge une photo Telegram dans public/uploads. Retourne son chemin."""
        remote_path, payload = self._fetch_file(file_id, config.MAX_IMAGE_BYTES)

        extension = Path(remote_path).suffix.lower() or ".jpg"
        if extension not in config.ALLOWED_IMAGE_EXT:
            raise RuntimeError(f"Format non accepte : {extension}")

        folder = config.UPLOAD_DIR / datetime.now().strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_tg{extension}"
        target.write_bytes(payload)
        return str(target)

    # --- Restitution -------------------------------------------------------

    def notify_result(self, capture, result):
        """Renvoie sur Telegram l'issue du triage d'une capture.

        Appele par le worker, dans un contexte applicatif deja ouvert.
        """
        chat_id = setting.get("telegram_chat_id")
        if not chat_id:
            return

        payload = result["proposal"]["payload"]
        outcome = result["outcome"]
        notified = capture.get("source") in NOTIFIED_SOURCES

        if outcome == "question":
            # Reponse implicite (sans citer le message) reservee aux entrees
            # parties du telephone : une question nee d'un depot sur le PC ne
            # doit pas capter le prochain message sans rapport.
            if notified:
                setting.set_value("telegram_waiting_capture", str(capture["id"]))
            self._send_about(chat_id, capture["id"], self._question_text(payload))
            return

        if not notified:
            return

        setting.set_value("telegram_waiting_capture", "")

        if outcome == "applied":
            lines = [
                f"P{task['priority']} · {task['title']}"
                + (f" — {task['deadline']}" if task["deadline"] else "")
                for task in result["tasks"]
            ]
            self.send(chat_id, f"{len(lines)} tache(s) creee(s) :\n" + "\n".join(lines))
            return

        lines = [f"P{task['priority']} · {task['title']}" for task in payload.get("tasks", [])]
        body = "\n".join(lines) if lines else "Aucune action identifiee."
        if capture.get("source") == "email":
            consigne = "Mail : rien n'est cree sans toi."
        else:
            consigne = "Pas assez sur de moi."
        consigne += " Valide dans NOMAIA, ou precise en repondant a ce message."
        entete = self._summary_and_reply(payload)
        self._send_about(chat_id, capture["id"], f"{entete}\n\n{body}\n\n{consigne}")

    @staticmethod
    def _summary_and_reply(payload):
        """Resume et message de NOMAIA, sans doublon quand ils se recoupent."""
        summary = (payload.get("summary") or "").strip()
        reply = (payload.get("reply") or "").strip()
        if reply and reply != summary:
            return f"{summary}\n\n{reply}" if summary else reply
        return summary

    def _question_text(self, payload):
        """Message complet d'une question : contexte, questions et suggestions."""
        lignes = []
        for index, item in enumerate(payload.get("questions", []), 1):
            lignes.append(f"{index}. {item['text']}")
            if item.get("suggestions"):
                lignes.append("   ex. : " + " / ".join(item["suggestions"]))
        corps = "\n".join(lignes)
        entete = self._summary_and_reply(payload)
        if entete:
            return f"{entete}\n\n{corps}\n\n{REPLY_HINT}"
        return f"{corps}\n\n{REPLY_HINT}"

    def notify_owner(self, text):
        """Envoie un message spontane au proprietaire du bot.

        Contrairement a notify_result, l'envoi n'est lie a aucune capture : il
        sert aux rendez-vous du battement de coeur. Silencieux tant que le bot
        n'est pas appaire.
        """
        chat_id = setting.get("telegram_chat_id")
        if not chat_id:
            return False
        self.send(chat_id, text)
        return True

    def notify_failure(self, capture, error):
        """Signale sur Telegram qu'une capture n'a pas pu etre traitee."""
        chat_id = setting.get("telegram_chat_id")
        if not chat_id or capture.get("source") not in NOTIFIED_SOURCES:
            return
        self._send_about(
            chat_id,
            capture["id"],
            f"Analyse impossible pour l'instant : {error}\n\nReponds a ce message pour relancer.",
        )

    # --- Instantane epingle -------------------------------------------------
    #
    # PC eteint, NOMAIA ne repond plus : c'est la limite d'un service local.
    # La parade ne coute rien : un unique message, edite a chaque changement et
    # epingle en tete du chat. Telegram le conserve sur ses serveurs, donc le
    # telephone l'affiche hors ligne, machine arretee, sans rien exposer de
    # plus que ce qui transite deja par le bot.

    # Delai minimal entre deux mises a jour de l'epingle. Cocher dix taches
    # d'affilee ne doit pas declencher dix appels a Telegram.
    DIGEST_DEBOUNCE_S = 60

    # Nombre de taches detaillees. Au-dela, le message devient illisible sur un
    # telephone et s'approche de la limite de Telegram.
    DIGEST_MAX_TASKS = 25

    # Rendez-vous detailles dans l'epingle. Au-dela, le message devient
    # un mur de texte illisible sur un telephone.
    DIGEST_MAX_EVENTS = 6

    def mark_digest_stale(self):
        """Signale qu'une ecriture a eu lieu : l'epingle sera rafraichie.

        Appelee depuis une requete HTTP : elle ne fait aucun appel reseau, sous
        peine de ralentir chaque clic de l'interface. C'est le battement de
        coeur qui pousse reellement la mise a jour.
        """
        self._digest_stale = True

    def refresh_digest_if_stale(self):
        """Pousse l'instantane si une ecriture a eu lieu et que le delai est passe."""
        if not getattr(self, "_digest_stale", False):
            return False

        dernier = getattr(self, "_digest_pushed_at", None)
        if dernier and (datetime.now() - dernier).total_seconds() < self.DIGEST_DEBOUNCE_S:
            return False

        return self.update_pinned_digest()

    def update_pinned_digest(self):
        """Met a jour, ou cree, le message epingle qui resume l'etat courant.

        :return: True si Telegram a accepte la mise a jour
        """
        chat_id = setting.get("telegram_chat_id")
        if not chat_id or setting.get("telegram_pinned_digest") != "1":
            return False

        texte = self._digest_text()[:MAX_MESSAGE_CHARS]
        message_id = setting.get("telegram_digest_message_id")

        self._digest_stale = False
        self._digest_pushed_at = datetime.now()

        if message_id:
            try:
                self._call(
                    "editMessageText",
                    {"chat_id": chat_id, "message_id": message_id, "text": texte},
                )
                return True
            except RuntimeError as error:
                # "message is not modified" : rien n'a change, c'est un succes.
                if "not modified" in str(error):
                    return True
                # Message supprime a la main, ou trop ancien pour etre edite :
                # on en repose un neuf plutot que d'abandonner l'epingle.
                logger.info("Instantane epingle a recreer : %s", error)

        return self._create_pinned_digest(chat_id, texte)

    def _create_pinned_digest(self, chat_id, texte):
        """Envoie un nouvel instantane et l'epingle en tete du chat."""
        try:
            envoye = self._call("sendMessage", {"chat_id": chat_id, "text": texte})
            message_id = str((envoye or {}).get("message_id") or "")
            if not message_id:
                return False

            setting.set_value("telegram_digest_message_id", message_id)
            # Sans notification : l'epingle change a chaque tache cochee, elle
            # ne doit pas faire sonner le telephone.
            self._call(
                "pinChatMessage",
                {"chat_id": chat_id, "message_id": message_id, "disable_notification": "true"},
            )
            return True
        except RuntimeError as error:
            self.last_error = str(error)
            logger.warning("Instantane epingle non publie : %s", error)
            return False

    def _digest_text(self):
        """Compose l'instantane : ce qui est en retard, du jour, puis le reste."""
        from datetime import date

        from models import task as task_model

        from models import event as event_model

        from services import workdays

        taches = task_model.list_tasks(status="todo")
        horodatage = datetime.now().strftime("%d/%m a %H:%M")

        # Un jour de repos se dit en tete : la liste de taches qui suit n'est
        # pas un programme pour aujourd'hui.
        motif = workdays.day_off_reason(date.today())
        repos = [f"Aujourd'hui : repos ({motif})"] if motif else []

        # Les rendez-vous du jour figurent en tete : PC eteint, c'est la seule
        # facon de savoir ce qui attend dans l'heure qui vient.
        rendez_vous = [
            item for item in event_model.list_for_day(date.today()) if item["kind"] != "absence"
        ]
        agenda = []
        if rendez_vous:
            agenda.append("")
            agenda.append(f"AGENDA ({len(rendez_vous)})")
            for item in rendez_vous[: self.DIGEST_MAX_EVENTS]:
                lieu = f" - {item['location']}" if item["location"] else ""
                agenda.append(
                    f"{item['starts_at'][11:16]}-{item['ends_at'][11:16]}"
                    f" {item['title']}{lieu}"
                )
            reste = len(rendez_vous) - self.DIGEST_MAX_EVENTS
            if reste > 0:
                agenda.append(f"... et {reste} autre(s).")

        if not taches:
            entete = f"NOMAIA - etat au {horodatage}"
            if agenda or repos:
                return "\n".join([entete] + repos + ["Aucune tache ouverte."] + agenda)
            return f"{entete}\n\nAucune tache ouverte."

        aujourdhui = date.today().isoformat()
        groupes = [
            ("EN RETARD", [t for t in taches if t["deadline"] and t["deadline"] < aujourdhui]),
            ("AUJOURD'HUI", [t for t in taches if t["deadline"] == aujourdhui]),
            ("A VENIR", [t for t in taches if t["deadline"] and t["deadline"] > aujourdhui]),
            ("SANS ECHEANCE", [t for t in taches if not t["deadline"]]),
        ]

        charge = sum(int(t["estimate_min"] or 0) for t in taches)
        lignes = [
            f"NOMAIA - etat au {horodatage}",
            f"{len(taches)} tache(s) ouverte(s), {charge // 60} h {charge % 60:02d} estimees",
        ]
        lignes.extend(repos)

        lignes.extend(agenda)

        restant = self.DIGEST_MAX_TASKS
        for titre, membres in groupes:
            if not membres or restant <= 0:
                continue
            lignes.append("")
            lignes.append(f"{titre} ({len(membres)})")
            for item in membres[:restant]:
                projet = f" [{item['project_name']}]" if item["project_name"] else ""
                echeance = f" - {item['deadline'][8:10]}/{item['deadline'][5:7]}" if item["deadline"] else ""
                lignes.append(f"P{item['priority']} {item['title']}{projet}{echeance}")
            reste = len(membres) - min(len(membres), restant)
            if reste > 0:
                lignes.append(f"... et {reste} autre(s).")
            restant -= len(membres)

        return "\n".join(lignes)
