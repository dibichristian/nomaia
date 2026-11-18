"""Conversations avec NOMAIA : plusieurs fils separes, chacun avec son historique.

Chaque conversation a son propre fil et son propre plan en attente : on peut
revenir sur un sujet sans le melanger aux autres, et seuls les derniers tours
de la conversation en cours repartent au modele, ce qui plafonne le cout en
jetons. Les conversations ne se voient pas d'office : NOMAIA en consulte une
autre a la demande (outils search_conversations et read_conversation).

Sans identifiant, les fonctions travaillent sur la conversation courante : la
derniere active, creee au besoin. C'est ce qui garde le comportement d'un fil
unique pour les appelants qui n'en precisent pas.
"""
import json
from datetime import datetime

from models import get_db
from models import space as space_model

# Les roles acceptes en base. Les resultats d'outils ne sont pas stockes :
# ils sont reconstruits a chaque tour a partir de donnees fraiches, et les
# figer donnerait a NOMAIA une vision perimee de la base.
VALID_ROLES = ("user", "assistant")

# Longueur maximale d'un message enregistre. Au-dela, le contenu est tronque :
# un collage de plusieurs pages n'a rien a faire dans un fil de conversation,
# il releve du depot de capture.
MAX_CONTENT_CHARS = 8000

# Titre automatique : le debut du premier message, sans appel au modele.
TITLE_MAX_CHARS = 60


class ConversationError(ValueError):
    """Conversation introuvable ou donnee refusee."""


def _now():
    """Horodatage local a la seconde, format SQLite."""
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


# Rang d'activite suivant : la conversation touchee en dernier a le plus grand.
_NEXT_ACTIVITY = "(SELECT COALESCE(MAX(activity), 0) + 1 FROM conversation)"


def _json_list(value):
    """Liste JSON stockee, ou liste vide si absente ou illisible."""
    if not value:
        return []
    try:
        data = json.loads(value)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _to_dict(row):
    """Convertit une ligne de chat_message en dictionnaire serialisable."""
    tools = _json_list(row["tools_json"])

    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "tools": tools,
        "sources": _json_list(row["sources_json"]),
        "created_at": row["created_at"],
    }


# --- Conversations -----------------------------------------------------------


def _conversation_dict(row):
    """Convertit une ligne de conversation (avec ses compteurs) en dictionnaire."""
    cles = row.keys()
    return {
        "id": row["id"],
        "title": row["title"] or "Nouvelle conversation",
        "archived": bool(row["archived"]),
        "space": row["space"] if "space" in cles else space_model.DEFAULT,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"] if "message_count" in cles else 0,
        "last_message": row["last_message"] if "last_message" in cles else None,
    }


def _clean_title(title):
    """Normalise un titre de conversation ; None s'il est vide."""
    texte = " ".join(str(title or "").split())
    return texte[:120] or None


def create_conversation(title=None, space=None):
    """Ouvre une nouvelle conversation et la retourne.

    :param space: espace de la conversation ; par defaut celui du perimetre.
        Le chat ne voit que les donnees de cet espace quand le selecteur en
        impose un (perimetre de la requete, models/space.py).
    """
    db = get_db()
    curseur = db.execute(
        "INSERT INTO conversation (title, updated_at, space, activity)"
        f" VALUES (?, ?, ?, {_NEXT_ACTIVITY})",
        (_clean_title(title), _now(), space_model.for_new(space)),
    )
    db.commit()
    return get_conversation(curseur.lastrowid)


def get_conversation(conversation_id):
    """Retourne une conversation, ou None."""
    row = get_db().execute(
        "SELECT c.*,"
        " (SELECT COUNT(*) FROM chat_message m WHERE m.conversation_id = c.id) AS message_count,"
        " NULL AS last_message"
        " FROM conversation c WHERE c.id = ?",
        (conversation_id,),
    ).fetchone()
    return _conversation_dict(row) if row else None


def list_conversations(include_archived=False, limit=50, space=None):
    """Conversations, la plus recemment active d'abord, avec leur dernier message."""
    filtre, valeurs = space_model.clause("c.space", space)
    clause = "WHERE 1 = 1" + ("" if include_archived else " AND c.archived = 0") + filtre
    rows = get_db().execute(
        "SELECT c.*,"
        " (SELECT COUNT(*) FROM chat_message m WHERE m.conversation_id = c.id) AS message_count,"
        " (SELECT substr(m.content, 1, 90) FROM chat_message m"
        "   WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_message"
        f" FROM conversation c {clause}"
        " ORDER BY c.activity DESC, c.id DESC LIMIT ?",
        (*valeurs, limit),
    ).fetchall()
    return [_conversation_dict(row) for row in rows]


def current_id():
    """Identifiant de la conversation courante : la derniere active de l'espace
    visible, creee au besoin."""
    filtre, valeurs = space_model.clause("space")
    row = get_db().execute(
        "SELECT id FROM conversation WHERE archived = 0" + filtre
        + " ORDER BY activity DESC, id DESC LIMIT 1",
        valeurs,
    ).fetchone()
    return row["id"] if row else create_conversation()["id"]


def resolve(conversation_id=None):
    """Retourne un identifiant de conversation valide.

    :param conversation_id: conversation demandee ; None pour la courante
    :raises ConversationError: conversation demandee introuvable
    """
    if conversation_id in (None, ""):
        return current_id()
    try:
        identifiant = int(conversation_id)
    except (TypeError, ValueError):
        raise ConversationError("Identifiant de conversation invalide.")
    if get_conversation(identifiant) is None:
        raise ConversationError(f"Conversation {identifiant} introuvable.")
    return identifiant


def update_conversation(conversation_id, title=None, archived=None):
    """Renomme ou archive une conversation. Retourne la version a jour, ou None."""
    if get_conversation(conversation_id) is None:
        return None
    db = get_db()
    if title is not None:
        db.execute(
            "UPDATE conversation SET title = ? WHERE id = ?", (_clean_title(title), conversation_id)
        )
    if archived is not None:
        db.execute(
            "UPDATE conversation SET archived = ? WHERE id = ?",
            (1 if archived else 0, conversation_id),
        )
    db.commit()
    return get_conversation(conversation_id)


def delete_conversation(conversation_id):
    """Supprime une conversation, ses messages et ses plans. False si introuvable."""
    db = get_db()
    db.execute("DELETE FROM chat_plan WHERE conversation_id = ?", (conversation_id,))
    db.execute("DELETE FROM chat_message WHERE conversation_id = ?", (conversation_id,))
    curseur = db.execute("DELETE FROM conversation WHERE id = ?", (conversation_id,))
    db.commit()
    return curseur.rowcount > 0


def search(query, limit=8, exclude_id=None):
    """Retrouve les conversations dont un message contient tous les mots cherches.

    :return: liste de {'conversation_id', 'title', 'updated_at', 'excerpts'}
    """
    mots = [mot for mot in str(query or "").split() if len(mot) >= 2]
    if not mots:
        return []

    clauses = " AND ".join("m.content LIKE ? ESCAPE '!'" for _ in mots)
    motifs = []
    for mot in mots:
        for caractere in ("!", "%", "_"):
            mot = mot.replace(caractere, "!" + caractere)
        motifs.append("%" + mot + "%")

    exclusion = "AND c.id != ?" if exclude_id else ""
    filtre, valeurs = space_model.clause("c.space")
    exclusion += filtre
    parametres = motifs + ([exclude_id] if exclude_id else []) + valeurs
    rows = get_db().execute(
        "SELECT c.id, c.title, c.updated_at, m.role, substr(m.content, 1, 240) AS extrait"
        " FROM chat_message m JOIN conversation c ON c.id = m.conversation_id"
        f" WHERE {clauses} {exclusion}"
        " ORDER BY c.activity DESC, m.id DESC LIMIT 60",
        parametres,
    ).fetchall()

    trouvees = {}
    for row in rows:
        entree = trouvees.setdefault(
            row["id"],
            {
                "conversation_id": row["id"],
                "title": row["title"] or "Nouvelle conversation",
                "updated_at": row["updated_at"],
                "excerpts": [],
            },
        )
        if len(entree["excerpts"]) < 3:
            entree["excerpts"].append(f"{row['role']} : {row['extrait']}")
    return list(trouvees.values())[:limit]


# --- Messages ------------------------------------------------------------------


def add(role, content, tools=None, conversation_id=None, sources=None):
    """Enregistre un message et retourne son identifiant.

    Le premier message de l'utilisateur donne son titre a une conversation qui
    n'en a pas : le debut du message, sans appel au modele.

    :param role: 'user' ou 'assistant'
    :param content: texte du message, tronque au-dela de MAX_CONTENT_CHARS
    :param tools: noms des outils appeles pendant ce tour
    :param sources: elements cites par la reponse (services/chat_sources.py)
    :param conversation_id: conversation visee (defaut : la courante)
    :raises ValueError: role inconnu
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Role de message inconnu : {role}")

    identifiant = resolve(conversation_id)
    texte = (content or "").strip()[:MAX_CONTENT_CHARS]
    outils = json.dumps(tools, ensure_ascii=False) if tools else None
    liens = json.dumps(sources, ensure_ascii=False) if sources else None

    db = get_db()
    curseur = db.execute(
        "INSERT INTO chat_message (role, content, tools_json, sources_json, conversation_id) VALUES (?, ?, ?, ?, ?)",
        (role, texte, outils, liens, identifiant),
    )
    db.execute(
        f"UPDATE conversation SET updated_at = ?, activity = {_NEXT_ACTIVITY} WHERE id = ?",
        (_now(), identifiant),
    )
    if role == "user":
        titre = " ".join(texte.split())
        if len(titre) > TITLE_MAX_CHARS:
            titre = titre[: TITLE_MAX_CHARS - 1].rstrip() + "…"
        db.execute(
            "UPDATE conversation SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
            (titre or None, identifiant),
        )
    db.commit()
    return curseur.lastrowid


def history(limit=50, conversation_id=None):
    """Retourne les derniers messages d'une conversation, du plus ancien au plus recent.

    :param limit: nombre de messages remontes
    :param conversation_id: conversation visee (defaut : la courante)
    """
    rows = get_db().execute(
        "SELECT * FROM chat_message WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (resolve(conversation_id), limit),
    ).fetchall()
    return [_to_dict(row) for row in reversed(rows)]


def context(turns, conversation_id=None):
    """Retourne les derniers echanges au format attendu par les fournisseurs.

    Un tour vaut deux messages (la question et la reponse) : le plafond est
    donc double avant lecture. Les etiquettes d'outils sont laissees de cote,
    elles n'ont de sens que pour l'affichage.

    :param turns: nombre de tours de dialogue rappeles au modele
    :param conversation_id: conversation visee (defaut : la courante)
    """
    messages = history(limit=max(0, turns) * 2, conversation_id=conversation_id)
    return [{"role": item["role"], "content": item["content"]} for item in messages]


def clear(conversation_id=None):
    """Vide le fil d'une conversation. Retourne le nombre de messages supprimes."""
    db = get_db()
    curseur = db.execute(
        "DELETE FROM chat_message WHERE conversation_id = ?", (resolve(conversation_id),)
    )
    db.commit()
    return curseur.rowcount


# --- Plan en attente de confirmation ---------------------------------------

# Peremption d'un plan non confirme. Au-dela, l'etat de la base a pu changer
# et executer un plan vieux de plusieurs heures agirait sur des donnees qui ne
# sont plus celles que NOMAIA avait lues.
PLAN_MAX_AGE_MIN = 30


def set_plan(steps, reply="", conversation_id=None):
    """Enregistre un plan d'actions en attente. Retourne le plan enregistre.

    Un seul plan vit a la fois par conversation : proposer un nouveau plan
    abandonne le precedent, comme une question qui en remplace une autre. Un
    « oui » tape dans une autre conversation ne l'applique donc jamais.
    """
    identifiant = resolve(conversation_id)
    db = get_db()
    db.execute(
        "UPDATE chat_plan SET status = 'dropped' WHERE status = 'pending' AND conversation_id = ?",
        (identifiant,),
    )
    curseur = db.execute(
        "INSERT INTO chat_plan (steps_json, reply, conversation_id) VALUES (?, ?, ?)",
        (json.dumps(steps, ensure_ascii=False), (reply or "").strip(), identifiant),
    )
    db.commit()
    return get_plan(curseur.lastrowid)


def get_plan(plan_id=None, conversation_id=None):
    """Retourne le plan en attente, ou None.

    :param plan_id: plan precis ; sans lui, le plan en attente le plus recent
        de la conversation
    :param conversation_id: conversation visee (defaut : la courante)
    """
    if plan_id is None:
        row = get_db().execute(
            "SELECT * FROM chat_plan WHERE status = 'pending' AND conversation_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (resolve(conversation_id),),
        ).fetchone()
    else:
        row = get_db().execute("SELECT * FROM chat_plan WHERE id = ?", (plan_id,)).fetchone()

    if row is None:
        return None

    try:
        etapes = json.loads(row["steps_json"])
    except ValueError:
        etapes = []

    return {
        "id": row["id"],
        "steps": etapes,
        "reply": row["reply"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def pending_plan(now=None, conversation_id=None):
    """Retourne le plan en attente s'il est encore frais, sinon None.

    Un plan perime est marque comme abandonne plutot que laisse en suspens :
    sans cela, un « oui » tape le lendemain rejouerait des actions decidees
    sur un etat de la base qui n'existe plus.
    """
    plan = get_plan(conversation_id=conversation_id)
    if plan is None:
        return None

    try:
        pose = datetime.strptime(plan["created_at"][:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return plan

    age = ((now or datetime.now()) - pose).total_seconds() / 60
    if age > PLAN_MAX_AGE_MIN:
        close_plan(plan["id"], "expired")
        return None

    return plan


def close_plan(plan_id, status):
    """Ferme un plan : 'applied', 'dropped' ou 'expired'."""
    db = get_db()
    db.execute("UPDATE chat_plan SET status = ? WHERE id = ?", (status, plan_id))
    db.commit()
