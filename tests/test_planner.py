"""Tests du plan du jour : capacite, marge, ordre des etapes."""
import unittest
from datetime import date, timedelta

from models import capture as capture_model
from models import setting
from models import task as task_model
from services import planner
from tests.base import NomaiaTestCase

TODAY = date(2026, 9, 16)


class PlanTest(NomaiaTestCase):
    """Le plan doit remplir la journee sans jamais la depasser."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")
        setting.set_value("plan_buffer_pct", "0")
        setting.set_value("plan_horizon_days", "3")

    def test_capacite_exclut_la_pause_dejeuner(self):
        plan = planner.build_plan(TODAY)
        self.assertEqual(plan["capacity_min"], 9 * 60)

    def test_la_marge_allonge_les_creneaux(self):
        task_model.create_task({"title": "Tache", "estimate_min": 60})
        sans_marge = planner.build_plan(TODAY)["slots"][0]["duration_min"]

        setting.set_value("plan_buffer_pct", "20")
        avec_marge = planner.build_plan(TODAY)["slots"][0]["duration_min"]

        self.assertEqual(sans_marge, 60)
        self.assertEqual(avec_marge, 72)

    def test_ce_qui_ne_rentre_pas_tombe_en_debordement(self):
        for index in range(4):
            task_model.create_task({"title": f"Bloc {index}", "estimate_min": 240})

        plan = planner.build_plan(TODAY)
        self.assertLessEqual(plan["planned_min"], plan["capacity_min"])
        self.assertTrue(plan["overflow"])

    def test_une_tache_hors_horizon_n_est_pas_planifiee(self):
        lointaine = (TODAY + timedelta(days=30)).isoformat()
        task_model.create_task({"title": "Plus tard", "deadline": lointaine})

        plan = planner.build_plan(TODAY)
        self.assertEqual(plan["slots"], [])
        self.assertEqual(plan["overflow"], [])

    def test_les_etapes_d_une_capture_gardent_leur_ordre(self):
        # L'IA emet les etapes dans l'ordre logique : corriger ne doit jamais
        # passer avant reproduire, meme si son score est plus eleve.
        capture = capture_model.create_capture(raw_text="bug a corriger")
        premiere = task_model.create_task(
            {"title": "Reproduire", "estimate_min": 120, "priority": 4,
             "capture_id": capture["id"]}
        )
        seconde = task_model.create_task(
            {"title": "Corriger", "estimate_min": 60, "priority": 1,
             "capture_id": capture["id"]}
        )

        ordre = [creneau["task"]["id"] for creneau in planner.build_plan(TODAY)["slots"]]
        self.assertEqual(ordre.index(premiere["id"]) < ordre.index(seconde["id"]), True)

    def test_une_tache_terminee_disparait_du_plan(self):
        tache = task_model.create_task({"title": "Deja faite", "estimate_min": 60})
        task_model.toggle_task(tache["id"])

        self.assertEqual(planner.build_plan(TODAY)["slots"], [])

    def test_charge_exprimee_en_pourcentage(self):
        task_model.create_task({"title": "Demi-journee", "estimate_min": 270})

        plan = planner.build_plan(TODAY)
        self.assertEqual(plan["load_pct"], 50)


if __name__ == "__main__":
    unittest.main()
