"""API JSON des conversations avec NOMAIA."""
from flask import Blueprint, jsonify, request

from models import chat as chat_model
from models import setting
from models.chat import ConversationError
from services import assistant, llm, providers

api_chat_bp = Blueprint("api_chat", __name__, url_prefix="/api")

# Nombre de messages renvoyes a l'interface. Bien au-dela des tours envoyes au
# modele : relire son fil ne coute rien, le rappeler au modele coute des jetons.
DISPLAY_LIMIT = 100


@api_chat_bp.errorhandler(ConversationError)
def handle_conversation_error(error):
    """Conversation introuvable ou identifiant invalide."""
    return jsonify({"error": str(error)}), 404


def _conversation_arg(source=None):
    """Conversation demandee (corps JSON ou parametre), None pour la courante."""
    if source and source.get("conversation_id") not in (None, ""):
        return source.get("conversation_id")
    return request.args.get("conversation_id") or None


def _pending(conversation_id):
    """Retourne le plan en attente de confirmation de la conversation, ou None.

    Il peut avoir ete pose depuis Telegram : l'interface doit donc le
    decouvrir au chargement, pas seulement apres un envoi.
    """
    plan = chat_model.pending_plan(conversation_id=conversation_id)
    if plan is None:
        return None
    return {"id": plan["id"], "steps": plan["steps"]}


def _conversations():
    """Liste des conversations, avec celle que suit Telegram.

    `archived=1` dans l'adresse inclut les conversations archivees.
    """
    return {
        "conversations": chat_model.list_conversations(
            include_archived=request.args.get("archived") == "1"
        ),
        "telegram_conversation_id": int(setting.get("telegram_conversation_id") or 0) or None,
    }


def _etat(conversation_id):
    """Retourne le fil d'une conversation et l'etat du fournisseur de conversation."""
    actif = llm.chat_provider()
    return {
        "conversation": chat_model.get_conversation(conversation_id),
        "messages": chat_model.history(limit=DISPLAY_LIMIT, conversation_id=conversation_id),
        "provider": actif,
        "provider_label": providers.PROVIDER_LABELS.get(actif, actif),
        "warning": assistant.provider_warning(),
        "pending": _pending(conversation_id),
        **_conversations(),
    }


@api_chat_bp.get("/chat")
def get_chat():
    """Retourne une conversation (par defaut la courante) et la liste des conversations."""
    return jsonify(_etat(chat_model.resolve(_conversation_arg())))


@api_chat_bp.post("/chat")
def post_chat():
    """Transmet un message a NOMAIA dans une conversation et retourne sa reponse.

    Appel long : la boucle enchaine jusqu'a chat_max_steps allers-retours avec
    le fournisseur, soit plusieurs dizaines de secondes.
    """
    charge = request.get_json(silent=True) or {}
    message = (charge.get("message") or "").strip()

    if not message:
        return jsonify({"error": "Message vide."}), 400

    if len(message) > chat_model.MAX_CONTENT_CHARS:
        return (
            jsonify(
                {
                    "error": "Message trop long pour une conversation."
                    " Depose-le dans la boite de reception : le triage est fait pour ca."
                }
            ),
            400,
        )

    conversation_id = chat_model.resolve(_conversation_arg(charge))
    resultat = assistant.reply_to(message, conversation_id=conversation_id)
    messages = chat_model.history(limit=DISPLAY_LIMIT, conversation_id=conversation_id)

    if resultat["error"]:
        return jsonify({"error": resultat["error"], "messages": messages}), 503

    return jsonify(
        {
            "reply": resultat["reply"],
            "tools": resultat["tools"],
            "sources": resultat.get("sources") or [],
            "messages": messages,
            "pending": resultat.get("pending"),
            "conversation": chat_model.get_conversation(conversation_id),
            **_conversations(),
        }
    )


@api_chat_bp.delete("/chat")
def delete_chat():
    """Vide le fil d'une conversation (par defaut la courante)."""
    conversation_id = chat_model.resolve(_conversation_arg())
    # Un plan en attente n'a plus de sens sans le fil qui l'a produit.
    plan = chat_model.get_plan(conversation_id=conversation_id)
    if plan and plan["status"] == "pending":
        chat_model.close_plan(plan["id"], "dropped")

    supprimes = chat_model.clear(conversation_id)
    return jsonify({"ok": True, "deleted": supprimes})


@api_chat_bp.get("/conversations")
def list_conversations():
    """Conversations, la plus recemment active d'abord."""
    return jsonify(_conversations())


@api_chat_bp.post("/conversations")
def create_conversation():
    """Ouvre une nouvelle conversation."""
    charge = request.get_json(silent=True) or {}
    return jsonify(
        chat_model.create_conversation(charge.get("title"), space=charge.get("space") or None)
    ), 201


@api_chat_bp.patch("/conversations/<int:conversation_id>")
def update_conversation(conversation_id):
    """Renomme ou archive une conversation."""
    charge = request.get_json(silent=True) or {}
    conversation = chat_model.update_conversation(
        conversation_id,
        title=charge.get("title"),
        archived=charge.get("archived"),
    )
    if conversation is None:
        return jsonify({"error": "Conversation introuvable."}), 404
    return jsonify(conversation)


@api_chat_bp.delete("/conversations/<int:conversation_id>")
def delete_conversation(conversation_id):
    """Supprime une conversation et tout son fil."""
    if not chat_model.delete_conversation(conversation_id):
        return jsonify({"error": "Conversation introuvable."}), 404
    return jsonify({"deleted": conversation_id})
