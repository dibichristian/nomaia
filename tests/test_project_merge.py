"""Tests de la fusion de projets en double (projet saisi ici + projet importe d'Odoo).

Le projet garde conserve son nom, ses etapes et ses informations ; tout ce
qui vivait dans le doublon le rejoint, liens Odoo compris, pour que la
synchro suivante ne recree pas le doublon.
"""
from models import get_db, init_schema, journal
from models import phase as phase_model
from models import project as project_model
from models import task as task_model
from models.project import ProjectError
from services import odoo_sync
from tests.base import NomaiaTestCase
from tests.test_odoo_sync import MAINTENANT, FakeOdoo


class ProjectMergeTest(NomaiaTestCase):

    def setUp(self):
        super().setUp()
        self.garde = project_model.create_project("Acme")
        self.doublon = project_model.create_project("Opportunite de Acme - (Integration)")

    def _count(self, table, project_id):
        return get_db().execute(
            f"SELECT COUNT(*) FROM {table} WHERE project_id = ?", (project_id,)
        ).fetchone()[0]

    def test_merge_moves_everything_and_deletes_duplicate(self):
        tache = task_model.create_task({"title": "Import", "project_id": self.doublon["id"]})
        journal.add_entry("Decision", project_id=self.doublon["id"], kind="decision")
        get_db().execute(
            "INSERT INTO person (name, project_id) VALUES ('Sarah', ?)", (self.doublon["id"],)
        )
        project_model.link_odoo(self.doublon["id"], 93)

        project_model.merge_projects(self.garde["id"], self.doublon["id"])

        self.assertIsNone(project_model.get_project(self.doublon["id"]))
        self.assertEqual(task_model.get_task(tache["id"])["project_id"], self.garde["id"])
        self.assertEqual(self._count("person", self.garde["id"]), 1)
        self.assertEqual(len(journal.list_register(self.garde["id"])), 1)
        self.assertEqual(project_model.find_by_odoo_id(93)["id"], self.garde["id"])
        self.assertIn("fusionne ici", journal.list_for_project(self.garde["id"])[0]["content"])

    def test_merge_keeps_target_phases_and_unphases_moved_tasks(self):
        etape_garde = phase_model.create_phase(self.garde["id"], {"name": "Recette"})
        etape_doublon = phase_model.create_phase(self.doublon["id"], {"name": "Cadrage"})
        tache = task_model.create_task(
            {"title": "T", "project_id": self.doublon["id"], "phase_id": etape_doublon["id"]}
        )

        project_model.merge_projects(self.garde["id"], self.doublon["id"])

        self.assertEqual(
            [etape["id"] for etape in phase_model.list_phases(self.garde["id"])], [etape_garde["id"]]
        )
        self.assertIsNone(phase_model.get_phase(etape_doublon["id"]))
        self.assertIsNone(task_model.get_task(tache["id"])["phase_id"])

    def test_merge_fills_only_missing_fields(self):
        project_model.update_project(self.garde["id"], {"weather": "sunny"})
        project_model.update_project(
            self.doublon["id"], {"weather": "stormy", "go_live": "2026-12-01"}
        )

        garde = project_model.merge_projects(self.garde["id"], self.doublon["id"])

        self.assertEqual((garde["name"], garde["weather"]), ("Acme", "sunny"))
        self.assertEqual(garde["go_live"], "2026-12-01")

    def test_merge_rejects_same_or_missing_project(self):
        with self.assertRaises(ProjectError):
            project_model.merge_projects(self.garde["id"], self.garde["id"])
        with self.assertRaises(ProjectError):
            project_model.merge_projects(self.garde["id"], 9999)

    def test_sync_after_merge_does_not_recreate_duplicate(self):
        odoo = FakeOdoo()
        odoo.add(1, "Import des immo", project=(93, "Opportunite de Acme - (Integration)"))
        odoo.add(2, "Tableau PNL", project=(281, "Acme - POWER BI - (Evolution)"))
        odoo_sync.sync(client=odoo, now=MAINTENANT)
        power_bi = project_model.find_by_odoo_id(281)
        doublon = project_model.find_by_odoo_id(93)

        project_model.merge_projects(self.garde["id"], doublon["id"])
        project_model.merge_projects(self.garde["id"], power_bi["id"])
        rapport = odoo_sync.sync(client=odoo, now=MAINTENANT)

        self.assertEqual(rapport["projects_created"], 0)
        projets = {task["project_id"] for task in task_model.list_tasks()}
        self.assertEqual(projets, {self.garde["id"]})

    def test_migration_moves_legacy_odoo_link(self):
        db = get_db()
        db.execute("UPDATE project SET odoo_id = 6 WHERE id = ?", (self.doublon["id"],))
        db.commit()

        init_schema()

        self.assertEqual(project_model.find_by_odoo_id(6)["id"], self.doublon["id"])
        self.assertIsNone(project_model.get_project(self.doublon["id"])["odoo_id"])

    def test_api_merge(self):
        reponse = self.client().post(
            f"/api/projects/{self.garde['id']}/merge",
            json={"merge_id": self.doublon["id"]},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["id"], self.garde["id"])

        refus = self.client().post(
            f"/api/projects/{self.garde['id']}/merge", json={}, headers=self.write_headers()
        )
        self.assertEqual(refus.status_code, 400)
