"""Plan du jour : indicateurs lus au jour affiche et leur detail."""
from datetime import date, timedelta

from models import get_db
from services import plan_kpis
from tests.base import NomaiaTestCase


class IndicateursPlanTest(NomaiaTestCase):
    """Temps suivi, retards et attente client, aujourd'hui et un jour passe."""

    def setUp(self):
        super().setUp()
        self.today = date.today()
        self.day = self.today - timedelta(days=3)

    def _task(self, title, created, deadline=None, done_at=None, side=None):
        """Cree une tache datee ; terminee a done_at si fourni."""
        db = get_db()
        task_id = db.execute(
            "INSERT INTO task (title, created_at, deadline, side, status, done_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, f"{created.isoformat()} 08:00:00", deadline and deadline.isoformat(), side,
             "done" if done_at else "todo", done_at and f"{done_at.isoformat()} 17:00:00"),
        ).lastrowid
        db.commit()
        return task_id

    def _session(self, task_id, day, minutes, hour="09:00"):
        """Ajoute une session terminee."""
        started = f"{day.isoformat()} {hour}:00"
        db = get_db()
        db.execute(
            "INSERT INTO session (task_id, started_at, ended_at, duration_min) VALUES (?, ?, ?, ?)",
            (task_id, started, started, minutes),
        )
        db.commit()

    def _person(self, name):
        db = get_db()
        person_id = db.execute("INSERT INTO person (name) VALUES (?)", (name,)).lastrowid
        db.commit()
        return person_id

    def test_retard_au_jour_passe(self):
        week_ago = self.today - timedelta(days=7)
        # En retard ce jour-la, close apres : comptee dans le passe, plus aujourd'hui.
        self._task("Close apres", week_ago, deadline=self.day - timedelta(days=1),
                   done_at=self.day + timedelta(days=1))
        # Close le jour meme : plus en retard a la fin de ce jour-la.
        self._task("Close ce jour", week_ago, deadline=self.day - timedelta(days=1), done_at=self.day)
        # Echeance posterieure au jour vu : pas encore en retard ce jour-la.
        self._task("Echeance apres", week_ago, deadline=self.day + timedelta(days=1))
        # Creee apres le jour vu : n'existait pas.
        self._task("Creee apres", self.day + timedelta(days=1), deadline=self.day - timedelta(days=2))

        titles = [item["title"] for item in plan_kpis.detail("late", self.day)["items"]]
        self.assertEqual(titles, ["Close apres"])
        self.assertEqual(plan_kpis.kpis(self.day)["late_count"], 1)
        today_titles = {item["title"] for item in plan_kpis.detail("late", self.today)["items"]}
        self.assertEqual(today_titles, {"Echeance apres", "Creee apres"})

    def test_attente_client_au_jour_passe(self):
        week_ago = self.today - timedelta(days=7)
        self._task("PV recette", week_ago, side="client", done_at=self.today)
        self._task("Cote nous", week_ago, side="us")
        person_id = self._person("Lucie")
        db = get_db()
        db.execute("INSERT INTO waiting (person_id, subject, since) VALUES (?, 'Acces Odoo', ?)",
                   (person_id, (self.day - timedelta(days=2)).isoformat()))
        db.execute("INSERT INTO waiting (person_id, subject, since) VALUES (?, 'Plus tard', ?)",
                   (person_id, self.today.isoformat()))
        db.commit()

        data = plan_kpis.detail("client", self.day)
        self.assertEqual([item["title"] for item in data["items"]], ["PV recette", "Acces Odoo"])
        self.assertEqual(data["items"][0]["days"], 4)
        kpis = plan_kpis.kpis(self.day)
        self.assertEqual(kpis["client"], {"count": 2, "oldest_days": 4})

    def test_temps_suivi_du_jour_vu(self):
        task_id = self._task("Paie", self.today - timedelta(days=7))
        self._session(task_id, self.day, 40, "10:00")
        self._session(task_id, self.day, 20, "08:30")
        self._session(task_id, self.today, 15)

        data = plan_kpis.detail("tracked", self.day)
        self.assertEqual([item["start"] for item in data["items"]], ["08:30", "10:00"])
        self.assertEqual(data["total_min"], 60)
        self.assertEqual(plan_kpis.kpis(self.day)["tracked_min"], 60)
        self.assertIsNone(plan_kpis.kpis(self.today + timedelta(days=1))["tracked_min"])

    def test_route_detail(self):
        client = self.client()
        response = client.get(f"/api/plan/kpi/tracked?date={self.day.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["day"], self.day.isoformat())
        self.assertEqual(client.get("/api/plan/kpi/inconnu").status_code, 404)
        self.assertEqual(client.get("/api/plan/kpi/late?date=2026-13-01").status_code, 400)

    def test_colonne_laterale_indicateurs_du_jour_vu(self):
        task_id = self._task("Paie", self.today - timedelta(days=7))
        self._session(task_id, self.day, 45)
        data = self.client().get(f"/api/plan/side?date={self.day.isoformat()}").get_json()
        self.assertEqual(data["kpis"]["tracked_min"], 45)
