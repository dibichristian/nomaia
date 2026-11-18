"""Sources cliquables des reponses du chat : tirees des outils, filtrees par la reponse."""
from unittest.mock import patch

from models import chat as chat_model
from models import project as project_model
from models import task as task_model
from services import assistant, chat_sources
from tests.base import NomaiaTestCase
from tests.test_assistant import scenario


class ExtractionTest(NomaiaTestCase):
    """Reconnaissance des elements dans les resultats d'outils."""

    def test_seuls_les_elements_cites_deviennent_des_liens(self):
        resultat = {"ok": True, "data": {"tasks": [
            {"id": 12, "title": "Corriger l'export de la paie", "status": "todo"},
            {"id": 13, "title": "Relancer Lucie pour le PV", "status": "todo"},
            {"id": 14, "title": "Préparer la démo Nordik", "status": "todo"},
        ]}}
        sources = chat_sources.from_results([resultat], "Commence par corriger l'EXPORT de la paie, puis #14.")
        self.assertEqual([(s["kind"], s["id"]) for s in sources], [("task", 12), ("task", 14)])
        self.assertEqual(sources[0]["url"], "/plan?tache=12")
        self.assertEqual(sources[0]["kind_label"], "Tâche")

    def test_accents_et_titre_tronque_par_l_outil(self):
        resultat = {"data": {"slots": [{"start": "09:00", "task": {
            "id": 7, "title": "Tester la déclaration FNE de la facture Vortex du 19/10/2026 et inform…"}}]}}
        sources = chat_sources.from_results([resultat], "Ce matin : tester la declaration FNE de la facture Vortex.")
        self.assertEqual(sources, [{
            "kind": "task", "kind_label": "Tâche", "id": 7, "url": "/plan?tache=7",
            "label": "Tester la déclaration FNE de la facture Vortex du 19/10/2026 et inform",
        }])

    def test_projets_contacts_evenements_conversations(self):
        resultats = [
            {"data": {"projects": [{"id": 4, "name": "ATLAS INDUSTRIES", "open_count": 3}]}},
            {"data": {"people": [{"id": 3, "name": "Hugo Lambert"}]}},
            {"data": {"events": [{"id": 9, "title": "Point hebdo ORION", "starts_at": "2026-11-10 11:00"}]}},
            {"data": {"conversations": [{"conversation_id": 2, "title": "Migration Odoo 17"}]}},
        ]
        reponse = "Atlas Industries : 3 taches. Hugo Lambert attend. Point hebdo ORION demain. Voir Migration Odoo 17."
        urls = [s["url"] for s in chat_sources.from_results(resultats, reponse)]
        self.assertEqual(urls, ["/projets/4", "/contacts?personne=3", "/agenda?date=2026-11-10", "/chat?c=2"])

    def test_nom_sans_contexte_ignore_et_titre_trop_court(self):
        # Un {id, name} hors d'une liste connue (etape, pays...) n'est pas un lien.
        resultats = [{"data": {"phases": [{"id": 1, "name": "Recette"}]}},
                     {"data": {"tasks": [{"id": 5, "title": "Paie"}]}}]
        self.assertEqual(chat_sources.from_results(resultats, "Recette en cours, paie a revoir."), [])

    def test_doublons_et_plafond(self):
        taches = [{"id": i, "title": f"Tache numero {i:02d}"} for i in range(20)]
        reponse = " ".join(f"tache numero {i:02d}" for i in range(20)) + " #3"
        sources = chat_sources.from_results([{"data": {"tasks": taches}}, {"data": {"tasks": taches}}], reponse)
        self.assertEqual(len(sources), chat_sources.MAX_SOURCES)
        self.assertEqual(len({s["id"] for s in sources}), chat_sources.MAX_SOURCES)


class ChatTest(NomaiaTestCase):
    """Bout en bout : la reponse porte ses sources et le fil les garde."""

    def test_reponse_avec_sources_enregistree_dans_le_fil(self):
        projet = project_model.create_project("ALPHA")
        tache = task_model.create_task({"title": "Corriger le crash de la paie", "project_id": projet["id"]})
        task_model.create_task({"title": "Archiver les vieux exports"})
        faux = scenario({"tool": "list_tasks", "args": {}}, {"reply": "Priorité : corriger le crash de la paie."})
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("par quoi je commence ?")

        self.assertEqual([s["id"] for s in resultat["sources"]], [tache["id"]])
        dernier = chat_model.history()[-1]
        self.assertEqual(dernier["sources"], resultat["sources"])

        reponse = self.client().get("/api/chat").get_json()
        self.assertEqual(reponse["messages"][-1]["sources"][0]["url"], f"/plan?tache={tache['id']}")

    def test_plan_applique_lie_les_taches_touchees(self):
        tache = task_model.create_task({"title": "Relancer le client ORION"})
        faux = scenario({"plan": [{"tool": "postpone_task", "args": {"task_id": tache["id"], "days": 2}}]})
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("repousse la relance de deux jours")

        self.assertTrue(resultat["reply"].startswith("Fait"))
        self.assertEqual([s["id"] for s in resultat["sources"]], [tache["id"]])

    def test_tache_supprimee_n_a_pas_de_lien(self):
        tache = task_model.create_task({"title": "Doublon a supprimer"})
        faux = scenario({"plan": [{"tool": "delete_task", "args": {"task_id": tache["id"]}}]})
        with patch.object(assistant.llm, "ask", faux):
            assistant.reply_to("supprime le doublon")
            resultat = assistant.reply_to("oui")

        self.assertTrue(resultat["reply"].startswith("Fait"))
        self.assertEqual(resultat["sources"], [])

    def test_ancien_message_sans_sources(self):
        chat_model.add("assistant", "Réponse d'avant les sources.")
        self.assertEqual(chat_model.history()[-1]["sources"], [])
