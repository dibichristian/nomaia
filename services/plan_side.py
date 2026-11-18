"""Plan du jour : indicateurs et colonne laterale (agenda, signaux projets, boite de reception).

Tout est lu dans ce qui existe deja (sessions, taches, agenda, pilotage des
projets, captures) : une seule requete HTTP pour habiller la page.
"""
from datetime import date, datetime

from models import capture as capture_model
from models import event as event_model
from services import plan_kpis, project_board

MAX_SIGNALS = 3
MAX_EVENTS = 6


def side(day=None):
    """Indicateurs du jour et contenu de la colonne laterale du plan."""
    today = date.today()
    day = day or today
    signals = []
    for card in project_board.overview(today)["projects"]:
        if card.get("status") == "done" or not card.get("signals"):
            continue
        first = card["signals"][0]
        signals.append({
            "project_id": card["id"], "name": card["name"], "color": card.get("color"),
            "level": first["level"], "label": first["label"], "progress_pct": card.get("progress_pct") or 0,
        })
    signals.sort(key=lambda item: item["level"] != "danger")

    events = [
        {"title": ev["title"], "starts_at": ev["starts_at"], "ends_at": ev["ends_at"], "kind": ev.get("kind"),
         "location": ev.get("location"), "project_name": ev.get("project_name")}
        for ev in event_model.list_events(day, day)
    ][:MAX_EVENTS]

    last = capture_model.list_captures(limit=1)
    return {
        "day": day.isoformat(),
        # Indicateurs lus au jour affiche : un jour passe montre son propre etat.
        "kpis": plan_kpis.kpis(day),
        "agenda": events,
        "signals": signals[:MAX_SIGNALS],
        "inbox": {
            "pending": capture_model.count_open_captures(),
            "last": ({"text": (last[0]["raw_text"] or "")[:200], "source": last[0]["source"],
                      "created_at": last[0]["created_at"]} if last else None),
        },
        "generated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
    }
