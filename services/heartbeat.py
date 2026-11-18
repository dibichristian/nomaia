"""Battement de coeur : NOMAIA parle en premier.

Un gestionnaire de taches attend qu'on l'ouvre ; un assistant vient au-devant.
Ce module tient les deux rendez-vous quotidiens :

- le matin, le plan de la journee et l'alerte de surcharge ;
- le soir, ce qui a ete fait, ce qui reste, et les taches qui derivent.

Le declenchement vit en base (`heartbeat_last_morning`, `heartbeat_last_evening`)
et non en memoire : redemarrer NOMAIA trois fois dans la matinee ne renvoie pas
trois fois le meme message.
"""
import logging
import threading
from datetime import date, datetime, time, timedelta

from models import alert as alert_model
from models import event as event_model
from models import person as person_model
from models import setting
from models import task as task_model
from services import calendar_sync, foresight, holidays, mail_reader, planner, workdays

logger = logging.getLogger("nomaia.heartbeat")

# Granularite de reveil. Les rendez-vous sont a la minute : inutile de
# consulter l'heure plus souvent, et un thread endormi ne coute rien.
TICK_S = 30

# Retard au-dela duquel un rendez-vous manque n'est plus rattrape. Allumer le
# PC a 17 h ne doit pas declencher le point du matin ; le rendez-vous est
# simplement marque comme passe.
MAX_LATE_MIN = 180

# Nombre de creneaux detailles dans le point du matin. Au-dela, le message
# devient un mur de texte que personne ne lit sur un telephone.
MAX_SLOTS_SHOWN = 8

# Attentes citees dans une relance. Au-dela, le message devient une liste
# qu'on saute au lieu de la lire.
MAX_NUDGES_SHOWN = 5

# Retard au-dela duquel un rappel de reunion n'est plus envoye : reveiller le
# PC a 15 h ne doit pas annoncer une reunion de 9 h comme imminente.
REMINDER_GRACE_MIN = 10

# Taches dormantes citees dans le menage de la semaine.
MAX_DORMANT_SHOWN = 5

# Silence entre deux signalements d'un meme sujet, en jours. None : une seule
# fois, jamais plus (l'alerte immediate d'une echeance donnee).
DORMANT_QUIET_DAYS = 14
VALIDATION_QUIET_DAYS = 2
IDLE_PROJECT_QUIET_DAYS = 7

# Frequence de recalcul des echeances en danger pour l'alerte immediate.
FORESIGHT_EVERY_MIN = 15

DAYS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS_FR = (
    "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
)


def _long_date(day):
    """Retourne une date en clair : 'mardi 16 septembre'."""
    return f"{DAYS_FR[day.weekday()]} {day.day} {MONTHS_FR[day.month - 1]}"


def _duration(minutes):
    """Formate une duree en heures et minutes."""
    minutes = int(minutes or 0)
    heures, reste = divmod(minutes, 60)
    if not heures:
        return f"{reste} min"
    return f"{heures} h {reste:02d}" if reste else f"{heures} h"


def morning_brief(day=None):
    """Compose le point du matin a partir du plan de la journee.

    :return: texte pret a envoyer, ou None s'il n'y a rien a dire
    """
    day = day or date.today()
    plan = planner.build_plan(day)
    # Le temps deja ecoule de la journee n'est plus disponible : le point
    # envoye a 10 h ne compte pas les deux heures passees.
    now = datetime.now() if day == date.today() else datetime.combine(day, time.min)

    risques = []
    propositions, questions, cles_validation = [], [], []
    dormantes, a_l_arret, cles_menage, semaine = [], [], [], None
    if setting.get("foresight_enabled") == "1":
        risques = foresight.deadlines_at_risk(now)
        propositions, questions, cles_validation = _validation_items(now)
        dormantes, a_l_arret, cles_menage, semaine = _weekly_items(day, now)

    # Une journee sans tache mais pleine de reunions a bel et bien quelque
    # chose a dire : se taire laisserait croire a une journee vide.
    if (
        not plan["slots"]
        and not plan["overflow"]
        and not plan["events"]
        and not pending_nudges(day)
        and not risques
        and not propositions
        and not questions
        and not dormantes
        and not a_l_arret
    ):
        return None

    lignes = [f"Plan du jour - {_long_date(day)}"]
    lignes.append(
        f"Charge {plan['load_pct']} % "
        f"({_duration(plan['planned_min'])} sur {_duration(plan['capacity_min'])})"
    )

    # Le temps deja pris est annonce a part : une capacite reduite sans
    # explication laisse croire a une erreur de calcul.
    if plan.get("busy_min"):
        lignes.append(
            f"{_duration(plan['busy_min'])} d'agenda, "
            f"{len(plan['events'])} rendez-vous."
        )

    if plan["load_pct"] > 100:
        lignes.append("Journee en surcharge : quelque chose devra sauter.")

    lignes.append("")
    fil = planner.timeline(plan)
    for entree in fil[:MAX_SLOTS_SHOWN]:
        if entree["kind"] == "event":
            evenement = entree["event"]
            lieu = f" - {evenement['location']}" if evenement.get("location") else ""
            marque = "Perso" if evenement["kind"] == "personal" else "RDV"
            lignes.append(
                f"{entree['start']}-{entree['end']} [{marque}] {evenement['title']}{lieu}"
            )
            continue

        tache = entree["task"]
        lignes.append(
            f"{entree['start']} P{tache['priority']} {tache['title']}"
            f" ({_duration(entree['duration_min'])})"
        )

    reste = len(fil) - MAX_SLOTS_SHOWN
    if reste > 0:
        lignes.append(f"... et {reste} autre(s) creneau(x).")

    if plan["overflow"]:
        lignes.append("")
        lignes.append(f"Ne rentre pas dans la journee : {len(plan['overflow'])} tache(s).")

    lignes.extend(risk_block(risques))

    # Les attentes sont marquees ici, au moment ou elles partent reellement :
    # les marquer a la lecture les ferait taire sans que rien ne soit envoye.
    attentes = pending_nudges(day)
    lignes.extend(nudge_block(attentes))
    person_model.mark_nudged([attente["id"] for attente in attentes])

    lignes.extend(validation_block(propositions, questions))
    lignes.extend(weekly_block(dormantes, a_l_arret, day))

    # Meme regle pour les alertes : marquees a l'envoi. Une echeance de
    # demain deja annoncee ici ne repart pas en alerte immediate dans l'heure.
    cles = cles_validation + cles_menage
    cles += [
        urgent_key(tache, risque["deadline"])
        for risque in risques
        if risque["days_left"] <= 1
        for tache in risque["tasks"]
    ]
    alert_model.mark_sent(cles, now)
    if semaine:
        setting.set_value("foresight_last_weekly", semaine)

    return "\n".join(lignes)


def _short_date(iso):
    """Date courte lisible : « jeu. 08/10 »."""
    jour = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{DAYS_FR[jour.weekday()][:3]}. {jour.day:02d}/{jour.month:02d}"


def _titles(taches, limite=2):
    """Titres d'une liste de taches, tronques au-dela de `limite`."""
    noms = [tache["title"] for tache in taches[:limite]]
    reste = len(taches) - limite
    return ", ".join(noms) + (f" (+{reste})" if reste > 0 else "")


def urgent_key(tache, echeance):
    """Cle d'alerte immediate : une tache, pour une echeance donnee."""
    return f"urgent:task:{tache['id']}:{echeance}"


def risk_block(risques):
    """Compose le bloc des echeances en danger. Liste de lignes, vide si rien."""
    if not risques:
        return []

    lignes = ["", "Echeances en danger :"]
    for risque in risques:
        lignes.append(
            f"- {_short_date(risque['deadline'])} : {_titles(risque['tasks'])}"
            f" - {_duration(risque['work_min'])} de travail,"
            f" {_duration(risque['free_min'])} libres d'ici la"
        )
    lignes.append("Decale, delegue ou reduis : en l'etat, tout ne tiendra pas.")
    return lignes


def _validation_items(now):
    """Propositions et questions du triage a rappeler, hors periode de silence."""
    attente = foresight.forgotten_validations(now)
    propositions = alert_model.filter_quiet(
        attente["proposals"], lambda item: f"validation:proposal:{item['id']}",
        VALIDATION_QUIET_DAYS, now,
    )
    questions = alert_model.filter_quiet(
        attente["questions"], lambda item: f"validation:question:{item['id']}",
        VALIDATION_QUIET_DAYS, now,
    )
    cles = [f"validation:proposal:{item['id']}" for item in propositions]
    cles += [f"validation:question:{item['id']}" for item in questions]
    return propositions, questions, cles


def validation_block(propositions, questions):
    """Compose le bloc des validations oubliees. Liste de lignes, vide si rien."""
    if not propositions and not questions:
        return []

    lignes = ["", "En attente de ta validation :"]
    if propositions:
        lignes.append(f"- {len(propositions)} proposition(s) du triage a valider")
    for question in questions[:3]:
        extrait = (question["raw_text"] or "capture").strip().replace("\n", " ")[:60]
        lignes.append(f"- Question sans reponse : {extrait}")
    return lignes


def _weekly_items(day, now):
    """Taches dormantes et projets a l'arret, si le menage de la semaine est du.

    Le menage part au premier point du matin de chaque semaine ISO : le lundi,
    ou le mardi si le lundi est ferie.

    :return: (taches, projets, cles, semaine) ; semaine vaut None s'il n'est pas du
    """
    annee, numero, _ = day.isocalendar()
    semaine = f"{annee}-W{numero:02d}"
    if setting.get("foresight_last_weekly") == semaine:
        return [], [], [], None

    taches = alert_model.filter_quiet(
        foresight.dormant_tasks(day), lambda item: f"dormant:task:{item['id']}",
        DORMANT_QUIET_DAYS, now,
    )[:MAX_DORMANT_SHOWN]
    projets = alert_model.filter_quiet(
        foresight.idle_projects(day), lambda item: f"idle:project:{item['id']}",
        IDLE_PROJECT_QUIET_DAYS, now,
    )
    cles = [f"dormant:task:{item['id']}" for item in taches]
    cles += [f"idle:project:{item['id']}" for item in projets]
    return taches, projets, cles, semaine


def weekly_block(taches, projets, day):
    """Compose le menage de la semaine. Liste de lignes, vide si rien."""
    if not taches and not projets:
        return []

    lignes = ["", "Menage de la semaine :"]
    if taches:
        lignes.append("Ces taches dorment (planifie, delegue ou abandonne) :")
        for tache in taches:
            creee = datetime.strptime(tache["created_at"][:10], "%Y-%m-%d").date()
            lignes.append(f"- {tache['title']} ({(day - creee).days} j)")
    if projets:
        lignes.append("Projets a l'arret : " + ", ".join(projet["name"] for projet in projets))
    return lignes


def pending_nudges(day=None):
    """Retourne les attentes qui meritent une relance, la plus ancienne d'abord.

    Ce que l'utilisateur attend des autres n'est pas une tache : sans ce rappel,
    des specs attendues depuis huit jours ne se signalent nulle part.
    """
    return person_model.stale_waiting(
        min_days=setting.get_int("nudge_after_days"),
        quiet_days=setting.get_int("nudge_quiet_days"),
        today=day or date.today(),
    )


def nudge_block(attentes):
    """Compose le bloc de relance. Retourne une liste de lignes, vide si rien."""
    if not attentes:
        return []

    lignes = ["", "Tu attends toujours :"]
    for attente in attentes[:MAX_NUDGES_SHOWN]:
        retard = " (en retard)" if attente["overdue"] else ""
        bloque = f" - bloque : {attente['task_title']}" if attente["task_title"] else ""
        lignes.append(
            f"- {attente['person_name']} : {attente['subject']}"
            f" ({attente['days_open']} j){retard}{bloque}"
        )

    reste = len(attentes) - MAX_NUDGES_SHOWN
    if reste > 0:
        lignes.append(f"... et {reste} autre(s).")

    return lignes


def drifting_tasks(day=None):
    """Retourne les taches en retard depuis plus que le seuil de derive.

    Une tache repoussee jour apres jour ne se signale nulle part : elle reste
    en tete de liste et devient un meuble. C'est elle qu'il faut nommer.
    """
    day = day or date.today()
    seuil = setting.get_int("drift_alert_days")
    limite = day.toordinal() - seuil

    en_derive = []
    for tache in task_model.list_tasks(status="todo"):
        if not tache["deadline"]:
            continue
        try:
            echeance = datetime.strptime(tache["deadline"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if echeance.toordinal() < limite:
            en_derive.append((day.toordinal() - echeance.toordinal(), tache))

    en_derive.sort(key=lambda couple: -couple[0])
    return en_derive


def evening_brief(day=None):
    """Compose le point du soir : fait, reste, derive.

    :return: texte pret a envoyer, ou None s'il n'y a rien a dire
    """
    day = day or date.today()
    chiffres = task_model.stats()

    if not chiffres["done_today"] and not chiffres["todo_count"]:
        return None

    lignes = [f"Fin de journee - {_long_date(day)}"]
    lignes.append(
        f"{chiffres['done_today']} terminee(s), {chiffres['todo_count']} restante(s)"
    )

    if chiffres["late_count"]:
        lignes.append(f"En retard : {chiffres['late_count']}")

    derive = drifting_tasks(day)
    if derive:
        lignes.append("")
        lignes.append("Ces taches derivent :")
        for jours, tache in derive[:5]:
            destinataire = f" - {tache['person_name']} attend" if tache.get("person_name") else ""
            lignes.append(f"- {tache['title']} ({jours} j de retard){destinataire}")
        lignes.append("Replanifie-les ou abandonne-les, elles ne partiront pas seules.")

    return "\n".join(lignes)


def reminder_text(evenement):
    """Compose le rappel d'une reunion imminente."""
    debut = evenement["starts_at"][11:16]
    fin = evenement["ends_at"][11:16]
    lignes = [f"Dans quelques minutes : {evenement['title']}", f"{debut} - {fin}"]

    if evenement.get("location"):
        lignes.append(evenement["location"])

    if setting.get("foresight_enabled") != "1":
        if evenement.get("project_name"):
            lignes.append(f"Projet : {evenement['project_name']}")
        return "\n".join(lignes)

    # Preparation : ce que j'ai promis aux personnes presentes, ce qu'elles me
    # doivent, ce qui tombe bientot sur le projet. Arriver en reunion sans
    # l'avoir en tete, c'est decouvrir sur place qu'on est en retard.
    contexte = foresight.meeting_context(evenement)
    if contexte["project"]:
        lignes.append(f"Projet : {contexte['project']}")
    if contexte["i_owe"]:
        lignes.append("Tu leur dois : " + " ; ".join(
            f"{tache['title']} ({tache['person_name']})" for tache in contexte["i_owe"]
        ))
    if contexte["they_owe"]:
        lignes.append("On te doit : " + " ; ".join(
            f"{attente['person_name']} - {attente['subject']}" for attente in contexte["they_owe"]
        ))
    if contexte["due_soon"]:
        lignes.append("A rendre bientot : " + " ; ".join(
            f"{tache['title']} ({_short_date(tache['deadline'])})"
            for tache in contexte["due_soon"]
        ))

    return "\n".join(lignes)


class Heartbeat:
    """Thread qui tient les rendez-vous quotidiens de NOMAIA."""

    # Rendez-vous : (nom, reglage d'heure, reglage d'etat, fabricant de message)
    APPOINTMENTS = (
        ("matin", "morning_brief_at", "heartbeat_last_morning", morning_brief),
        ("soir", "evening_brief_at", "heartbeat_last_evening", evening_brief),
    )

    def __init__(self, app, interval=TICK_S):
        """:param app: application Flask, pour ouvrir un contexte par reveil
        :param interval: delai entre deux verifications de l'heure
        """
        self.app = app
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def start(self):
        """Demarre le thread de fond (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="nomaia-heartbeat", daemon=True
        )
        self._thread.start()

    def stop(self):
        """Demande l'arret du thread."""
        self._stop.set()

    def is_running(self):
        """Indique si le thread tourne toujours."""
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        """Verifie l'heure a intervalle regulier et declenche ce qui est du."""
        while not self._stop.is_set():
            try:
                with self.app.app_context():
                    # Lecture d'un reglage a chaque tour, import effectif une
                    # fois par an et par pays.
                    holidays.ensure_imported()
                    # Relecture des agendas externes, au plus toutes les
                    # calendar_sync_min minutes : un appel reseau au pire.
                    calendar_sync.sync_due()
                    # Mails Outlook, toutes les heures en heures de travail.
                    # Un passage dure jusqu'a quelques minutes : il part dans
                    # son propre thread pour ne pas retarder les rappels.
                    mail_reader.read_due(self.app)
                    if setting.get("heartbeat_enabled") == "1":
                        maintenant = datetime.now()
                        self._check(maintenant)
                        self._check_reminders(maintenant)
                        self._check_foresight(maintenant)
                    # L'interface se contente de marquer l'instantane comme
                    # perime : c'est ici, hors requete HTTP, qu'il part.
                    bridge = self.app.extensions.get("telegram")
                    if bridge:
                        bridge.refresh_digest_if_stale()
            except Exception as error:  # le thread ne doit jamais mourir
                self.last_error = str(error)
                logger.exception("Erreur inattendue dans le battement de coeur.")

            self._stop.wait(self.interval)

    def _check(self, now):
        """Declenche les rendez-vous echus de la journee en cours."""
        for nom, reglage_heure, reglage_etat, fabriquer in self.APPOINTMENTS:
            retard = self._minutes_late(now, setting.get(reglage_heure))
            if retard is None or retard < 0:
                continue
            if setting.get(reglage_etat) == now.date().isoformat():
                continue

            # Le rendez-vous est marque AVANT l'envoi : un echec de reseau ne
            # doit pas faire rejouer le message a chaque reveil pendant 3 h.
            setting.set_value(reglage_etat, now.date().isoformat())

            if retard > MAX_LATE_MIN:
                logger.info("Rendez-vous du %s trop en retard (%s min) : ignore.", nom, retard)
                continue

            # Un jour de repos, NOMAIA ne pousse rien : ni plan, ni bilan, ni
            # relance. Les rappels d'agenda, eux, restent actifs.
            motif = workdays.day_off_reason(now.date())
            if motif:
                logger.info("Rendez-vous du %s saute : %s.", nom, motif)
                continue

            texte = fabriquer(now.date())
            if texte:
                self._deliver(nom, texte)

    def _check_foresight(self, now):
        """Previent tout de suite quand une echeance proche bascule en danger.

        Le point du matin dit l'etat au reveil ; une reunion Outlook arrivee a
        11 h peut rendre intenable une echeance de demain. Seules les
        echeances d'aujourd'hui et de demain declenchent l'alerte, une seule
        fois chacune, un jour ouvre, pendant les heures de travail et apres le
        point du matin : jamais la nuit, jamais en doublon du matin.
        """
        if setting.get("foresight_enabled") != "1":
            return
        if not workdays.is_working_day(now.date()):
            return
        if setting.get("heartbeat_last_morning") != now.date().isoformat():
            return

        debut = planner._parse_time(setting.get("work_start"), "08:30")
        fin = planner._parse_time(setting.get("work_end"), "18:00")
        if not debut <= now.time() < fin:
            return

        dernier = setting.get("foresight_last_check") or ""
        try:
            if now - datetime.strptime(dernier, "%Y-%m-%d %H:%M") < timedelta(
                minutes=FORESIGHT_EVERY_MIN
            ):
                return
        except ValueError:
            pass
        setting.set_value("foresight_last_check", now.strftime("%Y-%m-%d %H:%M"))

        nouvelles = []
        for risque in foresight.deadlines_at_risk(now):
            if risque["days_left"] > 1:
                continue
            taches = [
                tache for tache in risque["tasks"]
                if not alert_model.sent_within(urgent_key(tache, risque["deadline"]), None)
            ]
            if taches:
                nouvelles.append((risque, taches))

        if not nouvelles:
            return

        lignes = ["Echeance en danger"]
        cles = []
        for risque, taches in nouvelles:
            quand = "aujourd'hui" if risque["days_left"] == 0 else "demain"
            lignes.append(
                f"{_titles(taches)} - du {quand} : {_duration(risque['work_min'])} de travail,"
                f" {_duration(risque['free_min'])} libres."
            )
            cles += [urgent_key(tache, risque["deadline"]) for tache in taches]
        lignes.append("Decale, delegue ou reduis maintenant, pas ce soir.")

        alert_model.mark_sent(cles, now)
        self._deliver("alerte", "\n".join(lignes))

    def _check_reminders(self, now):
        """Previent avant chaque reunion qui approche.

        Le filigrane retient le debut de la derniere occurrence annoncee : il
        vit en base et non en memoire, donc redemarrer NOMAIA ne renvoie pas
        les rappels deja partis. Deux reunions a la meme heure partent dans le
        meme passage, puis le filigrane avance sur la plus tardive.
        """
        if setting.get("event_reminder_enabled") != "1":
            return

        delai = setting.get_int("event_reminder_min")
        horizon = now + timedelta(minutes=max(delai, 0))

        # Un rappel manque de plus de REMINDER_GRACE_MIN est perime : la
        # reunion a commence, l'annoncer comme imminente n'aide personne.
        plancher = max(
            self._reminder_watermark(),
            now - timedelta(minutes=REMINDER_GRACE_MIN),
        )

        occurrences = event_model.next_occurrences(plancher, horizon)
        if not occurrences:
            return

        dernier = plancher
        for evenement in occurrences:
            if evenement["kind"] == "absence":
                # Une absence n'est pas un rendez-vous : rien a rappeler.
                continue
            self._deliver("rappel", reminder_text(evenement))
            debut = datetime.strptime(evenement["starts_at"], "%Y-%m-%d %H:%M")
            dernier = max(dernier, debut)

        setting.set_value("event_reminder_watermark", dernier.strftime("%Y-%m-%d %H:%M"))

    @staticmethod
    def _reminder_watermark():
        """Retourne le debut de la derniere occurrence deja annoncee."""
        brut = setting.get("event_reminder_watermark")
        try:
            return datetime.strptime(brut, "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return datetime.min

    @staticmethod
    def _minutes_late(now, moment):
        """Retourne les minutes ecoulees depuis l'heure prevue, ou None si illisible."""
        try:
            prevu = datetime.strptime(moment, "%H:%M").time()
        except (TypeError, ValueError):
            return None
        return (now.hour * 60 + now.minute) - (prevu.hour * 60 + prevu.minute)

    def _deliver(self, nom, texte):
        """Pousse un message sur Telegram et en bulle Windows."""
        bridge = self.app.extensions.get("telegram")
        if bridge:
            bridge.notify_owner(texte)
            # Le rendez-vous est le bon moment pour remettre l'epingle a jour.
            bridge.update_pinned_digest()

        notifier = self.app.extensions.get("notifier")
        if notifier:
            # La bulle Windows ne tient qu'une ligne utile : la premiere.
            notifier("NOMAIA", texte.split("\n", 1)[0])

        logger.info("Point du %s envoye.", nom)
