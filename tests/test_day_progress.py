"""Avancement du jour : la valeur affichee par l'anneau du logo."""
from datetime import date, timedelta
from unittest.mock import patch

from models import get_db, setting
from services import planner
from tests.base import NomaiaTestCase


class AvancementDuJourTest(NomaiaTestCase):
    """day_progress : fait / (fait + encore planifie), marge comprise."""

    def setUp(self):
        super().setUp()
        setting.set_value("plan_buffer_pct", "0")

    def _done(self, title, estimate, day=None):
        """Cree une tache terminee le jour donne (aujourd'hui par defaut)."""
        jour = (day or date.today()).isoformat()
        db = get_db()
        db.execute(
            "INSERT INTO task (title, estimate_min, status, done_at) VALUES (?, ?, 'done', ?)",
            (title, estimate, jour + " 10:00:00"),
        )
        db.commit()

    def _progress(self, planned_min):
        """Avancement avec un plan restant fixe, pour ne dependre ni de l'heure ni de l'agenda."""
        with patch.object(planner, "build_plan", return_value={"planned_min": planned_min}):
            return planner.day_progress()

    def test_journee_vide(self):
        self.assertEqual(self._progress(0), {"pct": 0, "done_min": 0, "planned_min": 0})

    def test_moitie_faite(self):
        self._done("Corriger l'export", 60)
        self.assertEqual(self._progress(60)["pct"], 50)

    def test_tout_fait(self):
        self._done("Corriger l'export", 45)
        self.assertEqual(self._progress(0)["pct"], 100)

    def test_taches_d_hier_ignorees(self):
        self._done("Vieille tache", 120, day=date.today() - timedelta(days=1))
        self.assertEqual(self._progress(60)["pct"], 0)

    def test_marge_du_plan_appliquee(self):
        setting.set_value("plan_buffer_pct", "50")
        self._done("Corriger l'export", 40)
        resultat = self._progress(60)
        self.assertEqual(resultat["done_min"], 60)
        self.assertEqual(resultat["pct"], 50)

    def test_route_protegee_et_json(self):
        with patch.object(planner, "build_plan", return_value={"planned_min": 30}):
            reponse = self.client().get("/api/day/progress")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["planned_min"], 30)
        self.assertEqual(self.app.test_client().get("/api/day/progress").status_code, 401)
