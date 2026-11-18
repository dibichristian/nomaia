"""Boite de reception : compteur des entrees a traiter, hygiene des propositions, liste."""
from models import capture as capture_model
from models import get_db, init_schema
from services import notifications
from tests.base import NomaiaTestCase


def _capture(texte, statut="new"):
    """Cree une capture et lui donne un statut."""
    capture = capture_model.create_capture(raw_text=texte)
    if statut != "new":
        capture_model.set_status(capture["id"], statut)
    return capture


def _statuts_propositions(capture_id):
    """Statuts des propositions d'une capture, de la plus ancienne a la plus recente."""
    rows = get_db().execute(
        "SELECT status FROM proposal WHERE capture_id = ? ORDER BY id", (capture_id,)
    ).fetchall()
    return [row["status"] for row in rows]


class CompteurTest(NomaiaTestCase):
    def test_compteur_compte_les_captures_non_closes(self):
        for statut in ("new", "processing", "waiting", "failed", "done", "done"):
            _capture("entree " + statut, statut)
        self.assertEqual(capture_model.count_open_captures(), 4)

    def test_statut_et_cloche_comptent_les_entrees_a_traiter(self):
        client = self.client()
        self.assertEqual(client.get("/api/status").get_json()["inbox_pending"], 0)
        ouverte = _capture("a trier", "waiting")
        close = _capture("deja vue", "done")
        # Des propositions en attente sur une capture close ne comptent pas.
        capture_model.create_proposal(ouverte["id"], {"summary": "a"})
        get_db().execute(
            "INSERT INTO proposal (capture_id, payload_json) VALUES (?, '{}')", (close["id"],)
        )
        get_db().commit()

        self.assertEqual(client.get("/api/status").get_json()["inbox_pending"], 1)
        textes = [item["text"] for item in notifications.current()]
        self.assertIn("1 entrée à traiter", textes)

        _capture("autre", "failed")
        textes = [item["text"] for item in notifications.current()]
        self.assertIn("2 entrées à traiter", textes)

    def test_cloche_muette_sans_entree_a_traiter(self):
        _capture("deja vue", "done")
        textes = [item["text"] for item in notifications.current()]
        self.assertFalse(any("à traiter" in texte for texte in textes))


class HygienePropositionsTest(NomaiaTestCase):
    def test_capture_close_rejette_ses_propositions_en_attente(self):
        capture = _capture("facture")
        capture_model.create_proposal(capture["id"], {"summary": "x"})
        capture_model.set_status(capture["id"], "done")
        self.assertEqual(_statuts_propositions(capture["id"]), ["rejected"])

    def test_capture_close_garde_la_proposition_appliquee(self):
        capture = _capture("facture")
        proposition = capture_model.create_proposal(capture["id"], {"summary": "x"})
        capture_model.set_proposal_status(proposition["id"], "applied")
        capture_model.set_status(capture["id"], "done")
        self.assertEqual(_statuts_propositions(capture["id"]), ["applied"])

    def test_autre_statut_ne_touche_pas_aux_propositions(self):
        capture = _capture("facture")
        capture_model.create_proposal(capture["id"], {"summary": "x"})
        capture_model.set_status(capture["id"], "waiting")
        self.assertEqual(_statuts_propositions(capture["id"]), ["pending"])

    def test_nouvelle_proposition_rejette_les_precedentes(self):
        capture = _capture("facture")
        voisine = _capture("autre")
        capture_model.create_proposal(capture["id"], {"summary": "v1"})
        capture_model.create_proposal(voisine["id"], {"summary": "voisine"})
        capture_model.create_proposal(capture["id"], {"summary": "v2"})
        derniere = capture_model.create_proposal(capture["id"], {"summary": "v3"})

        self.assertEqual(_statuts_propositions(capture["id"]), ["rejected", "rejected", "pending"])
        self.assertEqual(_statuts_propositions(voisine["id"]), ["pending"])
        self.assertEqual(capture_model.latest_proposal(capture["id"])["id"], derniere["id"])

    def test_migration_rejette_les_propositions_des_captures_closes(self):
        db = get_db()
        close = db.execute("INSERT INTO capture (raw_text, status) VALUES ('a', 'done')").lastrowid
        ouverte = db.execute("INSERT INTO capture (raw_text, status) VALUES ('b', 'waiting')").lastrowid
        for capture_id, statut in ((close, "pending"), (close, "applied"), (ouverte, "pending")):
            db.execute(
                "INSERT INTO proposal (capture_id, payload_json, status) VALUES (?, '{}', ?)",
                (capture_id, statut),
            )
        db.commit()

        init_schema()
        init_schema()  # idempotente

        self.assertEqual(_statuts_propositions(close), ["rejected", "applied"])
        self.assertEqual(_statuts_propositions(ouverte), ["pending"])


class ListeApiTest(NomaiaTestCase):
    def test_liste_limitee_avec_suite(self):
        for numero in range(5):
            _capture(f"entree {numero}")
        client = self.client()

        data = client.get("/api/capture?limit=3").get_json()
        self.assertEqual(len(data["captures"]), 3)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["limit"], 3)
        self.assertEqual(data["captures"][0]["raw_text"], "entree 4")

        data = client.get("/api/capture?limit=6").get_json()
        self.assertEqual(len(data["captures"]), 5)
        self.assertFalse(data["has_more"])

    def test_limite_par_defaut_et_plafond(self):
        client = self.client()
        self.assertEqual(client.get("/api/capture").get_json()["limit"], 30)
        self.assertEqual(client.get("/api/capture?limit=abc").get_json()["limit"], 30)
        self.assertEqual(client.get("/api/capture?limit=100000").get_json()["limit"], 300)
        self.assertEqual(client.get("/api/capture?limit=0").get_json()["limit"], 1)

    def test_filtre_a_traiter_voit_les_anciennes_entrees(self):
        ancienne = _capture("ancienne a trier", "waiting")
        for numero in range(4):
            _capture(f"close {numero}", "done")
        client = self.client()

        data = client.get("/api/capture?filter=open&limit=2").get_json()
        self.assertEqual([item["id"] for item in data["captures"]], [ancienne["id"]])
        self.assertFalse(data["has_more"])
        self.assertEqual(data["open_count"], 1)

        tout = client.get("/api/capture?filter=all&limit=2").get_json()
        self.assertEqual(len(tout["captures"]), 2)
        self.assertTrue(tout["has_more"])
        self.assertEqual(tout["open_count"], 1)

    def test_liste_porte_la_derniere_proposition(self):
        avec = _capture("avec")
        sans = _capture("sans")
        capture_model.create_proposal(avec["id"], {"summary": "v1"})
        derniere = capture_model.create_proposal(avec["id"], {"summary": "v2"})

        data = self.client().get("/api/capture").get_json()
        par_id = {item["id"]: item for item in data["captures"]}
        self.assertEqual(par_id[avec["id"]]["proposal"]["id"], derniere["id"])
        self.assertEqual(par_id[avec["id"]]["proposal"]["payload"]["summary"], "v2")
        self.assertIsNone(par_id[sans["id"]]["proposal"])

    def test_capture_close_ne_propose_plus_de_validation(self):
        capture = _capture("facture", "waiting")
        capture_model.create_proposal(capture["id"], {"summary": "x", "tasks": [{"title": "t"}]})
        capture_model.set_status(capture["id"], "done")

        fil = self.client().get(f"/api/capture/{capture['id']}").get_json()
        self.assertNotEqual(fil["proposal"]["status"], "pending")


class ClasserSansSuiteTest(NomaiaTestCase):
    """Fermer une entree sans la valider."""

    def test_entree_en_attente_classee_sans_suite(self):
        capture = capture_model.create_capture(raw_text="Bonjour NOMAIA")
        capture_model.create_proposal(capture["id"], {"summary": "salutation", "confidence": 0.2})
        capture_model.set_status(capture["id"], "waiting")
        reponse = self.client().post(f"/api/capture/{capture['id']}/dismiss", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(capture_model.get_capture(capture["id"])["status"], "done")
        self.assertEqual(capture_model.latest_proposal(capture["id"])["status"], "rejected")
        self.assertEqual(capture_model.count_open_captures(), 0)
        self.assertIn("Classée sans suite.", [m["content"] for m in reponse.get_json()["messages"]])

    def test_entree_deja_traitee_ou_en_analyse_refusee(self):
        capture = capture_model.create_capture(raw_text="x")
        h = self.write_headers()
        self.assertEqual(self.client().post(f"/api/capture/{capture['id']}/dismiss", headers=h).status_code, 409)
        self.assertEqual(self.client().post("/api/capture/999/dismiss", headers=h).status_code, 404)
