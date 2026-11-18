"""Page Bilan : temps par projet, taches a rattacher et ordre des sections.

Scenarios couverts : une sous-tache sans projet compte pour le projet de sa
parente, la barre « Sans projet » se detaille en taches a rattacher, le
rattachement par l'API vide ce detail, le journal des sessions Claude porte
la couleur du projet, et la page presente ses sections dans l'ordre retenu.
"""
from datetime import date, datetime

from models import get_db, journal, stats
from models import project as project_model
from models import task as task_model
from services import review
from tests.base import NomaiaTestCase


def chronometre(task_id, minutes):
    """Ajoute une session terminee aujourd'hui sur une tache."""
    moment = datetime.now().replace(microsecond=0).isoformat(sep=" ")
    db = get_db()
    db.execute(
        "INSERT INTO session (task_id, started_at, ended_at, duration_min) VALUES (?, ?, ?, ?)",
        (task_id, moment, moment, minutes),
    )
    db.commit()


def aujourd_hui():
    """Bornes ISO d'une periode reduite a aujourd'hui."""
    jour = date.today().isoformat()
    return jour, jour


class TempsParProjetTest(NomaiaTestCase):
    """Le temps d'une sous-tache sans projet revient au projet de sa parente."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("Acme")
        self.parente = task_model.create_task({"title": "Paie", "project_id": self.projet["id"]})
        self.sous_tache = task_model.create_task(
            {"title": "Bulletins", "parent_id": self.parente["id"]}
        )
        self.orpheline = task_model.create_task({"title": "Veille Odoo"})

    def test_sous_tache_comptee_pour_le_projet_du_parent(self):
        chronometre(self.parente["id"], 30)
        chronometre(self.sous_tache["id"], 45)

        lignes = stats.tracked_by_project(*aujourd_hui())

        self.assertEqual(
            [(ligne["project_id"], ligne["project"], ligne["minutes"]) for ligne in lignes],
            [(self.projet["id"], "Acme", 75)],
        )

    def test_sans_projet_porte_un_identifiant_nul(self):
        chronometre(self.orpheline["id"], 20)

        lignes = stats.tracked_by_project(*aujourd_hui())

        self.assertEqual(lignes[0]["project"], "Sans projet")
        self.assertIsNone(lignes[0]["project_id"])

    def test_taches_a_rattacher(self):
        sous_orpheline = task_model.create_task(
            {"title": "Note de veille", "parent_id": self.orpheline["id"]}
        )
        chronometre(self.sous_tache["id"], 45)
        chronometre(self.orpheline["id"], 20)
        chronometre(self.orpheline["id"], 15)
        chronometre(sous_orpheline["id"], 10)

        a_rattacher = stats.unassigned_tracked_tasks(*aujourd_hui())

        self.assertEqual(
            a_rattacher,
            [
                {"id": self.orpheline["id"], "title": "Veille Odoo", "minutes": 35},
                {"id": sous_orpheline["id"], "title": "Note de veille", "minutes": 10},
            ],
        )
        sans_projet = [
            ligne for ligne in stats.tracked_by_project(*aujourd_hui())
            if ligne["project_id"] is None
        ]
        self.assertEqual(sans_projet[0]["minutes"], sum(t["minutes"] for t in a_rattacher))

    def test_session_hors_periode_ignoree(self):
        db = get_db()
        db.execute(
            "INSERT INTO session (task_id, started_at, ended_at, duration_min)"
            " VALUES (?, '2020-01-06 09:00:00', '2020-01-06 10:00:00', 60)",
            (self.orpheline["id"],),
        )
        db.commit()

        self.assertEqual(stats.unassigned_tracked_tasks(*aujourd_hui()), [])

    def test_rattachement_par_l_api(self):
        chronometre(self.orpheline["id"], 20)
        client = self.client()

        donnees = client.get("/api/stats").get_json()
        self.assertEqual([t["id"] for t in donnees["unassigned_tasks"]], [self.orpheline["id"]])

        reponse = client.patch(
            f"/api/tasks/{self.orpheline['id']}",
            json={"project_id": self.projet["id"]},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)

        donnees = client.get("/api/stats").get_json()
        self.assertEqual(donnees["unassigned_tasks"], [])
        self.assertEqual(
            [ligne["project"] for ligne in donnees["tracked_by_project"]], ["Acme"]
        )


class JournalSessionsClaudeTest(NomaiaTestCase):
    """Le journal du bilan suit le projet actuel de la tache."""

    def test_couleur_du_projet_apres_rattachement(self):
        tache = task_model.create_task({"title": "Corriger l'export"})
        journal.add_entry("Export corrige", task_id=tache["id"], kind="work", minutes=30, source="claude")
        projet = project_model.create_project("Nordik", "#ff8800")
        task_model.update_task(tache["id"], {"project_id": projet["id"]})

        debut, fin = review.period_bounds()
        entree = review.gather(debut, fin)["work_log"][0]

        self.assertEqual(entree["project_name"], "Nordik")
        self.assertEqual(entree["project_color"], "#ff8800")
        self.assertEqual(entree["task_title"], "Corriger l'export")


class OrdreDesSectionsTest(NomaiaTestCase):
    """Chiffres, graphiques, notes, projets, analyse, rapport, historique, journal."""

    def test_ordre_des_sections(self):
        page = self.client().get("/bilan").get_data(as_text=True)

        reperes = [
            'id="bl-stats"',
            'id="bl-charts"',
            'id="bl-notes"',
            'id="bl-projects"',
            'id="bl-review"',
            'id="rp-manager"',
            'id="bl-history"',
            'id="bl-worklog"',
        ]
        positions = [page.index(repere) for repere in reperes]
        self.assertEqual(positions, sorted(positions))

    def test_journal_hors_impression(self):
        page = self.client().get("/bilan").get_data(as_text=True)
        self.assertIn('<section class="no-print" id="bl-worklog">', page)
