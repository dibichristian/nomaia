"""Espaces de travail pro et perso, sur le modele du multi-societe d'Odoo.

Chaque donnee metier appartient a un espace. Une requete voit un perimetre
(le « scope »), fixe une fois pour toutes :

- session Claude : l'espace de sa cle (controllers/security.py, g.agent_space) ;
- interface : le selecteur Tout / Pro / Perso de la barre du haut (reglage
  space_filter) ; « Tout » montre les deux espaces ;
- hors requete (worker de tri, battement, Telegram) : tout. Ces traitements
  fixent eux-memes l'espace de ce qu'ils creent.

Contacts et notes techniques acceptent en plus « commun » : visibles dans les
deux espaces, comme une fiche Odoo sans societe.

Convention des parametres `space` des fonctions de liste :
    None      -> perimetre de la requete en cours (scope())
    'pro'...  -> cet espace
    ALL       -> tous les espaces, quel que soit le perimetre (plan du jour :
                 le temps de l'utilisateur est le meme pour les deux espaces)
"""
from flask import g, has_request_context, request

SPACES = ("pro", "perso")
COMMON = "commun"
SHARED_SPACES = SPACES + (COMMON,)
DEFAULT = "pro"
ALL = "all"

LABELS = {"pro": "Pro", "perso": "Perso", COMMON: "Commun"}


class SpaceError(ValueError):
    """Espace inconnu ou incoherent."""


def scope():
    """Espace visible par la requete en cours : 'pro', 'perso', ou None (tous)."""
    if not has_request_context():
        return None
    # Memorise sur la requete, pas sur g : g appartient au contexte applicatif,
    # qui peut survivre a plusieurs requetes (tests, contexte deja pousse).
    cache = request.environ
    if "nomaia.space_scope" not in cache:
        from models import setting  # import tardif : setting importe models

        agent = g.get("agent_space") if request.path.startswith("/api/agent") else None
        cache["nomaia.space_scope"] = agent or setting.space_filter()
    return cache["nomaia.space_scope"]


def resolve(space=None):
    """Traduit un parametre `space` de fonction de liste en filtre effectif.

    :return: 'pro', 'perso' ou None (aucun filtre)
    """
    if space == ALL:
        return None
    if space in SPACES:
        return space
    return scope()


def clause(column, space=None, shared=False):
    """Fragment SQL de filtre par espace, a ajouter apres un WHERE.

    :param column: colonne qualifiee (ex. 't.space')
    :param shared: la table accepte « commun », visible de partout
    :return: (sql commencant par ' AND ', liste de parametres), vides si aucun filtre
    """
    espace = resolve(space)
    if espace is None:
        return "", []
    if shared:
        return f" AND {column} IN (?, '{COMMON}')", [espace]
    return f" AND {column} = ?", [espace]


def clean(value, shared=False):
    """Valide un espace saisi ; leve SpaceError s'il est inconnu."""
    valeur = (value or "").strip().lower()
    autorises = SHARED_SPACES if shared else SPACES
    if valeur not in autorises:
        raise SpaceError("Espace invalide : " + ", ".join(autorises) + ".")
    return valeur


def for_new(explicit=None, shared=False):
    """Espace d'un enregistrement cree sans espace impose par un parent.

    Ordre : valeur explicite, perimetre de la requete, espace selectionne dans
    l'interface (Telegram, mail : decision du 2026-11-13), puis, en mode « Tout »,
    l'espace de creation choisi a cote du selecteur (comme la societe courante
    d'Odoo quand plusieurs societes sont cochees).
    """
    if explicit:
        return clean(explicit, shared)
    espace = scope()
    if espace:
        return espace
    from models import setting

    return setting.space_filter() or setting.create_space()


def for_record(project_id=None, explicit=None, shared=False, task_id=None):
    """Espace d'un enregistrement rattache (ou non) a un projet ou a une tache.

    Un rattachement impose son espace (le projet prime sur la tache) ; sans
    rattachement, voir for_new.
    """
    from models import get_db

    db = get_db()
    if project_id not in (None, "", "none"):
        row = db.execute("SELECT space FROM project WHERE id = ?", (int(project_id),)).fetchone()
        if row:
            return row["space"]
    if task_id not in (None, "", "none"):
        row = db.execute("SELECT space FROM task WHERE id = ?", (int(task_id),)).fetchone()
        if row:
            return row["space"]
    return for_new(explicit, shared)


def cascade_task(db, task_id, value):
    """Fait suivre a une tache qui change d'espace son journal et ses attentes."""
    db.execute("UPDATE note SET space = ? WHERE task_id = ?", (value, task_id))
    db.execute("UPDATE waiting SET space = ? WHERE task_id = ?", (value, task_id))


def cascade_project(db, project_id, value):
    """Fait suivre a un projet qui change d'espace tout ce qui lui est rattache.

    Les taches sont deja deplacees par models/project.update_project ; restent
    leur journal et leurs attentes, et ce qui porte directement le projet.
    Les contacts ne bougent pas : un contact peut servir ailleurs.
    """
    taches = "(SELECT id FROM task WHERE project_id = ?)"
    db.execute(f"UPDATE note SET space = ? WHERE project_id = ? OR task_id IN {taches}",
               (value, project_id, project_id))
    db.execute(f"UPDATE waiting SET space = ? WHERE task_id IN {taches}", (value, project_id))
    for table in ("event", "minutes", "kb_note"):
        db.execute(f"UPDATE {table} SET space = ? WHERE project_id = ?", (value, project_id))


def visible(value, space=None):
    """Indique si un enregistrement d'espace `value` est visible du perimetre."""
    espace = resolve(space)
    return espace is None or value in (espace, COMMON)
