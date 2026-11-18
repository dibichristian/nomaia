"""Tolerance des sessions Claude : noms de projet approximatifs, statuts synonymes."""
from models import project as project_model
from models import task as task_model
from services import agent
from services.agent import AgentError
from tests.base import NomaiaTestCase


class ProjetApproximatifTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        self.nordik = project_model.create_project("NORDIK EUROPA")
        self.delta = project_model.create_project("DELTA TRANSPORT")
        self.delta_bis = project_model.create_project("Delta Logistique")

    def test_nom_partiel_unique_accepte(self):
        cree = agent.create_task("Préparer la démo", project="Nordik")["created"]
        self.assertEqual(task_model.get_task(cree["id"])["project_id"], self.nordik["id"])

    def test_casse_accents_et_ponctuation_ignores(self):
        self.assertEqual(agent._project_id("nordik-europa"), self.nordik["id"])
        self.assertEqual(agent._project_id("Delta  Transport"), self.delta["id"])

    def test_nom_dans_une_phrase_accepte(self):
        self.assertEqual(agent._project_id("paie Nordik Europa"), self.nordik["id"])

    def test_ambigu_ou_inconnu_refuse_avec_les_candidats(self):
        with self.assertRaises(AgentError) as ambigu:
            agent._project_id("Delta")
        self.assertIn("DELTA TRANSPORT", str(ambigu.exception))
        self.assertIn("Delta Logistique", str(ambigu.exception))
        with self.assertRaises(AgentError) as inconnu:
            agent._project_id("Sun")  # mot partiel : pas de correspondance hasardeuse
        self.assertIn("inconnu", str(inconnu.exception))


class StatutSynonymeTest(NomaiaTestCase):
    def test_in_progress_et_completed_acceptes(self):
        tache = task_model.create_task({"title": "Corriger la paie"})
        agent.update_task(tache["id"], status="in_progress", progress_pct=30)
        self.assertEqual(task_model.get_task(tache["id"])["status"], "todo")
        agent.update_task(tache["id"], status="Terminée")
        self.assertEqual(task_model.get_task(tache["id"])["status"], "done")

    def test_filtre_de_liste_synonyme(self):
        task_model.create_task({"title": "Ouverte"})
        self.assertEqual(len(agent.list_tasks(status="open")), 1)

    def test_statut_inconnu_toujours_refuse(self):
        tache = task_model.create_task({"title": "X"})
        with self.assertRaises(AgentError):
            agent.update_task(tache["id"], status="annulee_peut_etre")
