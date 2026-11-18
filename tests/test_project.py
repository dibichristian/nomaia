"""Tests des projets : validation, rattrapage des taches orphelines, API."""
import unittest

from models import project as project_model
from models import task as task_model
from models.project import ProjectError
from tests.base import NomaiaTestCase


class ProjectModelTest(NomaiaTestCase):
    """Regles de validation et cycle de vie d'un projet."""

    def test_nom_obligatoire(self):
        with self.assertRaises(ProjectError):
            project_model.create_project("   ")

    def test_couleur_hors_format_refusee(self):
        with self.assertRaises(ProjectError):
            project_model.create_project("ALPHA", "red;background:url(http://pirate)")

    def test_couleur_par_defaut_quand_vide(self):
        projet = project_model.create_project("ALPHA", "")
        self.assertEqual(projet["color"], project_model.DEFAULT_COLOR)

    def test_couleur_suggeree_est_stable(self):
        self.assertEqual(
            project_model.suggest_color("BRIGHTJET"), project_model.suggest_color("brightjet")
        )
        self.assertIn(project_model.suggest_color("BRIGHTJET"), project_model.PALETTE)

    def test_recherche_par_nom_ignore_la_casse(self):
        project_model.create_project("ALPHA")
        self.assertIsNotNone(project_model.find_by_name("alpha"))
        self.assertIsNone(project_model.find_by_name("orion"))

    def test_supprimer_un_projet_detache_ses_taches(self):
        projet = project_model.create_project("ALPHA")
        tache = task_model.create_task({"title": "Corriger", "project_id": projet["id"]})

        project_model.delete_project(projet["id"])
        self.assertIsNone(task_model.get_task(tache["id"])["project_id"])

    def test_statut_invalide_refuse(self):
        projet = project_model.create_project("ALPHA")
        with self.assertRaises(ProjectError):
            project_model.update_project(projet["id"], {"status": "zombie"})


class AttachOrphanTest(NomaiaTestCase):
    """Le rattrapage vise les taches ouvertes sans projet qui citent le nom."""

    def setUp(self):
        super().setUp()
        self.citante = task_model.create_task({"title": "Corriger le crash paie ALPHA"})
        self.autre = task_model.create_task({"title": "Analyser les logs ORION"})
        self.terminee = task_model.create_task({"title": "Ancienne tache ALPHA"})
        task_model.toggle_task(self.terminee["id"])

        self.projet = project_model.create_project("ALPHA")

    def test_compte_avant_de_rattacher(self):
        self.assertEqual(project_model.count_orphan_matches(self.projet["id"]), 1)

    def test_rattache_uniquement_les_taches_citantes_et_ouvertes(self):
        rattachees = project_model.attach_orphan_tasks(self.projet["id"])

        self.assertEqual(rattachees, 1)
        self.assertEqual(task_model.get_task(self.citante["id"])["project_id"], self.projet["id"])
        self.assertIsNone(task_model.get_task(self.autre["id"])["project_id"])
        self.assertIsNone(task_model.get_task(self.terminee["id"])["project_id"])

    def test_rattachement_deux_fois_ne_double_rien(self):
        project_model.attach_orphan_tasks(self.projet["id"])
        self.assertEqual(project_model.attach_orphan_tasks(self.projet["id"]), 0)

    def test_une_tache_deja_rattachee_n_est_pas_volee(self):
        autre_projet = project_model.create_project("ALPHA bis")
        task_model.update_task(self.citante["id"], {"project_id": autre_projet["id"]})

        project_model.attach_orphan_tasks(self.projet["id"])
        self.assertEqual(
            task_model.get_task(self.citante["id"])["project_id"], autre_projet["id"]
        )

    def test_les_jokers_sql_du_nom_sont_echappes(self):
        # Un projet nomme "%" rattacherait toutes les taches si le motif LIKE
        # n'echappait pas ses jokers.
        joker = project_model.create_project("%")
        self.assertEqual(project_model.count_orphan_matches(joker["id"]), 0)

    def test_projet_inexistant_ne_touche_a_rien(self):
        self.assertEqual(project_model.attach_orphan_tasks(9999), 0)
        self.assertEqual(project_model.count_orphan_matches(9999), 0)


class ProjectApiTest(NomaiaTestCase):
    """Parcours reel de la page Projets, garde-fou anti-CSRF compris."""

    def test_creation_avec_rattrapage(self):
        client = self.client()
        client.post("/api/tasks", json={"title": "Bug ALPHA"}, headers=self.write_headers())

        reponse = client.post(
            "/api/projects",
            json={"name": "ALPHA", "attach_matching": True},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(reponse.get_json()["attached"], 1)

    def test_nom_vide_repond_400(self):
        reponse = self.client().post(
            "/api/projects", json={"name": ""}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_rattachement_d_un_projet_inconnu_repond_404(self):
        reponse = self.client().post("/api/projects/9999/attach", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 404)

    def test_ecriture_depuis_une_autre_origine_est_refusee(self):
        reponse = self.client().post(
            "/api/projects", json={"name": "Pirate"}, headers={"Origin": "http://mechant.example"}
        )
        self.assertEqual(reponse.status_code, 403)

    def test_lecture_sans_cle_est_refusee(self):
        reponse = self.app.test_client().get("/api/projects")
        self.assertEqual(reponse.status_code, 401)


class TaskFilterTest(NomaiaTestCase):
    """Le filtre projet du tableau de bord, y compris 'sans projet'."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")
        task_model.create_task({"title": "Rattachee", "project_id": self.projet["id"]})
        task_model.create_task({"title": "Orpheline"})

    def test_filtre_par_projet(self):
        taches = task_model.list_tasks(project_id=self.projet["id"])
        self.assertEqual([item["title"] for item in taches], ["Rattachee"])

    def test_filtre_sans_projet(self):
        taches = task_model.list_tasks(project_id=task_model.NO_PROJECT)
        self.assertEqual([item["title"] for item in taches], ["Orpheline"])

    def test_filtre_illisible_ne_masque_rien(self):
        reponse = self.client().get("/api/tasks?project_id=nawak")
        self.assertEqual(len(reponse.get_json()["tasks"]), 2)

    def test_lecture_unitaire_d_une_tache(self):
        tache = task_model.list_tasks()[0]
        reponse = self.client().get(f"/api/tasks/{tache['id']}")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["id"], tache["id"])


if __name__ == "__main__":
    unittest.main()
