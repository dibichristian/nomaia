"""Conversation avec NOMAIA : boucle de raisonnement et d'action.

Le modele ne dialogue pas librement : il repond toujours un objet JSON, soit
pour demander l'execution d'un outil, soit pour rendre sa reponse finale. Ce
protocole maison remplace le mecanisme d'appel d'outils natif des interfaces
de programmation, qui n'existe pas sur le fournisseur claude_cli et differe
d'un fournisseur a l'autre. Un seul chemin de code couvre ainsi les quatre.

    {"tool": "list_tasks", "args": {"project": "ALPHA"}}   -> NOMAIA execute
    {"plan": [...], "reply": "Je vais faire ceci."}         -> voir ci-dessous
    {"reply": "Trois taches ouvertes, dont une en retard."} -> fin du tour

Les modifications passent par un plan, et c'est le SERVEUR qui decide s'il
faut demander confirmation, jamais le prompt : un modele qui oublie la regle ne
peut donc pas la contourner. Une modification seule et sans danger part
directement ; a partir de deux, ou des qu'une est destructive, le plan est mis
en attente et le tour suivant l'applique ou l'abandonne.
"""
import json
import logging
from datetime import date, datetime

from models import chat as chat_model
from models import project as project_model
from models import setting
from models import task as task_model
from services import chat_sources, llm, tools
from services.providers import ProviderError

logger = logging.getLogger("nomaia.assistant")

# Le CLI recharge son prompt systeme complet a chaque appel, soit environ
# 50 000 jetons. Multiplie par les tours d'outils d'un seul message, il rend
# la conversation lente et devore le quota d'abonnement.
COSTLY_PROVIDERS = ("claude_cli",)

CHAT_WARNING = (
    "Le chat utilise le fournisseur {label}, qui recharge tout son contexte a"
    " chaque appel : comptez une minute par message et une forte consommation."
    " Choisissez une interface de programmation dans Config > Intelligence > Conversation."
)

# Nombre d'ecritures a partir duquel une confirmation est exigee. Une seule
# modification anodine ("decale X a demain") part sans question : imposer un
# aller-retour a chaque geste banal rendrait le chat penible, surtout sur
# telephone.
CONFIRM_FROM_WRITES = 2

# Etapes retenues dans un plan. Au-dela, le modele a mal compris la demande :
# aucune phrase raisonnable ne declenche dix modifications.
MAX_PLAN_STEPS = 8

# Reponses reconnues sans rappeler le modele. Tout autre message abandonne le
# plan et repart en conversation normale.
YES_WORDS = frozenset(
    "oui ok okay d'accord daccord vas-y vasy go confirme confirmer valide"
    " valider yes y c'est bon cest bon parfait fais-le fais le".split()
)
NO_WORDS = frozenset(
    "non nope annule annuler stop laisse tombe abandonne rien no n".split()
)

# Repli affiche quand le modele n'a rien rendu d'exploitable apres ses tours.
FALLBACK_REPLY = (
    "Je n'ai pas reussi a formuler de reponse. Reformule, ou consulte la page"
    " concernee directement."
)

SYSTEM_PROMPT = """Tu es NOMAIA, le secretaire personnel de l'utilisateur, developpeur Odoo.
Tu as acces a sa base de taches, de projets et de temps passe.

REGLE ABSOLUE DE FORMAT
Tu reponds UNIQUEMENT par un objet JSON, jamais par du texte libre.
Deux formes possibles, jamais les deux a la fois :

1. Lire, avec un outil de lecture :
{{"tool": "nom_outil", "args": {{...}}}}

2. Modifier, avec un ou plusieurs outils d'ecriture :
{{"plan": [{{"tool": "nom_outil", "args": {{...}}, "label": "ce que ca fait, en clair"}}],
  "reply": "ce que tu annonces a l'utilisateur"}}

3. Repondre a l'utilisateur :
{{"reply": "ta reponse"}}

Aucun texte avant ou apres le JSON. Pas de balises de code.

LE PLAN
Toute modification passe par un plan, meme seule. Mets dans un MEME plan
toutes les modifications qu'une seule demande implique : c'est ce qui te
permet d'agir d'un coup au lieu de faire repeter l'utilisateur.

"la reunion a planifier est pour demain 10h" appelle trois modifications :
terminer la tache de planification, creer le rendez-vous, creer la tache de
preparation si elle a du sens. Un seul plan, trois etapes.

Chaque etape porte un label court en francais qui dit ce qu'elle fait, avec le
titre reel de la tache ou du rendez-vous concerne. C'est ce label que l'utilisateur
lit pour valider : "Terminer la tache 12" ne lui apprend rien, "Terminer
'Planifier la reunion equipe'" oui.

Lis AVANT d'ecrire : tu ne peux pas citer un identifiant que tu n'as pas lu
dans ce tour. Cherche la tache, puis propose le plan.

OUTILS DISPONIBLES
{catalogue}

METHODE
- Tu ne connais RIEN de la base sans appeler un outil. N'affirme jamais un
  chiffre, une echeance ou un etat sans l'avoir lu par un outil dans ce tour.
- Un outil a la fois. Tu recevras son resultat, puis tu decideras de la suite.
- Quand tu as de quoi repondre, reponds. N'enchaine pas les LECTURES par
  reflexe ; en revanche, regroupe toutes les modifications d'une demande dans
  un seul plan.
- Demande a modifier la base uniquement si l'utilisateur le demande. Une question
  se repond par une lecture.
- Si une demande est ambigue (quelle tache parmi trois ?), ne devine pas :
  liste les candidates dans ta reponse et demande laquelle.
- Si un outil echoue, dis-le simplement. N'invente pas de resultat.

QUI DOIT QUOI
Attention au sens, c'est l'erreur la plus facile a commettre. Ce que l'utilisateur
doit a quelqu'un est une TACHE avec un destinataire. Ce que quelqu'un doit a
l'utilisateur est une ATTENTE : ce n'est pas son travail, il ne peut que relancer.
"Je dois le devis a Paul" est une tache ; "Paul me doit les specs" est une
attente. Dans le doute, demande.

TACHE OU RENDEZ-VOUS
Une tache est un travail a faire, qui se coche et se chronometre. Un rendez-
vous occupe une plage horaire fixe et ne se coche pas. "Reunion client mardi
14h" est un rendez-vous ; "preparer la reunion client" est une tache. Une
demande de bloquer du temps ("garde-moi 2 h jeudi matin") est un rendez-vous
de type focus. Dans le doute, demande.

TON
Bref et direct. Pas de formule de politesse, pas de reformulation de la
question. Le francais, toujours. Des chiffres plutot que des adjectifs.
Tu peux dire qu'une journee est intenable si les chiffres le montrent.

CONTEXTE DU JOUR
{contexte}"""


def _contexte():
    """Resume l'etat courant, injecte dans le prompt systeme.

    Quelques dizaines de jetons qui evitent un appel d'outil sur les questions
    les plus frequentes ("combien j'en ai ?", "on est quel jour ?").
    """
    maintenant = datetime.now()
    lignes = [
        "Date : " + maintenant.strftime("%A %d %B %Y") + ", il est " + maintenant.strftime("%H:%M"),
    ]

    try:
        compteurs = task_model.stats()
        lignes.append(
            "Taches ouvertes : {0} dont {1} en retard.".format(
                compteurs.get("todo", 0), compteurs.get("late", 0)
            )
        )
    except Exception:
        logger.debug("Compteurs de taches indisponibles.", exc_info=True)

    try:
        actifs = [item["name"] for item in project_model.list_projects(status="active")]
        if actifs:
            lignes.append("Projets actifs : " + ", ".join(actifs[:12]) + ".")
    except Exception:
        logger.debug("Liste des projets indisponible.", exc_info=True)

    return "\n".join(lignes)


def build_system():
    """Construit le bloc d'instructions stable envoye au modele."""
    return SYSTEM_PROMPT.format(catalogue=tools.describe(), contexte=_contexte())


def provider_warning():
    """Retourne l'avertissement a afficher, ou None si le fournisseur convient."""
    from services import providers

    actif = llm.chat_provider()
    if actif in COSTLY_PROVIDERS:
        return CHAT_WARNING.format(label=providers.PROVIDER_LABELS.get(actif, actif))
    return None


def _decision(charge):
    """Extrait la decision du modele : lecture, plan de modification, ou reponse.

    Le modele deraille de deux facons connues : il repond du texte a la place
    du JSON attendu, ou il renvoie un objet vide. Les deux sont traites comme
    une absence de decision, et la boucle appelante tranche.

    :param charge: objet JSON deserialise rendu par le fournisseur
    :return: (nom_outil, arguments, plan, reponse_finale) dont trois valent None
    """
    if not isinstance(charge, dict):
        return None, None, None, None

    # Le plan prime sur la reponse : un modele qui propose des modifications
    # accompagne presque toujours son plan d'une phrase d'annonce.
    etapes = _normalize_plan(charge.get("plan"))
    if etapes:
        return None, None, etapes, None

    reponse = charge.get("reply")
    if isinstance(reponse, str) and reponse.strip():
        return None, None, None, reponse.strip()

    nom = charge.get("tool")
    if isinstance(nom, str) and nom.strip():
        arguments = charge.get("args")
        return nom.strip(), arguments if isinstance(arguments, dict) else {}, None, None

    return None, None, None, None


def _step_label(nom, arguments, fourni=None):
    """Retourne le libelle lisible d'une etape de plan.

    Le modele fournit normalement un label en clair. Quand il l'oublie, on en
    fabrique un a partir des arguments : l'utilisateur doit toujours pouvoir lire
    ce qu'il valide, jamais un nom d'outil brut.
    """
    texte = str(fourni or "").strip()
    if texte:
        return texte[:200]

    reperes = []
    for cle in ("title", "subject", "name", "person", "project", "task_id", "event_id"):
        valeur = arguments.get(cle)
        if valeur not in (None, ""):
            reperes.append(str(valeur))
    return (nom + (" : " + ", ".join(reperes) if reperes else ""))[:200]


def _normalize_plan(etapes):
    """Valide les etapes proposees par le modele.

    Les etapes qui ne designent pas un outil d'ecriture connu sont ecartees :
    un plan n'est pas un moyen detourne de lire, et un outil inexistant ne doit
    pas atteindre la confirmation pour echouer ensuite sous les yeux de
    l'utilisateur.
    """
    if not isinstance(etapes, list):
        return []

    retenues = []
    for item in etapes:
        if not isinstance(item, dict):
            continue

        nom = str(item.get("tool") or "").strip()
        regle = tools.TOOLS.get(nom)
        if not regle or not regle.get("writes"):
            logger.warning("Chat : etape de plan ignoree (%s).", nom or "sans outil")
            continue

        arguments = item.get("args")
        arguments = arguments if isinstance(arguments, dict) else {}

        retenues.append(
            {
                "tool": nom,
                "args": arguments,
                "label": _step_label(nom, arguments, item.get("label")),
                "destructive": bool(regle.get("destructive")),
            }
        )

    return retenues[:MAX_PLAN_STEPS]


def needs_confirmation(etapes):
    """Indique si un plan doit etre confirme avant d'etre applique.

    La regle vit ici, cote serveur, et non dans le prompt : un modele qui
    oublie la consigne ne peut donc pas modifier la base en douce.
    """
    if len(etapes) >= CONFIRM_FROM_WRITES:
        return True
    return any(etape["destructive"] for etape in etapes)


def announce_plan(etapes, annonce=""):
    """Compose le texte qui presente un plan en attente.

    Les etapes sont ecrites dans le texte lui-meme, et pas seulement rendues
    par l'interface : sur Telegram, il n'y a que ce texte. Sans cela, le
    telephone afficherait « J'y vais ? » sans dire de quoi il s'agit.
    """
    lignes = [annonce.strip() or "Je vais faire :"]
    for rang, etape in enumerate(etapes, start=1):
        marque = " (destructif)" if etape["destructive"] else ""
        lignes.append(f"{rang}. {etape['label']}{marque}")
    lignes.append("")
    lignes.append("Reponds oui pour valider, non pour annuler.")
    return "\n".join(lignes)


def _apply_plan(etapes):
    """Execute les etapes d'un plan. Retourne (resume, outils appeles, sources).

    Une etape qui echoue n'interrompt pas les suivantes : l'utilisateur a valide
    un ensemble, et s'arreter au milieu laisserait la base a moitie modifiee
    sans qu'il sache ou. Le resume dit ce qui est passe et ce qui a echoue.
    """
    lignes = []
    appeles = []
    reussies = []
    echecs = 0

    for etape in etapes:
        resultat = tools.execute(etape["tool"], etape["args"])
        appeles.append(etape["tool"])

        if resultat.get("ok"):
            lignes.append("- " + etape["label"])
            reussies.append((etape, resultat))
        else:
            echecs += 1
            lignes.append("- ECHEC " + etape["label"] + " : " + str(resultat.get("error")))

    entete = "Fait." if not echecs else f"Fait, avec {echecs} echec(s)."
    return entete + "\n" + "\n".join(lignes), appeles, chat_sources.from_steps(reussies)


def _confirmation(texte):
    """Lit une reponse de confirmation. Retourne True, False ou None.

    Reconnue sans rappeler le modele : un « oui » ne doit couter ni jeton ni
    attente. Tout autre message rend None, et le plan est alors abandonne au
    profit de la nouvelle demande.
    """
    mots = texte.lower().strip(" .!?").strip()
    if mots in YES_WORDS:
        return True
    if mots in NO_WORDS:
        return False
    return None


def _observation(nom, resultat):
    """Formate le resultat d'un outil pour le renvoyer au modele."""
    return (
        "Resultat de l'outil " + nom + " :\n"
        + json.dumps(resultat, ensure_ascii=False, default=str)
        + "\n\nEnchaine avec un autre outil si necessaire, sinon reponds."
    )


def _repondre(texte, reponse, appeles, conversation_id, pending=None, sources=None):
    """Enregistre le tour dans le fil de la conversation et compose le retour de reply_to."""
    chat_model.add("user", texte, conversation_id=conversation_id)
    chat_model.add("assistant", reponse, tools=appeles, conversation_id=conversation_id, sources=sources)
    return {
        "reply": reponse,
        "tools": appeles,
        "sources": sources or [],
        "error": None,
        "pending": pending,
        "conversation_id": conversation_id,
    }


def _resoudre_plan_en_attente(texte, conversation_id):
    """Traite un message recu alors qu'un plan attend une confirmation.

    Seul le plan de la conversation en cours est concerne : un « oui » tape
    ailleurs ne l'applique jamais.

    :return: le retour de reply_to si le message tranchait, sinon None
    """
    plan = chat_model.pending_plan(conversation_id=conversation_id)
    if plan is None:
        return None

    verdict = _confirmation(texte)

    if verdict is True:
        resume, appeles, sources = _apply_plan(plan["steps"])
        chat_model.close_plan(plan["id"], "applied")
        return _repondre(texte, resume, appeles, conversation_id, sources=sources)

    if verdict is False:
        chat_model.close_plan(plan["id"], "dropped")
        return _repondre(texte, "Annule, rien n'a ete modifie.", [], conversation_id)

    # Ni oui ni non : l'utilisateur passe a autre chose, le plan tombe.
    chat_model.close_plan(plan["id"], "dropped")
    return None


def reply_to(message, conversation_id=None):
    """Repond a un message de l'utilisateur, en appelant les outils necessaires.

    Le message et la reponse sont enregistres dans le fil de la conversation.
    Les resultats d'outils, eux, ne le sont pas : ils sont relus a chaque tour
    sur des donnees fraiches, et les figer donnerait a NOMAIA une base perimee.

    :param message: texte saisi par l'utilisateur
    :param conversation_id: conversation visee (defaut : la courante)
    :return: {"reply", "tools", "sources", "error", "pending", "conversation_id"}
    :raises ValueError: message vide ou conversation introuvable
    """
    texte = (message or "").strip()
    if not texte:
        raise ValueError("Message vide.")

    conversation_id = chat_model.resolve(conversation_id)
    tranche = _resoudre_plan_en_attente(texte, conversation_id)
    if tranche is not None:
        return tranche

    # Le message n'est enregistre qu'une fois la reponse obtenue : un echec de
    # fournisseur laisserait sinon une question sans reponse dans le fil, que
    # le rechargement suivant reafficherait sans moyen d'y repondre.
    systeme = build_system()
    fournisseur = llm.chat_provider()
    plafond = max(1, setting.get_int("chat_max_steps"))
    historique = chat_model.context(
        setting.get_int("chat_history_turns"), conversation_id=conversation_id
    )

    appeles = []
    lus = []  # resultats des outils de lecture : d'ou viennent les sources
    sources = None
    prompt = texte
    reponse = None
    erreur = None
    attente = None

    for tour in range(plafond):
        try:
            charge = llm.ask(
                prompt,
                system=systeme,
                history=historique,
                provider=fournisseur,
                purpose="chat",
                max_tokens=1500,
            )
        except ProviderError as panne:
            logger.error("Chat : echec du fournisseur au tour %s : %s", tour + 1, panne)
            erreur = str(panne)
            break

        nom, arguments, etapes, finale = _decision(charge)

        if finale is not None:
            reponse = finale
            break

        if etapes:
            annonce = str(charge.get("reply") or "").strip()

            if needs_confirmation(etapes):
                plan = chat_model.set_plan(etapes, annonce, conversation_id=conversation_id)
                reponse = announce_plan(etapes, annonce)
                attente = {"id": plan["id"], "steps": etapes}
            else:
                resume, faits, sources = _apply_plan(etapes)
                appeles.extend(faits)
                reponse = resume
            break

        if nom is None:
            # Ni outil ni reponse : le modele a perdu le protocole. On le
            # rappelle une fois, puis la boucle s'arrete d'elle-meme.
            logger.warning("Chat : reponse hors protocole : %s", str(charge)[:200])
            prompt = (
                "Ta reponse ne respecte pas le format. Reponds un unique objet"
                ' JSON : {"tool": ...}, {"plan": [...]} ou {"reply": "..."}.'
            )
            continue

        # Un outil d'ecriture appele en direct est traite comme un plan d'une
        # etape : la regle de confirmation ne se contourne pas en oubliant le
        # format du plan.
        regle = tools.TOOLS.get(nom) or {}
        if regle.get("writes"):
            etapes = _normalize_plan([{"tool": nom, "args": arguments}])
            if needs_confirmation(etapes):
                plan = chat_model.set_plan(etapes, "", conversation_id=conversation_id)
                reponse = announce_plan(etapes)
                attente = {"id": plan["id"], "steps": etapes}
            else:
                resume, faits, sources = _apply_plan(etapes)
                appeles.extend(faits)
                reponse = resume
            break

        resultat = tools.execute(nom, arguments)
        appeles.append(nom)
        lus.append(resultat)

        # L'echange outil est rejoue dans l'historique du tour suivant : c'est
        # ainsi que le modele se souvient de ce qu'il vient de lire.
        historique = historique + [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": json.dumps({"tool": nom, "args": arguments}, ensure_ascii=False)},
        ]
        prompt = _observation(nom, resultat)

    if reponse is None and erreur is None:
        logger.warning("Chat : plafond de %s tours atteint sans reponse.", plafond)
        reponse = FALLBACK_REPLY

    if erreur is not None:
        return {
            "reply": None,
            "tools": appeles,
            "sources": [],
            "error": erreur,
            "pending": None,
            "conversation_id": conversation_id,
        }

    if sources is None and attente is None:
        sources = chat_sources.from_results(lus, reponse)
    return _repondre(texte, reponse, appeles, conversation_id, attente, sources)


def today_label():
    """Retourne la date du jour au format ISO, pour les gabarits."""
    return date.today().isoformat()
