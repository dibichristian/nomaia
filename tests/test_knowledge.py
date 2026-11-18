"""Lot 6 : base de connaissances, cause et solution des anomalies, detecteur de secrets."""
from models import get_db, journal, knowledge
from models import search as search_model
from models import task as task_model
from services.secrets_guard import find_secret
from tests.base import NomaiaTestCase


class DetecteurDeSecretsTest(NomaiaTestCase):
    def test_reconnait_les_secrets(self):
        for text in ("admin_passwd = MonVraiMot2026", "db_password: S3cr3t!", "mot de passe : Azerty123",
                     "clé sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123", "-----BEGIN RSA PRIVATE KEY-----\nabc",
                     "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"):
            with self.subTest(text=text):
                self.assertIsNotNone(find_secret(text))

    def test_laisse_passer_les_textes_ordinaires(self):
        for text in ("mot de passe : ***", "Le mot de passe a été changé", "token=${ODOO_TOKEN}",
                     "password: <voir KeePass>", "clé rangée dans le coffre du client", "api_key=os.environ['KEY']", ""):
            with self.subTest(text=text):
                self.assertIsNone(find_secret(text))

    def test_journal_compte_rendu_et_note_refusent_un_secret(self):
        db = get_db()
        task_id = db.execute("INSERT INTO task (title) VALUES ('Déployer')").lastrowid
        db.commit()
        with self.assertRaises(journal.JournalError):
            journal.add_entry("Connexion : admin_passwd = MonVraiMot2026", task_id=task_id)
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.create({"title": "Accès", "content": "password: Hunter2024"})
        client = self.client()
        minutes = client.post("/api/minutes", json={"title": "Point", "held_on": "2026-11-06"},
                              headers=self.write_headers()).get_json()
        reponse = client.patch(f"/api/minutes/{minutes['id']}", json={"notes": "mdp: Azerty123"},
                               headers=self.write_headers())
        self.assertEqual(reponse.status_code, 400)
        self.assertIn("secret", reponse.get_json()["error"])


class SolutionDAnomalieTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.pid = db.execute("INSERT INTO project (name) VALUES ('ACME')").lastrowid
        db.commit()
        self.anomaly = task_model.create_task({
            "title": "Avoir FNE refusé", "description": "Erreur de signature à l'envoi", "project_id": self.pid,
            "kind": "anomaly", "severity": "major", "module": "custom_fne",
        })["id"]

    def note(self):
        return get_db().execute("SELECT * FROM kb_note WHERE source_task_id = ?", (self.anomaly,)).fetchone()

    def test_anomalie_resolue_avec_solution_cree_sa_note(self):
        task_model.update_task(self.anomaly, {"cause": "Horodatage en UTC", "solution": "Passer l'heure locale"})
        self.assertIsNone(self.note())  # pas encore resolue
        task_model.toggle_task(self.anomaly)
        note = self.note()
        self.assertEqual((note["title"], note["category"], note["project_id"], note["module"]),
                         ("Solution : Avoir FNE refusé", "bug_solution", self.pid, "custom_fne"))
        for part in ("## Symptôme", "Erreur de signature", "## Cause", "Horodatage en UTC", "## Solution"):
            self.assertIn(part, note["content"])

    def test_note_tenue_a_jour_et_unique(self):
        task_model.update_task(self.anomaly, {"solution": "Version 1"})
        task_model.toggle_task(self.anomaly)
        task_model.update_task(self.anomaly, {"solution": "Version 2"})
        rows = get_db().execute("SELECT content FROM kb_note WHERE source_task_id = ?", (self.anomaly,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Version 2", rows[0]["content"])

    def test_sans_solution_ou_pas_une_anomalie(self):
        task_model.toggle_task(self.anomaly)
        self.assertIsNone(self.note())
        ordinaire = task_model.create_task({"title": "Tâche", "solution": "x"})["id"]
        task_model.toggle_task(ordinaire)
        self.assertEqual(get_db().execute("SELECT COUNT(*) FROM kb_note").fetchone()[0], 0)

    def test_solution_avec_secret_refusee(self):
        with self.assertRaises(task_model.ValidationError):
            task_model.update_task(self.anomaly, {"solution": "Se connecter avec admin_passwd = Secret2026"})


class BaseDeConnaissancesTest(NomaiaTestCase):
    def test_crud_filtres_et_recherche(self):
        client, h = self.client(), self.write_headers()
        created = client.post("/api/notes", json={
            "title": "Domaine multi-société", "content": "```python\n[('company_id', 'in', company_ids)]\n```",
            "category": "python", "odoo_version": "17.0", "module": "sale", "tags": "Domaine, multi-société, domaine",
        }, headers=h)
        self.assertEqual(created.status_code, 201)
        note = created.get_json()
        self.assertEqual(note["tags"], ["domaine", "multi-société"])
        client.post("/api/notes", json={"title": "Déploiement ALPHA", "category": "deployment"}, headers=h)

        data = client.get("/api/notes?category=python").get_json()
        self.assertEqual([n["title"] for n in data["notes"]], ["Domaine multi-société"])
        self.assertEqual(client.get("/api/notes?tag=domaine").get_json()["notes"][0]["id"], note["id"])
        self.assertIn("17.0", data["facets"]["versions"])
        self.assertIn("## Vérifications", data["deployment_template"])

        self.assertIn(("note", note["id"]), [(h_["kind"], h_["id"]) for h_ in search_model.search("company_ids")])
        r = client.patch(f"/api/notes/{note['id']}", json={"pinned": True}, headers=h).get_json()
        self.assertEqual(r["pinned"], 1)
        self.assertEqual(client.get("/api/notes").get_json()["notes"][0]["id"], note["id"])  # epinglee en tete
        self.assertEqual(client.post("/api/notes", json={"title": " "}, headers=h).status_code, 400)
        self.assertEqual(client.post("/api/notes", json={"title": "x", "category": "rust"}, headers=h).status_code, 400)
        self.assertEqual(client.delete(f"/api/notes/{note['id']}", headers=h).status_code, 200)
        self.assertEqual(client.get(f"/api/notes/{note['id']}").status_code, 404)
