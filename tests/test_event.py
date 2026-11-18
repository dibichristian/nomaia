"""Tests de l'agenda : validation, repetitions, temps occupe, plan du jour.

Le coeur du lot est la soustraction : une capacite fausse contamine l'alerte
de surcharge, le point du matin et le calibrage des durees.
"""
import unittest
from datetime import date, datetime, timedelta

from models import event as event_model
from models import project as project_model
from models import setting
from models import task as task_model
from models.event import EventError
from services import heartbeat as heartbeat_service
from services import planner
from tests.base import NomaiaTestCase

LUNDI = date(2026, 9, 21)


def stamp(day, heure):
    """Compose un horodatage au format de stockage."""
    return f"{day.isoformat()} {heure}"


class MergeIntervalsTest(unittest.TestCase):
    """La fusion precede toute soustraction : sans elle, la capacite ment."""

    @staticmethod
    def _bloc(debut, fin):
        return (
            datetime(2026, 9, 21, *debut),
            datetime(2026, 9, 21, *fin),
        )

    def test_intervalles_disjoints_conserves(self):
        blocs = [self._bloc((9, 0), (10, 0)), self._bloc((14, 0), (15, 0))]
        self.assertEqual(len(event_model.merge_intervals(blocs)), 2)

    def test_chevauchement_fusionne(self):
        blocs = [self._bloc((9, 0), (11, 0)), self._bloc((10, 0), (12, 0))]
        fusion = event_model.merge_intervals(blocs)
        self.assertEqual(len(fusion), 1)
        self.assertEqual(fusion[0][1].hour, 12)

    def test_intervalle_englobe_disparait(self):
        blocs = [self._bloc((9, 0), (17, 0)), self._bloc((10, 0), (11, 0))]
        fusion = event_model.merge_intervals(blocs)
        self.assertEqual(len(fusion), 1)
        self.assertEqual((fusion[0][0].hour, fusion[0][1].hour), (9, 17))

    def test_intervalles_jointifs_fusionnes(self):
        blocs = [self._bloc((9, 0), (10, 0)), self._bloc((10, 0), (11, 0))]
        self.assertEqual(len(event_model.merge_intervals(blocs)), 1)

    def test_ordre_d_entree_sans_importance(self):
        blocs = [self._bloc((14, 0), (15, 0)), self._bloc((9, 0), (10, 0))]
        fusion = event_model.merge_intervals(blocs)
        self.assertEqual(fusion[0][0].hour, 9)


class SubtractTest(unittest.TestCase):
    """Decoupage des fenetres de travail par les intervalles occupes."""

    @staticmethod
    def _fenetre(debut, fin, etiquette="afternoon"):
        return (datetime(2026, 9, 21, debut, 0), datetime(2026, 9, 21, fin, 0), etiquette)

    @staticmethod
    def _occupe(debut, fin):
        return (datetime(2026, 9, 21, debut, 0), datetime(2026, 9, 21, fin, 0))

    def test_reunion_au_milieu_coupe_en_deux(self):
        restant = event_model.subtract([self._fenetre(13, 18)], [self._occupe(14, 16)])
        self.assertEqual(len(restant), 2)
        self.assertEqual((restant[0][0].hour, restant[0][1].hour), (13, 14))
        self.assertEqual((restant[1][0].hour, restant[1][1].hour), (16, 18))

    def test_etiquette_conservee_par_les_fragments(self):
        restant = event_model.subtract([self._fenetre(13, 18)], [self._occupe(14, 16)])
        self.assertTrue(all(fragment[2] == "afternoon" for fragment in restant))

    def test_reunion_couvrant_toute_la_fenetre(self):
        self.assertEqual(event_model.subtract([self._fenetre(13, 18)], [self._occupe(12, 19)]), [])

    def test_reunion_hors_fenetre_ne_retire_rien(self):
        restant = event_model.subtract([self._fenetre(13, 18)], [self._occupe(19, 20)])
        self.assertEqual(len(restant), 1)
        self.assertEqual((restant[0][0].hour, restant[0][1].hour), (13, 18))

    def test_reunion_a_cheval_sur_le_debut(self):
        restant = event_model.subtract([self._fenetre(13, 18)], [self._occupe(12, 14)])
        self.assertEqual(len(restant), 1)
        self.assertEqual((restant[0][0].hour, restant[0][1].hour), (14, 18))


class ValidationTest(NomaiaTestCase):
    """La saisie est une donnee hostile jusqu'a preuve du contraire."""

    def _base(self, **extra):
        donnees = {
            "title": "Point ORION",
            "starts_at": stamp(LUNDI, "14:00"),
            "ends_at": stamp(LUNDI, "15:00"),
        }
        donnees.update(extra)
        return donnees

    def test_creation_nominale(self):
        evenement = event_model.create_event(self._base())
        self.assertEqual(evenement["title"], "Point ORION")
        self.assertEqual(evenement["kind"], "meeting")
        self.assertFalse(evenement["is_occurrence"])

    def test_titre_obligatoire(self):
        with self.assertRaises(EventError):
            event_model.create_event(self._base(title="   "))

    def test_fin_avant_debut_refusee(self):
        with self.assertRaises(EventError):
            event_model.create_event(self._base(ends_at=stamp(LUNDI, "13:00")))

    def test_fin_egale_au_debut_refusee(self):
        with self.assertRaises(EventError):
            event_model.create_event(self._base(ends_at=stamp(LUNDI, "14:00")))

    def test_format_d_horodatage_refuse(self):
        with self.assertRaises(EventError):
            event_model.create_event(self._base(starts_at="21/09/2026 14h"))

    def test_type_inconnu_refuse(self):
        with self.assertRaises(EventError):
            event_model.create_event(self._base(kind="apero"))

    def test_repetition_inconnue_refusee(self):
        with self.assertRaises(EventError):
            event_model.create_event(self._base(repeat="monthly"))

    def test_evenement_de_plus_de_24h_refuse(self):
        with self.assertRaises(EventError):
            event_model.create_event(
                self._base(ends_at=stamp(LUNDI + timedelta(days=2), "14:00"))
            )

    def test_fin_de_repetition_avant_le_debut_refusee(self):
        with self.assertRaises(EventError):
            event_model.create_event(
                self._base(repeat="weekly", repeat_until=(LUNDI - timedelta(days=7)).isoformat())
            )

    def test_mise_a_jour_partielle_controle_les_bornes(self):
        # Changer la seule heure de fin doit rester soumis au controle.
        evenement = event_model.create_event(self._base())
        with self.assertRaises(EventError):
            event_model.update_event(evenement["id"], {"ends_at": stamp(LUNDI, "13:00")})

    def test_projet_rattache(self):
        projet = project_model.create_project("ORION")
        evenement = event_model.create_event(self._base(project_id=projet["id"]))
        self.assertEqual(evenement["project_name"], "ORION")


class RepeatTest(NomaiaTestCase):
    """Expansion des series : calculee a la demande, jamais stockee."""

    def _serie(self, repeat="weekly", **extra):
        donnees = {
            "title": "Point hebdo",
            "starts_at": stamp(LUNDI, "09:00"),
            "ends_at": stamp(LUNDI, "10:00"),
            "repeat": repeat,
        }
        donnees.update(extra)
        return event_model.create_event(donnees)

    def test_une_seule_ligne_en_base(self):
        self._serie()
        lignes = self.app.test_client() and None  # pas d'appel HTTP ici
        from models import get_db

        self.assertEqual(get_db().execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"], 1)

    def test_hebdomadaire_developpe_sur_quatre_semaines(self):
        self._serie()
        evenements = event_model.list_events(LUNDI, LUNDI + timedelta(days=27))
        self.assertEqual(len(evenements), 4)

    def test_quotidien_developpe_par_jour(self):
        self._serie(repeat="daily")
        evenements = event_model.list_events(LUNDI, LUNDI + timedelta(days=4))
        self.assertEqual(len(evenements), 5)

    def test_occurrence_porte_le_bon_jour_et_la_bonne_heure(self):
        self._serie()
        evenements = event_model.list_events(LUNDI + timedelta(days=7), LUNDI + timedelta(days=7))
        self.assertEqual(len(evenements), 1)
        self.assertEqual(evenements[0]["starts_at"], stamp(LUNDI + timedelta(days=7), "09:00"))
        self.assertTrue(evenements[0]["is_occurrence"])

    def test_la_premiere_occurrence_n_en_est_pas_une(self):
        self._serie()
        evenements = event_model.list_for_day(LUNDI)
        self.assertFalse(evenements[0]["is_occurrence"])

    def test_fin_de_repetition_respectee(self):
        self._serie(repeat_until=(LUNDI + timedelta(days=7)).isoformat())
        evenements = event_model.list_events(LUNDI, LUNDI + timedelta(days=60))
        self.assertEqual(len(evenements), 2)

    def test_serie_ancienne_visible_dans_une_fenetre_lointaine(self):
        # Le calcul saute directement a la premiere occurrence visible plutot
        # que de parcourir depuis l'origine.
        self._serie()
        lointain = LUNDI + timedelta(days=700)
        evenements = event_model.list_events(lointain, lointain + timedelta(days=6))
        self.assertEqual(len(evenements), 1)

    def test_annulation_d_une_occurrence(self):
        serie = self._serie()
        cible = LUNDI + timedelta(days=7)
        event_model.cancel_occurrence(serie["id"], cible.isoformat())

        self.assertEqual(event_model.list_for_day(cible), [])
        self.assertEqual(len(event_model.list_for_day(LUNDI)), 1)

    def test_annulation_refusee_sur_un_evenement_unique(self):
        unique = self._serie(repeat="none")
        with self.assertRaises(EventError):
            event_model.cancel_occurrence(unique["id"], LUNDI.isoformat())

    def test_deplacer_une_serie_purge_ses_annulations(self):
        serie = self._serie()
        event_model.cancel_occurrence(serie["id"], (LUNDI + timedelta(days=7)).isoformat())
        event_model.update_event(serie["id"], {"starts_at": stamp(LUNDI, "11:00"),
                                               "ends_at": stamp(LUNDI, "12:00")})
        # L'annulation portait sur une cadence qui n'existe plus.
        self.assertEqual(len(event_model.list_for_day(LUNDI + timedelta(days=7))), 1)

    def test_suppression_emporte_la_serie(self):
        serie = self._serie()
        self.assertTrue(event_model.delete_event(serie["id"]))
        self.assertEqual(event_model.list_events(LUNDI, LUNDI + timedelta(days=30)), [])

    def test_periode_inversee_refusee(self):
        with self.assertRaises(EventError):
            event_model.list_events(LUNDI, LUNDI - timedelta(days=1))


class BusyTest(NomaiaTestCase):
    """Temps occupe d'une journee, chevauchements compris."""

    def test_journee_vide(self):
        self.assertEqual(event_model.busy_intervals(LUNDI), [])
        self.assertEqual(event_model.stats_for_day(LUNDI), 0)

    def test_deux_reunions_qui_se_recouvrent_ne_comptent_qu_une_fois(self):
        event_model.create_event(
            {"title": "A", "starts_at": stamp(LUNDI, "14:00"), "ends_at": stamp(LUNDI, "16:00")}
        )
        event_model.create_event(
            {"title": "B", "starts_at": stamp(LUNDI, "15:00"), "ends_at": stamp(LUNDI, "17:00")}
        )
        # 14 h - 17 h, pas 4 heures.
        self.assertEqual(event_model.stats_for_day(LUNDI), 180)


class PlanWithAgendaTest(NomaiaTestCase):
    """Le plan du jour doit tenir compte du temps deja pris."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")

    def test_capacite_pleine_sans_agenda(self):
        plan = planner.build_plan(LUNDI)
        self.assertEqual(plan["capacity_min"], 540)
        self.assertEqual(plan["busy_min"], 0)
        self.assertEqual(plan["events"], [])

    def test_reunion_retire_du_temps(self):
        event_model.create_event(
            {"title": "Client", "starts_at": stamp(LUNDI, "14:00"), "ends_at": stamp(LUNDI, "16:00")}
        )
        plan = planner.build_plan(LUNDI)
        self.assertEqual(plan["capacity_min"], 420)
        self.assertEqual(plan["busy_min"], 120)
        self.assertEqual(len(plan["events"]), 1)

    def test_reunion_hors_heures_ne_retire_rien(self):
        event_model.create_event(
            {"title": "Diner", "starts_at": stamp(LUNDI, "19:00"), "ends_at": stamp(LUNDI, "21:00")}
        )
        self.assertEqual(planner.build_plan(LUNDI)["capacity_min"], 540)

    def test_reunion_pendant_la_pause_ne_retire_rien(self):
        event_model.create_event(
            {"title": "Dejeuner", "starts_at": stamp(LUNDI, "12:00"), "ends_at": stamp(LUNDI, "13:00")}
        )
        self.assertEqual(planner.build_plan(LUNDI)["capacity_min"], 540)

    def test_aucune_tache_placee_dans_une_reunion(self):
        event_model.create_event(
            {"title": "Client", "starts_at": stamp(LUNDI, "08:00"), "ends_at": stamp(LUNDI, "12:00")}
        )
        task_model.create_task({"title": "Tache du matin", "estimate_min": 60})

        plan = planner.build_plan(LUNDI)
        for creneau in plan["slots"]:
            self.assertGreaterEqual(creneau["start"], "13:00")

    def test_charge_calculee_sur_la_capacite_reelle(self):
        event_model.create_event(
            {"title": "Client", "starts_at": stamp(LUNDI, "08:00"), "ends_at": stamp(LUNDI, "12:00")}
        )
        task_model.create_task({"title": "Une tache", "estimate_min": 120})
        setting.set_value("plan_buffer_pct", "0")

        plan = planner.build_plan(LUNDI)
        # 120 min planifiees sur 300 min restantes, pas sur 540.
        self.assertEqual(plan["capacity_min"], 300)
        self.assertEqual(plan["load_pct"], 40)

    def test_journee_entierement_prise(self):
        event_model.create_event(
            {"title": "Formation", "starts_at": stamp(LUNDI, "07:00"), "ends_at": stamp(LUNDI, "19:00")}
        )
        task_model.create_task({"title": "Une tache"})

        plan = planner.build_plan(LUNDI)
        self.assertEqual(plan["capacity_min"], 0)
        self.assertEqual(plan["slots"], [])
        self.assertEqual(plan["load_pct"], 0)
        self.assertEqual(len(plan["overflow"]), 1)

    def test_fragment_trop_court_ecarte_mais_compte_comme_perdu(self):
        # Il reste 10 minutes avant midi : inexploitables, mais bien perdues.
        event_model.create_event(
            {"title": "Client", "starts_at": stamp(LUNDI, "08:00"), "ends_at": stamp(LUNDI, "11:50")}
        )
        plan = planner.build_plan(LUNDI)
        self.assertEqual(plan["capacity_min"], 300)
        self.assertEqual(plan["busy_min"], 240)

    def test_serie_repetitive_prise_en_compte(self):
        event_model.create_event(
            {
                "title": "Daily",
                "starts_at": stamp(LUNDI, "09:00"),
                "ends_at": stamp(LUNDI, "09:30"),
                "repeat": "daily",
            }
        )
        plan = planner.build_plan(LUNDI + timedelta(days=3))
        self.assertEqual(plan["busy_min"], 30)


class FreeSlotsTest(NomaiaTestCase):
    """Repond a la question « quand suis-je libre ? »."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        setting.set_value("lunch_start", "12:00")
        setting.set_value("lunch_end", "13:00")

    def test_journee_libre_rend_deux_fenetres(self):
        creneaux = planner.free_slots(LUNDI)
        self.assertEqual(len(creneaux), 2)
        self.assertEqual(creneaux[0]["duration_min"], 240)

    def test_les_trous_trop_courts_sont_ecartes(self):
        event_model.create_event(
            {"title": "Client", "starts_at": stamp(LUNDI, "08:00"), "ends_at": stamp(LUNDI, "11:30")}
        )
        creneaux = planner.free_slots(LUNDI, min_minutes=60)
        self.assertTrue(all(item["duration_min"] >= 60 for item in creneaux))

    def test_les_trous_portent_leur_fenetre(self):
        creneaux = planner.free_slots(LUNDI)
        self.assertEqual([item["window"] for item in creneaux], ["morning", "afternoon"])


class TimelineTest(NomaiaTestCase):
    """Taches et reunions dans un seul fil horaire."""

    def test_fusion_triee(self):
        setting.set_value("work_start", "08:00")
        setting.set_value("plan_buffer_pct", "0")
        event_model.create_event(
            {"title": "Client", "starts_at": stamp(LUNDI, "10:00"), "ends_at": stamp(LUNDI, "11:00")}
        )
        task_model.create_task({"title": "Une tache", "estimate_min": 60})

        fil = planner.timeline(planner.build_plan(LUNDI))
        self.assertEqual([entree["kind"] for entree in fil], ["task", "event"])
        self.assertEqual(fil[1]["event"]["title"], "Client")


class ReminderTest(NomaiaTestCase):
    """Rappel avant reunion : jamais deux fois, jamais en retard."""

    def _battement(self):
        return heartbeat_service.Heartbeat(self.app)

    def _reunion(self, debut, fin, **extra):
        donnees = {"title": "Client", "starts_at": debut, "ends_at": fin}
        donnees.update(extra)
        return event_model.create_event(donnees)

    def test_le_texte_cite_l_heure_et_le_lieu(self):
        evenement = self._reunion(stamp(LUNDI, "14:00"), stamp(LUNDI, "15:00"), location="Visio")
        texte = heartbeat_service.reminder_text(evenement)
        self.assertIn("14:00", texte)
        self.assertIn("Visio", texte)

    def test_rappel_envoye_une_seule_fois(self):
        setting.set_value("event_reminder_min", "10")
        debut = datetime.combine(LUNDI, datetime.min.time()).replace(hour=14)
        self._reunion(stamp(LUNDI, "14:00"), stamp(LUNDI, "15:00"))

        battement = self._battement()
        envoyes = []
        battement._deliver = lambda nom, texte: envoyes.append(texte)

        maintenant = debut - timedelta(minutes=5)
        battement._check_reminders(maintenant)
        battement._check_reminders(maintenant + timedelta(minutes=1))

        self.assertEqual(len(envoyes), 1)

    def test_reunion_trop_lointaine_pas_encore_annoncee(self):
        setting.set_value("event_reminder_min", "10")
        debut = datetime.combine(LUNDI, datetime.min.time()).replace(hour=14)
        self._reunion(stamp(LUNDI, "14:00"), stamp(LUNDI, "15:00"))

        battement = self._battement()
        envoyes = []
        battement._deliver = lambda nom, texte: envoyes.append(texte)
        battement._check_reminders(debut - timedelta(hours=2))

        self.assertEqual(envoyes, [])

    def test_reunion_deja_commencee_depuis_longtemps_ignoree(self):
        # Allumer le PC a 15 h ne doit pas annoncer la reunion de 9 h.
        setting.set_value("event_reminder_min", "10")
        self._reunion(stamp(LUNDI, "09:00"), stamp(LUNDI, "10:00"))

        battement = self._battement()
        envoyes = []
        battement._deliver = lambda nom, texte: envoyes.append(texte)
        battement._check_reminders(
            datetime.combine(LUNDI, datetime.min.time()).replace(hour=15)
        )

        self.assertEqual(envoyes, [])

    def test_absence_ne_declenche_pas_de_rappel(self):
        setting.set_value("event_reminder_min", "10")
        debut = datetime.combine(LUNDI, datetime.min.time()).replace(hour=14)
        self._reunion(stamp(LUNDI, "14:00"), stamp(LUNDI, "15:00"), kind="absence")

        battement = self._battement()
        envoyes = []
        battement._deliver = lambda nom, texte: envoyes.append(texte)
        battement._check_reminders(debut - timedelta(minutes=5))

        self.assertEqual(envoyes, [])

    def test_rappel_desactivable(self):
        setting.set_value("event_reminder_enabled", "0")
        debut = datetime.combine(LUNDI, datetime.min.time()).replace(hour=14)
        self._reunion(stamp(LUNDI, "14:00"), stamp(LUNDI, "15:00"))

        battement = self._battement()
        envoyes = []
        battement._deliver = lambda nom, texte: envoyes.append(texte)
        battement._check_reminders(debut - timedelta(minutes=5))

        self.assertEqual(envoyes, [])

    def test_deux_reunions_simultanees_partent_ensemble(self):
        setting.set_value("event_reminder_min", "10")
        debut = datetime.combine(LUNDI, datetime.min.time()).replace(hour=14)
        self._reunion(stamp(LUNDI, "14:00"), stamp(LUNDI, "15:00"))
        event_model.create_event(
            {"title": "Autre", "starts_at": stamp(LUNDI, "14:00"), "ends_at": stamp(LUNDI, "14:30")}
        )

        battement = self._battement()
        envoyes = []
        battement._deliver = lambda nom, texte: envoyes.append(texte)
        battement._check_reminders(debut - timedelta(minutes=5))

        self.assertEqual(len(envoyes), 2)


class MorningBriefTest(NomaiaTestCase):
    """Le point du matin annonce les rendez-vous du jour."""

    def test_agenda_cite_dans_le_point_du_matin(self):
        event_model.create_event(
            {
                "title": "Point ORION",
                "starts_at": stamp(LUNDI, "14:00"),
                "ends_at": stamp(LUNDI, "16:00"),
                "location": "Visio",
            }
        )
        texte = heartbeat_service.morning_brief(LUNDI)
        self.assertIn("Point ORION", texte)
        self.assertIn("RDV", texte)
        self.assertIn("d'agenda", texte)


class EventApiTest(NomaiaTestCase):
    """Routes HTTP de l'agenda, garde-fou anti-CSRF compris."""

    def _creer(self, **extra):
        charge = {
            "title": "Point ORION",
            "starts_at": stamp(LUNDI, "14:00"),
            "ends_at": stamp(LUNDI, "15:00"),
        }
        charge.update(extra)
        return self.client().post("/api/events", json=charge, headers=self.write_headers())

    def test_liste_vide_au_depart(self):
        reponse = self.client().get("/api/events")
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["events"], [])

    def test_creation_et_relecture(self):
        self.assertEqual(self._creer().status_code, 201)
        reponse = self.client().get(f"/api/events?start={LUNDI}&end={LUNDI}")
        self.assertEqual(len(reponse.get_json()["events"]), 1)

    def test_creation_invalide_rend_400(self):
        reponse = self._creer(ends_at=stamp(LUNDI, "13:00"))
        self.assertEqual(reponse.status_code, 400)

    def test_mise_a_jour(self):
        event_id = self._creer().get_json()["event"]["id"]
        reponse = self.client().patch(
            f"/api/events/{event_id}", json={"title": "Renomme"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.get_json()["event"]["title"], "Renomme")

    def test_mise_a_jour_introuvable_rend_404(self):
        reponse = self.client().patch(
            "/api/events/999", json={"title": "X"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 404)

    def test_suppression(self):
        event_id = self._creer().get_json()["event"]["id"]
        self.assertEqual(
            self.client().delete(f"/api/events/{event_id}", headers=self.write_headers()).status_code,
            200,
        )
        self.assertEqual(self.client().get("/api/events").get_json()["events"], [])

    def test_annulation_d_occurrence(self):
        event_id = self._creer(repeat="weekly").get_json()["event"]["id"]
        cible = (LUNDI + timedelta(days=7)).isoformat()

        reponse = self.client().post(
            f"/api/events/{event_id}/cancel", json={"day": cible}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 200)

        restants = self.client().get(f"/api/events?start={cible}&end={cible}").get_json()
        self.assertEqual(restants["events"], [])

    def test_annulation_sur_evenement_unique_rend_400(self):
        event_id = self._creer().get_json()["event"]["id"]
        reponse = self.client().post(
            f"/api/events/{event_id}/cancel",
            json={"day": LUNDI.isoformat()},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)

    def test_periode_trop_large_refusee(self):
        reponse = self.client().get("/api/events?start=2026-01-01&end=2027-01-01")
        self.assertEqual(reponse.status_code, 400)

    def test_date_mal_formee_refusee(self):
        self.assertEqual(self.client().get("/api/events?start=hier").status_code, 400)

    def test_creneaux_libres(self):
        reponse = self.client().get(f"/api/events/free?date={LUNDI}&min_minutes=60")
        self.assertEqual(reponse.status_code, 200)
        self.assertGreater(reponse.get_json()["free_min"], 0)

    def test_ecriture_depuis_un_autre_site_refusee(self):
        reponse = self.client().post(
            "/api/events",
            json={"title": "X", "starts_at": stamp(LUNDI, "14:00"), "ends_at": stamp(LUNDI, "15:00")},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(reponse.status_code, 403)


class AgendaToolsTest(NomaiaTestCase):
    """Outils d'agenda exposes au modele : c'est lui qui les appelle mal."""

    def setUp(self):
        super().setUp()
        from services import tools

        self.tools = tools

    def _creer(self, **args):
        charge = {
            "title": "Point ORION",
            "starts_at": stamp(LUNDI, "14:00"),
            "ends_at": stamp(LUNDI, "15:00"),
        }
        charge.update(args)
        return self.tools.execute("create_event", charge)

    def test_creation_nominale(self):
        resultat = self._creer()
        self.assertTrue(resultat["ok"], resultat)

    def test_duree_a_la_place_de_la_fin(self):
        resultat = self.tools.execute(
            "create_event",
            {"title": "Appel", "starts_at": stamp(LUNDI, "14:00"), "duration_min": 45},
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["event"]["ends_at"], stamp(LUNDI, "14:45"))

    def test_alias_francais_acceptes(self):
        resultat = self.tools.execute(
            "create_event",
            {
                "titre": "Reunion",
                "debut": stamp(LUNDI, "09:00"),
                "fin": stamp(LUNDI, "10:00"),
                "lieu": "Bureau",
            },
        )
        self.assertTrue(resultat["ok"], resultat)

    def test_format_de_date_refuse_sans_trace(self):
        resultat = self._creer(starts_at="mardi prochain")
        self.assertFalse(resultat["ok"])
        self.assertNotIn("Traceback", resultat["error"])

    def test_projet_introuvable_cree_quand_meme(self):
        resultat = self._creer(project="PROJET_QUI_N_EXISTE_PAS")
        self.assertTrue(resultat["ok"], resultat)

    def test_liste_des_evenements(self):
        self._creer()
        resultat = self.tools.execute("list_events", {"date": LUNDI.isoformat()})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["count"], 1)

    def test_creneaux_libres(self):
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "18:00")
        resultat = self.tools.execute(
            "find_free_slots", {"date": LUNDI.isoformat(), "min_minutes": 60}
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertGreater(resultat["data"]["free_min"], 0)

    def test_annulation_d_une_serie_refusee_sans_portee(self):
        # Supprimer une serie en croyant annuler un mardi ne se rattrape pas.
        event_id = self._creer(repeat="weekly")["data"]["event"]["id"]
        resultat = self.tools.execute("cancel_event", {"event_id": event_id})
        self.assertFalse(resultat["ok"])
        self.assertIn("repete", resultat["error"])

    def test_annulation_d_une_occurrence(self):
        event_id = self._creer(repeat="weekly")["data"]["event"]["id"]
        cible = (LUNDI + timedelta(days=7)).isoformat()
        resultat = self.tools.execute("cancel_event", {"event_id": event_id, "day": cible})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(event_model.list_for_day(LUNDI + timedelta(days=7)), [])
        self.assertEqual(len(event_model.list_for_day(LUNDI)), 1)

    def test_suppression_d_un_evenement_unique(self):
        event_id = self._creer()["data"]["event"]["id"]
        resultat = self.tools.execute("cancel_event", {"event_id": event_id})
        self.assertTrue(resultat["ok"], resultat)
        self.assertIsNone(event_model.get_event(event_id))

    def test_evenement_inexistant_rend_une_erreur_lisible(self):
        resultat = self.tools.execute("cancel_event", {"event_id": 9999})
        self.assertFalse(resultat["ok"])
        self.assertIn("introuvable", resultat["error"])

    def test_le_catalogue_cite_les_outils_d_agenda(self):
        catalogue = self.tools.describe()
        for nom in ("list_events", "create_event", "cancel_event", "find_free_slots"):
            self.assertIn(nom, catalogue)


if __name__ == "__main__":
    unittest.main()
