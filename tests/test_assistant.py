"""Tests de la conversation : boucle d'outils, protocole, garde-fous.

Le fournisseur est toujours remplace par une fonction locale : aucun test
n'appelle un moteur reel, ni ne depend du reseau.
"""
import json
import unittest
from unittest.mock import patch

from models import chat as chat_model
from models import setting
from models import task as task_model
from services import assistant, tools
from services.providers import ProviderError
from tests.base import NomaiaTestCase


def scenario(*reponses):
    """Retourne un faux llm.ask rendant les reponses fournies, dans l'ordre.

    Chaque appel consomme une reponse. La derniere est rejouee si la boucle
    demande plus de tours que prevu, ce qui rend visible un enchainement
    inattendu au lieu de faire echouer le test sur un IndexError.
    """
    etat = {"appels": 0, "prompts": []}

    def faux_ask(prompt, **kwargs):
        etat["prompts"].append(prompt)
        index = min(etat["appels"], len(reponses) - 1)
        etat["appels"] += 1
        return reponses[index]

    faux_ask.etat = etat
    return faux_ask


class DecisionTest(unittest.TestCase):
    """La sortie du modele est une donnee hostile jusqu'a preuve du contraire."""

    def test_reponse_finale_reconnue(self):
        nom, args, plan, finale = assistant._decision({"reply": "Trois taches."})
        self.assertIsNone(nom)
        self.assertIsNone(plan)
        self.assertEqual(finale, "Trois taches.")

    def test_appel_d_outil_reconnu(self):
        nom, args, plan, finale = assistant._decision({"tool": "list_tasks", "args": {"limit": 3}})
        self.assertEqual(nom, "list_tasks")
        self.assertEqual(args, {"limit": 3})
        self.assertIsNone(plan)
        self.assertIsNone(finale)

    def test_arguments_absents_valent_dictionnaire_vide(self):
        nom, args, _, _ = assistant._decision({"tool": "current_timer"})
        self.assertEqual(args, {})

    def test_arguments_mal_types_valent_dictionnaire_vide(self):
        nom, args, _, _ = assistant._decision({"tool": "list_tasks", "args": "n'importe quoi"})
        self.assertEqual(args, {})

    def test_reponse_vide_est_sans_decision(self):
        vide = (None, None, None, None)
        self.assertEqual(assistant._decision({}), vide)
        self.assertEqual(assistant._decision({"reply": "   "}), vide)
        self.assertEqual(assistant._decision(["liste"]), vide)

    def test_la_reponse_finale_prime_sur_l_outil(self):
        # Un modele qui rend les deux a tranche : on ne relance pas d'outil.
        nom, _, _, finale = assistant._decision({"reply": "Fini.", "tool": "list_tasks"})
        self.assertIsNone(nom)
        self.assertEqual(finale, "Fini.")


class ReplyToTest(NomaiaTestCase):
    """Boucle complete : question, outils, reponse, enregistrement du fil."""

    def test_reponse_directe_sans_outil(self):
        faux = scenario({"reply": "Bonjour."})
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("salut")

        self.assertEqual(resultat["reply"], "Bonjour.")
        self.assertEqual(resultat["tools"], [])
        self.assertEqual(faux.etat["appels"], 1)

    def test_un_outil_puis_reponse(self):
        task_model.create_task({"title": "Corriger le crash paie"})
        faux = scenario(
            {"tool": "list_tasks", "args": {}},
            {"reply": "Une tache ouverte."},
        )
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("qu'est-ce que j'ai ?")

        self.assertEqual(resultat["tools"], ["list_tasks"])
        self.assertEqual(resultat["reply"], "Une tache ouverte.")
        # Le resultat de l'outil est bien reinjecte dans le second appel.
        self.assertIn("Resultat de l'outil list_tasks", faux.etat["prompts"][1])

    def test_le_fil_enregistre_la_question_et_la_reponse(self):
        faux = scenario({"reply": "Compris."})
        with patch.object(assistant.llm, "ask", faux):
            assistant.reply_to("note ca")

        fil = chat_model.history()
        self.assertEqual([item["role"] for item in fil], ["user", "assistant"])
        self.assertEqual(fil[0]["content"], "note ca")
        self.assertEqual(fil[1]["content"], "Compris.")

    def test_outil_inconnu_n_interrompt_pas_le_tour(self):
        faux = scenario(
            {"tool": "lancer_les_missiles", "args": {}},
            {"reply": "Cet outil n'existe pas."},
        )
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("fais un truc")

        self.assertEqual(resultat["reply"], "Cet outil n'existe pas.")
        self.assertIn('"ok": false', faux.etat["prompts"][1].lower())

    def test_plafond_de_tours_respecte(self):
        setting.set_value("chat_max_steps", "3")
        # Le modele boucle sans jamais conclure.
        faux = scenario({"tool": "current_timer", "args": {}})
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("tourne en rond")

        self.assertEqual(faux.etat["appels"], 3)
        self.assertEqual(resultat["reply"], assistant.FALLBACK_REPLY)

    def test_hors_protocole_declenche_un_rappel(self):
        faux = scenario({"bavardage": "je reponds n'importe quoi"}, {"reply": "Voila."})
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("bonjour")

        self.assertEqual(resultat["reply"], "Voila.")
        self.assertIn("format", faux.etat["prompts"][1])

    def test_panne_du_moteur_ne_salit_pas_le_fil(self):
        def tombe(prompt, **kwargs):
            raise ProviderError("Moteur injoignable", transient=True)

        with patch.object(assistant.llm, "ask", tombe):
            resultat = assistant.reply_to("une question")

        self.assertIsNone(resultat["reply"])
        self.assertIn("injoignable", resultat["error"])
        # Une question sans reponse ne doit pas rester dans le fil.
        self.assertEqual(chat_model.history(), [])

    def test_message_vide_refuse(self):
        with self.assertRaises(ValueError):
            assistant.reply_to("   ")

    def test_l_historique_est_plafonne(self):
        setting.set_value("chat_history_turns", "2")
        for index in range(5):
            chat_model.add("user", f"question {index}")
            chat_model.add("assistant", f"reponse {index}")

        recu = {}

        def capture_historique(prompt, **kwargs):
            recu["history"] = kwargs.get("history")
            return {"reply": "ok"}

        with patch.object(assistant.llm, "ask", capture_historique):
            assistant.reply_to("derniere question")

        self.assertEqual(len(recu["history"]), 4)
        self.assertEqual(recu["history"][-1]["content"], "reponse 4")


class SystemPromptTest(NomaiaTestCase):
    """Le prompt systeme porte le catalogue et l'etat du jour."""

    def test_le_catalogue_est_injecte(self):
        systeme = assistant.build_system()
        self.assertIn("list_tasks", systeme)
        self.assertIn("create_task", systeme)

    def test_les_outils_destructifs_sont_marques(self):
        # La suppression et la fusion existent, mais elles sont etiquetees
        # destructives : le serveur impose alors une confirmation, quoi que
        # le modele decide. C'est ce marqueur qui porte la garantie, pas le
        # prompt, qu'un modele peut oublier.
        for nom in ("delete_task", "merge_tasks", "merge_people"):
            self.assertIn(nom, tools.TOOLS, nom)
            self.assertTrue(tools.TOOLS[nom].get("destructive"), nom)
            self.assertTrue(assistant.needs_confirmation(
                [{"tool": nom, "args": {}, "label": nom, "destructive": True}]
            ))

    def test_le_contexte_cite_les_projets_actifs(self):
        from models import project as project_model

        project_model.create_project("ALPHA")
        self.assertIn("ALPHA", assistant.build_system())


class ProviderWarningTest(NomaiaTestCase):
    """Le fournisseur trop couteux doit etre signale, pas bloque."""

    def test_le_cli_est_signale(self):
        setting.set_value("chat_provider", "claude_cli")
        self.assertIsNotNone(assistant.provider_warning())

    def test_une_interface_de_programmation_ne_l_est_pas(self):
        setting.set_value("chat_provider", "anthropic")
        self.assertIsNone(assistant.provider_warning())

    def test_reglage_vide_retombe_sur_le_triage(self):
        setting.set_value("chat_provider", "")
        setting.set_value("llm_provider", "gemini")
        self.assertEqual(assistant.llm.chat_provider(), "gemini")


class ChatApiTest(NomaiaTestCase):
    """Routes HTTP de la conversation, garde-fou anti-CSRF compris."""

    def test_historique_vide_au_depart(self):
        reponse = self.client().get("/api/chat")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["messages"], [])

    def test_envoi_et_reponse(self):
        faux = scenario({"reply": "Trois taches ouvertes."})
        with patch.object(assistant.llm, "ask", faux):
            reponse = self.client().post(
                "/api/chat",
                json={"message": "combien de taches ?"},
                headers=self.write_headers(),
            )

        self.assertEqual(reponse.status_code, 200)
        corps = reponse.get_json()
        self.assertEqual(corps["reply"], "Trois taches ouvertes.")
        self.assertEqual(len(corps["messages"]), 2)

    def test_message_vide_refuse(self):
        reponse = self.client().post(
            "/api/chat", json={"message": "  "}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_message_trop_long_renvoye_au_triage(self):
        reponse = self.client().post(
            "/api/chat",
            json={"message": "x" * (chat_model.MAX_CONTENT_CHARS + 1)},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("triage", reponse.get_json()["error"])

    def test_panne_du_moteur_rend_503(self):
        def tombe(prompt, **kwargs):
            raise ProviderError("Moteur injoignable")

        with patch.object(assistant.llm, "ask", tombe):
            reponse = self.client().post(
                "/api/chat", json={"message": "salut"}, headers=self.write_headers()
            )

        self.assertEqual(reponse.status_code, 503)

    def test_ecriture_depuis_un_autre_site_refusee(self):
        # Sans ce garde-fou, une page web ouverte dans le navigateur pourrait
        # faire agir NOMAIA sur les taches a l'insu de l'utilisateur.
        reponse = self.client().post(
            "/api/chat",
            json={"message": "supprime tout"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(reponse.status_code, 403)

    def test_ecriture_sans_cle_d_acces_refusee(self):
        reponse = self.app.test_client().post(
            "/api/chat", json={"message": "salut"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 401)

    def test_effacement_du_fil(self):
        chat_model.add("user", "une question")
        reponse = self.client().delete("/api/chat", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(chat_model.history(), [])


class ChatModelTest(NomaiaTestCase):
    """Stockage du fil : troncature, roles, plafond de contexte."""

    def test_role_inconnu_refuse(self):
        with self.assertRaises(ValueError):
            chat_model.add("systeme", "contenu")

    def test_contenu_tronque(self):
        chat_model.add("user", "x" * (chat_model.MAX_CONTENT_CHARS + 500))
        self.assertEqual(len(chat_model.history()[0]["content"]), chat_model.MAX_CONTENT_CHARS)

    def test_outils_serialises_et_relus(self):
        chat_model.add("assistant", "fait", tools=["list_tasks", "get_plan"])
        self.assertEqual(chat_model.history()[0]["tools"], ["list_tasks", "get_plan"])

    def test_contexte_rend_le_plus_ancien_en_premier(self):
        chat_model.add("user", "premiere")
        chat_model.add("assistant", "deuxieme")
        contexte = chat_model.context(turns=5)
        self.assertEqual([item["content"] for item in contexte], ["premiere", "deuxieme"])


class ProviderHistoryTest(unittest.TestCase):
    """Les quatre fournisseurs acceptent un historique de conversation."""

    HISTORIQUE = [
        {"role": "user", "content": "premiere question"},
        {"role": "assistant", "content": "premiere reponse"},
        {"role": "inconnu", "content": "a ecarter"},
        {"role": "user", "content": "   "},
    ]

    def test_les_entrees_invalides_sont_ecartees(self):
        from services import providers

        retenus = providers._history({"history": self.HISTORIQUE})
        self.assertEqual(len(retenus), 2)
        self.assertEqual(retenus[0]["role"], "user")

    def test_historique_absent_rend_une_liste_vide(self):
        from services import providers

        self.assertEqual(providers._history({}), [])

    def test_gemini_renomme_le_role_assistant(self):
        from services import providers

        contenus = providers._contenus_gemini({"history": self.HISTORIQUE})
        self.assertEqual([item["role"] for item in contenus], ["user", "model"])


if __name__ == "__main__":
    unittest.main()
