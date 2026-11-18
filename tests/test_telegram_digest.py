"""Tests de l'instantane epingle : le seul acces aux taches PC eteint."""
import unittest
from datetime import date, datetime, timedelta

from models import project as project_model
from models import setting
from models import task as task_model
from services.telegram import TelegramBridge
from tests.base import NomaiaTestCase


class BridgeEspion(TelegramBridge):
    """Passerelle qui note les appels au lieu de joindre Telegram."""

    def __init__(self, app, echec=None):
        super().__init__(app)
        self.appels = []
        self.echec = echec or {}

    def _call(self, method, params=None, timeout=20, attempts=1):
        self.appels.append((method, params or {}))
        if method in self.echec:
            raise RuntimeError(self.echec[method])
        if method == "sendMessage":
            return {"message_id": 4242}
        return {}

    def methodes(self):
        """Retourne la suite des methodes appelees."""
        return [method for method, _ in self.appels]


class DigestTextTest(NomaiaTestCase):
    """Contenu de l'instantane."""

    def setUp(self):
        super().setUp()
        self.bridge = BridgeEspion(self.app)

    def test_base_vide(self):
        self.assertIn("Aucune tache ouverte", self.bridge._digest_text())

    def test_groupes_par_echeance(self):
        today = date.today()
        task_model.create_task(
            {"title": "Retard", "deadline": (today - timedelta(days=2)).isoformat()}
        )
        task_model.create_task({"title": "Du jour", "deadline": today.isoformat()})
        task_model.create_task(
            {"title": "Plus tard", "deadline": (today + timedelta(days=5)).isoformat()}
        )
        task_model.create_task({"title": "Un jour"})

        texte = self.bridge._digest_text()
        for entete in ("EN RETARD (1)", "AUJOURD'HUI (1)", "A VENIR (1)", "SANS ECHEANCE (1)"):
            self.assertIn(entete, texte)
        self.assertIn("4 tache(s) ouverte(s)", texte)

    def test_le_projet_est_cite(self):
        projet = project_model.create_project("ALPHA")
        task_model.create_task({"title": "Corriger", "project_id": projet["id"]})

        self.assertIn("[ALPHA]", self.bridge._digest_text())

    def test_une_tache_terminee_disparait(self):
        tache = task_model.create_task({"title": "Faite"})
        task_model.toggle_task(tache["id"])

        self.assertIn("Aucune tache ouverte", self.bridge._digest_text())

    def test_liste_plafonnee(self):
        for index in range(TelegramBridge.DIGEST_MAX_TASKS + 10):
            task_model.create_task({"title": f"Tache {index}"})

        texte = self.bridge._digest_text()
        self.assertIn("autre(s).", texte)
        self.assertLess(len(texte), 3800)


class DigestPublishTest(NomaiaTestCase):
    """Creation, edition et reprise de l'epingle."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "123456")
        task_model.create_task({"title": "Une tache"})

    def test_sans_appairage_rien_ne_part(self):
        setting.set_value("telegram_chat_id", "")
        bridge = BridgeEspion(self.app)

        self.assertFalse(bridge.update_pinned_digest())
        self.assertEqual(bridge.methodes(), [])

    def test_reglage_desactive(self):
        setting.set_value("telegram_pinned_digest", "0")
        bridge = BridgeEspion(self.app)

        self.assertFalse(bridge.update_pinned_digest())
        self.assertEqual(bridge.methodes(), [])

    def test_premier_envoi_epingle_le_message(self):
        bridge = BridgeEspion(self.app)

        self.assertTrue(bridge.update_pinned_digest())
        self.assertEqual(bridge.methodes(), ["sendMessage", "pinChatMessage"])
        self.assertEqual(setting.get("telegram_digest_message_id"), "4242")

    def test_l_epingle_ne_fait_pas_sonner_le_telephone(self):
        bridge = BridgeEspion(self.app)
        bridge.update_pinned_digest()

        params = dict(bridge.appels)["pinChatMessage"]
        self.assertEqual(params["disable_notification"], "true")

    def test_envoi_suivant_edite_au_lieu_de_reposter(self):
        bridge = BridgeEspion(self.app)
        bridge.update_pinned_digest()
        bridge.appels.clear()

        self.assertTrue(bridge.update_pinned_digest())
        self.assertEqual(bridge.methodes(), ["editMessageText"])

    def test_contenu_inchange_est_un_succes(self):
        setting.set_value("telegram_digest_message_id", "4242")
        bridge = BridgeEspion(self.app, echec={"editMessageText": "message is not modified"})

        self.assertTrue(bridge.update_pinned_digest())
        self.assertEqual(bridge.methodes(), ["editMessageText"])

    def test_message_supprime_a_la_main_est_repose(self):
        setting.set_value("telegram_digest_message_id", "4242")
        bridge = BridgeEspion(self.app, echec={"editMessageText": "message to edit not found"})

        self.assertTrue(bridge.update_pinned_digest())
        self.assertEqual(bridge.methodes(), ["editMessageText", "sendMessage", "pinChatMessage"])


class DigestDebounceTest(NomaiaTestCase):
    """Cocher dix taches ne doit pas declencher dix appels a Telegram."""

    def setUp(self):
        super().setUp()
        setting.set_value("telegram_chat_id", "123456")
        task_model.create_task({"title": "Une tache"})
        self.bridge = BridgeEspion(self.app)

    def test_rien_a_pousser_sans_ecriture(self):
        self.assertFalse(self.bridge.refresh_digest_if_stale())
        self.assertEqual(self.bridge.methodes(), [])

    def test_une_ecriture_declenche_un_envoi(self):
        self.bridge.mark_digest_stale()

        self.assertTrue(self.bridge.refresh_digest_if_stale())
        self.assertIn("sendMessage", self.bridge.methodes())

    def test_deuxieme_envoi_differe(self):
        self.bridge.mark_digest_stale()
        self.bridge.refresh_digest_if_stale()
        self.bridge.appels.clear()

        self.bridge.mark_digest_stale()
        self.assertFalse(self.bridge.refresh_digest_if_stale())
        self.assertEqual(self.bridge.methodes(), [])

    def test_le_delai_passe_libere_l_envoi(self):
        self.bridge.mark_digest_stale()
        self.bridge.refresh_digest_if_stale()
        self.bridge.appels.clear()

        self.bridge._digest_pushed_at = datetime.now() - timedelta(
            seconds=TelegramBridge.DIGEST_DEBOUNCE_S + 1
        )
        self.bridge.mark_digest_stale()
        self.assertTrue(self.bridge.refresh_digest_if_stale())
        self.assertEqual(self.bridge.methodes(), ["editMessageText"])


class DigestHookTest(NomaiaTestCase):
    """Le marquage se fait depuis les routes d'ecriture, sans appel reseau."""

    def setUp(self):
        super().setUp()
        self.bridge = BridgeEspion(self.app)
        self.app.extensions["telegram"] = self.bridge

    def test_une_creation_de_tache_marque_l_instantane(self):
        self.client().post(
            "/api/tasks", json={"title": "Nouvelle"}, headers=self.write_headers()
        )
        self.assertTrue(self.bridge._digest_stale)
        self.assertEqual(self.bridge.methodes(), [], "aucun appel reseau pendant la requete")

    def test_une_lecture_ne_marque_rien(self):
        self.client().get("/api/tasks")
        self.assertFalse(self.bridge._digest_stale)

    def test_une_ecriture_refusee_ne_marque_rien(self):
        self.client().post("/api/projects", json={"name": ""}, headers=self.write_headers())
        self.assertFalse(self.bridge._digest_stale)

    def test_un_changement_de_reglage_ne_marque_rien(self):
        self.client().patch(
            "/api/settings", json={"plan_buffer_pct": "20"}, headers=self.write_headers()
        )
        self.assertFalse(self.bridge._digest_stale)


if __name__ == "__main__":
    unittest.main()
