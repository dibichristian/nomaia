"""Tests du tableau de bord des projets et de la fiche projet.

L'avancement doit refleter le travail reel (pondere par les estimations,
etapes comprises, avancement declare inclus) et les signaux doivent pointer
ce qui merite attention, sans bruit sur un projet termine.
"""
from datetime import date, timedelta

from models import event as event_model
from models import get_db, journal, setting
from models import person as person_model
from models import project as project_model
from models import task as task_model
from services import project_board
from tests.base import NomaiaTestCase


class AvancementTest(NomaiaTestCase):
    """Calcul de l'avancement et du reste a faire."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")

    def _carte(self):
        return project_board.overview()["projects"][0]

    def _tache(self, titre, estimation, **extra):
        return task_model.create_task(
            {"title": titre, "estimate_min": estimation, "project_id": self.projet["id"], **extra}
        )

    def test_pondere_par_les_estimations(self):
        grosse = self._tache("Grosse", 180)
        self._tache("Petite", 60)
        task_model.toggle_task(grosse["id"])
        self.assertEqual(self._carte()["progress_pct"], 75)
        self.assertEqual(self._carte()["remaining_min"], 60)

    def test_avancement_declare_compte(self):
        tache = self._tache("Migration", 100)
        task_model.update_task(tache["id"], {"progress_pct": 40})
        self.assertEqual(self._carte()["progress_pct"], 40)
        self.assertEqual(self._carte()["remaining_min"], 60)

    def test_tache_detaillee_mesuree_par_ses_etapes(self):
        parent = self._tache("Parent", 600)
        etape = self._tache("Etape", 60, parent_id=parent["id"])
        self._tache("Etape 2", 60, parent_id=parent["id"])
        task_model.toggle_task(etape["id"])
        self.assertEqual(self._carte()["progress_pct"], 50)

    def test_projet_vide(self):
        self.assertEqual(self._carte()["progress_pct"], 0)


class SignauxTest(NomaiaTestCase):
    """Ce qui merite attention, et seulement cela."""

    def setUp(self):
        super().setUp()
        self.today = date.today()

    def _signaux(self, projet):
        cartes = {carte["id"]: carte for carte in project_board.overview()["projects"]}
        return [signal["label"] for signal in cartes[projet["id"]]["signals"]]

    def test_echeance_depassee(self):
        projet = project_model.create_project(
            "ORION", deadline=(self.today - timedelta(days=3)).isoformat()
        )
        task_model.create_task({"title": "Livrer", "project_id": projet["id"]})
        self.assertIn("échéance dépassée de 3 j", self._signaux(projet))

    def test_taches_en_retard(self):
        projet = project_model.create_project("ORION")
        task_model.create_task({
            "title": "Livrer", "project_id": projet["id"],
            "deadline": (self.today - timedelta(days=1)).isoformat(),
        })
        self.assertIn("1 tâche(s) en retard", self._signaux(projet))
        self.assertEqual(project_board.overview()["kpis"]["at_risk"], 1)

    def test_projet_a_l_arret(self):
        setting.set_value("project_idle_days", "14")
        projet = project_model.create_project("ORION")
        tache = task_model.create_task({"title": "Livrer", "project_id": projet["id"]})
        ancien = (self.today - timedelta(days=30)).isoformat() + " 09:00:00"
        get_db().execute("UPDATE task SET created_at = ? WHERE id = ?", (ancien, tache["id"]))
        get_db().commit()
        self.assertTrue(any("à l'arrêt" in label for label in self._signaux(projet)))

    def test_projet_termine_silencieux(self):
        projet = project_model.create_project(
            "ORION", deadline=(self.today - timedelta(days=3)).isoformat()
        )
        task_model.create_task({"title": "Livrer", "project_id": projet["id"]})
        project_model.update_project(projet["id"], {"status": "done"})
        self.assertEqual(self._signaux(projet), [])


class FicheTest(NomaiaTestCase):
    """Fiche complete d'un projet."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")
        self.today = date.today()

    def test_taches_reparties(self):
        task_model.create_task({
            "title": "En retard", "project_id": self.projet["id"],
            "deadline": (self.today - timedelta(days=2)).isoformat(),
        })
        task_model.create_task({"title": "A faire", "project_id": self.projet["id"]})
        faite = task_model.create_task({"title": "Faite", "project_id": self.projet["id"]})
        task_model.toggle_task(faite["id"])

        taches = project_board.detail(self.projet["id"])["tasks"]
        self.assertEqual([t["title"] for t in taches["late"]], ["En retard"])
        self.assertEqual([t["title"] for t in taches["open"]], ["A faire"])
        self.assertEqual([t["title"] for t in taches["done"]], ["Faite"])

    def test_journal_regroupe_projet_et_taches(self):
        tache = task_model.create_task({"title": "Migration", "project_id": self.projet["id"]})
        journal.add_entry("Note de tache", task_id=tache["id"])
        journal.add_entry("Decision projet", project_id=self.projet["id"])

        contenus = [e["content"] for e in project_board.detail(self.projet["id"])["journal"]]
        self.assertEqual(sorted(contenus), ["Decision projet", "Note de tache"])

    def test_contacts_attentes_et_rendez_vous(self):
        paul = person_model.create_person({"name": "Paul", "project_id": self.projet["id"]})
        person_model.create_waiting({"person_id": paul["id"], "subject": "Les specs"})
        demain = self.today + timedelta(days=1)
        event_model.create_event({
            "title": "Point ALPHA", "project_id": self.projet["id"],
            "starts_at": f"{demain.isoformat()} 10:00", "ends_at": f"{demain.isoformat()} 11:00",
        })

        fiche = project_board.detail(self.projet["id"])
        self.assertEqual([p["name"] for p in fiche["people"]], ["Paul"])
        self.assertEqual([w["subject"] for w in fiche["waiting"]], ["Les specs"])
        self.assertEqual([e["title"] for e in fiche["events"]], ["Point ALPHA"])

    def test_activite_par_semaine(self):
        tache = task_model.create_task({"title": "Faite", "project_id": self.projet["id"]})
        task_model.toggle_task(tache["id"])
        activite = project_board.detail(self.projet["id"])["activity"]
        self.assertEqual(sum(semaine["done"] for semaine in activite), 1)

    def test_projet_inconnu(self):
        self.assertIsNone(project_board.detail(999))


class ProjetApiTest(NomaiaTestCase):
    """Routes et pages."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")

    def test_tableau_de_bord(self):
        corps = self.client().get("/api/projects/board").get_json()
        self.assertEqual(corps["projects"][0]["name"], "ALPHA")
        self.assertIn("kpis", corps)

    def test_fiche_et_404(self):
        self.assertEqual(self.client().get(f"/api/projects/{self.projet['id']}/detail").status_code, 200)
        self.assertEqual(self.client().get("/api/projects/999/detail").status_code, 404)

    def test_commentaire_ajoute_puis_supprime(self):
        client = self.client()
        cree = client.post(
            f"/api/projects/{self.projet['id']}/journal",
            json={"content": "Go client recu"},
            headers=self.write_headers(),
        )
        self.assertEqual(cree.status_code, 201)
        entree_id = cree.get_json()["id"]

        supprime = client.delete(
            f"/api/projects/{self.projet['id']}/journal/{entree_id}", headers=self.write_headers()
        )
        self.assertEqual(supprime.status_code, 200)
        self.assertEqual(journal.list_for_project(self.projet["id"]), [])

    def test_commentaire_vide_refuse(self):
        reponse = self.client().post(
            f"/api/projects/{self.projet['id']}/journal", json={"content": ""}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_pages(self):
        client = self.client()
        self.assertEqual(client.get("/projets").status_code, 200)
        page = client.get(f"/projets/{self.projet['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"task-detail", page.data)
        self.assertEqual(client.get("/projets/999").status_code, 404)
