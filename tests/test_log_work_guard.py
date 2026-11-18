"""Temps declare par une session Claude : avertissement (non bloquant) au-dela du temps ecoule."""
from datetime import datetime, timedelta

from models import get_db
from models import task as task_model
from services import agent
from tests.base import NomaiaTestCase


class GardeFouTempsTest(NomaiaTestCase):
    def _tache_creee_il_y_a(self, minutes):
        tache = task_model.create_task({"title": "Corriger la paie"})
        moment = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        get_db().execute("UPDATE task SET created_at = ? WHERE id = ?", (moment, tache["id"]))
        get_db().commit()
        return tache

    def test_duree_plausible_sans_avertissement(self):
        tache = self._tache_creee_il_y_a(30)
        resultat = agent.log_work(tache["id"], 45, "Correctif écrit.")  # 30 x 1,2 + 15 = 51 : dans la marge
        self.assertNotIn("warning", resultat)

    def test_duree_gonflee_enregistree_mais_signalee(self):
        tache = self._tache_creee_il_y_a(12)
        resultat = agent.log_work(tache["id"], 60, "Correctif écrit.")
        self.assertIn("warning", resultat)
        self.assertIn("~12 min", resultat["warning"])
        # Non bloquant : la duree est bien enregistree.
        self.assertEqual(resultat["tracked_min"], 60)

    def test_reference_a_la_derniere_declaration(self):
        tache = self._tache_creee_il_y_a(300)
        agent.log_work(tache["id"], 30, "Première partie.")
        # La declaration precedente vient de finir : 40 min de plus depassent la marge.
        self.assertIn("warning", agent.log_work(tache["id"], 40, "Deuxième partie."))
        self.assertIn("derniere declaration", agent.log_work(tache["id"], 40, "Encore.")["warning"])

    def test_marge_minimale_pour_une_tache_toute_neuve(self):
        tache = task_model.create_task({"title": "Nouvelle"})
        self.assertNotIn("warning", agent.log_work(tache["id"], 15, "Fait vite."))
