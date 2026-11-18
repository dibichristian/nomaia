"""Tests du dialogue de triage via Telegram : relais complet et reponse citee.

Une question de NOMAIA doit arriver entiere sur le telephone, et repondre a un
message precis doit poursuivre l'entree dont il parle, meme si d'autres
questions se sont intercalees.
"""
from models import capture as capture_model
from models import setting
from services.telegram import TelegramBridge
from tests.base import NomaiaTestCase


class BridgeNumerote(TelegramBridge):
    """Passerelle qui note ses envois et leur attribue un identifiant."""

    def __init__(self, app):
        super().__init__(app)
        self.envois = []

    def _call(self, method, params=None, timeout=20, attempts=1):
        if method == "sendMessage":
            self.envois.append((params or {}).get("text", ""))
            return {"message_id": 100 + len(self.envois)}
        return {}

    def dernier(self):
        """Retourne le dernier texte envoye, ou une chaine vide."""
        return self.envois[-1] if self.envois else ""


def _resultat(outcome, **payload):
    """Construit une issue de triage minimale."""
    base = {"summary": "Relance client Alpha", "reply": "", "questions": [], "tasks": []}
    base.update(payload)
    return {"outcome": outcome, "proposal": {"payload": base}, "tasks": []}


QUESTION = {
    "key": "q1",
    "text": "Pour quand ?",
    "suggestions": ["demain", "vendredi"],
}


class RelaisTelegramTest(NomaiaTestCase):
    """Contenu des messages envoyes et liens message -> capture."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "42")
        setting.set_value("telegram_chat_mode", "1")
        self.bridge = BridgeNumerote(self.app)

    def _message(self, texte, cite=None):
        message = {"chat": {"id": 42}, "text": texte}
        if cite:
            message["reply_to_message"] = {"message_id": cite}
        return message

    def test_question_relayee_avec_reponse_et_suggestions(self):
        capture = capture_model.create_capture(raw_text="relancer alpha", source="telegram")
        self.bridge.notify_result(
            capture, _resultat("question", reply="Je prepare la relance.", questions=[QUESTION])
        )

        texte = self.bridge.dernier()
        self.assertIn("Je prepare la relance.", texte)
        self.assertIn("Pour quand ?", texte)
        self.assertIn("demain / vendredi", texte)

    def test_question_d_une_entree_web_relayee_sans_capter_la_suite(self):
        capture = capture_model.create_capture(raw_text="relancer alpha", source="web")
        self.bridge.notify_result(capture, _resultat("question", questions=[QUESTION]))

        self.assertIn("Pour quand ?", self.bridge.dernier())
        self.assertEqual(setting.get("telegram_waiting_capture") or "", "")

    def test_issue_web_hors_question_reste_silencieuse(self):
        capture = capture_model.create_capture(raw_text="relancer alpha", source="web")
        self.bridge.notify_result(capture, _resultat("review", tasks=[]))

        self.assertEqual(self.bridge.envois, [])

    def test_reponse_citee_poursuit_la_bonne_entree(self):
        premiere = capture_model.create_capture(raw_text="relancer alpha", source="telegram")
        seconde = capture_model.create_capture(raw_text="audit orion", source="telegram")
        for capture in (premiere, seconde):
            capture_model.set_status(capture["id"], "waiting")
            self.bridge.notify_result(capture, _resultat("question", questions=[QUESTION]))

        # Le premier message porte l'identifiant 101, le second 102.
        self.bridge._handle(self._message("demain", cite=101))

        self.assertEqual(capture_model.get_capture(premiere["id"])["status"], "new")
        self.assertEqual(capture_model.get_capture(seconde["id"])["status"], "waiting")
        reponses = [m["content"] for m in capture_model.list_messages(premiere["id"])]
        self.assertIn("demain", reponses)
        self.assertEqual(len(capture_model.list_captures()), 2)

    def test_precision_citee_sur_une_proposition_peu_sure(self):
        capture = capture_model.create_capture(raw_text="truc a faire", source="email")
        capture_model.set_status(capture["id"], "waiting")
        self.bridge.notify_result(capture, _resultat("review", tasks=[]))

        self.assertIn("repondant a ce message", self.bridge.dernier())
        self.bridge._handle(self._message("c'est pour le projet Orion", cite=101))

        self.assertEqual(capture_model.get_capture(capture["id"])["status"], "new")

    def test_reponse_citee_sur_une_entree_traitee_ne_cree_rien(self):
        capture = capture_model.create_capture(raw_text="relancer alpha", source="telegram")
        capture_model.link_telegram_message(555, capture["id"])
        capture_model.set_status(capture["id"], "done")

        self.bridge._handle(self._message("encore une chose", cite=555))

        self.assertIn("deja traitee", self.bridge.dernier())
        self.assertEqual(len(capture_model.list_captures()), 1)

    def test_citation_inconnue_suit_le_chemin_normal(self):
        setting.set_value("telegram_chat_mode", "0")
        self.bridge._handle(self._message("nouvelle entree", cite=999))

        self.assertEqual(len(capture_model.list_captures()), 1)

    def test_echec_lie_permet_de_relancer(self):
        capture = capture_model.create_capture(raw_text="relancer alpha", source="telegram")
        capture_model.set_status(capture["id"], "failed", error="moteur")
        self.bridge.notify_failure(capture, "moteur indisponible")

        self.bridge._handle(self._message("reessaie", cite=101))

        self.assertEqual(capture_model.get_capture(capture["id"])["status"], "new")
