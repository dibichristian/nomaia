"""Remplit une base NOMAIA avec un petit jeu de donnees fictif.

Sert a decouvrir l'interface ou a faire des captures d'ecran sans exposer de
vraies donnees. Refuse d'ecrire dans une base qui contient deja des taches.

A lancer sur un dossier de donnees dedie :
    NOMAIA_DATA_DIR=./demo-data python tools/seed_demo.py
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from models import get_db  # noqa: E402
from models import person as person_model  # noqa: E402
from models import project as project_model  # noqa: E402
from models import task as task_model  # noqa: E402

PROJECTS = (
    ("ALPHA", "#6366f1", 30),
    ("ORION", "#0ea5e9", 60),
    ("Outils internes", "#22c55e", None),
)

# (titre, projet, priorite, estimation en minutes, echeance en jours)
TASKS = (
    ("Corriger le calcul des congés", "ALPHA", 1, 90, 1),
    ("Préparer la recette du module paie", "ALPHA", 2, 120, 5),
    ("Rédiger le compte rendu du comité", "ALPHA", 3, 30, 2),
    ("Chiffrer l'import des écritures", "ORION", 2, 60, 7),
    ("Configurer les droits des commerciaux", "ORION", 3, 45, None),
    ("Mettre à jour le modèle de devis", "Outils internes", 4, 20, None),
)

PEOPLE = (
    {"name": "Paul Martin", "role": "Chef de projet client", "project": "ALPHA"},
    {"name": "Lucie Bernard", "role": "Responsable comptable", "project": "ORION"},
)


def _deadline(days):
    """Echeance a `days` jours d'aujourd'hui, ou None."""
    return None if days is None else (date.today() + timedelta(days=days)).isoformat()


def seed():
    """Cree les projets, taches, contacts et une attente de demonstration."""
    projects = {}
    for name, color, days in PROJECTS:
        projects[name] = project_model.create_project(name, color=color, deadline=_deadline(days))

    for title, project, priority, estimate, days in TASKS:
        task_model.create_task({
            "title": title,
            "project_id": projects[project]["id"],
            "priority": priority,
            "estimate_min": estimate,
            "deadline": _deadline(days),
        })

    people = {}
    for data in PEOPLE:
        people[data["name"]] = person_model.create_person({
            "name": data["name"],
            "role": data["role"],
            "project_id": projects[data["project"]]["id"],
        })

    person_model.create_waiting({
        "person_id": people["Paul Martin"]["id"],
        "subject": "Les spécifications de l'export de paie",
    })


def main():
    """Point d'entree : exige un dossier dedie et refuse une base deja utilisee."""
    if not os.environ.get("NOMAIA_DATA_DIR"):
        sys.exit("Definir NOMAIA_DATA_DIR vers un dossier dedie a la demonstration.")
    app = create_app(start_worker=False)
    with app.app_context():
        if get_db().execute("SELECT COUNT(*) FROM task").fetchone()[0]:
            sys.exit("La base contient deja des taches : rien n'est ecrit.")
        seed()
    print("Donnees de demonstration creees.")


if __name__ == "__main__":
    main()
