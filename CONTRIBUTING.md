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
