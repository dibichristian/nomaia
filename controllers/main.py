"""Pages HTML de l'interface."""
from datetime import date

from flask import Blueprint, g, redirect, render_template, request

import config
from models import project as project_model
from models import setting

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def inbox():
    """Boite de reception : depot d'entrees brutes et dialogue avec NOMAIA."""
    return render_template("inbox.html", today=date.today().isoformat())


@main_bp.post("/appairage")
def pair_device():
    """Appaire un appareil (telephone) avec le code affiche dans Config.

    Ouverte sans cle (voir controllers.security) : un code juste pose le
    cookie d'acces, comme l'ouverture depuis l'icone de la barre des taches.
    """
    if setting.consume_pairing_code(request.form.get("code")):
        g.grant_cookie = setting.ensure_access_key()
        return redirect("/")
    return render_template("acces.html", pairing_failed=True), 401


@main_bp.route("/chat")
def chat():
    """Conversation avec NOMAIA : questions et ordres en langage naturel."""
    return render_template("chat.html", today=date.today().isoformat())


@main_bp.route("/plan")
def plan():
    """Plan du jour : les taches placees dans les creneaux de travail."""
    return render_template("plan.html", today=date.today().isoformat())


@main_bp.route("/agenda")
def agenda():
    """Agenda : reunions, creneaux bloques et absences de la semaine."""
    return render_template(
        "agenda.html",
        today=date.today().isoformat(),
        projects=[
            item
            for item in project_model.list_projects(status=None)
            if item["status"] != "done"
        ],
    )


@main_bp.route("/projets")
def projects():
    """Projets : charge, avancement et temps passe."""
    return render_template("projets.html", today=date.today().isoformat())


@main_bp.route("/projets/<int:project_id>")
def project_detail(project_id):
    """Fiche d'un projet : avancement, taches, commentaires, contacts, agenda."""
    projet = project_model.get_project(project_id)
    if projet is None:
        return render_template("projets.html", today=date.today().isoformat()), 404
    return render_template(
        "projet.html", today=date.today().isoformat(), project=projet
    )


@main_bp.route("/contacts")
def contacts():
    """Interlocuteurs : qui attend quoi, et ce que l'utilisateur leur doit."""
    return render_template(
        "contacts.html",
        today=date.today().isoformat(),
        projects=[
            item
            for item in project_model.list_projects(status=None)
            if item["status"] != "done"
        ],
    )


@main_bp.route("/bilan")
def review():
    """Bilan de periode : chiffres et analyse de l'IA."""
    return render_template("bilan.html", today=date.today().isoformat())


@main_bp.route("/config")
def configuration():
    """Configuration : moteur, jetons, seuils, horaires, Telegram, systeme."""
    return render_template("config.html", today=date.today().isoformat())


@main_bp.route("/taches")
def dashboard():
    """Tableau de bord : liste des taches et saisie manuelle."""
    return render_template(
        "dashboard.html",
        today=date.today().isoformat(),
        projects=[
            item
            for item in project_model.list_projects(
                status=None, space=setting.space_filter()
            )
            if item["status"] != "done"
        ],
        priority_labels=config.PRIORITY_LABELS,
        energy_labels=config.ENERGY_LABELS,
    )


def _open_projects():
    """Projets proposes dans les listes de rattachement (hors termines)."""
    return [item for item in project_model.list_projects(status=None) if item["status"] != "done"]


@main_bp.route("/notes")
@main_bp.route("/notes/<int:note_id>")
def knowledge(note_id=None):
    """Base de connaissances : notes techniques en Markdown ; note_id ouvre une note."""
    return render_template(
        "notes.html", today=date.today().isoformat(), note_id=note_id, projects=_open_projects()
    )


@main_bp.route("/comptes-rendus")
@main_bp.route("/comptes-rendus/<int:minutes_id>")
def minutes(minutes_id=None):
    """Comptes rendus de reunion, d'intervention et de formation ; minutes_id ouvre l'editeur."""
    return render_template(
        "minutes.html", today=date.today().isoformat(), minutes_id=minutes_id, projects=_open_projects()
    )
