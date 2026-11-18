# NOMAIA — guide détaillé

Serveur local Flask + SQLite. Aucune donnée ne sort de la machine
(le serveur écoute uniquement sur `127.0.0.1`).

## Structure

| Dossier | Rôle |
|---|---|
| `models/` | Accès SQLite, schéma, validation, CRUD, secrets chiffrés |
| `controllers/` | Pages HTML, API JSON, contrôle d'accès |
| `services/` | Moteur LLM, chat, outils, triage, worker, Telegram, priorisation, agenda, interlocuteurs, bilan, rendez-vous |
| `templates/`, `static/` | Interface (Jinja2, CSS, JavaScript sans dépendance) |
| `tests/` | Tests unitaires (`unittest`, dépendances de `requirements.txt`) |
| `tools/` | Construction, installation, désinstallation, icônes |
| `%APPDATA%\NOMAIA\` | Données en version installée : base, captures, sauvegardes |

## Conversation

NOMAIA répond aux questions et exécute les ordres en langage naturel, depuis la
page **Chat** ou depuis Telegram.

```
Où en est ALPHA ?             lit les tâches du projet, répond avec les chiffres
Qu'est-ce que je fais ?       lit le plan du jour, donne le prochain créneau
Décale le crash paie à demain
Crée une tâche : rappeler le comptable, P2, 20 min
Lance le chrono sur la 14
```

**Sources cliquables.** Sous une réponse, les tâches, projets, contacts,
événements et conversations qu'elle cite deviennent des liens
(`services/chat_sources.py`). Ils sont tirés des résultats des outils lus
pendant le tour, jamais du texte du modèle : un lien pointe toujours vers un
élément qui existe. Un élément est retenu s'il est cité par son titre (accents
et casse ignorés, 40 premiers caractères) ou, pour une tâche, par `#id` ; 8 au
plus. Après un plan appliqué, les tâches modifiées ou créées sont liées ; une
tâche supprimée ou fusionnée ne l'est pas. Les liens sont gardés avec le
message (`chat_message.sources_json`). Telegram n'affiche pas les sources.

### Conversations séparées

Chaque sujet a sa conversation, avec son propre fil et son propre plan en
attente : on revient sur « Recette ORION » sans la mêler au chiffrage ALPHA.
La page **Chat** liste les conversations (la plus récemment active en tête,
filtre, archivées sur demande) ; on en ouvre une nouvelle, on la renomme, on
l'archive, on la vide ou on la supprime. Le titre se fixe tout seul sur le
début du premier message, sans appel au modèle. L'adresse `?c=<id>` rouvre
une conversation précise.

Seuls les derniers tours de la conversation ouverte repartent au modèle : le
coût en jetons ne grandit pas avec le nombre de conversations.

**Liens entre conversations, à la demande.** Les tâches, contacts et agenda
sont communs à toutes. Pour le reste, NOMAIA ne voit pas d'office les autres
fils : quand on y fait référence (« comme on a dit pour le chiffrage »), il
les cherche (`search_conversations`) puis les relit (`read_conversation`).
Aucun résumé n'est injecté à chaque message.

Un plan en attente appartient à sa conversation : un « oui » tapé ailleurs ne
l'applique jamais. L'historique du fil unique d'avant est rattaché, au premier
démarrage, à une « Conversation d'origine ».

**Sur Telegram**, la conversation suivie est mémorisée :

- `/convs` liste les conversations récentes, la suivie marquée `>` ;
- `/conv 3` reprend la conversation 3 et rappelle ses deux derniers messages ;
- `/newconv [titre]` en ouvre une nouvelle et la suit.

Une conversation supprimée ou archivée depuis le PC n'est plus suivie : les
messages repartent dans la conversation courante.

### Comment il agit

Le modèle ne dialogue pas librement : il répond toujours un objet JSON, soit
pour demander un outil, soit pour rendre sa réponse.

```
{"tool": "list_tasks", "args": {"project": "ALPHA"}}     NOMAIA exécute, réinjecte, reboucle
{"reply": "Une tâche ouverte, en retard de 3 jours."}    fin du tour
```

Ce protocole maison remplace le mécanisme d'appel d'outils natif des interfaces
de programmation : il n'existe pas sur `claude_cli` et diffère d'un fournisseur
à l'autre. Un seul chemin de code couvre ainsi les quatre. Le nombre d'outils
par message est plafonné (5 par défaut) ; au-delà, NOMAIA répond avec ce qu'il a.

### Outils

26 outils au total. Lecture : tâches, plan, projets, statistiques,
chronomètre, agenda, créneaux libres, interlocuteurs, attentes, doublons.
Écriture : créer, modifier, terminer, reporter une tâche, fusionner des
doublons, supprimer, chronométrer, créer ou annuler un rendez-vous,
enregistrer ou solder une attente, corriger un interlocuteur.

Les deux outils destructifs — `delete_task` et `merge_tasks` — portent un
marqueur qui **impose la confirmation**, quoi que le modèle décide. Le
catalogue le lui annonce, pour qu'il ne présente pas comme faite une action
qui attend encore un feu vert.

Le modèle ne sait rien de la base sans appeler un outil : le prompt lui interdit
d'avancer un chiffre qu'il n'a pas lu dans le tour en cours. Seul l'état du jour
(date, compteurs, projets actifs) est injecté d'office, quelques dizaines de
jetons qui évitent un appel sur les questions les plus fréquentes.

### Modifier en chaîne : le plan

Une seule phrase implique souvent plusieurs modifications. « La réunion à
planifier est pour demain 10 h » en appelle trois : terminer la tâche de
planification, poser le rendez-vous, créer la préparation.

NOMAIA les regroupe dans un **plan** et demande le feu vert :

```
toi    : la reunion a planifier est pour demain 10h
NOMAIA : Je vais faire :
         1. Terminer « Planifier la reunion equipe »
         2. Creer le rendez-vous 23/09 10:00-11:00
         3. Creer « Preparer le support »
         J'y vais ?
toi    : oui
NOMAIA : Fait.
         - Terminer « Planifier la reunion equipe »
         - Creer le rendez-vous 23/09 10:00-11:00
         - Creer « Preparer le support »
```

**La règle de confirmation vit côté serveur, pas dans le prompt.** Un modèle qui
oublie la consigne ne peut donc pas modifier la base en douce : un outil
d'écriture appelé en direct est traité comme un plan d'une étape et passe par la
même porte.

| Situation | Ce qui se passe |
|---|---|
| 1 modification anodine | exécutée directement |
| 2 modifications ou plus | **confirmation** |
| 1 modification destructive (`delete_task`, `merge_tasks`) | **confirmation** |

Un « oui », « ok », « vas-y » est reconnu **sans rappeler le modèle** : la
confirmation ne coûte ni jeton ni attente. Tout autre message abandonne le plan
et repart en conversation normale — répondre « plutôt 11 h » ne déclenche rien.

Le plan vit en base, pas en mémoire : il survit à un redémarrage, se retrouve au
rechargement de la page, et un plan proposé depuis Telegram se confirme depuis
le web. Il périme au bout de 30 minutes — sans cela, un « oui » tapé le
lendemain rejouerait des actions décidées sur un état de la base qui n'existe
plus.

Une étape qui échoue n'interrompt pas les suivantes : tu as validé un ensemble,
s'arrêter au milieu laisserait la base à moitié modifiée sans que tu saches où.
Le récapitulatif dit ce qui est passé et ce qui a échoué.

### Doublons

`find_duplicates` rapproche les tâches ouvertes dont les **mots du titre** se
recouvrent à 60 % ou plus. Deux formulations d'un même travail partagent leur
vocabulaire, pas leur ponctuation. Les mots vides (« pour », « faire »,
« tâche »…) sont écartés, sinon tout se ressemblerait.

`merge_tasks` fusionne, et l'arbitrage va au plus contraignant :

```
garder       la plus ancienne, elle porte l'historique
sessions     reattribuees            <- sans ca, du temps travaille disparait
notes        reattribuees
attentes     repointees vers la survivante
description  concatenees si elles different
echeance     la plus proche
priorite     la plus haute
projet, personne   la valeur renseignee l'emporte sur l'absente
```

La réattribution des sessions est le point qui compte : sans elle, fusionner
effacerait du temps réellement travaillé, ce qui fausserait le calibrage des
estimations et le bilan — les deux fonctions qui reposent dessus.

`delete_task` existe aussi, mais sa description dit au modèle de préférer la
fusion sur un doublon, et elle annonce le temps chronométré qui part avec.

### Fournisseur dédié

Le chat a son propre réglage `chat_provider`, séparé du triage. Un tour de chat
enchaîne jusqu'à cinq appels : sur `claude_cli`, qui recharge tout son contexte
à chaque fois, cela représente 250 000 jetons et plusieurs minutes pour une
seule question. La page **Chat** affiche un avertissement tant que ce
fournisseur est retenu. Le triage, lui, peut rester sur l'abonnement.

Le fil est plafonné à 12 tours rappelés au modèle ; l'affichage en garde 100.
Les résultats d'outils ne sont jamais stockés : ils sont relus à chaque tour sur
des données fraîches, sans quoi NOMAIA raisonnerait sur une base périmée.

Une panne de moteur n'enregistre rien : la question ne reste pas dans le fil
sans réponse.

## Fonctionnement du triage

```
dépôt brut (texte, capture d'écran, conversation)
      v
table `capture`  -> worker de fond -> fournisseur de modèle -> table `proposal`
      v
confiance >= 0.8 et aucune question  ->  tâches créées seules
sinon                                ->  NOMAIA pose ses questions, puis crée
```

Chaque tâche est rattachée à un projet via `project_hint`. Si le client ou le
module cité n'a pas encore de projet, NOMAIA le crée et lui attribue une couleur
déduite de son nom. Entre deux projets qui correspondent, le nom le plus long
gagne : un indice citant « ALPHA paie » va sur ce projet, pas sur « ALPHA ».
Réglable depuis **Config** (`auto_create_project`).

Répondre à une question de NOMAIA relance un triage neuf : le budget de quatre
tentatives ne compte que les pannes du moteur, jamais les tours de dialogue.

Seule la dernière proposition d'une entrée reste à valider : un nouveau triage
rejette les précédentes, et une entrée close rejette celles encore en attente.
Le compteur du menu et la cloche comptent les **entrées à traiter** (statut
autre que `done` : en attente de réponse, en analyse ou en échec), pas les
propositions.

**Liste de la boîte de réception** : filtre « À traiter / Tout » (« À traiter »
par défaut s'il y en a), « Afficher plus » au-delà des 30 entrées récentes
(`GET /api/capture?filter=open|all&limit=N`, 300 au plus). Sur grand écran,
liste et détail défilent chacun de leur côté : le détail reste visible quel que
soit l'endroit de la liste où l'on clique.

**Classer sans suite** : une entrée en attente ou en échec se ferme sans créer
de tâche (`POST /api/capture/<id>/dismiss`) ; sa proposition éventuelle passe
en « rejetée » et l'entrée reste lisible dans « Tout ».

## Fournisseurs de modèle

Le fournisseur se change depuis la page **Config**, sans redémarrage. Le modèle
retenu est mémorisé par fournisseur : basculer de l'un à l'autre n'efface pas le
choix précédent.

| Fournisseur | Clé | Jetons par triage | Coût |
|---|---|---|---|
| Claude Code (abonnement) | non | ~50 000 | inclus dans l'abonnement |
| API Claude | oui | ~500 | Haiku 1 $ / Sonnet 2 $ / Opus 5 $ par million en entrée |
| API Gemini | oui | ~500 | Flash Lite 0,10 $ / Flash 0,75 $ par million |
| Ollama (local) | non | ~500 | gratuit, 8 Go de VRAM |

**Ollama** : la liste des modèles proposée dans **Config** vient d'Ollama
lui-même (`/api/tags`), pas d'un catalogue figé dans le code. Un catalogue
fixe proposait des modèles absents du poste et cachait ceux qui étaient là.
Un modèle configuré mais non téléchargé s'affiche **NON INSTALLÉ** avec la
commande `ollama pull` à lancer, et **Tester la connexion** le refuse au lieu
de répondre OK sur un moteur qui échouerait en 404 à la première capture.
Un modèle sans vision est signalé : il traite le texte, pas les captures
d'écran. Ollama éteint, la liste connue sert de repli et l'interface le dit.

L'écart vient du CLI : il recharge son prompt système complet à chaque appel.
Mesuré sur dix triages réels — 528 000 jetons consommés pour 5 000 jetons de
contenu utile. Les interfaces de programmation n'envoient que le prompt de
NOMAIA, dont le bloc d'instructions stable est mis en cache et refacturé au
dixième ensuite.

Les clés sont chiffrées au repos par DPAPI, jamais renvoyées par l'API (seuls
les quatre derniers caractères s'affichent) et effacées des sauvegardes.

## Économie de jetons

- Bloc d'instructions stable envoyé en `system` : mis en cache par les
  fournisseurs qui le supportent.
- Contexte plafonné : 12 tâches ouvertes et 6 tours de dialogue par défaut,
  réglables. Chaque tâche listée coûte une quinzaine de jetons par triage.
- Titres tronqués à 70 caractères dans le contexte.
- Chaque appel est comptabilisé dans la table `usage` : jetons d'entrée, de
  sortie, servis du cache, et coût estimé. La page **Config** affiche la
  consommation des 30 derniers jours par modèle.

## Configuration

Tout se règle depuis `/config` : fournisseur et modèle, clés d'accès, délai,
plafonds de contexte, seuils d'autonomie, calibrage des durées, horaires de
travail, rendez-vous quotidiens, seuil de dérive, jeton Telegram.
Un bouton **Tester la connexion** envoie un appel minimal au fournisseur actif
et affiche le temps de réponse.

La page est rangée en six sections, une affichée à la fois (menu à gauche, en
rangée de pastilles sur petit écran), retenue dans l'adresse (`/config#journee`) :

| Section | Cartes |
|---|---|
| Général | Affichage, Système |
| Intelligence | Moteur d'intelligence, Économie de jetons, Conversation, Autonomie du triage |
| Journée et agenda | Journée de travail, Rendez-vous quotidiens, Rappel avant réunion, Agendas externes |
| Suivi et relances | Proactivité, Interlocuteurs et relances |
| Connexions | Tâches Odoo, Sessions Claude, Téléphone (Telegram), Téléphone (navigateur) |
| Consommation | Jetons et appels des 30 derniers jours |

## Sécurité

Le serveur n'écoute que sur `127.0.0.1`. Quatre barrières le protègent ; le
téléphone passe par Tailscale (voir « Sur le téléphone »).

**Clé d'accès.** Sur Windows, `127.0.0.1` est joignable par *tous* les comptes
de la machine, alors que le fichier de base n'appartient qu'à toi. Une clé
tirée au sort au premier démarrage, chiffrée en base, ferme donc l'interface
aux autres comptes et aux extensions de navigateur. L'icône de la barre des
tâches ouvre `?k=<clé>` ; le serveur la remplace aussitôt par un cookie
`HttpOnly` + `SameSite=Strict` valable un an, et nettoie l'URL. Ouvrir
`http://127.0.0.1:5555` sans cookie affiche une page d'accès refusé.

**Secrets chiffrés au repos** (`models/secret.py`). Le jeton Telegram et la clé
d'accès sont chiffrés par DPAPI, lié à ta session Windows : une base copiée,
synchronisée sur un cloud ou récupérée sur un disque volé est inexploitable
ailleurs. Après chiffrement, un `VACUUM` purge les pages libérées — sans lui,
l'ancienne valeur en clair resterait lisible dans le fichier.

**Serveur waitress** à la place du serveur de développement de Werkzeug, en
ligne de commande comme via l'icône.

**Requêtes inter-origines refusées** (`controllers/security.py`). Sans ce
garde-fou, n'importe quelle page web ouverte dans le navigateur pouvait créer
des tâches ou déclencher des triages : les envois simples et les formulaires
multipart échappent au contrôle CORS. Toute écriture dont l'`Origin` ou le
`Sec-Fetch-Site` n'est pas NOMAIA est rejetée en 403. Les lectures restent libres.
Seule origine distante admise : l'adresse Tailscale réglée dans Config >
Téléphone (`mobile_origin`).

**Lecture du sous-processus limitée au dossier des captures.** Le triage passe
par `--allowedTools "Read(<uploads>/**)"`. Attention : `--add-dir` **ajoute** un
accès, il ne restreint rien, et `--allowedTools Read` seul autorise la lecture
de tout le disque accessible à l'utilisateur. Seul le motif `Read(chemin/**)`
impose une frontière. Aucun outil d'écriture, de commande ou de réseau n'est
accordé : une capture hostile ne peut donc rien exécuter ni exfiltrer.

**Aucun secret dans les sauvegardes.** `backup_db()` efface le jeton Telegram
de chaque copie. Restaurer une sauvegarde impose de saisir à nouveau le jeton.
La copie passe par l'API `backup()` de SQLite, pas par une copie de fichier :
en mode WAL, les dernières transactions vivent dans le fichier `-wal` qu'une
copie brute laisserait de côté.

Limite assumée : un programme lancé sous ton propre compte Windows peut lire
`nomaia.db`, donc la clé. Aucune mesure applicative n'y change rien — c'est le
modèle de sécurité du système. La seule parade serait un compte Windows dédié,
incompatible avec une icône dans la barre des tâches.

Autres points : requêtes SQL paramétrées et colonnes en liste blanche, échappement
HTML de toute donnée affichée, couleurs de projet validées en hexadécimal strict,
envois d'images filtrés par extension et taille, et filtrage Telegram sur le seul
`chat_id` appairé.

## Accès concurrent à la base

Trois écrivains se partagent `nomaia.db` : les threads du serveur waitress, le
worker de triage et le sondeur Telegram. La base est donc ouverte en **WAL**
(lectures possibles pendant une écriture) avec une attente sur verrou de 5
secondes. Sans ces deux réglages, une écriture concurrente échouait aussitôt
sur `database is locked` au lieu de patienter le temps de la transaction
voisine. Le mode WAL est une propriété du fichier : il se pose une fois et se
conserve, y compris sur une base créée par une version antérieure.

## Rechargement à chaud

Les pages ouvertes suivent les modifications faites ailleurs (session Claude,
Telegram, triage, synchronisation Odoo ou Outlook, mails) sans F5.

- **Principe** : une table `data_revision` tient un compteur unique, incrémenté
  par des triggers SQL à chaque écriture sur les tables affichées (tâches,
  sessions, agenda, captures, contacts, projets et étapes, journal, notes,
  comptes rendus, conversations, pièces jointes…). Tous les écrivains sont
  couverts sans toucher à leur code. Réglages, consommation du modèle, index de
  recherche et mémoire technique (alertes, Telegram, mails vus) restent hors
  suivi : ils changent sans rien modifier à l'écran. Liste et justification :
  `models/revision.py`.
- **Sondage** : la page relit `GET /api/revision` toutes les **10 s** quand la
  fenêtre est visible, et aussitôt à son retour au premier plan. Si le compteur
  a bougé, elle recharge ses propres données (pas la page entière) en gardant
  défilement, filtres, sélection et onglet, sans rejouer les animations
  d'entrée. La cloche, la pastille du menu et le chronomètre suivent aussi.
- **Différé pendant une saisie** : fenêtre modale ouverte, champ de saisie
  actif et non vide, liste déroulante active ou glisser-déposer en cours
  repoussent la mise à jour jusqu'à ce que la condition cesse. L'éditeur d'une
  note ou d'un compte rendu n'est jamais écrasé : la note en édition est
  ignorée, le compte rendu n'est relu que sans modification en attente. Le chat
  attend la fin d'une réponse en cours.

## Installation

NOMAIA s'installe comme une application Windows ordinaire, sans droits
administrateur et sans Python sur le poste de destination.

```powershell
powershell -ExecutionPolicy Bypass -File tools\build.ps1 -Install
```

`build.ps1` produit `dist\NOMAIA\` (~310 Mo, dont ~250 pour la transcription
locale des vocaux ; exécutable autonome via PyInstaller),
puis `install.ps1` met en place :

| Emplacement | Contenu |
|---|---|
| `%LOCALAPPDATA%\Programs\NOMAIA` | le programme, remplacé à chaque mise à jour |
| `%APPDATA%\NOMAIA` | base, captures, sauvegardes, journal — jamais touchés |
| Menu Démarrer, Bureau, Démarrage | raccourcis vers `NOMAIA.exe` |
| Registre `HKCU` | entrée dans Paramètres > Applications |

Une installation précédente en mode développement est reprise automatiquement :
la base et les captures du dossier projet sont recopiées au premier passage.

**Désinstallation** depuis Paramètres > Applications, ou en lançant
`uninstall.ps1` présent dans le dossier du programme. Les données sont
**conservées** par défaut ; l'option `-RemoveData` les supprime.

**Fausse alerte antivirus.** Windows Defender peut signaler `NOMAIA.exe`
(`Trojan:Win32/Bearfoos.A!ml`, détection par apprentissage automatique) : le
lanceur fourni avec PyInstaller est le même pour tous, malwares compris. Pour
s'en défaire, recompiler ce lanceur sur le poste, une fois puis à chaque
nouvelle version de PyInstaller :

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_bootloader.ps1
```

Prérequis : les outils de compilation C++ de Visual Studio (`winget install
Microsoft.VisualStudio.2022.BuildTools` avec la charge de travail `VCTools`).
Le script crée l'environnement isolé `.venv-build\` (le Python global n'est pas
touché), compile le lanceur depuis les sources de PyInstaller (`build-cache\`)
et vérifie qu'il diffère du lanceur standard. `build.ps1` utilise ensuite
`.venv-build\` d'office ; le supprimer revient au lanceur standard. Ce n'est
pas une garantie : seule une signature de code supprime l'alerte à coup sûr.

**Dépendance externe** : Claude Code doit rester installé pour le triage
(`npm install -g @anthropic-ai/claude-code`). L'installeur prévient s'il est absent.

## Sur le téléphone

L'interface s'adapte au téléphone : menu en **bulle flottante** en bas de
l'écran (Réception, Chat, Plan, Tâches, et « Plus » pour les autres pages dans
un panneau qui monte du bas), fenêtres ouvertes en feuille pleine largeur,
champs en 16 px (sinon iOS zoome à chaque saisie), cibles tactiles d'au moins
44 px. La bulle s'efface quand le clavier est ouvert. Sur PC, le menu du haut
devient une capsule, avec la même bulle qui glisse d'une page à l'autre.

### Accès par Tailscale

Le serveur reste sur `127.0.0.1` : le téléphone le joint par Tailscale, un
réseau privé chiffré entre tes appareils, au bureau comme dehors.

1. Installer Tailscale sur le PC et le téléphone, même compte.
2. Sur le PC, une fois : `tailscale serve --bg 5555`. Tailscale publie NOMAIA
   en HTTPS sur ton réseau privé seulement (`https://<pc>.<tailnet>.ts.net`).
3. Config > Téléphone : coller cette adresse, enregistrer.
4. Sur le téléphone, ouvrir l'adresse : la page d'accès demande un **code
   d'appairage**. Config > Téléphone > « Afficher un code », le saisir.

Le code est à usage unique, valable dix minutes, et effacé après cinq essais
faux : le deviner est hors de portée. Il est chiffré en base et absent des
sauvegardes, comme les autres secrets. Il donne le même cookie d'un an que
l'icône de la barre des tâches.

### Écran d'accueil

Sur le téléphone, « Ajouter à l'écran d'accueil » installe NOMAIA comme une
app : icône dédiée, plein écran, sans barre d'adresse (`static/manifest.json`,
icônes produites par `tools/make_icon.py`). Sur iPhone, l'app installée garde
ses propres cookies : il faut l'appairer une fois de plus, depuis l'app.

Limite : rien ne fonctionne PC éteint. Pour consulter ses tâches dans ce cas,
voir « Consulter ses tâches PC éteint » (Telegram).

## Fenêtre et lancement

NOMAIA s'ouvre dans sa **propre fenêtre**, pas dans un navigateur : pas de barre
d'adresse, pas d'onglet, une entrée distincte dans la barre des tâches. Le rendu
s'appuie sur WebView2, le moteur livré avec Windows 10 et 11.

**Fermer la fenêtre ne quitte pas l'application** : elle se replie dans la barre
des tâches, où le triage et l'écoute Telegram continuent. Pour arrêter
réellement NOMAIA : clic droit sur l'icône, puis Quitter.

Relancer l'application alors qu'elle tourne déjà ne démarre pas un second
serveur : la fenêtre existante revient au premier plan (route interne
`POST /api/window/show`). Deux instances se voleraient les messages du bot.

Menu de l'icône : Ouvrir, Boîte de réception, Chat, Plan du jour, Agenda,
Tâches, Contacts, Bilan, Quitter.

Si pywebview ou WebView2 manque, NOMAIA se rabat sur le navigateur par défaut
au lieu de refuser de démarrer.

En développement : `python tray.py` (fenêtre native) ou `python app.py`
(serveur seul). Les données restent alors dans le dossier du projet.

Journal : `%APPDATA%\NOMAIA\nomaia.log` en version installée, `nomaia.log` à la
racine du projet en développement.

### Empreinte mémoire

| Élément | Mémoire |
|---|---|
| Service seul, fenêtre fermée | ~65 Mo |
| Avec la fenêtre ouverte | ~520 Mo (6 processus WebView2) |

C'est le prix d'un moteur Chromium. Le mode navigateur, lui, ne coûte rien de
plus que l'onglet ouvert.

## Dépôt depuis le téléphone (Telegram)

Le bot fonctionne en *long polling* : aucun port ouvert, aucun nom de domaine,
rien d'exposé sur Internet. PC éteint, Telegram garde les messages 24 h et
NOMAIA les récupère au redémarrage.

Appairage, une seule fois : ouvrir le bot sur le téléphone et envoyer
`/pair <code>`, le code étant affiché en bannière sur la boîte de réception.
Ensuite, seul ce chat est accepté ; tout autre est ignoré.

Un message texte court reçoit une réponse de NOMAIA : c'est la même
conversation que sur la page **Chat**, avec les mêmes outils. Une image, ou un
texte de plus de 400 caractères, part au triage : un message client transféré
est un dépôt, pas une question. `/note <texte>` force le triage sur un texte
court, et le seuil se règle depuis **Config**.

Si le moteur échoue pendant une conversation, le message n'est pas perdu : il
repart au triage, qui dispose de son propre budget de tentatives.

Commandes : `/plan`, `/taches`, `/bilan`, `/note`, `/mail`, `/mails`,
`/nouveau`, `/convs`, `/conv <n>`, `/newconv`, `/aide`.

### Répondre à NOMAIA depuis le téléphone

Quand le triage pose une question, elle part sur Telegram en entier : résumé,
message de NOMAIA, questions et réponses suggérées. C'est vrai aussi pour une
entrée déposée depuis le PC. Une proposition peu sûre ou un échec d'analyse
partent de la même façon pour les entrées venues du téléphone ou d'un mail.

**Répondre** à l'un de ces messages (glisser vers la droite) poursuit l'entrée
dont il parle, même si d'autres questions sont arrivées entre-temps. Chaque
message envoyé est lié à son entrée (table `telegram_link`, liens purgés après
60 jours). Sans citer de message, la réponse va à la dernière question d'une
entrée venue du téléphone ; `/nouveau` y coupe court.

Côté PC, la boîte de réception propose une zone de réponse sur toute entrée en
attente, question ou non, et la saisie en cours survit au rafraîchissement
automatique.

### Vocaux

Un vocal est transcrit **sur le PC**, puis suit exactement le circuit d'un
texte tapé : réponse à une question du triage en attente, conversation s'il est
court, triage s'il est long. NOMAIA renvoie d'abord ce qu'il a compris
(`Entendu : "..."`), pour qu'une erreur de reconnaissance se voie avant d'agir.
Une légende éventuelle précède la transcription : elle donne le contexte
(client, projet).

Moteur : [faster-whisper](https://github.com/SYSTRAN/faster-whisper), sur CPU
et en `int8`. Rien ne quitte le poste. Le format natif de Telegram (OGG/Opus)
est décodé directement, sans ffmpeg. L'audio est effacé dès la transcription,
qu'elle réussisse ou non.

| Modèle | Téléchargement | Qualité en français |
|---|---|---|
| `tiny` | ~75 Mo | approximative |
| `base` | ~145 Mo | correcte |
| `small` (défaut) | ~480 Mo | fiable |

Le modèle est téléchargé au **premier vocal** dans `DATA_DIR/models/whisper`,
jamais à l'installation : ce premier vocal prend donc le temps du
téléchargement. Une fois chargé, le modèle reste en mémoire et il est libéré
après dix minutes sans vocal, ce qui laisse l'empreinte du service seul
inchangée le reste du temps.

Refus annoncés, jamais silencieux : vocaux désactivés, vocal plus long que
`voice_max_s` (300 s par défaut, refusé avant téléchargement), moteur absent,
aucune parole reconnue. Langue forcée sur `fr` par défaut, car la détection
automatique se trompe sur les vocaux courts. Tout se règle dans
**Config > Conversation**.

### Consulter ses tâches PC éteint

NOMAIA est un service local : machine arrêtée, il ne répond plus. La parade ne
coûte rien et n'expose rien de plus que ce qui transite déjà par le bot : un
**unique message épinglé** en tête du chat, que NOMAIA édite à chaque
changement. Telegram le conserve sur ses serveurs, donc le téléphone l'affiche
hors ligne, PC arrêté, en un tap.

```
NOMAIA - etat au 16/09 a 08:12
7 tache(s) ouverte(s), 6 h 40 estimees

EN RETARD (2)
P1 Corriger le crash paie [ALPHA] - 12/09
...
AUJOURD'HUI (3)
...
```

Le message est réécrit après chaque rendez-vous quotidien et après toute
écriture sur les tâches, les projets ou les captures. L'interface se contente
de le marquer périmé : l'envoi part du battement de cœur, hors requête HTTP,
et au plus une fois par minute — cocher dix tâches d'affilée ne déclenche pas
dix appels à Telegram. L'épingle ne fait jamais sonner le téléphone.

Un message supprimé à la main est reposé et ré-épinglé au passage suivant.
Réglable depuis **Config** (`telegram_pinned_digest`).

Le jeton du bot vit dans la table `setting` (clé `telegram_token`), jamais dans
le code ni dans un fichier versionné. Le remplacer après une révocation :

```sql
UPDATE setting SET value = 'nouveau_jeton' WHERE key = 'telegram_token';
```

## Plan du jour

Le plan est recalculé à chaque affichage, jamais stocké : cocher une tâche
réorganise immédiatement le reste de la journée.

Le bouton **Faites (N)** (éteint par défaut, retenu par appareil) range dans le
fil du jour les tâches terminées aujourd'hui, à l'heure de leur première
session du jour (sinon de leur clôture), dans la période correspondante, avec
le temps passé ce jour-là (`done_today` de `GET /api/plan`).

Score d'une tâche :

```
score = (5 - priorite) * 30          P1 = 120 … P4 = 30
      + urgence                       retard 130-200, aujourd'hui 100, demain 70…
      + 12 si duree <= 30 min         les tâches courtes libèrent l'esprit
      - 15 si issue de l'IA non relue
```

Placement : le matin favorise les tâches exigeantes (`energy = high`, +25),
l'après-midi les tâches légères (+10). Les étapes d'une même capture gardent
leur ordre de création — l'IA les émet dans l'ordre logique, donc un correctif
n'est jamais testé avant d'être écrit.

Chaque estimation est majorée d'une marge (15 % par défaut). Ce qui ne rentre
pas dans la journée tombe dans « Ne rentre pas dans la journée ».

Réglages modifiables depuis le bouton **Horaires** : début, fin, pause
déjeuner, marge, horizon de planification.

### Reprendre la main sur l'ordre

Le classement propose, l'utilisateur dispose. Trois gestes :

- **Glisser-déposer** une tâche dans le plan : l'ordre affiché devient l'ordre
  du jour (`POST /api/plan/order`).
- **⇧** sur un créneau, ou **Maintenant** sur une tâche qui ne rentrait pas :
  la tâche passe en tête de journée (`POST /api/plan/pin/<id>`).
- **📌** sur une tâche épinglée la rend au classement automatique ; le bouton
  **Ordre automatique** oublie tout l'ordre manuel du jour.

Techniquement, une tâche épinglée porte `pinned_day` et `pinned_order`. Le
plan de ce jour place d'abord les épinglées, dans l'ordre choisi, avant le
classement automatique ; elles passent outre le score, l'horizon et l'ordre
des étapes d'une capture. Un épinglage n'a d'effet que le jour visé : celui
d'un jour passé est simplement ignoré. Dans le chat, l'outil `pin_task` fait la
même chose (« je fais le devis maintenant »).

### Détailler une tâche

Un clic sur le titre ouvre sa fiche : titre, description, priorité, durée,
échéance, énergie, projet, et ses **étapes**. Une étape est une sous-tâche
(`parent_id`) qui hérite du projet, de l'échéance et de la priorité. Une tâche
découpée en étapes ouvertes est planifiée **par ses étapes** : la placer aussi
compterait deux fois le même travail.

### Jours passés

Planifier un jour écoulé n'a pas de sens. Naviguer vers un jour passé affiche
à la place :

1. **Bouclées ce jour-là** : les tâches terminées à cette date ;
2. l'agenda du jour ;
3. **Oublié de cocher ?** : les tâches encore ouvertes qui existaient déjà ce
   jour-là. Les cocher les clôt **à cette date**
   (`POST /api/tasks/<id>/done-on`), pour que le bilan et le calibrage les
   rangent dans la bonne journée.

### Calibrage des durées

Les estimations de NOMAIA sont comparées au temps réellement chronométré, par
projet, sur 90 jours glissants. L'écart mesuré est injecté dans le prompt de
triage sous forme de facteur :

```
CALIBRAGE (temps reel / temps estime, 90 derniers jours) : global x1.49 ; ALPHA x2.0 ; ORION x0.92
```

Garde-fous : au moins quatre tâches chronométrées avant de conclure, quatre
projets détaillés au maximum, et rien n'est dit tant que l'écart reste sous
15 %. Une tâche dont le ratio sort de l'intervalle 0,4–4 est écartée de la
mesure sans l'annuler — un chronomètre laissé tourner toute la nuit ne doit pas
priver le triage de calibrage. Le facteur s'affiche aussi sur la page **Bilan**,
et se coupe depuis **Config**.

## Agenda

Une réunion n'est pas une tâche : elle ne se coche pas, ne se chronomètre pas,
ne se score pas. Sa seule influence est de **retirer du temps à la journée**.

Sans agenda, `build_plan()` calculait la capacité comme `fin - début - pause` :
une réunion de 14 h à 16 h n'existait pas, NOMAIA promettait deux heures qu'il
n'avait pas, et la charge affichée était fausse. Ce chiffre alimente l'alerte
de surcharge, le point du matin et, de proche en proche, le calibrage des
durées.

```
avant   [08:30-12:30 matin] [13:30-18:00 apres-midi]      capacite 510 min
apres   [08:30-12:30 matin] [13:30-14:00] [16:00-18:00]   capacite 390 min
        reunion 14:00-16:00 soustraite
```

Les fragments héritent de l'étiquette de leur fenêtre mère : le bonus d'énergie
matin ou après-midi continue de s'appliquer. Un fragment de moins de 15 minutes
est écarté du placement, mais compté comme perdu — l'ignorer gonflerait la
capacité.

**La fusion précède la soustraction.** Deux réunions qui se recouvrent,
retirées l'une puis l'autre, retireraient deux fois le temps commun et
produiraient une capacité fausse, voire négative. Une réunion hors des heures
de travail, ou pendant la pause, ne retire rien.

### Lisibilité de la grille

Un créneau de 30 minutes ne laisse pas la place à l'heure **et** au titre l'un
sous l'autre : sous 44 px de haut, le bloc passe sur une ligne, titre en tête,
tronqué proprement. L'info-bulle de chaque bloc donne heure, titre, type, lieu
et début des notes.

Un clic ouvre une **fiche de détail** : date et heures, type, lieu, projet,
répétition, source, et les notes en entier avec leurs liens cliquables. Le
bouton **Modifier** y mène au formulaire ; il est absent pour un événement
importé, en lecture seule.

### Quatre types

| Type | Sens |
|---|---|
| `meeting` | réunion, rendez-vous |
| `focus` | créneau bloqué pour travailler sans être dérangé |
| `absence` | indisponible — ne déclenche aucun rappel |
| `personal` | sport, famille, médecin — rappelé, marqué `[Perso]` dans le point du matin |

Un événement `personal` ne retire du temps que s'il tombe pendant les heures
de travail : le sport à 19 h ne touche pas au plan, le médecin à 10 h si.

### Répétitions

Quotidienne ou hebdomadaire, avec une date de fin facultative, plus
l'annulation d'une occurrence isolée. Pas de règle plus riche : sans
répétition du tout, un point hebdomadaire se ressaisit tous les lundis ; avec
une grammaire complète, on écrit un moteur de calendrier.

Les occurrences sont **calculées à la demande**, jamais stockées : une série
vit dans une seule ligne. Matérialiser imposerait de décider jusqu'à quand,
puis de rattraper chaque fois que l'horizon avance. Déplacer une série purge
ses annulations : elles portaient sur une cadence qui n'existe plus.

### Rappel avant réunion

NOMAIA prévient dix minutes avant chaque rendez-vous, sur Telegram et en bulle
Windows. Le délai se règle depuis **Config**.

Le filigrane retient le début de la dernière occurrence annoncée, en base et
non en mémoire : redémarrer NOMAIA ne renvoie pas les rappels déjà partis.
Deux réunions à la même heure partent ensemble. Un rappel en retard de plus de
dix minutes est abandonné — allumer le PC à 15 h ne doit pas annoncer la
réunion de 9 h comme imminente.

### Où l'agenda apparaît

- **Agenda** (`/agenda`) : vue semaine, saisie, modification, annulation. En
  dessous de 860 px, la grille bascule en liste par jour.
- **Plan du jour** : les réunions s'intercalent entre les créneaux de travail,
  et le temps d'agenda est annoncé à côté de la charge.
- **Point du matin** : rendez-vous cités dans le fil horaire. Une journée sans
  tâche mais pleine de réunions n'est plus annoncée comme vide.
- **Instantané épinglé** : les rendez-vous du jour figurent en tête, avant les
  tâches. PC éteint, c'est la seule façon de savoir ce qui attend.
- **Chat** : « bloque 2 h jeudi matin », « quand suis-je libre ? ».

## Jours de repos

Un jour de repos ne raccourcit pas la journée comme une réunion : il la
supprime. Trois sources, une seule règle (`services/workdays.py`) :

| Source | Réglage |
|---|---|
| Jours de la semaine non travaillés | `work_days`, lundi à vendredi par défaut (**Config > Journée**) |
| Congés | saisis dans l'**Agenda** ou au chat (« je suis en congé du 12 au 16 ») |
| Jours fériés | importés selon `holiday_country` : Côte d'Ivoire par défaut, Sénégal, France ou aucun |

Ce jour-là :

- **Plan** : capacité nulle, aucun créneau, bandeau « Jour de repos ». Le
  contrôle vit dans `_windows()` du planificateur, point unique d'où découlent
  la capacité, les trous libres et la charge.
- **Messages** : ni point du matin, ni point du soir, ni relance, ni alerte de
  dérive. Le rendez-vous est marqué tenu, il ne part pas plus tard.
- **Rappels d'agenda** : maintenus. Un sport du samedi reste rappelé.
- **Captures et chat** : fonctionnent normalement, rien n'est perdu.
- **Instantané épinglé** : « Aujourd'hui : repos (motif) » en tête.

### Échéances en jours ouvrés

Le score d'urgence compte le temps de travail restant, pas les jours
calendaires : une échéance lundi vue vendredi vaut « demain ». Une échéance
passée reste un retard même si aucun jour ouvré ne s'est écoulé (due samedi,
vue dimanche). Au-delà de 62 jours, le score est saturé de toute façon : le
calcul retombe sur les jours calendaires.

Le calendrier est chargé en **une requête par requête HTTP** (cache `flask.g`),
pas une par tâche classée.

### Fériés

Seuls les fériés calculables sont importés : dates fixes et fêtes dérivées de
Pâques (lundi de Pâques, Ascension, lundi de Pentecôte). Les fêtes musulmanes
(Tabaski, Aïd el-Fitr, Maouloud…) dépendent de l'observation de la lune et sont
fixées par décret : les calculer donnerait une date fausse une année sur deux.
Elles se saisissent à la main, comme un congé.

L'import est **automatique et unique** : l'année en cours et la suivante, une
fois par pays (`holidays_imported`). Un férié supprimé à la main ne revient
donc pas. Changer de pays retire les fériés futurs de l'ancien, jamais le passé
ni les saisies manuelles. Le bouton « Ajouter les fériés » de l'agenda
réimporte une année à la demande, sans doublon.

Ajouter un pays : une entrée dans `COUNTRIES` de `services/holidays.py`.

## Mails Outlook (via le connecteur Claude)

NOMAIA lit la boîte Outlook **sans aucun accès propre** : il lance Claude Code
en ligne de commande (`claude -p`), qui passe par le connecteur Microsoft 365
branché sur le compte Claude. Aucun jeton, aucune application Azure côté
NOMAIA. **Lecture seule** : seuls `outlook_email_search` et `read_resource` sont
autorisés. Code : `services/mail_reader.py`.

**Fonctionnement** : Claude cherche les mails reçus depuis le dernier passage
réussi (24 h au premier passage, 15 min de recouvrement), lit le corps des mails
retenus et rend pour chacun l'expéditeur, l'objet, la date et l'extrait utile
(sans signature ni historique cité, 4 000 caractères au plus). Chaque mail
devient une entrée de la boîte de réception, source `outlook`, que le triage
analyse comme les autres. Un mail déjà déposé ne l'est plus jamais (table
`mail_seen`, clé `message_id`), même si son entrée a été supprimée.

**Périmètre** : expéditeur connu (adresse, nom ou alias d'un interlocuteur) ou
mail qui cite un projet actif (mot entier, objet ou corps). Newsletters,
notifications automatiques (`no-reply`…) et mails de l'utilisateur sont écartés. Le
tri est demandé à Claude **puis refait par NOMAIA**. Un secret présent dans un
mail (mot de passe, clé d'API, clé privée) est remplacé par `***`.

**Rythme** : toutes les heures pendant les heures de travail réglées (jours
travaillés, entre début et fin de journée), dans un thread à part du battement
de cœur. À la demande : bouton **Lire mes mails** de la boîte de réception,
**Lire maintenant** dans *Config > Connexions*, ou `/mails` sur Telegram.

**Validation** : toujours. Une entrée `outlook` n'applique jamais seule sa
proposition, quelle que soit la confiance du triage.

**Coût indicatif** : ~0,12 $ par passage (une douzaine de secondes), imputé à
l'abonnement Claude.

**Prérequis** : connecteur Microsoft 365 branché sur le compte Claude, et
fournisseur *Claude Code (abonnement)* choisi dans *Config > Intelligence* ;
sinon la lecture est refusée avec un message. Activation (`mail_enabled`,
désactivé par défaut), état du dernier passage, nombre importé et dernière
erreur : *Config > Connexions > Mails Outlook (via Claude)*.

## Agenda Outlook (synchro ICS)

NOMAIA relit l'agenda Outlook publié et en garde une copie **en lecture
seule**. Sens unique : Outlook fait foi. Pas d'OAuth, pas d'application à
déclarer chez Microsoft, rien d'ouvert sur le réseau.

**Mise en place** : Outlook web > Paramètres > Calendrier > Calendriers
partagés > *Publier un calendrier*, niveau « Peut afficher tous les détails »,
puis coller le lien **ICS** dans **Config > Agendas externes**. La première
synchro part à l'enregistrement : une adresse fausse se voit tout de suite.

Le lien donne accès à tout l'agenda sans mot de passe : c'est un secret. Il
est chiffré au repos (DPAPI), jamais renvoyé par l'API (seulement un indice
`hôte/…fin`), jamais écrit au journal, et purgé des sauvegardes.

### Ce qui est importé

| Statut Outlook | Dans NOMAIA |
|---|---|
| Occupé, Provisoire | réunion (`meeting`) |
| Absent du bureau, horaire | `absence` |
| Absent du bureau, journée entière | jour de repos |
| Libre, Ailleurs, transparent, annulé | ignoré — ne bloque aucun temps |
| Autre journée entière (anniversaire…) | ignoré |

Les répétitions Outlook (exceptions, occurrences déplacées, fuseaux Windows,
heure d'été) sont développées par `recurring-ical-events`, pas par le moteur
de l'agenda : leur grammaire dépasse de loin les séries quotidiennes et
hebdomadaires de NOMAIA. Chaque occurrence devient une ligne simple, en heure
locale. Une réunion à cheval sur minuit est coupée par jour.

La description de l'invitation (ordre du jour, lien Teams) est importée dans
les notes, lignes vides resserrées, 2 000 caractères au plus. Elle apparaît
dans la fiche de détail, liens cliquables.

### Synchronisation

Toutes les `calendar_sync_min` minutes (15 par défaut), depuis le battement de
cœur, ou à la demande (bouton « Synchroniser »). Fenêtre glissante J-7 → J+60,
remplacée **dans une seule transaction** : une réunion supprimée dans Outlook
disparaît, une réunion déplacée bouge. Le passé antérieur à la fenêtre est
conservé.

Un échec (réseau, flux illisible, HTTP 404 après dépublication) est affiché
sur la source et **laisse les données précédentes intactes** : une coupure ne
vide pas l'agenda.

Outlook ne republie son flux que toutes les 15 à 30 minutes : une réunion
posée à l'instant met ce délai à apparaître, quelle que soit la fréquence de
relecture.

### Lecture seule

Un événement ou une absence importés ne se modifient ni dans l'interface, ni
par l'API, ni par le chat : la synchro suivante écraserait la modification
sans prévenir. Réponse : « modifie-le dans Outlook ». Supprimer **ou
désactiver** la source retire tout ce qu'elle a importé : des réunions figées,
que plus rien ne met à jour, bloqueraient du temps à tort. La réactiver
resynchronise aussitôt.

Ne pas ressaisir à la main une réunion qui vient d'Outlook : il n'y a pas de
dédoublonnage, elle apparaîtrait deux fois.

### Agenda Odoo

**Config > Agendas externes > Ajouter l'agenda Odoo** (bouton visible quand la
connexion de *Connexions > Tâches Odoo* est renseignée). Une seule source Odoo,
sans adresse à saisir : elle réutilise cette connexion. **Lecture seule** : rien
n'est jamais écrit dans Odoo. Même synchro que l'ICS (battement de cœur,
fenêtre J-7 → J+60 remplacée en une transaction, échec affiché sur la source
sans toucher aux données). Code : `services/odoo_calendar.py`.

| Odoo | Dans NOMAIA |
|---|---|
| Congé validé à la journée (`hr.leave`) | jour de repos, titre = type de congé |
| Congé validé à la demi-journée ou à l'heure | `absence` horaire |
| Congé en attente (`confirm`, `validate1`) | jour de repos « en attente » : affiché, **ne bloque pas le plan** |
| Congé refusé ou annulé | ignoré |
| Férié / fermeture (`resource.calendar.leaves` sans ressource, mon calendrier ou aucun, ma société) | jour férié, sauf date déjà couverte par un autre férié |
| Réunion où je suis invité (`calendar.event`) | réunion ; « journée entière » = horaires de travail réglés |
| Réunion déclinée | ignorée |

Les horaires Odoo (UTC) sont convertis dans le fuseau de l'utilisateur Odoo.
Une réunion déjà présente ailleurs (Outlook, saisie) à la même heure avec un
titre équivalent (sans casse ni accents, l'un contenu dans l'autre) n'est pas
importée deux fois. Le projet est deviné quand le titre contient le nom d'un
projet en cours (mot entier, le plus long gagne).

Quatre cases sous la source choisissent ce qui est importé (réglages
`odoo_cal_leaves`, `odoo_cal_pending`, `odoo_cal_holidays`,
`odoo_cal_meetings`, tous actifs par défaut). Si un module manque dans Odoo
(pas de RH, pas d'agenda), la synchro échoue avec le message d'Odoo : décocher
l'import correspondant.

## Proactivité

Les rendez-vous quotidiens constatent : retard, dérive, surcharge du jour.
`services/foresight.py` **anticipe**. Ses détecteurs ne font que détecter ;
le moment d'envoi et le silence entre deux alertes sont décidés par le
battement de cœur.

### Échéances en danger

Pour chaque échéance des `risk_horizon_days` prochains jours ouvrés (10 par
défaut), NOMAIA compare le **travail restant cumulé** au **temps libre réel**
d'ici là :

- travail restant = estimation avec marge, moins le temps déjà chronométré
  (15 min au minimum : une tâche pas finie n'est jamais à zéro) ;
- temps libre = créneaux du planificateur, donc après réunions (Outlook
  compris), congés, fériés et week-ends ; pour aujourd'hui, seulement ce qui
  reste après l'heure actuelle.

Le cumul est indispensable : deux tâches de trois heures dues jeudi tiennent
chacune seule dans quatre heures libres, pas ensemble.

```
Echeances en danger :
- jeu. 08/10 : Rapport ALPHA, Recette paie - 7 h de travail, 4 h 30 libres d'ici la
```

L'alerte ne vaut que ce que valent les estimations : le calibrage des durées
l'aide, une tâche estimée 30 min qui en prend 4 h la trompera.

### Où et quand

| Alerte | Où | Silence |
|---|---|---|
| Échéance en danger | point du matin, page Plan, chat (`get_risks`) | état du jour, répété tant qu'il dure |
| Échéance d'aujourd'hui ou demain qui bascule | message immédiat | une seule fois par tâche et par échéance |
| Préparation de réunion | rappel avant réunion | — |
| Validations oubliées (propositions, questions du triage) | point du matin | 2 jours |
| Tâches dormantes, projets à l'arrêt | « Ménage de la semaine », premier point du matin de la semaine | 14 et 7 jours |

L'alerte immédiate est recalculée toutes les 15 minutes, et part seulement un
jour ouvré, pendant les heures de travail, **après** le point du matin : une
réunion Outlook arrivée à 11 h peut rendre intenable une échéance de demain,
mais NOMAIA ne réveille personne la nuit et ne double pas le matin. Une
échéance déjà annoncée au point du matin ne repart pas en alerte immédiate.

La mémoire des alertes vit en base (`alert_log`) : redémarrer NOMAIA ne
renvoie rien.

### Préparation de réunion

Le rappel avant réunion ajoute ce que tu dois aux personnes présentes, ce
qu'elles te doivent, et les tâches du projet dues sous trois jours. Le flux
Outlook publié ne porte pas les participants : projet et interlocuteurs sont
retrouvés par le projet rattaché, ou par leur nom cité dans le titre
(« Point ALPHA », « Call Paul »), en mot entier, sans accents ni casse, trois
lettres minimum (« RH » ne se retrouverait pas partout).

### Ménage

- **Tâche dormante** : sans échéance, sans aucune session chronométrée, créée
  il y a plus de `dormant_after_days` jours (21). Sans échéance, elle n'est
  jamais en retard : rien d'autre ne la signale.
- **Projet à l'arrêt** : actif, avec des tâches ouvertes, sans création,
  clôture ni chronométrage depuis `project_idle_days` jours (14). Un projet
  sans tâche ouverte n'est pas signalé : il est probablement fini.

Tout se règle dans **Config > Proactivité** ; `foresight_enabled = 0` coupe
les alertes anticipées sans toucher aux points du matin et du soir.

## Interlocuteurs et relances

Un secrétaire ne suit pas que des tâches : il suit **qui attend quoi**. Il y a
deux directions, et elles ne se modélisent pas pareil.

| | Je dois quelque chose | On me doit quelque chose |
|---|---|---|
| Exemple | « le devis à Paul vendredi » | « Paul me doit les specs » |
| Est-ce mon travail ? | oui | **non** |
| Dans NOMAIA | une **tâche** avec un destinataire (`task.person_id`) | une **attente** (`waiting`) |

**Plusieurs projets par interlocuteur** (table `person_project`) : un chef de
projet ou un informaticien intervient souvent sur plusieurs projets.
`person.project_id` reste son projet principal, affiché en premier, et figure
toujours parmi ses projets. Cité sur un nouveau projet (triage, chat, session
Claude), il y est ajouté sans quitter les précédents. La fiche se règle par
cases « Projets suivis », la liste des contacts se filtre par projet, et la
fiche projet montre tous les interlocuteurs qui le suivent. Une fusion de
contacts ou de projets conserve les rattachements.

Le premier cas ne mérite pas d'entité nouvelle : une promesse à tenir **est**
une tâche. Une table parallèle doublonnerait les tâches et imposerait de les
tenir synchronisées. Une colonne suffit.

Le second était le trou : ce que tu attends des autres n'est pas ton travail,
donc n'existait nulle part. Des specs attendues depuis huit jours ne se
signalaient pas.

### Trois portes d'entrée

Une base de contacts qu'il faut remplir à la main reste vide, et la fonction
meurt. Aucune saisie n'est donc obligatoire :

1. **Le triage extrait.** Il identifiait déjà le projet ; il identifie
   maintenant la personne citée (`person_hint`) et ce qu'elle doit (`waiting`).
2. **Le chat.** « Paul attend le devis pour vendredi », « je relance qui ? »
3. **La page Contacts**, pour corriger ce que le triage a lu de travers.

Les deux champs ajoutés au triage sont **facultatifs de bout en bout** : une
sortie de modèle qui les ignore produit exactement le résultat d'avant. Une
extraction ratée est journalisée et n'empêche jamais la création des tâches.
Le réglage `extract_people` la coupe entièrement.

### Relances

Le point du matin nomme ce que tu attends depuis trop longtemps :

```
Tu attends toujours :
- Paul : Les specs de l'export (8 j) (en retard) - bloque : Corriger l'export
```

Une attente dont l'échéance est passée est signalée sans attendre le délai
d'ancienneté. Après signalement, elle se tait pendant `nudge_quiet_days` :
sans ce silence, la même ligne reviendrait chaque matin jusqu'à devenir un
bruit qu'on ne lit plus. La marque est posée au moment où le message part,
pas à sa lecture — sinon une attente se tairait sans que rien ne soit envoyé.

Le point du soir, lui, nomme le destinataire d'une tâche qui dérive :
« Le devis (10 j de retard) - Paul attend ».

### Saisie après coup

« Il devait me l'envoyer vendredi, on est mardi » est le cas le plus courant.
Enregistrer une attente dont l'échéance est déjà passée recule son départ à
cette échéance : son ancienneté ne repart pas de zéro. Un départ explicite
postérieur à l'échéance reste refusé, lui : la saisie se contredit.

### Modification, doublons et alias

La fiche d'un interlocuteur (page **Contacts**) se modifie directement :
nom, rôle, projet, email, téléphone, notes et **alias**, les autres noms sous
lesquels il est cité.

Deux captures qui citent « Paul M. » puis « Paul Martin » créent deux
fiches. **Fusionner un doublon** les regroupe : les tâches dues et les
attentes passent sur la fiche gardée, ses champs vides sont complétés, les
notes mises bout à bout, puis le doublon est supprimé. Le nom absorbé devient
un alias : le triage et le chat retrouvent la bonne fiche au lieu de recréer
le doublon. Renommer une fiche garde de même l'ancien nom en alias.

Recherche par nom : le nom exact d'abord (casse ignorée), un alias ensuite.
Dans le chat, l'outil `merge_people` (par identifiants) fait la même fusion ;
il est marqué destructif et passe donc par une confirmation.

### Suppression

Supprimer un interlocuteur emporte ses attentes mais **détache** ses tâches au
lieu de les perdre — même règle qu'à la suppression d'un projet. Une tâche
porte du travail, une fiche de contact non.

## Sessions Claude Code

Les sessions Claude Code de ce poste écrivent directement dans les tâches :
elles retrouvent ou ouvrent la tâche du travail en cours, notent l'avancement
aux étapes clés et déclarent le temps passé. Le suivi devient réel, et le
bilan s'appuie sur ce qui a été fait au lieu d'un chronomètre souvent oublié.

**Le lien passe par la machine, pas par le compte Anthropic.** NOMAIA écoute
sur 127.0.0.1 : seules les sessions Claude Code (ou Claude Desktop) lancées
sur ce PC, NOMAIA démarré, peuvent le joindre. Une session web ou cloud
(claude.ai/code) ne le peut pas.

```
Claude Code ──stdio──> integrations/claude_mcp.py ──HTTP + clé──> /api/agent/
                       (Python du poste, sans dépendance)
```

### Mise en place, une fois

1. **Config > Sessions Claude > Générer une clé.** La commande affichée porte
   la clé ; elle n'est montrée qu'à ce moment.
2. La lancer dans un terminal :
   `claude mcp add --scope user nomaia -e NOMAIA_AGENT_KEY=<clé> -- python "<programme>\integrations\claude_mcp.py"`
3. Nouvelle session Claude Code : `/mcp` doit lister `nomaia`.

Le script MCP s'exécute avec le Python du poste : Python doit être installé,
même avec la version Windows autonome.

Régénérer la clé coupe aussitôt les sessions enregistrées avec l'ancienne :
`claude mcp remove --scope user nomaia`, puis la nouvelle commande.

### Espaces pro et perso

Chaque projet et chaque tâche appartient à un espace, `pro` ou `perso`
(champ « Espace » de la fiche projet). Une tâche rattachée à un projet suit
l'espace du projet, y compris quand on la déplace ou que le projet change
d'espace ; une tâche sans projet garde le sien. Tout l'existant est en `pro`.

**Une clé par espace.** Config > Sessions Claude propose une clé pro et une
clé perso ; chacune s'enregistre dans le profil Claude Code concerné
(`CLAUDE_CONFIG_DIR`). La clé présentée fixe l'espace de toute la requête :

| Avec la clé perso | Effet |
|---|---|
| Lister, chercher | Seuls les projets, tâches et entrées de journal perso |
| Lire ou modifier une tâche pro | « introuvable » : la session n'apprend pas qu'elle existe |
| Créer une tâche ou un projet | Toujours dans l'espace perso |
| Rapport d'avancement | Refusé (il ne concerne que l'espace pro) |

**Dans l'interface**, tout est séparé, sur le modèle du multi-société d'Odoo.
Le sélecteur Tout / Pro / Perso de la barre du haut choisit le périmètre affiché
(enregistré en base : poste et téléphone montrent le même). « Tout » montre les
deux espaces, avec un badge Pro / Perso sur chaque ligne.

| Donnée | Espace |
|---|---|
| Tâches, projets, journal | Une tâche suit son projet ; un projet qui change d'espace emmène tâches, journal, rendez-vous, comptes rendus et notes rattachés |
| Boîte de réception | Espace sélectionné au moment de la capture (Telegram compris) ; mails Outlook en Pro ; le tri déplace la capture si ses tâches tombent dans un projet de l'autre espace |
| Chat | Une conversation par espace ; le chat ne voit que les données du périmètre |
| Agenda | Un rendez-vous suit son projet ; un calendrier importé a son espace (Config, agenda Odoo toujours Pro) |
| Contacts, notes techniques | Pro, Perso ou **Commun** (visibles des deux côtés) |
| Comptes rendus, attentes | Suivent leur projet, leur tâche ou leur contact |
| Bilan, statistiques, recherche | Calculés sur le périmètre ; un bilan enregistré n'est relu que dans son périmètre |
| Réglages | Communs, sauf l'espace des calendriers et les horaires perso |

**Créer en mode Tout** : la petite liste « + Pro / + Perso » à côté du sélecteur
choisit l'espace des nouveaux éléments (comme la société courante d'Odoo).

**Plan du jour** : calculé sur les deux espaces, ton temps étant le même. En Pro ou
en Perso, les créneaux et rendez-vous de l'autre espace restent à leur place,
grisés « Occupé ». Config > Journée peut réserver le perso au soir et aux jours
non travaillés (désactivé par défaut).

La clé de session perso ne voit que le perso, contacts et notes communs compris.
L'ancienne clé unique devient la clé pro : un lien déjà enregistré continue de
fonctionner après la mise à jour.

### Quand une session écrit

Le serveur MCP remet sa consigne à Claude Code à la connexion (champ
`instructions`), sans toucher au CLAUDE.md : **une tâche pour un vrai travail**
(code écrit ou corrigé, analyse livrée), jamais pour une simple question.

| Outil | Rôle |
|---|---|
| `nomaia_list_tasks` | Tâches ouvertes, filtrables par projet et mots du titre |
| `nomaia_get_task` | Détail, étapes, journal récent |
| `nomaia_create_task` | Création ; si une tâche ouverte au titre proche existe, rien n'est créé et elle est proposée (`force` pour passer outre) ; `person` : à qui je la dois |
| `nomaia_update_task` | Avancement (`progress_pct`), note, clôture (`status=done` met l'avancement à 100 %), cadrage, `person` |
| `nomaia_log_work` | Temps passé (1 à 600 min) et résumé |
| `nomaia_list_projects` | Projets actifs ou en pause |
| `nomaia_create_project` | Création d'un projet (nom, échéance facultative) ; si un projet du même nom existe (casse ignorée), rien n'est créé et il est renvoyé |
| `nomaia_search` | Recherche plein texte (tâches, projets, journal, contacts, événements, comptes rendus, notes), accents ignorés ; chaque résultat porte sa nature (`kind`) et son lien (`url`) |
| `nomaia_progress_report` | Rapport d'avancement d'un chef de projet (sans `manager` : liste des chefs) |
| `nomaia_set_report_url` | Lien du tableau de bord publié pour un chef de projet |
| `nomaia_list_people` | Contacts : rôle, projet, ce que je leur dois (tâches) et ce qu'ils me doivent (attentes) |
| `nomaia_save_person` | Crée un contact ou complète sa fiche (nom, alias ou nom partiel unique) ; les notes s'ajoutent |
| `nomaia_list_waiting` | Ce que j'attends des autres, la plus ancienne d'abord |
| `nomaia_add_waiting` | Ce que quelqu'un me doit, suivi par les relances ; contact créé s'il est inconnu, pas de doublon |
| `nomaia_resolve_waiting` | Clôt une attente : reçue ou abandonnée |
| `nomaia_get_note` | Note de la base de connaissances, contenu Markdown compris |
| `nomaia_create_note` | Création d'une note (titre, contenu, catégorie, projet, version d'Odoo, module, étiquettes) |
| `nomaia_update_note` | Modification des champs fournis ; `append=true` ajoute le contenu à la fin |
| `nomaia_get_minutes` | Compte rendu avec ses éléments (décisions, actions, demandes) |
| `nomaia_create_minutes` | Création d'un compte rendu et de ses éléments ; `status=final` le valide et liste ce qui reste à convertir |
| `nomaia_update_minutes` | Sections remplacées, éléments ajoutés (`add_items`), validation ou réouverture |

Notes et comptes rendus ne s'écrivent que sur demande de l'utilisateur (consigne du
serveur MCP). Le détecteur de secrets s'y applique comme dans l'interface. Pas
de suppression par le MCP : elle reste dans l'interface. Transformer une action
en tâche ou une décision en entrée du registre se fait aussi dans l'interface.

**Tolérance des sessions.** Un projet se désigne par son nom exact ou
approché : casse, accents et ponctuation ignorés, nom partiel accepté s'il ne
désigne qu'un projet (« Nordik » → NORDIK EUROPA ; « Delta », ambigu, est
refusé avec la liste des candidats). Les statuts courants sont ramenés à ceux
de NOMAIA (`in_progress`, `open` → `todo` ; `completed`, `closed`, « terminée »
→ `done`).

**Temps déclaré.** `nomaia_log_work` attend le temps réellement écoulé à
l'horloge, pas une estimation de l'effort. Une durée supérieure au temps écoulé
depuis la création de la tâche (ou depuis la dernière déclaration sur elle),
marge comprise (×1,2 + 15 min), est enregistrée mais renvoyée avec un
avertissement (`warning`). Rien n'est bloqué.

**Autorisation dans Claude Code.** En mode d'autorisation « auto », le
classificateur de Claude Code refuse les écritures MCP qu'il juge externes
quand la session ne porte pas sur NOMAIA. La règle `"mcp__nomaia"` dans
`permissions.allow` des réglages utilisateur les laisse passer.

### Ce qui est enregistré

- `task.source = 'claude'` sur une tâche ouverte par une session, et
  `task.progress_pct` (0 à 100) pour toute tâche.
- **Journal de tâche** (`models/journal.py`, table `note`) : notes,
  avancements et travail déclaré, avec leur source. Visible et complétable
  depuis la fiche de détail du plan.
- Le temps déclaré devient une **session terminée** (`session.source =
  'claude'`) : il compte dans le temps suivi, le bilan et le calibrage, comme
  un chronomètre.

### Sécurité

La clé des sessions est distincte de la clé de l'interface : elle n'ouvre que
`/api/agent/`, se présente en `Authorization: Bearer`, et le cookie de
l'interface n'ouvre pas ces routes. Elle est chiffrée au repos et effacée des
sauvegardes comme les autres secrets. Un en-tête `Authorization` posé par une
page web déclenche une vérification CORS que NOMAIA n'accorde jamais.

Limite assumée : la commande `claude mcp add -e NOMAIA_AGENT_KEY=…` laisse la
clé en clair dans la configuration de Claude Code (dossier du compte Windows)
et dans l'historique du terminal. Elle n'ouvre que `/api/agent/` (tâches,
avancement, temps) et se révoque d'un clic en en générant une nouvelle.

## Projets, temps et bilan

**Projets** (`/projets`) : un tableau de bord général, puis une fiche par
projet.

- **Tableau de bord** : indicateurs de la semaine (projets actifs, à
  surveiller, tâches ouvertes et en retard, terminées, temps suivi), puis une
  carte par projet — avancement, reste à faire, échéance, activité de la
  semaine, signaux, dernière activité. Filtres : en cours, à risque, terminés,
  tous.
- **Fiche projet** (`/projets/<id>`) : indicateurs, tâches en retard / à faire
  / terminées (case à cocher, fiche de détail partagée avec le plan), ajout
  rapide d'une tâche, **commentaires et journal** (commentaires du projet,
  notes des tâches, travail des sessions Claude), activité des six dernières
  semaines, interlocuteurs rattachés et ce qu'on attend d'eux, rendez-vous des
  30 prochains jours. Modifier, rattacher les tâches orphelines qui citent le
  projet, supprimer (tâches détachées, jamais perdues).

**Avancement** : pondéré par les estimations — une tâche de 4 h terminée pèse
plus qu'une de 15 min — et une tâche ouverte compte pour la part déclarée de
son avancement (`progress_pct`). Une tâche découpée en étapes est mesurée par
ses étapes, comme dans le plan. Le reste à faire suit la même règle.

**Signaux** (projets non terminés) : échéance dépassée ; échéance à moins de
7 jours avec moins de 70 % fait ; tâches en retard ; projet actif sans aucune
trace (tâche, temps, note) depuis `project_idle_days` jours.

**Chronomètre** : bouton ▶ sur chaque créneau du plan du jour. Une seule
session ouverte à la fois — démarrer un chronomètre ferme le précédent. Une
session compte au minimum une minute.

**Bilan** (`/bilan`) : chiffres bruts d'une période, puis analyse par l'IA sur
demande. Le bilan est enregistré en base (table `review`) et consultable depuis
Telegram avec `/bilan` (semaine en cours).

- **Période** : semaine en cours, semaine dernière, il y a deux semaines, mois
  en cours, mois dernier, ou dates libres. La page affiche le bilan généré pour
  **cette** période, pas le dernier généré.
- **Historique** : la liste des bilans précédents se relit d'un clic (période
  et chiffres rechargés avec).
- **Ordre de la page** : chiffres ; temps par projet et tâches terminées par
  jour côte à côte ; écart d'estimation et calibrage ; projets ; analyse ;
  rapport chef de projet ; bilans précédents ; enfin le **journal des sessions
  Claude**, replié, groupé par jour, une ligne par entrée (exclu de
  l'impression).
- **Temps par projet** : une sous-tâche sans projet compte pour celui de sa
  tâche parente. La barre « Sans projet » se déplie sur les tâches concernées,
  rattachables à un projet en un clic.
- **Projets** : pour chaque projet actif ou ayant bougé, l'état actuel
  (avancement, étape en cours, prochain jalon, anomalies ouvertes, livrables
  attendus, alertes) et les mouvements de la période (tâches et étapes
  terminées, anomalies remontées ou corrigées, livrables reçus, entrées du
  registre). Aucun historique d'avancement n'est conservé : l'état est celui
  d'aujourd'hui, les mouvements ceux de la période.

Le prompt du bilan impose à l'IA d'appuyer chaque constat sur un chiffre réel
et de signaler un chronomètre inutilisé au lieu d'en déduire une performance.

### Rapport d'avancement pour un chef de projet

Chaque projet peut désigner son **chef de projet** parmi les contacts (fiche
projet > Modifier). Le bloc « Rapport d'avancement » de `/bilan` réunit tous
les projets en cours de ce chef sur la période choisie : indicateurs de tête et
message WhatsApp prêt à relire (copie, ou ouverture de WhatsApp sur son numéro
s'il est au format international `+225…`).

Le rapport est **calculé**, pas rédigé par l'IA (`services/progress_report.py`) :
avancement et météo, étape en cours et jalons, frise des étapes, fait sur la
période, à venir sous 14 jours, anomalies ouvertes par gravité, livrables
attendus du client, registre de la période, points d'attention. Il ne montre
que ce qui regarde le projet : ni description de tâche, ni temps chronométré,
ni journal de travail, ni tâche sans projet.

**Tableau de bord partagé** : une session Claude Code lit le rapport
(`nomaia_progress_report`) et le publie en artifact claude.ai (type Dashboard),
un par chef de projet. Le lien reste le même à chaque mise à jour : il
s'envoie une fois sur WhatsApp, puis on demande « mets à jour le tableau de
bord de X ». `nomaia_set_report_url` retient ce lien, ajouté ensuite au message
WhatsApp. L'artifact est privé par défaut : son partage se règle sur claude.ai.

**Rattrapage des tâches orphelines.** Un projet est presque toujours créé après
les tâches qu'il concerne. À la création, une case **Rattacher les tâches
existantes** reprend les tâches ouvertes sans projet dont le titre ou la
description cite son nom. La page affiche ensuite un bouton **Rattacher n** dès
qu'un projet a de quoi récupérer. Les tâches déjà rattachées et les tâches
terminées ne sont jamais touchées.

**Affectation manuelle.** La saisie rapide et la fenêtre de modification de
`/taches` portent un sélecteur de projet, et un filtre au-dessus de la liste
restreint l'affichage à un projet — ou aux tâches sans projet.

## Interface, recherche et mémoire

**Interface.** Barre latérale groupée (Aujourd'hui, Pilotage, Mémoire, Assistant), en-tête avec la palette de
commandes, le chronomètre en cours et le thème. Thème sombre ou clair, fond animé facultatif et animations
réduites : réglages propres à chaque appareil (Config > Affichage), gardés dans le navigateur. Feuilles :
`static/css/tokens.css` (jetons des deux thèmes), `shell.css` (coquille), `components.css` (composants
partagés), `motion.css` (animations courtes, sur transform et opacity seulement), `pages/*.css` (une par page),
`print.css` (impression).

**Palette Ctrl+K** (ou `/` hors d'un champ) : actions (thème, chronomètre, capture, clôture de journée), pages,
et recherche dans les données dès deux caractères. **Alt+N** capture une idée depuis n'importe quelle page.

**Recherche plein texte** : index SQLite FTS5 (`models/search.py`) sur les tâches, projets, journal, contacts,
événements, comptes rendus et notes. Accents et casse ignorés, mots en préfixe. Tenu à jour par des triggers ;
reconstruit au démarrage quand sa version change. Outil MCP `nomaia_search`.

**« Où en étais-je ? »** (fiche projet) : dernière action, prochaine étape (une tâche entamée passe devant), étape
en cours, ce qui bloque ou attend le client, anomalies, livrables, décisions, sessions Claude, puis la chronologie.
Le résumé rédigé par l'IA est facultatif ; chaque affirmation cite sa source ([T12], [J8]), les références
inventées sont retirées.

**Comptes rendus** (`/comptes-rendus`) : réunion, intervention ou formation, rattaché à une séance de l'agenda
(un par séance d'un événement répété) mais conservé si l'événement disparaît. Une action devient une tâche, une
décision s'inscrit au registre du projet, une demande du client devient une tâche côté client ; chaque
conversion n'a lieu qu'une fois.

**Notes** (`/notes`) : base de connaissances en Markdown (rendu maison, sans dépendance), classée par projet,
version d'Odoo, module, catégorie et étiquettes ; modèle « Déploiement ». Une anomalie terminée avec une solution
produit sa note « Solution », tenue à jour.

**Pièces jointes** sur les tâches (livrables reçus), projets, comptes rendus et notes : 10 Mo par fichier, rangées
dans `public/uploads/attachments` sous un nom aléatoire. Images et PDF s'ouvrent ; tout autre type se télécharge.

**Secrets en clair refusés** (`services/secrets_guard.py`) dans le journal, les comptes rendus, les notes, la
cause et la solution d'une anomalie et la note du soir : mot de passe, clé d'API, clé privée. Noter où le secret
est rangé, pas le secret.

**Clôture de journée** (plan du jour) : récapitulatif calculé (terminé, temps suivi, journal) et note « pour
demain », relue le lendemain matin en tête du plan et reprise par le bilan. Une échéance repoussée deux fois ou
plus affiche « reportée N fois ». Fiche projet : client et version d'Odoo.

**Impression / PDF** : bouton sur le bilan, la fiche projet et les comptes rendus ; la feuille d'impression
retire la coquille et imprime sur fond blanc.

## Rendez-vous quotidiens

NOMAIA parle en premier, deux fois par jour, sur Telegram et en bulle Windows.

| Moment | Contenu |
|---|---|
| Matin (08:30 par défaut) | plan de la journée, charge en pourcentage, alerte de surcharge, ce qui ne rentre pas |
| Soir (18:00 par défaut) | terminées et restantes, nombre en retard, tâches qui dérivent |

Une tâche « dérive » quand elle traîne plus de trois jours après son échéance :
repoussée sans fin, elle devient un meuble en tête de liste et ne se signale
nulle part. Le seuil est réglable.

Le déclenchement vit en base, pas en mémoire : redémarrer NOMAIA trois fois
dans la matinée ne renvoie pas trois fois le même message. Un rendez-vous
manqué de plus de trois heures est passé sous silence plutôt que rattrapé —
allumer le PC à 17 h ne doit pas déclencher le point du matin.

`/plan` sur Telegram redemande le point du matin à n'importe quelle heure.

## API

| Méthode | Route | Rôle |
|---|---|---|
| `GET` | `/api/tasks?status=&project_id=` | Liste + compteurs (`project_id=none` : tâches sans projet) |
| `GET` | `/api/tasks/<id>` | Lecture d'une tâche |
| `POST` | `/api/tasks` | Création |
| `PATCH` | `/api/tasks/<id>` | Mise à jour partielle |
| `POST` | `/api/tasks/<id>/toggle` | Bascule à faire / terminée |
| `DELETE` | `/api/tasks/<id>` | Suppression |
| `POST` | `/api/capture` | Dépôt d'une entrée brute (JSON ou multipart `image`) |
| `GET` | `/api/capture` | Liste des entrées et de leur dernière proposition |
| `GET` | `/api/capture/<id>` | Fil complet : dialogue, proposition, tâches |
| `POST` | `/api/capture/<id>/answer` | Réponse aux questions, relance le triage |
| `POST` | `/api/proposal/<id>/apply` | Validation manuelle d'une proposition |
| `POST` | `/api/proposal/<id>/reject` | Rejet d'une proposition |
| `GET` | `/api/people` | Interlocuteurs et leurs deux compteurs |
| `GET` | `/api/people/<id>` | Fiche : attentes en cours et tâches dues |
| `POST` | `/api/people` | Création d'un interlocuteur |
| `PATCH` | `/api/people/<id>` | Mise à jour |
| `DELETE` | `/api/people/<id>` | Suppression (tâches détachées) |
| `GET` | `/api/waiting?status=` | Ce qu'on doit à l'utilisateur, plus ancien d'abord |
| `POST` | `/api/waiting` | Enregistrement d'une attente |
| `PATCH` | `/api/waiting/<id>` | Sujet, échéance, statut (`received`, `dropped`) |
| `DELETE` | `/api/waiting/<id>` | Suppression |
| `GET` | `/api/events?start=&end=` | Événements d'une période, répétitions développées |
| `POST` | `/api/events` | Création d'un événement |
| `PATCH` | `/api/events/<id>` | Mise à jour (s'applique à toute la série) |
| `DELETE` | `/api/events/<id>` | Suppression de la série |
| `POST` | `/api/events/<id>/cancel` | Annulation d'une seule occurrence |
| `GET` | `/api/events/free?date=&min_minutes=` | Créneaux libres d'une journée |
| `GET` | `/api/chat?conversation_id=` | Fil d'une conversation (défaut : la courante), liste des conversations |
| `POST` | `/api/chat` | Envoi d'un message (`message`, `conversation_id`), réponse de NOMAIA (appel long) |
| `DELETE` | `/api/chat?conversation_id=` | Vide le fil d'une conversation |
| `GET` | `/api/conversations?archived=1` | Conversations, la plus récemment active d'abord |
| `POST` | `/api/conversations` | Nouvelle conversation (`title` facultatif) |
| `PATCH` | `/api/conversations/<id>` | Renommer (`title`), archiver (`archived`) |
| `DELETE` | `/api/conversations/<id>` | Suppression avec tout son fil |
| `GET` | `/api/status` | État du moteur et du worker |
| `GET` | `/api/plan?date=` | Plan d'une journée |
| `GET` | `/api/plan/why/<id>` | Détail du score d'une tâche |
| `PATCH` | `/api/plan/settings` | Horaires de travail et marge |
| `GET` | `/api/projects` | Projets avec compteurs et temps suivi |
| `POST` | `/api/projects` | Création d'un projet (`attach_matching` : rattrapage des orphelines) |
| `POST` | `/api/projects/<id>/attach` | Rattache les tâches orphelines citant le projet |
| `PATCH` | `/api/projects/<id>` | Nom, couleur, échéance, statut, météo, chef de projet (`manager_id`) |
| `DELETE` | `/api/projects/<id>` | Suppression (tâches détachées) |
| `GET` | `/api/tasks/timer` | Session de travail en cours |
| `POST` | `/api/tasks/<id>/timer/start` | Démarrer le chronomètre |
| `POST` | `/api/tasks/timer/stop` | Arrêter le chronomètre |
| `GET` | `/api/stats?weeks_back=\|months_back=\|start=&end=` | Indicateurs bruts d'une période, travail des sessions Claude et projets compris |
| `GET` | `/api/review?<période>` | Dernier bilan généré pour cette période |
| `POST` | `/api/review?<période>` | Génération d'un bilan par l'IA |
| `GET` | `/api/reviews` | Historique des bilans |
| `GET` | `/api/reviews/<id>` | Un bilan de l'historique |
| `GET` | `/api/progress-report/managers` | Chefs de projet (contacts suivant un projet en cours) |
| `GET` | `/api/progress-report?manager_id=&<période>` | Rapport d'avancement d'un chef de projet et message WhatsApp |
| `POST` | `/api/people/<id>/merge` | Absorbe un doublon (`merge_id`) dans cet interlocuteur |
| `POST` | `/api/plan/order` | Ordre manuel des tâches d'un jour (`date`, `task_ids`) |
| `DELETE` | `/api/plan/order?date=` | Retour au classement automatique pour ce jour |
| `POST` | `/api/plan/pin/<id>` | Épingle une tâche (`position` : `now` ou `end`) |
| `DELETE` | `/api/plan/pin/<id>` | Désépingle une tâche |
| `POST` | `/api/tasks/<id>/done-on` | Clôt une tâche à une date passée (`day`) |
| `POST` | `/api/tasks/<id>/journal` | Note au journal d'une tâche (`content`, `progress_pct`) |
| `GET` | `/api/projects/board` | Tableau de bord : indicateurs et cartes de projets |
| `GET` | `/api/projects/<id>/detail` | Fiche d'un projet : tâches, journal, contacts, agenda, activité |
| `POST` | `/api/projects/<id>/journal` | Commentaire sur un projet |
| `DELETE` | `/api/projects/<id>/journal/<note>` | Suppression d'un commentaire |
| `GET` | `/api/mobile` | Adresse Tailscale réglée pour le téléphone |
| `PUT` | `/api/mobile` | Enregistre l'adresse Tailscale (`origin`, HTTPS) |
| `POST` | `/api/mobile/code` | Code d'appairage à usage unique (10 min) |
| `POST` | `/appairage` | Saisie du code sur le téléphone : pose le cookie d'accès |
| `GET` | `/api/mail/status` | Lecture des mails Outlook : activation, dernier passage, dernière erreur |
| `POST` | `/api/mail/read` | Lecture immédiate des mails Outlook : nombre d'entrées déposées ou erreur |
| `GET` | `/api/agent-setup` | État du lien avec les sessions Claude (sans la clé) |
| `POST` | `/api/agent-setup/key` | Nouvelle clé des sessions et commande d'enregistrement |
| `GET` | `/api/agent/tasks?status=&project=&q=` | Sessions Claude : tâches (clé des sessions) |
| `GET` | `/api/agent/tasks/<id>` | Sessions Claude : détail et journal |
| `POST` | `/api/agent/tasks` | Sessions Claude : création, ou tâches proches |
| `POST` | `/api/agent/tasks/<id>/progress` | Sessions Claude : avancement, note, clôture |
| `POST` | `/api/agent/tasks/<id>/work` | Sessions Claude : temps passé et résumé |
| `GET` | `/api/agent/projects` | Sessions Claude : projets |
| `GET` | `/api/agent/progress-report?manager=&start=&end=` | Sessions Claude : rapport d'un chef de projet (7 derniers jours par défaut), ou liste des chefs |
| `POST` | `/api/agent/progress-report/url` | Sessions Claude : lien du tableau de bord publié |

## Tests

Écrits avec `unittest` (bibliothèque standard) ; les dépendances de
`requirements.txt` doivent être installées. Chaque test
repart d'une base temporaire : l'ordre d'exécution n'a aucune importance et la
vraie base n'est jamais touchée.

```powershell
python -m unittest discover -s tests -t .
```

Couverture : priorisation (`scheduler`), plan du jour (`planner`), validation
de la sortie du modèle et rattachement projet (`triage`), CRUD et rattrapage
des projets, file de triage et budget de tentatives, sauvegarde sans secret,
calibrage des durées, rendez-vous quotidiens, conversation (protocole,
boucle d'outils, plafond de tours, panne de moteur, historique des
fournisseurs), agenda (fusion des chevauchements, soustraction des fenêtres,
répétitions, rappels), interlocuteurs (les deux directions, ancienneté,
relances espacées, extraction optionnelle du triage), plan de modifications
(seuil de confirmation, péremption, échecs partiels) et fusion de doublons
(temps chronométré conservé), vocaux Telegram (aiguillage, refus, audio
effacé), jours de repos (calendrier ouvré, fériés et Pâques, plan et
messages un jour chômé, outils du chat), synchro Outlook (séries, exceptions,
fuseaux, absences, suppression amont, panne réseau, lecture seule, secret hors
sauvegarde), proactivité (déficit cumulé, réunion qui fait basculer, temps
chronométré déduit, rapprochement titre ↔ projet, silence des alertes, alerte
immédiate unique et muette la nuit), et les routes d'API correspondantes garde-fou
anti-CSRF compris.

## Avancement

- [x] **Lot 1** — base SQLite, CRUD tâches, tableau de bord
- [x] **Lot 2** — captures brutes, triage IA, dialogue, seuil de confiance
- [x] **Lot 3** — bot Telegram (dépôt depuis le téléphone)
- [x] **Lot 4** — icône barre des tâches, lancement au démarrage
- [x] **Lot 5** — plan du jour et priorisation automatique
- [x] **Lot 6** — projets, suivi du temps, statistiques, bilan hebdomadaire
- [x] **Durcissement** — clé d'accès, anti-CSRF, portée de lecture, secrets chiffrés
- [x] **Empaquetage** — exécutable autonome, installeur, désinstalleur
- [x] **Lot B** — WAL et attente sur verrou, sauvegarde par l'API SQLite,
      budget de tentatives séparé du dialogue, erreur Claude Code complète au journal
- [x] **Lot A** — boucle projet fermée : sélecteur, filtre, rattrapage des
      orphelines, création de projet par le triage
- [x] **Lot C** — rendez-vous quotidiens, alerte de surcharge et de dérive
- [x] **Lot D** — calibrage des estimations sur le temps réellement chronométré
- [x] **Lot E** — tests unitaires
- [x] **Accès hors ligne** — instantané épinglé sur Telegram, lisible PC éteint
- [x] **Lot F** — conversation : chat web et Telegram, douze outils de
      lecture et d'écriture, fournisseur dédié
- [x] **Lot G** — agenda : réunions et créneaux bloqués, capacité du plan
      enfin exacte, répétitions, rappel avant réunion
- [x] **Lot H** — interlocuteurs : ce qu'on me doit suivi et relancé, ce que
      je dois rattaché à son destinataire, extraction par le triage
- [x] **Lot I** — modifications en chaîne : plan confirmé côté serveur,
      détection et fusion des doublons, édition des contacts et attentes
- [x] **Lot J** — vocaux Telegram : transcription locale faster-whisper,
      modèle chargé à la demande et libéré après inactivité
- [x] **Lot K** — jours de repos : jours ouvrés réglables, congés sur
      plusieurs jours, fériés par pays, échéances en jours ouvrés, événements
      personnels
- [x] **Lot L** — synchro Outlook : flux ICS publié, lecture seule, URL
      chiffrée, fenêtre glissante remplacée en une transaction
- [x] **Lot M** — proactivité : échéances en danger (temps libre réel),
      alerte immédiate unique, préparation de réunion, ménage hebdomadaire,
      validations oubliées
