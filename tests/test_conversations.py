"""Tests des conversations separees du chat, cote web et cote Telegram.

Chaque conversation garde son fil et son plan en attente ; aucune ne deborde
sur une autre, et NOMAIA ne consulte une autre conversation qu'a la demande.
"""
import sqlite3
from unittest.mock import patch

import config
from models import chat as chat_model
from models import init_schema, setting
from models import task as task_model
from services import assistant, tools
from services.telegram import TelegramBridge
from tests.base import NomaiaTestCase
from tests.test_chat_plan import scenario


class FilsSeparesTest(NomaiaTestCase):
    """Historique, titre et conversation courante."""

    def test_historiques_independants(self):
        a = chat_model.create_conversation("ALPHA")["id"]
        b = chat_model.create_conversation("ORION")["id"]
        chat_model.add("user", "question alpha", conversation_id=a)
        chat_model.add("user", "question orion", conversation_id=b)

        self.assertEqual([m["content"] for m in chat_model.history(conversation_id=a)], ["question alpha"])
        self.assertEqual([m["content"] for m in chat_model.context(5, conversation_id=b)][0], "question orion")

    def test_titre_tire_du_premier_message(self):
        conversation = chat_model.create_conversation()
        chat_model.add("user", "Où en est le chiffrage du module paie ALPHA pour la semaine prochaine ?",
                       conversation_id=conversation["id"])
        chat_model.add("user", "autre chose", conversation_id=conversation["id"])

        titre = chat_model.get_conversation(conversation["id"])["title"]
        self.assertTrue(titre.startswith("Où en est le chiffrage"))
        self.assertLessEqual(len(titre), chat_model.TITLE_MAX_CHARS)

    def test_courante_est_la_derniere_active_hors_archives(self):
        a = chat_model.create_conversation("A")["id"]
        b = chat_model.create_conversation("B")["id"]
        chat_model.add("user", "relance", conversation_id=a)
        get = chat_model.current_id
        # Meme seconde : l'identifiant le plus recent departage, d'ou le message.
        self.assertEqual(get(), a)

        chat_model.update_conversation(a, archived=True)
        self.assertEqual(get(), b)

    def test_conversation_inconnue_refusee(self):
        with self.assertRaises(chat_model.ConversationError):
            chat_model.history(conversation_id=999)

    def test_suppression_emporte_le_fil(self):
        conversation = chat_model.create_conversation("A")["id"]
        chat_model.add("user", "x", conversation_id=conversation)
        self.assertTrue(chat_model.delete_conversation(conversation))
        self.assertIsNone(chat_model.get_conversation(conversation))


class PlanParConversationTest(NomaiaTestCase):
    """Un « oui » ne s'applique qu'au plan de sa conversation."""

    def test_oui_ailleurs_n_applique_rien(self):
        tache = task_model.create_task({"title": "A supprimer"})
        a = chat_model.create_conversation("A")["id"]
        b = chat_model.create_conversation("B")["id"]
        chat_model.set_plan(
            [{"tool": "delete_task", "args": {"task_id": tache["id"]}, "label": "Supprimer", "destructive": True}],
            "J'y vais ?",
            conversation_id=a,
        )

        with patch.object(assistant.llm, "ask", scenario({"reply": "De quoi parles-tu ?"})):
            assistant.reply_to("oui", conversation_id=b)

        self.assertIsNotNone(task_model.get_task(tache["id"]))
        self.assertIsNotNone(chat_model.pending_plan(conversation_id=a))

        assistant.reply_to("oui", conversation_id=a)
        self.assertIsNone(task_model.get_task(tache["id"]))


class MigrationTest(NomaiaTestCase):
    """L'historique du fil unique d'avant n'est pas perdu."""

    def test_messages_orphelins_rattaches(self):
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("INSERT INTO chat_message (role, content) VALUES ('user', 'ancien message')")
        conn.commit()
        conn.close()

        init_schema()

        conversations = chat_model.list_conversations()
        origine = [c for c in conversations if c["title"] == "Conversation d'origine"]
        self.assertEqual(len(origine), 1)
        self.assertEqual(
            [m["content"] for m in chat_model.history(conversation_id=origine[0]["id"])],
            ["ancien message"],
        )

        init_schema()
        self.assertEqual(
            len([c for c in chat_model.list_conversations() if c["title"] == "Conversation d'origine"]), 1
        )


class OutilsTest(NomaiaTestCase):
    """Consultation d'une autre conversation, a la demande."""

    def setUp(self):
        super().setUp()
        self.a = chat_model.create_conversation("Chiffrage ALPHA")["id"]
        chat_model.add("user", "Le chiffrage paie ALPHA est de 12 jours", conversation_id=self.a)
        chat_model.add("assistant", "Note : 12 jours pour la paie.", conversation_id=self.a)

    def test_recherche(self):
        resultat = tools.execute("search_conversations", {"query": "chiffrage paie"})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["conversations"][0]["conversation_id"], self.a)

    def test_lecture(self):
        resultat = tools.execute("read_conversation", {"conversation_id": self.a})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(len(resultat["data"]["messages"]), 2)

    def test_lecture_inconnue(self):
        self.assertFalse(tools.execute("read_conversation", {"conversation_id": 999})["ok"])


class ApiTest(NomaiaTestCase):
    """Routes des conversations."""

    def test_cycle_complet(self):
        client = self.client()
        cree = client.post("/api/conversations", json={"title": "ORION"}, headers=self.write_headers())
        self.assertEqual(cree.status_code, 201)
        conversation_id = cree.get_json()["id"]

        with patch.object(assistant.llm, "ask", scenario({"reply": "Bonjour."})):
            reponse = client.post(
                "/api/chat",
                json={"message": "salut", "conversation_id": conversation_id},
                headers=self.write_headers(),
            )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["conversation"]["id"], conversation_id)

        fil = client.get(f"/api/chat?conversation_id={conversation_id}").get_json()
        self.assertEqual(len(fil["messages"]), 2)

        renomme = client.patch(
            f"/api/conversations/{conversation_id}", json={"title": "ORION recette"}, headers=self.write_headers()
        )
        self.assertEqual(renomme.get_json()["title"], "ORION recette")

        self.assertEqual(
            client.delete(f"/api/conversations/{conversation_id}", headers=self.write_headers()).status_code, 200
        )
        self.assertEqual(client.get(f"/api/chat?conversation_id={conversation_id}").status_code, 404)


class BridgeEspion(TelegramBridge):
    """Passerelle qui note ce qu'elle enverrait au lieu de joindre Telegram."""

    def __init__(self, app):
        super().__init__(app)
        self.envois = []

    def _call(self, method, params=None, timeout=20, attempts=1):
        if method == "sendMessage":
            self.envois.append((params or {}).get("text", ""))
            return {"message_id": len(self.envois)}
        return {}


class TelegramTest(NomaiaTestCase):
    """Commandes /convs, /conv et /newconv."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "42")
        setting.set_value("telegram_chat_mode", "1")
        self.bridge = BridgeEspion(self.app)

    def _message(self, texte):
        return {"chat": {"id": 42}, "text": texte}

    def test_nouvelle_conversation_suivie(self):
        self.bridge._handle(self._message("/newconv Recette ORION"))
        active = int(setting.get("telegram_conversation_id"))
        self.assertEqual(chat_model.get_conversation(active)["title"], "Recette ORION")

        with patch.object(assistant, "reply_to", return_value={"reply": "ok"}) as reponse:
            self.bridge._handle(self._message("on en est ou ?"))
        self.assertEqual(reponse.call_args.kwargs["conversation_id"], active)

    def test_liste_et_reprise(self):
        a = chat_model.create_conversation("ALPHA")["id"]
        chat_model.add("user", "point paie", conversation_id=a)
        chat_model.add("assistant", "12 jours", conversation_id=a)
        chat_model.create_conversation("ORION")

        self.bridge._handle(self._message("/convs"))
        self.assertIn("ALPHA", self.bridge.envois[-1])

        self.bridge._handle(self._message(f"/conv {a}"))
        self.assertEqual(int(setting.get("telegram_conversation_id")), a)
        self.assertIn("12 jours", self.bridge.envois[-1])

    def test_numero_inconnu(self):
        self.bridge._handle(self._message("/conv 999"))
        self.assertIn("inconnu", self.bridge.envois[-1])

    def test_conversation_supprimee_retombe_sur_la_courante(self):
        self.bridge._handle(self._message("/newconv"))
        supprimee = int(setting.get("telegram_conversation_id"))
        chat_model.delete_conversation(supprimee)
        self.assertNotEqual(self.bridge._conversation_id(), supprimee)
