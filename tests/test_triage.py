"""Tests du triage : validation de la sortie du modele et rattachement projet."""
import unittest

from models import project as project_model
from models import setting
from models import task as task_model
from services import triage
from services.llm import LLMError
from tests.base import NomaiaTestCase


class NormalizePayloadTest(unittest.TestCase):
    """La sortie du modele est une donnee hostile jusqu'a preuve du contraire."""

    def test_refuse_ce_qui_n_est_pas_un_objet(self):
        with self.assertRaises(LLMError):
            triage.normalize_payload(["pas", "un", "objet"])

    def test_complete_les_champs_manquants(self):
        resultat = triage.normalize_payload({})
        self.assertEqual(resultat["summary"], "Analyse sans resume.")
        self.assertEqual(resultat["confidence"], 0.0)
        self.assertEqual(resultat["tasks"], [])
        self.assertEqual(resultat["questions"], [])

    def test_confiance_est_bornee(self):
        self.assertEqual(triage.normalize_payload({"confidence": 5})["confidence"], 1.0)
        self.assertEqual(triage.normalize_payload({"confidence": -3})["confidence"], 0.0)
        self.assertEqual(triage.normalize_payload({"confidence": "nawak"})["confidence"], 0.0)

    def test_tache_sans_titre_est_ecartee(self):
        payload = {"tasks": [{"title": "  "}, {"title": "Vraie tache"}]}
        taches = triage.normalize_payload(payload)["tasks"]
        self.assertEqual([item["title"] for item in taches], ["Vraie tache"])

    def test_priorite_et_duree_sont_bornees(self):
        payload = {"tasks": [{"title": "T", "priority": 99, "estimate_min": 99999}]}
        tache = triage.normalize_payload(payload)["tasks"][0]
        self.assertEqual(tache["priority"], 4)
        self.assertEqual(tache["estimate_min"], 1440)

    def test_energie_inconnue_retombe_sur_moyenne(self):
        payload = {"tasks": [{"title": "T", "energy": "surhumaine"}]}
        self.assertEqual(triage.normalize_payload(payload)["tasks"][0]["energy"], "med")

    def test_trois_questions_au_maximum(self):
        payload = {"questions": [{"text": f"q{index}"} for index in range(10)]}
        self.assertEqual(len(triage.normalize_payload(payload)["questions"]), 3)

    def test_quatre_suggestions_au_maximum(self):
        payload = {"questions": [{"text": "q", "suggestions": list("abcdefgh")}]}
        question = triage.normalize_payload(payload)["questions"][0]
        self.assertEqual(len(question["suggestions"]), 4)


class ResolveProjectTest(NomaiaTestCase):
    """Le rattachement doit etre franc : exact d'abord, jamais approximatif."""

    def setUp(self):
        super().setUp()
        self.alpha = project_model.create_project("ALPHA")
        self.alpha_paie = project_model.create_project("ALPHA paie")

    def test_egalite_exacte_prime_sur_inclusion(self):
        self.assertEqual(triage._resolve_project("alpha"), self.alpha["id"])

    def test_inclusion_acceptee_sur_un_nom_discriminant(self):
        self.assertEqual(triage._resolve_project("module ALPHA paie"), self.alpha_paie["id"])

    def test_indice_trop_court_est_ignore(self):
        project_model.create_project("V2")
        self.assertIsNone(triage._resolve_project("V2"))

    def test_sans_creation_un_projet_inconnu_reste_sans_suite(self):
        self.assertIsNone(triage._resolve_project("BRIGHTJET"))

    def test_creation_a_la_demande(self):
        identifiant = triage._resolve_project("BRIGHTJET", create=True)
        self.assertIsNotNone(identifiant)
        self.assertEqual(project_model.get_project(identifiant)["name"], "BRIGHTJET")

    def test_creation_desactivee_par_reglage(self):
        setting.set_value("auto_create_project", "0")
        self.assertIsNone(triage._resolve_project("BRIGHTJET", create=True))

    def test_indice_vide_ne_cree_rien(self):
        self.assertIsNone(triage._resolve_project(None, create=True))
        self.assertIsNone(triage._resolve_project("", create=True))


class ApplyProposalTest(NomaiaTestCase):
    """L'application d'une proposition cree les taches et pose les liens."""

    def test_les_doublons_ne_sont_pas_recrees(self):
        from models import capture as capture_model

        capture = capture_model.create_capture(raw_text="bug paie")
        payload = triage.normalize_payload(
            {
                "confidence": 0.9,
                "tasks": [
                    {"title": "Corriger le bug paie ALPHA"},
                    {"title": "Deja fait ailleurs", "duplicate_of": 42},
                ],
            }
        )
        proposal = capture_model.create_proposal(capture["id"], payload)

        creees = triage.apply_proposal(proposal)
        self.assertEqual([item["title"] for item in creees], ["Corriger le bug paie ALPHA"])
        self.assertEqual(creees[0]["capture_id"], capture["id"])

    def test_le_projet_est_deduit_et_cree(self):
        from models import capture as capture_model

        capture = capture_model.create_capture(raw_text="probleme chez brightjet")
        payload = triage.normalize_payload(
            {"tasks": [{"title": "Former la comptable", "project_hint": "BRIGHTJET"}]}
        )
        proposal = capture_model.create_proposal(capture["id"], payload)

        tache = triage.apply_proposal(proposal)[0]
        self.assertEqual(tache["project_name"], "BRIGHTJET")

    def test_une_tache_relue_plus_tard_est_marquee(self):
        from models import capture as capture_model

        capture = capture_model.create_capture(raw_text="note")
        payload = triage.normalize_payload({"tasks": [{"title": "A verifier"}]})
        proposal = capture_model.create_proposal(capture["id"], payload)

        tache = triage.apply_proposal(proposal, needs_review=True)[0]
        self.assertEqual(tache["needs_review"], 1)


class CalibrationLineTest(NomaiaTestCase):
    """Le bloc de calibrage ne parle que lorsqu'il a de quoi dire."""

    def test_silencieux_sans_historique(self):
        self.assertIsNone(triage._calibration_line())

    def test_silencieux_quand_l_ecart_est_negligeable(self):
        for index in range(5):
            tache = task_model.create_task({"title": f"T{index}", "estimate_min": 60})
            self.complete_task(tache["id"], 61)
        self.assertIsNone(triage._calibration_line())

    def test_annonce_le_facteur_quand_l_ecart_est_net(self):
        for index in range(5):
            tache = task_model.create_task({"title": f"T{index}", "estimate_min": 60})
            self.complete_task(tache["id"], 120)

        ligne = triage._calibration_line()
        self.assertIn("CALIBRAGE", ligne)
        self.assertIn("x2.0", ligne)

    def test_reglage_a_zero_coupe_le_bloc(self):
        for index in range(5):
            tache = task_model.create_task({"title": f"T{index}", "estimate_min": 60})
            self.complete_task(tache["id"], 120)

        setting.set_value("estimate_calibration", "0")
        self.assertIsNone(triage._calibration_line())


if __name__ == "__main__":
    unittest.main()
