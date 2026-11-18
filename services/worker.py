"""Worker de triage : consomme la file des captures en arriere-plan.

Le triage prend 5 a 20 secondes selon le moteur : il ne doit jamais bloquer
une requete HTTP. La file vit en base, donc un arret brutal du PC ne perd
aucune capture (elles repartent en 'new' au redemarrage).
"""
import logging
import threading

from models import capture as capture_model
from models.capture import MAX_ATTEMPTS
from services import triage
from services.llm import LLMError

logger = logging.getLogger("nomaia.worker")

# Pause avant de rejouer une capture apres une panne passagere du moteur
# (Claude Code en cours d'auto-mise a jour, service Ollama qui redemarre).
TRANSIENT_BACKOFF_S = 45


class TriageWorker:
    """Thread unique qui traite les captures les unes apres les autres."""

    def __init__(self, app, interval=2.0):
        """:param app: application Flask (pour ouvrir un contexte par iteration)
        :param interval: delai d'attente en secondes quand la file est vide
        """
        self.app = app
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def start(self):
        """Demarre le thread de fond (idempotent)."""
        if self._thread and self._thread.is_alive():
            return

        with self.app.app_context():
            requeued = capture_model.reset_stuck_captures()
            if requeued:
                logger.info("%s capture(s) remise(s) en file au demarrage.", requeued)

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nomaia-triage", daemon=True)
        self._thread.start()

    def stop(self):
        """Demande l'arret du thread."""
        self._stop.set()

    def is_running(self):
        """Indique si le thread tourne toujours."""
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        """Boucle principale : reserve une capture, la traite, recommence."""
        while not self._stop.is_set():
            processed = False
            try:
                with self.app.app_context():
                    capture = capture_model.claim_next_capture()
                    if capture is not None:
                        processed = True
                        self._process(capture)
            except Exception as error:  # le thread ne doit jamais mourir
                self.last_error = str(error)
                logger.exception("Erreur inattendue dans le worker de triage.")

            # Enchainement immediat tant que la file n'est pas vide.
            if not processed:
                self._stop.wait(self.interval)

    def _notify_desktop(self, result):
        """Affiche une bulle Windows si le lanceur en a fourni le moyen."""
        notifier = self.app.extensions.get("notifier")
        if not notifier:
            return

        if result["outcome"] == "applied":
            tasks = result["tasks"]
            if len(tasks) == 1:
                message = f"Tache creee : {tasks[0]['title']}"
            else:
                message = f"{len(tasks)} taches creees."
            notifier("NOMAIA", message)
        elif result["outcome"] == "question":
            notifier("NOMAIA", "J'ai une question en attente dans la boite de reception.")
        else:
            notifier("NOMAIA", "Une proposition attend ta validation.")

    def _process(self, capture):
        """Traite une capture reservee et gere les echecs de moteur."""
        capture_id = capture["id"]
        bridge = self.app.extensions.get("telegram")
        try:
            result = triage.triage_capture(capture)
            logger.info("Capture %s traitee : %s", capture_id, result["outcome"])
            if bridge:
                bridge.notify_result(capture, result)
            self._notify_desktop(result)
        except LLMError as error:
            self.last_error = str(error)
            if capture["attempts"] >= MAX_ATTEMPTS:
                capture_model.set_status(capture_id, "failed", error=str(error))
                logger.error("Capture %s abandonnee : %s", capture_id, error)
                if bridge:
                    bridge.notify_failure(capture, error)
                return

            capture_model.requeue(capture_id)
            logger.warning("Capture %s renvoyee en file : %s", capture_id, error)

            if getattr(error, "transient", False):
                # Inutile de marteler un moteur momentanement absent : on laisse
                # passer la mise a jour avant de rejouer la capture.
                logger.info("Panne passagere, pause de %s s.", TRANSIENT_BACKOFF_S)
                self._stop.wait(TRANSIENT_BACKOFF_S)
        except Exception as error:
            self.last_error = str(error)
            capture_model.set_status(capture_id, "failed", error=str(error))
            logger.exception("Capture %s en echec.", capture_id)
