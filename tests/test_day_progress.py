"""Avancement du jour : la valeur affichee par l'anneau du logo."""
from datetime import date, timedelta
from unittest.mock import patch

from models import get_db, setting
from models import plan_day as plan_day_model
from services import planner
from tests.base import NomaiaTestCase


class AvancementDuJourTest(NomaiaTestCase):
    """day_progress : fait / (fait + encore planifie), marge comprise."""

    def setUp(self):
        super().setUp()
        setting.set_value("plan_buffer_pct", "0")

    def _done(self, title, estimate, day=None, space="pro"):
        """Cree une tache terminee le jour donne (aujourd'hui par defaut)."""
        jour = (day or date.today()).isoformat()
        db = get_db()
        db.execute(
            "INSERT INTO task (title, estimate_min, status, done_at, space) VALUES (?, ?, 'done', ?, ?)",
            (title, estimate, jour + " 10:00:00", space),
        )
        db.commit()

    def _progress(self, planned_min):
        """Avancement avec un plan restant fixe, pour ne dependre ni de l'heure ni de l'agenda."""
        with patch.object(planner, "build_plan", return_value={"planned_min": planned_min}):
            return planner.day_progress()

    def test_journee_vide(self):
        resultat = self._progress(0)
        self.assertEqual((resultat["pct"], resultat["done_min"], resultat["planned_min"]), (0, 0, 0))
        self.assertIsNone(resultat["space"])

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


class AvancementParEspaceEtParJourTest(NomaiaTestCase):
    """day_progress selon l'espace affiche et pour un jour passe."""

    def setUp(self):
        super().setUp()
        setting.set_value("plan_buffer_pct", "0")
        self.yesterday = date.today() - timedelta(days=1)

    _done = AvancementDuJourTest._done

    @staticmethod
    def _plan(pro_min, perso_min):
        """Plan restant fixe, un creneau par espace."""
        slots = [
            {"duration_min": pro_min, "task": {"space": "pro"}},
            {"duration_min": perso_min, "task": {"space": "perso"}},
        ]
        return {"planned_min": pro_min + perso_min, "slots": slots}

    def test_aujourd_hui_par_espace(self):
        self._done("Pro faite", 60, space="pro")
        self._done("Perso faite", 30, space="perso")
        with patch.object(planner, "build_plan", return_value=self._plan(60, 0)):
            self.assertEqual(planner.day_progress(space="pro")["pct"], 50)
            self.assertEqual(planner.day_progress(space="perso")["pct"], 100)
            self.assertEqual(planner.day_progress()["pct"], 60)

    def test_jour_passe_sans_trace(self):
        self._done("Faite hier", 60, day=self.yesterday)
        resultat = planner.day_progress(self.yesterday)
        self.assertIsNone(resultat["pct"])
        self.assertEqual(resultat["done_min"], 60)

    def test_jour_passe_par_espace(self):
        self._done("Pro faite", 60, day=self.yesterday, space="pro")
        plan_day_model.record(self.yesterday.isoformat(), 300, 480, {"pro": 240, "perso": 60})
        with patch.object(planner, "build_plan", side_effect=AssertionError("pas de plan pour le passe")):
            pro = planner.day_progress(self.yesterday, "pro")
            perso = planner.day_progress(self.yesterday, "perso")
            tout = planner.day_progress(self.yesterday)
        self.assertEqual((pro["pct"], pro["planned_min"], pro["by_space"]), (25, 180, True))
        self.assertEqual(perso["pct"], 0)
        self.assertEqual(tout["pct"], 20)

    def test_jour_passe_sans_detail_retombe_sur_le_total(self):
        self._done("Pro faite", 60, day=self.yesterday)
        plan_day_model.record(self.yesterday.isoformat(), 120, 480)
        resultat = planner.day_progress(self.yesterday, "perso")
        self.assertFalse(resultat["by_space"])
        self.assertEqual(resultat["pct"], 50)

    def test_fait_hors_plan_plafonne_a_100(self):
        self._done("Imprevue", 90, day=self.yesterday)
        plan_day_model.record(self.yesterday.isoformat(), 60, 480, {"pro": 60, "perso": 0})
        resultat = planner.day_progress(self.yesterday, "pro")
        self.assertEqual((resultat["pct"], resultat["planned_min"]), (100, 0))

    def test_route_jour_et_espace(self):
        setting.set_value("space_filter", "pro")
        plan_day_model.record(self.yesterday.isoformat(), 100, 480, {"pro": 100, "perso": 0})
        self._done("Pro faite", 50, day=self.yesterday)
        reponse = self.client().get("/api/day/progress?date=" + self.yesterday.isoformat())
        self.assertEqual(reponse.status_code, 200)
        data = reponse.get_json()
        self.assertEqual((data["space"], data["pct"], data["date"]), ("pro", 50, self.yesterday.isoformat()))
        self.assertEqual(self.client().get("/api/day/progress?date=hier").status_code, 400)
