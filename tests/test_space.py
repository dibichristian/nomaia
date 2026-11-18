"""Tests des espaces de travail pro et perso.

Chaque cle des sessions Claude est liee a un espace : une session perso ne
voit, ne cree et ne modifie que des projets et taches perso, et inversement.
Une tache suit l'espace de son projet ; l'existant bascule en 'pro'.
"""
import sqlite3

import config
from models import _migrate, get_db, setting
from models import project as project_model
from models import task as task_model
from models.task import ValidationError
from services import search as search_service
from tests.base import NomaiaTestCase


class MigrationTest(NomaiaTestCase):
    """La migration ajoute l'espace sans rien perdre, et range tout en pro."""

    def test_base_anterieure_basculee_en_pro(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE project (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT, project_id INTEGER);"
            "INSERT INTO project (id, name) VALUES (1, 'ALPHA');"
            "INSERT INTO task (id, title, project_id) VALUES (1, 'Migrer', 1);"
        )
        for table, column, definition in __import__("models").MIGRATIONS:
            if table in ("project", "task") and column == "space":
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.assertEqual(conn.execute("SELECT space FROM project").fetchone()[0], "pro")
        self.assertEqual(conn.execute("SELECT space FROM task").fetchone()[0], "pro")

    def test_migration_idempotente(self):
        conn = sqlite3.connect(config.DB_PATH)
        try:
            _migrate(conn)
            _migrate(conn)
            colonnes = {row[1] for row in conn.execute("PRAGMA table_info(task)")}
        finally:
            conn.close()
        self.assertIn("space", colonnes)

    def test_cle_perso_effacee_des_sauvegardes(self):
        from models import SECRET_SETTINGS
        self.assertIn("agent_key_perso", SECRET_SETTINGS)
        self.assertIn("agent_key_perso", setting.SECRET_KEYS)


class ModeleTest(NomaiaTestCase):
    """Une tache suit l'espace de son projet, ou de sa parente."""

    def setUp(self):
        super().setUp()
        self.perso = project_model.create_project("NOMAIA", space="perso")
        self.pro = project_model.create_project("ALPHA")

    def test_projet_pro_par_defaut(self):
        self.assertEqual(self.pro["space"], "pro")

    def test_espace_invalide_refuse(self):
        with self.assertRaises(project_model.ProjectError):
            project_model.create_project("X", space="vacances")

    def test_tache_herite_de_l_espace_du_projet(self):
        tache = task_model.create_task({"title": "Ajouter les espaces", "project_id": self.perso["id"]})
        self.assertEqual(tache["space"], "perso")

    def test_tache_sans_projet_garde_son_espace(self):
        tache = task_model.create_task({"title": "Courses", "space": "perso"})
        self.assertEqual(tache["space"], "perso")
        self.assertEqual(task_model.create_task({"title": "Relancer"})["space"], "pro")

    def test_espace_contraire_au_projet_refuse(self):
        with self.assertRaises(ValidationError):
            task_model.create_task(
                {"title": "Incoherente", "project_id": self.pro["id"], "space": "perso"}
            )

    def test_tache_deplacee_change_d_espace(self):
        tache = task_model.create_task({"title": "Outil", "project_id": self.pro["id"]})
        tache = task_model.update_task(tache["id"], {"project_id": self.perso["id"]})
        self.assertEqual(tache["space"], "perso")

    def test_sous_tache_sans_projet_suit_sa_parente(self):
        parente = task_model.create_task({"title": "Parente", "space": "perso"})
        enfant = task_model.create_task({"title": "Etape", "parent_id": parente["id"]})
        self.assertEqual(enfant["space"], "perso")

    def test_changer_l_espace_du_projet_entraine_ses_taches(self):
        tache = task_model.create_task({"title": "Outil", "project_id": self.pro["id"]})
        project_model.update_project(self.pro["id"], {"space": "perso"})
        self.assertEqual(task_model.get_task(tache["id"])["space"], "perso")

    def test_fusion_aligne_les_taches_sur_le_projet_garde(self):
        tache = task_model.create_task({"title": "Outil", "project_id": self.perso["id"]})
        project_model.merge_projects(self.pro["id"], self.perso["id"])
        self.assertEqual(task_model.get_task(tache["id"])["space"], "pro")

    def test_rattachement_des_orphelines_aligne_l_espace(self):
        tache = task_model.create_task({"title": "Corriger NOMAIA"})
        project_model.attach_orphan_tasks(self.perso["id"])
        self.assertEqual(task_model.get_task(tache["id"])["space"], "perso")

    def test_filtres_par_espace(self):
        task_model.create_task({"title": "Perso", "project_id": self.perso["id"]})
        task_model.create_task({"title": "Pro", "project_id": self.pro["id"]})
        self.assertEqual([t["title"] for t in task_model.list_tasks(space="perso")], ["Perso"])
        self.assertEqual([p["name"] for p in project_model.list_projects(space="pro")], ["ALPHA"])
        self.assertEqual(len(task_model.list_tasks()), 2)


class AgentEspaceTest(NomaiaTestCase):
    """La cle presentee fixe l'espace de toute la requete."""

    def setUp(self):
        super().setUp()
        self.cle_pro = setting.regenerate_agent_key("pro")
        self.cle_perso = setting.regenerate_agent_key("perso")
        self.perso = project_model.create_project("NOMAIA", space="perso")
        self.pro = project_model.create_project("ALPHA")
        self.tache_pro = task_model.create_task(
            {"title": "Livrer la paie ALPHA", "project_id": self.pro["id"]}
        )
        self.tache_perso = task_model.create_task(
            {"title": "Ajouter les espaces NOMAIA", "project_id": self.perso["id"]}
        )

    def _appel(self, methode, chemin, cle, **kwargs):
        client = self.app.test_client()
        return getattr(client, methode)(
            "/api/agent" + chemin, headers={"Authorization": "Bearer " + cle}, **kwargs
        )

    def test_cle_perso_ne_voit_que_le_perso(self):
        taches = self._appel("get", "/tasks", self.cle_perso).get_json()["tasks"]
        self.assertEqual([t["id"] for t in taches], [self.tache_perso["id"]])
        projets = self._appel("get", "/projects", self.cle_perso).get_json()
        noms = [p["name"] for p in (projets.get("projects") if isinstance(projets, dict) else projets)]
        self.assertEqual(noms, ["NOMAIA"])

    def test_cle_pro_ne_voit_que_le_pro(self):
        taches = self._appel("get", "/tasks", self.cle_pro).get_json()["tasks"]
        self.assertEqual([t["id"] for t in taches], [self.tache_pro["id"]])

    def test_tache_de_l_autre_espace_introuvable(self):
        reponse = self._appel("get", f"/tasks/{self.tache_pro['id']}", self.cle_perso)
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("introuvable", reponse.get_json()["error"])
        reponse = self._appel(
            "post", f"/tasks/{self.tache_pro['id']}/progress", self.cle_perso,
            json={"progress_pct": 50},
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(task_model.get_task(self.tache_pro["id"])["progress_pct"], 0)

    def test_log_work_refuse_sur_l_autre_espace(self):
        reponse = self._appel(
            "post", f"/tasks/{self.tache_pro['id']}/work", self.cle_perso,
            json={"minutes": 10, "summary": "Essai"},
        )
        self.assertEqual(reponse.status_code, 400)

    def test_creation_sans_projet_dans_l_espace_de_la_cle(self):
        reponse = self._appel(
            "post", "/tasks", self.cle_perso, json={"title": "Ranger le garage", "force": True}
        )
        self.assertEqual(reponse.status_code, 201)
        tache = task_model.get_task(reponse.get_json()["created"]["id"])
        self.assertEqual(tache["space"], "perso")

    def test_projet_de_l_autre_espace_inconnu(self):
        reponse = self._appel(
            "post", "/tasks", self.cle_perso,
            json={"title": "Glisser dans le pro", "project": "ALPHA", "force": True},
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("inconnu", reponse.get_json()["error"])
        self.assertNotIn("ALPHA", reponse.get_json()["error"].split("Projets actifs")[-1])

    def test_doublon_cherche_seulement_dans_l_espace(self):
        reponse = self._appel(
            "post", "/tasks", self.cle_perso, json={"title": "Livrer la paie ALPHA"}
        )
        self.assertEqual(reponse.status_code, 201)

    def test_projet_cree_dans_l_espace_de_la_cle(self):
        reponse = self._appel("post", "/projects", self.cle_perso, json={"name": "Maison"})
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(project_model.find_by_name("Maison")["space"], "perso")

    def test_nom_de_projet_pris_dans_l_autre_espace(self):
        reponse = self._appel("post", "/projects", self.cle_perso, json={"name": "alpha"})
        self.assertEqual(reponse.status_code, 400)
        self.assertNotIn("existing", reponse.get_json())

    def test_rapport_d_avancement_refuse_en_perso(self):
        reponse = self._appel("get", "/progress-report", self.cle_perso)
        self.assertEqual(reponse.status_code, 400)

    def test_recherche_filtree_par_espace(self):
        get_db().commit()
        resultats = search_service.search("ALPHA", space="perso")
        self.assertFalse(any(r["kind"] in ("tache", "projet") for r in resultats))
        resultats = search_service.search("ALPHA", space="pro")
        self.assertTrue(any(r["kind"] == "tache" for r in resultats))

    def test_cle_pro_historique_toujours_valable(self):
        # La cle 'agent_key' d'avant la mise a jour reste la cle pro.
        self.assertEqual(setting.get_agent_key(), self.cle_pro)
        self.assertEqual(self._appel("get", "/tasks", self.cle_pro).status_code, 200)

    def test_regenerer_une_cle_ne_coupe_pas_l_autre(self):
        setting.regenerate_agent_key("perso")
        self.assertEqual(self._appel("get", "/tasks", self.cle_pro).status_code, 200)
        self.assertEqual(self._appel("get", "/tasks", self.cle_perso).status_code, 401)


class ConfigTest(NomaiaTestCase):
    """La page Config genere une cle par espace sans jamais la relire."""

    def test_generation_cle_perso(self):
        client = self.client()
        reponse = client.post(
            "/api/agent-setup/key", json={"space": "perso"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 200)
        cle = setting.get_agent_key("perso")
        self.assertIn(cle, reponse.get_json()["command"])
        self.assertEqual(setting.get_agent_key("pro"), "")
        etat = client.get("/api/agent-setup").get_json()
        self.assertEqual(etat["spaces"], {"pro": False, "perso": True})
        self.assertNotIn(cle, str(etat))

    def test_espace_inconnu_refuse(self):
        reponse = self.client().post(
            "/api/agent-setup/key", json={"space": "autre"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)
