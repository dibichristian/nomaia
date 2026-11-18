"""Tests de la couche reseau Telegram : connexion conservee, reprises, hors ligne."""
import json
import socket
import unittest
from datetime import datetime, timedelta
from unittest import mock

from models import setting
from services import telegram
from services.telegram import TelegramBridge, TelegramOffline
from tests.base import NomaiaTestCase


class FausseReponse:
    """Reponse HTTP minimale."""

    def __init__(self, status, corps):
        self.status = status
        self._corps = corps

    def read(self):
        return self._corps


class FausseConnexion:
    """Connexion HTTPS simulee, pilotee par une file de resultats partagee.

    Chaque element de la file est soit une exception levee a l'envoi, soit
    un couple (statut, corps) renvoye par getresponse().
    """

    ouvertes = []

    def __init__(self, host, timeout=None):
        self.host = host
        self.timeout = timeout
        self.sock = None
        self.requetes = []
        self.fermee = False
        FausseConnexion.ouvertes.append(self)

    def request(self, verbe, chemin, body=None, headers=None):
        resultat = FausseConnexion.file.pop(0)
        if isinstance(resultat, BaseException):
            raise resultat
        self.requetes.append(chemin)
        self._prochaine = resultat

    def getresponse(self):
        statut, corps = self._prochaine
        return FausseReponse(statut, corps)

    def close(self):
        self.fermee = True


def ok(resultat):
    """Corps JSON d'une reponse Telegram reussie."""
    return 200, json.dumps({"ok": True, "result": resultat}).encode("utf-8")


class TelegramCallTest(NomaiaTestCase):
    """Appels API : reutilisation de la connexion et reprises."""

    def setUp(self):
        super().setUp()
        setting.set_secret("telegram_token", "123:abc")
        FausseConnexion.ouvertes = []
        FausseConnexion.file = []
        patch_conn = mock.patch.object(telegram.http.client, "HTTPSConnection", FausseConnexion)
        patch_conn.start()
        self.addCleanup(patch_conn.stop)
        self.bridge = TelegramBridge(self.app)
        # Pas d'attente reelle entre deux essais.
        self.bridge._stop.wait = lambda delai: None

    def test_connexion_reutilisee_entre_appels(self):
        FausseConnexion.file = [ok({"username": "nomaia"}), ok([])]
        self.bridge._call("getMe")
        self.bridge._call("getUpdates")
        self.assertEqual(len(FausseConnexion.ouvertes), 1)
        self.assertEqual(
            FausseConnexion.ouvertes[0].requetes,
            ["/bot123:abc/getMe", "/bot123:abc/getUpdates"],
        )

    def test_reset_rejoue_sur_connexion_neuve(self):
        reset = ConnectionResetError(10054, "connexion fermee par l'hote distant")
        FausseConnexion.file = [reset, reset, ok({"message_id": 1})]
        resultat = self.bridge._call("sendMessage", {"chat_id": 1, "text": "x"})
        self.assertEqual(resultat, {"message_id": 1})
        self.assertEqual(len(FausseConnexion.ouvertes), 3)
        self.assertTrue(FausseConnexion.ouvertes[0].fermee)

    def test_connexion_morte_apres_veille_reprise(self):
        FausseConnexion.file = [ok({}), telegram.http.client.RemoteDisconnected("veille"), ok([])]
        self.bridge._call("getMe")
        self.assertEqual(self.bridge._call("getUpdates"), [])
        self.assertEqual(len(FausseConnexion.ouvertes), 2)

    def test_essais_epuises_leve_runtime_error(self):
        FausseConnexion.file = [ConnectionResetError(10054, "reset")] * telegram.CALL_ATTEMPTS
        with self.assertRaises(RuntimeError) as contexte:
            self.bridge._call("getUpdates")
        self.assertNotIsInstance(contexte.exception, TelegramOffline)
        self.assertIn("injoignable", str(contexte.exception))

    def test_erreur_http_non_rejouee(self):
        FausseConnexion.file = [(401, b'{"ok":false}'), ok({})]
        with self.assertRaises(RuntimeError) as contexte:
            self.bridge._call("getMe")
        self.assertIn("HTTP 401", str(contexte.exception))
        self.assertEqual(len(FausseConnexion.file), 1)

    def test_hors_ligne_signale_sans_insister(self):
        FausseConnexion.file = [socket.gaierror(11001, "getaddrinfo failed"), ok({})]
        with self.assertRaises(TelegramOffline):
            self.bridge._call("getUpdates")
        self.assertEqual(len(FausseConnexion.file), 1)

    def test_reseau_injoignable_windows_hors_ligne(self):
        erreur = OSError(10051, "reseau injoignable")
        erreur.winerror = 10051
        FausseConnexion.file = [erreur]
        with self.assertRaises(TelegramOffline):
            self.bridge._call("getUpdates")


class TelegramOfflineLoopTest(NomaiaTestCase):
    """Boucle de reception : pause discrete hors ligne."""

    def setUp(self):
        super().setUp()
        self.bridge = TelegramBridge(self.app)

    def test_attente_hors_ligne_croissante_plafonnee(self):
        self.bridge._offline_since = datetime.now()
        self.assertEqual(self.bridge._offline_wait(), telegram.OFFLINE_WAIT_MIN_S)
        self.bridge._offline_since = datetime.now() - timedelta(seconds=30)
        self.assertAlmostEqual(self.bridge._offline_wait(), 30, delta=1)
        self.bridge._offline_since = datetime.now() - timedelta(hours=2)
        self.assertEqual(self.bridge._offline_wait(), telegram.OFFLINE_WAIT_MAX_S)

    def test_un_seul_avertissement_puis_reprise(self):
        sequence = [TelegramOffline("hors ligne")] * 3 + [[]]
        bridge = self.bridge

        def faux_call(method, params=None, timeout=20, attempts=1):
            if method == "setMyCommands":
                return True
            resultat = sequence.pop(0)
            if not sequence:
                bridge._stop.set()
            if isinstance(resultat, BaseException):
                raise resultat
            return resultat

        bridge._call = faux_call
        bridge._stop.wait = lambda delai: None
        with self.assertLogs("nomaia.telegram", level="INFO") as journal:
            bridge._loop()

        avertissements = [l for l in journal.output if "Hors ligne" in l]
        self.assertEqual(len(avertissements), 1)
        self.assertTrue(any("Reseau retabli" in l for l in journal.output))
        self.assertIsNone(bridge._offline_since)


if __name__ == "__main__":
    unittest.main()
