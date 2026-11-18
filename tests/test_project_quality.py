"""Tests du lot 2 du pilotage : anomalies, livrables du client et registre.

Une anomalie et un livrable restent des taches (meme statut derive, memes
outils) ; une entree du registre est une ligne du journal du projet, dont le
statut se deduit de sa tache de suivi. Rien n'est saisi deux fois.
"""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import config
from models import journal, setting
from models import project as project_model
from models import task as task_model
from models.journal import JournalError
from models.task import ValidationError
from services import agent, project_board, project_register
from services.agent import AgentError
from tests.base import NomaiaTestCase


class NatureTacheTest(NomaiaTestCase):
    """Regles de coherence entre nature, gravite, module et camp."""

    def test_anomalie_majeure_par_defaut(self):
        tache = task_model.create_task({"title": "Plafond de credit errone", "kind": "anomaly"})
        self.assertEqual(tache["kind"], "anomaly")
        self.assertEqual(tache["severity"], "major")

    def test_tache_ordinaire_sans_gravite_ni_module(self):
        tache = task_model.create_task(
            {"title": "Corriger le stock", "kind": "anomaly", "severity": "critical", "module": "Stock"}
        )
        tache = task_model.update_task(tache["id"], {"kind": "task"})
        self.assertIsNone(tache["severity"])
        self.assertIsNone(tache["module"])

    def test_livrable_attendu_du_client_par_defaut(self):
        tache = task_model.create_task({"title": "Recevoir la base des camions", "kind": "deliverable"})
        self.assertEqual(tache["side"], "client")

        nous = task_model.create_task({"title": "Envoyer le PV", "kind": "deliverable", "side": "us"})
        self.assertEqual(nous["side"], "us")

    def test_valeurs_invalides_refusees(self):
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "X", "kind": "bug"})
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "X", "kind": "anomaly", "severity": "bloquante"})
        with self.assertRaises(ValidationError):
            task_model.create_task({"title": "X", "module": "M" * 81})


class TableauDeBordTest(NomaiaTestCase):
    """Compteurs, signaux et listes du tableau de bord."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("Acme")
        pid = self.projet["id"]
        self.critique = task_model.create_task(
            {"title": "Plafond de credit", "project_id": pid, "kind": "anomaly", "severity": "critical"}
        )
        self.mineure = task_model.create_task(
            {"title": "Bons de commande", "project_id": pid, "kind": "anomaly", "severity": "minor"}
        )
        corrigee = task_model.create_task({"title": "Provisions", "project_id": pid, "kind": "anomaly"})
        task_model.toggle_task(corrigee["id"])
        task_model.create_task({"title": "Plan comptable", "project_id": pid, "kind": "deliverable"})
        recu = task_model.create_task({"title": "Soldes", "project_id": pid, "kind": "deliverable"})
        task_model.toggle_task(recu["id"])
        task_model.create_task({"title": "Parametrer les ventes", "project_id": pid})

    def test_compteurs_et_signal_critique(self):
        carte = project_board.detail(self.projet["id"])["project"]
        self.assertEqual(carte["anomalies"]["critical"], 1)
        self.assertEqual(carte["anomalies"]["minor"], 1)
        self.assertEqual(carte["anomalies"]["open"], 2)
        self.assertEqual(carte["anomalies"]["fixed"], 1)
        self.assertEqual(carte["deliverables"], {"awaited": 1, "received": 1})
        self.assertEqual(carte["signals"][0]["level"], "danger")
        self.assertIn("anomalie(s) critique(s)", " ".join(s["label"] for s in carte["signals"]))

    def test_indicateurs_globaux(self):
        kpis = project_board.overview()["kpis"]
        self.assertEqual(kpis["anomalies_open"], 2)
        self.assertEqual(kpis["anomalies_critical"], 1)
        self.assertEqual(kpis["deliverables_awaited"], 1)

    def test_listes_ordonnees(self):
        taches = project_board.detail(self.projet["id"])["tasks"]
        anomalies = taches["anomalies"]
        self.assertEqual(anomalies[0]["id"], self.critique["id"])
        self.assertEqual(anomalies[1]["id"], self.mineure["id"])
        self.assertEqual(anomalies[-1]["status"], "done")
        self.assertEqual([ligne["status"] for ligne in taches["deliverables"]], ["todo", "done"])


class RegistreTest(NomaiaTestCase):
    """Decisions, changements de perimetre et dependances."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("Atlas")

    def test_decision_sans_tache_de_suivi(self):
        entree = project_register.add_record(
            self.projet["id"], "decision", "Bascule visee au 01/01/2027", follow_up=True
        )
        self.assertIsNone(entree["task"])
        registre = project_board.detail(self.projet["id"])["register"]
        self.assertEqual(registre[0]["status"], "acted")

    def test_perimetre_suivi_jusqu_au_reglement(self):
        entree = project_register.add_record(
            self.projet["id"], "scope", "Solution POS pour les ventes stations", follow_up=True
        )
        tache = entree["task"]
        self.assertEqual(tache["title"], "Arbitrer : Solution POS pour les ventes stations")
        self.assertEqual(tache["project_id"], self.projet["id"])
        self.assertEqual(project_board.detail(self.projet["id"])["register"][0]["status"], "open")

        task_model.toggle_task(tache["id"])
        self.assertEqual(project_board.detail(self.projet["id"])["register"][0]["status"], "settled")

    def test_dependance_sans_suivi_notee(self):
        project_register.add_record(self.projet["id"], "dependency", "Acces VPN du client")
        self.assertEqual(project_board.detail(self.projet["id"])["register"][0]["status"], "noted")

    def test_titre_de_suivi_tronque(self):
        entree = project_register.add_record(self.projet["id"], "dependency", "x" * 300, follow_up=True)
        self.assertLessEqual(len(entree["task"]["title"]), project_register.FOLLOW_UP_TITLE_CHARS)

    def test_type_hors_registre_refuse(self):
        with self.assertRaises(JournalError):
            project_register.add_record(self.projet["id"], "work", "Du temps")
        with self.assertRaises(JournalError):
            project_register.add_record(self.projet["id"], "decision", "   ")

    def test_route_du_journal(self):
        client = self.client()
        base = f"/api/projects/{self.projet['id']}/journal"

        reponse = client.post(base, json={"content": "Point d'etape"}, headers=self.write_headers())
        self.assertEqual(reponse.get_json()["kind"], "note")

        reponse = client.post(
            base, json={"content": "Destinations manquantes", "kind": "dependency", "follow_up": True},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 201)
        self.assertIsNotNone(reponse.get_json()["task"])

        reponse = client.post(base, json={"content": "X", "kind": "magie"}, headers=self.write_headers())
        self.assertEqual(reponse.status_code, 400)

        self.assertEqual(len(journal.list_register(self.projet["id"])), 1)


class SessionsClaudeTest(NomaiaTestCase):
    """Les memes notions ouvertes aux sessions Claude."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ZENTRO")

    def test_anomalie_creee_et_filtree(self):
        resultat = agent.create_task(
            "Ecarts IGR sur la paie de juin", project="ZENTRO", kind="anomaly", module="RH / Paie"
        )
        self.assertEqual(resultat["created"]["kind"], "anomaly")
        self.assertEqual(resultat["created"]["severity"], "major")
        agent.create_task("Relire le contrat", project="ZENTRO")

        titres = [t["title"] for t in agent.list_tasks(kind="anomaly")]
        self.assertEqual(titres, ["Ecarts IGR sur la paie de juin"])
        with self.assertRaises(AgentError):
            agent.list_tasks(kind="bug")

    def test_gravite_modifiee_et_journalisee(self):
        tache = task_model.create_task({"title": "Stock faux", "kind": "anomaly"})
        resultat = agent.update_task(tache["id"], severity="critical")
        self.assertEqual(resultat["severity"], "critical")
        self.assertIn("gravite critical", journal.list_for_task(tache["id"])[0]["content"])

    def test_registre(self):
        resultat = agent.add_record("ZENTRO", "scope", "Comptes collectifs avec fiche tiers", follow_up=True)
        self.assertEqual(resultat["kind"], "scope")
        self.assertEqual(resultat["follow_up_task"]["source"], "claude")
        with self.assertRaises(AgentError):
            agent.add_record("ZENTRO", "note", "Pas du registre")
        with self.assertRaises(AgentError):
            agent.add_record("", "decision", "Sans projet")

    def test_route_et_serveur_mcp(self):
        cle = setting.regenerate_agent_key()
        reponse = self.app.test_client().post(
            "/api/agent/records",
            json={"project": "ZENTRO", "kind": "decision", "content": "Plan comptable valide"},
            headers={"Authorization": "Bearer " + cle},
        )
        self.assertEqual(reponse.status_code, 201)

        chemin = Path(config.RESOURCE_DIR) / "integrations" / "claude_mcp.py"
        spec = importlib.util.spec_from_file_location("claude_mcp", chemin)
        mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp)
        with patch.object(mcp, "call_nomaia", return_value={}) as appel:
            mcp.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "nomaia_add_record", "arguments": {"project": "ZENTRO", "kind": "decision", "content": "x"}},
            })
        appel.assert_called_once_with(
            "POST", "/records", payload={"project": "ZENTRO", "kind": "decision", "content": "x"}
        )
