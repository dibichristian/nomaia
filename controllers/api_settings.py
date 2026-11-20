"""API JSON de configuration : reglages, fournisseurs, consommation de jetons."""
import re
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

import config
from models import setting
from models import usage as usage_model
from services import holidays, llm, providers, transcribe

api_settings_bp = Blueprint("api_settings", __name__, url_prefix="/api")

# Reglages modifiables depuis l'interface, avec leur regle de validation.
# 'secret' : jamais renvoye en lecture, seulement remplace en ecriture.
EDITABLE = {
    "llm_provider": {"type": "choice", "choices": tuple(providers.PROVIDERS)},
    "model_claude_cli": {"type": "model", "provider": "claude_cli"},
    "model_anthropic": {"type": "model", "provider": "anthropic"},
    "model_gemini": {"type": "model", "provider": "gemini"},
    "model_ollama": {"type": "text", "max": 80},
    "anthropic_api_key": {"type": "secret"},
    "gemini_api_key": {"type": "secret"},
    "claude_path": {"type": "text", "max": 400, "allow_empty": True},
    "claude_config_dir": {"type": "text", "max": 400, "allow_empty": True},
    "llm_timeout_s": {"type": "number", "min": 15, "max": 600},
    "auto_apply_confidence": {"type": "number", "min": 0, "max": 1},
    "manual_confidence": {"type": "number", "min": 0, "max": 1},
    "context_task_limit": {"type": "number", "min": 0, "max": 60},
    "context_history_turns": {"type": "number", "min": 2, "max": 40},
    "chat_provider": {"type": "choice", "choices": tuple(providers.PROVIDERS)},
    "chat_history_turns": {"type": "number", "min": 2, "max": 40},
    "chat_max_steps": {"type": "number", "min": 1, "max": 10},
    "telegram_chat_mode": {"type": "choice", "choices": ("0", "1")},
    "telegram_capture_chars": {"type": "number", "min": 80, "max": 4000},
    "voice_enabled": {"type": "choice", "choices": ("0", "1")},
    "voice_model": {"type": "choice", "choices": ("tiny", "base", "small")},
    "voice_language": {"type": "choice", "choices": ("", "fr", "en")},
    "voice_max_s": {"type": "number", "min": 10, "max": 900},
    "work_start": {"type": "time"},
    "work_end": {"type": "time"},
    "lunch_start": {"type": "time"},
    "lunch_end": {"type": "time"},
    "work_days": {"type": "weekdays"},
    "holiday_country": {"type": "choice", "choices": ("",) + tuple(holidays.COUNTRIES)},
    "calendar_sync_min": {"type": "number", "min": 5, "max": 240},
    "foresight_enabled": {"type": "choice", "choices": ("0", "1")},
    "risk_horizon_days": {"type": "number", "min": 1, "max": 30},
    "dormant_after_days": {"type": "number", "min": 7, "max": 180},
    "project_idle_days": {"type": "number", "min": 3, "max": 90},
    "validation_after_hours": {"type": "number", "min": 2, "max": 168},
    "plan_buffer_pct": {"type": "number", "min": 0, "max": 100},
    "plan_horizon_days": {"type": "number", "min": 0, "max": 30},
    "heartbeat_enabled": {"type": "choice", "choices": ("0", "1")},
    "morning_brief_at": {"type": "time"},
    "evening_brief_at": {"type": "time"},
    "drift_alert_days": {"type": "number", "min": 1, "max": 60},
    "event_reminder_enabled": {"type": "choice", "choices": ("0", "1")},
    "event_reminder_min": {"type": "number", "min": 1, "max": 120},
    "estimate_calibration": {"type": "choice", "choices": ("0", "1")},
    "telegram_pinned_digest": {"type": "choice", "choices": ("0", "1")},
    "auto_create_project": {"type": "choice", "choices": ("0", "1")},
    "extract_people": {"type": "choice", "choices": ("0", "1")},
    "display_name": {"type": "text", "max": 80, "allow_empty": True},
    "nudge_after_days": {"type": "number", "min": 1, "max": 60},
    "nudge_quiet_days": {"type": "number", "min": 1, "max": 30},
    "telegram_token": {"type": "secret"},
    "odoo_url": {"type": "text", "max": 300, "allow_empty": True},
    "odoo_db": {"type": "text", "max": 120, "allow_empty": True},
    "odoo_login": {"type": "text", "max": 200, "allow_empty": True},
    "odoo_protocol": {"type": "choice", "choices": ("json2", "jsonrpc")},
    "odoo_api_key": {"type": "secret"},
    "odoo_cal_leaves": {"type": "choice", "choices": ("0", "1")},
    "odoo_cal_pending": {"type": "choice", "choices": ("0", "1")},
    "odoo_cal_holidays": {"type": "choice", "choices": ("0", "1")},
    "odoo_cal_meetings": {"type": "choice", "choices": ("0", "1")},
    "mail_enabled": {"type": "choice", "choices": ("0", "1")},
    "space_filter": {"type": "choice", "choices": setting.SPACE_FILTERS},
    "create_space": {"type": "choice", "choices": ("pro", "perso")},
    "perso_hours_enabled": {"type": "choice", "choices": ("0", "1")},
    "perso_start": {"type": "time"},
    "perso_end": {"type": "time"},
    "perso_offday_start": {"type": "time"},
    "perso_offday_end": {"type": "time"},
    "space_pro_color_from": {"type": "color"},
    "space_pro_color_to": {"type": "color"},
    "space_perso_color_from": {"type": "color"},
    "space_perso_color_to": {"type": "color"},
}

SECRET_FIELDS = tuple(key for key, regle in EDITABLE.items() if regle["type"] == "secret")


class SettingError(ValueError):
    """Valeur de reglage refusee."""


def _validate(key, value):
    """Valide une valeur de reglage et retourne la valeur a stocker."""
    regle = EDITABLE.get(key)
    if regle is None:
        raise SettingError(f"Reglage inconnu : {key}")

    if regle["type"] == "choice":
        if value not in regle["choices"]:
            raise SettingError(f"{key} : valeur hors liste.")
        return value

    if regle["type"] == "model":
        connus = {entree["id"] for entree in providers.models_for(regle["provider"])}
        if value not in connus:
            raise SettingError(f"{key} : modele inconnu pour ce fournisseur.")
        return value

    if regle["type"] == "number":
        try:
            nombre = float(value)
        except (TypeError, ValueError):
            raise SettingError(f"{key} attend un nombre.")
        if not regle["min"] <= nombre <= regle["max"]:
            raise SettingError(f"{key} doit rester entre {regle['min']} et {regle['max']}.")
        return str(value)

    if regle["type"] == "weekdays":
        # Liste '0,1,2,3,4' (lundi = 0). Au moins un jour : une semaine
        # entierement chomee rendrait tout plan vide sans raison visible.
        parties = [part.strip() for part in str(value or "").split(",") if part.strip()]
        if not parties or any(part not in "0123456" or len(part) != 1 for part in parties):
            raise SettingError(f"{key} attend une liste de jours 0 a 6, au moins un.")
        return ",".join(sorted(set(parties)))

    if regle["type"] == "time":
        try:
            datetime.strptime(str(value), "%H:%M")
        except ValueError:
            raise SettingError(f"{key} attend une heure au format HH:MM.")
        return value

    if regle["type"] == "color":
        # Injectee dans une balise <style> : seul #rrggbb passe. La casse est
        # ramenee en minuscules (format renvoye par <input type="color">).
        couleur = value.strip().lower() if isinstance(value, str) else ""
        if not setting.is_space_color(couleur):
            raise SettingError(f"{key} attend une couleur au format #rrggbb.")
        return couleur

    texte = str(value or "").strip()
    if regle["type"] == "secret":
        # Une cle ne contient jamais d'espace : une valeur masquee renvoyee
        # telle quelle par l'interface serait une erreur de manipulation.
        if texte and (" " in texte or len(texte) > 400):
            raise SettingError(f"{key} : format de cle invalide.")
        return texte

    if not texte and not regle.get("allow_empty"):
        raise SettingError(f"{key} ne peut pas etre vide.")
    if len(texte) > regle.get("max", 200):
        raise SettingError(f"{key} est trop long.")
    return texte


def _mcp_script():
    """Chemin du serveur MCP des sessions Claude, a cote du code de NOMAIA."""
    return config.RESOURCE_DIR / "integrations" / "claude_mcp.py"


def _mcp_command(key):
    """Commande d'enregistrement du serveur MCP dans Claude Code (portee utilisateur)."""
    return (
        f'claude mcp add --scope user nomaia -e NOMAIA_AGENT_KEY={key}'
        f' -- python "{_mcp_script()}"'
    )


@api_settings_bp.get("/agent-setup")
def get_agent_setup():
    """Etat du lien avec les sessions Claude, par espace. Les cles ne sont jamais relues ici."""
    spaces = {space: bool(setting.get_agent_key(space)) for space in setting.AGENT_KEY_SETTINGS}
    return jsonify(
        {
            "defined": spaces["pro"],
            "spaces": spaces,
            "script": str(_mcp_script()),
            "script_exists": _mcp_script().exists(),
        }
    )


@api_settings_bp.post("/agent-setup/key")
def regenerate_agent_key():
    """Tire une nouvelle cle d'un espace et renvoie, une seule fois, la commande qui la porte.

    Corps JSON facultatif : {"space": "pro" | "perso"}, 'pro' par defaut.
    L'ancienne cle de cet espace cesse aussitot de fonctionner : une session
    deja enregistree doit l'etre a nouveau avec la commande renvoyee.
    """
    space = (request.get_json(silent=True) or {}).get("space") or "pro"
    try:
        key = setting.regenerate_agent_key(space)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(
        {
            "defined": True,
            "space": space,
            "command": _mcp_command(key),
            "remove_command": "claude mcp remove --scope user nomaia",
        }
    )


# Adresse servie par Tailscale serve : HTTPS, nom d'hote, port facultatif.
MOBILE_ORIGIN_PATTERN = re.compile(r"^https://[a-z0-9-]+(\.[a-z0-9-]+)+(:\d{1,5})?$")


def _normalize_origin(value):
    """Ramene une adresse collee (slash final, chemin, sans schema) a une origine.

    :return: 'https://hote[:port]', ou chaine vide pour retirer l'acces
    """
    texte = str(value or "").strip().lower()
    if not texte:
        return ""
    if "://" not in texte:
        texte = "https://" + texte
    schema, reste = texte.split("://", 1)
    origine = schema + "://" + reste.split("/", 1)[0]
    if not MOBILE_ORIGIN_PATTERN.match(origine):
        raise SettingError(
            "Adresse attendue en HTTPS, par exemple https://mon-pc.tail1234.ts.net"
        )
    return origine


@api_settings_bp.get("/mobile")
def get_mobile():
    """Adresse Tailscale reglee pour le telephone, et port local a servir."""
    return jsonify({"origin": setting.get("mobile_origin"), "port": config.PORT})


@api_settings_bp.put("/mobile")
def update_mobile():
    """Enregistre l'adresse Tailscale par laquelle le telephone joint NOMAIA."""
    payload = request.get_json(silent=True) or {}
    try:
        origine = _normalize_origin(payload.get("origin"))
    except SettingError as error:
        return jsonify({"error": str(error)}), 400
    setting.set_value("mobile_origin", origine)
    return jsonify({"origin": origine, "port": config.PORT})


@api_settings_bp.post("/mobile/code")
def issue_pairing_code():
    """Tire un code d'appairage a saisir sur le telephone.

    Refuse tant qu'aucune adresse n'est reglee : sans elle, le telephone ne
    pourrait pas envoyer le code (origine refusee).
    """
    if not setting.get("mobile_origin"):
        return jsonify({"error": "Renseigne d'abord l'adresse Tailscale du PC."}), 400
    code = setting.issue_pairing_code()
    return jsonify(
        {
            "code": code[:4] + "-" + code[4:],
            "expires_in": setting.PAIRING_TTL_S,
            "url": setting.get("mobile_origin"),
        }
    )


@api_settings_bp.get("/settings")
def get_settings():
    """Retourne la configuration courante, secrets masques."""
    valeurs = {key: setting.get(key) for key in EDITABLE if key not in SECRET_FIELDS}
    # Couleurs d'espace : meme repli que la page (valeur hors format -> defaut).
    couleurs = setting.space_colors()
    for variable, key in setting.SPACE_COLOR_SETTINGS.items():
        valeurs[key] = couleurs[variable]

    secrets_definis = {}
    for key in SECRET_FIELDS:
        valeur = setting.get_secret(key)
        secrets_definis[key] = {
            "defined": bool(valeur),
            "hint": ("…" + valeur[-4:]) if valeur else "",
        }

    bridge = current_app.extensions.get("telegram")
    worker = current_app.extensions.get("triage_worker")
    heartbeat = current_app.extensions.get("heartbeat")

    return jsonify(
        {
            "settings": valeurs,
            "secrets": secrets_definis,
            "holiday_countries": [
                {"id": code, "label": regles["label"]}
                for code, regles in holidays.COUNTRIES.items()
            ],
            "providers": [
                {
                    "id": identifiant,
                    "label": providers.PROVIDER_LABELS[identifiant],
                    "models": providers.models_for(identifiant),
                    "needs_key": identifiant in providers.PROVIDER_KEYS,
                    "key_setting": providers.PROVIDER_KEYS.get(identifiant),
                }
                for identifiant in providers.PROVIDERS
            ],
            "status": {
                "llm": llm.check_backend(),
                "telegram": bridge.check() if bridge else {"ok": False},
                "worker_running": bool(worker and worker.is_running()),
                "heartbeat_running": bool(heartbeat and heartbeat.is_running()),
                "voice_available": transcribe.is_available(),
            },
            "system": {
                "version": config.VERSION,
                "data_dir": str(config.DATA_DIR),
                "resource_dir": str(config.RESOURCE_DIR),
                "db_size_kb": round(config.DB_PATH.stat().st_size / 1024)
                if config.DB_PATH.exists()
                else 0,
                "backups": len(list(config.BACKUP_DIR.glob("nomaia_*.db"))),
            },
        }
    )


@api_settings_bp.patch("/settings")
def update_settings():
    """Met a jour un ou plusieurs reglages."""
    payload = request.get_json(silent=True) or {}
    ecrits = []

    try:
        for key, value in payload.items():
            valeur = _validate(key, value)
            if key in SECRET_FIELDS:
                # Une chaine vide efface la cle ; c'est le seul moyen de la retirer.
                setting.set_secret(key, valeur)
            else:
                setting.set_value(key, valeur)
            ecrits.append(key)
    except SettingError as error:
        return jsonify({"error": str(error)}), 400

    # Un nouveau pays prend effet tout de suite, sans attendre le battement.
    if "holiday_country" in ecrits:
        holidays.ensure_imported()

    # La passerelle Telegram doit repartir si son jeton vient de changer.
    if "telegram_token" in ecrits:
        bridge = current_app.extensions.get("telegram")
        if bridge:
            bridge.stop()
            bridge.start()

    return jsonify({"updated": ecrits, "status": {"llm": llm.check_backend()}})


@api_settings_bp.post("/settings/test")
def test_provider():
    """Envoie un appel minimal au fournisseur actif pour verifier la chaine."""
    debut = datetime.now()
    try:
        reponse = llm.ask(
            'Reponds uniquement : {"ok": true}',
            system="Tu reponds uniquement par du JSON valide.",
            purpose="test",
            max_tokens=50,
            timeout=60,
        )
    except Exception as error:
        return jsonify({"ok": False, "error": str(error)}), 503

    return jsonify(
        {
            "ok": bool(reponse),
            "seconds": round((datetime.now() - debut).total_seconds(), 1),
            "reply": reponse,
        }
    )


@api_settings_bp.get("/usage")
def get_usage():
    """Retourne la consommation de jetons sur une periode glissante."""
    jours = request.args.get("days", default=30, type=int)
    return jsonify(usage_model.summary(days=max(1, min(jours, 365))))
