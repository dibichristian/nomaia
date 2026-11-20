"""Tests du chargement de l'extension locale facultative (paquet perso/)."""
import sys
import tempfile
import types
from pathlib import Path

from flask import Blueprint, jsonify

import config
from services.extensions import load_extensions
from tests.base import NomaiaTestCase

FAKE_MODULE = "nomaia_fake_extension"


class ExtensionLoadingTest(NomaiaTestCase):
    """Absence, chargement et echec d'une extension locale."""

    def tearDown(self):
        """Retire le faux paquet installe par le test."""
        sys.modules.pop(FAKE_MODULE, None)
        super().tearDown()

    def _install_fake(self, register=None, location=None):
        """Installe un faux paquet d'extension, avec ou sans register(app).

        :param location: chemin simule du paquet ; par defaut dans le dossier
            du programme, comme un vrai perso/
        """
        module = types.ModuleType(FAKE_MODULE)
        module.__file__ = str(location or Path(config.RESOURCE_DIR) / "perso" / "__init__.py")
        if register is not None:
            module.register = register
        sys.modules[FAKE_MODULE] = module

    def test_missing_package_is_ignored(self):
        """Sans paquet, rien n'est charge et l'application reste utilisable."""
        self.assertFalse(load_extensions(self.app, "nomaia_no_such_extension"))
        self.assertEqual(self.client().get("/api/tasks").status_code, 200)

    def test_registered_route_is_served_and_protected(self):
        """Une route de l'extension repond, mais seulement avec la cle d'acces."""

        def register(app):
            blueprint = Blueprint("fake_extension", __name__)

            @blueprint.route("/api/fake-extension/ping", methods=["GET", "POST"])
            def ping():
                return jsonify({"pong": True})

            app.register_blueprint(blueprint)

        self._install_fake(register)
        self.assertTrue(load_extensions(self.app, FAKE_MODULE))

        response = self.client().get("/api/fake-extension/ping")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"pong": True})

        anonymous = self.app.test_client().get("/api/fake-extension/ping")
        self.assertEqual(anonymous.status_code, 401)

        # Ecriture depuis un site tiers : refusee par le garde-fou anti-CSRF.
        forged = self.client().post(
            "/api/fake-extension/ping", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(forged.status_code, 403)
        accepted = self.client().post("/api/fake-extension/ping", headers=self.write_headers())
        self.assertEqual(accepted.status_code, 200)

    def test_foreign_package_is_ignored(self):
        """Un paquet homonyme hors du dossier du programme n'est pas branche."""
        called = []
        outside = Path(tempfile.gettempdir()) / "ailleurs" / "perso" / "__init__.py"
        self._install_fake(lambda app: called.append(app), location=outside)
        with self.assertLogs("nomaia.extensions", level="WARNING"):
            self.assertFalse(load_extensions(self.app, FAKE_MODULE))
        self.assertEqual(called, [])

    def test_failing_register_does_not_block_startup(self):
        """Un register(app) qui plante est journalise sans arreter NOMAIA."""

        def register(app):
            raise RuntimeError("extension cassee")

        self._install_fake(register)
        with self.assertLogs("nomaia.extensions", level="ERROR") as logs:
            self.assertFalse(load_extensions(self.app, FAKE_MODULE))
        self.assertIn("extension cassee", "\n".join(logs.output))
        self.assertEqual(self.client().get("/api/tasks").status_code, 200)

    def test_package_without_register_is_ignored(self):
        """Un paquet sans register(app) est signale puis ignore."""
        self._install_fake()
        with self.assertLogs("nomaia.extensions", level="WARNING"):
            self.assertFalse(load_extensions(self.app, FAKE_MODULE))
