"""Comptes rendus (lot 5) : seances de l'agenda, elements, conversions en tache et registre."""
from models import get_db
from models import minutes as minutes_model
from models import search as search_model
from models.minutes import MinutesError
from services import minutes as minutes_service
from tests.base import NomaiaTestCase


class ComptesRendusTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.pid = db.execute("INSERT INTO project (name) VALUES ('ALPHA')").lastrowid
        self.event = db.execute(
            "INSERT INTO event (project_id, title, starts_at, ends_at, repeat) VALUES "
            "(?, 'Point hebdo', '2026-11-02 10:00', '2026-11-02 11:00', 'weekly')", (self.pid,)
        ).lastrowid
        db.commit()
        self.client_http = self.client()
        self.h = self.write_headers()

    def post(self, url, data=None):
        return self.client_http.post(url, json=data or {}, headers=self.h)

    def test_un_compte_rendu_par_seance(self):
        first = self.post("/api/minutes", {"event_id": self.event, "held_on": "2026-11-02"})
        self.assertEqual(first.status_code, 201)
        data = first.get_json()
        self.assertEqual((data["title"], data["project_id"], data["held_on"]), ("Point hebdo", self.pid, "2026-11-02"))
        again = self.post("/api/minutes", {"event_id": self.event, "held_on": "2026-11-02"})
        self.assertEqual((again.status_code, again.get_json()["id"]), (200, data["id"]))
        # Seance suivante d'un evenement repete : un autre compte rendu.
        other = self.post("/api/minutes", {"event_id": self.event, "held_on": "2026-11-09"})
        self.assertEqual(other.status_code, 201)
        self.assertNotEqual(other.get_json()["id"], data["id"])

    def test_survit_a_la_suppression_de_l_evenement(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        db = get_db()
        db.execute("DELETE FROM event WHERE id = ?", (self.event,))
        db.commit()
        kept = minutes_model.get(minutes["id"])
        self.assertIsNone(kept["event_id"])
        self.assertEqual(kept["title"], "Point hebdo")

    def test_sauvegarde_automatique_et_validation(self):
        minutes = minutes_model.create({"title": "Atelier stocks", "held_on": "2026-11-05", "kind": "training"})
        r = self.client_http.patch(f"/api/minutes/{minutes['id']}", json={"discussed": "Règles FIFO", "topics": "Lettrage"},
                                   headers=self.h)
        self.assertEqual(r.get_json()["discussed"], "Règles FIFO")
        for bad in ({"title": "  "}, {"kind": "atelier"}, {"held_on": "06/11/2026"}, {"project_id": 999}):
            with self.subTest(bad=bad):
                self.assertEqual(self.client_http.patch(f"/api/minutes/{minutes['id']}", json=bad, headers=self.h).status_code, 400)

    def test_action_devient_tache_une_seule_fois(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        item = self.post(f"/api/minutes/{minutes['id']}/items",
                         {"type": "action", "text": "Envoyer le plan de comptes", "owner": "Lucie", "due": "2026-11-12"}).get_json()
        task = self.post(f"/api/minutes/items/{item['id']}/task")
        self.assertEqual(task.status_code, 201)
        t = task.get_json()
        self.assertEqual((t["title"], t["project_id"], t["deadline"]), ("Envoyer le plan de comptes", self.pid, "2026-11-12"))
        self.assertIn("Point hebdo", t["description"])
        self.assertEqual(self.post(f"/api/minutes/items/{item['id']}/task").status_code, 400)
        self.assertEqual(minutes_model.get(minutes["id"])["items"][0]["task_id"], t["id"])

    def test_demande_client_devient_tache_cote_client(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        item = minutes_model.add_item(minutes["id"], {"type": "request", "text": "Ajouter un état de TVA"})
        self.assertEqual(minutes_service.item_to_task(item["id"])["side"], "client")

    def test_decision_au_registre(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        item = minutes_model.add_item(minutes["id"], {"type": "decision", "text": "Valorisation FIFO"})
        entry = self.post(f"/api/minutes/items/{item['id']}/register").get_json()
        row = get_db().execute("SELECT kind, project_id, content FROM note WHERE id = ?", (entry["id"],)).fetchone()
        self.assertEqual((row["kind"], row["project_id"]), ("decision", self.pid))
        self.assertIn("Valorisation FIFO", row["content"])
        self.assertEqual(self.post(f"/api/minutes/items/{item['id']}/register").status_code, 400)
        with self.assertRaises(MinutesError):
            minutes_service.item_to_task(item["id"])  # une decision ne devient pas une tache

    def test_decision_sans_projet_refusee(self):
        minutes = minutes_model.create({"title": "Point libre", "held_on": "2026-11-05"})
        item = minutes_model.add_item(minutes["id"], {"type": "decision", "text": "On décale"})
        with self.assertRaises(MinutesError):
            minutes_service.item_to_register(item["id"])

    def test_validation_signale_le_reste_a_convertir(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        minutes_model.add_item(minutes["id"], {"type": "action", "text": "A"})
        done = minutes_model.add_item(minutes["id"], {"type": "action", "text": "B"})
        minutes_service.item_to_task(done["id"])
        minutes_model.add_item(minutes["id"], {"type": "decision", "text": "C"})
        result = self.post(f"/api/minutes/{minutes['id']}/finalize").get_json()
        self.assertEqual(result["minutes"]["status"], "final")
        self.assertEqual([i["text"] for i in result["pending_actions"]], ["A"])
        self.assertEqual([i["text"] for i in result["pending_decisions"]], ["C"])

    def test_liste_et_recherche(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        minutes_model.update(minutes["id"], {"discussed": "Bascule de la facturation électronique"})
        minutes_model.add_item(minutes["id"], {"type": "action", "text": "Préparer la recette"})
        listed = self.client_http.get(f"/api/minutes?project_id={self.pid}").get_json()["minutes"]
        self.assertEqual((listed[0]["actions"], listed[0]["actions_open"]), (1, 1))
        kinds = {hit["kind"] for hit in search_model.search("electronique")}
        self.assertIn("cr", kinds)
        self.assertIn("cr_element", {hit["kind"] for hit in search_model.search("recette")})

    def test_suppression_garde_les_taches(self):
        minutes, _ = minutes_model.create_from_event(self.event, "2026-11-02")
        item = minutes_model.add_item(minutes["id"], {"type": "action", "text": "Garder cette tâche"})
        task = minutes_service.item_to_task(item["id"])
        self.assertEqual(self.client_http.delete(f"/api/minutes/{minutes['id']}", headers=self.h).status_code, 200)
        self.assertIsNotNone(get_db().execute("SELECT 1 FROM task WHERE id = ?", (task["id"],)).fetchone())
