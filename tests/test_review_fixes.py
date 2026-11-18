"""Non-regression des defauts releves a la relecture des lots N.

Chaque test decrit un scenario qui cassait : suppression de projet qui
emportait le journal des taches, tache epinglee comptee deux fois, avancement
ecrit malgre une note refusee, parametres ou cles malformes en erreur 500.
"""
from datetime import date

from models import journal, setting
from models import project as project_model
from models import task as task_model
from services import agent, planner
from services.agent import AgentError
from tests.base import NomaiaTestCase


class SuppressionProjetTest(NomaiaTestCase):
    """Le journal des taches survit a la suppression de leur projet."""

    def test_journal_des_taches_conserve(self):
        projet = project_model.create_project("ALPHA")
        tache = task_model.create_task({"title": "Migration", "project_id": projet["id"]})
        journal.add_entry("Travail fait", task_id=tache["id"], kind="work", minutes=30)
        journal.add_entry("Commentaire projet", project_id=projet["id"])

        project_model.delete_project(projet["id"])

        restant = journal.list_for_task(tache["id"])
        self.assertEqual([entree["content"] for entree in restant], ["Travail fait"])
        self.assertIsNone(restant[0]["project_id"])
        self.assertIsNone(task_model.get_task(tache["id"])["project_id"])


class EpinglageEtapesTest(NomaiaTestCase):
    """Epingler une tache decoupee place ses etapes, pas le bloc entier."""

    def test_etapes_a_la_place_du_parent(self):
        jour = date.today()
        parent = task_model.create_task({"title": "Parent", "estimate_min": 240})
        task_model.create_task({"title": "Etape 1", "parent_id": parent["id"], "estimate_min": 30})
        task_model.create_task({"title": "Etape 2", "parent_id": parent["id"], "estimate_min": 30})
        task_model.pin_task(parent["id"], jour.isoformat())

        titres = [slot["task"]["title"] for slot in planner.build_plan(jour)["slots"]]
        self.assertEqual(titres[:2], ["Etape 1", "Etape 2"])
        self.assertNotIn("Parent", titres)


class JournalTacheTest(NomaiaTestCase):
    """Une note refusee n'ecrit pas l'avancement."""

    def test_avancement_non_ecrit_si_note_vide(self):
        tache = task_model.create_task({"title": "Migration"})
        reponse = self.client().post(
            f"/api/tasks/{tache['id']}/journal",
            json={"content": "", "progress_pct": 60},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(task_model.get_task(tache["id"])["progress_pct"], 0)


class AgentRobustesseTest(NomaiaTestCase):
    """Entrees malformees : erreur lisible, jamais une 500."""

    def test_limite_illisible(self):
        with self.assertRaises(AgentError):
            agent.list_tasks(limit="abc")

    def test_cle_non_ascii_refusee_proprement(self):
        setting.regenerate_agent_key()
        reponse = self.app.test_client().get(
            "/api/agent/tasks", headers={"Authorization": "Bearer été"}
        )
        self.assertEqual(reponse.status_code, 401)
