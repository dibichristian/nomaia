"""Acces depuis le telephone : adresse Tailscale et code d'appairage.

Le telephone joint NOMAIA par une origine distante (https://...ts.net) : elle
doit etre admise pour les ecritures, et seulement elle. Le cookie s'obtient
par un code a usage unique, qu'on ne peut pas deviner par essais successifs.
"""
from datetime import datetime, timedelta

from models import setting
from tests.base import NomaiaTestCase

ORIGINE = "https://mon-pc.tail1234.ts.net"


class AdresseTelephoneTest(NomaiaTestCase):
    """Reglage de l'adresse Tailscale."""

    def test_adresse_normalisee(self):
        reponse = self.client().put(
            "/api/mobile",
            json={"origin": "Mon-PC.tail1234.ts.net/plan"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(setting.get("mobile_origin"), ORIGINE)

    def test_http_refuse(self):
        reponse = self.client().put(
            "/api/mobile",
            json={"origin": "http://mon-pc.tail1234.ts.net"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(setting.get("mobile_origin"), "")

    def test_code_refuse_sans_adresse(self):
        reponse = self.client().post("/api/mobile/code", headers=self.write_headers())
        self.assertEqual(reponse.status_code, 400)


class OrigineTelephoneTest(NomaiaTestCase):
    """Seule l'adresse reglee est admise comme origine distante."""

    def test_ecriture_admise_depuis_adresse_reglee(self):
        setting.set_value("mobile_origin", ORIGINE)
        reponse = self.client().post(
            "/api/tasks", json={"title": "Depuis le telephone"}, headers={"Origin": ORIGINE}
        )
        self.assertEqual(reponse.status_code, 201)

    def test_autre_origine_refusee(self):
        setting.set_value("mobile_origin", ORIGINE)
        reponse = self.client().post(
            "/api/tasks",
            json={"title": "Piege"},
            headers={"Origin": "https://autre.tail1234.ts.net"},
        )
        self.assertEqual(reponse.status_code, 403)

    def test_sans_reglage_origine_distante_refusee(self):
        reponse = self.client().post(
            "/api/tasks", json={"title": "Piege"}, headers={"Origin": ORIGINE}
        )
        self.assertEqual(reponse.status_code, 403)


class AppairageTest(NomaiaTestCase):
    """Le code donne le cookie une fois, puis plus jamais."""

    def setUp(self):
        super().setUp()
        setting.set_value("mobile_origin", ORIGINE)
        self.code = setting.issue_pairing_code()

    def _appairer(self, code):
        """Envoie le formulaire de la page d'acces, sans cookie."""
        return self.app.test_client().post(
            "/appairage", data={"code": code}, headers={"Origin": ORIGINE}
        )

    def test_code_juste_pose_le_cookie(self):
        reponse = self._appairer(self.code[:4].lower() + "-" + self.code[4:])
        self.assertEqual(reponse.status_code, 302)
        self.assertIn("nomaia_access=" + setting.ensure_access_key(), reponse.headers["Set-Cookie"])

    def test_code_a_usage_unique(self):
        self._appairer(self.code)
        reponse = self._appairer(self.code)
        self.assertEqual(reponse.status_code, 401)
        self.assertNotIn("Set-Cookie", reponse.headers)

    def test_code_expire(self):
        setting.set_value("pairing_expires", (datetime.now() - timedelta(seconds=1)).isoformat())
        self.assertEqual(self._appairer(self.code).status_code, 401)

    def test_code_efface_apres_essais_faux(self):
        for _ in range(setting.PAIRING_MAX_FAILURES):
            self.assertEqual(self._appairer("ZZZZZZZZ").status_code, 401)
        self.assertEqual(self._appairer(self.code).status_code, 401)

    def test_appairage_depuis_autre_site_refuse(self):
        reponse = self.app.test_client().post(
            "/appairage", data={"code": self.code}, headers={"Origin": "https://piege.example"}
        )
        self.assertEqual(reponse.status_code, 403)
        # L'essai bloque n'a pas consomme le code.
        self.assertEqual(self._appairer(self.code).status_code, 302)

    def test_code_absent_des_sauvegardes(self):
        from models import SECRET_SETTINGS
        self.assertIn("pairing_code", SECRET_SETTINGS)
        self.assertIn("pairing_code", setting.SECRET_KEYS)
