"""Un interlocuteur peut suivre plusieurs projets (table person_project)."""
from models import get_db, init_schema
from models import person as person_model
from models import project as project_model
from services import agent, project_board
from tests.base import NomaiaTestCase


class ProjetsMultiplesTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        self.acme = project_model.create_project("ACME")
        self.nordik = project_model.create_project("NORDIK EUROPA")
        self.delta = project_model.create_project("DELTA TRANSPORT")

    def _projets(self, person_id):
        return [projet["name"] for projet in person_model.get_person(person_id)["projects"]]

    def test_creation_puis_ajout_sans_perdre_le_principal(self):
        hugo = person_model.create_person({"name": "Hugo", "project_id": self.acme["id"]})
        self.assertEqual(self._projets(hugo["id"]), ["ACME"])
        person_model.add_project(hugo["id"], self.nordik["id"])
        fiche = person_model.get_person(hugo["id"])
        self.assertEqual(fiche["project_id"], self.acme["id"])
        self.assertEqual([p["name"] for p in fiche["projects"]], ["ACME", "NORDIK EUROPA"])

    def test_edition_de_l_ensemble_et_principal_remplace(self):
        hugo = person_model.create_person({"name": "Hugo", "project_id": self.acme["id"]})
        person_model.update_person(hugo["id"], {"project_ids": [self.nordik["id"], self.delta["id"]]})
        fiche = person_model.get_person(hugo["id"])
        self.assertEqual(fiche["project_id"], self.nordik["id"])
        self.assertEqual(sorted(p["name"] for p in fiche["projects"]), ["DELTA TRANSPORT", "NORDIK EUROPA"])
        person_model.update_person(hugo["id"], {"project_ids": []})
        self.assertEqual((person_model.get_person(hugo["id"])["project_id"], self._projets(hugo["id"])), (None, []))

    def test_changer_le_principal_garde_les_autres(self):
        hugo = person_model.create_person({"name": "Hugo", "project_ids": [self.acme["id"], self.nordik["id"]]})
        person_model.update_person(hugo["id"], {"project_id": self.delta["id"]})
        fiche = person_model.get_person(hugo["id"])
        self.assertEqual(fiche["project_id"], self.delta["id"])
        self.assertEqual(fiche["projects"][0]["name"], "DELTA TRANSPORT")
        self.assertEqual(len(fiche["projects"]), 3)
        person_model.remove_project(hugo["id"], self.delta["id"])
        self.assertIn(person_model.get_person(hugo["id"])["project_id"], (self.acme["id"], self.nordik["id"]))

    def test_cite_sur_un_autre_projet_par_le_triage(self):
        person_model.get_or_create("Lucie", project_id=self.acme["id"])
        lucie = person_model.get_or_create("lucie", project_id=self.nordik["id"])
        self.assertEqual(sorted(self._projets(lucie["id"])), ["ACME", "NORDIK EUROPA"])

    def test_fiche_projet_et_filtre(self):
        hugo = person_model.create_person({"name": "Hugo", "project_ids": [self.acme["id"], self.nordik["id"]]})
        person_model.create_person({"name": "Tom", "project_id": self.delta["id"]})
        noms = [p["name"] for p in person_model.list_people(project_id=self.nordik["id"])]
        self.assertEqual(noms, ["Hugo"])
        self.assertEqual([p["id"] for p in project_board.detail(self.nordik["id"])["people"]], [hugo["id"]])

    def test_fusions_conservent_les_projets(self):
        a = person_model.create_person({"name": "Paul", "project_id": self.acme["id"]})
        b = person_model.create_person({"name": "Paul M.", "project_id": self.nordik["id"]})
        person_model.merge_people(a["id"], b["id"])
        self.assertEqual(sorted(self._projets(a["id"])), ["ACME", "NORDIK EUROPA"])
        project_model.merge_projects(self.acme["id"], self.nordik["id"])
        self.assertEqual(self._projets(a["id"]), ["ACME"])

    def test_migration_reprend_le_projet_principal(self):
        db = get_db()
        pid = db.execute("INSERT INTO person (name, project_id) VALUES ('Ancien', ?)", (self.delta["id"],)).lastrowid
        db.commit()
        self.assertEqual(self._projets(pid), [])
        init_schema()
        init_schema()  # rejouable sans doublon
        self.assertEqual(self._projets(pid), ["DELTA TRANSPORT"])


class SessionsClaudeTest(NomaiaTestCase):
    def test_ajout_et_retrait_de_projet_par_le_mcp(self):
        project_model.create_project("ACME")
        project_model.create_project("NORDIK EUROPA")
        agent.save_person("Hugo", project="Acme")
        resultat = agent.save_person("Hugo", project="Nordik")["person"]
        self.assertEqual(resultat["projects"], ["ACME", "NORDIK EUROPA"])
        resultat = agent.save_person("Hugo", remove_project="ACME")["person"]
        self.assertEqual(resultat["projects"], ["NORDIK EUROPA"])
        self.assertEqual([p["name"] for p in agent.list_people(project="nordik")["people"]], ["Hugo"])
        self.assertEqual(agent.list_people(query="nordik")["people"][0]["name"], "Hugo")
