"""Recherche universelle (lot 3) : index FTS5 synchrone, mise en forme, API, MCP."""
from models import get_db, init_schema
from models import search as search_model
from services import search as search_service
from tests.base import NomaiaTestCase


class IndexTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.project_id = db.execute("INSERT INTO project (name) VALUES ('ALPHA Paie')").lastrowid
        self.task_id = db.execute(
            "INSERT INTO task (title, description, project_id) VALUES (?, ?, ?)",
            ("Paramétrer la réunion de recette", "Prévoir les comptables", self.project_id),
        ).lastrowid
        db.commit()

    def kinds(self, text):
        return [(hit["kind"], hit["id"]) for hit in search_model.search(text)]

    def test_accents_casse_et_prefixes(self):
        self.assertIn(("tache", self.task_id), self.kinds("reunion"))
        self.assertIn(("tache", self.task_id), self.kinds("PARAMETR"))
        self.assertIn(("projet", self.project_id), self.kinds("alp"))
        self.assertEqual(self.kinds("x"), [])  # un seul caractere : rien

    def test_tous_les_mots_sont_requis(self):
        self.assertIn(("tache", self.task_id), self.kinds("recette comptables"))
        self.assertNotIn(("tache", self.task_id), self.kinds("recette inexistant"))

    def test_index_suit_insertion_modification_suppression(self):
        db = get_db()
        db.execute("UPDATE task SET title = 'Livrer le module' WHERE id = ?", (self.task_id,))
        db.commit()
        self.assertNotIn(("tache", self.task_id), self.kinds("reunion"))
        self.assertIn(("tache", self.task_id), self.kinds("livrer"))
        db.execute("DELETE FROM task WHERE id = ?", (self.task_id,))
        db.commit()
        self.assertEqual(self.kinds("livrer"), [])

    def test_saisie_hostile_sans_erreur(self):
        for text in ['"', "NEAR(", "a* OR b", "tâche) AND (", "'; DROP TABLE task; --", "^*"]:
            with self.subTest(text=text):
                search_model.search(text)  # aucune exception
        self.assertIsNotNone(get_db().execute("SELECT 1 FROM task").fetchone())

    def test_sources_journal_contacts_evenements(self):
        db = get_db()
        note = db.execute("INSERT INTO note (task_id, content) VALUES (?, 'Décision : valorisation FIFO')",
                          (self.task_id,)).lastrowid
        person = db.execute("INSERT INTO person (name, role) VALUES ('Lucie Bernard', 'Chef comptable')").lastrowid
        event = db.execute("INSERT INTO event (title, location, starts_at, ends_at) VALUES "
                           "('Atelier stocks', 'Abidjan', '2026-11-10 09:00', '2026-11-10 10:00')").lastrowid
        db.commit()
        self.assertIn(("journal", note), self.kinds("fifo"))
        self.assertIn(("contact", person), self.kinds("comptable"))
        self.assertIn(("evenement", event), self.kinds("abidjan"))

    def test_reconstruction_si_version_change(self):
        db = get_db()
        db.execute("DELETE FROM search_fts")
        db.execute("UPDATE search_meta SET version = 0")
        db.commit()
        self.assertEqual(self.kinds("reunion"), [])
        init_schema()  # demarrage suivant : l'index est reconstruit
        self.assertIn(("tache", self.task_id), self.kinds("reunion"))

    def test_mise_en_forme_et_api(self):
        results = search_service.search("reunion")
        tache = next(r for r in results if r["kind"] == "tache")
        self.assertEqual(tache["url"], f"/plan?tache={self.task_id}")
        self.assertIn("ALPHA Paie", tache["sub"])
        projet = search_service.search("alpha")[0]
        self.assertEqual((projet["kind"], projet["url"]), ("projet", f"/projets/{self.project_id}"))

        client = self.client()
        data = client.get("/api/search?q=reunion").get_json()
        self.assertEqual(data["query"], "reunion")
        self.assertTrue(any(r["id"] == self.task_id for r in data["results"]))
        self.assertEqual(client.get("/api/search?q=").get_json()["results"], [])
        self.assertEqual(client.get("/api/search?q=reunion&limit=abc").status_code, 200)

    def test_api_exige_la_cle_d_acces(self):
        self.assertNotEqual(self.app.test_client().get("/api/search?q=reunion").status_code, 200)


class RechercheMcpTest(NomaiaTestCase):
    def test_route_agent_et_outil_mcp(self):
        import importlib.util
        from pathlib import Path

        import config
        from models import setting

        chemin = Path(config.RESOURCE_DIR) / "integrations" / "claude_mcp.py"
        spec = importlib.util.spec_from_file_location("claude_mcp", chemin)
        claude_mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(claude_mcp)

        db = get_db()
        db.execute("INSERT INTO task (title) VALUES ('Corriger l''export FNE')")
        db.commit()
        client = self.app.test_client()
        headers = {"Authorization": "Bearer " + setting.regenerate_agent_key()}
        data = client.get("/api/agent/search?q=fne", headers=headers).get_json()
        self.assertEqual(data["results"][0]["label"], "Corriger l'export FNE")
        self.assertEqual(set(data["results"][0]), {"kind", "kind_label", "id", "label", "sub", "url"})
        self.assertEqual(client.get("/api/agent/search?q=f", headers=headers).status_code, 400)
        self.assertIn("nomaia_search", [tool["name"] for tool in claude_mcp.TOOLS])
