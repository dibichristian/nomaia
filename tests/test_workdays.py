"""Tests des jours de repos : calendrier ouvre, feries, plan et messages.

Un jour de repos doit supprimer la journee de travail, pas seulement la
raccourcir : capacite nulle, aucun message proactif, et aucun temps compte
avant une echeance. Les rappels d'agenda, eux, continuent.
"""
from datetime import date, datetime

from flask import g

from models import day_off as day_off_model
from models import event as event_model
from models import setting
from models import task as task_model
from models.day_off import DayOffError
from services import heartbeat, holidays, planner, scheduler, tools, workdays
from tests.base import NomaiaTestCase
from tests.test_heartbeat import FauxBridge
from tests.test_telegram_chat import BridgeEspion

VENDREDI = date(2026, 10, 2)
SAMEDI = date(2026, 10, 3)
DIMANCHE = date(2026, 10, 4)
LUNDI = date(2026, 10, 5)
MARDI = date(2026, 10, 6)


class CalendrierTestCase(NomaiaTestCase):
    """Semaine de lundi a vendredi, sans ferie importe."""

    def setUp(self):
        super().setUp()
        self.semaine("0,1,2,3,4")

    @staticmethod
    def semaine(valeur):
        """Pose les jours ouvres et vide le cache de requete."""
        setting.set_value("work_days", valeur)
        g.pop(workdays._WEEKDAYS_KEY, None)

    @staticmethod
    def conge(debut, fin=None, titre="Conges"):
        """Cree un conge sur une plage de dates."""
        return day_off_model.create_day_off(
            {
                "title": titre,
                "start_day": debut.isoformat(),
                "end_day": (fin or debut).isoformat(),
            }
        )


class JoursDeReposModeleTest(CalendrierTestCase):
    """Saisie et lecture des conges."""

    def test_un_seul_jour_sans_fin(self):
        entree = self.conge(LUNDI)
        self.assertEqual(entree["end_day"], LUNDI.isoformat())
        self.assertEqual(entree["days"], 1)

    def test_plage_de_plusieurs_jours(self):
        entree = self.conge(LUNDI, date(2026, 10, 16))
        self.assertEqual(entree["days"], 12)

    def test_fin_avant_debut_refusee(self):
        with self.assertRaises(DayOffError):
            self.conge(MARDI, LUNDI)

    def test_plage_demesuree_refusee(self):
        with self.assertRaises(DayOffError):
            self.conge(LUNDI, date(2028, 1, 1))

    def test_date_invalide_refusee(self):
        with self.assertRaises(DayOffError):
            day_off_model.create_day_off({"title": "X", "start_day": "2026-02-30"})

    def test_carte_ne_couvre_que_la_periode(self):
        self.conge(VENDREDI, MARDI)
        carte = day_off_model.off_map(LUNDI, date(2026, 10, 10))
        self.assertEqual(sorted(carte), [LUNDI, MARDI])

    def test_ecriture_vide_le_cache(self):
        self.assertFalse(day_off_model.off_map(LUNDI, LUNDI))
        self.conge(LUNDI)
        self.assertIn(LUNDI, day_off_model.off_map(LUNDI, LUNDI))


class JoursOuvresTest(CalendrierTestCase):
    """Motif de repos et delai en jours ouvres."""

    def test_week_end(self):
        self.assertEqual(workdays.day_off_reason(SAMEDI), "Week-end")
        self.assertTrue(workdays.is_working_day(VENDREDI))

    def test_conge_donne_son_motif(self):
        self.conge(LUNDI, titre="Vacances")
        self.assertEqual(workdays.day_off_reason(LUNDI), "Vacances")

    def test_semaine_personnalisee(self):
        self.semaine("0,1,2,3,4,5")
        self.assertTrue(workdays.is_working_day(SAMEDI))
        self.assertFalse(workdays.is_working_day(DIMANCHE))

    def test_reglage_illisible_retombe_sur_lundi_vendredi(self):
        self.assertEqual(workdays.parse_work_days("n'importe quoi"), workdays.DEFAULT_WORK_DAYS)
        self.assertEqual(workdays.parse_work_days(""), workdays.DEFAULT_WORK_DAYS)

    def test_vendredi_pour_lundi_fait_un_jour(self):
        self.assertEqual(workdays.working_days_until(LUNDI, VENDREDI), 1)

    def test_conge_allonge_le_delai(self):
        self.conge(LUNDI)
        self.assertEqual(workdays.working_days_until(MARDI, VENDREDI), 1)

    def test_echeance_un_jour_chome_vue_la_veille(self):
        # Due samedi, vue vendredi : c'est aujourd'hui qu'il faut la finir.
        self.assertEqual(workdays.working_days_until(SAMEDI, VENDREDI), 0)

    def test_retard_sans_jour_ouvre_ecoule_reste_un_retard(self):
        self.assertEqual(workdays.working_days_until(SAMEDI, DIMANCHE), -1)

    def test_retard_compte_en_jours_ouvres(self):
        self.assertEqual(workdays.working_days_until(VENDREDI, MARDI), -2)

    def test_score_d_urgence_saute_le_week_end(self):
        tache = {"priority": 3, "deadline": LUNDI.isoformat()}
        self.assertEqual(scheduler.urgency(tache, VENDREDI), scheduler.URGENCY_BY_DELAY[1])

    def test_calendrier_jour_par_jour(self):
        self.conge(LUNDI, titre="Vacances")
        jours = workdays.calendar(VENDREDI, LUNDI)
        self.assertEqual([jour["working"] for jour in jours], [True, False, False, False])
        self.assertEqual(jours[-1]["reason"], "Vacances")


class FeriesTest(CalendrierTestCase):
    """Feries calculables, import unique et changement de pays."""

    def test_paques(self):
        self.assertEqual(holidays.easter_sunday(2025), date(2025, 4, 20))
        self.assertEqual(holidays.easter_sunday(2026), date(2026, 4, 5))
        self.assertEqual(holidays.easter_sunday(2027), date(2027, 3, 28))

    def test_feries_ivoiriens_2026(self):
        jours = dict(holidays.holidays_for("CI", 2026))
        self.assertEqual(jours[date(2026, 8, 7)], "Fête de l'Indépendance")
        self.assertEqual(jours[date(2026, 4, 6)], "Lundi de Pâques")
        self.assertEqual(jours[date(2026, 5, 14)], "Ascension")
        self.assertEqual(jours[date(2026, 5, 25)], "Lundi de Pentecôte")
        self.assertIn(date(2026, 11, 15), jours)

    def test_pays_inconnu(self):
        self.assertEqual(holidays.holidays_for("XX", 2026), [])

    def test_import_annee_courante_et_suivante(self):
        setting.set_value("holiday_country", "CI")
        ajoutes = holidays.ensure_imported(VENDREDI)

        attendus = len(holidays.holidays_for("CI", 2026)) + len(holidays.holidays_for("CI", 2027))
        self.assertEqual(ajoutes, attendus)
        self.assertEqual(workdays.day_off_reason(date(2026, 11, 2)), None)
        self.assertEqual(workdays.day_off_reason(date(2027, 1, 1)), "Jour de l'An")

    def test_ferie_supprime_n_est_pas_reimporte(self):
        setting.set_value("holiday_country", "CI")
        holidays.ensure_imported(VENDREDI)
        toussaint = day_off_model.list_between(date(2026, 11, 1), date(2026, 11, 1))[0]
        day_off_model.delete_day_off(toussaint["id"])

        self.assertEqual(holidays.ensure_imported(VENDREDI), 0)
        self.assertEqual(day_off_model.list_between(date(2026, 11, 1), date(2026, 11, 1)), [])

    def test_import_manuel_idempotent(self):
        setting.set_value("holiday_country", "CI")
        premier = holidays.import_year(2026)
        self.assertGreater(premier, 0)
        self.assertEqual(holidays.import_year(2026), 0)

    def test_changement_de_pays_retire_les_feries_futurs_seulement(self):
        setting.set_value("holiday_country", "FR")
        holidays.import_year(2026, "FR")
        self.conge(date(2026, 11, 11), titre="Saisie manuelle")

        setting.set_value("holiday_country", "CI")
        holidays.ensure_imported(VENDREDI)

        restants = day_off_model.list_between(date(2026, 1, 1), date(2026, 12, 31))
        # Passe francais conserve, futur francais retire, saisie manuelle intacte.
        self.assertIn("Victoire 1945", [e["title"] for e in restants])
        self.assertNotIn("Armistice 1918", [e["title"] for e in restants])
        self.assertIn("Saisie manuelle", [e["title"] for e in restants])

    def test_aucun_pays_aucun_import(self):
        setting.set_value("holiday_country", "")
        self.assertEqual(holidays.ensure_imported(VENDREDI), 0)


class PlanJourDeReposTest(CalendrierTestCase):
    """Un jour chome n'a ni capacite ni trou libre."""

    def test_samedi_rien_n_est_planifie(self):
        task_model.create_task({"title": "Rapport", "estimate_min": 60})
        plan = planner.build_plan(SAMEDI)

        self.assertEqual(plan["slots"], [])
        self.assertEqual(plan["capacity_min"], 0)
        self.assertEqual(plan["day_off"], "Week-end")
        self.assertEqual(len(plan["overflow"]), 1)

    def test_jour_ouvre_sans_motif(self):
        self.assertIsNone(planner.build_plan(VENDREDI)["day_off"])
        self.assertGreater(planner.build_plan(VENDREDI)["capacity_min"], 0)

    def test_conge_vide_les_trous_libres(self):
        self.conge(LUNDI)
        self.assertEqual(planner.free_slots(LUNDI), [])


class EvenementPersonnelTest(CalendrierTestCase):
    """Le type personal : visible, rappele, et marque dans le point du matin."""

    def _perso(self, jour, debut="10:00", fin="11:00"):
        return event_model.create_event(
            {
                "title": "Medecin",
                "starts_at": f"{jour.isoformat()} {debut}",
                "ends_at": f"{jour.isoformat()} {fin}",
                "kind": "personal",
            }
        )

    def test_retire_du_temps_pendant_les_heures_de_travail(self):
        sans = planner.build_plan(VENDREDI)["capacity_min"]
        self._perso(VENDREDI)
        self.assertEqual(planner.build_plan(VENDREDI)["capacity_min"], sans - 60)

    def test_le_soir_ne_retire_rien(self):
        sans = planner.build_plan(VENDREDI)["capacity_min"]
        self._perso(VENDREDI, "19:00", "20:00")
        self.assertEqual(planner.build_plan(VENDREDI)["capacity_min"], sans)

    def test_marque_perso_dans_le_point_du_matin(self):
        self._perso(VENDREDI)
        self.assertIn("[Perso] Medecin", heartbeat.morning_brief(VENDREDI))

    def test_alias_du_chat(self):
        self.assertEqual(tools._as_kind("sport"), "personal")


class MessagesJourDeReposTest(NomaiaTestCase):
    """Aucun message proactif un jour chome ; les rappels continuent."""

    def setUp(self):
        super().setUp()
        self.bridge = FauxBridge()
        self.app.extensions["telegram"] = self.bridge
        self.beat = heartbeat.Heartbeat(self.app)
        task_model.create_task({"title": "Une tache", "estimate_min": 60})
        aujourd_hui = date.today().isoformat()
        day_off_model.create_day_off({"title": "Tabaski", "start_day": aujourd_hui})

    def _a_neuf_heures(self):
        return datetime.combine(date.today(), datetime.min.time()).replace(hour=9)

    def test_le_point_du_matin_ne_part_pas(self):
        self.beat._check(self._a_neuf_heures())

        self.assertEqual(self.bridge.messages, [])
        # Marque comme tenu : il ne doit pas partir plus tard dans la journee.
        self.assertEqual(setting.get("heartbeat_last_morning"), date.today().isoformat())

    def test_le_rappel_de_reunion_part_quand_meme(self):
        maintenant = self._a_neuf_heures()
        event_model.create_event(
            {
                "title": "Appel famille",
                "starts_at": maintenant.replace(minute=5).strftime("%Y-%m-%d %H:%M"),
                "ends_at": maintenant.replace(minute=30).strftime("%Y-%m-%d %H:%M"),
                "kind": "personal",
            }
        )
        setting.set_value("event_reminder_watermark", "")
        self.beat._check_reminders(maintenant)

        self.assertTrue(any("Appel famille" in texte for texte in self.bridge.messages))

    def test_l_instantane_annonce_le_repos(self):
        bridge = BridgeEspion(self.app)
        self.assertIn("repos (Tabaski)", bridge._digest_text())


class OutilsJourDeReposTest(CalendrierTestCase):
    """Outils du chat : poser, lister, supprimer un conge."""

    def test_poser_un_conge_de_plusieurs_jours(self):
        resultat = tools.execute(
            "create_day_off",
            {"title": "Conges", "start_day": "2026-10-12", "end_day": "2026-10-16"},
        )
        self.assertTrue(resultat["ok"])
        self.assertEqual(resultat["data"]["day_off"]["days"], 5)

    def test_type_inconnu_refuse(self):
        resultat = tools.execute(
            "create_day_off", {"title": "X", "start_day": "2026-10-12", "kind": "fete"}
        )
        self.assertFalse(resultat["ok"])

    def test_agenda_cite_les_jours_non_travailles(self):
        resultat = tools.execute("list_events", {"date": VENDREDI.isoformat(), "days": 3})
        self.assertEqual(
            [jour["date"] for jour in resultat["data"]["days_off"]],
            [SAMEDI.isoformat(), DIMANCHE.isoformat()],
        )

    def test_semaine_ordinaire_sans_cle_vide(self):
        resultat = tools.execute("list_events", {"date": LUNDI.isoformat(), "days": 2})
        self.assertNotIn("days_off", resultat["data"])

    def test_lister_puis_supprimer(self):
        entree = self.conge(LUNDI)
        liste = tools.execute("list_days_off", {"date": VENDREDI.isoformat(), "days": 7})
        self.assertEqual(liste["data"]["days_off"][0]["id"], entree["id"])

        suppression = tools.execute("delete_day_off", {"day_off_id": entree["id"]})
        self.assertTrue(suppression["ok"])
        self.assertIsNone(day_off_model.get_day_off(entree["id"]))


class ApiJourDeReposTest(CalendrierTestCase):
    """Routes /api/days-off et reglages du calendrier."""

    def test_creer_lister_modifier_supprimer(self):
        client = self.client()
        cree = client.post(
            "/api/days-off",
            json={"title": "Conges", "start_day": "2026-10-12", "end_day": "2026-10-14"},
            headers=self.write_headers(),
        )
        self.assertEqual(cree.status_code, 201)
        identifiant = cree.get_json()["day_off"]["id"]

        liste = client.get("/api/days-off?start=2026-10-10&end=2026-10-14").get_json()
        self.assertEqual(len(liste["days_off"]), 1)
        self.assertEqual(len(liste["calendar"]), 5)
        self.assertEqual(liste["calendar"][2]["reason"], "Conges")

        modifie = client.patch(
            f"/api/days-off/{identifiant}", json={"end_day": "2026-10-16"},
            headers=self.write_headers(),
        )
        self.assertEqual(modifie.get_json()["day_off"]["days"], 5)

        supprime = client.delete(f"/api/days-off/{identifiant}", headers=self.write_headers())
        self.assertEqual(supprime.status_code, 200)

    def test_saisie_invalide(self):
        reponse = self.client().post(
            "/api/days-off", json={"title": "", "start_day": "2026-10-12"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)

    def test_import_des_feries(self):
        setting.set_value("holiday_country", "CI")
        reponse = self.client().post(
            "/api/days-off/holidays", json={"year": 2026}, headers=self.write_headers()
        )
        self.assertEqual(reponse.get_json()["added"], len(holidays.holidays_for("CI", 2026)))

    def test_import_sans_pays_refuse(self):
        setting.set_value("holiday_country", "")
        reponse = self.client().post(
            "/api/days-off/holidays", json={"year": 2026}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_reglage_jours_ouvres(self):
        client = self.client()
        bon = client.patch(
            "/api/settings", json={"work_days": "4,0,1"}, headers=self.write_headers()
        )
        self.assertEqual(bon.status_code, 200)
        self.assertEqual(setting.get("work_days"), "0,1,4")

        for valeur in ("", "7", "0,a"):
            mauvais = client.patch(
                "/api/settings", json={"work_days": valeur}, headers=self.write_headers()
            )
            self.assertEqual(mauvais.status_code, 400, valeur)

    def test_changer_de_pays_importe_aussitot(self):
        self.client().patch(
            "/api/settings", json={"holiday_country": "SN"}, headers=self.write_headers()
        )
        independance = date(date.today().year, 4, 4)
        titres = [e["title"] for e in day_off_model.list_between(independance, independance)]
        self.assertIn("Fête de l'Indépendance", titres)

    def test_le_plan_porte_le_motif(self):
        plan = self.client().get(f"/api/plan?date={SAMEDI.isoformat()}").get_json()
        self.assertEqual(plan["day_off"], "Week-end")
