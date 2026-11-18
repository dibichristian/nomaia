"""Tests du pilotage par etapes : gabarit, avancement pondere, jalons, statuts.

Regles de pilotage : avancement
global = somme des poids multiplies par l'avancement des etapes, etape en
cours = premiere en cours ou bloquee, dernier jalon = derniere terminee.
"""
from datetime import date, timedelta

from models import journal, setting
from models import phase as phase_model
from models import project as project_model
from models import task as task_model
from models.task import ValidationError
from services import agent, project_board, tools
from services.agent import AgentError
from tests.base import NomaiaTestCase


class GabaritTest(NomaiaTestCase):
    """Creation des etapes standard."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ZENTRO")

    def test_apply_template_cree_les_etapes_du_gabarit(self):
        etapes = phase_model.apply_template(self.projet["id"])
        self.assertEqual(len(etapes), 11)
        self.assertEqual(sum(etape["weight"] for etape in etapes), 100)
        self.assertEqual(etapes[0]["name"], "Recueil du besoin")
        self.assertEqual(etapes[-1]["stage"], "Support")

    def test_apply_template_refuse_projet_deja_decoupe(self):
        phase_model.create_phase(self.projet["id"], {"name": "Cadrage"})
        with self.assertRaises(ValidationError):
            phase_model.apply_template(self.projet["id"])

    def test_done_date_posee_puis_retiree(self):
        etape = phase_model.create_phase(self.projet["id"], {"name": "Cadrage"})
        etape = phase_model.update_phase(etape["id"], {"status": "done"})
        self.assertEqual(etape["done_date"], date.today().isoformat())
        etape = phase_model.update_phase(etape["id"], {"status": "doing"})
        self.assertIsNone(etape["done_date"])

    def test_delete_phase_garde_les_taches(self):
        etape = phase_model.create_phase(self.projet["id"], {"name": "Cadrage"})
        tache = task_model.create_task(
            {"title": "Recueil", "project_id": self.projet["id"], "phase_id": etape["id"]}
        )
        phase_model.delete_phase(etape["id"])
        self.assertIsNone(task_model.get_task(tache["id"])["phase_id"])


class CoherenceTacheTest(NomaiaTestCase):
    """Une tache ne peut pointer que vers une etape de son projet."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ZENTRO")
        self.autre = project_model.create_project("VORTEX")
        self.etape = phase_model.create_phase(self.projet["id"], {"name": "Cadrage"})

    def test_etape_d_un_autre_projet_refusee(self):
        with self.assertRaises(ValidationError):
            task_model.create_task(
                {"title": "Hors sujet", "project_id": self.autre["id"], "phase_id": self.etape["id"]}
            )

    def test_changement_de_projet_retire_l_etape(self):
        tache = task_model.create_task(
            {"title": "Recueil", "project_id": self.projet["id"], "phase_id": self.etape["id"]}
        )
        tache = task_model.update_task(tache["id"], {"project_id": self.autre["id"]})
        self.assertIsNone(tache["phase_id"])

    def test_side_invalide_refuse(self):
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "X", "side": "fournisseur"})


class PilotageTest(NomaiaTestCase):
    """Avancement, etape en cours, jalons et statuts derives."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("Atlas")
        self.a = phase_model.create_phase(self.projet["id"], {"name": "A", "weight": 20})
        self.b = phase_model.create_phase(self.projet["id"], {"name": "B", "weight": 30})
        self.c = phase_model.create_phase(self.projet["id"], {"name": "C", "weight": 50})

    def _carte(self):
        return project_board.overview()["projects"][0]

    def _tache(self, titre, etape, **extra):
        return task_model.create_task(
            {"title": titre, "project_id": self.projet["id"], "phase_id": etape["id"], **extra}
        )

    def test_avancement_pondere_par_les_etapes(self):
        phase_model.update_phase(self.a["id"], {"status": "done"})
        phase_model.update_phase(self.b["id"], {"progress_pct": 50})
        # 20 x 100 + 30 x 50 + 50 x 0 = 3500 / 100
        self.assertEqual(self._carte()["progress_pct"], 35)

    def test_avancement_d_etape_vient_des_taches(self):
        fait = self._tache("Fait", self.b, estimate_min=60)
        self._tache("Reste", self.b, estimate_min=60)
        task_model.toggle_task(fait["id"])
        # La saisie manuelle est ignoree des que l'etape a des taches.
        phase_model.update_phase(self.b["id"], {"progress_pct": 90})
        etape = self._carte()["phases"][1]
        self.assertEqual(etape["progress_pct"], 50)
        self.assertEqual(etape["status"], "doing")

    def test_jalons(self):
        hier = (date.today() - timedelta(days=1)).isoformat()
        phase_model.update_phase(self.a["id"], {"status": "done"})
        phase_model.update_phase(self.b["id"], {"planned_date": hier})
        carte = self._carte()
        self.assertEqual(carte["last_milestone"]["name"], "A")
        self.assertEqual(carte["current_phase"]["name"], "B")
        self.assertEqual(carte["next_milestone"]["days_left"], -1)
        self.assertTrue(any("dépassé" in signal["label"] for signal in carte["signals"]))

    def test_etape_bloquee_prioritaire(self):
        phase_model.update_phase(self.c["id"], {"status": "blocked"})
        carte = self._carte()
        self.assertEqual(carte["current_phase"]["name"], "C")
        self.assertEqual(carte["signals"][0]["level"], "danger")

    def test_statuts_derives(self):
        hier = (date.today() - timedelta(days=1)).isoformat()
        self._tache("A faire", self.a)
        self._tache("En cours", self.a, progress_pct=30)
        self._tache("En retard", self.a, deadline=hier)
        self._tache("Bloquee", self.a, blocked=True, deadline=hier)
        termine = self._tache("Terminee", self.a)
        task_model.toggle_task(termine["id"])
        self._tache("Client", self.a, side="client")

        carte = self._carte()
        self.assertEqual(
            carte["task_states"], {"done": 1, "doing": 1, "todo": 2, "late": 1, "blocked": 1}
        )
        self.assertEqual(carte["blocked_count"], 1)
        self.assertEqual(carte["waiting_client"], 1)

    def test_sous_tache_herite_de_l_etape(self):
        parent = self._tache("Parent", self.b, estimate_min=120)
        task_model.create_task(
            {"title": "Sous", "project_id": self.projet["id"], "parent_id": parent["id"]}
        )
        etape = self._carte()["phases"][1]
        self.assertEqual(etape["task_total"], 1)

    def test_fiche_range_les_taches_par_etape(self):
        self._tache("Dans A", self.a)
        task_model.create_task({"title": "Libre", "project_id": self.projet["id"]})
        fiche = project_board.detail(self.projet["id"])
        self.assertEqual([t["title"] for t in fiche["project"]["phases"][0]["tasks"]], ["Dans A"])
        self.assertEqual([t["title"] for t in fiche["tasks"]["unphased"]], ["Libre"])

    def test_projet_sans_etape_garde_l_ancien_calcul(self):
        autre = project_model.create_project("Zeta")
        tache = task_model.create_task({"title": "T", "project_id": autre["id"], "estimate_min": 60})
        task_model.toggle_task(tache["id"])
        cartes = {carte["id"]: carte for carte in project_board.overview()["projects"]}
        self.assertEqual(cartes[autre["id"]]["progress_pct"], 100)
        self.assertEqual(cartes[autre["id"]]["phases"], [])


class ApiEtapesTest(NomaiaTestCase):
    """Routes HTTP des etapes."""

    def test_cycle_complet(self):
        projet = project_model.create_project("Acme")
        client = self.client()
        base = f"/api/projects/{projet['id']}/phases"

        reponse = client.post(base + "/template", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(len(reponse.get_json()["phases"]), 11)

        reponse = client.post(base + "/template", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 400)

        etape_id = client.get(base).get_json()["phases"][0]["id"]
        reponse = client.patch(
            f"{base}/{etape_id}", json={"status": "blocked"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.get_json()["status"], "blocked")

        autre = project_model.create_project("Autre")
        reponse = client.delete(
            f"/api/projects/{autre['id']}/phases/{etape_id}", headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 404)

        reponse = client.delete(f"{base}/{etape_id}", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 200)

    def test_meteo_du_projet(self):
        projet = project_model.create_project("Nordik")
        reponse = self.client().patch(
            f"/api/projects/{projet['id']}",
            json={"weather": "cloudy", "weather_note": "Plan comptable attendu"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["weather"], "cloudy")
        reponse = self.client().patch(
            f"/api/projects/{projet['id']}", json={"weather": "neige"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)


class PassageEtapesTest(NomaiaTestCase):
    """Terminer une etape clot les precedentes ; chat et sessions Claude le font aussi."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("Atlas")
        self.etapes = phase_model.apply_template(self.projet["id"])

    def _statuts(self):
        return [etape["status"] for etape in phase_model.list_phases(self.projet["id"])]

    def test_terminer_la_cinquieme_clot_les_quatre_premieres(self):
        phase_model.update_phase(self.etapes[1]["id"], {"status": "done", "done_date": "2026-09-01"})
        etape = phase_model.update_phase(self.etapes[4]["id"], {"status": "done", "done_date": "2026-10-20"})

        # La 2 avait deja clos la 1 : seules la 3 et la 4 restaient ouvertes.
        self.assertEqual(etape["cascaded"], 2)
        self.assertEqual(self._statuts()[:5], ["done"] * 5)
        self.assertEqual(self._statuts()[5:], ["todo"] * 6)
        dates = [e["done_date"] for e in phase_model.list_phases(self.projet["id"])[:4]]
        self.assertEqual(dates, ["2026-09-01", "2026-09-01", "2026-10-20", "2026-10-20"])

    def test_rouvrir_ne_rouvre_pas_les_precedentes(self):
        phase_model.update_phase(self.etapes[4]["id"], {"status": "done"})
        phase_model.update_phase(self.etapes[4]["id"], {"status": "doing"})
        self.assertEqual(self._statuts()[:5], ["done"] * 4 + ["doing"])

    def test_autre_projet_intouche(self):
        autre = project_model.create_project("Acme")
        phase_model.apply_template(autre["id"])
        phase_model.update_phase(self.etapes[4]["id"], {"status": "done"})
        self.assertEqual({e["status"] for e in phase_model.list_phases(autre["id"])}, {"todo"})

    def test_changement_journalise(self):
        etape = project_board.change_phase(self.projet["id"], "5", {"status": "done"}, source="chat")
        self.assertEqual(etape["cascaded"], 4)
        entree = journal.list_for_project(self.projet["id"])[0]
        self.assertEqual(entree["source"], "chat")
        self.assertIn("Étape 5. Reprise des données : terminée", entree["content"])
        self.assertIn("4 étape(s) précédente(s)", entree["content"])

    def test_avancement_refuse_si_l_etape_a_des_taches(self):
        task_model.create_task(
            {"title": "Paramétrer", "project_id": self.projet["id"], "phase_id": self.etapes[2]["id"]}
        )
        with self.assertRaises(ValidationError):
            project_board.change_phase(self.projet["id"], "3", {"progress_pct": 50})
        project_board.change_phase(self.projet["id"], "2", {"progress_pct": 50})
        self.assertEqual(phase_model.get_phase(self.etapes[1]["id"])["progress_pct"], 50)

    def test_champs_de_structure_refuses(self):
        with self.assertRaises(ValidationError):
            project_board.change_phase(self.projet["id"], "1", {"name": "Renommee"})
        with self.assertRaises(ValidationError):
            project_board.change_phase(self.projet["id"], "1", {})

    def test_outil_du_chat(self):
        reponse = tools.execute("update_phase", {"project": "atlas", "phase": "developpements", "status": "Terminée"})
        self.assertTrue(reponse["ok"], reponse)
        self.assertEqual(reponse["data"]["cascaded"], 3)
        self.assertEqual(reponse["data"]["phase"]["status"], "done")

        lecture = tools.execute("list_phases", {"project": "Atlas"})
        self.assertEqual(lecture["data"]["current"], 5)

        erreur = tools.execute("update_phase", {"project": "Atlas", "phase": "42", "status": "done"})
        self.assertFalse(erreur["ok"])
        self.assertIn("1. Recueil du besoin", erreur["error"])
        self.assertFalse(tools.execute("update_phase", {"project": "Atlas", "phase": "1", "status": "fini?"})["ok"])

    def test_sessions_claude(self):
        resultat = agent.update_phase("Atlas", "3", status="done")
        self.assertEqual(resultat["cascaded"], 2)
        with self.assertRaises(AgentError):
            agent.update_phase("Atlas", "3")

        cle = setting.regenerate_agent_key()
        reponse = self.app.test_client().post(
            "/api/agent/phases/update",
            json={"project": "Atlas", "phase": "4", "planned_date": "2026-11-15"},
            headers={"Authorization": "Bearer " + cle},
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(phase_model.get_phase(self.etapes[3]["id"])["planned_date"], "2026-11-15")

    def test_route_de_l_interface_renvoie_la_cascade(self):
        reponse = self.client().patch(
            f"/api/projects/{self.projet['id']}/phases/{self.etapes[2]['id']}",
            json={"status": "done"}, headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["cascaded"], 2)
