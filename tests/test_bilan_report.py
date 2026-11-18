"""Bilan par periode, projets dans le bilan et rapport d'avancement d'un chef de projet.

Scenarios couverts : le bilan affiche est celui de la periode choisie (et non
le dernier genere), les bornes d'un mois, les mouvements d'un projet sur une
periode, et le rapport destine au chef de projet, qui ne doit montrer que ce
qui regarde ses projets.
"""
import json
from datetime import date, timedelta

from models import get_db, journal, setting
from models import phase as phase_model
from models import person as person_model
from models import project as project_model
from models import stats
from models import task as task_model
from services import agent, progress_report, review
from services.agent import AgentError
from tests.base import NomaiaTestCase


def enregistre_bilan(debut, fin, resume):
    """Insere un bilan sans passer par l'IA."""
    record = {"data": {}, "payload": {"resume": resume}}
    db = get_db()
    db.execute(
        "INSERT INTO review (period_start, period_end, payload_json) VALUES (?, ?, ?)",
        (debut.isoformat(), fin.isoformat(), json.dumps(record)),
    )
    db.commit()


class BornesMoisTest(NomaiaTestCase):
    """Un mois va du premier au dernier jour, changement d'annee compris."""

    def test_mois_en_cours(self):
        self.assertEqual(
            review.month_bounds(date(2026, 3, 15)), (date(2026, 3, 1), date(2026, 3, 31))
        )

    def test_mois_precedent_fevrier(self):
        self.assertEqual(
            review.month_bounds(date(2026, 3, 15), months_back=1),
            (date(2026, 2, 1), date(2026, 2, 28)),
        )

    def test_mois_precedent_change_d_annee(self):
        self.assertEqual(
            review.month_bounds(date(2026, 1, 10), months_back=1),
            (date(2025, 12, 1), date(2025, 12, 31)),
        )


class BilanParPeriodeTest(NomaiaTestCase):
    """Choisir une periode affiche son bilan, pas le dernier genere."""

    def test_bilan_de_la_periode_choisie(self):
        courante = review.period_bounds()
        precedente = review.period_bounds(weeks_back=1)
        enregistre_bilan(*precedente, "semaine derniere")
        enregistre_bilan(*courante, "semaine en cours")

        client = self.client()
        reponse = client.get("/api/review?weeks_back=1").get_json()
        self.assertEqual(reponse["review"]["payload"]["resume"], "semaine derniere")

    def test_periode_sans_bilan(self):
        enregistre_bilan(*review.period_bounds(), "semaine en cours")
        reponse = self.client().get("/api/review?weeks_back=2").get_json()
        self.assertIsNone(reponse["review"])

    def test_historique_et_relecture(self):
        enregistre_bilan(*review.period_bounds(weeks_back=1), "ancien")
        enregistre_bilan(*review.month_bounds(), "mensuel")

        client = self.client()
        historique = client.get("/api/reviews").get_json()["reviews"]
        self.assertEqual([item["resume"] for item in historique], ["mensuel", "ancien"])

        detail = client.get(f"/api/reviews/{historique[1]['id']}").get_json()
        self.assertEqual(detail["review"]["payload"]["resume"], "ancien")
        self.assertEqual(client.get("/api/reviews/999").status_code, 404)

    def test_stats_du_mois(self):
        debut, fin = review.month_bounds()
        donnees = self.client().get("/api/stats?months_back=0").get_json()
        self.assertEqual(
            (donnees["period_start"], donnees["period_end"]), (debut.isoformat(), fin.isoformat())
        )


class ProjetsDansLeBilanTest(NomaiaTestCase):
    """Le bilan voit ce qui a bouge sur chaque projet pendant la periode."""

    def test_mouvements_de_la_periode(self):
        projet = project_model.create_project("Acme")
        aujourd_hui = date.today().isoformat()
        fait = task_model.create_task({"title": "Parametrer la paie", "project_id": projet["id"]})
        task_model.toggle_task(fait["id"])
        task_model.create_task(
            {"title": "Ecart de TVA", "project_id": projet["id"], "kind": "anomaly", "severity": "critical"}
        )
        journal.add_entry("Go retenu pour le 5", project_id=projet["id"], kind="decision")

        activite = stats.project_activity(aujourd_hui, aujourd_hui)[projet["id"]]
        self.assertEqual(activite["tasks_done"], 1)
        self.assertEqual(activite["anomalies_opened"], 1)
        self.assertEqual(activite["register_entries"], 1)

        vue = review.projects_view(date.today(), date.today())
        self.assertEqual(vue[0]["name"], "Acme")
        self.assertEqual(vue[0]["anomalies_critical"], 1)

        prompt = review.build_prompt(review.gather(date.today(), date.today()))
        self.assertIn("PROJETS", prompt)
        self.assertIn("Acme", prompt)

    def test_projet_immobile_sans_mouvement(self):
        project_model.create_project("Delta")
        hier = (date.today() - timedelta(days=30)).isoformat()
        self.assertEqual(stats.project_activity(hier, hier), {})


class RapportChefDeProjetTest(NomaiaTestCase):
    """Le rapport couvre les projets du chef, et rien de ce qui reste interne."""

    def setUp(self):
        super().setUp()
        self.chef = person_model.create_person({"name": "Lucie Bernard", "role": "Chef de projet"})
        self.autre = person_model.create_person({"name": "Leo"})
        self.acme = project_model.create_project("Acme")
        self.nordik = project_model.create_project("Nordik")
        self.interne = project_model.create_project("Outils internes")
        project_model.update_project(self.acme["id"], {"manager_id": self.chef["id"]})
        project_model.update_project(self.nordik["id"], {"manager_id": self.chef["id"]})
        project_model.update_project(self.interne["id"], {"manager_id": self.autre["id"]})

    def test_projets_du_chef_seulement(self):
        rapport = progress_report.build(self.chef["id"])
        self.assertEqual([s["name"] for s in rapport["projects"]], ["Acme", "Nordik"])
        self.assertEqual(rapport["summary"]["projects"], 2)

    def test_contenu_interne_exclu(self):
        tache = task_model.create_task(
            {
                "title": "Migrer les tiers",
                "description": "SECRET interne",
                "project_id": self.acme["id"],
            }
        )
        journal.add_entry("NOTE interne", task_id=tache["id"], kind="work", minutes=45)
        self.complete_task(tache["id"], tracked_min=45)

        texte = json.dumps(progress_report.build(self.chef["id"]), ensure_ascii=False)
        self.assertIn("Migrer les tiers", texte)
        self.assertNotIn("SECRET", texte)
        self.assertNotIn("NOTE interne", texte)
        self.assertNotIn("tracked_min", texte)

    def test_anomalies_livrables_registre(self):
        hier = (date.today() - timedelta(days=1)).isoformat()
        projet = self.acme["id"]
        task_model.create_task({"title": "Bug mineur", "project_id": projet, "kind": "anomaly", "severity": "minor"})
        task_model.create_task({"title": "Bug bloquant", "project_id": projet, "kind": "anomaly", "severity": "critical"})
        task_model.create_task(
            {"title": "PV de recette", "project_id": projet, "kind": "deliverable", "deadline": hier}
        )
        journal.add_entry("Lot paie reporte en phase 2", project_id=projet, kind="scope")

        section = progress_report.build(self.chef["id"])["projects"][0]
        self.assertEqual([a["title"] for a in section["anomalies"]["open"]], ["Bug bloquant", "Bug mineur"])
        self.assertEqual(section["anomalies"]["by_severity"]["critical"], 1)
        self.assertTrue(section["deliverables"]["awaited"][0]["late"])
        self.assertEqual(section["register"][0]["kind"], "scope")

    def test_tache_terminee_hors_periode_ignoree(self):
        tache = task_model.create_task({"title": "Vieille tache", "project_id": self.acme["id"]})
        task_model.complete_on(tache["id"], (date.today() - timedelta(days=40)).isoformat())
        section = progress_report.build(self.chef["id"])["projects"][0]
        self.assertEqual(section["done"], [])

    def test_message_whatsapp(self):
        person_model.update_person(self.chef["id"], {"report_url": "https://claude.ai/artifact/abc"})
        rapport = progress_report.build(self.chef["id"])
        message = progress_report.as_whatsapp(rapport)
        self.assertTrue(message.startswith("Bonjour Lucie,"))
        self.assertIn("*Acme*", message)
        self.assertIn("https://claude.ai/artifact/abc", message)

    def test_message_resume_les_etapes_closes_en_masse(self):
        etapes = phase_model.apply_template(self.acme["id"])
        phase_model.update_phase(etapes[-1]["id"], {"status": "done"})
        message = progress_report.as_whatsapp(progress_report.build(self.chef["id"]))
        self.assertIn(f"{len(etapes)} étapes terminées, jusqu'à « {etapes[-1]['name']} »", message)

    def test_chef_sans_projet(self):
        sans = person_model.create_person({"name": "Marc"})
        with self.assertRaises(progress_report.ReportError):
            progress_report.build(sans["id"])

    def test_liste_des_chefs(self):
        noms = {chef["name"]: chef["projects"] for chef in progress_report.list_managers()}
        self.assertEqual(noms, {"Lucie Bernard": ["Acme", "Nordik"], "Leo": ["Outils internes"]})

    def test_api_interface(self):
        client = self.client()
        reponse = client.get(f"/api/progress-report?manager_id={self.chef['id']}&weeks_back=0")
        self.assertEqual(reponse.status_code, 200)
        self.assertIn("Acme", reponse.get_json()["message"])
        self.assertEqual(client.get("/api/progress-report").status_code, 400)
        self.assertEqual(client.get("/api/progress-report?manager_id=999").status_code, 400)


class ChefDeProjetSaisieTest(NomaiaTestCase):
    """Saisie du chef de projet et du lien de son tableau de bord."""

    def test_chef_inconnu_refuse(self):
        projet = project_model.create_project("Acme")
        with self.assertRaises(project_model.ProjectError):
            project_model.update_project(projet["id"], {"manager_id": 999})

    def test_retirer_le_chef(self):
        chef = person_model.create_person({"name": "Lucie"})
        projet = project_model.create_project("Acme")
        project_model.update_project(projet["id"], {"manager_id": chef["id"]})
        apres = project_model.update_project(projet["id"], {"manager_id": ""})
        self.assertIsNone(apres["manager_id"])

    def test_lien_non_https_refuse(self):
        chef = person_model.create_person({"name": "Lucie"})
        with self.assertRaises(person_model.PersonError):
            person_model.update_person(chef["id"], {"report_url": "javascript:alert(1)"})

    def test_fusion_garde_les_projets_suivis(self):
        garde = person_model.create_person({"name": "Lucie Bernard"})
        doublon = person_model.create_person({"name": "Lucie"})
        projet = project_model.create_project("Acme")
        project_model.update_project(projet["id"], {"manager_id": doublon["id"]})
        person_model.merge_people(garde["id"], doublon["id"])
        self.assertEqual(project_model.get_project(projet["id"])["manager_id"], garde["id"])

    def test_suppression_du_chef_detache_le_projet(self):
        chef = person_model.create_person({"name": "Lucie"})
        projet = project_model.create_project("Acme")
        project_model.update_project(projet["id"], {"manager_id": chef["id"]})
        person_model.delete_person(chef["id"])
        self.assertIsNone(project_model.get_project(projet["id"])["manager_id"])


class RapportSessionClaudeTest(NomaiaTestCase):
    """Les sessions Claude lisent le rapport et retiennent le lien publie."""

    def setUp(self):
        super().setUp()
        self.cle = setting.regenerate_agent_key()
        chef = person_model.create_person({"name": "Lucie Bernard"})
        projet = project_model.create_project("Acme")
        project_model.update_project(projet["id"], {"manager_id": chef["id"]})

    def test_sans_chef_liste_les_chefs(self):
        self.assertEqual(agent.progress_report()["managers"][0]["name"], "Lucie Bernard")

    def test_chef_inconnu_explique(self):
        with self.assertRaises(AgentError) as erreur:
            agent.progress_report("Inconnu")
        self.assertIn("Lucie Bernard", str(erreur.exception))

    def test_dates_incompletes_refusees(self):
        with self.assertRaises(AgentError):
            agent.progress_report("Lucie Bernard", start="2026-10-01")

    def test_api_agent_et_lien(self):
        client = self.app.test_client()
        entete = {"Authorization": "Bearer " + self.cle}
        reponse = client.get("/api/agent/progress-report?manager=Lucie%20Bernard", headers=entete)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["report"]["projects"][0]["name"], "Acme")

        reponse = client.post(
            "/api/agent/progress-report/url",
            json={"manager": "Lucie Bernard", "url": "https://claude.ai/artifact/xyz"},
            headers=entete,
        )
        self.assertEqual(reponse.get_json()["report_url"], "https://claude.ai/artifact/xyz")
        self.assertEqual(client.get("/api/agent/progress-report", headers=entete).status_code, 200)
        self.assertEqual(client.get("/api/agent/progress-report").status_code, 401)
