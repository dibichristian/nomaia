"""Page des comptes rendus : liste, editeur et bouton de l'agenda."""
from pathlib import Path

from models import get_db
from models import minutes as minutes_model
from tests.base import NomaiaTestCase

STATIC = Path(__file__).resolve().parent.parent / "static"


class PageComptesRendusTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.pid = db.execute("INSERT INTO project (name, color) VALUES ('ALPHA', '#22d3ee')").lastrowid
        db.commit()
        self.minutes = minutes_model.create(
            {"title": "Point hebdo", "held_on": "2026-11-02", "kind": "training", "project_id": self.pid}
        )

    def assert_page(self, url):
        reponse = self.client().get(url)
        self.assertEqual(reponse.status_code, 200)
        html = reponse.get_data(as_text=True)
        for marqueur in ("css/pages/minutes.css", "js/minutes.js", 'id="mn-root"', 'id="mn-list-view"',
                         'id="mn-editor-view"', 'id="mn-new-modal"', 'id="mn-delete-modal"'):
            self.assertIn(marqueur, html)
        return html

    def test_liste_repond_avec_ses_ressources(self):
        html = self.assert_page("/comptes-rendus")
        self.assertIn('data-minutes-id=""', html)
        self.assertIn("Comptes rendus", html)
        # Le projet est propose au filtre et a la creation.
        self.assertIn(f'<option value="{self.pid}">ALPHA</option>', html)

    def test_editeur_repond_avec_l_identifiant(self):
        html = self.assert_page(f"/comptes-rendus/{self.minutes['id']}")
        self.assertIn(f'data-minutes-id="{self.minutes["id"]}"', html)
        for section in ("Participants", "Objectifs", "Points discutés", "Sujets enseignés", "Points à revoir",
                        "Décisions", "Actions", "Demandes du client", "Valider le compte rendu", "Imprimer",
                        "Pièces jointes", 'id="mn-attachments"', "js/attachments.js"):
            self.assertIn(section, html)

    def test_page_inaccessible_sans_cle(self):
        reponse = self.app.test_client().get("/comptes-rendus")
        self.assertNotEqual(reponse.status_code, 200)

    def test_impression_masque_la_coquille(self):
        css = (STATIC / "css" / "pages" / "minutes.css").read_text(encoding="utf-8")
        impression = css[css.index("@media print"):]
        for selecteur in (".sidebar", ".topbar", "#main-nav", "button"):
            self.assertIn(selecteur, impression)

    def test_agenda_propose_le_compte_rendu(self):
        script = (STATIC / "js" / "agenda.js").read_text(encoding="utf-8")
        self.assertIn('"/api/minutes"', script)
        self.assertIn('"/comptes-rendus/"', script)
        self.assertIn("event_id: event.id", script)
