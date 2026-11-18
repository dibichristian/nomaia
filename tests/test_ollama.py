"""Tests du catalogue Ollama : ce que le poste a vraiment.

Un catalogue fige proposait trois modeles absents de la machine et cachait le
seul present. Impossible de corriger depuis l'interface, et le triage echouait
en 404 sur un modele qui n'existait pas.
"""
import unittest
from unittest.mock import patch

from services import providers
from tests.base import NomaiaTestCase

# Reponse type de /api/tags, telle qu'Ollama la rend.
TAGS = {
    "models": [
        {
            "name": "qwen2.5-coder:latest",
            "model": "qwen2.5-coder:latest",
            "size": 4683087561,
            "details": {"parameter_size": "7.6B"},
            "capabilities": ["completion", "tools", "insert"],
        },
        {
            "name": "qwen2.5vl:7b",
            "model": "qwen2.5vl:7b",
            "size": 6000000000,
            "details": {"parameter_size": "7B"},
            "capabilities": ["completion", "vision"],
        },
    ]
}


class OllamaModelsTest(unittest.TestCase):
    """Lecture de la liste reelle, et repli quand Ollama est eteint."""

    def _avec_tags(self, charge):
        """Remplace l'appel reseau par une charge connue."""

        class FausseReponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            @staticmethod
            def read():
                import json

                return json.dumps(charge).encode("utf-8")

        return patch("urllib.request.urlopen", return_value=FausseReponse())

    def test_les_modeles_installes_sont_rendus(self):
        with self._avec_tags(TAGS):
            modeles = providers.ollama_models()

        self.assertEqual([m["id"] for m in modeles], ["qwen2.5-coder:latest", "qwen2.5vl:7b"])

    def test_la_vision_est_detectee(self):
        with self._avec_tags(TAGS):
            par_id = {m["id"]: m for m in providers.ollama_models()}

        self.assertFalse(par_id["qwen2.5-coder:latest"]["vision"])
        self.assertTrue(par_id["qwen2.5vl:7b"]["vision"])

    def test_l_absence_de_vision_est_annoncee_dans_le_libelle(self):
        # C'est la seule chose que l'utilisateur lira dans le selecteur.
        with self._avec_tags(TAGS):
            par_id = {m["id"]: m for m in providers.ollama_models()}

        self.assertIn("sans vision", par_id["qwen2.5-coder:latest"]["label"])
        self.assertNotIn("sans vision", par_id["qwen2.5vl:7b"]["label"])

    def test_la_taille_est_lisible(self):
        with self._avec_tags(TAGS):
            modeles = providers.ollama_models()

        self.assertIn("Go", modeles[0]["label"])
        self.assertIn("7.6B", modeles[0]["label"])

    def test_ollama_eteint_rend_une_liste_vide(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connexion refusee")):
            self.assertEqual(providers.ollama_models(), [])

    def test_reponse_illisible_ne_leve_pas(self):
        with patch("urllib.request.urlopen", side_effect=ValueError("JSON casse")):
            self.assertEqual(providers.ollama_models(), [])

    def test_entree_sans_nom_ecartee(self):
        with self._avec_tags({"models": [{"size": 1}, {"model": "bon:latest"}]}):
            modeles = providers.ollama_models()

        self.assertEqual([m["id"] for m in modeles], ["bon:latest"])

    def test_charge_vide_ne_plante_pas(self):
        with self._avec_tags({}):
            self.assertEqual(providers.ollama_models(), [])


class ErreurOllamaTest(unittest.TestCase):
    """Un motif brut d'Ollama ne dit pas quoi faire : on le traduit."""

    @staticmethod
    def _panne(texte):
        from services.providers import ProviderError

        return ProviderError("Ollama a repondu 500 : " + texte)

    def test_architecture_inconnue_devient_actionnable(self):
        # Le cas vecu : modele telecharge, visible dans la liste, mais que le
        # moteur ne sait pas charger. Rien ne le distingue d'un modele sain.
        message = str(
            providers._erreur_ollama(
                self._panne('{"error":"unknown model architecture: mllama"}'),
                "llama3.2-vision:11b",
            )
        )
        self.assertIn("incompatible", message)
        self.assertIn("llama3.2-vision:11b", message)

    def test_modele_absent_indique_la_commande(self):
        message = str(
            providers._erreur_ollama(
                self._panne('{"error":"model absent not found"}'), "absent:latest"
            )
        )
        self.assertIn("ollama pull absent:latest", message)

    def test_memoire_insuffisante_signalee_comme_passagere(self):
        erreur = providers._erreur_ollama(
            self._panne('{"error":"not enough memory"}'), "gros:70b"
        )
        self.assertIn("memoire", str(erreur))
        self.assertTrue(erreur.transient)

    def test_motif_inconnu_est_rendu_tel_quel(self):
        panne = self._panne("quelque chose d'autre")
        self.assertIs(providers._erreur_ollama(panne, "un:modele"), panne)


class ModelsForTest(unittest.TestCase):
    """models_for : dynamique pour Ollama, fige pour les autres."""

    def test_les_autres_fournisseurs_gardent_leur_catalogue(self):
        self.assertEqual(providers.models_for("anthropic"), providers.MODELS["anthropic"])
        self.assertEqual(providers.models_for("gemini"), providers.MODELS["gemini"])

    def test_ollama_rend_les_modeles_installes(self):
        with patch.object(providers, "ollama_models", return_value=[{"id": "reel:latest"}]):
            self.assertEqual(providers.models_for("ollama"), [{"id": "reel:latest"}])

    def test_ollama_eteint_retombe_sur_le_catalogue(self):
        # Une liste vide empecherait toute configuration : mieux vaut proposer
        # les modeles connus et laisser l'interface signaler qu'Ollama dort.
        with patch.object(providers, "ollama_models", return_value=[]):
            self.assertEqual(providers.models_for("ollama"), providers.MODELS["ollama"])

    def test_fournisseur_inconnu_rend_une_liste_vide(self):
        self.assertEqual(providers.models_for("nawak"), [])


class SettingsApiTest(NomaiaTestCase):
    """La page de configuration doit proposer ce qui existe vraiment."""

    def test_les_modeles_ollama_viennent_du_poste(self):
        with patch.object(
            providers, "ollama_models", return_value=[{"id": "reel:latest", "label": "reel", "in": 0, "out": 0, "vision": True}]
        ):
            corps = self.client().get("/api/settings").get_json()

        ollama = next(item for item in corps["providers"] if item["id"] == "ollama")
        self.assertEqual([m["id"] for m in ollama["models"]], ["reel:latest"])

    def test_un_modele_installe_hors_catalogue_est_acceptable(self):
        # model_ollama est un champ libre : un modele tout juste telecharge
        # doit pouvoir etre saisi sans attendre une mise a jour du code.
        reponse = self.client().patch(
            "/api/settings",
            json={"model_ollama": "qwen2.5-coder:latest"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 200, reponse.get_json())

    def test_un_modele_anthropic_inconnu_reste_refuse(self):
        reponse = self.client().patch(
            "/api/settings",
            json={"model_anthropic": "modele-invente"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)


class CheckBackendTest(NomaiaTestCase):
    """« Tester la connexion » ne doit pas repondre OK sur un moteur casse."""

    def setUp(self):
        super().setUp()
        from models import setting

        setting.set_value("llm_provider", "ollama")
        setting.set_value("model_ollama", "absent:latest")

    def test_modele_absent_signale(self):
        # Le service repondait, donc le test passait au vert, alors que le
        # triage echouait en 404 des la premiere capture.
        from services import llm

        with patch.object(
            providers, "ollama_models",
            return_value=[{"id": "present:latest", "label": "present", "in": 0, "out": 0, "vision": False}],
        ):
            etat = llm.check_backend()

        self.assertFalse(etat["ok"])
        self.assertIn("ollama pull absent:latest", etat["detail"])
        self.assertIn("present:latest", etat["detail"])

    def test_ollama_eteint_signale(self):
        from services import llm

        with patch.object(providers, "ollama_models", return_value=[]):
            etat = llm.check_backend()

        self.assertFalse(etat["ok"])
        self.assertIn("ne repond pas", etat["detail"])

    def test_modele_present_passe(self):
        from models import setting
        from services import llm

        setting.set_value("model_ollama", "present:latest")
        with patch.object(
            providers, "ollama_models",
            return_value=[{"id": "present:latest", "label": "present", "in": 0, "out": 0, "vision": True}],
        ):
            etat = llm.check_backend()

        self.assertTrue(etat["ok"], etat)


class ClaudeCliTest(unittest.TestCase):
    """Sous-processus claude : fenetre masquee et compte impose."""

    class FauxTermine:
        returncode = 0
        stdout = '{"result": "{}", "usage": {}}'
        stderr = ""

    def _lance(self, options):
        """Lance ask_claude_cli en interceptant subprocess.run. Rend ses arguments."""
        vus = {}

        def faux_run(args, **kwargs):
            vus["args"] = args
            vus.update(kwargs)
            return self.FauxTermine()

        with patch("services.providers.claude_path", return_value="claude.exe"):
            with patch("subprocess.run", faux_run):
                providers.ask_claude_cli("bonjour", [], 30, options)
        return vus

    def test_aucune_fenetre_console(self):
        # Sans ce drapeau, une fenetre noire surgit au premier plan a chaque
        # triage, vole le focus, puis disparait.
        vus = self._lance({"model": "haiku", "system": "", "max_tokens": 50})
        self.assertEqual(vus.get("creationflags"), providers.NO_WINDOW)

    def test_le_dossier_de_configuration_est_transmis(self):
        vus = self._lance(
            {"model": "haiku", "system": "", "max_tokens": 50,
             "claude_config_dir": "C:/ailleurs/.claude-pro"}
        )
        self.assertEqual(vus["env"]["CLAUDE_CONFIG_DIR"], "C:/ailleurs/.claude-pro")

    def test_sans_reglage_l_environnement_est_herite(self):
        import os

        vus = self._lance({"model": "haiku", "system": "", "max_tokens": 50})
        # Aucune valeur imposee : ce qui existait dans l'environnement reste.
        self.assertEqual(
            vus["env"].get("CLAUDE_CONFIG_DIR"), os.environ.get("CLAUDE_CONFIG_DIR")
        )


if __name__ == "__main__":
    unittest.main()
