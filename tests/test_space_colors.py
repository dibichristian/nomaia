"""Tests des couleurs des espaces Pro et Perso (page Configuration).

Les couleurs finissent dans une balise <style> de layout.html : seul le
format #rrggbb est accepte, a l'ecriture comme a la lecture.
"""
from models import setting
from tests.base import NomaiaTestCase


class SpaceColorsTest(NomaiaTestCase):
    """Reglage, validation par l'API et injection dans le HTML rendu."""

    def _patch(self, payload):
        return self.client().patch("/api/settings", json=payload, headers=self.write_headers())

    def test_defauts_identiques_a_shell_css(self):
        self.assertEqual(
            setting.space_colors(),
            {
                "--space-pro-from": "#6366f1",
                "--space-pro-to": "#22d3ee",
                "--space-perso-from": "#f59e0b",
                "--space-perso-to": "#ec4899",
            },
        )

    def test_enregistrement_valide(self):
        reponse = self._patch({"space_pro_color_from": "#ABCDEF", "space_perso_color_to": "#102030"})
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(setting.get("space_pro_color_from"), "#abcdef")
        self.assertEqual(setting.space_colors()["--space-perso-to"], "#102030")
        valeurs = self.client().get("/api/settings").get_json()["settings"]
        self.assertEqual(valeurs["space_pro_color_from"], "#abcdef")

    def test_valeur_invalide_refusee(self):
        for valeur in ("red;}", "#12345", "#1234567", "red", "#gggggg", "", None, 123,
                       "#123456;}body{x:y"):
            with self.subTest(valeur=valeur):
                reponse = self._patch({"space_pro_color_to": valeur})
                self.assertEqual(reponse.status_code, 400)
                self.assertEqual(setting.get("space_pro_color_to"), "#22d3ee")

    def test_valeur_corrompue_en_base_retombe_sur_le_defaut(self):
        setting.set_value("space_perso_color_from", "red;}</style><script>")
        self.assertEqual(setting.space_colors()["--space-perso-from"], "#f59e0b")
        html = self.client().get("/plan").get_data(as_text=True)
        self.assertNotIn("red;}", html)

    def test_variables_injectees_dans_la_page(self):
        setting.set_value("space_pro_color_from", "#010203")
        html = self.client().get("/plan").get_data(as_text=True)
        tete = html[:html.index("</head>")]
        self.assertIn(
            "<style>:root{--space-pro-from:#010203;--space-pro-to:#22d3ee;"
            "--space-perso-from:#f59e0b;--space-perso-to:#ec4899;}</style>",
            tete,
        )
        # Apres shell.css, qui porte les defauts : la balise l'emporte.
        self.assertLess(tete.index("css/shell.css"), tete.index("--space-pro-from"))

    def test_config_propose_les_couleurs(self):
        html = self.client().get("/config").get_data(as_text=True)
        for space in ("pro", "perso"):
            for edge in ("from", "to"):
                self.assertIn(f'<input type="color" id="cf-space-{space}-{edge}">', html)
            self.assertIn(f'id="cf-space-{space}-preview"', html)
        self.assertIn('id="cf-space-reset"', html)
        self.assertIn('data-save="espaces"', html)


class CouleursEspacesLectureTest(NomaiaTestCase):
    """GET /api/settings applique le meme repli que la page."""

    def test_valeur_corrompue_renvoyee_par_defaut(self):
        from models import setting

        setting.set_value("space_perso_color_from", "red;}")
        data = self.client().get("/api/settings").get_json()["settings"]
        self.assertEqual(data["space_perso_color_from"], setting.DEFAULTS["space_perso_color_from"])
