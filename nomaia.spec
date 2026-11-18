# Recette PyInstaller de NOMAIA.
#
#   python -m PyInstaller nomaia.spec --noconfirm
#
# Mode un dossier (et non un fichier unique) : demarrage plus rapide, pas
# d'extraction a chaque lancement, et bien moins de faux positifs antivirus.

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import config  # noqa: E402
from PyInstaller.utils.hooks import collect_all  # noqa: E402

# Gabarits, styles et images embarques a cote de l'executable.
donnees = [
    ("templates", "templates"),
    ("static", "static"),
    # Serveur MCP des sessions Claude Code : lance par Claude Code avec le
    # Python du poste, il doit exister en clair dans le dossier du programme.
    ("integrations", "integrations"),
]

# Modules charges dynamiquement, que l'analyse statique ne voit pas.
imports_caches = [
    "waitress",
    "pystray._win32",
    "win32crypt",
    "win32timezone",
    # Fenetre native : backend WebView2, charge dynamiquement par pywebview.
    "webview",
    "webview.platforms.edgechromium",
    "clr",
]

# Transcription des vocaux : faster-whisper embarque le filtre de silence
# (modele ONNX) dans ses donnees, et ctranslate2 / PyAV leurs DLL natives.
# Le modele Whisper lui-meme n'est pas embarque : il est telecharge au premier
# vocal dans le dossier de donnees.
binaires = []
# Synchro d'agenda : icalendar porte ses tables de fuseaux Windows, et tzdata
# la base IANA, absente de Windows, sans laquelle aucun horaire Outlook ne se
# convertit.
for paquet in (
    "faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers",
    "icalendar", "recurring_ical_events", "x_wr_timezone", "tzdata",
):
    paquet_donnees, paquet_binaires, paquet_imports = collect_all(paquet)
    donnees += paquet_donnees
    binaires += paquet_binaires
    imports_caches += paquet_imports

analyse = Analysis(
    ["tray.py"],
    pathex=[str(Path.cwd())],
    binaries=binaires,
    datas=donnees,
    hiddenimports=imports_caches,
    hookspath=[],
    runtime_hooks=[],
    # Bibliotheques scientifiques et interfaces graphiques inutiles ici :
    # les exclure fait gagner plusieurs dizaines de megaoctets.
    excludes=[
        # numpy reste embarque : faster-whisper en depend.
        "tkinter",
        "pandas",
        "matplotlib",
        "scipy",
        "pytest",
        # Tires par pywin32 et IPython, sans aucun usage ici : 40 Mo gagnes.
        "IPython",
        "jedi",
        "parso",
        "astroid",
        "zmq",
        "Pythonwin",
        "pythonwin",
        "sqlalchemy",
        # Tire par les hooks Hugging Face, jamais importe : 24 Mo gagnes.
        "boto3",
        "botocore",
    ],
    noarchive=False,
)

pyz = PYZ(analyse.pure)

executable = EXE(
    pyz,
    analyse.scripts,
    [],
    exclude_binaries=True,
    name="NOMAIA",
    debug=False,
    strip=False,
    upx=False,
    # console=False : aucune fenetre noire au lancement.
    console=False,
    icon=str(config.ICON_ICO),
    version_info={
        "version": config.VERSION,
        "description": "NOMAIA - assistant de taches",
        "product": config.APP_NAME,
    },
)

COLLECT(
    executable,
    analyse.binaries,
    analyse.datas,
    strip=False,
    upx=False,
    name="NOMAIA",
)
