"""Controle d'acces et protection contre les requetes forgees.

Deux barrieres complementaires :

1. Cle d'acces. 127.0.0.1 est joignable par TOUS les comptes Windows de la
   machine, alors que le fichier de base n'appartient qu'a l'utilisateur. Une
   cle tiree au sort et stockee en base ferme donc l'interface aux autres
   comptes et aux extensions de navigateur. Elle ne protege pas d'un programme
   lance sous le compte de l'utilisateur, qui peut lire cette base.

2. Verification de l'origine. Les envois simples et les formulaires multipart
   echappent au controle CORS du navigateur : sans ce garde-fou, une page web
   piegee pourrait agir sur NOMAIA a l'insu de l'utilisateur.

3. Cle des sessions Claude, pour /api/agent/ seulement. Le serveur MCP local
   n'a pas de cookie : il presente une cle distincte de celle de l'interface,
   en en-tete Authorization. Elle n'ouvre que ces routes, et se revoque depuis
   Config sans toucher a l'acces de l'interface. Un en-tete Authorization pose
   par une page web declenche une verification CORS que NOMAIA n'accorde
   jamais : une page piegee ne peut pas s'en servir.

4. Telephone. Il joint NOMAIA par Tailscale serve (HTTPS, reseau prive), a
   l'adresse reglee dans mobile_origin, seule origine distante admise. Il
   recoit le cookie en saisissant un code d'appairage affiche dans Config :
   usage unique, valable dix minutes, efface apres cinq essais faux.
"""
import secrets

from flask import g, jsonify, redirect, render_template, request

import config
from models import setting

# Methodes qui modifient l'etat : les lectures sont soumises a la cle, pas a
# la verification d'origine.
GUARDED_METHODS = ("POST", "PUT", "PATCH", "DELETE")

ALLOWED_ORIGINS = frozenset(
    {
        f"http://{config.HOST}:{config.PORT}",
        f"http://localhost:{config.PORT}",
    }
)

COOKIE_NAME = "nomaia_access"
COOKIE_MAX_AGE = 365 * 24 * 3600

# Chemins accessibles sans cle : la page d'accueil refusee et les feuilles de
# style qui la rendent lisible.
PUBLIC_PREFIXES = ("/static/",)

# Routes des sessions Claude : cle dediee, jamais le cookie de l'interface.
AGENT_PREFIX = "/api/agent/"

# Saisie du code d'appairage : ouverte sans cle, c'est elle qui la donne.
PAIRING_PATH = "/appairage"


def _origin_allowed(origin):
    """Origine locale, ou adresse Tailscale reglee pour le telephone."""
    if origin in ALLOWED_ORIGINS:
        return True
    mobile = setting.get("mobile_origin")
    return bool(mobile) and origin == mobile


def _wants_json():
    """Indique si l'appelant attend du JSON plutot qu'une page HTML."""
    return request.path.startswith("/api/") or "application/json" in request.headers.get(
        "Accept", ""
    )


def _refuse_origin():
    """Reponse renvoyee a une ecriture provenant d'une autre origine."""
    return (
        jsonify(
            {
                "error": "Requete refusee : elle ne provient pas de NOMAIA."
                " Un autre site a tente d'agir sur ton assistant."
            }
        ),
        403,
    )


def _refuse_access():
    """Reponse renvoyee a un appelant sans cle valide."""
    if _wants_json():
        return jsonify({"error": "Acces refuse : cle manquante ou invalide."}), 401
    return render_template("acces.html"), 401


def check_request_origin():
    """Filtre d'entree : verifie l'origine puis la cle d'acces.

    Branche sur before_request, donc applique a toutes les routes, envois de
    fichiers compris.
    """
    if request.path.startswith(PUBLIC_PREFIXES):
        return None

    # --- Origine ---
    if request.method in GUARDED_METHODS:
        fetch_site = request.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return _refuse_origin()

        origin = request.headers.get("Origin")
        if origin and not _origin_allowed(origin):
            return _refuse_origin()

    # --- Sessions Claude : cle dediee ---
    if request.path.startswith(AGENT_PREFIX):
        return _check_agent_key()

    # --- Appairage d'un appareil : la route verifie le code elle-meme ---
    if request.path == PAIRING_PATH and request.method == "POST":
        return None

    # --- Cle d'acces ---
    expected = setting.ensure_access_key()

    # Cle passee en parametre : cas du lien ouvert par l'icone de la barre des
    # taches. On la retire de l'URL et on la remplace par un cookie.
    provided = request.args.get("k")
    if provided and secrets.compare_digest(provided, expected):
        g.grant_cookie = expected
        clean_query = {key: value for key, value in request.args.items() if key != "k"}
        target = request.path + ("?" + "&".join(
            f"{key}={value}" for key, value in clean_query.items()
        ) if clean_query else "")
        return redirect(target)

    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and secrets.compare_digest(cookie, expected):
        return None

    return _refuse_access()


def _check_agent_key():
    """Verifie la cle des sessions Claude presentee en Authorization: Bearer.

    Chaque espace (pro, perso) a sa cle : celle qui correspond fixe
    g.agent_space, que services/agent.py applique a toutes les lectures et
    ecritures de la requete.
    """
    entete = request.headers.get("Authorization", "")
    fournie = entete[7:].strip() if entete.startswith("Bearer ") else ""
    if fournie:
        for space in setting.AGENT_KEY_SETTINGS:
            expected = setting.get_agent_key(space)
            if expected and secrets.compare_digest(
                fournie.encode("utf-8"), expected.encode("utf-8")
            ):
                g.agent_space = space
                return None
    return (
        jsonify(
            {
                "error": "Cle des sessions Claude absente ou invalide."
                " Genere-la dans NOMAIA > Config > Connexions > Sessions Claude."
            }
        ),
        401,
    )


def attach_cookie(response):
    """Pose le cookie d'acces apres une ouverture avec la cle.

    Branche sur after_request. HttpOnly : hors de portee du JavaScript.
    SameSite=Strict : jamais envoye depuis un autre site.
    """
    granted = g.pop("grant_cookie", None)
    if granted:
        response.set_cookie(
            COOKIE_NAME,
            granted,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="Strict",
            path="/",
        )
    return response
