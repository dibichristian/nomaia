"""Socle commun des tests : une base neuve et isolee par test."""
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import config
from app import create_app
from models import get_db, init_schema, setting


class NomaiaTestCase(unittest.TestCase):
    """Cas de test disposant d'une base vierge et d'un contexte applicatif.

    Chaque test repart d'un dossier de donnees neuf : aucun ordre d'execution
    n'est impose et un test qui salit la base ne contamine pas le suivant.
    """

    def setUp(self):
        """Cree une base temporaire et ouvre un contexte applicatif."""
        self._data_dir = Path(tempfile.mkdtemp(prefix="nomaia-case-"))

        config.DATA_DIR = self._data_dir
        config.DB_PATH = self._data_dir / "nomaia.db"
        config.BACKUP_DIR = self._data_dir / "public" / "backups"
        config.EXPORT_DIR = self._data_dir / "public" / "exports"
        config.UPLOAD_DIR = self._data_dir / "public" / "uploads"
        config.LOG_PATH = self._data_dir / "nomaia.log"
        config.ensure_data_dirs()

        init_schema()
        self.app = create_app(start_worker=False)
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Les tests s'appuient sur la date du jour : sans cela, ils
        # echoueraient le week-end ou un jour ferie. Les tests du calendrier
        # posent leurs propres jours ouvres.
        setting.set_value("work_days", "0,1,2,3,4,5,6")
        setting.set_value("holiday_country", "")

    def tearDown(self):
        """Ferme le contexte et efface le dossier temporaire."""
        self.ctx.pop()
        shutil.rmtree(self._data_dir, ignore_errors=True)

    # --- Fabriques ---------------------------------------------------------

    def client(self):
        """Retourne un client HTTP muni de la cle d'acces."""
        client = self.app.test_client()
        client.set_cookie("nomaia_access", setting.ensure_access_key())
        return client

    @staticmethod
    def write_headers():
        """En-tetes d'une ecriture acceptee par le garde-fou anti-CSRF."""
        return {"Origin": f"http://{config.HOST}:{config.PORT}"}

    @staticmethod
    def complete_task(task_id, tracked_min, done_at=None):
        """Marque une tache terminee et lui attache une session chronometree."""
        moment = done_at or datetime.now().replace(microsecond=0).isoformat(sep=" ")
        db = get_db()
        db.execute("UPDATE task SET status = 'done', done_at = ? WHERE id = ?", (moment, task_id))
        db.execute(
            "INSERT INTO session (task_id, started_at, ended_at, duration_min)"
            " VALUES (?, ?, ?, ?)",
            (task_id, moment, moment, tracked_min),
        )
        db.commit()
