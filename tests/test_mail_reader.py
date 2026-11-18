"""Tests de la lecture des mails Outlook par Claude Code (connecteur Microsoft 365).

Ni Claude Code ni Outlook ne sont joints : subprocess.run est remplace par un
faux qui rend une sortie preparee et note les arguments recus.
"""
import json
import subprocess
from datetime import datetime, timedelta
from unittest.mock import patch

from models import capture as capture_model
from models import get_db
from models import person as person_model
from models import project as project_model
from models import setting
from models import task as task_model
from services import llm, mail_reader, providers, secrets_guard, triage
from tests.base import NomaiaTestCase
from tests.test_telegram_chat import BridgeEspion

OUTILS = (
    "mcp__claude_ai_Microsoft_365__outlook_email_search,"
    "mcp__claude_ai_Microsoft_365__read_resource"
)


def mail(message_id="m1", from_name="Lucie Bernard", from_email="lucie@alpha.example",
         subject="Devis", excerpt="Peux-tu livrer le devis vendredi ?"):
    """Mail tel que Claude Code le rend."""
    return {
        "message_id": message_id,
        "from_name": from_name,
        "from_email": from_email,
        "subject": subject,
        "received_at": "2026-11-09T09:12",
        "project_hint": None,
        "excerpt": excerpt,
    }


def enveloppe(mails=None, result=None, is_error=False):
    """Sortie JSON de `claude -p --output-format json`."""
    if result is None:
        result = json.dumps({"mails": mails or []})
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "result": result,
            "total_cost_usd": 0.12,
            "usage": {"input_tokens": 1200, "output_tokens": 300},
        }
    )


class FauxCli:
    """Remplace subprocess.run : rend une sortie fixe et note chaque appel."""

    def __init__(self, stdout="", returncode=0, stderr="", erreur=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.erreur = erreur
        self.appels = []

    def __call__(self, args, **kwargs):
        self.appels.append({"args": list(args), **kwargs})
        if self.erreur:
            raise self.erreur
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


class MailReaderCase(NomaiaTestCase):
    """Socle : un interlocuteur connu, un projet actif, Claude Code choisi."""

    def setUp(self):
        super().setUp()
        setting.set_value("llm_provider", "claude_cli")
        person_model.create_person({"name": "Lucie Bernard", "email": "lucie@alpha.example"})
        project_model.create_project("ALPHA")
        self._chemin = patch.object(providers, "claude_path", return_value="claude")
        self._chemin.start()

    def tearDown(self):
        self._chemin.stop()
        super().tearDown()

    def lire(self, faux, now=None):
        """Lance un passage avec le faux CLI donne."""
        with patch.object(mail_reader.subprocess, "run", faux):
            return mail_reader.read_mails(now=now)

    @staticmethod
    def outlook_captures():
        return [c for c in capture_model.list_captures(limit=100) if c["source"] == "outlook"]


class PassageNominalTest(MailReaderCase):
    """Un passage reussi depose les mails utiles, sans jamais rien appliquer."""

    def test_mails_deposes_en_source_outlook(self):
        faux = FauxCli(enveloppe([
            mail("m1"),
            mail("m2", from_name="Jean Dupont", from_email="jean@client.example",
                 subject="Point ALPHA demain", excerpt="On fait le point sur la paie ?"),
        ]))
        resultat = self.lire(faux)

        self.assertTrue(resultat["ok"])
        self.assertEqual(resultat["count"], 2)
        captures = self.outlook_captures()
        self.assertEqual(len(captures), 2)
        textes = " ".join(c["raw_text"] for c in captures)
        self.assertIn("De : Lucie Bernard <lucie@alpha.example>", textes)
        self.assertIn("Objet : Devis", textes)
        self.assertIn("Reçu : 2026-11-09T09:12", textes)
        self.assertIn("livrer le devis", textes)
        self.assertEqual(setting.get("mail_last_count"), "2")
        self.assertEqual(setting.get("mail_last_error"), "")
        self.assertTrue(setting.get("mail_last_seen"))

    def test_proposition_jamais_appliquee_automatiquement(self):
        self.lire(FauxCli(enveloppe([mail("m1")])))
        capture = self.outlook_captures()[0]
        sortie = {
            "summary": "Devis demande", "confidence": 0.99, "reply": "", "questions": [],
            "tasks": [{"title": "Livrer le devis ALPHA", "priority": 2, "estimate_min": 60}],
            "waiting": [],
        }
        with patch.object(llm, "ask", return_value=sortie):
            issue = triage.triage_capture(capture)

        self.assertEqual(issue["outcome"], "review")
        self.assertEqual(task_model.list_tasks(), [])

    def test_le_triage_traite_le_mail_comme_une_donnee(self):
        self.lire(FauxCli(enveloppe([mail("m1")])))
        prompt = triage.build_prompt(self.outlook_captures()[0])
        self.assertIn("CONSIGNE MAIL", prompt)

    def test_bloc_json_entoure_de_texte(self):
        texte = "Voici les mails :\n```json\n" + json.dumps({"mails": [mail("m1")]}) + "\n```"
        resultat = self.lire(FauxCli(enveloppe(result=texte)))
        self.assertEqual(resultat["count"], 1)

    def test_liste_nue_acceptee(self):
        resultat = self.lire(FauxCli(enveloppe(result=json.dumps([mail("m1")]))))
        self.assertEqual(resultat["count"], 1)

    def test_aucun_mail(self):
        resultat = self.lire(FauxCli(enveloppe([])))
        self.assertTrue(resultat["ok"])
        self.assertEqual(resultat["count"], 0)
        self.assertEqual(mail_reader.result_text(resultat), "Aucun nouveau mail utile.")

    def test_consommation_enregistree(self):
        self.lire(FauxCli(enveloppe([])))
        ligne = get_db().execute("SELECT * FROM usage WHERE purpose = 'mail'").fetchone()
        self.assertIsNotNone(ligne)
        self.assertEqual(ligne["provider"], "claude_cli")


class DedoublonnageTest(MailReaderCase):
    """Un mail deja vu n'est jamais redepose."""

    def test_meme_message_id_ignore_au_passage_suivant(self):
        faux = FauxCli(enveloppe([mail("m1")]))
        self.lire(faux)
        second = self.lire(faux)

        self.assertEqual(second["count"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.outlook_captures()), 1)

    def test_doublon_dans_un_meme_passage(self):
        resultat = self.lire(FauxCli(enveloppe([mail("m1"), mail("m1")])))
        self.assertEqual(resultat["count"], 1)

    def test_capture_supprimee_ne_revient_pas(self):
        self.lire(FauxCli(enveloppe([mail("m1")])))
        db = get_db()
        db.execute("DELETE FROM capture")
        db.commit()

        resultat = self.lire(FauxCli(enveloppe([mail("m1")])))
        self.assertEqual(resultat["count"], 0)

    def test_sans_identifiant_une_empreinte_sert_de_cle(self):
        sans_id = mail("")
        self.lire(FauxCli(enveloppe([sans_id])))
        resultat = self.lire(FauxCli(enveloppe([sans_id])))
        self.assertEqual(resultat["count"], 0)
        self.assertEqual(len(self.outlook_captures()), 1)


class FiltreTest(MailReaderCase):
    """Defense en profondeur : le tri demande a Claude est refait ici."""

    def test_inconnu_sans_projet_ecarte(self):
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="Inconnu", from_email="x@ailleurs.com",
                 subject="Promo", excerpt="Nos offres du mois."),
        ])))
        self.assertTrue(resultat["ok"])
        self.assertEqual(resultat["count"], 0)
        self.assertEqual(resultat["skipped"], 1)
        self.assertEqual(self.outlook_captures(), [])

    def test_contact_reconnu_par_son_alias(self):
        personne = person_model.find_by_name("Lucie Bernard")
        person_model.update_person(personne["id"], {"aliases": "Lucie K."})
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="lucie k.", from_email="lucie.k@autre.example"),
        ])))
        self.assertEqual(resultat["count"], 1)

    def test_adresse_reconnue_sans_tenir_compte_de_la_casse(self):
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="", from_email="LUCIE@ALPHA.EXAMPLE"),
        ])))
        self.assertEqual(resultat["count"], 1)

    def test_projet_cite_en_mot_entier_seulement(self):
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="Inconnu", from_email="x@ailleurs.com",
                 subject="ALPHAX", excerpt="Rien a voir."),
        ])))
        self.assertEqual(resultat["count"], 0)

    def test_projet_en_pause_ne_suffit_pas(self):
        projet = project_model.create_project("Brightjet")
        project_model.update_project(projet["id"], {"status": "paused"})
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="Inconnu", from_email="x@ailleurs.com",
                 subject="Brightjet", excerpt="Relance."),
        ])))
        self.assertEqual(resultat["count"], 0)

    def test_notification_automatique_ecartee(self):
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="Odoo", from_email="no-reply@odoo.com",
                 subject="ALPHA : nouvelle tache", excerpt="Notification."),
        ])))
        self.assertEqual(resultat["count"], 0)

    def test_mail_de_l_utilisateur_ecarte(self):
        setting.set_value("display_name", "Jean Dupont")
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", from_name="Jean Dupont", from_email="jean.dupont@example.com",
                 subject="ALPHA", excerpt="Mon propre mail."),
        ])))
        self.assertEqual(resultat["count"], 0)


class SecretTest(MailReaderCase):
    """Un secret dans un mail est masque, le mail est garde."""

    def test_secret_masque_dans_la_capture(self):
        resultat = self.lire(FauxCli(enveloppe([
            mail("m1", excerpt="Acces admin : password = Sup3rS3cret! a changer."),
        ])))
        self.assertEqual(resultat["count"], 1)
        texte = self.outlook_captures()[0]["raw_text"]
        self.assertNotIn("Sup3rS3cret", texte)
        self.assertIn("password = ***", texte)

    def test_masquage_des_differents_secrets(self):
        texte, compte = secrets_guard.mask_secrets(
            "cle sk-ant-api03-abcdefghijklmnopqrstuvwxyz\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n"
            "mdp: hunter22"
        )
        self.assertEqual(compte, 3)
        self.assertNotIn("abcdefghij", texte)
        self.assertNotIn("MIIEow", texte)
        self.assertNotIn("hunter22", texte)
        self.assertIsNone(secrets_guard.find_secret(texte))

    def test_valeur_de_remplacement_laissee_telle_quelle(self):
        texte, compte = secrets_guard.mask_secrets("password = *** ; token: ${TOKEN}")
        self.assertEqual(compte, 0)
        self.assertEqual(texte, "password = *** ; token: ${TOKEN}")


class EchecTest(MailReaderCase):
    """Un echec est enregistre, ne depose rien et ne fait pas avancer la fenetre."""

    def setUp(self):
        super().setUp()
        setting.set_value("mail_last_seen", "2026-11-09 08:00")

    def verifier_echec(self, faux, fragment):
        resultat = self.lire(faux)
        self.assertFalse(resultat["ok"])
        self.assertIn(fragment, resultat["error"])
        self.assertEqual(self.outlook_captures(), [])
        self.assertEqual(setting.get("mail_last_seen"), "2026-11-09 08:00")
        self.assertIn(fragment, setting.get("mail_last_error"))
        self.assertTrue(setting.get("mail_last_run"))
        self.assertEqual(setting.get("mail_last_count"), "0")

    def test_sortie_illisible(self):
        self.verifier_echec(FauxCli("pas du json"), "illisible")

    def test_reponse_en_prose(self):
        self.verifier_echec(FauxCli(enveloppe(result="Je n'ai pas acces a Outlook.")), "illisible")

    def test_connecteur_absent_signale_par_le_modele(self):
        self.verifier_echec(
            FauxCli(enveloppe(result='{"error": "connecteur Microsoft 365 absent"}')),
            "connecteur Microsoft 365 absent",
        )

    def test_code_retour_d_erreur(self):
        faux = FauxCli(json.dumps({"is_error": True, "result": "Session expiree"}), returncode=1)
        self.verifier_echec(faux, "Session expiree")

    def test_erreur_signalee_avec_code_zero(self):
        faux = FauxCli(enveloppe(result="Max turns", is_error=True))
        self.verifier_echec(faux, "Max turns")

    def test_delai_depasse(self):
        faux = FauxCli(erreur=subprocess.TimeoutExpired("claude", mail_reader.TIMEOUT_S))
        self.verifier_echec(faux, "n'a pas repondu")

    def test_lancement_impossible(self):
        self.verifier_echec(FauxCli(erreur=OSError("introuvable")), "Lancement")


class FournisseurTest(MailReaderCase):
    """Sans Claude Code, la lecture est refusee proprement."""

    def test_refus_si_fournisseur_non_claude_code(self):
        setting.set_value("llm_provider", "gemini")
        faux = FauxCli(enveloppe([mail("m1")]))
        resultat = self.lire(faux)

        self.assertFalse(resultat["ok"])
        self.assertTrue(resultat["refused"])
        self.assertIn("Claude Code", resultat["error"])
        self.assertEqual(faux.appels, [])
        self.assertEqual(self.outlook_captures(), [])

    def test_passage_deja_en_cours(self):
        mail_reader._lock.acquire()
        try:
            resultat = self.lire(FauxCli(enveloppe([])))
        finally:
            mail_reader._lock.release()
        self.assertTrue(resultat["busy"])


class LigneDeCommandeTest(MailReaderCase):
    """Arguments et environnement du sous-processus."""

    def test_arguments_du_cli(self):
        faux = FauxCli(enveloppe([]))
        self.lire(faux)
        args = faux.appels[0]["args"]

        self.assertNotIn("--strict-mcp-config", args)
        self.assertNotIn("--mcp-config", args)
        self.assertEqual(args.count("--allowedTools"), 1)
        self.assertEqual(args[args.index("--allowedTools") + 1], OUTILS)
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        self.assertEqual(args[args.index("--permission-mode") + 1], "default")
        self.assertEqual(args[:2], ["claude", "-p"])
        self.assertEqual(faux.appels[0]["timeout"], mail_reader.TIMEOUT_S)

    def test_dossier_de_configuration_transmis(self):
        setting.set_value("claude_config_dir", r"C:\Users\moi\.claude-pro")
        faux = FauxCli(enveloppe([]))
        self.lire(faux)
        self.assertEqual(faux.appels[0]["env"]["CLAUDE_CONFIG_DIR"], r"C:\Users\moi\.claude-pro")

    def test_prompt_porte_contacts_projets_et_fenetre(self):
        faux = FauxCli(enveloppe([]))
        maintenant = datetime(2026, 11, 9, 10, 0)
        self.lire(faux, now=maintenant)
        prompt = faux.appels[0]["input"]

        self.assertIn("lucie@alpha.example", prompt)
        self.assertIn("Lucie Bernard", prompt)
        self.assertIn("- ALPHA", prompt)
        # Premier passage : les dernieres 24 h.
        self.assertIn("2026-11-08T10:00", prompt)
        self.assertEqual(setting.get("mail_last_seen"), "2026-11-09 10:00")
        # Constate en reel : folderName="Inbox" ne trouve rien sur une boite en francais.
        self.assertIn("SANS filtre de dossier", prompt)

    def test_fenetre_reprend_au_dernier_passage(self):
        setting.set_value("mail_last_seen", "2026-11-09 09:00")
        faux = FauxCli(enveloppe([]))
        self.lire(faux, now=datetime(2026, 11, 9, 10, 0))
        self.assertIn("2026-11-09T08:45", faux.appels[0]["input"])


class DeclenchementTest(MailReaderCase):
    """Battement : toutes les heures, pendant les heures de travail seulement."""

    def setUp(self):
        super().setUp()
        setting.set_value("mail_enabled", "1")
        setting.set_value("work_start", "08:30")
        setting.set_value("work_end", "18:00")
        self.midi = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)

    def declencher(self, now):
        faux = FauxCli(enveloppe([mail("m1")]))
        with patch.object(mail_reader.subprocess, "run", faux):
            thread = mail_reader.read_due(self.app, now=now)
            if thread:
                thread.join(10)
        return thread, faux

    def test_hors_heures_de_travail_rien_ne_part(self):
        thread, faux = self.declencher(self.midi.replace(hour=20))
        self.assertIsNone(thread)
        self.assertEqual(faux.appels, [])

    def test_jour_non_travaille_rien_ne_part(self):
        autres = [str(jour) for jour in range(7) if jour != self.midi.weekday()]
        setting.set_value("work_days", ",".join(autres))
        thread, faux = self.declencher(self.midi)
        self.assertIsNone(thread)
        self.assertEqual(faux.appels, [])

    def test_desactive_rien_ne_part(self):
        setting.set_value("mail_enabled", "0")
        thread, faux = self.declencher(self.midi)
        self.assertIsNone(thread)

    def test_en_heures_de_travail_le_passage_part(self):
        thread, faux = self.declencher(self.midi)
        self.assertIsNotNone(thread)
        self.assertEqual(len(faux.appels), 1)
        self.assertEqual(len(self.outlook_captures()), 1)

    def test_une_fois_par_heure(self):
        setting.set_value("mail_last_run", (self.midi - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M"))
        self.assertFalse(mail_reader.is_due(self.midi))
        setting.set_value("mail_last_run", (self.midi - timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M"))
        self.assertTrue(mail_reader.is_due(self.midi))

    def test_fournisseur_non_claude_code_rien_ne_part(self):
        setting.set_value("llm_provider", "ollama")
        self.assertFalse(mail_reader.is_due(self.midi))


class ApiMailTest(MailReaderCase):
    """Routes /api/mail."""

    def test_etat(self):
        setting.set_value("mail_enabled", "1")
        reponse = self.client().get("/api/mail/status")
        self.assertEqual(reponse.status_code, 200)
        data = reponse.get_json()
        self.assertTrue(data["enabled"])
        self.assertTrue(data["available"])
        self.assertIsNone(data["last_run"])

    def test_etat_sans_claude_code(self):
        setting.set_value("llm_provider", "anthropic")
        data = self.client().get("/api/mail/status").get_json()
        self.assertFalse(data["available"])
        self.assertIn("Claude Code", data["refusal"])

    def test_lecture_immediate(self):
        with patch.object(mail_reader.subprocess, "run", FauxCli(enveloppe([mail("m1")]))):
            reponse = self.client().post("/api/mail/read", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.get_json()["count"], 1)

    def test_lecture_en_echec(self):
        with patch.object(mail_reader.subprocess, "run", FauxCli("illisible")):
            reponse = self.client().post("/api/mail/read", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 502)
        self.assertIn("illisible", reponse.get_json()["error"])

    def test_lecture_refusee_sans_claude_code(self):
        setting.set_value("llm_provider", "gemini")
        reponse = self.client().post("/api/mail/read", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 400)

    def test_reglage_d_activation_modifiable(self):
        reponse = self.client().patch(
            "/api/settings", json={"mail_enabled": "1"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(setting.get("mail_enabled"), "1")


class TelegramMailsTest(MailReaderCase):
    """Commande /mails : lecture en arriere-plan, resultat envoye a la fin."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "42")
        self.bridge = BridgeEspion(self.app)
        self.threads = []
        original = mail_reader.start_background

        def espion(app, on_done=None):
            thread = original(app, on_done)
            self.threads.append(thread)
            return thread

        self._espion = patch.object(mail_reader, "start_background", espion)
        self._espion.start()

    def tearDown(self):
        self._espion.stop()
        super().tearDown()

    def test_commande_mails(self):
        with patch.object(mail_reader.subprocess, "run", FauxCli(enveloppe([mail("m1")]))):
            self.bridge._handle({"chat": {"id": 42}, "text": "/mails"})
            for thread in self.threads:
                thread.join(10)

        self.assertIn("1 mail(s) depose(s)", self.bridge.dernier())
        self.assertEqual(len(self.outlook_captures()), 1)

    def test_commande_mails_sans_claude_code(self):
        setting.set_value("llm_provider", "gemini")
        self.bridge._handle({"chat": {"id": 42}, "text": "/mails"})
        self.assertIn("Claude Code", self.bridge.dernier())
        self.assertEqual(self.threads, [])

    def test_commande_au_menu(self):
        from services import telegram

        self.assertIn("mails", [entree["command"] for entree in telegram.BOT_COMMANDS])
