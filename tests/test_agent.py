"""Tests du lien avec les sessions Claude Code : API /api/agent/ et serveur MCP.

Une session Claude doit pouvoir retrouver, creer et faire avancer une tache,
et declarer son temps, sans jamais passer par le cookie de l'interface ni
ouvrir de doublon a la legere. Le bilan doit ensuite voir ce travail.
"""
import importlib.util
from datetime import date
from pathlib import Path
from unittest.mock import patch

import config
from models import journal, setting
from models import minutes as minutes_model
from models import phase as phase_model
from models import project as project_model
from models import stats
from models import task as task_model
from services import agent, review
from services.agent import AgentError
from tests.base import NomaiaTestCase


def charger_serveur_mcp():
    """Importe integrations/claude_mcp.py comme module, sans le lancer."""
    chemin = Path(config.RESOURCE_DIR) / "integrations" / "claude_mcp.py"
    spec = importlib.util.spec_from_file_location("claude_mcp", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AccesTest(NomaiaTestCase):
    """La cle des sessions est la seule porte de /api/agent/."""

    def setUp(self):
        super().setUp()
        self.cle = setting.regenerate_agent_key()

    def _get(self, entete=None, cookie=False):
        client = self.client() if cookie else self.app.test_client()
        headers = {"Authorization": entete} if entete else {}
        return client.get("/api/agent/tasks", headers=headers)

    def test_sans_cle_refuse(self):
        self.assertEqual(self._get().status_code, 401)

    def test_mauvaise_cle_refusee(self):
        self.assertEqual(self._get("Bearer faux").status_code, 401)

    def test_cookie_de_l_interface_ne_suffit_pas(self):
        self.assertEqual(self._get(cookie=True).status_code, 401)

    def test_bonne_cle_acceptee(self):
        self.assertEqual(self._get("Bearer " + self.cle).status_code, 200)

    def test_cle_regeneree_revoque_l_ancienne(self):
        setting.regenerate_agent_key()
        self.assertEqual(self._get("Bearer " + self.cle).status_code, 401)

    def test_cle_des_sessions_n_ouvre_pas_l_interface(self):
        reponse = self.app.test_client().get(
            "/api/tasks", headers={"Authorization": "Bearer " + self.cle}
        )
        self.assertEqual(reponse.status_code, 401)

    def test_page_config_ne_relit_jamais_la_cle(self):
        corps = self.client().get("/api/agent-setup").get_json()
        self.assertTrue(corps["defined"])
        self.assertNotIn(self.cle, str(corps))

    def test_generation_renvoie_la_commande(self):
        corps = self.client().post("/api/agent-setup/key", headers=self.write_headers()).get_json()
        self.assertIn("claude mcp add --scope user nomaia", corps["command"])
        self.assertIn(setting.get_agent_key(), corps["command"])


class OperationsTest(NomaiaTestCase):
    """Retrouver, creer, faire avancer, declarer du temps."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")

    def test_creation_marquee_et_journalisee(self):
        resultat = agent.create_task("Corriger l'export paie", project="ALPHA", estimate_min=90)
        tache = task_model.get_task(resultat["created"]["id"])

        self.assertEqual(tache["source"], "claude")
        self.assertEqual(tache["project_id"], self.projet["id"])
        self.assertEqual(len(journal.list_for_task(tache["id"])), 1)

    def test_tache_proche_proposee_au_lieu_d_un_doublon(self):
        existante = task_model.create_task({"title": "Corriger l'export de la paie ALPHA"})
        resultat = agent.create_task("Corriger export paie ALPHA")

        self.assertIsNone(resultat["created"])
        self.assertEqual(resultat["similar"][0]["id"], existante["id"])

        force = agent.create_task("Corriger export paie ALPHA", force=True)
        self.assertIsNotNone(force["created"])

    def test_projet_inconnu_explique(self):
        with self.assertRaises(AgentError) as erreur:
            agent.create_task("Faire un truc", project="Inexistant")
        self.assertIn("ALPHA", str(erreur.exception))

    def test_avancement_puis_cloture(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        agent.update_task(tache["id"], progress_pct=40, note="Script ecrit, reste les tests.")
        self.assertEqual(task_model.get_task(tache["id"])["progress_pct"], 40)

        resultat = agent.update_task(tache["id"], status="done")
        self.assertEqual(resultat["status"], "done")
        self.assertEqual(resultat["progress_pct"], 100)
        types = [entree["kind"] for entree in journal.list_for_task(tache["id"])]
        self.assertIn("progress", types)

    def test_mise_a_jour_vide_refusee(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        with self.assertRaises(AgentError):
            agent.update_task(tache["id"])

    def test_temps_declare_compte_dans_le_suivi(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        agent.log_work(tache["id"], 45, "Migration testee en recette.")

        self.assertEqual(task_model.get_task(tache["id"])["tracked_min"], 45)
        aujourd_hui = date.today().isoformat()
        travail = stats.agent_work(aujourd_hui, aujourd_hui)
        self.assertEqual(travail["minutes"], 45)
        self.assertEqual(travail["tasks_worked"], 1)

    def test_temps_aberrant_refuse(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        with self.assertRaises(AgentError):
            agent.log_work(tache["id"], 2000, "Trop long")
        with self.assertRaises(AgentError):
            agent.log_work(tache["id"], 30, "  ")

    def test_recherche_par_mots(self):
        task_model.create_task({"title": "Corriger l'export paie"})
        task_model.create_task({"title": "Relancer Paul"})
        titres = [t["title"] for t in agent.list_tasks(query="export")]
        self.assertEqual(titres, ["Corriger l'export paie"])

    def test_bilan_voit_le_travail_des_sessions(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        agent.log_work(tache["id"], 30, "Script de migration ecrit.")
        aujourd_hui = date.today()

        donnees = review.gather(aujourd_hui, aujourd_hui)
        self.assertEqual(donnees["agent"]["minutes"], 30)
        self.assertIn("SESSIONS CLAUDE", review.build_prompt(donnees))
        self.assertIn("Script de migration", review.build_prompt(donnees))

    def test_route_complete(self):
        cle = setting.regenerate_agent_key()
        client = self.app.test_client()
        entete = {"Authorization": "Bearer " + cle}

        cree = client.post("/api/agent/tasks", json={"title": "Ecrire le module RH"}, headers=entete)
        self.assertEqual(cree.status_code, 201)
        task_id = cree.get_json()["created"]["id"]

        avance = client.post(
            f"/api/agent/tasks/{task_id}/progress", json={"progress_pct": 150}, headers=entete
        )
        self.assertEqual(avance.status_code, 400)

        travail = client.post(
            f"/api/agent/tasks/{task_id}/work",
            json={"minutes": 20, "summary": "Modele cree."},
            headers=entete,
        )
        self.assertEqual(travail.status_code, 200)
        detail = client.get(f"/api/agent/tasks/{task_id}", headers=entete).get_json()
        self.assertEqual(detail["journal"][0]["content"], "Modele cree.")


class CreationProjetTest(NomaiaTestCase):
    """Une session cree un projet, sans jamais doubler un projet existant."""

    def setUp(self):
        super().setUp()
        self.cle = setting.regenerate_agent_key()
        self.entete = {"Authorization": "Bearer " + self.cle}

    def _post(self, corps, entete=None):
        return self.app.test_client().post(
            "/api/agent/projects", json=corps, headers=self.entete if entete is None else entete
        )

    def test_creation_de_projet(self):
        reponse = self._post({"name": "  Brightjet  ", "deadline": "2026-12-31"})

        self.assertEqual(reponse.status_code, 201)
        corps = reponse.get_json()
        self.assertTrue(corps["created"])
        self.assertEqual(corps["project"]["name"], "Brightjet")
        self.assertEqual(corps["project"]["deadline"], "2026-12-31")
        self.assertEqual(corps["project"]["open_tasks"], 0)
        self.assertEqual(set(corps["project"]), set(agent.list_projects()[0]))
        projet = project_model.find_by_name("brightjet")
        self.assertEqual(projet["color"], project_model.suggest_color("Brightjet"))

    def test_doublon_insensible_a_la_casse_renvoie_l_existant(self):
        existant = project_model.create_project("ALPHA")
        project_model.update_project(existant["id"], {"status": "done"})
        task_model.create_task({"title": "Migrer la base", "project_id": existant["id"]})

        reponse = self._post({"name": "alpha"})

        self.assertEqual(reponse.status_code, 200)
        corps = reponse.get_json()
        self.assertFalse(corps["created"])
        self.assertEqual(corps["existing"]["id"], existant["id"])
        self.assertEqual(corps["existing"]["open_tasks"], 1)
        self.assertIn("Aucun projet cree", corps["hint"])
        self.assertEqual(len(project_model.list_projects(status=None)), 1)

    def test_nom_vide_refuse(self):
        reponse = self._post({"name": "   "})
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(project_model.list_projects(status=None), [])

    def test_echeance_mal_formee_refusee(self):
        with self.assertRaises(AgentError):
            agent.create_project("Brightjet", deadline="31/12/2026")

    def test_sans_cle_refuse(self):
        self.assertEqual(self._post({"name": "Brightjet"}, entete={}).status_code, 401)
        self.assertEqual(project_model.list_projects(status=None), [])


class CadrageTest(NomaiaTestCase):
    """Une session ajuste echeance, priorite, projet, etape, blocage et camp."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ALPHA")
        self.etapes = phase_model.apply_template(self.projet["id"])
        self.tache = task_model.create_task({"title": "Parametrer la paie", "project_id": self.projet["id"]})

    def test_etape_par_numero_ou_par_nom(self):
        resultat = agent.update_task(self.tache["id"], phase="3")
        self.assertEqual(resultat["phase"], "3. Configuration")

        agent.update_task(self.tache["id"], phase="")
        self.assertIsNone(task_model.get_task(self.tache["id"])["phase_id"])

        agent.update_task(self.tache["id"], phase="configuration")
        self.assertEqual(task_model.get_task(self.tache["id"])["phase_id"], self.etapes[2]["id"])

    def test_etape_ambigue_ou_inconnue_expliquee(self):
        for phase in ("des", "Pilotage", "42"):
            with self.assertRaises(AgentError) as erreur:
                agent.update_task(self.tache["id"], phase=phase)
            self.assertIn("1. Recueil du besoin", str(erreur.exception))

    def test_etape_sans_projet_refusee(self):
        orpheline = task_model.create_task({"title": "Classer les mails"})
        with self.assertRaises(AgentError):
            agent.update_task(orpheline["id"], phase="1")

    def test_cadrage_applique_et_journalise(self):
        resultat = agent.update_task(
            self.tache["id"], deadline="2026-11-30", priority=1, blocked=True, side="client"
        )
        self.assertEqual(resultat["deadline"], "2026-11-30")
        self.assertEqual(resultat["priority"], 1)
        self.assertTrue(resultat["blocked"])
        self.assertEqual(resultat["side"], "client")
        entree = journal.list_for_task(self.tache["id"])[0]["content"]
        self.assertIn("bloquee", entree)
        self.assertIn("qui doit agir client", entree)

        agent.update_task(self.tache["id"], deadline="", side="")
        tache = task_model.get_task(self.tache["id"])
        self.assertIsNone(tache["deadline"])
        self.assertIsNone(tache["side"])

    def test_valeur_inchangee_non_journalisee(self):
        agent.update_task(self.tache["id"], blocked=False, note="Rien de neuf.")
        self.assertEqual(journal.list_for_task(self.tache["id"])[0]["content"], "Rien de neuf.")

    def test_camp_invalide_refuse(self):
        with self.assertRaises(AgentError):
            agent.update_task(self.tache["id"], side="eux")

    def test_changement_de_projet_retire_l_etape(self):
        project_model.create_project("Acme")
        agent.update_task(self.tache["id"], phase="3")
        resultat = agent.update_task(self.tache["id"], project="Acme")

        self.assertEqual(resultat["project"], "Acme")
        self.assertIsNone(resultat["phase"])
        self.assertIn("etape retiree", journal.list_for_task(self.tache["id"])[0]["content"])

    def test_creation_avec_etape_et_camp(self):
        resultat = agent.create_task(
            "Relancer le client sur le plan comptable", project="ALPHA", phase="1",
            side="client", blocked=True,
        )
        tache = task_model.get_task(resultat["created"]["id"])
        self.assertEqual(tache["phase_id"], self.etapes[0]["id"])
        self.assertEqual(tache["side"], "client")
        self.assertEqual(tache["blocked"], 1)

    def test_liste_des_etapes(self):
        agent.update_task(self.tache["id"], phase="3", progress_pct=50)
        resultat = agent.list_phases("alpha")

        self.assertEqual(len(resultat["phases"]), 11)
        self.assertEqual(resultat["current"], 3)
        self.assertEqual(resultat["phases"][2]["tasks_open"], 1)
        with self.assertRaises(AgentError):
            agent.list_phases("")

    def test_routes(self):
        cle = setting.regenerate_agent_key()
        client = self.app.test_client()
        entete = {"Authorization": "Bearer " + cle}

        reponse = client.post(
            f"/api/agent/tasks/{self.tache['id']}/progress",
            json={"phase": "Developpements", "blocked": True, "progress_pct": 20},
            headers=entete,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["phase"], "4. Développements")

        etapes = client.get("/api/agent/phases?project=ALPHA", headers=entete)
        self.assertEqual(etapes.status_code, 200)
        self.assertEqual(etapes.get_json()["current"], 4)
        self.assertEqual(client.get("/api/agent/phases", headers=entete).status_code, 400)


class JournalUiTest(NomaiaTestCase):
    """Journal d'une tache depuis l'interface."""

    def test_note_et_avancement(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        reponse = self.client().post(
            f"/api/tasks/{tache['id']}/journal",
            json={"content": "A mi-chemin", "progress_pct": 50},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 201)
        corps = self.client().get(f"/api/tasks/{tache['id']}").get_json()
        self.assertEqual(corps["progress_pct"], 50)
        self.assertEqual(corps["journal"][0]["content"], "A mi-chemin")

    def test_note_vide_refusee(self):
        tache = task_model.create_task({"title": "Migrer la base"})
        reponse = self.client().post(
            f"/api/tasks/{tache['id']}/journal", json={"content": " "}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)


class ServeurMcpTest(NomaiaTestCase):
    """Protocole du serveur MCP, sans reseau."""

    def setUp(self):
        super().setUp()
        self.mcp = charger_serveur_mcp()

    def test_initialisation_porte_les_consignes(self):
        reponse = self.mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )
        self.assertEqual(reponse["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("VRAI travail", reponse["result"]["instructions"])

    def test_version_inconnue_ramenee_a_la_plus_recente(self):
        reponse = self.mcp.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
        )
        self.assertEqual(reponse["result"]["protocolVersion"], self.mcp.PROTOCOL_VERSIONS[0])

    def test_notification_sans_reponse(self):
        self.assertIsNone(self.mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_catalogue(self):
        reponse = self.mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        noms = {outil["name"] for outil in reponse["result"]["tools"]}
        self.assertIn("nomaia_log_work", noms)

    def test_appel_relaye_a_nomaia(self):
        with patch.object(self.mcp, "call_nomaia", return_value={"tasks": []}) as appel:
            reponse = self.mcp.handle({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "nomaia_log_work", "arguments": {"task_id": 7, "minutes": 10, "summary": "x"}},
            })
        appel.assert_called_once_with("POST", "/tasks/7/work", payload={"minutes": 10, "summary": "x"})
        self.assertFalse(reponse["result"]["isError"])

    def test_etapes_relayees_a_nomaia(self):
        with patch.object(self.mcp, "call_nomaia", return_value={"phases": []}) as appel:
            self.mcp.handle({
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "nomaia_list_phases", "arguments": {"project": "ALPHA"}},
            })
        appel.assert_called_once_with("GET", "/phases", query={"project": "ALPHA"})

    def test_cadrage_relaye_a_nomaia(self):
        with patch.object(self.mcp, "call_nomaia", return_value={}) as appel:
            self.mcp.handle({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "nomaia_update_task", "arguments": {"task_id": 7, "phase": "3", "blocked": True}},
            })
        appel.assert_called_once_with("POST", "/tasks/7/progress", payload={"phase": "3", "blocked": True})

    def test_creation_de_projet_relayee_a_nomaia(self):
        reponse = self.mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        outil = next(o for o in reponse["result"]["tools"] if o["name"] == "nomaia_create_project")
        self.assertEqual(outil["inputSchema"]["required"], ["name"])

        with patch.object(self.mcp, "call_nomaia", return_value={"created": True}) as appel:
            reponse = self.mcp.handle({
                "jsonrpc": "2.0", "id": 8, "method": "tools/call",
                "params": {"name": "nomaia_create_project", "arguments": {"name": "Brightjet", "deadline": "2026-12-31"}},
            })
        appel.assert_called_once_with(
            "POST", "/projects", payload={"name": "Brightjet", "deadline": "2026-12-31"}
        )
        self.assertFalse(reponse["result"]["isError"])

    def test_nomaia_injoignable_devient_une_erreur_d_outil(self):
        erreur = self.mcp.NomaiaUnavailable("NOMAIA injoignable")
        with patch.object(self.mcp, "call_nomaia", side_effect=erreur):
            reponse = self.mcp.handle({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "nomaia_list_tasks", "arguments": {}},
            })
        self.assertTrue(reponse["result"]["isError"])
        self.assertIn("injoignable", reponse["result"]["content"][0]["text"])


class NotesEtComptesRendusTest(NomaiaTestCase):
    """Une session Claude lit, cree et complete notes et comptes rendus, sans secret en clair."""

    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ORION")
        self.headers = {"Authorization": "Bearer " + setting.regenerate_agent_key()}
        self.http = self.app.test_client()

    def _appel(self, methode, chemin, corps=None):
        return self.http.open("/api/agent" + chemin, method=methode, json=corps, headers=self.headers)

    def test_note_creee_lue_puis_completee(self):
        reponse = self._appel("POST", "/notes", {
            "title": "Recalcul des congés", "content": "## Contexte\nCalcul faux en fin de mois.",
            "category": "python", "project": "ORION", "odoo_version": "17.0", "tags": ["paie", "Congés"],
        })
        self.assertEqual(reponse.status_code, 201)
        note = reponse.get_json()["created"]
        self.assertEqual((note["project"], note["tags"]), ("ORION", ["paie", "congés"]))
        self.assertNotIn("content", note)

        self._appel("PATCH", f"/notes/{note['id']}", {"content": "## Solution\nForcer le recalcul.", "append": True})
        lue = self._appel("GET", f"/notes/{note['id']}").get_json()
        self.assertEqual(lue["content"], "## Contexte\nCalcul faux en fin de mois.\n\n## Solution\nForcer le recalcul.")

        self._appel("PATCH", f"/notes/{note['id']}", {"content": "Remplacé", "project": ""})
        lue = agent.get_note(note["id"])
        self.assertEqual((lue["content"], lue["project"], lue["title"]), ("Remplacé", None, "Recalcul des congés"))

    def test_note_refusee_si_secret_projet_inconnu_ou_rien_a_modifier(self):
        secret = self._appel("POST", "/notes", {"title": "Accès", "content": "password: Azerty2026"})
        self.assertEqual(secret.status_code, 400)
        self.assertIn("secret", secret.get_json()["error"])
        self.assertIn("inconnu", self._appel("POST", "/notes", {"title": "X", "project": "Fantôme"}).get_json()["error"])
        note = agent.create_note({"title": "Vide"})["created"]
        self.assertEqual(self._appel("PATCH", f"/notes/{note['id']}", {}).status_code, 400)
        self.assertEqual(self._appel("GET", "/notes/999").status_code, 400)

    def test_compte_rendu_cree_avec_elements_puis_valide(self):
        reponse = self._appel("POST", "/minutes", {
            "title": "Point paie", "project": "ORION", "discussed": "Écarts de congés",
            "items": [
                {"type": "decision", "text": "Recalcul au 30"},
                {"type": "action", "text": "Corriger la règle", "owner": "Jean", "due": "2026-11-20"},
            ],
        })
        self.assertEqual(reponse.status_code, 201)
        cr = reponse.get_json()["minutes"]
        self.assertEqual((cr["held_on"], cr["kind"], cr["status"], cr["project"]),
                         (date.today().isoformat(), "meeting", "draft", "ORION"))
        self.assertEqual([i["type"] for i in cr["items"]], ["decision", "action"])

        valide = self._appel("PATCH", f"/minutes/{cr['id']}", {
            "notes": "RAS", "add_items": [{"type": "request", "text": "Envoyer l'export"}], "status": "final",
        }).get_json()
        self.assertEqual(valide["minutes"]["status"], "final")
        self.assertEqual(len(valide["minutes"]["items"]), 3)
        self.assertEqual(valide["pending_actions"], ["Corriger la règle"])
        self.assertEqual(valide["pending_decisions"], ["Recalcul au 30"])
        self.assertEqual(agent.get_minutes(cr["id"])["discussed"], "Écarts de congés")

    def test_compte_rendu_rien_n_est_ecrit_si_un_element_est_invalide(self):
        avant = len(minutes_model.list_minutes())
        mauvais = self._appel("POST", "/minutes", {
            "title": "Atelier", "items": [{"type": "action", "text": "OK"}, {"type": "idee", "text": "?"}],
        })
        self.assertEqual(mauvais.status_code, 400)
        self.assertIn("Element 2", mauvais.get_json()["error"])
        secret = self._appel("POST", "/minutes", {"title": "Install", "notes": "mdp: Azerty2026"})
        self.assertEqual(secret.status_code, 400)
        self.assertIn("Notes", secret.get_json()["error"])
        cle = self._appel("POST", "/minutes", {"title": "Install", "items": [{"type": "action", "text": "token=Azerty2026"}]})
        self.assertEqual(cle.status_code, 400)
        self.assertEqual(len(minutes_model.list_minutes()), avant)

    def test_recherche_donne_nature_et_lien(self):
        cr = agent.create_minutes({"title": "Comité de pilotage Brightjet"})["minutes"]
        resultat = self._appel("GET", "/search?q=brightjet").get_json()["results"]
        self.assertIn({"kind": "cr", "url": f"/comptes-rendus/{cr['id']}"},
                      [{"kind": r["kind"], "url": r["url"]} for r in resultat])


class ServeurMcpNotesTest(NomaiaTestCase):
    """Les outils notes et comptes rendus sont relayes vers les bonnes routes."""

    def setUp(self):
        super().setUp()
        self.mcp = charger_serveur_mcp()

    def _outil(self, nom, arguments):
        with patch.object(self.mcp, "call_nomaia", return_value={}) as appel:
            reponse = self.mcp.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": nom, "arguments": arguments},
            })
        self.assertFalse(reponse["result"]["isError"])
        return appel

    def test_routes_relayees(self):
        self._outil("nomaia_get_note", {"note_id": 4}).assert_called_once_with("GET", "/notes/4")
        self._outil("nomaia_create_note", {"title": "T"}).assert_called_once_with("POST", "/notes", payload={"title": "T"})
        self._outil("nomaia_update_note", {"note_id": 4, "content": "x", "append": True}).assert_called_once_with(
            "PATCH", "/notes/4", payload={"content": "x", "append": True})
        self._outil("nomaia_get_minutes", {"minutes_id": 2}).assert_called_once_with("GET", "/minutes/2")
        self._outil("nomaia_create_minutes", {"title": "CR"}).assert_called_once_with(
            "POST", "/minutes", payload={"title": "CR"})
        self._outil("nomaia_update_minutes", {"minutes_id": 2, "status": "final"}).assert_called_once_with(
            "PATCH", "/minutes/2", payload={"status": "final"})

    def test_consigne_limite_l_ecriture_a_la_demande(self):
        self.assertIn("SEULEMENT quand l'utilisateur le demande", self.mcp.INSTRUCTIONS)
        noms = {outil["name"] for outil in self.mcp.TOOLS}
        self.assertTrue({"nomaia_create_note", "nomaia_update_minutes"} <= noms)
        self.assertFalse({nom for nom in noms if "delete" in nom})
