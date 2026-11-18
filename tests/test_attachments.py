"""Lot 7 : pieces jointes (depot, ouverture sure, suppression, nettoyage)."""
import io

from models import attachment as attachment_model
from models import get_db
from tests.base import NomaiaTestCase


class PiecesJointesTest(NomaiaTestCase):
    def setUp(self):
        super().setUp()
        db = get_db()
        self.task = db.execute("INSERT INTO task (title, kind) VALUES ('PV de recette', 'deliverable')").lastrowid
        db.commit()
        self.http = self.client()
        self.h = self.write_headers()

    def upload(self, name, data, target="task", target_id=None, mimetype=None):
        return self.http.post("/api/attachments", headers=self.h, content_type="multipart/form-data", data={
            "target": target, "id": str(target_id or self.task),
            "file": (io.BytesIO(data), name, mimetype or "application/octet-stream"),
        })

    def test_depot_liste_et_ouverture(self):
        r = self.upload("PV signé.pdf", b"%PDF-1.4 test", mimetype="application/pdf")
        self.assertEqual(r.status_code, 201)
        item = r.get_json()
        self.assertEqual((item["filename"], item["size"]), ("PV signé.pdf", 13))
        self.assertNotIn("stored_name", item)
        listed = self.http.get(f"/api/attachments?target=task&id={self.task}").get_json()["attachments"]
        self.assertEqual([a["id"] for a in listed], [item["id"]])
        fichier = self.http.get(f"/api/attachments/{item['id']}/file")
        self.assertEqual(fichier.data, b"%PDF-1.4 test")
        self.assertIn("inline", fichier.headers.get("Content-Disposition", "inline"))
        self.assertEqual(fichier.headers["X-Content-Type-Options"], "nosniff")

    def test_html_et_svg_jamais_affiches(self):
        for name, mime in (("piege.html", "text/html"), ("logo.svg", "image/svg+xml")):
            with self.subTest(name=name):
                item = self.upload(name, b"<script>alert(1)</script>", mimetype=mime).get_json()
                r = self.http.get(f"/api/attachments/{item['id']}/file")
                self.assertIn("attachment", r.headers["Content-Disposition"])
                self.assertEqual(r.mimetype, "application/octet-stream")

    def test_nom_de_fichier_nettoye_et_cible_verifiee(self):
        item = self.upload("../../windows/system32/evil.exe", b"x").get_json()
        self.assertEqual(item["filename"], "evil.exe")
        stored = attachment_model.get(item["id"])["stored_name"]
        self.assertTrue((attachment_model.folder() / stored).is_file())
        self.assertEqual(self.upload("a.txt", b"x", target_id=99999).status_code, 400)
        self.assertEqual(self.upload("a.txt", b"x", target="user").status_code, 400)
        self.assertEqual(self.upload("vide.txt", b"").status_code, 400)

    def test_trop_lourd(self):
        r = self.upload("gros.bin", b"x" * (attachment_model.MAX_BYTES + 1))
        self.assertIn(r.status_code, (400, 413))

    def test_suppression_de_la_cible_et_nettoyage(self):
        item = self.upload("a.txt", b"bonjour").get_json()
        path = attachment_model.folder() / attachment_model.get(item["id"])["stored_name"]
        db = get_db()
        db.execute("DELETE FROM task WHERE id = ?", (self.task,))
        db.commit()
        self.assertIsNone(attachment_model.get(item["id"]))  # ligne emportee par le trigger
        self.assertEqual(attachment_model.sweep_orphans(), 1)
        self.assertFalse(path.exists())

    def test_suppression_directe(self):
        item = self.upload("a.txt", b"bonjour").get_json()
        self.assertEqual(self.http.delete(f"/api/attachments/{item['id']}", headers=self.h).status_code, 200)
        self.assertEqual(self.http.get(f"/api/attachments/{item['id']}/file").status_code, 404)


class InterfacePiecesJointesTest(NomaiaTestCase):
    def test_composant_charge_et_monte(self):
        pid = get_db().execute("INSERT INTO project (name) VALUES ('Docs')").lastrowid
        get_db().commit()
        client = self.client()
        projet = client.get(f"/projets/{pid}").get_data(as_text=True)
        self.assertIn("js/attachments.js", projet)
        self.assertIn('id="pj-attachments"', projet)
        self.assertIn(f'"project", {pid}', projet)
        self.assertIn('id="td-attachments"', client.get("/plan").get_data(as_text=True))
