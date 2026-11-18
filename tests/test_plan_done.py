"""Plan du jour : taches deja faites aujourd'hui (bouton « Faites »)."""
from datetime import date, timedelta

from models import get_db, setting
from services import planner
from tests.base import NomaiaTestCase


class TachesFaitesTest(NomaiaTestCase):
    """done_today : heure, temps du jour et periode des taches terminees."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")
        self.today = date.today().isoformat()
        self.yesterday = (date.today() - timedelta(days=1)).isoformat()

    def _task(self, title, done_at=None):
        """Cree une tache, terminee a done_at ('AAAA-MM-JJ HH:MM:SS') si fourni."""
        db = get_db()
        task_id = db.execute("INSERT INTO task (title) VALUES (?)", (title,)).lastrowid
        if done_at:
            db.execute("UPDATE task SET status = 'done', done_at = ? WHERE id = ?", (done_at, task_id))
        db.commit()
        return task_id

    def _session(self, task_id, started_at, minutes):
        """Ajoute une session terminee, datee explicitement."""
        db = get_db()
        db.execute(
            "INSERT INTO session (task_id, started_at, ended_at, duration_min) VALUES (?, ?, ?, ?)",
            (task_id, started_at, started_at, minutes),
        )
        db.commit()

    def _done_today(self):
        """Taches faites retournees par l'API, indexees par titre."""
        data = self.client().get("/api/plan").get_json()
        return {task["title"]: task for task in data["done_today"]}

    def test_present_seulement_aujourd_hui(self):
        self._task("Paie", f"{self.today} 10:00:00")
        client = self.client()
        self.assertEqual(len(client.get("/api/plan").get_json()["done_today"]), 1)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertNotIn("done_today", client.get(f"/api/plan?date={tomorrow}").get_json())
        self.assertNotIn("done_today", client.get(f"/api/plan?date={self.yesterday}").get_json())

    def test_heure_premiere_session_du_jour_sinon_cloture(self):
        avec = self._task("Avec sessions", f"{self.today} 16:00:00")
        self._session(avec, f"{self.yesterday} 08:00:00", 30)
        self._session(avec, f"{self.today} 14:10:00", 20)
        self._session(avec, f"{self.today} 09:15:00", 40)
        self._task("Sans session", f"{self.today} 11:42:00")
        taches = self._done_today()
        self.assertEqual(taches["Avec sessions"]["at"], "09:15")
        self.assertEqual(taches["Sans session"]["at"], "11:42")

    def test_temps_du_jour_seulement(self):
        task_id = self._task("Migration", f"{self.today} 17:00:00")
        self._session(task_id, f"{self.yesterday} 10:00:00", 90)
        self._session(task_id, f"{self.today} 09:00:00", 25)
        self._session(task_id, f"{self.today} 15:00:00", 35)
        tache = self._done_today()["Migration"]
        self.assertEqual(tache["tracked_today_min"], 60)
        # Le temps sur toute la vie de la tache reste disponible a cote.
        self.assertEqual(tache["tracked_min"], 150)

    def test_periode_rangee_selon_l_heure(self):
        cas = {
            "Avant le debut": "07:30",
            "Matin": "10:00",
            "Pause dejeuner": "12:30",
            "Apres-midi": "15:00",
            "Apres la fin": "19:30",
        }
        for title, heure in cas.items():
            self._task(title, f"{self.today} {heure}:00")
        taches = self._done_today()
        self.assertEqual(
            {title: taches[title]["window"] for title in cas},
            {
                "Avant le debut": "morning",
                "Matin": "morning",
                "Pause dejeuner": "afternoon",
                "Apres-midi": "afternoon",
                "Apres la fin": "afternoon",
            },
        )
        # Fil horaire : tri par heure.
        self.assertEqual([t["at"] for t in planner.done_today(date.today())],
                         ["07:30", "10:00", "12:30", "15:00", "19:30"])

    def test_terminees_hier_et_ouvertes_absentes(self):
        self._task("Hier", f"{self.yesterday} 17:00:00")
        self._task("Encore ouverte")
        self._task("Aujourd'hui", f"{self.today} 09:00:00")
        self.assertEqual(list(self._done_today()), ["Aujourd'hui"])

    def test_jour_non_ouvre_sans_periode(self):
        repos = date.today().weekday()
        setting.set_value("work_days", ",".join(str(d) for d in range(7) if d != repos))
        self._task("Dimanche", f"{self.today} 10:00:00")
        self.assertIsNone(planner.done_today(date.today())[0]["window"])
