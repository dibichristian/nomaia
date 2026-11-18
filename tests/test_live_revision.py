"""Rechargement a chaud : compteur de revision tenu par triggers et route /api/revision."""
from models import capture as capture_model
from models import event as event_model
from models import get_db, init_schema
from models import knowledge
from models import journal
from models import person as person_model
from models import revision
from models import setting
from models import task as task_model
from tests.base import NomaiaTestCase


class RevisionTriggersTest(NomaiaTestCase):
    """Toute ecriture sur une table affichee fait avancer la revision."""

    def assertBumps(self, action):
        """Verifie que l'action fait avancer la revision, et retourne son resultat."""
        before = revision.current()
        result = action()
        self.assertGreater(revision.current(), before)
        return result

    def test_task_create_update_delete(self):
        task = self.assertBumps(lambda: task_model.create_task({"title": "Livrer la paie"}))
        self.assertBumps(lambda: task_model.update_task(task["id"], {"title": "Livrer la paie v2"}))
        self.assertBumps(lambda: task_model.delete_task(task["id"]))

    def test_event_create_update_delete(self):
        event = self.assertBumps(lambda: event_model.create_event({
            "title": "Atelier stocks",
            "starts_at": "2026-11-10 09:00",
            "ends_at": "2026-11-10 10:00",
        }))
        self.assertBumps(lambda: event_model.update_event(event["id"], {"title": "Atelier achats"}))
        self.assertBumps(lambda: event_model.delete_event(event["id"]))

    def test_capture_create_update_delete(self):
        capture = self.assertBumps(lambda: capture_model.create_capture("Rappeler Lucie"))
        self.assertBumps(lambda: capture_model.set_status(capture["id"], "done"))

        def delete():
            db = get_db()
            db.execute("DELETE FROM capture WHERE id = ?", (capture["id"],))
            db.commit()

        self.assertBumps(delete)

    def test_person_create_update_delete(self):
        person = self.assertBumps(lambda: person_model.create_person({"name": "Lucie Bernard"}))
        self.assertBumps(lambda: person_model.update_person(person["id"], {"role": "Chef comptable"}))
        self.assertBumps(lambda: person_model.delete_person(person["id"]))

    def test_note_create_update_delete(self):
        note = self.assertBumps(lambda: knowledge.create({"title": "Valorisation FIFO", "content": "x"}))
        self.assertBumps(lambda: knowledge.update(note["id"], {"content": "Procedure complete"}))
        self.assertBumps(lambda: knowledge.delete(note["id"]))

    def test_journal_entry(self):
        task = task_model.create_task({"title": "Cadrer les stocks"})
        entry = self.assertBumps(lambda: journal.add_entry("Decision : FIFO", task_id=task["id"]))
        self.assertBumps(lambda: journal.delete_entry(entry["id"]))

    def test_setting_does_not_bump(self):
        before = revision.current()
        setting.set_value("display_name", "Jean")
        setting.set_value("heartbeat_last_morning", "2026-11-09")
        self.assertEqual(revision.current(), before)

    def test_triggers_idempotent(self):
        init_schema()
        init_schema()
        names = [
            row["name"]
            for row in get_db().execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name GLOB 'revision_*'"
            )
        ]
        self.assertEqual(len(names), 3 * len(revision.TRACKED_TABLES))
        self.assertEqual(len(names), len(set(names)))
        # Une seule ligne de revision, et une ecriture n'incremente qu'une fois.
        self.assertEqual(get_db().execute("SELECT COUNT(*) FROM data_revision").fetchone()[0], 1)
        before = revision.current()
        task_model.create_task({"title": "Une fois"})
        self.assertEqual(revision.current(), before + 1)

    def test_untracked_table_trigger_removed(self):
        db = get_db()
        db.execute(
            "CREATE TRIGGER revision_usage_ai AFTER INSERT ON usage"
            " BEGIN UPDATE data_revision SET rev = rev + 1 WHERE id = 1; END"
        )
        db.commit()
        init_schema()
        self.assertIsNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'revision_usage_ai'"
        ).fetchone())


class RevisionRouteTest(NomaiaTestCase):
    """/api/revision : lecture legere, reservee a l'interface."""

    def test_route_returns_revision(self):
        client = self.client()
        first = client.get("/api/revision")
        self.assertEqual(first.status_code, 200)
        task_model.create_task({"title": "Nouvelle"})
        second = client.get("/api/revision").get_json()
        self.assertGreater(second["rev"], first.get_json()["rev"])

    def test_route_refused_without_access(self):
        response = self.app.test_client().get("/api/revision")
        self.assertEqual(response.status_code, 401)

    def test_route_refused_with_agent_key(self):
        agent_key = setting.regenerate_agent_key()
        response = self.app.test_client().get(
            "/api/revision", headers={"Authorization": "Bearer " + agent_key}
        )
        self.assertEqual(response.status_code, 401)
