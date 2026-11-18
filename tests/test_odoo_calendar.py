"""Tests de l'agenda Odoo (services/odoo_calendar.py).

Lecture seule : un faux client Odoo repond selon le modele, le vrai serveur
n'est jamais appele. Odoo fait foi : ce qui y disparait disparait de NOMAIA,
un conge en attente s'affiche sans bloquer le plan, et une panne laisse
l'agenda intact.
"""
from datetime import date
from unittest.mock import patch

from models import calendar_source as source_model
from models import day_off as day_off_model
from models import event as event_model
from models import get_db, setting
from models import project as project_model
from services import calendar_sync, odoo_calendar, odoo_sync, planner
from services.odoo_client import OdooRpcError
from tests.base import NomaiaTestCase

AUJOURD_HUI = date(2026, 11, 9)
UID = 7
PARTENAIRE = 3

# Champs connus du faux serveur, par modele (fields_get).
CHAMPS = {
    "hr.leave": {
        "holiday_status_id", "name", "state", "date_from", "date_to", "request_date_from",
        "request_date_to", "request_unit_half", "request_unit_hours", "number_of_days",
    },
    "resource.calendar.leaves": {
        "name", "date_from", "date_to", "time_type", "calendar_id", "company_id", "resource_id",
    },
    "calendar.event": {
        "name", "start", "stop", "allday", "start_date", "stop_date", "location", "description",
        "partner_ids", "res_model",
    },
}


def _valeur(champ):
    """Ramene un many2one ([id, nom]) a son identifiant pour comparer."""
    if isinstance(champ, list) and len(champ) == 2 and isinstance(champ[1], str):
        return champ[0]
    return champ


class FauxOdoo:
    """Faux client Odoo : memes methodes que OdooClient, donnees en memoire.

    Le domaine n'est applique que pour les clauses simples ('=' et 'in') sur
    un champ present dans la ligne : de quoi verifier le filtre des etats et
    des invitations declinees, sans reecrire Odoo.
    """

    def __init__(self, donnees=None, tz="Africa/Abidjan", champs=None, panne=None):
        self.donnees = donnees or {}
        self.tz = tz
        self.champs = champs or CHAMPS
        self.panne = panne
        self.appels = []

    def authenticate(self):
        if self.panne:
            raise self.panne
        return UID

    def read(self, model, ids, fields, context=None):
        self.appels.append(("read", model))
        assert model == "res.users"
        return [{"id": UID, "partner_id": [PARTENAIRE, "Moi"], "tz": self.tz,
                 "company_id": [1, "Exemple SARL"]}]

    def fields_get(self, model, attributes=("type", "selection")):
        self.appels.append(("fields_get", model))
        return {nom: {"type": "char"} for nom in self.champs.get(model, ())}

    def search(self, model, domain, context=None, order="id"):
        return [row["id"] for row in self.search_read(model, domain, ["id"])]

    def search_read(self, model, domain, fields, context=None, order="id"):
        self.appels.append(("search_read", model))
        connus = self.champs.get(model)
        if connus is not None:
            inconnus = set(fields) - connus
            assert not inconnus, f"champs inconnus demandes : {inconnus}"
        lignes = []
        for row in self.donnees.get(model, []):
            if self._evalue(row, domain):
                lignes.append({cle: row[cle] for cle in ["id", *fields] if cle in row})
        return lignes

    def _evalue(self, row, domain):
        """Evalue un domaine en notation polonaise ('|' et '&' prefixes)."""
        pile = list(domain)

        def suivant():
            tete = pile.pop(0)
            if tete in ("|", "&"):
                gauche, droite = suivant(), suivant()
                return (gauche or droite) if tete == "|" else (gauche and droite)
            return self._garde(row, tete)

        resultats = []
        while pile:
            resultats.append(suivant())
        return all(resultats)

    @staticmethod
    def _garde(row, clause):
        if not isinstance(clause, tuple) or clause[0] not in row:
            return True
        champ, operateur, attendu = clause
        valeur = _valeur(row[champ])
        if operateur == "=":
            return valeur == attendu
        if operateur == "in":
            return valeur in attendu
        return True


def conge(odoo_id, debut, fin, etat="validate", type_conge="Congés annuels", **extra):
    """Conge hr.leave ; debut et fin en UTC 'AAAA-MM-JJ HH:MM:SS'."""
    ligne = {
        "id": odoo_id,
        "holiday_status_id": [29, type_conge],
        "name": False,
        "state": etat,
        "date_from": debut,
        "date_to": fin,
        "request_date_from": debut[:10],
        "request_date_to": fin[:10],
        "request_unit_half": False,
        "request_unit_hours": False,
        "number_of_days": 1.0,
    }
    ligne.update(extra)
    return ligne


def reunion(odoo_id, titre, debut, fin, **extra):
    """Reunion calendar.event ou je suis invite."""
    ligne = {
        "id": odoo_id,
        "name": titre,
        "start": debut,
        "stop": fin,
        "allday": False,
        "start_date": False,
        "stop_date": False,
        "location": False,
        "description": False,
    }
    ligne.update(extra)
    return ligne


class OdooCalendarTest(NomaiaTestCase):
    """Import, transformation et remplacement de l'agenda Odoo."""

    def setUp(self):
        super().setUp()
        self.source = source_model.create_odoo_source()

    def _synchro(self, client):
        return odoo_calendar.sync_source(self.source["id"], AUJOURD_HUI, client=client)

    def _evenements(self):
        return event_model.list_events(date(2026, 11, 1), date(2027, 1, 31))

    def _absences(self):
        return day_off_model.list_between(date(2026, 11, 1), date(2027, 1, 31))

    # --- Conges -------------------------------------------------------------

    def test_conge_journee_devient_jour_de_repos(self):
        client = FauxOdoo({"hr.leave": [
            conge(1, "2026-11-16 08:00:00", "2026-11-18 17:00:00"),
        ]})
        issue = self._synchro(client)

        self.assertTrue(issue["ok"])
        absence = self._absences()[0]
        self.assertEqual(
            (absence["title"], absence["start_day"], absence["end_day"], absence["kind"]),
            ("Congés annuels", "2026-11-16", "2026-11-18", "leave"),
        )
        self.assertTrue(absence["read_only"])
        self.assertFalse(absence["tentative"])
        plan = planner.build_plan(date(2026, 11, 17))
        self.assertEqual(plan["day_off"], "Congés annuels")
        self.assertEqual(plan["capacity_min"], 0)
        self.assertEqual(self._evenements(), [])

    def test_demi_journee_absence_horaire_en_heure_locale(self):
        client = FauxOdoo(
            {"hr.leave": [conge(2, "2026-11-10 08:00:00", "2026-11-10 12:00:00",
                                request_unit_half=True, number_of_days=0.5)]},
            tz="Europe/Paris",
        )
        self._synchro(client)

        self.assertEqual(self._absences(), [])
        absence = self._evenements()[0]
        # Heure d'hiver a Paris : UTC + 1.
        self.assertEqual(
            (absence["kind"], absence["starts_at"], absence["ends_at"]),
            ("absence", "2026-11-10 09:00", "2026-11-10 13:00"),
        )
        self.assertTrue(absence["read_only"])

    def test_champs_de_demi_journee_absents_la_duree_tranche(self):
        champs = dict(CHAMPS)
        champs["hr.leave"] = CHAMPS["hr.leave"] - {"request_unit_half", "request_unit_hours"}
        client = FauxOdoo(
            {"hr.leave": [conge(3, "2026-11-10 13:00:00", "2026-11-10 17:00:00",
                                number_of_days=0.5)]},
            champs=champs,
        )
        self.assertTrue(self._synchro(client)["ok"])
        self.assertEqual(self._evenements()[0]["kind"], "absence")

    def test_conge_en_attente_affiche_sans_bloquer(self):
        client = FauxOdoo({"hr.leave": [
            conge(4, "2026-11-20 08:00:00", "2026-11-20 17:00:00", etat="confirm"),
            # En attente a l'heure : pas d'absence horaire, un jour provisoire.
            conge(5, "2026-11-23 08:00:00", "2026-11-23 10:00:00", etat="validate1",
                  request_unit_hours=True, number_of_days=0.25),
        ]})
        self._synchro(client)

        absences = self._absences()
        self.assertEqual([a["start_day"] for a in absences], ["2026-11-20", "2026-11-23"])
        self.assertTrue(all(a["tentative"] for a in absences))
        self.assertEqual(self._evenements(), [])
        self.assertEqual(day_off_model.off_map(date(2026, 11, 20), date(2026, 11, 23)), {})
        plan = planner.build_plan(date(2026, 11, 20))
        self.assertIsNone(plan["day_off"])
        self.assertGreater(plan["capacity_min"], 0)

    def test_conge_refuse_ou_annule_ignore(self):
        client = FauxOdoo({"hr.leave": [
            conge(6, "2026-11-24 08:00:00", "2026-11-24 17:00:00", etat="refuse"),
            conge(7, "2026-11-25 08:00:00", "2026-11-25 17:00:00", etat="cancel"),
        ]})
        issue = self._synchro(client)
        self.assertEqual(issue["count"], 0)
        self.assertEqual(self._absences(), [])

    # --- Feries -------------------------------------------------------------

    def test_ferie_deja_present_non_double(self):
        day_off_model.add_holiday("Noël", date(2026, 12, 25), "CI")
        client = FauxOdoo({
            "hr.employee": [{"id": 1, "user_id": [UID, "Moi"], "resource_calendar_id": [1, "Std"],
                             "company_id": [1, "Exemple SARL"]}],
            "resource.calendar.leaves": [
            {"id": 1, "name": "Fermeture de fin d'année", "date_from": "2026-12-24 00:00:00",
             "date_to": "2026-12-25 23:59:59", "time_type": "leave", "calendar_id": [1, "Std"],
             "company_id": [1, "Exemple SARL"], "resource_id": False},
            {"id": 2, "name": "Formation", "date_from": "2026-12-02 08:00:00",
             "date_to": "2026-12-02 17:00:00", "time_type": "other", "calendar_id": False,
             "company_id": [1, "Exemple SARL"], "resource_id": False},
            # Autre calendrier de travail : pas le mien.
            {"id": 3, "name": "Pont", "date_from": "2026-12-04 00:00:00",
             "date_to": "2026-12-04 23:59:59", "time_type": "leave", "calendar_id": [2, "Nuit"],
             "company_id": [1, "Exemple SARL"], "resource_id": False},
        ]})
        self._synchro(client)

        importes = [a for a in self._absences() if a["source_id"] == self.source["id"]]
        self.assertEqual(
            [(a["title"], a["start_day"], a["end_day"], a["kind"]) for a in importes],
            [("Fermeture de fin d'année", "2026-12-24", "2026-12-24", "holiday")],
        )
        self.assertEqual(planner.build_plan(date(2026, 12, 24))["capacity_min"], 0)

    # --- Reunions -----------------------------------------------------------

    def test_reunion_importee_avec_projet_devine(self):
        project_model.create_project("Alpha")
        paie = project_model.create_project("Alpha Paie")
        client = FauxOdoo({"calendar.event": [
            reunion(81, "Démonstration ALPHA paie - client", "2026-11-12 10:00:00",
                    "2026-11-12 11:30:00", location="Teams",
                    description="<p>Ordre du jour<br>Paie</p>"),
            reunion(82, "Point interne", "2026-11-13 09:00:00", "2026-11-13 09:30:00"),
        ]})
        self._synchro(client)

        premiere, seconde = self._evenements()
        self.assertEqual(
            (premiere["kind"], premiere["starts_at"], premiere["ends_at"], premiere["location"]),
            ("meeting", "2026-11-12 10:00", "2026-11-12 11:30", "Teams"),
        )
        self.assertEqual(premiere["project_id"], paie["id"])
        self.assertEqual(premiere["notes"], "Ordre du jour\nPaie")
        self.assertEqual(premiere["source_name"], "Odoo")
        self.assertTrue(premiere["read_only"])
        self.assertIsNone(seconde["project_id"])
        uid = get_db().execute(
            "SELECT external_uid FROM event WHERE id = ?", (premiere["id"],)
        ).fetchone()[0]
        self.assertEqual(uid, "odoo:81")

    def test_projet_en_mot_entier_seulement(self):
        project_model.create_project("Orion")
        client = FauxOdoo({"calendar.event": [
            reunion(83, "Copains d'abord", "2026-11-12 10:00:00", "2026-11-12 11:00:00"),
        ]})
        self._synchro(client)
        self.assertIsNone(self._evenements()[0]["project_id"])

    def test_reunion_declinee_ignoree(self):
        client = FauxOdoo({
            "calendar.event": [
                reunion(84, "Comité", "2026-11-12 10:00:00", "2026-11-12 11:00:00"),
                reunion(85, "Revue", "2026-11-12 14:00:00", "2026-11-12 15:00:00"),
            ],
            "calendar.attendee": [
                {"id": 1, "event_id": [84, "Comité"], "partner_id": [PARTENAIRE, "Moi"],
                 "state": "declined"},
                {"id": 2, "event_id": [85, "Revue"], "partner_id": [PARTENAIRE, "Moi"],
                 "state": "accepted"},
            ],
        })
        self._synchro(client)
        self.assertEqual([e["title"] for e in self._evenements()], ["Revue"])

    def test_reunion_creee_par_un_conge_ignoree(self):
        # Odoo cree « X en conge » dans l'agenda pour chaque conge : deja importe en jour de repos.
        client = FauxOdoo({
            "calendar.event": [
                reunion(90, "Jean Dupont en congé : 1 jours", "2026-11-12 09:00:00",
                        "2026-11-12 18:00:00", res_model="hr.leave"),
                reunion(91, "Point hebdo", "2026-11-12 14:00:00", "2026-11-12 15:00:00"),
            ],
        })
        self._synchro(client)
        self.assertEqual([e["title"] for e in self._evenements()], ["Point hebdo"])

    def test_reunion_journee_entiere_sur_les_horaires_de_travail(self):
        setting.set_value("work_start", "08:00")
        setting.set_value("work_end", "17:30")
        client = FauxOdoo({"calendar.event": [
            reunion(86, "Séminaire", "2026-11-12 08:00:00", "2026-11-13 18:00:00",
                    allday=True, start_date="2026-11-12", stop_date="2026-11-13"),
        ]})
        self._synchro(client)
        self.assertEqual(
            [(e["starts_at"], e["ends_at"]) for e in self._evenements()],
            [("2026-11-12 08:00", "2026-11-12 17:30"), ("2026-11-13 08:00", "2026-11-13 17:30")],
        )

    def test_doublon_d_un_autre_agenda_ignore(self):
        ics = source_model.create_source(
            {"name": "Outlook", "url": "https://outlook.office365.com/x/calendar.ics"}
        )
        db = get_db()
        db.execute(
            "INSERT INTO event (title, starts_at, ends_at, kind, repeat, source_id)"
            " VALUES ('Seance de demonstration', '2026-11-12 10:00', '2026-11-12 11:00',"
            " 'meeting', 'none', ?)",
            (ics["id"],),
        )
        db.commit()
        event_model.create_event({
            "title": "point hebdo", "starts_at": "2026-11-13 09:00",
            "ends_at": "2026-11-13 09:30", "kind": "meeting",
        })
        client = FauxOdoo({"calendar.event": [
            reunion(87, "Séance de démonstration - NORDIK EUROPA", "2026-11-12 10:00:00",
                    "2026-11-12 11:00:00"),
            reunion(88, "Point hebdo", "2026-11-13 09:00:00", "2026-11-13 09:30:00"),
            # Meme titre, autre heure : ce n'est pas un doublon.
            reunion(89, "Point hebdo", "2026-11-20 09:00:00", "2026-11-20 09:30:00"),
        ]})
        self._synchro(client)

        odoo = [e for e in self._evenements() if e["source_id"] == self.source["id"]]
        self.assertEqual([e["starts_at"] for e in odoo], ["2026-11-20 09:00"])

    # --- Remplacement, reglages, pannes -------------------------------------

    def test_resynchro_remplace_sans_doublon(self):
        donnees = {
            "hr.leave": [conge(1, "2026-11-16 08:00:00", "2026-11-16 17:00:00")],
            "calendar.event": [
                reunion(90, "Garde", "2026-11-12 10:00:00", "2026-11-12 11:00:00"),
                reunion(91, "Partie", "2026-11-12 14:00:00", "2026-11-12 15:00:00"),
            ],
        }
        self._synchro(FauxOdoo(donnees))
        self._synchro(FauxOdoo(donnees))
        self.assertEqual(len(self._evenements()), 2)
        self.assertEqual(len(self._absences()), 1)

        donnees["calendar.event"] = donnees["calendar.event"][:1]
        self._synchro(FauxOdoo(donnees))
        self.assertEqual([e["title"] for e in self._evenements()], ["Garde"])
        self.assertEqual(source_model.get_source(self.source["id"])["last_count"], 2)

    def test_reglages_coupes_rien_importe(self):
        donnees = {
            "hr.leave": [conge(1, "2026-11-16 08:00:00", "2026-11-16 17:00:00")],
            "calendar.event": [reunion(92, "Revue", "2026-11-12 10:00:00", "2026-11-12 11:00:00")],
        }
        self._synchro(FauxOdoo(donnees))

        setting.set_value("odoo_cal_meetings", "0")
        client = FauxOdoo(donnees)
        self._synchro(client)
        self.assertEqual(self._evenements(), [])
        self.assertEqual(len(self._absences()), 1)
        self.assertNotIn(("search_read", "calendar.event"), client.appels)

        for cle in odoo_calendar.OPTION_KEYS.values():
            setting.set_value(cle, "0")
        # Tout coupe : Odoo n'est meme pas interroge.
        issue = self._synchro(FauxOdoo(donnees, panne=AssertionError("appel inattendu")))
        self.assertTrue(issue["ok"])
        self.assertEqual(issue["count"], 0)
        self.assertEqual(self._absences(), [])

    def test_erreur_odoo_enregistree_et_donnees_gardees(self):
        donnees = {"calendar.event": [
            reunion(93, "Revue", "2026-11-12 10:00:00", "2026-11-12 11:00:00"),
        ]}
        self._synchro(FauxOdoo(donnees))

        issue = self._synchro(FauxOdoo(panne=OdooRpcError("Authentification refusee")))
        self.assertFalse(issue["ok"])
        source = source_model.get_source(self.source["id"])
        self.assertIn("Authentification refusee", source["last_error"])
        self.assertEqual(source["last_count"], 1)
        self.assertEqual(len(self._evenements()), 1)

    def test_connexion_incomplete_enregistree(self):
        issue = odoo_calendar.sync_source(self.source["id"], AUJOURD_HUI)
        self.assertFalse(issue["ok"])
        self.assertIn("incomplete", source_model.get_source(self.source["id"])["last_error"])

    def test_synchro_planifiee_couvre_la_source_odoo(self):
        client = FauxOdoo({"calendar.event": [
            reunion(94, "Revue", "2026-11-12 10:00:00", "2026-11-12 11:00:00"),
        ]})
        with patch.object(odoo_sync, "build_client", return_value=client):
            issue = calendar_sync.sync_source(self.source["id"], AUJOURD_HUI)
        self.assertEqual(issue["count"], 1)
        # Synchronisee a l'instant : le battement ne la relit pas.
        self.assertEqual(calendar_sync.sync_due(), 0)

        source_model.record_sync(self.source["id"], "2000-01-01 00:00", count=0)
        with patch.object(odoo_sync, "build_client", side_effect=odoo_sync.OdooSyncError("x")):
            self.assertEqual(calendar_sync.sync_due(), 1)
        self.assertEqual(source_model.get_source(self.source["id"])["last_error"], "x")


class ApiOdooCalendarTest(NomaiaTestCase):
    """Routes /api/calendar-sources pour l'agenda Odoo."""

    def _configurer(self):
        setting.set_value("odoo_url", "https://exemple.odoo.com")
        setting.set_secret("odoo_api_key", "cle")

    def test_bouton_visible_si_connexion_et_aucune_source(self):
        client = self.client()
        self.assertFalse(client.get("/api/calendar-sources").get_json()["odoo_available"])
        self._configurer()
        self.assertTrue(client.get("/api/calendar-sources").get_json()["odoo_available"])

    def test_ajout_unique_et_options(self):
        self._configurer()
        client = self.client()
        with patch.object(odoo_sync, "build_client", return_value=FauxOdoo()):
            reponse = client.post(
                "/api/calendar-sources", json={"kind": "odoo"}, headers=self.write_headers()
            )
            self.assertEqual(reponse.status_code, 201)
            self.assertTrue(reponse.get_json()["sync"]["ok"])

            doublon = client.post(
                "/api/calendar-sources", json={"kind": "odoo"}, headers=self.write_headers()
            )
            self.assertEqual(doublon.status_code, 400)

        corps = client.get("/api/calendar-sources").get_json()
        self.assertFalse(corps["odoo_available"])
        source = corps["sources"][0]
        self.assertEqual(source["kind"], "odoo")
        self.assertEqual(
            source["options"],
            {"leaves": True, "pending": True, "holidays": True, "meetings": True},
        )

    def test_ajout_refuse_sans_connexion(self):
        reponse = self.client().post(
            "/api/calendar-sources", json={"kind": "odoo"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_reglage_d_import_modifiable(self):
        reponse = self.client().patch(
            "/api/settings", json={"odoo_cal_pending": "0"}, headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertFalse(odoo_calendar.options()["pending"])
