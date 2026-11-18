"""Tests du socle : validation des taches, file de triage, sauvegarde, calibrage."""
import sqlite3
import unittest

import config
from models import backup_db, get_db, setting, stats, vacuum
from models import capture as capture_model
from models import task as task_model
from models.task import ValidationError
from tests.base import NomaiaTestCase


class DatabaseTest(NomaiaTestCase):
    """Reglages d'acces concurrent et sauvegarde sans secret."""

    def test_journal_en_mode_wal(self):
        self.assertEqual(get_db().execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_attente_sur_verrou_configuree(self):
        from models import BUSY_TIMEOUT_MS

        self.assertEqual(get_db().execute("PRAGMA busy_timeout").fetchone()[0], BUSY_TIMEOUT_MS)

    def test_la_sauvegarde_ne_transporte_aucun_secret(self):
        setting.set_secret("telegram_token", "jeton-secret-123")
        task_model.create_task({"title": "Une tache a retrouver"})

        copie = backup_db()
        conn = sqlite3.connect(copie)
        try:
            jeton = conn.execute(
                "SELECT value FROM setting WHERE key = 'telegram_token'"
            ).fetchone()[0]
            taches = conn.execute("SELECT COUNT(*) FROM task").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(jeton, "")
        self.assertEqual(taches, 1)
        self.assertFalse((copie.parent / (copie.name + "-wal")).exists())

    def test_rotation_des_sauvegardes(self):
        for _ in range(config.BACKUP_KEEP + 3):
            backup_db()
        conservees = list(config.BACKUP_DIR.glob("nomaia_*.db"))
        self.assertLessEqual(len(conservees), config.BACKUP_KEEP)

    def test_vacuum_ne_leve_pas(self):
        vacuum()


class TaskValidationTest(NomaiaTestCase):
    """La validation protege la base des donnees mal formees."""

    def test_titre_obligatoire(self):
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "   "})

    def test_echeance_mal_formee_refusee(self):
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "T", "deadline": "16/09/2026"})

    def test_duree_hors_bornes_refusee(self):
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "T", "estimate_min": 5000})

    def test_energie_inconnue_refusee(self):
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "T", "energy": "surhumaine"})

    def test_bascule_pose_et_retire_la_date_de_fin(self):
        tache = task_model.create_task({"title": "T"})

        termine = task_model.toggle_task(tache["id"])
        self.assertEqual(termine["status"], "done")
        self.assertIsNotNone(termine["done_at"])

        rouvert = task_model.toggle_task(tache["id"])
        self.assertEqual(rouvert["status"], "todo")
        self.assertIsNone(rouvert["done_at"])

    def test_classement_par_panier(self):
        from datetime import date, timedelta

        today = date.today()
        en_retard = task_model.create_task(
            {"title": "Retard", "deadline": (today - timedelta(days=1)).isoformat()}
        )
        sans_echeance = task_model.create_task({"title": "Un jour"})

        self.assertEqual(task_model.get_task(en_retard["id"])["bucket"], "late")
        self.assertEqual(task_model.get_task(sans_echeance["id"])["bucket"], "someday")


class CaptureQueueTest(NomaiaTestCase):
    """File de triage : reservation, tentatives, dialogue."""

    def test_capture_vide_refusee(self):
        with self.assertRaises(ValueError):
            capture_model.create_capture()

    def test_reservation_incremente_les_tentatives(self):
        capture = capture_model.create_capture(raw_text="quelque chose")

        reservee = capture_model.claim_next_capture()
        self.assertEqual(reservee["id"], capture["id"])
        self.assertEqual(reservee["attempts"], 1)
        self.assertEqual(reservee["status"], "processing")

    def test_la_file_vide_ne_rend_rien(self):
        self.assertIsNone(capture_model.claim_next_capture())

    def test_une_reponse_humaine_remet_le_budget_a_zero(self):
        # Sans cette remise a zero, quatre questions de NOMAIA suffisaient a
        # condamner une capture parfaitement saine.
        capture = capture_model.create_capture(raw_text="bug")
        for _ in range(3):
            capture_model.claim_next_capture()
            capture_model.requeue(capture["id"])

        self.assertEqual(capture_model.get_capture(capture["id"])["attempts"], 3)

        capture_model.requeue(capture["id"], reset_attempts=True)
        self.assertEqual(capture_model.get_capture(capture["id"])["attempts"], 0)

    def test_une_panne_moteur_conserve_le_compteur(self):
        capture = capture_model.create_capture(raw_text="bug")
        capture_model.claim_next_capture()
        capture_model.requeue(capture["id"])

        self.assertEqual(capture_model.get_capture(capture["id"])["attempts"], 1)

    def test_les_captures_bloquees_repartent_ou_echouent(self):
        vivante = capture_model.create_capture(raw_text="a")
        epuisee = capture_model.create_capture(raw_text="b")

        db = get_db()
        db.execute("UPDATE capture SET status = 'processing', attempts = 1 WHERE id = ?",
                   (vivante["id"],))
        db.execute("UPDATE capture SET status = 'processing', attempts = ? WHERE id = ?",
                   (capture_model.MAX_ATTEMPTS, epuisee["id"]))
        db.commit()

        capture_model.reset_stuck_captures()
        self.assertEqual(capture_model.get_capture(vivante["id"])["status"], "new")
        self.assertEqual(capture_model.get_capture(epuisee["id"])["status"], "failed")

    def test_role_de_message_invalide_refuse(self):
        capture = capture_model.create_capture(raw_text="a")
        with self.assertRaises(ValueError):
            capture_model.add_message(capture["id"], "pirate", "coucou")


class CalibrationTest(NomaiaTestCase):
    """Mesure de la derive entre duree estimee et duree chronometree."""

    def _serie(self, combien, estime, reel, project_id=None):
        for index in range(combien):
            tache = task_model.create_task(
                {"title": f"T{index}-{project_id}", "estimate_min": estime,
                 "project_id": project_id}
            )
            self.complete_task(tache["id"], reel)

    def test_echantillon_insuffisant_ne_conclut_pas(self):
        self._serie(stats.CALIBRATION_MIN_TASKS - 1, 60, 120)
        self.assertIsNone(stats.estimation_calibration()["global"])

    def test_facteur_global(self):
        self._serie(stats.CALIBRATION_MIN_TASKS, 60, 90)
        self.assertEqual(stats.estimation_calibration()["global"]["ratio"], 1.5)

    def test_une_tache_aberrante_est_ecartee_sans_tout_annuler(self):
        # Un chronometre laisse tourner toute la nuit ne doit pas priver le
        # triage de calibrage : la tache sort, la mesure reste.
        self._serie(stats.CALIBRATION_MIN_TASKS, 60, 90)
        aberrante = task_model.create_task({"title": "Chrono oublie", "estimate_min": 10})
        self.complete_task(aberrante["id"], 5000)

        self.assertEqual(stats.estimation_calibration()["global"]["ratio"], 1.5)

    def test_une_tache_sans_chronometre_ne_compte_pas(self):
        self._serie(stats.CALIBRATION_MIN_TASKS, 60, 90)
        ignoree = task_model.create_task({"title": "Jamais chronometree", "estimate_min": 600})
        task_model.toggle_task(ignoree["id"])

        self.assertEqual(stats.estimation_calibration()["global"]["tasks"],
                         stats.CALIBRATION_MIN_TASKS)


if __name__ == "__main__":
    unittest.main()
