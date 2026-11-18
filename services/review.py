"""Bilan de periode : agrege les chiffres, puis les fait commenter par l'IA."""
import json
from datetime import date, timedelta

from models import get_db, journal, stats
from models import space as space_model
from models import task as task_model
from services import llm, project_board
from services import day_close

OUTPUT_SCHEMA = """{
  "resume": "deux phrases sur la periode",
  "points_forts": ["constat appuye sur un chiffre"],
  "points_faibles": ["constat appuye sur un chiffre"],
  "recommandations": [
    {"action": "action concrete et realisable", "pourquoi": "le chiffre qui la justifie"}
  ],
  "focus_semaine": "la seule chose a ne pas rater sur la periode suivante"
}"""

# Journal des sessions Claude : tout est affiche sur la page (dans la limite
# de WORK_LOG_LIMIT), mais le prompt n'en garde que le debut pour rester court.
WORK_LOG_LIMIT = 200
WORK_LOG_PROMPT_LIMIT = 40

INSTRUCTIONS = """Tu es NOMAIA, l'assistant de l'utilisateur, developpeur Odoo.

Analyse les chiffres de la periode ci-dessous et produis un bilan court, concret et honnete.

REGLES
1. Appuie chaque constat sur un chiffre reel des donnees fournies. Aucune generalite.
2. Si le temps suivi est tres inferieur au temps estime, c'est que le chronometre n'a pas ete utilise : dis-le au lieu d'en deduire une performance.
   Le temps declare par les sessions Claude est du temps reel, deja compte dans le temps suivi : son journal dit ce qui a ete fait.
3. La section PROJETS donne l'etat actuel de chaque projet et ce qui a bouge sur la periode : etapes franchies, anomalies, livrables, registre. Signale un projet a risque ou immobile.
4. Trois recommandations maximum, actionnables des le prochain jour ouvre. Pas de conseil de developpement personnel.
5. Si les donnees sont trop maigres pour conclure, dis-le franchement dans le resume.
6. Ton direct, sans flatterie.

FORMAT DE SORTIE
Reponds UNIQUEMENT par un objet JSON valide, sans texte autour, conforme a ce modele :
"""


def period_bounds(reference=None, weeks_back=0):
    """Retourne le lundi et le dimanche de la semaine visee.

    :param reference: date de reference (defaut : aujourd'hui)
    :param weeks_back: 0 pour la semaine en cours, 1 pour la precedente
    """
    reference = reference or date.today()
    monday = reference - timedelta(days=reference.weekday() + 7 * weeks_back)
    return monday, monday + timedelta(days=6)


def month_bounds(reference=None, months_back=0):
    """Retourne le premier et le dernier jour du mois vise.

    :param reference: date de reference (defaut : aujourd'hui)
    :param months_back: 0 pour le mois en cours, 1 pour le precedent
    """
    reference = reference or date.today()
    index = reference.year * 12 + reference.month - 1 - months_back
    first = date(index // 12, index % 12 + 1, 1)
    following = date((index + 1) // 12, (index + 1) % 12 + 1, 1)
    return first, following - timedelta(days=1)


def projects_view(start_date, end_date):
    """Projets non termines ou ayant bouge sur la periode : etat actuel et mouvements.

    L'etat (avancement, etape, anomalies ouvertes...) est celui d'aujourd'hui :
    aucun historique d'avancement n'est conserve. Les mouvements, eux, sont
    ceux de la periode.

    :return: liste de projets compacts, les plus actifs d'abord
    """
    activite = stats.project_activity(start_date.isoformat(), end_date.isoformat())
    projets = []
    for carte in project_board.overview()["projects"]:
        mouvements = activite.get(carte["id"])
        if carte["status"] != "active" and not mouvements:
            continue
        etape = carte["current_phase"]
        jalon = carte["next_milestone"]
        projets.append(
            {
                "id": carte["id"],
                "name": carte["name"],
                "color": carte["color"],
                "status": carte["status"],
                "progress_pct": carte["progress_pct"],
                "weather": carte["weather"],
                "current_phase": etape["name"] if etape else None,
                "next_milestone": (
                    {"name": jalon["name"], "date": jalon["date"], "days_left": jalon["days_left"]}
                    if jalon
                    else None
                ),
                "late_count": carte["late_count"],
                "blocked_count": carte["blocked_count"],
                "anomalies_open": carte["anomalies"]["open"],
                "anomalies_critical": carte["anomalies"]["critical"],
                "deliverables_awaited": carte["deliverables"]["awaited"],
                "signals": [signal["label"] for signal in carte["signals"]],
                "activity": mouvements or {},
            }
        )
    projets.sort(key=lambda projet: -sum(projet["activity"].values()))
    return projets


def gather(start_date, end_date):
    """Rassemble tous les indicateurs d'une periode."""
    start_iso, end_iso = start_date.isoformat(), end_date.isoformat()

    return {
        "period_start": start_iso,
        "period_end": end_iso,
        "summary": stats.period_summary(start_iso, end_iso),
        "done_by_day": stats.done_by_day(start_iso, end_iso),
        "done_by_priority": stats.done_by_priority(start_iso, end_iso),
        "tracked_by_project": stats.tracked_by_project(start_iso, end_iso),
        # Detail de la barre « Sans projet » : taches a rattacher depuis le bilan.
        "unassigned_tasks": stats.unassigned_tracked_tasks(start_iso, end_iso),
        "estimation": stats.estimation_gap(start_iso, end_iso),
        "calibration": stats.estimation_calibration(),
        "captures": stats.capture_breakdown(start_iso, end_iso),
        "done_tasks": stats.done_tasks(start_iso, end_iso),
        "agent": stats.agent_work(start_iso, end_iso),
        "work_log": journal.list_between(
            start_iso, end_iso, source="claude", limit=WORK_LOG_LIMIT
        ),
        "projects": projects_view(start_date, end_date),
        # Notes du soir (cloture de journee) : intentions du lendemain, a comparer au realise.
        "day_notes": day_close.notes_between(start_iso, end_iso),
    }


def build_prompt(data):
    """Assemble le prompt du bilan a partir des indicateurs."""
    summary = data["summary"]
    estimation = data["estimation"]

    lines = [
        f"PERIODE : du {data['period_start']} au {data['period_end']}",
        "",
        "CHIFFRES",
        f"- taches creees : {summary['created']}",
        f"- taches terminees : {summary['done']}",
        f"- entrees deposees (captures) : {summary['captures']}",
        f"  dont {data['captures']['with_tasks']} ayant produit des taches,"
        f" {data['captures']['waiting']} en attente de ta reponse,"
        f" {data['captures']['failed']} en echec",
        f"- temps estime des taches terminees : {summary['estimated_min']} min",
        f"- temps reellement chronometre : {summary['tracked_min']} min",
        f"- taches encore ouvertes : {summary['open_tasks']}",
        f"- taches en retard : {summary['late_tasks']}",
    ]

    if estimation["ratio"] is not None:
        lines.append(
            f"- sur {estimation['tasks']} tache(s) chronometrees :"
            f" {estimation['estimated_min']} min estimees contre"
            f" {estimation['tracked_min']} min reelles (ratio {estimation['ratio']})"
        )

    if data["done_by_day"]:
        detail = ", ".join(f"{row['day']} : {row['count']}" for row in data["done_by_day"])
        lines.append(f"- terminees par jour : {detail}")

    if data["done_by_priority"]:
        detail = ", ".join(
            f"P{row['priority']} : {row['count']}" for row in data["done_by_priority"]
        )
        lines.append(f"- terminees par priorite : {detail}")

    if data["tracked_by_project"]:
        detail = ", ".join(
            f"{row['project']} : {row['minutes']} min" for row in data["tracked_by_project"]
        )
        lines.append(f"- temps par projet : {detail}")

    if data["done_tasks"]:
        lines.append("")
        lines.append("TACHES TERMINEES")
        for item in data["done_tasks"]:
            lines.append(
                f"- [P{item['priority']}] {item['title']}"
                f" (estime {item['estimate_min']} min, suivi {item['tracked_min']} min)"
            )

    if data.get("projects"):
        lines.append("")
        lines.append("PROJETS (etat actuel, puis mouvements de la periode)")
        for projet in data["projects"]:
            lines.append(_project_line(projet))

    agent = data.get("agent") or {}
    if agent.get("entries"):
        lines.append("")
        lines.append(
            "TRAVAIL DECLARE PAR LES SESSIONS CLAUDE"
            f" ({agent['minutes']} min, {agent['tasks_worked']} tache(s) suivie(s),"
            f" {agent['tasks_created']} creee(s), {agent['tasks_done']} terminee(s))"
        )
        for entree in (data.get("work_log") or [])[:WORK_LOG_PROMPT_LIMIT]:
            duree = f" ({entree['minutes']} min)" if entree.get("minutes") else ""
            lines.append(
                f"- {entree['created_at'][:10]} {entree.get('task_title') or 'projet'} :"
                f" {entree['content'][:160]}{duree}"
            )

    if data.get("day_notes"):
        lines.append("")
        lines.append("NOTES DU SOIR (ce qui etait prevu pour le lendemain)")
        for note in data["day_notes"]:
            lines.append(f"- {note['day']} : {note['tomorrow'][:200]}")

    open_tasks = task_model.list_tasks(status="todo")[:20]
    if open_tasks:
        lines.append("")
        lines.append("TACHES ENCORE OUVERTES")
        for item in open_tasks:
            deadline = item["deadline"] or "sans echeance"
            lines.append(f"- [P{item['priority']}] {item['title']} ({deadline})")

    return "\n".join(lines)


def _project_line(projet):
    """Une ligne de prompt par projet : etat, alertes, mouvements."""
    morceaux = [f"{projet['progress_pct']} % fait"]
    if projet["status"] != "active":
        morceaux.append(f"statut {projet['status']}")
    if projet["current_phase"]:
        morceaux.append(f"etape en cours : {projet['current_phase']}")
    jalon = projet["next_milestone"]
    if jalon and jalon["date"]:
        morceaux.append(f"prochain jalon {jalon['name']} le {jalon['date']}")
    if projet["anomalies_open"]:
        morceaux.append(
            f"{projet['anomalies_open']} anomalie(s) ouverte(s)"
            f" dont {projet['anomalies_critical']} critique(s)"
        )
    if projet["deliverables_awaited"]:
        morceaux.append(f"{projet['deliverables_awaited']} livrable(s) client attendu(s)")
    if projet["signals"]:
        morceaux.append("alertes : " + " ; ".join(projet["signals"]))

    activite = projet["activity"]
    mouvements = [
        f"{activite[cle]} {libelle}"
        for cle, libelle in (
            ("tasks_done", "tache(s) terminee(s)"),
            ("phases_done", "etape(s) terminee(s)"),
            ("anomalies_opened", "anomalie(s) remontee(s)"),
            ("anomalies_fixed", "anomalie(s) corrigee(s)"),
            ("deliverables_received", "livrable(s) recu(s)"),
            ("register_entries", "entree(s) au registre"),
        )
        if activite.get(cle)
    ]
    morceaux.append(
        "periode : " + (", ".join(mouvements) if mouvements else "aucun mouvement")
    )
    return f"- {projet['name']} : " + ", ".join(morceaux)


def _normalize(payload):
    """Valide la sortie du modele et comble les champs manquants."""
    if not isinstance(payload, dict):
        raise llm.LLMError("Le moteur n'a pas renvoye un objet JSON.")

    def text_list(key):
        return [str(item).strip() for item in (payload.get(key) or []) if str(item).strip()][:5]

    recommendations = []
    for item in payload.get("recommandations") or []:
        if isinstance(item, dict) and (item.get("action") or "").strip():
            recommendations.append(
                {
                    "action": item["action"].strip(),
                    "pourquoi": (item.get("pourquoi") or "").strip(),
                }
            )

    return {
        "resume": (payload.get("resume") or "").strip() or "Bilan sans resume.",
        "points_forts": text_list("points_forts"),
        "points_faibles": text_list("points_faibles"),
        "recommandations": recommendations[:3],
        "focus_semaine": (payload.get("focus_semaine") or "").strip(),
    }


def generate(start_date=None, end_date=None):
    """Produit le bilan d'une periode et l'enregistre.

    :return: dictionnaire {id, period_start, period_end, data, payload}
    """
    if start_date is None or end_date is None:
        start_date, end_date = period_bounds()

    data = gather(start_date, end_date)
    payload = _normalize(
        llm.ask(
            build_prompt(data),
            system=INSTRUCTIONS + OUTPUT_SCHEMA,
            purpose="bilan",
            max_tokens=1500,
        )
    )
    # Le bilan enregistre garde le journal tel que le modele l'a lu (40 entrees,
    # comme avant) : le journal complet se relit sur la page, pas dans l'historique.
    record = {
        "data": {**data, "work_log": (data.get("work_log") or [])[:WORK_LOG_PROMPT_LIMIT]},
        "payload": payload,
    }

    db = get_db()
    cursor = db.execute(
        "INSERT INTO review (period_start, period_end, payload_json, space) VALUES (?, ?, ?, ?)",
        (
            start_date.isoformat(),
            end_date.isoformat(),
            json.dumps(record, ensure_ascii=False),
            _scope_key(),
        ),
    )
    db.commit()

    return _row_to_dict(
        get_db().execute("SELECT * FROM review WHERE id = ?", (cursor.lastrowid,)).fetchone()
    )


def _scope_key():
    """Perimetre d'un bilan enregistre : 'pro', 'perso' ou 'all'.

    Les chiffres du bilan suivent le perimetre de la requete (models/stats.py) :
    un bilan calcule en Perso n'est relu qu'en Perso.
    """
    return space_model.scope() or space_model.ALL


def latest():
    """Retourne le dernier bilan enregistre, ou None."""
    row = get_db().execute(
        "SELECT * FROM review WHERE space = ? ORDER BY id DESC LIMIT 1", (_scope_key(),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_for_period(start_date, end_date):
    """Retourne le dernier bilan genere pour exactement cette periode, ou None."""
    row = get_db().execute(
        "SELECT * FROM review WHERE period_start = ? AND period_end = ? AND space = ?"
        " ORDER BY id DESC LIMIT 1",
        (start_date.isoformat(), end_date.isoformat(), _scope_key()),
    ).fetchone()
    return _row_to_dict(row) if row else None


def get_review(review_id):
    """Retourne un bilan par son identifiant, ou None."""
    row = get_db().execute("SELECT * FROM review WHERE id = ?", (review_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_reviews(limit=40):
    """Historique des bilans, le plus recent d'abord, sans les chiffres detailles.

    :return: liste de {id, period_start, period_end, created_at, resume}
    """
    rows = get_db().execute(
        "SELECT * FROM review WHERE space = ? ORDER BY id DESC LIMIT ?", (_scope_key(), limit)
    ).fetchall()
    historique = []
    for row in rows:
        review = _row_to_dict(row)
        historique.append(
            {
                "id": review["id"],
                "period_start": review["period_start"],
                "period_end": review["period_end"],
                "created_at": review["created_at"],
                "resume": review["payload"].get("resume", ""),
            }
        )
    return historique


def as_text(review):
    """Met un bilan en texte simple, pour Telegram."""
    payload = review["payload"]
    lines = [
        f"Bilan du {review['period_start']} au {review['period_end']}",
        "",
        payload["resume"],
    ]

    if payload["points_forts"]:
        lines += ["", "Points forts :"] + [f"+ {item}" for item in payload["points_forts"]]
    if payload["points_faibles"]:
        lines += ["", "Points faibles :"] + [f"- {item}" for item in payload["points_faibles"]]
    if payload["recommandations"]:
        lines += ["", "Recommandations :"] + [
            f"{index}. {item['action']}" + (f" ({item['pourquoi']})" if item["pourquoi"] else "")
            for index, item in enumerate(payload["recommandations"], 1)
        ]
    if payload["focus_semaine"]:
        lines += ["", f"Focus : {payload['focus_semaine']}"]

    return "\n".join(lines)


def _row_to_dict(row):
    """Convertit une ligne review en dictionnaire exploitable."""
    data = dict(row)
    record = json.loads(data.pop("payload_json"))
    data["payload"] = record.get("payload", {})
    data["data"] = record.get("data", {})
    return data
