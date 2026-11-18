"""Tests de l'import des taches Odoo (lecture seule, hierarchie parent / sous-taches).

Odoo fait foi pour le titre, l'echeance, le projet et le parent ; NOMAIA garde
la main sur la priorite, l'avancement et la cloture locale. Rejouer la
synchro ne cree jamais de doublon.
"""
import io
from datetime import datetime
from unittest.mock import patch

from models import get_db, journal, setting
from models import project as project_model
from models import task as task_model
from services import odoo_sync
from services.odoo_client import OdooClient, OdooConfig, OdooRpcError
from services.odoo_sync import OdooSyncError
from tests.base import NomaiaTestCase

MOI = 7
COLLEGUE = 9
MAINTENANT = datetime(2026, 10, 30, 10, 0)
PROJET = (60, "Projet test")


class FakeOdoo:
    """Base Odoo en memoire : evalue les domaines utilises par la synchro.

    version 18 : ni is_template ni priorite a quatre niveaux, comme le vrai
    serveur ; un domaine sur is_template y leve une erreur.
    """

    def __init__(self, version=19):
        self.version = version
        self.tasks = {}

    def add(self, task_id, name, users=(MOI,), parent=None, project=PROJET, **extra):
        """Ajoute une tache ; project est un couple (id, nom), None pour une tache privee."""
        self.tasks[task_id] = {
            "id": task_id,
            "name": name,
            "description": False,
            "user_ids": list(users),
            "parent_id": [parent, self.tasks[parent]["name"]] if parent else False,
            "project_id": list(project) if project else False,
            "state": "01_in_progress",
            "date_deadline": False,
            "priority": "0",
            "allocated_hours": 0.0,
            "sequence": 10,
            "active": True,
            **({"is_template": False} if self.version >= 19 else {}),
            **extra,
        }

    # --- API du client -----------------------------------------------------

    def authenticate(self):
        return MOI

    def user_info(self):
        return {"name": "Jean", "login": "jean.dupont@example.com"}

    def fields_get(self, model):
        if self.version >= 19:
            return {
                "is_template": {"type": "boolean"},
                "priority": {"type": "selection",
                             "selection": [["0", "Low"], ["1", "Medium"], ["2", "High"], ["3", "Urgent"]]},
            }
        return {"priority": {"type": "selection", "selection": [["0", "Low"], ["1", "High"]]}}

    @staticmethod
    def _match(row, domain):
        for field, op, value in domain:
            if field not in row:
                raise OdooRpcError(f"Invalid field {field} on model project.task")
            current = row[field]
            if field == "user_ids":
                ok = bool(set(current) & set(value))
            elif op == "in":
                ok = current in value
            elif op == "not in":
                ok = current not in value
            elif op == "!=":
                ok = current != value
            else:
                ok = current == value
            if not ok:
                return False
        return True

    def _select(self, domain, context):
        avec_archives = (context or {}).get("active_test") is False
        return [
            row for row in sorted(self.tasks.values(), key=lambda r: r["id"])
            if (avec_archives or row["active"]) and self._match(row, domain)
        ]

    @staticmethod
    def _project(row, fields):
        return {"id": row["id"], **{field: row[field] for field in fields}}

    def search(self, model, domain, context=None):
        return [row["id"] for row in self._select(domain, context)]

    def search_read(self, model, domain, fields, context=None):
        return [self._project(row, fields) for row in self._select(domain, context)]

    def iter_read(self, model, domain, fields, context=None):
        yield self.search_read(model, domain, fields, context)


class OdooSyncTest(NomaiaTestCase):

    def setUp(self):
        super().setUp()
        self.odoo = FakeOdoo()

    def _sync(self):
        return odoo_sync.sync(client=self.odoo, now=MAINTENANT)

    @staticmethod
    def _by_odoo(odoo_id):
        row = get_db().execute("SELECT id FROM task WHERE odoo_id = ?", (odoo_id,)).fetchone()
        return task_model.get_task(row["id"]) if row else None

    # --- Hierarchie --------------------------------------------------------

    def test_sync_imports_parent_and_subtasks(self):
        self.odoo.add(1, "Parametrer la paie", project=(50, "ALPHA"))
        self.odoo.add(2, "Regles salariales", parent=1, project=(50, "ALPHA"), sequence=2)
        self.odoo.add(3, "Bulletins test", parent=1, project=(50, "ALPHA"), sequence=1)
        self.odoo.add(4, "Rubrique prime", parent=2, project=(50, "ALPHA"))

        rapport = self._sync()

        self.assertEqual(rapport["created"], 4)
        self.assertEqual(rapport["projects_created"], 1)
        parent = self._by_odoo(1)
        self.assertIsNone(parent["parent_id"])
        self.assertEqual(parent["source"], "odoo")
        self.assertEqual(parent["project_name"], "ALPHA")
        etapes = task_model.list_subtasks(parent["id"])
        self.assertEqual([etape["title"] for etape in etapes], ["Bulletins test", "Regles salariales"])
        self.assertEqual(self._by_odoo(4)["parent_id"], self._by_odoo(2)["id"])

    def test_sync_imports_unassigned_ancestor(self):
        self.odoo.add(1, "Lot ventes", users=(COLLEGUE,))
        self.odoo.add(2, "Devis", parent=1)

        rapport = self._sync()

        self.assertEqual(rapport["total"], 2)
        self.assertEqual(self._by_odoo(2)["parent_id"], self._by_odoo(1)["id"])

    def test_sync_reparents_when_parent_changes(self):
        self.odoo.add(1, "A")
        self.odoo.add(2, "B")
        self.odoo.add(3, "Enfant", parent=1)
        self._sync()

        self.odoo.tasks[3]["parent_id"] = [2, "B"]
        rapport = self._sync()

        self.assertEqual(rapport["reparented"], 1)
        self.assertEqual(self._by_odoo(3)["parent_id"], self._by_odoo(2)["id"])

    # --- Idempotence et partage des champs ----------------------------------

    def test_sync_twice_creates_nothing(self):
        self.odoo.add(1, "Tache", project=(50, "ALPHA"))
        self.odoo.add(2, "Sous-tache", parent=1, project=(50, "ALPHA"))
        self._sync()

        rapport = self._sync()

        self.assertEqual((rapport["created"], rapport["updated"], rapport["reparented"]), (0, 0, 0))
        self.assertEqual(get_db().execute("SELECT COUNT(*) FROM task").fetchone()[0], 2)
        self.assertEqual(get_db().execute("SELECT COUNT(*) FROM project").fetchone()[0], 1)

    def test_sync_updates_odoo_fields_keeps_local_ones(self):
        self.odoo.add(1, "Ancien titre", priority="3", allocated_hours=2.0,
                      date_deadline="2026-11-05 17:00:00", description="<p>Ligne 1</p><p>Ligne&nbsp;2</p>")
        self._sync()
        tache = self._by_odoo(1)
        self.assertEqual((tache["priority"], tache["estimate_min"]), (1, 120))
        self.assertEqual(tache["deadline"], "2026-11-05")
        self.assertEqual(tache["description"], "Ligne 1\nLigne\xa02")
        task_model.update_task(tache["id"], {"priority": 3, "progress_pct": 40})

        self.odoo.tasks[1].update(name="Nouveau titre", priority="0", date_deadline=False)
        self._sync()

        tache = self._by_odoo(1)
        self.assertEqual(tache["title"], "Nouveau titre")
        self.assertIsNone(tache["deadline"])
        self.assertEqual((tache["priority"], tache["progress_pct"]), (3, 40))

    def test_sync_links_existing_project_by_name(self):
        existant = project_model.create_project("Alpha")
        self.odoo.add(1, "Tache", project=(50, "ALPHA"))

        rapport = self._sync()

        self.assertEqual(rapport["projects_created"], 0)
        self.assertEqual(self._by_odoo(1)["project_id"], existant["id"])
        self.assertEqual(project_model.find_by_odoo_id(50)["id"], existant["id"])

    # --- Cloture -----------------------------------------------------------

    def test_sync_closes_tasks_leaving_scope(self):
        for odoo_id in (1, 2, 3, 4):
            self.odoo.add(odoo_id, f"Tache {odoo_id}")
        self._sync()

        self.odoo.tasks[1]["state"] = "1_done"
        self.odoo.tasks[2]["active"] = False
        del self.odoo.tasks[3]
        self.odoo.tasks[4]["user_ids"] = [COLLEGUE]
        rapport = self._sync()

        self.assertEqual(rapport["closed"], 4)
        raisons = {}
        for odoo_id in (1, 2, 3, 4):
            tache = self._by_odoo(odoo_id)
            self.assertEqual(tache["status"], "done")
            raisons[odoo_id] = journal.list_for_task(tache["id"])[0]["content"]
        self.assertIn("terminee", raisons[1])
        self.assertIn("archivee", raisons[2])
        self.assertIn("supprimee", raisons[3])
        self.assertIn("reassignee", raisons[4])

    def test_sync_skips_tasks_without_project(self):
        self.odoo.add(1, "Bienvenue", project=None)
        self.odoo.add(2, "Tache projet")

        rapport = self._sync()

        self.assertEqual(rapport["total"], 1)
        self.assertIsNone(self._by_odoo(1))

    def test_sync_closes_imported_task_without_project(self):
        self.odoo.add(1, "Bienvenue")
        self._sync()

        self.odoo.tasks[1]["project_id"] = False
        self._sync()

        tache = self._by_odoo(1)
        self.assertEqual(tache["status"], "done")
        self.assertIn("sans projet", journal.list_for_task(tache["id"])[0]["content"])

    def test_sync_never_reopens_task_closed_locally(self):
        self.odoo.add(1, "Tache")
        self._sync()
        task_model.toggle_task(self._by_odoo(1)["id"])

        self._sync()

        self.assertEqual(self._by_odoo(1)["status"], "done")

    # --- Versions d'Odoo ---------------------------------------------------

    def test_sync_supports_odoo_18_schema(self):
        self.odoo = FakeOdoo(version=18)
        self.odoo.add(1, "Etoilee", priority="1")
        self.odoo.add(2, "Normale", parent=1)

        rapport = self._sync()

        self.assertEqual(rapport["created"], 2)
        self.assertEqual(self._by_odoo(1)["priority"], 2)
        self.assertEqual(self._by_odoo(2)["priority"], 3)
        self.assertEqual(odoo_sync.test_connection(self.odoo)["open_tasks"], 2)

    def test_json2_on_older_server_explains_redirect(self):
        client = OdooClient(OdooConfig(url="https://base.example.com", database="db", secret="k"))
        page = io.BytesIO(b"<!DOCTYPE html><html></html>")
        page.status = 200
        page.geturl = lambda: "https://base.example.com/web/login?redirect=%2Fjson%2F2"

        with patch("urllib.request.urlopen", return_value=page):
            with self.assertRaises(OdooRpcError) as contexte:
                client.authenticate()

        self.assertIn("JSON-RPC", str(contexte.exception))

    # --- Configuration et API ----------------------------------------------

    def test_build_client_rejects_incomplete_config(self):
        setting.set_value("odoo_url", "https://base.odoo.com")
        with self.assertRaises(OdooSyncError) as contexte:
            odoo_sync.build_client()
        self.assertIn("base", str(contexte.exception))
        self.assertIn("cle API", str(contexte.exception))

    def test_api_sync_reports_missing_config(self):
        reponse = self.client().post("/api/odoo/sync", headers=self.write_headers())

        self.assertEqual(reponse.status_code, 502)
        self.assertIn("incomplete", reponse.get_json()["error"])

    def test_api_status_returns_last_report(self):
        self.odoo.add(1, "Tache")
        self._sync()

        corps = self.client().get("/api/odoo").get_json()

        self.assertEqual(corps["last_report"]["created"], 1)
        self.assertEqual(corps["last_sync"], "2026-10-30 10:00:00")
