"""Plan du jour : historique des heures planifiees et suivies, barre temps reel."""
from datetime import date, timedelta

from models import get_db, setting
from models import plan_day as plan_day_model
from services import plan_history
from tests.base import NomaiaTestCase


class HistoriquePlanTest(NomaiaTestCase):
    """Trace du planifie, regroupement du suivi par jour et route d'historique."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")
        setting.set_value("plan_buffer_pct", "0")
        # Tous les jours ouvres : les tests ne dependent pas du jour de la semaine.
        setting.set_value("work_days", "0,1,2,3,4,5,6")
        self.today = date.today()

    def _task(self, title, estimate_min=60, done_at=None):
        """Cree une tache, terminee a done_at ('AAAA-MM-JJ HH:MM:SS') si fourni."""
        db = get_db()
        task_id = db.execute(
            "INSERT INTO task (title, estimate_min) VALUES (?, ?)", (title, estimate_min)
        ).lastrowid
        if done_at:
            db.execute("UPDATE task SET status = 'done', done_at = ? WHERE id = ?", (done_at, task_id))
        db.commit()
        return task_id

    def _session(self, task_id, day, minutes):
        """Ajoute une session terminee un jour donne."""
        started = f"{day.isoformat()} 09:00:00"
        db = get_db()
        db.execute(
            "INSERT INTO session (task_id, started_at, ended_at, duration_min) VALUES (?, ?, ?, ?)",
            (task_id, started, started, minutes),
        )
        db.commit()

    def test_record_planifie_a_l_ouverture_du_plan(self):
        self._task("Ouverte", estimate_min=90)
        self.client().get("/api/plan")
        record = plan_day_model.between(self.today.isoformat(), self.today.isoformat())
        self.assertEqual(record[self.today.isoformat()]["planned_min"], 90)
        self.assertGreater(record[self.today.isoformat()]["capacity_min"], 0)

    def test_record_planifie_stable_apres_cloture_d_une_tache(self):
        task_id = self._task("A boucler", estimate_min=60)
        self._task("Autre", estimate_min=30)
        client = self.client()
        client.get("/api/plan")
        db = get_db()
        db.execute(
            "UPDATE task SET status = 'done', done_at = ? WHERE id = ?",
            (f"{self.today.isoformat()} 10:00:00", task_id),
        )
        db.commit()
        client.get("/api/plan")
        record = plan_day_model.between(self.today.isoformat(), self.today.isoformat())
        self.assertEqual(record[self.today.isoformat()]["planned_min"], 90)

    def test_suivi_regroupe_par_jour(self):
        yesterday = self.today - timedelta(days=1)
        task_id = self._task("Suivie")
        self._session(task_id, yesterday, 40)
        self._session(task_id, yesterday, 20)
        self._session(task_id, self.today, 15)
        tracked = plan_history.tracked_by_day(yesterday, self.today)
        self.assertEqual(tracked[yesterday.isoformat()], 60)
        self.assertEqual(tracked[self.today.isoformat()], 15)

    def test_historique_jour_sans_trace_de_plan(self):
        yesterday = self.today - timedelta(days=1)
        self._session(self._task("Hier"), yesterday, 45)
        plan_day_model.record(self.today.isoformat(), 300, 540)
        data = self.client().get(
            f"/api/stats/hours?start={yesterday.isoformat()}&end={self.today.isoformat()}"
        ).get_json()
        self.assertEqual([row["date"] for row in data["days"]], [yesterday.isoformat(), self.today.isoformat()])
        rows = {row["date"]: row for row in data["days"]}
        self.assertIsNone(rows[yesterday.isoformat()]["planned_min"])
        self.assertEqual(rows[yesterday.isoformat()]["tracked_min"], 45)
        self.assertEqual(rows[self.today.isoformat()]["planned_min"], 300)
        self.assertEqual(data["totals"]["planned_min"], 300)
        self.assertEqual(data["totals"]["tracked_min"], 45)
        self.assertEqual(data["totals"]["tracked_on_planned_min"], 0)
        self.assertEqual(data["totals"]["planned_days"], 1)

    def test_historique_ecarte_les_jours_a_venir(self):
        tomorrow = self.today + timedelta(days=1)
        data = self.client().get(
            f"/api/stats/hours?start={self.today.isoformat()}&end={(tomorrow + timedelta(days=5)).isoformat()}"
        ).get_json()
        self.assertEqual(data["end"], self.today.isoformat())
        self.assertEqual([row["date"] for row in data["days"]], [self.today.isoformat()])

    def test_historique_periode_semaine_par_defaut(self):
        response = self.client().get("/api/stats/hours")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["truncated"])

    def test_historique_refuse_dates_invalides(self):
        client = self.client()
        self.assertEqual(client.get("/api/stats/hours?start=2026-13-01&end=2026-13-02").status_code, 400)
        self.assertEqual(client.get("/api/stats/hours?start=2026-11-10&end=2026-11-01").status_code, 400)

    def test_historique_periode_trop_longue_ramenee(self):
        start = self.today - timedelta(days=plan_history.MAX_DAYS + 30)
        data = plan_history.history(start, self.today)
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["days"]), plan_history.MAX_DAYS)

    def test_jour_passe_porte_temps_reel_et_planifie(self):
        yesterday = self.today - timedelta(days=1)
        self._session(self._task("Hier"), yesterday, 50)
        plan_day_model.record(yesterday.isoformat(), 240, 480)
        data = self.client().get(f"/api/plan?date={yesterday.isoformat()}").get_json()
        self.assertTrue(data["past"])
        self.assertEqual(data["tracked_day_min"], 50)
        self.assertEqual(data["planned_day_min"], 240)
        self.assertEqual(data["day_capacity_min"], 480)

    def test_jour_futur_sans_temps_reel(self):
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        data = self.client().get(f"/api/plan?date={tomorrow}").get_json()
        self.assertNotIn("tracked_day_min", data)
        self.assertEqual(plan_day_model.between(tomorrow, tomorrow), {})
