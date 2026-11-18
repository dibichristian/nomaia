"""Contacts et attentes depuis les sessions Claude : fiches sans doublon, taches dues, relances."""
from unittest.mock import patch

from models import person as person_model
from models import project as project_model
from models import setting
from models import task as task_model
from services import agent
from services.agent import AgentError
from tests.base import NomaiaTestCase
from tests.test_agent import charger_serveur_mcp


class ContactsTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        self.projet = project_model.create_project("ACME")
        self.hugo = person_model.create_person({"name": "Hugo Lambert", "role": "Chef de projet"})

    def test_nom_partiel_unique_retrouve_la_fiche(self):
        resultat = agent.save_person("Hugo", email="hugo@example.org", notes="Préfère WhatsApp.")
        self.assertFalse(resultat["created"])
        self.assertEqual(resultat["person"]["id"], self.hugo["id"])
        self.assertEqual(resultat["person"]["email"], "hugo@example.org")
        agent.save_person("hugo lambert", notes="Absent le vendredi.")
        self.assertEqual(person_model.get_person(self.hugo["id"])["notes"], "Préfère WhatsApp.\nAbsent le vendredi.")

    def test_nouveau_contact_cree_avec_son_projet(self):
        resultat = agent.save_person("Sarah", role="Comptable", project="Acme")
        self.assertTrue(resultat["created"])
        self.assertEqual((resultat["person"]["role"], resultat["person"]["project"]), ("Comptable", "ACME"))

    def test_nom_ambigu_refuse(self):
        person_model.create_person({"name": "Hugo Lucie"})
        with self.assertRaises(AgentError) as erreur:
            agent.save_person("Hugo", phone="0700")
        self.assertIn("ambigu", str(erreur.exception))

    def test_tache_due_a_un_contact(self):
        cree = agent.create_task("Envoyer le comparatif", project="ACME", person="Hugo")["created"]
        self.assertEqual(cree["person"], "Hugo Lambert")
        self.assertEqual([t["id"] for t in person_model.owed_tasks(self.hugo["id"])], [cree["id"]])
        tache = agent.update_task(cree["id"], person="")
        self.assertIsNone(tache["person"])
        nouveau = agent.update_task(cree["id"], person="Lucie Bernard")
        self.assertEqual(nouveau["person"], "Lucie Bernard")
        self.assertIsNotNone(person_model.find_by_name("Lucie Bernard"))

    def test_liste_filtree(self):
        person_model.create_person({"name": "Tom Petit", "role": "Chef d'équipe"})
        noms = [p["name"] for p in agent.list_people("chef")["people"]]
        self.assertEqual(sorted(noms), ["Hugo Lambert", "Tom Petit"])


class AttentesTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        self.hugo = person_model.create_person({"name": "Hugo Lambert"})

    def test_attente_creee_puis_dedoublonnee_puis_close(self):
        premiere = agent.add_waiting("Hugo", "Valider le PV de recette", due="2026-11-12")
        self.assertTrue(premiere["created"])
        self.assertEqual(premiere["waiting"]["person_name"], "Hugo Lambert")
        doublon = agent.add_waiting("Hugo Lambert", "valider le pv de recette")
        self.assertFalse(doublon["created"])
        self.assertEqual(doublon["waiting"]["id"], premiere["waiting"]["id"])

        ouvertes = agent.list_waiting(person="Hugo")["waiting"]
        self.assertEqual(len(ouvertes), 1)
        agent.resolve_waiting(premiere["waiting"]["id"], status="reçu")
        self.assertEqual(agent.list_waiting()["waiting"], [])
        self.assertEqual(agent.list_waiting(status="all")["waiting"][0]["status"], "received")

    def test_contact_inconnu_cree_et_tache_bloquee_verifiee(self):
        tache = task_model.create_task({"title": "Importer les écritures"})
        resultat = agent.add_waiting("Claire", "Envoyer l'export Sage", task_id=tache["id"])
        self.assertEqual(resultat["waiting"]["task_id"], tache["id"])
        self.assertIsNotNone(person_model.find_by_name("Claire"))
        with self.assertRaises(AgentError):
            agent.add_waiting("Claire", "Autre", task_id=99999)
        with self.assertRaises(AgentError):
            agent.resolve_waiting(resultat["waiting"]["id"], status="open")


class ApiEtMcpTest(NomaiaTestCase):
    def test_routes_de_l_api(self):
        client = self.app.test_client()
        h = {"Authorization": "Bearer " + setting.regenerate_agent_key()}
        self.assertEqual(client.post("/api/agent/people", json={"name": "Lucie"}, headers=h).status_code, 201)
        self.assertEqual(client.post("/api/agent/people", json={"name": "lucie", "role": "DSI"}, headers=h).status_code, 200)
        attente = client.post("/api/agent/waiting", json={"person": "Lucie", "subject": "Accès VPN"}, headers=h)
        self.assertEqual(attente.status_code, 201)
        wid = attente.get_json()["waiting"]["id"]
        self.assertEqual(client.get("/api/agent/waiting?person=Lucie", headers=h).get_json()["waiting"][0]["id"], wid)
        close = client.post(f"/api/agent/waiting/{wid}/resolve", json={"status": "dropped"}, headers=h)
        self.assertEqual(close.get_json()["waiting"]["status"], "dropped")
        self.assertEqual(client.get("/api/agent/people?q=dsi", headers=h).get_json()["people"][0]["name"], "Lucie")

    def test_outils_relayes(self):
        mcp = charger_serveur_mcp()
        cas = [
            ("nomaia_list_people", {"query": "chef"}, ("GET", "/people"), {"query": {"q": "chef", "project": None}}),
            ("nomaia_save_person", {"name": "Lucie"}, ("POST", "/people"), {"payload": {"name": "Lucie"}}),
            ("nomaia_list_waiting", {"person": "Lucie"}, ("GET", "/waiting"), {"query": {"person": "Lucie", "status": None}}),
            ("nomaia_add_waiting", {"person": "Lucie", "subject": "PV"}, ("POST", "/waiting"),
             {"payload": {"person": "Lucie", "subject": "PV"}}),
            ("nomaia_resolve_waiting", {"waiting_id": 3, "status": "received"}, ("POST", "/waiting/3/resolve"),
             {"payload": {"status": "received"}}),
        ]
        for nom, arguments, attendu, kwargs in cas:
            with patch.object(mcp, "call_nomaia", return_value={}) as appel:
                mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": nom, "arguments": arguments}})
            appel.assert_called_once_with(*attendu, **kwargs)
        self.assertIn("nomaia_add_waiting", mcp.INSTRUCTIONS)
        creation = next(o for o in mcp.TOOLS if o["name"] == "nomaia_create_task")
        self.assertIn("person", creation["inputSchema"]["properties"])
