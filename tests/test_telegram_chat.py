"""Tests de la conversation Telegram : aiguillage et echecs.

Un message depuis le telephone doit soit recevoir une reponse, soit une cause
lisible. Jamais un detour muet par le triage, qui cree des taches et ne repond
a aucune question.
"""
import unittest
from unittest.mock import patch

from models import capture as capture_model
from models import setting
from models import task as task_model
from services import assistant, telegram
from services.providers import ProviderError
from services.telegram import TelegramBridge
from tests.base import NomaiaTestCase


class BridgeEspion(TelegramBridge):
    """Passerelle qui note ce qu'elle enverrait au lieu de joindre Telegram."""

    def __init__(self, app):
        super().__init__(app)
        self.envois = []

    def _call(self, method, params=None, timeout=20, attempts=1):
        if method == "sendMessage":
            self.envois.append((params or {}).get("text", ""))
            return {"message_id": 1}
        return {}

    def dernier(self):
        """Retourne le dernier texte envoye, ou une chaine vide."""
        return self.envois[-1] if self.envois else ""


class MotifEchecTest(unittest.TestCase):
    """Le motif doit dire ou aller corriger, pas seulement ce qui a casse."""

    def test_cle_absente_renvoie_vers_la_config(self):
        texte = TelegramBridge._motif_d_echec("Aucune cle API Claude enregistree.")
        self.assertIn("Config", texte)
        self.assertIn("/note", texte)

    def test_surcharge_invite_a_reessayer(self):
        texte = TelegramBridge._motif_d_echec(
            "L'API Gemini a repondu 503 : This model is currently experiencing high demand"
        )
        self.assertIn("surcharge", texte.lower())
        self.assertNotIn("Config >", texte)

    def test_reseau_parle_de_connexion(self):
        texte = TelegramBridge._motif_d_echec("L'API Gemini injoignable : timeout")
        self.assertIn("connexion", texte.lower())

    def test_cause_inconnue_reste_lisible(self):
        texte = TelegramBridge._motif_d_echec("quelque chose d'inattendu")
        self.assertIn("inattendu", texte)
        self.assertIn("/note", texte)

    def test_erreur_absente_ne_plante_pas(self):
        self.assertTrue(TelegramBridge._motif_d_echec(None))


class ConversationTelegramTest(NomaiaTestCase):
    """Aiguillage d'un message recu et traitement des echecs."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "42")
        setting.set_value("telegram_chat_mode", "1")
        self.bridge = BridgeEspion(self.app)

    def _message(self, texte):
        return {"chat": {"id": 42}, "text": texte}

    def test_message_court_part_en_conversation(self):
        with patch.object(assistant, "reply_to", return_value={"reply": "Trois taches."}):
            self.bridge._handle(self._message("combien de taches ?"))

        self.assertEqual(self.bridge.dernier(), "Trois taches.")
        # Aucune capture : une question n'est pas un depot.
        self.assertEqual(capture_model.list_captures(), [])

    def test_message_long_part_au_triage(self):
        setting.set_value("telegram_capture_chars", "100")
        self.bridge._handle(self._message("x" * 200))

        self.assertIn("analyse", self.bridge.dernier().lower())
        self.assertEqual(len(capture_model.list_captures()), 1)

    def test_mode_conversation_coupe(self):
        setting.set_value("telegram_chat_mode", "0")
        self.bridge._handle(self._message("salut"))

        self.assertEqual(len(capture_model.list_captures()), 1)

    def test_commande_note_force_le_triage(self):
        self.bridge._handle(self._message("/note corriger le crash paie"))

        captures = capture_model.list_captures()
        self.assertEqual(len(captures), 1)
        self.assertIn("crash paie", captures[0]["raw_text"])

    def test_note_sans_texte_est_refusee(self):
        self.bridge._handle(self._message("/note"))
        self.assertEqual(capture_model.list_captures(), [])
        self.assertIn("Ecris", self.bridge.dernier())

    def test_echec_de_moteur_dit_la_cause_sans_creer_de_capture(self):
        # Le bug corrige : une salutation finissait au triage, qui repondait
        # « Aucune action identifiee » puis demandait une validation.
        with patch.object(
            assistant, "reply_to", return_value={"reply": None, "error": "Aucune cle API enregistree."}
        ):
            self.bridge._handle(self._message("salut nomaia"))

        self.assertIn("Config", self.bridge.dernier())
        self.assertEqual(capture_model.list_captures(), [])

    def test_exception_inattendue_reste_lisible(self):
        with patch.object(assistant, "reply_to", side_effect=RuntimeError("boum")):
            self.bridge._handle(self._message("salut"))

        self.assertIn("boum", self.bridge.dernier())
        self.assertEqual(capture_model.list_captures(), [])

    def test_le_plan_en_attente_part_detaille(self):
        # Sur telephone il n'y a que ce texte : les etapes doivent y figurer.
        tache = task_model.create_task({"title": "Planifier la reunion"})
        charge = {
            "plan": [
                {"tool": "complete_task", "args": {"task_id": tache["id"]},
                 "label": "Terminer « Planifier la reunion »"},
                {"tool": "create_task", "args": {"title": "Preparer"}, "label": "Creer « Preparer »"},
            ],
            "reply": "Deux actions.",
        }

        with patch.object(assistant.llm, "ask", lambda prompt, **kwargs: charge):
            self.bridge._handle(self._message("la reunion est pour demain"))

        texte = self.bridge.dernier()
        self.assertIn("Terminer « Planifier la reunion »", texte)
        self.assertIn("oui", texte)
        # Rien n'a encore bouge.
        self.assertEqual(task_model.get_task(tache["id"])["status"], "todo")

    def test_le_oui_depuis_telegram_applique_le_plan(self):
        tache = task_model.create_task({"title": "Planifier la reunion"})
        charge = {
            "plan": [
                {"tool": "complete_task", "args": {"task_id": tache["id"]}, "label": "Terminer"},
                {"tool": "create_task", "args": {"title": "Preparer"}, "label": "Creer"},
            ],
            "reply": "Deux actions.",
        }

        with patch.object(assistant.llm, "ask", lambda prompt, **kwargs: charge):
            self.bridge._handle(self._message("la reunion est pour demain"))
            self.bridge._handle(self._message("oui"))

        self.assertEqual(task_model.get_task(tache["id"])["status"], "done")
        self.assertIn("Fait", self.bridge.dernier())

    def test_un_chat_non_appaire_est_ignore(self):
        self.bridge._handle({"chat": {"id": 999}, "text": "laisse moi entrer"})
        self.assertEqual(self.bridge.envois, [])
        self.assertEqual(capture_model.list_captures(), [])


class AppairageTest(NomaiaTestCase):
    """Le code d'appairage ne se devine pas par essais successifs."""

    def setUp(self):
        super().setUp()
        self.bridge = BridgeEspion(self.app)
        self.code = self.bridge._new_pair_code()

    def test_bon_code_appaire(self):
        self.bridge._handle({"chat": {"id": 7}, "text": "/pair " + self.code})
        self.assertEqual(setting.get("telegram_chat_id"), "7")
        self.assertEqual(setting.get("telegram_pair_code"), "")

    def test_code_remplace_apres_trop_d_echecs(self):
        faux = "000000" if self.code != "000000" else "111111"
        for _ in range(telegram.PAIR_MAX_FAILURES):
            self.bridge._handle({"chat": {"id": 7}, "text": "/pair " + faux})
        nouveau = setting.get("telegram_pair_code")
        self.assertNotEqual(nouveau, self.code)
        self.assertRegex(nouveau, r"^[1-9][0-9]{5}$")
        self.bridge._handle({"chat": {"id": 7}, "text": "/pair " + self.code})
        self.assertFalse(setting.get("telegram_chat_id"))


if __name__ == "__main__":
    unittest.main()
