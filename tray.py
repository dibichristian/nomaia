"""Lanceur de NOMAIA : fenetre native, serveur local et icone de la barre des taches.

La fenetre s'appuie sur WebView2, le moteur de rendu livre avec Windows : pas
de navigateur visible, pas de barre d'adresse, une icone propre dans la barre
des taches. Si pywebview est indisponible, NOMAIA se rabat sur le navigateur
par defaut plutot que de refuser de demarrer.

Fermer la fenetre ne quitte pas l'application : elle se replie dans la barre
des taches, ou le triage et l'ecoute Telegram continuent de tourner.
"""
import logging
import socket
import sys
import threading
import urllib.request
import webbrowser
from urllib.parse import quote

import pystray
from PIL import Image
from waitress import create_server

import config
from app import create_app
from models import setting

try:
    import webview
except ImportError:  # environnement sans pywebview
    webview = None

logger = logging.getLogger("nomaia.tray")

BASE_URL = f"http://{config.HOST}:{config.PORT}"
ICON_PATH = config.ICON_PNG
WEBVIEW_STORAGE = config.DATA_DIR / "webview"

WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 820


def already_running():
    """Indique qu'une instance occupe deja le port de NOMAIA."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.6)
        return probe.connect_ex((config.HOST, config.PORT)) == 0


def access_key(app):
    """Retourne la cle d'acces de l'instance, lue dans la base."""
    with app.app_context():
        return setting.ensure_access_key()


def page_url(key, path=""):
    """Construit l'adresse d'une page avec la cle d'acces."""
    return f"{BASE_URL}{path}?k={quote(key)}"


class ServerThread(threading.Thread):
    """Serveur WSGI waitress dans un thread, arretable proprement."""

    def __init__(self, app):
        super().__init__(name="nomaia-http", daemon=True)
        self.server = create_server(
            app, host=config.HOST, port=config.PORT, threads=8, ident="NOMAIA"
        )

    def run(self):
        """Sert les requetes jusqu'a l'appel de shutdown()."""
        self.server.run()

    def shutdown(self):
        """Ferme le serveur et libere le port."""
        self.server.close()


def wake_existing_instance(key):
    """Demande a l'instance deja lancee d'afficher sa fenetre."""
    try:
        request = urllib.request.Request(
            f"{BASE_URL}/api/window/show",
            data=b"",
            headers={"Cookie": f"nomaia_access={key}", "Origin": BASE_URL},
        )
        urllib.request.urlopen(request, timeout=3).close()
        return True
    except OSError as error:
        logger.warning("Instance existante injoignable : %s", error)
        return False


class Launcher:
    """Assemble la fenetre, l'icone et le serveur, et gere leur cycle de vie."""

    def __init__(self, app, server, key):
        self.app = app
        self.server = server
        self.key = key
        self.window = None
        self.icon = None
        self.quitting = False

    # --- Fenetre -----------------------------------------------------------

    def create_window(self):
        """Cree la fenetre native et branche son evenement de fermeture."""
        self.window = webview.create_window(
            "NOMAIA",
            page_url(self.key),
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            min_size=(460, 600),
            background_color="#0f1117",
        )
        self.window.events.closing += self._on_closing
        return self.window

    def _on_closing(self):
        """Replie la fenetre dans la barre des taches au lieu de quitter.

        Retourner False annule la fermeture ; pendant un arret volontaire, on
        laisse au contraire la fenetre se detruire.
        """
        if self.quitting:
            return True
        self.window.hide()
        return False

    def show(self, path=""):
        """Affiche la fenetre sur une page donnee, ou ouvre le navigateur."""
        if self.window is None:
            webbrowser.open(page_url(self.key, path))
            return

        if path:
            self.window.load_url(page_url(self.key, path))
        self.window.show()

    # --- Icone de la barre des taches ---------------------------------------

    def build_icon(self):
        """Construit l'icone et son menu."""
        menu = pystray.Menu(
            pystray.MenuItem("Ouvrir NOMAIA", lambda i, item: self.show(), default=True),
            pystray.MenuItem("Boite de reception", lambda i, item: self.show("/")),
            pystray.MenuItem("Chat", lambda i, item: self.show("/chat")),
            pystray.MenuItem("Plan du jour", lambda i, item: self.show("/plan")),
            pystray.MenuItem("Agenda", lambda i, item: self.show("/agenda")),
            pystray.MenuItem("Taches", lambda i, item: self.show("/taches")),
            pystray.MenuItem("Contacts", lambda i, item: self.show("/contacts")),
            pystray.MenuItem("Bilan", lambda i, item: self.show("/bilan")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", lambda i, item: self.quit()),
        )
        self.icon = pystray.Icon("nomaia", Image.open(ICON_PATH), "NOMAIA", menu)
        return self.icon

    def notify(self, title, message):
        """Affiche une bulle Windows depuis l'icone."""
        try:
            if self.icon:
                self.icon.notify(message, title)
        except Exception:
            logger.debug("Notification systeme indisponible.", exc_info=True)

    # --- Arret --------------------------------------------------------------

    def quit(self):
        """Arrete les services, la fenetre et l'icone."""
        logger.info("Arret demande depuis la barre des taches.")
        self.quitting = True

        for nom in ("triage_worker", "telegram"):
            service = self.app.extensions.get(nom)
            if service:
                service.stop()

        self.server.shutdown()

        if self.icon:
            self.icon.stop()
        if self.window:
            self.window.destroy()


def main():
    """Point d'entree : serveur, fenetre native et icone."""
    config.ensure_data_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s : %(message)s",
        filename=str(config.LOG_PATH),
        encoding="utf-8",
    )

    if already_running():
        # Une seule instance peut tourner : le bot Telegram n'accepte qu'un
        # seul recepteur. On se contente de reveiller la fenetre existante.
        logger.info("Instance deja active : reveil de sa fenetre.")
        key = access_key(create_app(start_worker=False))
        if not wake_existing_instance(key):
            webbrowser.open(page_url(key))
        return 0

    app = create_app()
    server = ServerThread(app)
    server.start()
    logger.info("Serveur demarre sur %s", BASE_URL)

    launcher = Launcher(app, server, access_key(app))
    app.extensions["notifier"] = launcher.notify
    app.extensions["show_window"] = launcher.show

    icon = launcher.build_icon()

    if webview is None:
        logger.warning("pywebview indisponible : repli sur le navigateur.")
        launcher.show()
        icon.run()
        return 0

    # L'interface graphique exige le thread principal sous Windows : l'icone
    # de la barre des taches part donc dans un thread secondaire.
    launcher.create_window()
    WEBVIEW_STORAGE.mkdir(parents=True, exist_ok=True)

    webview.start(
        lambda: threading.Thread(target=icon.run, name="nomaia-tray", daemon=True).start(),
        private_mode=False,
        storage_path=str(WEBVIEW_STORAGE),
    )

    logger.info("Fenetre fermee, arret de NOMAIA.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
