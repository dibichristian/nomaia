"""Tests des interlocuteurs et des attentes.

Le point sensible du lot est la direction : ce qu'on me doit n'est pas une
tache, ce que je dois en est une. Les confondre vide la fonction de son sens.
"""
import unittest
from datetime import date, datetime, timedelta

from models import person as person_model
from models import project as project_model
from models import setting
from models import task as task_model
from models.person import PersonError
from services import heartbeat as heartbeat_service
from services import triage
from tests.base import NomaiaTestCase


def il_y_a(jours):
    """Retourne la date d'il y a N jours, au format de stockage."""
    return (date.today() - timedelta(days=jours)).isoformat()


class PersonCrudTest(NomaiaTestCase):
    """Cycle de vie d'un interlocuteur."""

    def test_creation_nominale(self):
        personne = person_model.create_person({"name": "Paul", "role": "Chef de projet"})
        self.assertEqual(personne["name"], "Paul")
        self.assertEqual(personne["waiting_count"], 0)

    def test_nom_obligatoire(self):
        with self.assertRaises(PersonError):
            person_model.create_person({"name": "   "})

    def test_rattachement_a_un_projet(self):
        projet = project_model.create_project("ORION")
        personne = person_model.create_person({"name": "Paul", "project_id": projet["id"]})
        self.assertEqual(personne["project_name"], "ORION")

    def test_recherche_par_nom_insensible_a_la_casse(self):
        person_model.create_person({"name": "Paul"})
        self.assertIsNotNone(person_model.find_by_name("paul"))
        self.assertIsNotNone(person_model.find_by_name("  PAUL  "))

    def test_recherche_prefere_le_nom_le_plus_court(self):
        person_model.create_person({"name": "Paul Martin"})
        person_model.create_person({"name": "Paul"})
        self.assertEqual(person_model.find_by_name("Paul")["name"], "Paul")

    def test_get_or_create_ne_duplique_pas(self):
        premier = person_model.get_or_create("Paul")
        second = person_model.get_or_create("paul")
        self.assertEqual(premier["id"], second["id"])
        self.assertEqual(len(person_model.list_people()), 1)

    def test_mise_a_jour(self):
        personne = person_model.create_person({"name": "Paul"})
        modifie = person_model.update_person(personne["id"], {"role": "Directeur"})
        self.assertEqual(modifie["role"], "Directeur")

    def test_mise_a_jour_refuse_un_nom_vide(self):
        personne = person_model.create_person({"name": "Paul"})
        with self.assertRaises(PersonError):
            person_model.update_person(personne["id"], {"name": ""})

    def test_mise_a_jour_introuvable(self):
        self.assertIsNone(person_model.update_person(999, {"role": "X"}))

    def test_suppression_emporte_les_attentes_mais_detache_les_taches(self):
        personne = person_model.create_person({"name": "Paul"})
        tache = task_model.create_task({"title": "Le devis", "person_id": personne["id"]})
        person_model.create_waiting({"person_id": personne["id"], "subject": "Les specs"})

        person_model.delete_person(personne["id"])

        self.assertEqual(person_model.list_waiting(status=None), [])
        # La tache survit : c'est du travail fait, pas une donnee de contact.
        survivante = task_model.get_task(tache["id"])
        self.assertIsNotNone(survivante)
        self.assertIsNone(survivante["person_id"])


class DirectionTest(NomaiaTestCase):
    """Les deux directions ne se modelisent pas pareil."""

    def test_ce_que_je_dois_est_une_tache(self):
        personne = person_model.create_person({"name": "Paul"})
        task_model.create_task({"title": "Le devis ORION", "person_id": personne["id"]})

        dues = person_model.owed_tasks(personne["id"])
        self.assertEqual(len(dues), 1)
        self.assertEqual(dues[0]["title"], "Le devis ORION")

    def test_ce_qu_on_me_doit_n_est_pas_une_tache(self):
        personne = person_model.create_person({"name": "Paul"})
        person_model.create_waiting({"person_id": personne["id"], "subject": "Les specs"})

        # L'attente ne doit apparaitre nulle part dans les taches.
        self.assertEqual(task_model.list_tasks(), [])
        self.assertEqual(len(person_model.list_waiting()), 1)

    def test_les_deux_compteurs_sont_distincts(self):
        personne = person_model.create_person({"name": "Paul"})
        task_model.create_task({"title": "Le devis", "person_id": personne["id"]})
        person_model.create_waiting({"person_id": personne["id"], "subject": "Les specs"})
        person_model.create_waiting({"person_id": personne["id"], "subject": "L'acces"})

        ligne = person_model.list_people()[0]
        self.assertEqual(ligne["owed_count"], 1)
        self.assertEqual(ligne["waiting_count"], 2)

    def test_une_tache_terminee_ne_compte_plus_comme_due(self):
        personne = person_model.create_person({"name": "Paul"})
        tache = task_model.create_task({"title": "Le devis", "person_id": personne["id"]})
        task_model.toggle_task(tache["id"])

        self.assertEqual(person_model.list_people()[0]["owed_count"], 0)

    def test_le_nom_du_destinataire_suit_la_tache(self):
        personne = person_model.create_person({"name": "Paul"})
        tache = task_model.create_task({"title": "Le devis", "person_id": personne["id"]})
        self.assertEqual(task_model.get_task(tache["id"])["person_name"], "Paul")
        self.assertEqual(task_model.list_tasks()[0]["person_name"], "Paul")


class WaitingTest(NomaiaTestCase):
    """Attentes : validation, anciennete, retard, statuts."""

    def setUp(self):
        super().setUp()
        self.personne = person_model.create_person({"name": "Paul"})

    def _attente(self, **extra):
        donnees = {"person_id": self.personne["id"], "subject": "Les specs"}
        donnees.update(extra)
        return person_model.create_waiting(donnees)

    def test_creation_nominale(self):
        attente = self._attente()
        self.assertEqual(attente["status"], "open")
        self.assertEqual(attente["since"], date.today().isoformat())
        self.assertEqual(attente["days_open"], 0)

    def test_interlocuteur_obligatoire(self):
        with self.assertRaises(PersonError):
            person_model.create_waiting({"subject": "Les specs"})

    def test_interlocuteur_inexistant_refuse(self):
        with self.assertRaises(PersonError):
            person_model.create_waiting({"person_id": 999, "subject": "Les specs"})

    def test_sujet_obligatoire(self):
        with self.assertRaises(PersonError):
            self._attente(subject="   ")

    def test_anciennete_calculee(self):
        self.assertEqual(self._attente(since=il_y_a(8))["days_open"], 8)

    def test_retard_signale(self):
        self.assertTrue(self._attente(since=il_y_a(10), due=il_y_a(3))["overdue"])

    def test_echeance_future_sans_retard(self):
        demain = (date.today() + timedelta(days=1)).isoformat()
        self.assertFalse(self._attente(due=demain)["overdue"])

    def test_echeance_avant_le_depart_refusee(self):
        # Depart explicite et posterieur a l'echeance : la saisie se contredit.
        with self.assertRaises(PersonError):
            self._attente(since=il_y_a(2), due=il_y_a(5))

    def test_saisie_apres_coup_recule_le_depart(self):
        # « Il devait me l'envoyer vendredi, on est mardi » : le cas le plus
        # courant. L'anciennete ne doit pas repartir de zero.
        attente = self._attente(due=il_y_a(4))
        self.assertEqual(attente["since"], il_y_a(4))
        self.assertEqual(attente["days_open"], 4)
        self.assertTrue(attente["overdue"])

    def test_date_mal_formee_refusee(self):
        with self.assertRaises(PersonError):
            self._attente(due="la semaine prochaine")

    def test_tache_bloquee_rappelee(self):
        tache = task_model.create_task({"title": "Corriger l'export"})
        attente = self._attente(task_id=tache["id"])
        self.assertEqual(attente["task_title"], "Corriger l'export")

    def test_marquee_recue_sort_des_ouvertes(self):
        attente = self._attente()
        person_model.update_waiting(attente["id"], {"status": "received"})
        self.assertEqual(person_model.list_waiting(status="open"), [])
        self.assertEqual(len(person_model.list_waiting(status="received")), 1)

    def test_statut_inconnu_refuse(self):
        attente = self._attente()
        with self.assertRaises(PersonError):
            person_model.update_waiting(attente["id"], {"status": "peut-etre"})

    def test_filtre_par_statut_inconnu_refuse(self):
        with self.assertRaises(PersonError):
            person_model.list_waiting(status="peut-etre")

    def test_la_plus_ancienne_en_premier(self):
        self._attente(subject="Recente")
        self._attente(subject="Ancienne", since=il_y_a(20))
        self.assertEqual(person_model.list_waiting()[0]["subject"], "Ancienne")

    def test_suppression(self):
        attente = self._attente()
        self.assertTrue(person_model.delete_waiting(attente["id"]))
        self.assertEqual(person_model.list_waiting(), [])


class NudgeTest(NomaiaTestCase):
    """Relances : ni trop tot, ni tous les matins."""

    def setUp(self):
        super().setUp()
        self.personne = person_model.create_person({"name": "Paul"})
        setting.set_value("nudge_after_days", "3")
        setting.set_value("nudge_quiet_days", "3")

    def _attente(self, **extra):
        donnees = {"person_id": self.personne["id"], "subject": "Les specs"}
        donnees.update(extra)
        return person_model.create_waiting(donnees)

    def test_attente_trop_recente_ignoree(self):
        self._attente(since=il_y_a(1))
        self.assertEqual(person_model.stale_waiting(3, 3), [])

    def test_attente_ancienne_signalee(self):
        self._attente(since=il_y_a(8))
        self.assertEqual(len(person_model.stale_waiting(3, 3)), 1)

    def test_attente_en_retard_signalee_meme_si_recente(self):
        # L'echeance est passee : l'anciennete n'a plus d'importance.
        self._attente(since=il_y_a(1), due=il_y_a(1))
        self.assertEqual(len(person_model.stale_waiting(3, 3)), 1)

    def test_attente_deja_signalee_se_tait(self):
        attente = self._attente(since=il_y_a(8))
        person_model.mark_nudged([attente["id"]])
        self.assertEqual(person_model.stale_waiting(3, 3), [])

    def test_attente_signalee_revient_apres_le_silence(self):
        attente = self._attente(since=il_y_a(20))
        person_model.mark_nudged([attente["id"]], moment=datetime.now() - timedelta(days=5))
        self.assertEqual(len(person_model.stale_waiting(3, 3)), 1)

    def test_attente_recue_ne_se_signale_plus(self):
        attente = self._attente(since=il_y_a(8))
        person_model.update_waiting(attente["id"], {"status": "received"})
        self.assertEqual(person_model.stale_waiting(3, 3), [])

    def test_la_plus_ancienne_en_tete(self):
        self._attente(subject="Huit jours", since=il_y_a(8))
        self._attente(subject="Vingt jours", since=il_y_a(20))
        self.assertEqual(person_model.stale_waiting(3, 3)[0]["subject"], "Vingt jours")

    def test_le_point_du_matin_relance(self):
        self._attente(since=il_y_a(8))
        texte = heartbeat_service.morning_brief()
        self.assertIn("Paul", texte)
        self.assertIn("Les specs", texte)

    def test_le_point_du_matin_ne_relance_qu_une_fois(self):
        self._attente(since=il_y_a(8))
        heartbeat_service.morning_brief()
        second = heartbeat_service.morning_brief()
        self.assertNotIn("Les specs", second or "")

    def test_une_journee_vide_avec_une_relance_parle_quand_meme(self):
        self._attente(since=il_y_a(8))
        self.assertIsNotNone(heartbeat_service.morning_brief())

    def test_le_point_du_soir_nomme_qui_attend(self):
        setting.set_value("drift_alert_days", "3")
        task_model.create_task(
            {"title": "Le devis", "deadline": il_y_a(10), "person_id": self.personne["id"]}
        )
        self.assertIn("Paul attend", heartbeat_service.evening_brief())


class TriageExtractionTest(NomaiaTestCase):
    """Extraction par le triage : optionnelle de bout en bout."""

    def test_sortie_sans_les_nouveaux_champs_reste_valide(self):
        # Garde-fou principal : un modele qui ignore l'ajout ne casse rien.
        resultat = triage.normalize_payload({"tasks": [{"title": "Une tache"}]})
        self.assertEqual(resultat["waiting"], [])
        self.assertEqual(len(resultat["tasks"]), 1)

    def test_attente_extraite(self):
        resultat = triage.normalize_payload(
            {"waiting": [{"person": "Paul", "subject": "Les specs", "due": "2026-10-01"}]}
        )
        self.assertEqual(len(resultat["waiting"]), 1)
        self.assertEqual(resultat["waiting"][0]["person"], "Paul")

    def test_attente_incomplete_ecartee(self):
        resultat = triage.normalize_payload(
            {"waiting": [{"person": "Paul"}, {"subject": "Sans personne"}, {"person": " "}]}
        )
        self.assertEqual(resultat["waiting"], [])

    def test_attente_mal_typee_ecartee(self):
        resultat = triage.normalize_payload({"waiting": ["pas un objet", 42]})
        self.assertEqual(resultat["waiting"], [])

    def test_date_illisible_ramenee_a_rien(self):
        resultat = triage.normalize_payload(
            {"waiting": [{"person": "Paul", "subject": "X", "due": "bientot"}]}
        )
        self.assertIsNone(resultat["waiting"][0]["due"])

    def test_nombre_d_attentes_plafonne(self):
        entrees = [{"person": f"P{i}", "subject": f"S{i}"} for i in range(20)]
        resultat = triage.normalize_payload({"waiting": entrees})
        self.assertEqual(len(resultat["waiting"]), triage.MAX_WAITING)

    def _proposition(self, payload):
        """Fabrique une proposition applicable a partir d'une sortie de modele."""
        from models import capture as capture_model

        capture = capture_model.create_capture(raw_text="peu importe")
        return capture_model.create_proposal(capture["id"], triage.normalize_payload(payload))

    def test_application_cree_la_personne_et_l_attente(self):
        proposition = self._proposition(
            {
                "tasks": [{"title": "Le devis", "person_hint": "Paul"}],
                "waiting": [{"person": "Emma", "subject": "Les specs"}],
            }
        )
        taches = triage.apply_proposal(proposition)

        self.assertEqual(taches[0]["person_name"], "Paul")
        attentes = person_model.list_waiting()
        self.assertEqual(len(attentes), 1)
        self.assertEqual(attentes[0]["person_name"], "Emma")

    def test_extraction_coupee_ne_cree_rien(self):
        setting.set_value("extract_people", "0")
        proposition = self._proposition(
            {
                "tasks": [{"title": "Le devis", "person_hint": "Paul"}],
                "waiting": [{"person": "Emma", "subject": "Les specs"}],
            }
        )
        taches = triage.apply_proposal(proposition)

        self.assertIsNone(taches[0]["person_id"])
        self.assertEqual(person_model.list_waiting(), [])
        self.assertEqual(person_model.list_people(), [])

    def test_la_personne_citee_n_est_pas_dupliquee(self):
        person_model.create_person({"name": "Paul"})
        proposition = self._proposition({"tasks": [{"title": "Le devis", "person_hint": "paul"}]})
        triage.apply_proposal(proposition)
        self.assertEqual(len(person_model.list_people()), 1)


class PersonApiTest(NomaiaTestCase):
    """Routes HTTP des interlocuteurs et des attentes."""

    def _personne(self, nom="Paul"):
        return self.client().post(
            "/api/people", json={"name": nom}, headers=self.write_headers()
        )

    def test_liste_vide_au_depart(self):
        reponse = self.client().get("/api/people")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["people"], [])

    def test_creation_et_relecture(self):
        self.assertEqual(self._personne().status_code, 201)
        self.assertEqual(len(self.client().get("/api/people").get_json()["people"]), 1)

    def test_creation_sans_nom_rend_400(self):
        reponse = self.client().post("/api/people", json={}, headers=self.write_headers())
        self.assertEqual(reponse.status_code, 400)

    def test_fiche_complete(self):
        person_id = self._personne().get_json()["person"]["id"]
        task_model.create_task({"title": "Le devis", "person_id": person_id})
        self.client().post(
            "/api/waiting",
            json={"person_id": person_id, "subject": "Les specs"},
            headers=self.write_headers(),
        )

        corps = self.client().get(f"/api/people/{person_id}").get_json()
        self.assertEqual(len(corps["owed"]), 1)
        self.assertEqual(len(corps["waiting"]), 1)

    def test_fiche_introuvable_rend_404(self):
        self.assertEqual(self.client().get("/api/people/999").status_code, 404)

    def test_mise_a_jour(self):
        person_id = self._personne().get_json()["person"]["id"]
        reponse = self.client().patch(
            f"/api/people/{person_id}", json={"role": "Directeur"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.get_json()["person"]["role"], "Directeur")

    def test_suppression(self):
        person_id = self._personne().get_json()["person"]["id"]
        self.assertEqual(
            self.client().delete(f"/api/people/{person_id}", headers=self.write_headers()).status_code,
            200,
        )

    def test_attente_marquee_recue(self):
        person_id = self._personne().get_json()["person"]["id"]
        waiting_id = self.client().post(
            "/api/waiting",
            json={"person_id": person_id, "subject": "Les specs"},
            headers=self.write_headers(),
        ).get_json()["waiting"]["id"]

        reponse = self.client().patch(
            f"/api/waiting/{waiting_id}",
            json={"status": "received"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(self.client().get("/api/waiting").get_json()["waiting"], [])

    def test_statut_inconnu_rend_400(self):
        person_id = self._personne().get_json()["person"]["id"]
        waiting_id = self.client().post(
            "/api/waiting",
            json={"person_id": person_id, "subject": "Les specs"},
            headers=self.write_headers(),
        ).get_json()["waiting"]["id"]

        reponse = self.client().patch(
            f"/api/waiting/{waiting_id}", json={"status": "bof"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_attente_introuvable_rend_404(self):
        reponse = self.client().patch(
            "/api/waiting/999", json={"status": "received"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 404)

    def test_ecriture_depuis_un_autre_site_refusee(self):
        reponse = self.client().post(
            "/api/people", json={"name": "Intrus"}, headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(reponse.status_code, 403)


class PersonToolsTest(NomaiaTestCase):
    """Outils exposes au modele : c'est lui qui confond les deux directions."""

    def setUp(self):
        super().setUp()
        from services import tools

        self.tools = tools

    def test_attente_enregistree_par_son_nom(self):
        resultat = self.tools.execute(
            "add_waiting", {"person": "Paul", "subject": "Les specs"}
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(len(person_model.list_waiting()), 1)

    def test_interlocuteur_inconnu_est_cree(self):
        self.tools.execute("add_waiting", {"person": "Emma", "subject": "Un retour"})
        self.assertIsNotNone(person_model.find_by_name("Emma"))

    def test_alias_francais_acceptes(self):
        resultat = self.tools.execute("add_waiting", {"qui": "Paul", "sujet": "Les specs"})
        self.assertTrue(resultat["ok"], resultat)

    def test_tache_due_a_quelqu_un(self):
        # La moitie « je dois » doit etre actionnable, pas seulement decrite.
        resultat = self.tools.execute(
            "create_task", {"title": "Le devis ORION", "person": "Paul"}
        )
        self.assertTrue(resultat["ok"], resultat)

        personne = person_model.find_by_name("Paul")
        self.assertEqual(len(person_model.owed_tasks(personne["id"])), 1)
        # Et ce n'est surtout pas une attente.
        self.assertEqual(person_model.list_waiting(), [])

    def test_destinataire_ajoute_apres_coup(self):
        tache = task_model.create_task({"title": "Le devis"})
        resultat = self.tools.execute(
            "update_task", {"task_id": tache["id"], "person": "Paul"}
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(task_model.get_task(tache["id"])["person_name"], "Paul")

    def test_destinataire_efface(self):
        personne = person_model.create_person({"name": "Paul"})
        tache = task_model.create_task({"title": "Le devis", "person_id": personne["id"]})
        self.tools.execute("update_task", {"task_id": tache["id"], "person": "none"})
        self.assertIsNone(task_model.get_task(tache["id"])["person_id"])

    def test_fiche_separe_les_deux_directions(self):
        personne = person_model.create_person({"name": "Paul"})
        task_model.create_task({"title": "Le devis", "person_id": personne["id"]})
        person_model.create_waiting({"person_id": personne["id"], "subject": "Les specs"})

        resultat = self.tools.execute("person_status", {"name": "Paul"})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["i_owe_count"], 1)
        self.assertEqual(resultat["data"]["they_owe_count"], 1)

    def test_personne_inconnue_oriente_vers_la_liste(self):
        resultat = self.tools.execute("person_status", {"name": "Inconnu"})
        self.assertFalse(resultat["ok"])
        self.assertIn("list_people", resultat["error"])

    def test_attente_marquee_recue(self):
        attente = self.tools.execute(
            "add_waiting", {"person": "Paul", "subject": "Les specs"}
        )["data"]["waiting"]
        resultat = self.tools.execute("resolve_waiting", {"waiting_id": attente["id"]})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(person_model.list_waiting(status="open"), [])

    def test_statut_interdit_refuse(self):
        attente = self.tools.execute(
            "add_waiting", {"person": "Paul", "subject": "Les specs"}
        )["data"]["waiting"]
        resultat = self.tools.execute(
            "resolve_waiting", {"waiting_id": attente["id"], "status": "open"}
        )
        self.assertFalse(resultat["ok"])

    def test_le_catalogue_cite_les_outils_et_la_distinction(self):
        catalogue = self.tools.describe()
        for nom in ("list_people", "person_status", "list_waiting", "add_waiting", "resolve_waiting"):
            self.assertIn(nom, catalogue)
        # Le modele doit lire la distinction dans le catalogue, pas la deviner.
        self.assertIn("create_task", catalogue)

    def test_le_prompt_du_chat_porte_la_distinction(self):
        from services import assistant

        systeme = assistant.build_system()
        self.assertIn("ATTENTE", systeme)
        self.assertIn("TACHE", systeme)


if __name__ == "__main__":
    unittest.main()
