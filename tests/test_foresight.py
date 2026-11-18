"""Tests de la proactivite : echeances en danger, preparation, menage.

Une alerte anticipee n'a de valeur que si elle est juste et rare : juste,
parce que le temps libre est le vrai (reunions, conges, week-ends, temps deja
passe) ; rare, parce qu'une alerte repetee s'apprend a ignorer.
"""
from datetime import date, datetime, timedelta

from flask import g

from models import alert as alert_model
from models import capture as capture_model
from models import day_off as day_off_model
from models import event as event_model
from models import get_db
from models import person as person_model
from models import project as project_model
from models import setting
from models import task as task_model
from services import foresight, heartbeat, tools, workdays
from tests.base import NomaiaTestCase
from tests.test_heartbeat import FauxBridge

LUNDI = date(2026, 10, 5)
MARDI = date(2026, 10, 6)
VENDREDI = date(2026, 10, 2)

# Lundi 8 h : journee entiere devant soi, 510 min libres (8 h 30 - 18 h,
# pause d'une heure).
LUNDI_MATIN = datetime(2026, 10, 5, 8, 0)


class ForesightTestCase(NomaiaTestCase):
    """Semaine de lundi a vendredi, marge de planification nulle."""

    def setUp(self):
        super().setUp()
        setting.set_value("work_days", "0,1,2,3,4")
        setting.set_value("plan_buffer_pct", "0")
        g.pop(workdays._WEEKDAYS_KEY, None)

    @staticmethod
    def tache(titre, minutes, echeance=None, **extra):
        data = {"title": titre, "estimate_min": minutes, **extra}
        if echeance:
            data["deadline"] = echeance.isoformat()
        return task_model.create_task(data)

    @staticmethod
    def vieillir(table, ligne_id, jours, reference=LUNDI):
        """Recule la date de creation d'une ligne."""
        moment = (datetime.combine(reference, datetime.min.time()) - timedelta(days=jours))
        get_db().execute(
            f"UPDATE {table} SET created_at = ? WHERE id = ?",
            (moment.strftime("%Y-%m-%d %H:%M:%S"), ligne_id),
        )
        get_db().commit()


class EcheancesEnDangerTest(ForesightTestCase):
    """Travail restant compare au temps libre reel."""

    def test_tout_tient(self):
        self.tache("Courte", 60, MARDI)
        self.assertEqual(foresight.deadlines_at_risk(LUNDI_MATIN), [])

    def test_cumul_de_deux_taches_qui_tiennent_seules(self):
        self.tache("Rapport A", 300, LUNDI)
        self.tache("Rapport B", 300, LUNDI)

        risques = foresight.deadlines_at_risk(LUNDI_MATIN)
        self.assertEqual(len(risques), 1)
        self.assertEqual(risques[0]["work_min"], 600)
        self.assertEqual(risques[0]["free_min"], 510)
        self.assertEqual(risques[0]["deficit_min"], 90)

    def test_une_reunion_fait_basculer(self):
        self.tache("Livrable", 420, LUNDI)
        self.assertEqual(foresight.deadlines_at_risk(LUNDI_MATIN), [])

        event_model.create_event(
            {"title": "Comite", "starts_at": "2026-10-05 10:00", "ends_at": "2026-10-05 12:00"}
        )
        self.assertEqual(len(foresight.deadlines_at_risk(LUNDI_MATIN)), 1)

    def test_le_week_end_ne_compte_pas(self):
        # Vendredi 17 h, du lundi : 1 h vendredi + 510 min lundi.
        vendredi_soir = datetime(2026, 10, 2, 17, 0)
        self.tache("Dossier", 500, LUNDI)
        self.assertEqual(foresight.deadlines_at_risk(vendredi_soir), [])

        day_off_model.create_day_off({"title": "Conges", "start_day": LUNDI.isoformat()})
        risques = foresight.deadlines_at_risk(vendredi_soir)
        self.assertEqual(risques[0]["free_min"], 60)

    def test_le_temps_ecoule_aujourd_hui_n_est_plus_libre(self):
        self.tache("Analyse", 300, LUNDI)
        self.assertEqual(foresight.deadlines_at_risk(LUNDI_MATIN), [])
        self.assertEqual(len(foresight.deadlines_at_risk(datetime(2026, 10, 5, 15, 0))), 1)

    def test_le_temps_deja_chronometre_est_deduit(self):
        tache = self.tache("Migration", 600, LUNDI)
        self.assertEqual(len(foresight.deadlines_at_risk(LUNDI_MATIN)), 1)

        self.complete_task(tache["id"], 300)
        get_db().execute("UPDATE task SET status = 'todo' WHERE id = ?", (tache["id"],))
        get_db().commit()
        self.assertEqual(foresight.deadlines_at_risk(LUNDI_MATIN), [])

    def test_hors_horizon_ignore(self):
        setting.set_value("risk_horizon_days", "2")
        self.tache("Lointaine", 1440, LUNDI + timedelta(days=14))
        self.assertEqual(foresight.deadlines_at_risk(LUNDI_MATIN), [])

    def test_la_marge_compte(self):
        setting.set_value("plan_buffer_pct", "50")
        self.tache("Juste", 400, LUNDI)
        self.assertEqual(len(foresight.deadlines_at_risk(LUNDI_MATIN)), 1)

    def test_outil_du_chat(self):
        self.tache("Rapport A", 600, LUNDI)
        with self.app.test_request_context():
            resultat = tools.execute("get_risks", {})
        self.assertTrue(resultat["ok"])


class PreparationReunionTest(ForesightTestCase):
    """Le rappel dit ce qu'il faut avoir en tete."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")
        self.paul = person_model.create_person({"name": "Paul"})

    def test_projet_reconnu_dans_le_titre(self):
        self.tache("Recette paie", 60, LUNDI + timedelta(days=2), project_id=self.projet["id"])
        self.tache("Plus tard", 60, LUNDI + timedelta(days=20), project_id=self.projet["id"])

        contexte = foresight.meeting_context({"title": "Point alpha hebdo"}, LUNDI)
        self.assertEqual(contexte["project"], "ALPHA")
        self.assertEqual([t["title"] for t in contexte["due_soon"]], ["Recette paie"])

    def test_pas_de_faux_positif_dans_un_mot(self):
        contexte = foresight.meeting_context({"title": "Revue alphaex"}, LUNDI)
        self.assertIsNone(contexte["project"])

    def test_nom_trop_court_ignore(self):
        project_model.create_project("RH")
        contexte = foresight.meeting_context({"title": "Point RH"}, LUNDI)
        self.assertIsNone(contexte["project"])

    def test_interlocuteur_dans_le_titre(self):
        self.tache("Envoyer le devis", 30, person_id=self.paul["id"])
        person_model.create_waiting({"person_id": self.paul["id"], "subject": "les specs"})

        contexte = foresight.meeting_context({"title": "Call avec Paul"}, LUNDI)
        self.assertEqual([t["title"] for t in contexte["i_owe"]], ["Envoyer le devis"])
        self.assertEqual([w["subject"] for w in contexte["they_owe"]], ["les specs"])

        texte = heartbeat.reminder_text(
            {"title": "Call avec Paul", "starts_at": "2026-10-05 10:00",
             "ends_at": "2026-10-05 10:30"}
        )
        self.assertIn("Tu leur dois : Envoyer le devis (Paul)", texte)
        self.assertIn("On te doit : Paul - les specs", texte)

    def test_desactive_rappel_sobre(self):
        setting.set_value("foresight_enabled", "0")
        self.tache("Envoyer le devis", 30, person_id=self.paul["id"])
        texte = heartbeat.reminder_text(
            {"title": "Call Paul", "starts_at": "2026-10-05 10:00", "ends_at": "2026-10-05 10:30"}
        )
        self.assertNotIn("Tu leur dois", texte)


class MenageTest(ForesightTestCase):
    """Taches dormantes, validations oubliees, projets a l'arret."""

    def test_tache_dormante(self):
        vieille = self.tache("Vieille idee", 30)
        self.vieillir("task", vieille["id"], 30)
        commencee = self.tache("Commencee", 30)
        self.vieillir("task", commencee["id"], 30)
        self.complete_task(commencee["id"], 10)
        get_db().execute("UPDATE task SET status = 'todo' WHERE id = ?", (commencee["id"],))
        datee = self.tache("Datee", 30, LUNDI + timedelta(days=5))
        self.vieillir("task", datee["id"], 30)
        self.tache("Recente", 30)

        self.assertEqual(
            [t["title"] for t in foresight.dormant_tasks(LUNDI)], ["Vieille idee"]
        )

    def test_proposition_oubliee(self):
        capture = capture_model.create_capture(raw_text="facture client")
        vieille = capture_model.create_proposal(capture["id"], {"summary": "x", "confidence": 0.4})
        # Une nouvelle proposition sur la meme capture rejette l'ancienne : la
        # recente vient donc d'une autre capture.
        autre = capture_model.create_capture(raw_text="autre")
        recente = capture_model.create_proposal(autre["id"], {"summary": "y", "confidence": 0.4})
        self.vieillir("proposal", vieille if isinstance(vieille, int) else vieille["id"], 2,
                      reference=date.today())

        attente = foresight.forgotten_validations(datetime.now())
        identifiants = [p["id"] for p in attente["proposals"]]
        recente_id = recente if isinstance(recente, int) else recente["id"]
        self.assertEqual(len(identifiants), 1)
        self.assertNotIn(recente_id, identifiants)

    def test_question_sans_reponse(self):
        capture = capture_model.create_capture(raw_text="relance banque")
        capture_model.set_status(capture["id"], "waiting")
        capture_model.add_message(capture["id"], "nomaia", "Pour quand ?")
        get_db().execute(
            "UPDATE message SET created_at = datetime('now', 'localtime', '-2 days')"
        )
        get_db().execute(
            "UPDATE capture SET created_at = datetime('now', 'localtime', '-2 days')"
        )
        get_db().commit()

        self.assertEqual(len(foresight.forgotten_validations(datetime.now())["questions"]), 1)

    def test_projet_a_l_arret(self):
        dormant = project_model.create_project("Dormant")
        vivant = project_model.create_project("Vivant")
        fini = project_model.create_project("Fini")
        for projet in (dormant, fini):
            self.vieillir("project", projet["id"], 30)
        self.vieillir("project", vivant["id"], 30)

        ancienne = self.tache("Ancienne", 30, project_id=dormant["id"])
        self.vieillir("task", ancienne["id"], 20)
        self.tache("Fraiche", 30, project_id=vivant["id"])

        noms = [p["name"] for p in foresight.idle_projects(LUNDI + timedelta(days=0))]
        self.assertIn("Dormant", noms)
        self.assertNotIn("Vivant", noms)
        self.assertNotIn("Fini", noms)


class PointDuMatinTest(ForesightTestCase):
    """Les nouveaux blocs du point du matin et leur silence."""

    def test_echeance_en_danger_dans_le_point(self):
        aujourd_hui = date.today()
        setting.set_value("work_days", "0,1,2,3,4,5,6")
        g.pop(workdays._WEEKDAYS_KEY, None)
        self.tache("Enorme", 1440, aujourd_hui + timedelta(days=1))

        texte = heartbeat.morning_brief(aujourd_hui)
        self.assertIn("Echeances en danger", texte)

    def test_menage_une_fois_par_semaine(self):
        aujourd_hui = date.today()
        setting.set_value("work_days", "0,1,2,3,4,5,6")
        g.pop(workdays._WEEKDAYS_KEY, None)
        vieille = self.tache("Vieille idee", 30)
        self.vieillir("task", vieille["id"], 40, reference=aujourd_hui)

        self.assertIn("Menage de la semaine", heartbeat.morning_brief(aujourd_hui))
        second = heartbeat.morning_brief(aujourd_hui) or ""
        self.assertNotIn("Menage de la semaine", second)

    def test_desactive_aucun_bloc(self):
        aujourd_hui = date.today()
        setting.set_value("foresight_enabled", "0")
        setting.set_value("work_days", "0,1,2,3,4,5,6")
        g.pop(workdays._WEEKDAYS_KEY, None)
        self.tache("Enorme", 1440, aujourd_hui + timedelta(days=1))
        self.assertNotIn("Echeances en danger", heartbeat.morning_brief(aujourd_hui))


class AlerteImmediateTest(ForesightTestCase):
    """Une echeance proche qui bascule part une fois, au bon moment."""

    def setUp(self):
        super().setUp()
        self.bridge = FauxBridge()
        self.app.extensions["telegram"] = self.bridge
        self.beat = heartbeat.Heartbeat(self.app)
        setting.set_value("heartbeat_last_morning", LUNDI.isoformat())
        self.tache("Enorme", 1440, MARDI)

    def test_part_une_seule_fois(self):
        self.beat._check_foresight(datetime(2026, 10, 5, 10, 0))
        self.beat._check_foresight(datetime(2026, 10, 5, 11, 0))

        alertes = [m for m in self.bridge.messages if "Echeance en danger" in m]
        self.assertEqual(len(alertes), 1)
        self.assertIn("du demain", alertes[0])

    def test_pas_avant_le_point_du_matin(self):
        setting.set_value("heartbeat_last_morning", "")
        self.beat._check_foresight(datetime(2026, 10, 5, 10, 0))
        self.assertEqual(self.bridge.messages, [])

    def test_pas_hors_des_heures_de_travail(self):
        self.beat._check_foresight(datetime(2026, 10, 5, 20, 0))
        self.assertEqual(self.bridge.messages, [])

    def test_pas_un_jour_de_repos(self):
        day_off_model.create_day_off({"title": "Pont", "start_day": LUNDI.isoformat()})
        self.beat._check_foresight(datetime(2026, 10, 5, 10, 0))
        self.assertEqual(self.bridge.messages, [])

    def test_deja_annonce_le_matin(self):
        alert_model.mark_sent(
            [heartbeat.urgent_key(t, MARDI.isoformat()) for t in task_model.list_tasks("todo")]
        )
        self.beat._check_foresight(datetime(2026, 10, 5, 10, 0))
        self.assertEqual(self.bridge.messages, [])

    def test_echeance_lointaine_pas_d_alerte_immediate(self):
        task_model.delete_task(task_model.list_tasks("todo")[0]["id"])
        # Mercredi : en danger, mais pas pour aujourd'hui ni demain.
        self.tache("Grosse A", 1440, LUNDI + timedelta(days=2))
        self.tache("Grosse B", 1440, LUNDI + timedelta(days=2))
        self.assertTrue(foresight.deadlines_at_risk(datetime(2026, 10, 5, 10, 0)))
        self.beat._check_foresight(datetime(2026, 10, 5, 10, 0))
        self.assertEqual(self.bridge.messages, [])


class ApiPlanTest(ForesightTestCase):
    """Le plan du jour porte les echeances en danger."""

    def test_le_plan_porte_les_risques(self):
        setting.set_value("work_days", "0,1,2,3,4,5,6")
        g.pop(workdays._WEEKDAYS_KEY, None)
        self.tache("Enorme", 1440, date.today() + timedelta(days=1))
        plan = self.client().get("/api/plan").get_json()
        self.assertEqual(len(plan["risks"]), 1)
