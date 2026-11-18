"""Index de recherche plein texte (SQLite FTS5) sur les donnees de NOMAIA.

Une seule table virtuelle, `search_fts`, indexe titre et corps de chaque
element cherchable. Son rowid encode la nature et l'identifiant de l'element
(nature * 1e9 + id) : supprimer ou remplacer une entree se fait par rowid,
sans parcourir l'index.

L'index est tenu a jour par des triggers SQL, donc toujours synchrone avec
les tables, quel que soit le code qui ecrit. Ajouter une source (notes de
connaissance, comptes rendus...) : la declarer dans SOURCES et incrementer
INDEX_VERSION ; l'index est alors reconstruit au demarrage suivant.

Tokeniseur unicode61 sans diacritiques : « reunion » trouve « Réunion ».
Prefixes indexes sur 2 et 3 caracteres : la recherche se fait a la frappe.
"""
import re

from models import get_db

INDEX_VERSION = 3
KIND_FACTOR = 1_000_000_000

# Nature -> (code, table, expression du titre, expression du corps, colonnes surveillees).
# Les expressions sont du SQL sur la ligne (prefixe new./old. ajoute par les triggers).
SOURCES = {
    "tache": (1, "task", "{r}.title",
              "coalesce({r}.description, '') || ' ' || coalesce({r}.module, '')",
              ("title", "description", "module")),
    "projet": (2, "project", "{r}.name", "coalesce({r}.weather_note, '')", ("name", "weather_note")),
    "journal": (3, "note", "''", "{r}.content", ("content",)),
    "contact": (4, "person", "{r}.name",
                "coalesce({r}.role, '') || ' ' || coalesce({r}.email, '') || ' ' || coalesce({r}.aliases, '')"
                " || ' ' || coalesce({r}.notes, '')",
                ("name", "role", "email", "aliases", "notes")),
    "evenement": (5, "event", "{r}.title",
                  "coalesce({r}.location, '') || ' ' || coalesce({r}.notes, '')",
                  ("title", "location", "notes")),
    "cr": (6, "minutes", "{r}.title",
           "{r}.attendees || ' ' || {r}.objectives || ' ' || {r}.discussed || ' ' || {r}.topics"
           " || ' ' || {r}.followups || ' ' || {r}.notes",
           ("title", "attendees", "objectives", "discussed", "topics", "followups", "notes")),
    "cr_element": (7, "minutes_item", "''", "{r}.text || ' ' || {r}.owner", ("text", "owner")),
    "note": (8, "kb_note", "{r}.title",
             "{r}.content || ' ' || {r}.module || ' ' || replace({r}.tags, ',', ' ') || ' ' || {r}.odoo_version",
             ("title", "content", "module", "tags", "odoo_version")),
}
CODES = {code: kind for kind, (code, *_rest) in SOURCES.items()}


def ensure_index(conn):
    """Cree l'index et ses triggers ; reconstruit l'index si sa version a change.

    Appelee par init_schema, apres les migrations : les triggers lisent des
    colonnes ajoutees par migration (task.module, person.aliases...).
    """
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5("
        "title, body, tokenize = 'unicode61 remove_diacritics 2', prefix = '2 3')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS search_meta (version INTEGER NOT NULL)")
    version = conn.execute("SELECT version FROM search_meta").fetchone()
    if version is None or version[0] != INDEX_VERSION:
        _drop_triggers(conn)
        _rebuild(conn)
        conn.execute("DELETE FROM search_meta")
        conn.execute("INSERT INTO search_meta (version) VALUES (?)", (INDEX_VERSION,))
    for kind in SOURCES:
        _create_triggers(conn, kind)


def _create_triggers(conn, kind):
    code, table, title, body, watched = SOURCES[kind]
    base = code * KIND_FACTOR
    insert = (
        f"INSERT INTO search_fts (rowid, title, body) "
        f"VALUES ({base} + new.id, {title.format(r='new')}, {body.format(r='new')});"
    )
    delete = f"DELETE FROM search_fts WHERE rowid = {base} + old.id;"
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS search_{kind}_ai AFTER INSERT ON {table} BEGIN {insert} END")
    conn.execute(
        f"CREATE TRIGGER IF NOT EXISTS search_{kind}_au AFTER UPDATE OF {', '.join(watched)} ON {table} "
        f"BEGIN {delete} {insert} END"
    )
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS search_{kind}_ad AFTER DELETE ON {table} BEGIN {delete} END")


def _drop_triggers(conn):
    for kind in SOURCES:
        for suffix in ("ai", "au", "ad"):
            conn.execute(f"DROP TRIGGER IF EXISTS search_{kind}_{suffix}")


def _rebuild(conn):
    """Vide puis remplit l'index depuis toutes les sources."""
    conn.execute("DELETE FROM search_fts")
    for code, table, title, body, _watched in SOURCES.values():
        conn.execute(
            f"INSERT INTO search_fts (rowid, title, body) "
            f"SELECT {code * KIND_FACTOR} + r.id, {title.format(r='r')}, {body.format(r='r')} FROM {table} r"
        )


def fts_query(text):
    """Requete FTS5 sure a partir d'une saisie libre : chaque mot devient un prefixe, tous requis.

    Ponctuation et operateurs FTS5 sont retires : la saisie ne peut pas
    produire une erreur de syntaxe ni detourner la requete.
    """
    words = re.findall(r"\w+", text or "", flags=re.UNICODE)
    return " ".join(f'"{word}"*' for word in words[:8])


def search(text, limit=30, kinds=None):
    """Elements correspondant a la saisie, les plus pertinents d'abord.

    :param text: saisie libre (deux caracteres au moins)
    :param limit: nombre maximal de resultats
    :param kinds: natures retenues (toutes par defaut)
    :return: liste de {"kind", "id", "title", "snippet", "rank"}
    """
    query = fts_query(text)
    if len(query) < 5:  # au moins deux caracteres utiles : « "xx"* »
        return []
    sql = (
        "SELECT rowid, title, snippet(search_fts, 1, '[', ']', '…', 12) AS snippet,"
        " bm25(search_fts, 8.0, 1.0) AS rank FROM search_fts WHERE search_fts MATCH ?"
    )
    params = [query]
    if kinds:
        ranges = " OR ".join("rowid BETWEEN ? AND ?" for _ in kinds)
        sql += f" AND ({ranges})"
        for kind in kinds:
            base = SOURCES[kind][0] * KIND_FACTOR
            params += [base, base + KIND_FACTOR - 1]
    sql += " ORDER BY rank LIMIT ?"
    params.append(int(limit))
    rows = get_db().execute(sql, params).fetchall()
    return [
        {
            "kind": CODES[row["rowid"] // KIND_FACTOR],
            "id": row["rowid"] % KIND_FACTOR,
            "title": row["title"],
            "snippet": row["snippet"],
            "rank": row["rank"],
        }
        for row in rows
        if row["rowid"] // KIND_FACTOR in CODES
    ]
