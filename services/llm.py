"""Facade des moteurs de langage : choix du fournisseur, comptage, parsing.

Le fournisseur actif est lu dans les reglages, donc changeable sans
redemarrage ni modification de code. Chaque appel enregistre les jetons
consommes et leur cout estime : c'est ce qui rend l'economie mesurable.
"""
import logging

from models import setting
from models import usage as usage_model
from services import providers
from services.providers import ProviderError

logger = logging.getLogger("nomaia.llm")

# Conserve pour le reste du code : le type d'erreur n'a pas change de nom.
LLMError = ProviderError


def _options(provider):
    """Rassemble les reglages utiles a un fournisseur."""
    options = {
        "model": setting.get(f"model_{provider}"),
        "claude_path": setting.get("claude_path"),
        "claude_config_dir": setting.get("claude_config_dir"),
    }

    cle_reglage = providers.PROVIDER_KEYS.get(provider)
    if cle_reglage:
        options["api_key"] = setting.get_secret(cle_reglage)

    return options


def chat_provider():
    """Retourne le fournisseur dedie a la conversation.

    Le chat est separe du triage parce que leurs economies different : le
    triage tourne quelques fois par jour et supporte un moteur lent, la
    conversation enchaine plusieurs appels par message et exige un moteur
    rapide et bon marche. Vide, le reglage retombe sur celui du triage.
    """
    return setting.get("chat_provider") or setting.get("llm_provider")


def ask(
    prompt,
    images=None,
    timeout=None,
    system=None,
    purpose="triage",
    max_tokens=2000,
    history=None,
    provider=None,
):
    """Interroge un fournisseur et retourne le JSON deserialise.

    :param prompt: partie variable de la demande
    :param images: chemins d'images a analyser
    :param system: bloc d'instructions stable, mis en cache quand c'est possible
    :param purpose: etiquette de comptabilite ('triage', 'bilan', 'chat')
    :param history: tours precedents, format [{'role': ..., 'content': ...}]
    :param provider: force un fournisseur au lieu de celui du triage
    :raises ProviderError: fournisseur indisponible, delai depasse, sortie illisible
    """
    provider = provider or setting.get("llm_provider")
    fonction = providers.PROVIDERS.get(provider)
    if fonction is None:
        raise ProviderError(f"Fournisseur inconnu : {provider}")

    options = _options(provider)
    options["system"] = system or ""
    options["max_tokens"] = max_tokens
    options["history"] = history or []

    reponse = fonction(
        prompt,
        [str(image) for image in (images or []) if image],
        timeout or setting.get_int("llm_timeout_s"),
        options,
    )

    _record(provider, options.get("model") or "", reponse, purpose)
    return extract_json(reponse.text)


def _record(provider, model, reponse, purpose):
    """Enregistre la consommation de l'appel, sans jamais faire echouer celui-ci."""
    try:
        prix_in, prix_out = providers.price_of(provider, model)
        # Les jetons servis depuis le cache sont factures au dixieme du tarif.
        cout = (
            reponse.input_tokens * prix_in
            + reponse.cached_tokens * prix_in * 0.1
            + reponse.output_tokens * prix_out
        ) / 1_000_000

        usage_model.record(
            provider=provider,
            model=model,
            purpose=purpose,
            input_tokens=reponse.input_tokens,
            output_tokens=reponse.output_tokens,
            cached_tokens=reponse.cached_tokens,
            cost_usd=round(cout, 6),
        )
    except Exception:
        logger.debug("Comptage de jetons impossible.", exc_info=True)


def extract_json(raw):
    """Extrait le premier objet JSON equilibre d'une reponse texte.

    Le modele encadre parfois sa reponse de texte ou de balises ```json :
    on ignore tout ce qui entoure le premier objet complet.
    """
    if not raw or not raw.strip():
        raise ProviderError("Le moteur a renvoye une reponse vide.")

    debut = raw.find("{")
    if debut == -1:
        raise ProviderError(f"Aucun JSON dans la reponse : {raw.strip()[:200]}")

    profondeur = 0
    dans_chaine = False
    echappe = False

    for index in range(debut, len(raw)):
        caractere = raw[index]

        if dans_chaine:
            if echappe:
                echappe = False
            elif caractere == "\\":
                echappe = True
            elif caractere == '"':
                dans_chaine = False
            continue

        if caractere == '"':
            dans_chaine = True
        elif caractere == "{":
            profondeur += 1
        elif caractere == "}":
            profondeur -= 1
            if profondeur == 0:
                candidat = raw[debut : index + 1]
                try:
                    import json

                    return json.loads(candidat)
                except ValueError as error:
                    raise ProviderError(f"JSON invalide : {error}")

    raise ProviderError("JSON incomplet dans la reponse du moteur.")


def check_backend():
    """Verifie que le fournisseur actif est utilisable. Retourne son etat."""
    provider = setting.get("llm_provider")
    label = providers.PROVIDER_LABELS.get(provider, provider)
    model = setting.get(f"model_{provider}") or ""

    try:
        cle_reglage = providers.PROVIDER_KEYS.get(provider)
        if cle_reglage and not setting.get_secret(cle_reglage):
            return {
                "backend": provider,
                "label": label,
                "model": model,
                "ok": False,
                "detail": "Cle API manquante.",
            }

        if provider == "claude_cli":
            detail = providers.claude_path(setting.get("claude_path"))
        elif provider == "ollama":
            installes = providers.ollama_models(timeout=5)
            if not installes:
                return {
                    "backend": provider,
                    "label": label,
                    "model": model,
                    "ok": False,
                    "detail": "Ollama ne repond pas. Demarre-le, puis reessaie.",
                }

            # Le service peut repondre alors que le modele choisi n'est pas
            # telecharge : l'appel echouerait en 404 a la premiere capture, et
            # un test au vert aurait laisse croire que tout allait bien.
            noms = {entree["id"] for entree in installes}
            if model and model not in noms:
                return {
                    "backend": provider,
                    "label": label,
                    "model": model,
                    "ok": False,
                    "detail": (
                        f"Le modele {model} n'est pas installe."
                        f" Lance : ollama pull {model} — ou choisis parmi : "
                        + ", ".join(sorted(noms))
                    ),
                }

            detail = model
        else:
            detail = model

        return {"backend": provider, "label": label, "model": model, "ok": True, "detail": detail}
    except Exception as error:
        return {
            "backend": provider,
            "label": label,
            "model": model,
            "ok": False,
            "detail": str(error),
        }
