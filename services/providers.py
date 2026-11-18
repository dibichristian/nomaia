"""Fournisseurs de modeles de langage.

Chaque fournisseur expose la meme fonction : recevoir un prompt et des images,
rendre un texte brut et le decompte de jetons consommes. Le choix se fait dans
les reglages, sans redemarrage.

Cout des jetons : le moteur claude_cli n'a pas de cout monetaire (il passe par
l'abonnement Claude Code) mais recharge son prompt systeme complet a chaque
appel, soit environ 40 000 jetons par triage. Les fournisseurs par interface de
programmation n'envoient que le prompt de NOMAIA, soit 1 500 a 2 500 jetons.
"""
import base64
import json
import logging
import mimetypes
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import config

logger = logging.getLogger("nomaia.providers")

# Longueur du detail d'erreur remonte a l'interface. La sortie complete part
# toujours au journal : c'est elle qui porte le motif reel d'un echec.
DETAIL_CHARS = 300

# Catalogue des modeles proposes dans la configuration.
# prix en dollars par million de jetons, a titre indicatif.
MODELS = {
    "claude_cli": [
        {"id": "haiku", "label": "Haiku 4.5 — le plus economique", "in": 0, "out": 0},
        {"id": "sonnet", "label": "Sonnet 5 — equilibre", "in": 0, "out": 0},
        {"id": "opus", "label": "Opus 5 — le plus capable", "in": 0, "out": 0},
    ],
    "anthropic": [
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "in": 1.0, "out": 5.0},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "in": 2.0, "out": 10.0},
        {"id": "claude-opus-5", "label": "Claude Opus 5", "in": 5.0, "out": 25.0},
    ],
    "gemini": [
        {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash Lite", "in": 0.1, "out": 0.4},
        {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "in": 0.75, "out": 3.75},
    ],
    "ollama": [
        {"id": "qwen2.5vl:7b", "label": "Qwen2.5-VL 7B (local, vision)", "in": 0, "out": 0},
        {"id": "llama3.2-vision:11b", "label": "Llama 3.2 Vision 11B (local)", "in": 0, "out": 0},
        {"id": "mistral-small:latest", "label": "Mistral Small (local, sans vision)", "in": 0, "out": 0},
    ],
}

PROVIDER_LABELS = {
    "claude_cli": "Claude Code (abonnement)",
    "anthropic": "API Claude (cle)",
    "gemini": "API Gemini (cle)",
    "ollama": "Ollama (local)",
}

# Fournisseurs exigeant une cle, et nom du reglage qui la porte.
PROVIDER_KEYS = {
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

MIN_NATIVE_BYTES = 1024 * 1024

# Windows ouvre une console pour tout executable console lance depuis une
# application sans terminal. Sans ce drapeau, une fenetre noire surgit au
# premier plan a chaque triage, vole le focus, puis disparait. Le getattr rend
# le code utilisable ailleurs, ou la constante n'existe pas.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NATIVE_CLAUDE = Path(config.BASE_DIR)  # remplace a l'initialisation ci-dessous


class ProviderError(RuntimeError):
    """Echec d'appel a un fournisseur.

    :param transient: panne passagere meritant une nouvelle tentative differee
    """

    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


class Response:
    """Reponse d'un fournisseur : texte brut et comptage de jetons."""

    def __init__(self, text, input_tokens=0, output_tokens=0, cached_tokens=0):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens


def _post_json(url, payload, headers, timeout, nom):
    """Envoie une requete JSON et retourne la reponse deserialisee."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        corps = error.read().decode("utf-8", "replace")
        logger.error("%s a repondu %s : %s", nom, error.code, corps)
        # 429 et 5xx sont passagers : surcharge ou incident cote fournisseur.
        raise ProviderError(
            f"{nom} a repondu {error.code} : {corps[:DETAIL_CHARS]}",
            transient=error.code == 429 or error.code >= 500,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderError(f"{nom} injoignable : {error}", transient=True)
    except ValueError as error:
        raise ProviderError(f"Reponse {nom} illisible : {error}")


def _encode_image(path):
    """Retourne le type MIME et le contenu encode en base64 d'une image."""
    chemin = Path(path)
    try:
        donnees = chemin.read_bytes()
    except OSError as error:
        raise ProviderError(f"Image illisible : {error}")

    mime = mimetypes.guess_type(chemin.name)[0] or "image/png"
    return mime, base64.b64encode(donnees).decode("ascii")


def _history(options):
    """Retourne l'historique de conversation normalise, jamais None.

    Seuls les roles 'user' et 'assistant' sont conserves et les entrees vides
    sont ecartees : un message vide fait echouer l'API Claude.
    """
    tours = []
    for message in options.get("history") or []:
        role = message.get("role")
        contenu = (message.get("content") or "").strip()
        if role in ("user", "assistant") and contenu:
            tours.append({"role": role, "content": contenu})
    return tours


# --- Claude Code en sous-processus -------------------------------------------


def claude_path(override=""):
    """Retourne le chemin d'un executable claude utilisable.

    La resolution est refaite a chaque appel : pendant une auto-mise a jour,
    npm renomme les raccourcis et remplace le binaire, donc un chemin memorise
    devient faux sans prevenir.
    """
    import os

    candidats = []
    if override:
        candidats.append(Path(override))

    natif = (
        Path(os.environ.get("APPDATA", ""))
        / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    )
    if natif.parent.exists():
        candidats.append(natif)

    trouve = shutil.which("claude")
    if trouve:
        candidats.append(Path(trouve))

    for candidat in candidats:
        if not candidat.is_file():
            continue
        # Un .exe minuscule est le talon laisse par une installation inachevee.
        if candidat.suffix.lower() == ".exe" and candidat.stat().st_size < MIN_NATIVE_BYTES:
            continue
        return str(candidat)

    raise ProviderError(
        "Executable 'claude' introuvable ou incomplet."
        " Claude Code est peut-etre en cours de mise a jour ;"
        " sinon relance : npm install -g @anthropic-ai/claude-code",
        transient=True,
    )


def _claude_env(config_dir=""):
    """Retourne l'environnement du sous-processus claude.

    :param config_dir: dossier de configuration a imposer, vide pour heriter
    """
    import os

    environnement = dict(os.environ)
    dossier = (config_dir or "").strip()
    if dossier:
        environnement["CLAUDE_CONFIG_DIR"] = dossier
    return environnement


def ask_claude_cli(prompt, images, timeout, options):
    """Appelle `claude -p` en sous-processus.

    Le sous-processus ne peut lire QUE le dossier des captures : la portee est
    portee par le motif Read(chemin/**). `--add-dir` ne restreint rien, il
    ajoute un acces, et `--allowedTools Read` seul autorise la lecture de tout
    le disque accessible a l'utilisateur.
    """
    portee = "Read(" + str(config.UPLOAD_DIR).replace("\\", "/") + "/**)"

    # Le CLI n'a pas de champ systeme distinct : le bloc d'instructions est
    # place en tete du prompt. Sans cela, le modele repond en prose au lieu
    # du JSON attendu.
    systeme = options.get("system") or ""
    if systeme:
        prompt = systeme + "\n\n---\n\n" + prompt

    # Le CLI est sans memoire d'un appel a l'autre et n'accepte pas de liste de
    # messages : l'historique de conversation est donc recopie en texte.
    tours = _history(options)
    if tours:
        lignes = [
            ("Moi : " if tour["role"] == "user" else "NOMAIA : ") + tour["content"]
            for tour in tours
        ]
        prompt = (
            "--- Echanges precedents ---\n"
            + "\n".join(lignes)
            + "\n--- Fin des echanges precedents ---\n\n"
            + prompt
        )

    args = [
        claude_path(options.get("claude_path", "")),
        "-p",
        "--model",
        options.get("model") or "sonnet",
        "--output-format",
        "json",
        "--allowedTools",
        portee,
        "--permission-mode",
        "default",
        # Sans cette paire, chaque appel tente de charger les serveurs MCP de
        # l'utilisateur, dont un serveur en echec qui fait perdre du temps.
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]

    try:
        termine = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(config.UPLOAD_DIR),
            env=_claude_env(options.get("claude_config_dir", "")),
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise ProviderError(f"Claude Code n'a pas repondu en {timeout} s.", transient=True)
    except OSError as error:
        # Disparait quand npm remplace le binaire pendant une mise a jour.
        raise ProviderError(f"Lancement de claude impossible : {error}", transient=True)

    if termine.returncode != 0:
        raise _erreur_cli(termine)

    # La sortie JSON du CLI porte le texte et le decompte de jetons.
    try:
        enveloppe = json.loads(termine.stdout)
        usage = enveloppe.get("usage") or {}
        return Response(
            enveloppe.get("result", ""),
            input_tokens=usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cache_read_input_tokens", 0),
        )
    except ValueError:
        # Repli : ancienne sortie texte brut.
        return Response(termine.stdout)


# Motifs d'echec qui se resolvent d'eux-memes : mise a jour npm en cours,
# surcharge passagere, coupure reseau. Ils meritent une nouvelle tentative.
CLI_TRANSIENT_MARKERS = (
    "native binary not installed",
    "introuvable",
    "rate limit",
    "overloaded",
    "timeout",
    "econnreset",
    "etimedout",
    "socket hang up",
)


def _erreur_cli(termine):
    """Construit l'erreur d'un appel `claude -p` qui a echoue.

    Le CLI repond en JSON (`--output-format json`) meme en echec : le motif
    reel vit dans le champ `result`, parfois plusieurs milliers de caracteres
    apres le debut de la sortie. Tronquer betement la chaine, comme le faisait
    la version precedente, coupait systematiquement avant ce champ et rendait
    toute panne indiagnosticable.

    :param termine: resultat de subprocess.run
    :return: ProviderError pret a etre leve
    """
    stdout = (termine.stdout or "").strip()
    stderr = (termine.stderr or "").strip()

    motif = ""
    try:
        enveloppe = json.loads(stdout)
    except ValueError:
        enveloppe = None

    if isinstance(enveloppe, dict):
        motif = str(
            enveloppe.get("result")
            or enveloppe.get("error")
            or enveloppe.get("subtype")
            or enveloppe.get("stop_reason")
            or ""
        ).strip()

    # Ni JSON exploitable ni champ de motif : on retombe sur les flux bruts.
    detail = motif or stderr or stdout or "aucune sortie"

    # Le journal garde tout : c'est le seul endroit ou le motif reste entier.
    logger.error(
        "Claude Code a echoue (code %s).\n--- stdout ---\n%s\n--- stderr ---\n%s",
        termine.returncode,
        stdout or "(vide)",
        stderr or "(vide)",
    )

    repere = (detail + " " + stderr).lower()
    passager = any(marqueur in repere for marqueur in CLI_TRANSIENT_MARKERS)

    return ProviderError(
        f"Claude Code a echoue (code {termine.returncode}) : {detail[:DETAIL_CHARS]}",
        transient=passager,
    )


# --- API Claude ---------------------------------------------------------------


def ask_anthropic(prompt, images, timeout, options):
    """Appelle l'API Claude.

    Le bloc d'instructions est place en `system` avec mise en cache : sur des
    triages successifs, la partie stable n'est facturee qu'un dixieme.
    """
    cle = options.get("api_key")
    if not cle:
        raise ProviderError("Aucune cle API Claude enregistree.")

    contenu = []
    for image in images:
        mime, donnees = _encode_image(image)
        contenu.append(
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": donnees}}
        )
    contenu.append({"type": "text", "text": prompt})

    payload = {
        "model": options.get("model") or "claude-sonnet-5",
        "max_tokens": options.get("max_tokens", 2000),
        "system": [
            {
                "type": "text",
                "text": options.get("system") or "",
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": _history(options) + [{"role": "user", "content": contenu}],
    }

    corps = _post_json(
        ANTHROPIC_URL,
        payload,
        {"x-api-key": cle, "anthropic-version": ANTHROPIC_VERSION},
        timeout,
        "L'API Claude",
    )

    texte = "".join(
        bloc.get("text", "") for bloc in corps.get("content", []) if bloc.get("type") == "text"
    )
    usage = corps.get("usage") or {}
    return Response(
        texte,
        input_tokens=usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cached_tokens=usage.get("cache_read_input_tokens", 0),
    )


# --- API Gemini ---------------------------------------------------------------


def _contenus_gemini(options):
    """Traduit l'historique au format Gemini, qui nomme 'model' le role assistant."""
    return [
        {
            "role": "user" if tour["role"] == "user" else "model",
            "parts": [{"text": tour["content"]}],
        }
        for tour in _history(options)
    ]


def ask_gemini(prompt, images, timeout, options):
    """Appelle l'API Gemini de Google."""
    cle = options.get("api_key")
    if not cle:
        raise ProviderError("Aucune cle API Gemini enregistree.")

    parts = []
    for image in images:
        mime, donnees = _encode_image(image)
        parts.append({"inlineData": {"mimeType": mime, "data": donnees}})

    systeme = options.get("system") or ""
    parts.append({"text": (systeme + "\n\n" + prompt) if systeme else prompt})

    payload = {
        "contents": _contenus_gemini(options) + [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": options.get("max_tokens", 2000),
        },
    }

    modele = options.get("model") or "gemini-3.8-flash"
    corps = _post_json(
        GEMINI_URL.format(model=modele),
        payload,
        {"x-goog-api-key": cle},
        timeout,
        "L'API Gemini",
    )

    candidats = corps.get("candidates") or []
    if not candidats:
        raise ProviderError("Gemini n'a renvoye aucune reponse.")

    texte = "".join(
        part.get("text", "") for part in (candidats[0].get("content") or {}).get("parts", [])
    )
    usage = corps.get("usageMetadata") or {}
    return Response(
        texte,
        input_tokens=usage.get("promptTokenCount", 0),
        output_tokens=usage.get("candidatesTokenCount", 0),
        cached_tokens=usage.get("cachedContentTokenCount", 0),
    )


# --- Ollama local -------------------------------------------------------------


def ask_ollama(prompt, images, timeout, options):
    """Appelle un modele local servi par Ollama."""
    message = {"role": "user", "content": prompt}

    if images:
        encodees = []
        for image in images:
            _, donnees = _encode_image(image)
            encodees.append(donnees)
        message["images"] = encodees

    systeme = options.get("system") or ""
    messages = (
        ([{"role": "system", "content": systeme}] if systeme else [])
        + _history(options)
        + [message]
    )

    payload = {
        "model": options.get("model") or "qwen2.5vl:7b",
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }

    try:
        corps = _post_json(OLLAMA_URL, payload, {}, timeout, "Ollama")
    except ProviderError as panne:
        raise _erreur_ollama(panne, payload["model"])

    return Response(
        (corps.get("message") or {}).get("content", ""),
        input_tokens=corps.get("prompt_eval_count", 0),
        output_tokens=corps.get("eval_count", 0),
    )


def _erreur_ollama(panne, modele):
    """Rend lisible un echec Ollama dont le motif brut n'aide pas.

    Le cas le plus deroutant est un modele telecharge mais que le moteur ne
    sait pas charger : rien dans l'interface ne le distingue d'un modele sain,
    et le motif parle d'architecture inconnue, ce qui ne dit pas quoi faire.
    """
    texte = str(panne)
    repere = texte.lower()

    if "unknown model architecture" in repere:
        return ProviderError(
            f"Le modele {modele} est telecharge mais incompatible avec cette"
            " version d'Ollama : son architecture n'est pas reconnue par le"
            " moteur. Choisis un autre modele, ou mets Ollama a jour.",
        )

    if "not found" in repere and "model" in repere:
        return ProviderError(
            f"Le modele {modele} n'est pas installe. Lance : ollama pull {modele}"
        )

    if "memory" in repere or "out of memory" in repere:
        return ProviderError(
            f"Pas assez de memoire pour charger {modele}. Prends un modele plus"
            " petit, ou libere de la memoire graphique.",
            transient=True,
        )

    return panne


# --- Modeles Ollama reellement installes ----------------------------------------

OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"

# Attente courte : la liste sert a remplir un formulaire, pas a repondre a une
# question. Ollama eteint ne doit pas figer la page de configuration.
OLLAMA_TAGS_TIMEOUT_S = 3


def _human_size(octets):
    """Retourne une taille de modele lisible, en gigaoctets."""
    try:
        return "%.1f Go" % (int(octets) / (1024 ** 3))
    except (TypeError, ValueError):
        return ""


def ollama_models(timeout=OLLAMA_TAGS_TIMEOUT_S):
    """Retourne les modeles Ollama presents sur le poste.

    La liste vient d'Ollama lui-meme : un catalogue fige proposerait des
    modeles absents et cacherait ceux qui sont la. Chaque entree porte la
    meme forme que MODELS, plus 'vision', qui dit si le modele sait lire une
    capture d'ecran.

    :return: liste de modeles, vide si Ollama est injoignable
    """
    import json as json_module
    import urllib.request

    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=timeout) as reponse:
            corps = json_module.loads(reponse.read().decode("utf-8"))
    except Exception:
        # Ollama eteint ou absent : l'appelant retombe sur le catalogue connu.
        logger.debug("Liste des modeles Ollama indisponible.", exc_info=True)
        return []

    entrees = []
    for item in corps.get("models") or []:
        identifiant = item.get("model") or item.get("name")
        if not identifiant:
            continue

        capacites = item.get("capabilities") or []
        details = item.get("details") or {}
        vision = "vision" in capacites

        precisions = [details.get("parameter_size"), _human_size(item.get("size"))]
        precisions = [texte for texte in precisions if texte]
        if not vision:
            precisions.append("sans vision")

        entrees.append(
            {
                "id": identifiant,
                "label": identifiant + (" — " + ", ".join(precisions) if precisions else ""),
                "in": 0,
                "out": 0,
                "vision": vision,
            }
        )

    return sorted(entrees, key=lambda entree: entree["id"])


def models_for(provider):
    """Retourne les modeles proposables pour un fournisseur.

    Ollama est le seul dont la liste depend du poste : les autres exposent un
    catalogue stable. Quand Ollama est eteint, on rend le catalogue connu
    plutot qu'une liste vide, qui empecherait toute configuration.
    """
    if provider != "ollama":
        return MODELS.get(provider, [])

    installes = ollama_models()
    return installes or MODELS.get("ollama", [])


PROVIDERS = {
    "claude_cli": ask_claude_cli,
    "anthropic": ask_anthropic,
    "gemini": ask_gemini,
    "ollama": ask_ollama,
}


def price_of(provider, model):
    """Retourne le tarif d'un modele, en dollars par million de jetons."""
    for entree in MODELS.get(provider, []):
        if entree["id"] == model:
            return entree["in"], entree["out"]
    return 0.0, 0.0
