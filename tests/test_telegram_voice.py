"""Tests des vocaux Telegram : transcription, aiguillage et refus.

Un vocal doit produire exactement ce qu'aurait produit le meme texte tape,
ou une cause lisible. Jamais un silence, et jamais d'audio laisse sur le disque.
"""
from pathlib import Path
from unittest.mock import patch

import config
from models import capture as capture_model
from models import setting
from services import assistant, transcribe
from tests.base import NomaiaTestCase
from tests.test_telegram_chat import BridgeEspion


class BridgeVocal(BridgeEspion):
    """Passerelle espion qui depose un faux vocal au lieu de le telecharger."""

    def __init__(self, app):
        super().__init__(app)
        self.fichiers = []

    def _download_voice(self, file_id):
        dossier = config.UPLOAD_DIR / "voice"
        dossier.mkdir(parents=True, exist_ok=True)
        chemin = dossier / f"{file_id}.ogg"
        chemin.write_bytes(b"OggS")
        self.fichiers.append(chemin)
        return str(chemin)


class VocalTelegramTest(NomaiaTestCase):
    """Un vocal suit le meme circuit qu'un message tape."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "42")
        setting.set_value("telegram_chat_mode", "1")
        self.bridge = BridgeVocal(self.app)
        disponible = patch.object(transcribe, "is_available", return_value=True)
        disponible.start()
        self.addCleanup(disponible.stop)

    def _vocal(self, duree=8, legende=None):
        message = {"chat": {"id": 42}, "voice": {"file_id": "v1", "duration": duree}}
        if legende:
            message["caption"] = legende
        return message

    def test_vocal_court_part_en_conversation(self):
        with patch.object(transcribe, "transcribe", return_value="quel est mon plan ?"), \
                patch.object(assistant, "reply_to", return_value={"reply": "Trois taches."}) as reponse:
            self.bridge._handle(self._vocal())

        reponse.assert_called_once()
        self.assertEqual(reponse.call_args.args[0], "quel est mon plan ?")
        self.assertIn("Entendu", self.bridge.envois[0])
        self.assertEqual(self.bridge.dernier(), "Trois taches.")
        self.assertEqual(capture_model.list_captures(), [])

    def test_vocal_long_part_au_triage(self):
        setting.set_value("telegram_capture_chars", "100")
        with patch.object(transcribe, "transcribe", return_value="relancer le client " * 20):
            self.bridge._handle(self._vocal())

        captures = capture_model.list_captures()
        self.assertEqual(len(captures), 1)
        self.assertIn("relancer le client", captures[0]["raw_text"])

    def test_legende_precede_la_transcription(self):
        setting.set_value("telegram_chat_mode", "0")
        with patch.object(transcribe, "transcribe", return_value="corriger la paie"):
            self.bridge._handle(self._vocal(legende="Client ALPHA"))

        texte = capture_model.list_captures()[0]["raw_text"]
        self.assertTrue(texte.startswith("Client ALPHA"))
        self.assertIn("corriger la paie", texte)

    def test_audio_efface_apres_transcription(self):
        with patch.object(transcribe, "transcribe", return_value="salut"), \
                patch.object(assistant, "reply_to", return_value={"reply": "Salut."}):
            self.bridge._handle(self._vocal())

        self.assertFalse(Path(self.bridge.fichiers[0]).exists())

    def test_echec_de_transcription_est_annonce_et_nettoye(self):
        erreur = transcribe.TranscriptionError("Aucune parole reconnue dans ce vocal.")
        with patch.object(transcribe, "transcribe", side_effect=erreur):
            self.bridge._handle(self._vocal())

        self.assertIn("Aucune parole", self.bridge.dernier())
        self.assertEqual(capture_model.list_captures(), [])
        self.assertFalse(Path(self.bridge.fichiers[0]).exists())

    def test_vocal_trop_long_refuse_sans_telechargement(self):
        setting.set_value("voice_max_s", "60")
        self.bridge._handle(self._vocal(duree=120))

        self.assertIn("trop long", self.bridge.dernier())
        self.assertEqual(self.bridge.fichiers, [])

    def test_vocaux_desactives(self):
        setting.set_value("voice_enabled", "0")
        self.bridge._handle(self._vocal())

        self.assertIn("desactives", self.bridge.dernier())
        self.assertEqual(self.bridge.fichiers, [])

    def test_moteur_absent_dit_la_cause(self):
        with patch.object(transcribe, "is_available", return_value=False):
            self.bridge._handle(self._vocal())

        self.assertIn("faster-whisper", self.bridge.dernier())
        self.assertEqual(capture_model.list_captures(), [])

    def test_vocal_repond_a_une_question_du_triage(self):
        capture = capture_model.create_capture(raw_text="facture", source="telegram")
        capture_model.set_status(capture["id"], "waiting")
        setting.set_value("telegram_waiting_capture", str(capture["id"]))

        with patch.object(transcribe, "transcribe", return_value="pour vendredi"), \
                patch("services.triage.answer_capture") as reponse:
            self.bridge._handle(self._vocal())

        reponse.assert_called_once_with(capture["id"], "pour vendredi")
