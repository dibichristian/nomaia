"""Configuration centrale de NOMAIA.

Deux racines distinctes :

- RESOURCE_DIR : le code, les gabarits et les fichiers statiques. En version
  installee, ce dossier est en lecture seule et remplace a chaque mise a jour.
- DATA_DIR : la base, les captures, les sauvegardes et le journal. Jamais
  touche par une mise a jour ni par une desinstallation ordinaire.

En developpement, les deux pointent sur le dossier du projet.
"""
import os
import sys
from pathlib import Path

APP_NAME = "NOMAIA"
VERSION = "1.0.0"

# PyInstaller pose sys.frozen et extrait les ressources dans sys._MEIPASS.
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    RESOURCE_DIR = Path(__file__).resolve().parent

# NOMAIA_DATA_DIR permet de deplacer les donnees (poste partage, disque dedie).
_override = os.environ.get("NOMAIA_DATA_DIR")
if _override:
    DATA_DIR = Path(_override)
elif FROZEN:
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
else:
    DATA_DIR = Path(__file__).resolve().parent

# Conserve pour le code existant : le dossier de reference des ressources.
BASE_DIR = RESOURCE_DIR

DB_PATH = DATA_DIR / "nomaia.db"
LOG_PATH = DATA_DIR / "nomaia.log"
BACKUP_DIR = DATA_DIR / "public" / "backups"
EXPORT_DIR = DATA_DIR / "public" / "exports"
UPLOAD_DIR = DATA_DIR / "public" / "uploads"

TEMPLATE_DIR = RESOURCE_DIR / "templates"
STATIC_DIR = RESOURCE_DIR / "static"
ICON_PNG = STATIC_DIR / "img" / "icon.png"
ICON_ICO = STATIC_DIR / "img" / "icon.ico"

# Images acceptees en capture.
ALLOWED_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Serveur local uniquement : jamais expose sur le reseau.
HOST = "127.0.0.1"
PORT = 5555

# Nombre de sauvegardes de la base conservees (rotation au demarrage).
BACKUP_KEEP = 7

# Libelles des priorites (affichage interface).
PRIORITY_LABELS = {
    1: "Critique",
    2: "Haute",
    3: "Normale",
    4: "Basse",
}

ENERGY_LABELS = {
    "low": "Faible",
    "med": "Moyenne",
    "high": "Forte",
}


def ensure_data_dirs():
    """Cree l'arborescence des donnees si elle n'existe pas encore."""
    for folder in (DATA_DIR, BACKUP_DIR, EXPORT_DIR, UPLOAD_DIR):
        folder.mkdir(parents=True, exist_ok=True)
