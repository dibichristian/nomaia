# Contribuer à NOMAIA

1. Ouvrez une issue pour discuter d'un changement important avant de coder.
2. Forkez le dépôt et créez une branche depuis `main`.
3. Lancez les tests avant toute pull request :

   ```
   python -m unittest discover -s tests -t .
   ```

4. Messages de commit au format Conventional Commits
   (`feat(chat): ajouter…`, `fix(planner): corriger…`).
5. N'incluez jamais de données réelles (noms de clients, contacts, base
   SQLite, journaux) ni de clé d'API, même dans les tests.

Conventions de code : identifiants en anglais `snake_case`, commentaires et
docstrings en français, pas d'accents dans le code.

## Extension locale (`perso/`)

Pour ajouter du code propre à une installation sans modifier les fichiers
communs, créez un paquet `perso/` à la racine du programme. Il expose une
fonction `register(app)`, appelée une fois à la construction de l'application,
après les routes du cœur :

```python
# perso/__init__.py
from flask import Blueprint


def register(app):
    """Branche le code local sur l'application NOMAIA."""
    blueprint = Blueprint(
        "perso", __name__, url_prefix="/perso",
        template_folder="templates", static_folder="static",
    )
    # ... routes, gabarits, fichiers statiques
    app.register_blueprint(blueprint)
```

- Sans paquet `perso/`, rien ne change. Seul le `perso/` du dossier du
  programme est chargé, jamais un paquet homonyme installé ailleurs.
- Les routes de l'extension passent par les mêmes contrôles que le reste
  (clé d'accès, garde-fou anti-CSRF), sauf sous `/static/`, public par
  conception : gardez un `url_prefix` (par exemple `/perso`).
- Une erreur dans l'extension est écrite dans `nomaia.log` sans empêcher le
  démarrage ; les routes déjà enregistrées avant l'erreur restent actives.
- `nomaia.spec` embarque `perso/` dans l'exécutable s'il existe.
- Ne proposez jamais de dossier `perso/` dans une pull request.
