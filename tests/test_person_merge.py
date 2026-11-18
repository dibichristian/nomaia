"""Tests de la fusion d'interlocuteurs et des alias.

Le cas reel : une capture cite « Paul M. », une autre « Paul Martin », et
le carnet se retrouve avec deux fiches. La fusion doit tout regrouper sans
rien perdre, et l'ancien nom doit rester reconnu, sinon la capture suivante
recree le doublon.
"""
from models import person as person_model
from models import project as project_model
from models import task as task_model
from models.person import PersonError
from services import tools
from tests.base import NomaiaTestCase


class FusionTest(NomaiaTestCase):
    """Regroupement de deux fiches en une."""

    def setUp(self):
        super().setUp()
        self.garde = person_model.create_person({"name": "Paul Martin", "notes": "DSI"})
        self.doublon = person_model.create_person(
            {"name": "Paul M.", "email": "paul@alpha.example", "notes": "Prefere WhatsApp"}
        )

    def test_taches_et_attentes_suivent_la_fiche_gardee(self):
        tache = task_model.create_task({"title": "Le devis", "person_id": self.doublon["id"]})
        person_model.create_waiting({"person_id": self.doublon["id"], "subject": "Les specs"})

        person_model.merge_people(self.garde["id"], self.doublon["id"])

        self.assertEqual(task_model.get_task(tache["id"])["person_id"], self.garde["id"])
        attentes = person_model.list_waiting(person_id=self.garde["id"])
        self.assertEqual([a["subject"] for a in attentes], ["Les specs"])

    def test_le_doublon_disparait(self):
        person_model.merge_people(self.garde["id"], self.doublon["id"])
        self.assertIsNone(person_model.get_person(self.doublon["id"]))
        self.assertEqual(len(person_model.list_people()), 1)

    def test_champs_vides_completes_et_notes_cumulees(self):
        gardee = person_model.merge_people(self.garde["id"], self.doublon["id"])

        self.assertEqual(gardee["email"], "paul@alpha.example")
        self.assertIn("DSI", gardee["notes"])
        self.assertIn("WhatsApp", gardee["notes"])

    def test_un_champ_rempli_n_est_pas_ecrase(self):
        person_model.update_person(self.garde["id"], {"email": "kk@alpha.example"})
        gardee = person_model.merge_people(self.garde["id"], self.doublon["id"])
        self.assertEqual(gardee["email"], "kk@alpha.example")

    def test_projet_repris_si_absent(self):
        projet = project_model.create_project("ALPHA")
        person_model.update_person(self.doublon["id"], {"project_id": projet["id"]})
        gardee = person_model.merge_people(self.garde["id"], self.doublon["id"])
        self.assertEqual(gardee["project_name"], "ALPHA")

    def test_ancien_nom_devient_alias_et_reste_reconnu(self):
        person_model.merge_people(self.garde["id"], self.doublon["id"])

        trouve = person_model.find_by_name("paul m.")
        self.assertEqual(trouve["id"], self.garde["id"])
        self.assertIn("Paul M.", trouve["aliases"])

    def test_le_triage_ne_recree_pas_le_doublon(self):
        person_model.merge_people(self.garde["id"], self.doublon["id"])
        personne = person_model.get_or_create("Paul M.")
        self.assertEqual(personne["id"], self.garde["id"])
        self.assertEqual(len(person_model.list_people()), 1)

    def test_alias_du_doublon_transmis(self):
        person_model.update_person(self.doublon["id"], {"aliases": "M. Martin"})
        person_model.merge_people(self.garde["id"], self.doublon["id"])
        self.assertEqual(person_model.find_by_name("M. Martin")["id"], self.garde["id"])

    def test_fusion_avec_soi_meme_refusee(self):
        with self.assertRaises(PersonError):
            person_model.merge_people(self.garde["id"], self.garde["id"])

    def test_fiche_introuvable_refusee(self):
        with self.assertRaises(PersonError):
            person_model.merge_people(self.garde["id"], 999)


class AliasTest(NomaiaTestCase):
    """Alias saisis et alias crees par un renommage."""

    def test_renommage_garde_l_ancien_nom(self):
        personne = person_model.create_person({"name": "Lucie"})
        person_model.update_person(personne["id"], {"name": "Lucie Traore"})

        self.assertEqual(person_model.find_by_name("Lucie")["name"], "Lucie Traore")

    def test_changement_de_casse_ne_cree_pas_d_alias(self):
        personne = person_model.create_person({"name": "lucie"})
        modifiee = person_model.update_person(personne["id"], {"name": "Lucie"})
        self.assertEqual(modifiee["aliases"], [])

    def test_alias_saisis_nettoyes(self):
        personne = person_model.create_person({"name": "Lucie"})
        modifiee = person_model.update_person(
            personne["id"], {"aliases": " Mme Traore, lucie ,  , Mme traore"}
        )
        # Vides, doublons et nom propre retires.
        self.assertEqual(modifiee["aliases"], ["Mme Traore"])

    def test_nom_exact_prioritaire_sur_un_alias(self):
        lucie = person_model.create_person({"name": "Lucie"})
        autre = person_model.create_person({"name": "Lucie Moreau"})
        person_model.update_person(autre["id"], {"aliases": "Lucie"})
        self.assertEqual(person_model.find_by_name("Lucie")["id"], lucie["id"])


class FusionApiTest(NomaiaTestCase):
    """Route HTTP et outil du chat."""

    def setUp(self):
        super().setUp()
        self.garde = person_model.create_person({"name": "Paul Martin"})
        self.doublon = person_model.create_person({"name": "Paul M."})

    def test_route_de_fusion(self):
        reponse = self.client().post(
            f"/api/people/{self.garde['id']}/merge",
            json={"merge_id": self.doublon["id"]},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["person"]["aliases"], ["Paul M."])

    def test_route_sans_doublon_rend_400(self):
        reponse = self.client().post(
            f"/api/people/{self.garde['id']}/merge", json={}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_route_alias_via_modification(self):
        reponse = self.client().patch(
            f"/api/people/{self.garde['id']}",
            json={"aliases": "KK"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["person"]["aliases"], ["KK"])

    def test_outil_du_chat(self):
        resultat = tools.execute(
            "merge_people", {"keep_id": self.garde["id"], "merge_id": self.doublon["id"]}
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["absorbed"], "Paul M.")
        self.assertEqual(len(person_model.list_people()), 1)
