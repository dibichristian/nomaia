"""Tests des rendez-vous quotidiens : un message par jour, jamais deux."""
import unittest
from datetime import date, datetime, timedelta

from models import setting
from models import task as task_model
from services import heartbeat
from tests.base import NomaiaTestCase


class FauxBridge:
    """Passerelle Telegram factice : retient ce qui aurait ete envoye."""

    def __init__(self):
        self.messages = []
        self.epingles = 0

    def notify_owner(self, text):
        self.messages.append(text)
        return True

    def update_pinned_digest(self):
        self.epingles += 1
        return True

    def refresh_digest_if_stale(self):
        return False


class BriefTest(NomaiaTestCase):
    """Contenu des deux points de la journee."""

    def test_point_du_matin_muet_sans_tache(self):
        self.assertIsNone(heartbeat.morning_brief())

    def test_point_du_matin_annonce_la_charge(self):
        task_model.create_task({"title": "Corriger le crash", "estimate_min": 90})

        texte = heartbeat.morning_brief()
        self.assertIn("Plan du jour", texte)
        self.assertIn("Charge", texte)
        self.assertIn("Corriger le crash", texte)

    def test_point_du_matin_signale_la_surcharge(self):
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "09:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")
        for index in range(5):
            task_model.create_task({"title": f"Bloc {index}", "estimate_min": 120})

        self.assertIn("Ne rentre pas dans la journee", heartbeat.morning_brief())

    def test_point_du_soir_muet_sans_activite(self):
        self.assertIsNone(heartbeat.evening_brief())

    def test_point_du_soir_nomme_les_taches_qui_derivent(self):
        vieille = (date.today() - timedelta(days=10)).isoformat()
        task_model.create_task({"title": "Migrer le module", "deadline": vieille})

        texte = heartbeat.evening_brief()
        self.assertIn("derivent", texte)
        self.assertIn("Migrer le module", texte)

    def test_une_tache_juste_en_retard_ne_derive_pas_encore(self):
        hier = (date.today() - timedelta(days=1)).isoformat()
        task_model.create_task({"title": "Tache d'hier", "deadline": hier})

        self.assertEqual(heartbeat.drifting_tasks(), [])

    def test_seuil_de_derive_configurable(self):
        avant_hier = (date.today() - timedelta(days=2)).isoformat()
        task_model.create_task({"title": "Tache", "deadline": avant_hier})

        setting.set_value("drift_alert_days", "1")
        self.assertEqual(len(heartbeat.drifting_tasks()), 1)


class ScheduleTest(NomaiaTestCase):
    """Declenchement : une fois par jour, et pas de rattrapage tardif."""

    def setUp(self):
        super().setUp()
        self.bridge = FauxBridge()
        self.app.extensions["telegram"] = self.bridge
        self.beat = heartbeat.Heartbeat(self.app)
        task_model.create_task({"title": "Une tache", "estimate_min": 60})

    @staticmethod
    def _at(heure, minute=0):
        """Retourne l'instant du jour demande."""
        return datetime.combine(date.today(), datetime.min.time()).replace(
            hour=heure, minute=minute
        )

    def test_avant_l_heure_rien_ne_part(self):
        self.beat._check(self._at(7))
        self.assertEqual(self.bridge.messages, [])

    def test_le_point_du_matin_part_une_seule_fois(self):
        self.beat._check(self._at(9))
        self.beat._check(self._at(10))
        self.assertEqual(len(self.bridge.messages), 1)

    def test_le_rendez_vous_rafraichit_l_epingle(self):
        self.beat._check(self._at(9))
        self.assertEqual(self.bridge.epingles, 1)

    def test_un_demarrage_tardif_ne_rejoue_pas_le_matin(self):
        self.beat._check(self._at(23))
        self.assertEqual(self.bridge.messages, [])
        self.assertEqual(
            setting.get("heartbeat_last_morning"), date.today().isoformat()
        )

    def test_reglage_d_heure_illisible_est_ignore(self):
        setting.set_value("morning_brief_at", "pas une heure")
        self.beat._check(self._at(9))
        self.assertEqual(self.bridge.messages, [])

    def test_minutes_de_retard(self):
        self.assertEqual(self.beat._minutes_late(self._at(9, 0), "08:30"), 30)
        self.assertEqual(self.beat._minutes_late(self._at(8, 0), "08:30"), -30)
        self.assertIsNone(self.beat._minutes_late(self._at(8, 0), ""))

    def test_le_thread_demarre_et_s_arrete(self):
        beat = heartbeat.Heartbeat(self.app, interval=0.05)
        beat.start()
        self.assertTrue(beat.is_running())
        beat.stop()


if __name__ == "__main__":
    unittest.main()
