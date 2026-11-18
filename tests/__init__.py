"""Tests de NOMAIA, sur la bibliotheque standard uniquement.

Lancement : `python -m unittest discover -s tests -t .` depuis la racine.

Ce fichier est importe avant tout module de test : il detourne les donnees vers
un dossier temporaire AVANT que `config` ne soit charge. Sans cela, le premier
import de `config` figerait les chemins sur la vraie base et un test effacerait
les taches reelles.
"""
import os
import tempfile

os.environ.setdefault("NOMAIA_DATA_DIR", tempfile.mkdtemp(prefix="nomaia-tests-"))
