"""Tests du plan pilote a la main : epinglage, etapes, jours passes.

Le classement automatique propose ; l'utilisateur dispose. Une tache qu'il veut
faire maintenant passe devant, quel que soit son score, et un jour passe se
relit comme un bilan, pas comme un plan.
"""
from datetime import date, timedelta

from models import get_db, setting
from models import task as task_model
from models.task import ValidationError
from services import planner, tools
from tests.base import NomaiaTestCase


class EpinglageTest(NomaiaTestCase):
    """Ordre choisi a la main dans le plan d'un jour."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")
        setting.set_value("plan_buffer_pct", "0")
        setting.set_value("plan_horizon_days", "3")
        self.jour = date.today()
        self.urgente = task_model.create_task({"title": "Urgente", "priority": 1, "estimate_min": 60})
        self.banale = task_model.create_task({"title": "Banale", "priority": 4, "estimate_min": 30})

    def _titres(self):
        return [slot["task"]["title"] for slot in planner.build_plan(self.jour)["slots"]]

    def test_maintenant_passe_devant_le_score(self):
        self.assertEqual(self._titres()[0], "Urgente")
        task_model.pin_task(self.banale["id"], self.jour.isoformat(), first=True)

        plan = planner.build_plan(self.jour)
        self.assertEqual(plan["slots"][0]["task"]["title"], "Banale")
        self.assertTrue(plan["slots"][0]["pinned"])
        self.assertEqual(plan["slots"][0]["start"], "08:00")
        self.assertEqual(plan["pinned_count"], 1)

    def test_ordre_complet_respecte(self):
        troisieme = task_model.create_task({"title": "Troisieme", "priority": 2, "estimate_min": 30})
        task_model.pin_tasks(
            self.jour.isoformat(), [troisieme["id"], self.banale["id"], self.urgente["id"]]
        )
        self.assertEqual(self._titres(), ["Troisieme", "Banale", "Urgente"])

    def test_epingle_hors_horizon_planifiee_quand_meme(self):
        lointaine = task_model.create_task(
            {"title": "Lointaine", "deadline": (self.jour + timedelta(days=40)).isoformat()}
        )
        self.assertNotIn("Lointaine", self._titres())
        task_model.pin_task(lointaine["id"], self.jour.isoformat())
        self.assertEqual(self._titres()[0], "Lointaine")

    def test_epingle_d_un_autre_jour_ignoree(self):
        demain = (self.jour + timedelta(days=1)).isoformat()
        task_model.pin_task(self.banale["id"], demain)
        self.assertEqual(self._titres()[0], "Urgente")

    def test_retour_au_classement_automatique(self):
        task_model.pin_task(self.banale["id"], self.jour.isoformat())
        task_model.unpin_task(self.banale["id"])
        self.assertEqual(self._titres()[0], "Urgente")

        task_model.pin_task(self.banale["id"], self.jour.isoformat())
        task_model.pin_tasks(self.jour.isoformat(), [])
        self.assertEqual(self._titres()[0], "Urgente")

    def test_tache_terminee_non_epinglable(self):
        task_model.toggle_task(self.banale["id"])
        with self.assertRaises(ValidationError):
            task_model.pin_task(self.banale["id"], self.jour.isoformat())

    def test_tache_detaillee_planifiee_par_ses_etapes(self):
        task_model.create_task({"title": "Etape 1", "parent_id": self.urgente["id"], "estimate_min": 20})
        titres = self._titres()
        self.assertIn("Etape 1", titres)
        self.assertNotIn("Urgente", titres)

    def test_route_d_ordre_et_outil_du_chat(self):
        reponse = self.client().post(
            "/api/plan/order",
            json={"date": self.jour.isoformat(), "task_ids": [self.banale["id"], self.urgente["id"]]},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["pinned"], [self.banale["id"], self.urgente["id"]])

        resultat = tools.execute("pin_task", {"task_id": self.urgente["id"], "position": "now"})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(self._titres()[:2], ["Urgente", "Banale"])

    def test_route_maintenant(self):
        reponse = self.client().post(
            f"/api/plan/pin/{self.banale['id']}",
            json={"date": self.jour.isoformat(), "position": "now"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(self._titres()[0], "Banale")


class JourPasseTest(NomaiaTestCase):
    """Un jour ecoule montre ce qui a ete boucle et permet le rattrapage."""

    def setUp(self):
        super().setUp()
        self.hier = date.today() - timedelta(days=1)
        self.tache = task_model.create_task({"title": "Oubliee"})
        # Creee avant hier : elle a pu etre faite hier.
        get_db().execute(
            "UPDATE task SET created_at = ? WHERE id = ?",
            ((self.hier - timedelta(days=2)).isoformat() + " 09:00:00", self.tache["id"]),
        )
        get_db().commit()

    def test_case_oubliee_close_a_la_bonne_date(self):
        task_model.complete_on(self.tache["id"], self.hier.isoformat())

        tache = task_model.get_task(self.tache["id"])
        self.assertEqual(tache["status"], "done")
        self.assertTrue(tache["done_at"].startswith(self.hier.isoformat()))

    def test_futur_refuse(self):
        demain = (date.today() + timedelta(days=1)).isoformat()
        with self.assertRaises(ValidationError):
            task_model.complete_on(self.tache["id"], demain)

    def test_bilan_du_jour(self):
        recente = task_model.create_task({"title": "Creee aujourd'hui"})
        revue = planner.day_review(self.hier)
        self.assertEqual([task["title"] for task in revue["open"]], ["Oubliee"])
        self.assertNotIn(recente["id"], [task["id"] for task in revue["open"]])

        task_model.complete_on(self.tache["id"], self.hier.isoformat())
        revue = planner.day_review(self.hier)
        self.assertEqual([task["title"] for task in revue["done"]], ["Oubliee"])
        self.assertEqual(revue["open"], [])

    def test_api_jour_passe_et_rattrapage(self):
        client = self.client()
        corps = client.get(f"/api/plan?date={self.hier.isoformat()}").get_json()
        self.assertTrue(corps["past"])
        self.assertNotIn("slots", corps)

        reponse = client.post(
            f"/api/tasks/{self.tache['id']}/done-on",
            json={"day": self.hier.isoformat()},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)
        corps = client.get(f"/api/plan?date={self.hier.isoformat()}").get_json()
        self.assertEqual(len(corps["done"]), 1)

    def test_fiche_avec_etapes(self):
        task_model.create_task({"title": "Etape", "parent_id": self.tache["id"]})
        corps = self.client().get(f"/api/tasks/{self.tache['id']}").get_json()
        self.assertEqual([step["title"] for step in corps["subtasks"]], ["Etape"])
