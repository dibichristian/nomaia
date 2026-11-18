"""Tests du moteur de priorisation : le score doit rester explicable."""
import unittest
from datetime import date, timedelta

from services import scheduler

TODAY = date(2026, 9, 16)


def task(**champs):
    """Construit une tache minimale, surchargeable champ par champ."""
    base = {
        "id": 1,
        "priority": 3,
        "estimate_min": 60,
        "deadline": None,
        "energy": "med",
        "needs_review": 0,
        "capture_id": None,
    }
    base.update(champs)
    return base


class UrgencyTest(unittest.TestCase):
    """L'urgence doit croitre a mesure que l'echeance approche puis passe."""

    def test_urgence_sans_echeance_est_minimale(self):
        self.assertEqual(scheduler.urgency(task(), TODAY), scheduler.URGENCY_NONE)

    def test_urgence_du_jour_est_superieure_a_celle_de_demain(self):
        aujourdhui = scheduler.urgency(task(deadline=TODAY.isoformat()), TODAY)
        demain = scheduler.urgency(task(deadline=(TODAY + timedelta(days=1)).isoformat()), TODAY)
        self.assertGreater(aujourdhui, demain)

    def test_urgence_croit_avec_le_retard(self):
        un_jour = scheduler.urgency(task(deadline=(TODAY - timedelta(days=1)).isoformat()), TODAY)
        dix_jours = scheduler.urgency(task(deadline=(TODAY - timedelta(days=10)).isoformat()), TODAY)
        self.assertGreater(dix_jours, un_jour)

    def test_urgence_de_retard_est_plafonnee(self):
        enorme = scheduler.urgency(task(deadline="2020-01-01"), TODAY)
        self.assertEqual(enorme, scheduler.URGENCY_LATE_MAX)

    def test_echeance_illisible_ne_casse_pas_le_calcul(self):
        self.assertEqual(scheduler.urgency(task(deadline="pas une date"), TODAY),
                         scheduler.URGENCY_NONE)


class ScoreTest(unittest.TestCase):
    """Le score compose priorite, urgence, effet tache courte et relecture."""

    def test_priorite_haute_passe_devant(self):
        self.assertGreater(
            scheduler.score(task(priority=1), TODAY),
            scheduler.score(task(priority=4), TODAY),
        )

    def test_tache_courte_recoit_un_bonus(self):
        courte = scheduler.score(task(estimate_min=scheduler.QUICK_WIN_MAX_MIN), TODAY)
        longue = scheduler.score(task(estimate_min=scheduler.QUICK_WIN_MAX_MIN + 1), TODAY)
        self.assertEqual(courte - longue, scheduler.QUICK_WIN_BONUS)

    def test_tache_a_verifier_est_penalisee(self):
        relue = scheduler.score(task(needs_review=0), TODAY)
        non_relue = scheduler.score(task(needs_review=1), TODAY)
        self.assertEqual(relue - non_relue, scheduler.REVIEW_PENALTY)


class RankTest(unittest.TestCase):
    """Le classement depend de la fenetre : le matin favorise l'effort."""

    def test_le_matin_remonte_les_taches_exigeantes(self):
        exigeante = task(id=1, energy="high")
        legere = task(id=2, energy="low")
        ordre = [item["id"] for item in scheduler.rank([legere, exigeante], "morning", TODAY)]
        self.assertEqual(ordre, [1, 2])

    def test_l_apres_midi_remonte_les_taches_legeres(self):
        exigeante = task(id=1, energy="high")
        legere = task(id=2, energy="low")
        ordre = [item["id"] for item in scheduler.rank([exigeante, legere], "afternoon", TODAY)]
        self.assertEqual(ordre, [2, 1])

    def test_classement_stable_a_score_egal(self):
        # A score identique, la plus ancienne passe : l'ordre ne doit pas
        # changer d'un affichage a l'autre.
        taches = [task(id=3), task(id=1), task(id=2)]
        ordre = [item["id"] for item in scheduler.rank(taches, None, TODAY)]
        self.assertEqual(ordre, [1, 2, 3])


class ExplainTest(unittest.TestCase):
    """Le detail affiche a l'utilisateur doit correspondre au score reel."""

    def test_le_total_explique_egale_le_score(self):
        sujet = task(priority=1, estimate_min=20, deadline=TODAY.isoformat(), needs_review=1)
        detail = scheduler.explain(sujet, TODAY)
        self.assertEqual(detail["total"], scheduler.score(sujet, TODAY))
        self.assertEqual(
            detail["priorite"] + detail["urgence"] + detail["tache_courte"] + detail["a_verifier"],
            detail["total"],
        )


if __name__ == "__main__":
    unittest.main()
