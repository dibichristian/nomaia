"""Tests des mails transferes a la main : lecture, depot, triage sous controle.

NOMAIA n'a aucun acces a la boite : chaque mail est choisi et depose par
l'utilisateur. Ce qui en sort reste une proposition, jamais une creation directe.
"""
import io
from email.message import EmailMessage
from unittest.mock import patch

from models import capture as capture_model
from models import setting
from models import task as task_model
from services import llm, mail_parse, triage
from tests.base import NomaiaTestCase
from tests.test_telegram_chat import BridgeEspion


def fabriquer_eml(corps="Bonjour, peux-tu livrer le devis vendredi ?", html=None,
                  objet="Devis ALPHA", piece_jointe=False):
    """Construit un .eml en memoire et retourne ses octets."""
    message = EmailMessage()
    message["From"] = "Lucie Bernard <lucie@alpha.example>"
    message["To"] = "jean.dupont@example.com"
    message["Date"] = "Mon, 05 Oct 2026 09:12:00 +0000"
    message["Subject"] = objet
    if corps is not None:
        message.set_content(corps)
        if html:
            message.add_alternative(html, subtype="html")
    elif html:
        message.set_content(html, subtype="html")
    if piece_jointe:
        message.add_attachment(b"%PDF-1.4 secret", maintype="application",
                               subtype="pdf", filename="contrat.pdf")
    return message.as_bytes()


def proposition(confiance=0.95):
    """Sortie de triage avec une tache, a la confiance donnee."""
    return {
        "summary": "Devis demande par ALPHA",
        "confidence": confiance,
        "reply": "",
        "questions": [],
        "tasks": [{"title": "Livrer le devis ALPHA", "priority": 2, "estimate_min": 60}],
        "waiting": [],
    }


class MailParseTest(NomaiaTestCase):
    """Lecture d'un .eml et mise en forme pour le triage."""

    def test_texte_brut_et_entetes(self):
        mail = mail_parse.parse_eml(fabriquer_eml())
        self.assertEqual(mail["subject"], "Devis ALPHA")
        self.assertIn("lucie@alpha.example", mail["sender"])
        self.assertEqual(mail["date"], "2026-10-05 09:12")
        self.assertIn("livrer le devis", mail["body"])

    def test_texte_brut_prefere_au_html(self):
        mail = mail_parse.parse_eml(fabriquer_eml(html="<p>version <b>HTML</b></p>"))
        self.assertNotIn("HTML", mail["body"])

    def test_html_seul_converti_en_texte(self):
        html = ("<html><head><style>p{color:red}</style></head><body>"
                "<p>Bonjour&nbsp;Jean</p><p>Merci de valider</p>"
                "<script>alert(1)</script></body></html>")
        mail = mail_parse.parse_eml(fabriquer_eml(corps=None, html=html))
        self.assertIn("Bonjour\xa0Jean", mail["body"])
        self.assertIn("Merci de valider", mail["body"])
        self.assertNotIn("color", mail["body"])
        self.assertNotIn("alert", mail["body"])

    def test_objet_encode_et_accents(self):
        mail = mail_parse.parse_eml(fabriquer_eml(objet="Réunion d'été", corps="Échéance déplacée"))
        self.assertEqual(mail["subject"], "Réunion d'été")
        self.assertIn("Échéance", mail["body"])

    def test_piece_jointe_ignoree(self):
        mail = mail_parse.parse_eml(fabriquer_eml(piece_jointe=True))
        self.assertNotIn("PDF", mail["body"])
        self.assertIn("livrer le devis", mail["body"])

    def test_corps_trop_long_coupe(self):
        mail = mail_parse.parse_eml(fabriquer_eml(corps="x" * (mail_parse.MAX_BODY_CHARS + 500)))
        self.assertLessEqual(len(mail["body"]), mail_parse.MAX_BODY_CHARS + 40)
        self.assertIn("coupee", mail["body"])

    def test_fichier_vide_ou_illisible_refuse(self):
        with self.assertRaises(ValueError):
            mail_parse.parse_eml(b"")
        with self.assertRaises(ValueError):
            mail_parse.parse_eml(b"\r\n\r\n")

    def test_fichier_trop_lourd_refuse(self):
        with self.assertRaises(ValueError):
            mail_parse.parse_eml(b"x" * (mail_parse.MAX_EML_BYTES + 1))

    def test_capture_text_porte_la_note(self):
        texte = mail_parse.capture_text(mail_parse.parse_eml(fabriquer_eml()), "client prioritaire")
        self.assertTrue(texte.startswith("NOTE DE L'UTILISATEUR : client prioritaire"))
        self.assertIn("Objet : Devis ALPHA", texte)

    def test_texte_colle_vide_refuse(self):
        with self.assertRaises(ValueError):
            mail_parse.pasted_text("   ")
        self.assertIn("MAIL TRANSFERE", mail_parse.pasted_text("Bonjour"))

    def test_reconnaissance_du_format(self):
        self.assertTrue(mail_parse.is_eml("Devis.EML", None))
        self.assertTrue(mail_parse.is_eml(None, "message/rfc822"))
        self.assertFalse(mail_parse.is_eml("photo.png", "image/png"))


class MailTriageTest(NomaiaTestCase):
    """Un mail ne cree jamais rien sans validation."""

    def _trier(self, source, confiance):
        capture = capture_model.create_capture(raw_text="MAIL TRANSFERE\n\nDevis", source=source)
        with patch.object(llm, "ask", return_value=proposition(confiance)) as faux:
            issue = triage.triage_capture(capture_model.get_capture(capture["id"]))
        return issue, faux.call_args.args[0]

    def test_mail_jamais_applique_automatiquement(self):
        issue, _ = self._trier("email", 0.99)
        self.assertEqual(issue["outcome"], "review")
        self.assertEqual(task_model.list_tasks(status="todo"), [])

    def test_meme_confiance_appliquee_hors_mail(self):
        issue, _ = self._trier("web", 0.99)
        self.assertEqual(issue["outcome"], "applied")

    def test_consigne_mail_seulement_pour_les_mails(self):
        _, prompt_mail = self._trier("email", 0.5)
        _, prompt_web = self._trier("web", 0.5)
        self.assertIn("CONSIGNE MAIL", prompt_mail)
        self.assertNotIn("CONSIGNE MAIL", prompt_web)


class MailApiTest(NomaiaTestCase):
    """Depot depuis l'interface web."""

    def test_depot_eml(self):
        reponse = self.client().post(
            "/api/capture/eml",
            data={"eml": (io.BytesIO(fabriquer_eml()), "devis.eml"), "text": "urgent"},
            content_type="multipart/form-data",
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 201, reponse.get_json())
        capture = reponse.get_json()
        self.assertEqual(capture["source"], "email")
        self.assertIn("Objet : Devis ALPHA", capture["raw_text"])
        self.assertIn("NOTE DE L'UTILISATEUR : urgent", capture["raw_text"])

    def test_autre_fichier_refuse(self):
        reponse = self.client().post(
            "/api/capture/eml",
            data={"eml": (io.BytesIO(b"PNG"), "image.png")},
            content_type="multipart/form-data",
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)

    def test_commande_mail_dans_le_texte(self):
        reponse = self.client().post(
            "/api/capture", json={"text": "/mail Bonjour, le devis ?"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 201)
        capture = reponse.get_json()
        self.assertEqual(capture["source"], "email")
        self.assertIn("Bonjour, le devis ?", capture["raw_text"])

    def test_mot_proche_de_mail_reste_une_note(self):
        reponse = self.client().post(
            "/api/capture", json={"text": "/mailing a preparer"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["source"], "web")


class BridgeMail(BridgeEspion):
    """Passerelle espion qui sert un .eml au lieu de le telecharger."""

    def __init__(self, app, contenu=None):
        super().__init__(app)
        self.contenu = contenu if contenu is not None else fabriquer_eml()

    def _fetch_file(self, file_id, max_bytes):
        return "documents/file.eml", self.contenu


class MailTelegramTest(NomaiaTestCase):
    """Depot depuis Telegram et retour de l'analyse."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "42")
        setting.set_value("telegram_chat_mode", "1")

    def _document(self, legende=None):
        message = {"chat": {"id": 42}, "document": {
            "file_id": "d1", "file_name": "Devis.eml", "mime_type": "message/rfc822"}}
        if legende:
            message["caption"] = legende
        return message

    def test_eml_devient_capture_mail(self):
        bridge = BridgeMail(self.app)
        bridge._handle(self._document("client prioritaire"))
        capture = capture_model.list_captures()[0]
        self.assertEqual(capture["source"], "email")
        self.assertIn("NOTE DE L'UTILISATEUR : client prioritaire", capture["raw_text"])
        self.assertIn("Devis ALPHA", bridge.dernier())

    def test_eml_illisible_signale(self):
        bridge = BridgeMail(self.app, contenu=b"")
        bridge._handle(self._document())
        self.assertEqual(capture_model.list_captures(), [])
        self.assertIn("Mail non lu", bridge.dernier())

    def test_commande_mail(self):
        bridge = BridgeMail(self.app)
        bridge._handle({"chat": {"id": 42}, "text": "/mail Bonjour, ou en est le devis ?"})
        capture = capture_model.list_captures()[0]
        self.assertEqual(capture["source"], "email")
        self.assertIn("ou en est le devis", capture["raw_text"])

    def test_commande_mail_vide(self):
        bridge = BridgeMail(self.app)
        bridge._handle({"chat": {"id": 42}, "text": "/mail"})
        self.assertEqual(capture_model.list_captures(), [])
        self.assertIn("/mail", bridge.dernier())

    def test_mail_depose_sur_le_web_notifie_sur_telegram(self):
        bridge = BridgeMail(self.app)
        capture = capture_model.create_capture(raw_text="MAIL TRANSFERE\n\nDevis", source="email")
        proposal = capture_model.create_proposal(capture["id"], triage.normalize_payload(proposition()))
        bridge.notify_result(capture, {"outcome": "review", "proposal": proposal, "tasks": []})
        self.assertIn("rien n'est cree sans toi", bridge.dernier())

    def test_capture_web_ordinaire_non_notifiee(self):
        bridge = BridgeMail(self.app)
        capture = capture_model.create_capture(raw_text="note", source="web")
        proposal = capture_model.create_proposal(capture["id"], triage.normalize_payload(proposition()))
        bridge.notify_result(capture, {"outcome": "review", "proposal": proposal, "tasks": []})
        self.assertEqual(bridge.envois, [])
