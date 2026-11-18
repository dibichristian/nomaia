"""Lot 8 : cloture legere de journee, compteur de reports, contexte Odoo du projet."""
from datetime import date, timedelta

from models import get_db
from models import project as project_model
from models import task as task_model
from services import day_close
from tests.base import NomaiaTestCase


class ClotureTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.today = date.today().isoformat()
        pid = db.execute("INSERT INTO project (name) VALUES ('ALPHA')").lastrowid
        self.task = db.execute("INSERT INTO task (title, project_id) VALUES ('Paie', ?)", (pid,)).lastrowid
        db.execute("UPDATE task SET status = 'done', done_at = ? WHERE id = ?", (self.today + " 17:00:00", self.task))
        db.execute("INSERT INTO session (task_id, started_at, ended_at, duration_min) VALUES (?, ?, ?, 45)",
                   (self.task, self.today + " 09:00:00", self.today + " 09:45:00"))
        db.execute("INSERT INTO note (task_id, project_id, content, kind) VALUES (?, ?, 'Règles saisies', 'work')",
                   (self.task, pid))
        db.commit()

    def test_recapitulatif_calcule(self):
        data = self.client().get(f"/api/day/recap?date={self.today}").get_json()
        self.assertEqual([t["title"] for t in data["done"]], ["Paie"])
        self.assertEqual(data["tracked_min"], 45)
        self.assertEqual(data["tracked_by_project"], [{"project": "ALPHA", "minutes": 45}])
        self.assertEqual(data["journal"][0]["content"], "Règles saisies")
        self.assertEqual(data["tomorrow"], "")

    def test_note_du_soir_relue_le_lendemain(self):
        client, h = self.client(), self.write_headers()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        r = client.post("/api/day/close", json={"date": yesterday, "tomorrow": "Finir la paie, relancer Lucie"}, headers=h)
        self.assertEqual(r.status_code, 200)
        client.post("/api/day/close", json={"date": yesterday, "tomorrow": "Finir la paie"}, headers=h)  # remplace
        note = client.get(f"/api/day/morning?date={self.today}").get_json()["note"]
        self.assertEqual(note, {"day": yesterday, "text": "Finir la paie"})
        self.assertEqual(day_close.notes_between(yesterday, self.today), [{"day": yesterday, "tomorrow": "Finir la paie"}])

    def test_note_du_vendredi_lue_le_lundi_et_pas_au_dela_d_une_semaine(self):
        monday = date(2026, 11, 9)
        day_close.close("2026-11-06", "Lundi : démo NORDIK")
        self.assertEqual(day_close.morning_note(monday)["text"], "Lundi : démo NORDIK")
        self.assertIsNone(day_close.morning_note(monday + timedelta(days=10)))

    def test_cloture_refuse_un_secret_et_une_date_invalide(self):
        h = self.write_headers()
        self.assertEqual(self.client().post("/api/day/close", json={"tomorrow": "mdp: Azerty2026"}, headers=h).status_code, 400)
        self.assertEqual(self.client().get("/api/day/recap?date=06/11/2026").status_code, 400)


class PetitsPlusTest(NomaiaTestCase):
    def test_echeance_repoussee_comptee(self):
        task = task_model.create_task({"title": "Migrer", "deadline": "2026-11-10"})
        task_model.update_task(task["id"], {"deadline": "2026-11-15"})
        task_model.update_task(task["id"], {"deadline": "2026-11-12"})  # avancee : ne compte pas
        task_model.update_task(task["id"], {"deadline": "2026-11-20"})
        self.assertEqual(task_model.get_task(task["id"])["deadline_shifts"], 2)
        sans = task_model.create_task({"title": "Sans échéance"})
        task_model.update_task(sans["id"], {"deadline": "2026-11-20"})  # premiere pose : ne compte pas
        self.assertEqual(task_model.get_task(sans["id"])["deadline_shifts"], 0)

    def test_contexte_odoo_du_projet(self):
        pid = project_model.create_project("ORION")["id"]
        project_model.update_project(pid, {"odoo_version": " 17.0 ", "client": "ORION SA"})
        projet = project_model.get_project(pid)
        self.assertEqual((projet["odoo_version"], projet["client"]), ("17.0", "ORION SA"))
        project_model.update_project(pid, {"client": ""})
        self.assertIsNone(project_model.get_project(pid)["client"])


class ColonneDuPlanTest(NomaiaTestCase):
    def test_indicateurs_et_colonne_laterale(self):
        db = get_db()
        pid = db.execute("INSERT INTO project (name, deadline) VALUES ('ORION', ?)",
                         ((date.today() - timedelta(days=3)).isoformat(),)).lastrowid
        db.execute("INSERT INTO task (title, project_id, deadline) VALUES ('En retard', ?, ?)",
                   (pid, (date.today() - timedelta(days=1)).isoformat()))
        db.execute("INSERT INTO task (title, side, created_at) VALUES ('Fichier client', 'client', ?)",
                   ((date.today() - timedelta(days=4)).isoformat() + " 09:00:00",))
        db.execute("INSERT INTO event (title, starts_at, ends_at) VALUES ('Point hebdo', ?, ?)",
                   (date.today().isoformat() + " 11:00", date.today().isoformat() + " 11:30"))
        db.commit()
        data = self.client().get("/api/plan/side").get_json()
        self.assertEqual(data["kpis"]["late_count"], 1)
        self.assertEqual(data["kpis"]["client"], {"count": 1, "oldest_days": 4})
        self.assertEqual([e["title"] for e in data["agenda"]], ["Point hebdo"])
        self.assertEqual(data["signals"][0]["name"], "ORION")
        self.assertEqual(data["signals"][0]["level"], "danger")
        self.assertEqual(self.client().get("/api/plan/side?date=06/11").status_code, 400)

    def test_page_du_plan_porte_la_mise_en_page_de_la_maquette(self):
        html = self.client().get("/plan").get_data(as_text=True)
        for marqueur in ('id="pl-kpis"', 'class="plan-aside"', 'id="ps-agenda"', 'id="ps-signals"', 'id="ps-inbox"'):
            self.assertIn(marqueur, html)
