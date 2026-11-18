"""« Ou en etais-je ? » (lot 4) : synthese de reprise d'un projet, resume IA source."""
from datetime import date
from unittest import mock

from models import get_db
from services import project_resume
from tests.base import NomaiaTestCase

TODAY = date(2026, 11, 6)


class SyntheseTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.pid = db.execute("INSERT INTO project (name) VALUES ('ALPHA')").lastrowid

        def task(title, **cols):
            cols = {"title": title, "project_id": self.pid, **cols}
            names = ", ".join(cols)
            return db.execute(f"INSERT INTO task ({names}) VALUES ({', '.join('?' * len(cols))})",
                              list(cols.values())).lastrowid

        self.urgent = task("Corriger l'export FNE", priority=1, deadline="2026-11-05")
        self.started = task("Paramétrer la paie", priority=3, progress_pct=40)
        self.blocked = task("Importer les soldes", blocked=1)
        self.client_side = task("Recevoir le plan de comptes", side="client")
        self.anomaly = task("Écart de TVA", kind="anomaly", severity="critical")
        self.deliverable = task("PV de recette", kind="deliverable", deadline="2026-11-20")
        self.done = task("Installer les modules", status="done", done_at="2026-11-04 17:00:00")
        db.execute("INSERT INTO note (task_id, project_id, content, kind, created_at) VALUES "
                   "(?, ?, 'Valorisation FIFO retenue', 'decision', '2026-11-03 10:00:00')", (self.started, self.pid))
        db.execute("INSERT INTO note (task_id, project_id, content, kind, source, created_at) VALUES "
                   "(?, ?, 'Règles de paie saisies à 40 %', 'progress', 'claude', '2026-11-05 16:00:00')",
                   (self.started, self.pid))
        db.commit()

    def test_synthese_de_reprise(self):
        data = project_resume.resume(self.pid, TODAY)
        self.assertEqual(data["project"]["name"], "ALPHA")
        # Derniere action : l'entree la plus recente (avancement d'hier).
        self.assertEqual(data["last_action"]["text"], "Règles de paie saisies à 40 %")
        self.assertEqual(data["idle_days"], 1)
        # Prochaine etape : la tache deja entamee passe devant l'urgente non commencee.
        self.assertEqual(data["next_step"]["id"], self.started)
        self.assertEqual({t["id"] for t in data["blocked"]}, {self.blocked, self.client_side})
        self.assertEqual([t["id"] for t in data["anomalies"]], [self.anomaly])
        self.assertEqual([t["id"] for t in data["deliverables"]], [self.deliverable])
        self.assertEqual([d["content"] for d in data["decisions"]], ["Valorisation FIFO retenue"])
        self.assertEqual(len(data["claude_sessions"]), 1)
        # Chronologie : du plus recent au plus ancien, taches terminees comprises.
        dates = [item["at"] for item in data["timeline"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertIn(f"T{self.done}", [item["ref"] for item in data["timeline"]])

    def test_prochaine_etape_ignore_bloquees_et_cote_client(self):
        db = get_db()
        db.execute("UPDATE task SET status = 'done' WHERE id IN (?, ?, ?)", (self.started, self.urgent, self.anomaly))
        db.commit()
        # Ne reste que du bloque, du cote client ou un livrable attendu : rien a faire de notre cote.
        self.assertIsNone(project_resume.resume(self.pid, TODAY)["next_step"])

    def test_projet_vide_et_inconnu(self):
        vide = get_db().execute("INSERT INTO project (name) VALUES ('Vide')").lastrowid
        get_db().commit()
        data = project_resume.resume(vide, TODAY)
        self.assertIsNone(data["last_action"])
        self.assertIsNone(data["next_step"])
        self.assertEqual(data["timeline"], [])
        self.assertIsNone(project_resume.resume(99999, TODAY))

    def test_api(self):
        client = self.client()
        data = client.get(f"/api/projects/{self.pid}/resume").get_json()
        self.assertEqual(data["project"]["id"], self.pid)
        self.assertEqual(client.get("/api/projects/99999/resume").status_code, 404)

    def test_resume_ia_retire_les_sources_inventees(self):
        answer = {
            "resume": f"La paie avance [T{self.started}] et une décision est actée [J1] ; rien sur [T99999].",
            "prochaine_action": "Finir la paie.",
            "sources": [f"T{self.started}", "T99999"],
        }
        with mock.patch.object(project_resume.llm, "ask", return_value=answer) as ask:
            result = project_resume.summarize(self.pid, TODAY)
        self.assertNotIn("T99999", result["summary"])
        self.assertNotIn("T99999", result["sources"])
        self.assertIn(f"T{self.started}", result["sources"])
        self.assertEqual(result["next_action"], "Finir la paie.")
        # Le modele ne recoit que la synthese factuelle, pas de consigne inventee.
        self.assertIn("Valorisation FIFO retenue", ask.call_args.args[0])

    def test_resume_ia_indisponible(self):
        from services.llm import LLMError

        with mock.patch.object(project_resume.llm, "ask", side_effect=LLMError("hors ligne")):
            reponse = self.client().post(f"/api/projects/{self.pid}/resume/summary", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 503)


class PanneauFicheProjetTest(NomaiaTestCase):
    def test_fiche_projet_porte_le_panneau(self):
        pid = get_db().execute("INSERT INTO project (name) VALUES ('Panneau')").lastrowid
        get_db().commit()
        html = self.client().get(f"/projets/{pid}").get_data(as_text=True)
        for marqueur in ('id="rs-panel"', 'data-tab="chrono"', 'id="rs-ai"', "js/resume.js"):
            self.assertIn(marqueur, html)
