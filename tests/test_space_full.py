"""Separation complete des espaces pro et perso (lot 3), facon multi-societe d'Odoo.

Chaque donnee porte un espace ; la requete voit le perimetre du selecteur (ou
de la cle de la session Claude) ; « Tout » montre les deux ; contacts et notes
admettent « commun ». Le plan du jour est calcule sur tout puis masque.
"""
import sqlite3
from datetime import date, datetime, timedelta

import config
from models import _migrate, get_db, setting
from models import capture as capture_model
from models import chat as chat_model
from models import event as event_model
from models import journal
from models import knowledge
from models import minutes as minutes_model
from models import person as person_model
from models import project as project_model
from models import space as space_model
from models import stats
from models import task as task_model
from services import planner, review, search as search_service, triage
from tests.base import NomaiaTestCase

DAY = date(2026, 9, 16)  # mercredi


class MigrationTest(NomaiaTestCase):
    """La reprise range l'existant sans rien perdre ni recalculer deux fois."""

    def _ancienne_base(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE project (id INTEGER PRIMARY KEY, name TEXT, space TEXT NOT NULL DEFAULT 'pro');"
            "CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT, project_id INTEGER, capture_id INTEGER,"
            " space TEXT NOT NULL DEFAULT 'pro');"
            "CREATE TABLE capture (id INTEGER PRIMARY KEY);"
            "CREATE TABLE conversation (id INTEGER PRIMARY KEY);"
            "CREATE TABLE event (id INTEGER PRIMARY KEY, project_id INTEGER);"
            "CREATE TABLE calendar_source (id INTEGER PRIMARY KEY);"
            "CREATE TABLE note (id INTEGER PRIMARY KEY, task_id INTEGER, project_id INTEGER);"
            "CREATE TABLE minutes (id INTEGER PRIMARY KEY, project_id INTEGER, event_id INTEGER);"
            "CREATE TABLE waiting (id INTEGER PRIMARY KEY, person_id INTEGER, task_id INTEGER);"
            "CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE person_project (person_id INTEGER, project_id INTEGER);"
            "CREATE TABLE kb_note (id INTEGER PRIMARY KEY, project_id INTEGER, source_task_id INTEGER);"
            "CREATE TABLE review (id INTEGER PRIMARY KEY);"
            "INSERT INTO project (id, name, space) VALUES (1, 'ALPHA', 'pro'), (2, 'NOMAIA', 'perso');"
            "INSERT INTO capture (id) VALUES (1), (2);"
            "INSERT INTO task (id, title, project_id, capture_id, space) VALUES"
            " (1, 'Paie', 1, 1, 'pro'), (2, 'Selecteur', 2, 2, 'perso'), (3, 'Libre', NULL, NULL, 'perso');"
            "INSERT INTO event (id, project_id) VALUES (1, 2), (2, NULL);"
            "INSERT INTO note (id, task_id, project_id) VALUES (1, 3, NULL), (2, NULL, 1);"
            "INSERT INTO minutes (id, project_id, event_id) VALUES (1, NULL, 1);"
            "INSERT INTO person (id, name) VALUES (1, 'Lucie'), (2, 'Paul'), (3, 'Marie');"
            "INSERT INTO person_project VALUES (1, 1), (2, 1), (2, 2);"
            "INSERT INTO waiting (id, person_id, task_id) VALUES (1, 1, NULL), (2, 3, 2);"
            "INSERT INTO kb_note (id, project_id, source_task_id) VALUES (1, NULL, NULL), (2, NULL, 2);"
        )
        return conn

    def _colonnes_lot3(self, conn):
        from models import MIGRATIONS

        for table, column, definition in MIGRATIONS:
            existantes = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column == "space" and column not in existantes:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def test_reprise_par_rattachement(self):
        from models import SPACE_BACKFILL, SPACE_BACKFILL_ORDER

        conn = self._ancienne_base()
        self._colonnes_lot3(conn)
        for table in SPACE_BACKFILL_ORDER:
            for sql in SPACE_BACKFILL[table]:
                conn.execute(sql)
        espace = lambda table, ident: conn.execute(  # noqa: E731
            f"SELECT space FROM {table} WHERE id = ?", (ident,)).fetchone()[0]
        self.assertEqual((espace("capture", 1), espace("capture", 2)), ("pro", "perso"))
        self.assertEqual((espace("event", 1), espace("event", 2)), ("perso", "pro"))
        self.assertEqual((espace("note", 1), espace("note", 2)), ("perso", "pro"))
        self.assertEqual(espace("minutes", 1), "perso")
        # Lucie : projets pro seulement ; Paul : les deux espaces ; Marie : aucun projet.
        self.assertEqual([espace("person", i) for i in (1, 2, 3)], ["pro", "commun", "commun"])
        # Attente sur une tache perso : perso ; attente sans tache d'un contact pro : pro.
        self.assertEqual((espace("waiting", 1), espace("waiting", 2)), ("pro", "perso"))
        self.assertEqual((espace("kb_note", 1), espace("kb_note", 2)), ("commun", "perso"))
        self.assertEqual(conn.execute("SELECT space FROM review").fetchall(), [])

    def test_reprise_une_seule_fois(self):
        # Base de test deja migree : une nouvelle passe ne recalcule rien.
        personne = person_model.create_person({"name": "Lucie", "space": "perso"})
        brute = sqlite3.connect(config.DB_PATH)
        _migrate(brute)
        brute.commit()
        brute.close()
        self.assertEqual(person_model.get_person(personne["id"])["space"], "perso")


class ScopeTest(NomaiaTestCase):
    """Le perimetre de la requete filtre toutes les listes ; hors requete, tout est visible."""

    def setUp(self):
        super().setUp()
        self.pro = project_model.create_project("ALPHA", space="pro")
        self.perso = project_model.create_project("NOMAIA", space="perso")
        self.lucie = person_model.create_person({"name": "Lucie", "project_id": self.pro["id"]})
        self.lea = person_model.create_person({"name": "Lea", "space": "perso"})
        self.paul = person_model.create_person({"name": "Paul", "space": "commun"})

    def _avec(self, espace):
        setting.set_value("space_filter", espace)
        return self.app.test_request_context("/")

    def test_contacts_communs_visibles_des_deux_cotes(self):
        with self._avec("perso"):
            self.assertEqual({p["name"] for p in person_model.list_people()}, {"Lea", "Paul"})
        with self._avec("pro"):
            self.assertEqual({p["name"] for p in person_model.list_people()}, {"Lucie", "Paul"})
        with self._avec("all"):
            self.assertEqual(len(person_model.list_people()), 3)
        self.assertEqual(len(person_model.list_people()), 3)  # hors requete

    def test_contact_de_l_autre_espace_jamais_rattache(self):
        # « Lucie » (pro) citee depuis le perso : un homonyme perso est cree.
        with self._avec("perso"):
            cite = person_model.get_or_create("Lucie")
        self.assertNotEqual(cite["id"], self.lucie["id"])
        self.assertEqual(cite["space"], "perso")
        # Un contact commun, lui, est retrouve des deux cotes.
        self.assertEqual(person_model.get_or_create("Paul", space="perso")["id"], self.paul["id"])

    def test_contact_cree_prend_l_espace_du_projet_ou_du_perimetre(self):
        self.assertEqual(self.lucie["space"], "pro")
        with self._avec("perso"):
            self.assertEqual(person_model.create_person({"name": "Zoe"})["space"], "perso")

    def test_captures_filtrees_et_creees_dans_le_perimetre(self):
        with self._avec("perso"):
            perso = capture_model.create_capture(raw_text="acheter du pain")
        with self._avec("pro"):
            pro = capture_model.create_capture(raw_text="relancer ALPHA")
            self.assertEqual([c["id"] for c in capture_model.list_captures()], [pro["id"]])
            self.assertEqual(capture_model.count_open_captures(), 1)
        self.assertEqual(perso["space"], "perso")

    def test_mode_tout_cree_dans_l_espace_de_creation(self):
        setting.set_value("create_space", "perso")
        with self._avec("all"):
            self.assertEqual(capture_model.create_capture(raw_text="x")["space"], "perso")
            self.assertEqual(task_model.create_task({"title": "Sans projet"})["space"], "perso")

    def test_capture_telegram_hors_requete_suit_le_selecteur(self):
        setting.set_value("space_filter", "perso")
        self.assertEqual(capture_model.create_capture(raw_text="x", source="telegram")["space"], "perso")
        self.assertEqual(capture_model.create_capture(raw_text="y", source="outlook", space="pro")["space"], "pro")

    def test_evenements_filtres_et_heritant_du_projet(self):
        debut = datetime(2026, 9, 16, 10, 0)
        evenement = event_model.create_event({
            "title": "Point NOMAIA", "project_id": self.perso["id"],
            "starts_at": debut.strftime("%Y-%m-%dT%H:%M"),
            "ends_at": (debut + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")})
        self.assertEqual(evenement["space"], "perso")
        with self._avec("pro"):
            self.assertEqual(event_model.list_for_day(DAY), [])
            # Le temps occupe reste global : une reunion perso prend aussi la journee.
            self.assertTrue(event_model.busy_intervals(DAY))

    def test_notes_et_comptes_rendus(self):
        commune = knowledge.create({"title": "Astuce Git", "space": "commun"})
        perso = knowledge.create({"title": "Domotique", "project_id": self.perso["id"]})
        cr = minutes_model.create({"title": "Point", "held_on": "2026-09-16", "project_id": self.pro["id"]})
        self.assertEqual((commune["space"], perso["space"], cr["space"]), ("commun", "perso", "pro"))
        with self._avec("pro"):
            self.assertEqual({n["title"] for n in knowledge.list_notes()}, {"Astuce Git"})
            self.assertEqual([m["id"] for m in minutes_model.list_minutes()], [cr["id"]])
        with self._avec("perso"):
            self.assertEqual(minutes_model.list_minutes(), [])

    def test_conversations_par_espace(self):
        with self._avec("perso"):
            perso = chat_model.create_conversation("Vacances")
        with self._avec("pro"):
            pro = chat_model.create_conversation("ALPHA")
            self.assertEqual([c["id"] for c in chat_model.list_conversations()], [pro["id"]])
            self.assertEqual(chat_model.current_id(), pro["id"])
        with self._avec("perso"):
            self.assertEqual(chat_model.current_id(), perso["id"])

    def test_changer_l_espace_d_un_projet_entraine_ses_donnees(self):
        tache = task_model.create_task({"title": "Paie", "project_id": self.pro["id"]})
        entree = journal.add_entry("fait", task_id=tache["id"])
        cr = minutes_model.create({"title": "Point", "held_on": "2026-09-16", "project_id": self.pro["id"]})
        project_model.update_project(self.pro["id"], {"space": "perso"})
        self.assertEqual(task_model.get_task(tache["id"])["space"], "perso")
        self.assertEqual(get_db().execute("SELECT space FROM note WHERE id = ?", (entree["id"],)).fetchone()[0], "perso")
        self.assertEqual(minutes_model.get(cr["id"])["space"], "perso")
        self.assertEqual(person_model.get_person(self.lucie["id"])["space"], "pro")  # contacts : inchanges

    def test_statistiques_et_bilan_suivent_le_perimetre(self):
        jour = date.today().isoformat()
        tache = task_model.create_task({"title": "Perso", "project_id": self.perso["id"]})
        self.complete_task(tache["id"], 30)
        with self._avec("pro"):
            self.assertEqual(stats.period_summary(jour, jour)["done"], 0)
            self.assertEqual(review._scope_key(), "pro")
        with self._avec("perso"):
            self.assertEqual(stats.period_summary(jour, jour)["tracked_min"], 30)
        self.assertEqual(stats.period_summary(jour, jour)["done"], 1)

    def test_recherche_ecarte_l_autre_espace(self):
        get_db().commit()
        with self._avec("pro"):
            noms = {r["label"] for r in search_service.search("Lea") if r["kind"] == "contact"}
            self.assertEqual(noms, set())
        with self._avec("perso"):
            noms = {r["label"] for r in search_service.search("Lea") if r["kind"] == "contact"}
            self.assertEqual(noms, {"Lea"})

    def test_api_interface_filtree(self):
        setting.set_value("space_filter", "perso")
        noms = {p["name"] for p in self.client().get("/api/people").get_json()["people"]}
        self.assertEqual(noms, {"Lea", "Paul"})


class TriageTest(NomaiaTestCase):
    """La capture suit ses taches quand elles tombent dans l'autre espace."""

    def test_capture_perso_suivie_vers_un_projet_pro(self):
        pro = project_model.create_project("ALPHA", space="pro")
        capture = capture_model.create_capture(raw_text="paie ALPHA", space="perso")
        proposition = capture_model.create_proposal(capture["id"], {"tasks": [{
            "title": "Lancer la paie", "description": "", "priority": 2, "estimate_min": 30,
            "deadline": None, "energy": "med", "project_hint": "ALPHA"}]})
        taches = triage.apply_proposal(proposition)
        self.assertEqual(taches[0]["project_id"], pro["id"])
        self.assertEqual(capture_model.get_capture(capture["id"])["space"], "pro")

    def test_tache_sans_projet_garde_l_espace_de_la_capture(self):
        capture = capture_model.create_capture(raw_text="pain", space="perso")
        proposition = capture_model.create_proposal(capture["id"], {"tasks": [{
            "title": "Acheter du pain", "description": "", "priority": 3, "estimate_min": 15,
            "deadline": None, "energy": "low", "project_hint": None}]})
        self.assertEqual(triage.apply_proposal(proposition)[0]["space"], "perso")


class PlanTest(NomaiaTestCase):
    """Plan calcule sur les deux espaces, masque a l'affichage, horaires perso optionnels."""

    def setUp(self):
        super().setUp()
        for cle, valeur in (("work_start", "08:00"), ("work_end", "12:00"), ("lunch_start", "12:00"),
                            ("lunch_end", "12:00"), ("plan_buffer_pct", "0"), ("plan_horizon_days", "3")):
            setting.set_value(cle, valeur)
        self.perso = task_model.create_task({"title": "Courses", "estimate_min": 60, "space": "perso"})
        self.pro = task_model.create_task({"title": "Paie", "estimate_min": 60, "space": "pro"})

    def test_plan_masque_l_autre_espace(self):
        setting.set_value("space_filter", "pro")
        plan = self.client().get("/api/plan").get_json()
        titres = [slot["task"]["title"] for slot in plan["slots"]]
        self.assertIn("Paie", titres)
        self.assertIn("Occupé · Perso", titres)
        self.assertNotIn("Courses", titres)
        masque = next(slot for slot in plan["slots"] if slot.get("masked"))
        self.assertIsNone(masque["task"]["id"])

    def test_plan_en_mode_tout_complet(self):
        plan = self.client().get("/api/plan").get_json()
        self.assertEqual({slot["task"]["title"] for slot in plan["slots"]}, {"Paie", "Courses"})

    def test_horaires_perso(self):
        setting.set_value("perso_hours_enabled", "1")
        setting.set_value("perso_start", "19:00")
        setting.set_value("perso_end", "21:00")
        plan = planner.build_plan(DAY)
        par_titre = {slot["task"]["title"]: slot for slot in plan["slots"]}
        self.assertGreaterEqual(par_titre["Courses"]["start"], "19:00")
        self.assertLess(par_titre["Paie"]["start"], "12:00")

    def test_jour_passe_masque_aussi(self):
        hier = date.today() - timedelta(days=1)
        self.complete_task(self.perso["id"], 30, done_at=f"{hier.isoformat()} 10:00:00")
        setting.set_value("space_filter", "pro")
        jour = self.client().get(f"/api/plan?date={hier.isoformat()}").get_json()
        self.assertNotIn(self.perso["id"], [tache["id"] for tache in jour["done"]])

    def test_horaires_perso_coupes_par_defaut(self):
        plan = planner.build_plan(DAY)
        self.assertTrue(all(slot["start"] < "12:00" for slot in plan["slots"]))


class AgentTest(NomaiaTestCase):
    """La cle perso ne voit plus les contacts, notes et comptes rendus pro."""

    def setUp(self):
        super().setUp()
        self.cle_perso = setting.regenerate_agent_key("perso")
        self.pro = project_model.create_project("ALPHA", space="pro")
        person_model.create_person({"name": "Lucie", "project_id": self.pro["id"]})
        person_model.create_person({"name": "Paul", "space": "commun"})
        self.note_pro = knowledge.create({"title": "Paie", "project_id": self.pro["id"]})

    def _appel(self, chemin):
        return self.app.test_client().get(
            "/api/agent" + chemin, headers={"Authorization": "Bearer " + self.cle_perso})

    def test_contacts_perso_et_communs(self):
        noms = {p["name"] for p in self._appel("/people").get_json()["people"]}
        self.assertEqual(noms, {"Paul"})

    def test_note_pro_introuvable(self):
        self.assertEqual(self._appel(f"/notes/{self.note_pro['id']}").status_code, 400)

    def test_espaces_valides(self):
        self.assertEqual(space_model.clean("Commun", shared=True), "commun")
        with self.assertRaises(space_model.SpaceError):
            space_model.clean("commun")
