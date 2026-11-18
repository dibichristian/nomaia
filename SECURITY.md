# Politique de sécurité

## Signaler une faille

Ne publiez pas de faille dans une issue publique.

Utilisez le signalement privé de GitHub : onglet **Security** du dépôt,
puis **Report a vulnerability**. Indiquez :

- la version ou le commit concerné ;
- les étapes pour reproduire ;
- l'impact estimé (lecture de données, exécution de code, contournement de
  l'accès…).

Un accusé de réception est envoyé sous 7 jours. Le correctif est publié dès
que possible, et le signalement est crédité si vous le souhaitez.

## Modèle de sécurité

NOMAIA est une application **locale et mono-utilisateur** :

- le serveur écoute uniquement sur `127.0.0.1` ;
- toutes les routes exigent une clé d'accès tirée au sort au premier
  démarrage (aucune clé ni mot de passe par défaut), sauf les fichiers
  statiques et la saisie du code d'appairage du téléphone (`POST /appairage`,
  code à usage unique, valable dix minutes, effacé après cinq essais faux) ;
- les écritures sont refusées si elles viennent d'une autre origine
  (protection contre les requêtes forgées) ;
- les routes du serveur MCP (`/api/agent/`) exigent une clé distincte,
  révocable ;
- sous Windows, les secrets (clés d'API, jeton Telegram) sont chiffrés au
  repos avec DPAPI ; ailleurs, ils restent en clair dans la base : protégez
  le dossier de données.

Hors périmètre : un programme lancé sous le même compte utilisateur, qui peut
lire la base directement.
