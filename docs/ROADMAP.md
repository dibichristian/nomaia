# Feuille de route

## Image Docker

**Objectif** : lancer NOMAIA en serveur seul (sans icône de barre des tâches)
avec `docker compose up`, sans affaiblir la sécurité de la version Windows.

**Pourquoi ce n'est pas encore fait** : les secrets (clés d'API, jeton
Telegram, clé d'accès) sont chiffrés par DPAPI, qui n'existe que sous Windows.
Dans un conteneur Linux, ils seraient stockés en clair. Le serveur n'écoute
aussi que sur `127.0.0.1`, ce qui le rend injoignable depuis l'hôte.

### Tâches

1. **Chiffrement portable des secrets** (`models/secret.py`)
   - Deux mécanismes de chiffrement : DPAPI sous Windows, Fernet
     (`cryptography`) ailleurs, avec une clé lue dans `NOMAIA_SECRET_KEY`.
   - Préfixe distinct (`fernet:`) pour reconnaître le mécanisme employé ; la
     reprise des valeurs en clair existe déjà (`setting.encrypt_legacy_secrets`).
   - Refuser de démarrer hors Windows sans `NOMAIA_SECRET_KEY`, plutôt que de
     stocker en clair.
   - Tests : chiffrement aller-retour, clé absente, clé changée (valeur
     illisible et message clair).

2. **Adresse d'écoute réglable** (`config.py`, `controllers/security.py`)
   - `NOMAIA_HOST` (défaut `127.0.0.1`) et `NOMAIA_PORT` (défaut `5555`).
   - `NOMAIA_PUBLIC_URL` pour l'origine admise par la vérification
     anti-requêtes forgées (`ALLOWED_ORIGINS`), qui suppose aujourd'hui
     `127.0.0.1`.
   - Avertissement dans le journal si l'écoute dépasse la boucle locale.
   - Tests : origines admises selon la configuration.

3. **Remise de la clé d'accès sans icône**
   - Aujourd'hui, seule l'icône de la barre des tâches ouvre l'interface avec
     la clé. Ajouter `python app.py --access-url`, qui affiche le lien
     d'ouverture, utilisable avec `docker compose exec`.
   - Ne jamais écrire la clé dans le journal.

4. **Dépendances séparées**
   - `requirements-server.txt` : flask, waitress, icalendar,
     recurring-ical-events ; faster-whisper en option (vocaux).
   - `requirements-desktop.txt` : le précédent, plus pystray, pywebview,
     pillow et pywin32.
   - Vérifier si `anthropic` est réellement importé ; le retirer sinon.

5. **Fichiers Docker**
   - `Dockerfile` : `python:3.11-slim`, utilisateur non root,
     `NOMAIA_DATA_DIR=/data`, `VOLUME /data`, `HEALTHCHECK`.
   - `docker-compose.yml` : port publié sur `127.0.0.1:5555:5555` uniquement,
     volume nommé pour `/data`, `NOMAIA_SECRET_KEY` lue depuis `.env`.
   - `.dockerignore` : bases, journaux, `build/`, `dist/`, `.venv*`.

6. **Fonctions limitées en conteneur** (à documenter, et à masquer dans
   l'interface)
   - Fournisseur « Claude Code (abonnement) » et lecture des mails Outlook :
     ils exigent le CLI `claude` sur le poste.
   - Ollama : joignable via `http://host.docker.internal:11434`.
   - Serveur MCP : il tourne sur l'hôte, avec `NOMAIA_URL` pointant vers le
     conteneur.

7. **Documentation et intégration continue**
   - Section « Installation via Docker » dans le README.
   - GitHub Actions : tests sous Windows et Linux, construction de l'image.

## Autres pistes

- Intégration continue : tests à chaque pull request (GitHub Actions).
- Interface et prompts en anglais (internationalisation).
