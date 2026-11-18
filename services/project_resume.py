"""« Ou en etais-je ? » : synthese de reprise d'un projet.

Assemble, a partir de ce qui est enregistre (fiche projet, journal, registre,
attentes, agenda), ce qu'il faut savoir pour reprendre un projet en trente
secondes : derniere action, prochaine etape, ce qui bloque, decisions
recentes, et une chronologie. Aucune information n'est inventee : un bloc
vide reste vide.

Le resume redige par l'IA est facultatif et separe (summarize) : il ne
s'appuie que sur la synthese et cite ses sources par reference [T12], [J8]...
que l'interface rend cliquables.
"""
import json
import re
from datetime import date, datetime

from services import llm, project_board

TIMELINE_SIZE = 25
RECENT_DECISIONS = 5
CLAUDE_SESSIONS = 5

JOURNAL_KINDS = {
    "note": "Note", "progress": "Avancement", "work": "Travail", "decision": "Décision",
    "scope": "Périmètre", "dependency": "Dépendance",
}


def _days_since(stamp, today):
    """Jours ecoules depuis un horodatage AAAA-MM-JJ[ HH:MM:SS], ou None."""
    if not stamp:
        return None
    try:
        return (today - date.fromisoformat(str(stamp)[:10])).days
    except ValueError:
        return None


def _next_step(open_tasks, today_iso):
    """Prochaine tache a reprendre : deja entamee d'abord, puis la plus urgente, cote « nous »."""
    candidates = [
        t for t in open_tasks
        if not t["blocked"] and t["side"] not in ("client",) and t["kind"] != "deliverable"
    ]
    if not candidates:
        return None

    def key(task):
        return (
            0 if task["progress_pct"] else 1,                    # deja entamee
            0 if task["deadline"] and task["deadline"] <= today_iso else 1,
            task["deadline"] or "9999",
            task["priority"],
            task["position"],
        )

    return sorted(candidates, key=key)[0]


def _timeline(detail):
    """Evenements recents du projet, du plus recent au plus ancien."""
    items = []
    for entry in detail["journal"]:
        items.append({
            "at": entry["created_at"], "type": entry.get("kind") or "note",
            "label": JOURNAL_KINDS.get(entry.get("kind"), "Note"),
            "text": entry["content"], "task_id": entry.get("task_id"), "task_title": entry.get("task_title"),
            "source": entry.get("source"), "ref": f"J{entry['id']}", "minutes": entry.get("minutes"),
        })
    for task in detail["tasks"]["done"]:
        if task["done_at"]:
            items.append({
                "at": task["done_at"], "type": "done", "label": "Terminée", "text": task["title"],
                "task_id": task["id"], "task_title": task["title"], "source": None, "ref": f"T{task['id']}",
                "minutes": None,
            })
    items.sort(key=lambda item: item["at"] or "", reverse=True)
    return items[:TIMELINE_SIZE]


def resume(project_id, today=None):
    """Synthese de reprise du projet, ou None s'il n'existe pas.

    :return: {project, last_action, next_step, current_phase, next_milestone,
              blocked, anomalies, deliverables, waiting, decisions,
              claude_sessions, upcoming, timeline, idle_days}
    """
    today = today or date.today()
    detail = project_board.detail(project_id, today)
    if detail is None:
        return None
    today_iso = today.isoformat()
    card = detail["project"]
    tasks = detail["tasks"]
    open_tasks = tasks["late"] + tasks["open"]

    timeline = _timeline(detail)
    last = timeline[0] if timeline else None
    step = _next_step(open_tasks, today_iso)

    blocked = [
        t for t in open_tasks if t["blocked"] or t["side"] in ("client", "both")
    ]
    decisions = [entry for entry in detail["register"] if entry["kind"] == "decision"][:RECENT_DECISIONS]

    return {
        "project": {
            "id": card["id"], "name": card["name"], "color": card.get("color"), "status": card.get("status"),
            "progress_pct": card.get("progress_pct"), "deadline": card.get("deadline"),
            "days_left": card.get("days_left"), "weather": card.get("weather"),
            "weather_note": card.get("weather_note"), "signals": card.get("signals") or [],
            "open_count": card.get("open_count"), "late_count": card.get("late_count"),
        },
        "idle_days": _days_since(last["at"], today) if last else None,
        "last_action": last,
        "next_step": step,
        "current_phase": card.get("current_phase"),
        "next_milestone": card.get("next_milestone"),
        "blocked": blocked,
        "anomalies": [t for t in tasks["anomalies"] if t["status"] != "done"],
        "deliverables": [t for t in tasks["deliverables"] if t["status"] != "done"],
        "waiting": detail["waiting"],
        "decisions": decisions,
        "claude_sessions": [item for item in timeline if item["source"] == "claude"][:CLAUDE_SESSIONS],
        "upcoming": detail["events"][:3],
        "timeline": timeline,
        "generated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
    }


# ---------------------------------------------------------------- Resume par l'IA (facultatif)

INSTRUCTIONS = """Tu aides un chef de projet Odoo a reprendre un projet apres une interruption.
On te donne la synthese factuelle du projet (donnees enregistrees uniquement).
Redige en francais un resume de reprise : ou en est le projet, ce qui a ete fait en dernier,
ce qui bloque ou attend le client, et la prochaine action concrete.

Regles strictes :
- N'utilise QUE les faits fournis. Si une information manque, ne la suppose pas.
- Chaque affirmation cite sa source entre crochets avec la reference fournie : [T12] pour une tache, [J8] pour une entree de journal ou de registre.
- 5 phrases au plus, ton direct, pas de formule de politesse.

Reponds UNIQUEMENT en JSON : {"resume": "texte avec references [T12]", "prochaine_action": "une phrase", "sources": ["T12", "J8"]}"""


def _facts(data):
    """Synthese reduite aux faits utiles, avec leurs references."""
    def task(t):
        return {"ref": f"T{t['id']}", "titre": t["title"], "echeance": t["deadline"],
                "avancement": t["progress_pct"], "bloquee": bool(t["blocked"]), "cote": t["side"]}

    return {
        "projet": data["project"],
        "jours_sans_activite": data["idle_days"],
        "etape_en_cours": data["current_phase"],
        "prochain_jalon": data["next_milestone"],
        "prochaine_tache": task(data["next_step"]) if data["next_step"] else None,
        "bloque_ou_attente_client": [task(t) for t in data["blocked"][:8]],
        "anomalies_ouvertes": [task(t) | {"gravite": t["severity"]} for t in data["anomalies"][:8]],
        "livrables_attendus": [task(t) for t in data["deliverables"][:8]],
        "attentes": [{"qui": w["person_name"], "objet": w["subject"], "depuis_jours": w["days_open"]}
                     for w in data["waiting"][:6]],
        "decisions": [{"ref": f"J{d['id']}", "texte": d["content"][:400], "le": d["created_at"][:10]}
                      for d in data["decisions"]],
        "chronologie": [{"ref": item["ref"], "le": (item["at"] or "")[:16], "type": item["label"],
                         "texte": (item["text"] or "")[:300], "tache": item["task_title"]}
                        for item in data["timeline"][:15]],
    }


def summarize(project_id, today=None):
    """Resume de reprise redige par l'IA, appuye sur des sources verifiees.

    :return: {"summary", "next_action", "sources"} ; les references inventees
             par le modele sont retirees du texte et de la liste.
    :raises llm.ProviderError: fournisseur indisponible
    """
    data = resume(project_id, today)
    if data is None:
        return None
    facts = _facts(data)
    known = set(re.findall(r'"ref": "([TJ]\d+)"', json.dumps(facts, ensure_ascii=False)))
    answer = llm.ask(json.dumps(facts, ensure_ascii=False, default=str), system=INSTRUCTIONS,
                     purpose="reprise", max_tokens=700)
    text = str(answer.get("resume") or "").strip()
    # Une reference absente des faits est une invention : on la retire.
    text = re.sub(r"\s*\[([TJ]\d+)\]", lambda m: f" [{m.group(1)}]" if m.group(1) in known else "", text).strip()
    sources = [ref for ref in answer.get("sources") or [] if ref in known]
    sources += [ref for ref in re.findall(r"\[([TJ]\d+)\]", text) if ref not in sources]
    return {
        "summary": text,
        "next_action": str(answer.get("prochaine_action") or "").strip(),
        "sources": sources,
    }
