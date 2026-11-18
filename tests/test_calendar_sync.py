"""Tests de la synchro d'agendas externes (flux ICS Outlook).

L'agenda externe fait foi : ce qui y disparait disparait de NOMAIA, ce qui y
bouge bouge, et rien de ce qui est importe ne se modifie ici. Une panne de
reseau ne doit jamais vider l'agenda.
"""
import sqlite3
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from models import backup_db, get_db
from models import calendar_source as source_model
from models import day_off as day_off_model
from models import event as event_model
from models.calendar_source import SourceError
from models.day_off import DayOffError
from models.event import EventError
from services import calendar_sync, planner, tools
from services.calendar_sync import SyncError
from tests.base import NomaiaTestCase

AUJOURD_HUI = date(2026, 10, 2)

URL = "https://outlook.office365.com/owa/calendar/abc/def/calendar.ics"


def heure_locale(annee, mois, jour, heure, minute=0, fuseau="UTC"):
    """Heure locale attendue pour un horaire ICS donne dans un fuseau.

    Le calcul suit celui du service : les tests restent justes quel que soit
    le fuseau du poste qui les execute.
    """
    zone = timezone.utc if fuseau == "UTC" else ZoneInfo(fuseau)
    moment = datetime(annee, mois, jour, heure, minute, tzinfo=zone)
    return moment.astimezone().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")


def flux(*evenements):
    """Construit un flux ICS minimal, fuseau Windows d'Outlook compris."""
    entete = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:Microsoft Exchange Server 2010\r\n"
        "BEGIN:VTIMEZONE\r\nTZID:Romance Standard Time\r\n"
        "BEGIN:STANDARD\r\nDTSTART:16010101T030000\r\nTZOFFSETFROM:+0200\r\n"
        "TZOFFSETTO:+0100\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:+0100\r\n"
        "TZOFFSETTO:+0200\r\nRRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3\r\n"
        "END:DAYLIGHT\r\nEND:VTIMEZONE\r\n"
    )
    corps = "".join(
        "BEGIN:VEVENT\r\n" + "\r\n".join(lignes) + "\r\nEND:VEVENT\r\n" for lignes in evenements
    )
    return (entete + corps + "END:VCALENDAR\r\n").encode("utf-8")


def reunion(uid, titre, debut, fin, statut="BUSY", extra=()):
    """Evenement horaire en UTC."""
    return [
        f"UID:{uid}",
        f"SUMMARY:{titre}",
        f"DTSTART:{debut}",
        f"DTEND:{fin}",
        f"X-MICROSOFT-CDO-BUSYSTATUS:{statut}",
        *extra,
    ]


SERIE_HEBDO = [
    "UID:serie-1",
    "SUMMARY:Point hebdo",
    "DTSTART;TZID=Romance Standard Time:20261005T100000",
    "DTEND;TZID=Romance Standard Time:20261005T110000",
    "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4",
    "EXDATE;TZID=Romance Standard Time:20261012T100000",
    "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
]
SERIE_DEPLACEE = [
    "UID:serie-1",
    "RECURRENCE-ID;TZID=Romance Standard Time:20261019T100000",
    "SUMMARY:Point hebdo (deplace)",
    "DTSTART;TZID=Romance Standard Time:20261019T150000",
    "DTEND;TZID=Romance Standard Time:20261019T160000",
    "X-MICROSOFT-CDO-BUSYSTATUS:BUSY",
]
CONGES_OUTLOOK = [
    "UID:oof-1",
    "SUMMARY:Conges",
    "DTSTART;VALUE=DATE:20261020",
    "DTEND;VALUE=DATE:20261023",
    "X-MICROSOFT-CDO-BUSYSTATUS:OOF",
    "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE",
]
ANNIVERSAIRE = [
    "UID:anniv-1",
    "SUMMARY:Anniversaire de Paul",
    "DTSTART;VALUE=DATE:20261007",
    "DTEND;VALUE=DATE:20261008",
    "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
]


class AdresseTest(NomaiaTestCase):
    """Validation et protection de l'adresse secrete."""

    def test_webcal_ramene_en_https(self):
        self.assertTrue(source_model.normalize_url("webcal://h.example/c.ics").startswith("https://"))

    def test_http_en_clair_refuse(self):
        with self.assertRaises(SourceError):
            source_model.normalize_url("http://h.example/c.ics")

    def test_adresse_vide_refusee(self):
        with self.assertRaises(SourceError):
            source_model.create_source({"name": "Outlook", "url": " "})

    def test_adresse_jamais_renvoyee(self):
        source = source_model.create_source({"name": "Outlook", "url": URL})
        self.assertNotIn("url", source)
        self.assertNotIn("/abc/def/", source["url_hint"])
        self.assertEqual(source_model.get_url(source["id"]), URL)

    def test_adresse_chiffree_en_base(self):
        from models import secret

        source = source_model.create_source({"name": "Outlook", "url": URL})
        brut = get_db().execute(
            "SELECT url FROM calendar_source WHERE id = ?", (source["id"],)
        ).fetchone()["url"]
        if secret.available():
            self.assertNotIn("outlook", brut)

    def test_adresse_absente_des_sauvegardes(self):
        source_model.create_source({"name": "Outlook", "url": URL})
        copie = backup_db()
        conn = sqlite3.connect(copie)
        try:
            urls = [row[0] for row in conn.execute("SELECT url FROM calendar_source")]
        finally:
            conn.close()
        self.assertEqual(urls, [""])

    def test_modification_sans_adresse_la_conserve(self):
        source = source_model.create_source({"name": "Outlook", "url": URL})
        source_model.update_source(source["id"], {"name": "Pro", "url": ""})
        self.assertEqual(source_model.get_url(source["id"]), URL)


class AnalyseTest(NomaiaTestCase):
    """Lecture du flux : repetitions, statuts, journees entieres, fuseaux."""

    def _analyse(self, *evenements):
        return calendar_sync.parse(flux(*evenements), date(2026, 9, 25), date(2026, 11, 30))

    def test_serie_avec_exception_et_occurrence_deplacee(self):
        evenements, _ = self._analyse(SERIE_HEBDO, SERIE_DEPLACEE)
        debuts = [item["starts_at"].strftime("%Y-%m-%d %H:%M") for item in evenements]

        self.assertEqual(
            sorted(debuts),
            sorted(
                [
                    heure_locale(2026, 10, 5, 10, fuseau="Europe/Paris"),
                    heure_locale(2026, 10, 19, 15, fuseau="Europe/Paris"),
                    # Apres le passage a l'heure d'hiver : le decalage change.
                    heure_locale(2026, 10, 26, 10, fuseau="Europe/Paris"),
                ]
            ),
        )

    def test_libre_et_annule_ignores(self):
        evenements, _ = self._analyse(
            reunion("a", "Info", "20261006T080000Z", "20261006T090000Z", statut="FREE"),
            reunion("b", "Annule", "20261006T100000Z", "20261006T110000Z", extra=["STATUS:CANCELLED"]),
            reunion("c", "Vraie", "20261006T120000Z", "20261006T130000Z"),
        )
        self.assertEqual([item["title"] for item in evenements], ["Vraie"])

    def test_absent_journee_entiere_devient_jour_de_repos(self):
        evenements, absences = self._analyse(CONGES_OUTLOOK)
        self.assertEqual(evenements, [])
        self.assertEqual(absences[0]["start_day"], date(2026, 10, 20))
        # DTEND exclusive : le 23 n'est pas chome.
        self.assertEqual(absences[0]["end_day"], date(2026, 10, 22))

    def test_autre_journee_entiere_ignoree(self):
        evenements, absences = self._analyse(ANNIVERSAIRE)
        self.assertEqual((evenements, absences), ([], []))

    def test_absent_horaire_devient_absence(self):
        evenements, _ = self._analyse(
            reunion("d", "Medecin", "20261006T080000Z", "20261006T100000Z", statut="OOF")
        )
        self.assertEqual(evenements[0]["kind"], "absence")

    def test_reunion_a_cheval_sur_minuit_coupee(self):
        evenements, _ = self._analyse(
            reunion("e", "Deploiement", "20261006T220000Z", "20261007T020000Z")
        )
        self.assertEqual(len({item["starts_at"].date() for item in evenements}), len(evenements))
        total = sum((item["ends_at"] - item["starts_at"]).total_seconds() for item in evenements)
        self.assertEqual(total, 4 * 3600)

    def test_description_importee_et_resserree(self):
        evenements, _ = self._analyse(
            reunion(
                "f", "Point ALPHA", "20261006T080000Z", "20261006T090000Z",
                extra=[r"DESCRIPTION:Ordre du jour\n\n\n\nLien : https://teams.microsoft.com/l/abc"],
            )
        )
        self.assertEqual(
            evenements[0]["notes"], "Ordre du jour\n\nLien : https://teams.microsoft.com/l/abc"
        )

    def test_sans_description_notes_vides(self):
        evenements, _ = self._analyse(reunion("g", "Revue", "20261006T080000Z", "20261006T090000Z"))
        self.assertIsNone(evenements[0]["notes"])

    def test_flux_illisible(self):
        with self.assertRaises(SyncError):
            calendar_sync.parse(b"ceci n'est pas un agenda", date(2026, 10, 1), date(2026, 10, 2))


class SynchroTest(NomaiaTestCase):
    """Remplacement, lecture seule et resistance aux pannes."""

    def setUp(self):
        super().setUp()
        self.source = source_model.create_source({"name": "Outlook", "url": URL})

    def _synchro(self, *evenements):
        with patch.object(calendar_sync, "fetch", return_value=flux(*evenements)):
            return calendar_sync.sync_source(self.source["id"], AUJOURD_HUI)

    def _importes(self):
        return event_model.list_events(date(2026, 9, 1), date(2026, 12, 31))

    def test_import_puis_lecture_seule(self):
        issue = self._synchro(reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z"))
        self.assertTrue(issue["ok"])

        evenement = self._importes()[0]
        self.assertTrue(evenement["read_only"])
        self.assertEqual(evenement["source_name"], "Outlook")

        with self.assertRaises(EventError):
            event_model.update_event(evenement["id"], {"title": "Autre"})
        with self.assertRaises(EventError):
            event_model.delete_event(evenement["id"])

    def test_description_enregistree_en_notes(self):
        self._synchro(
            reunion(
                "d", "Revue", "20261006T090000Z", "20261006T100000Z",
                extra=["DESCRIPTION:Preparer le chiffrage"],
            )
        )
        self.assertEqual(self._importes()[0]["notes"], "Preparer le chiffrage")

    def test_supprime_dans_outlook_disparait(self):
        self._synchro(
            reunion("r1", "Garde", "20261006T090000Z", "20261006T100000Z"),
            reunion("r2", "Partie", "20261007T090000Z", "20261007T100000Z"),
        )
        self._synchro(reunion("r1", "Garde", "20261006T090000Z", "20261006T100000Z"))
        self.assertEqual([e["title"] for e in self._importes()], ["Garde"])

    def test_resynchro_ne_double_pas(self):
        flux_reunion = reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z")
        self._synchro(flux_reunion)
        self._synchro(flux_reunion)
        self.assertEqual(len(self._importes()), 1)

    def test_panne_reseau_garde_les_donnees(self):
        self._synchro(reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z"))
        with patch.object(calendar_sync, "fetch", side_effect=SyncError("hote injoignable.")):
            issue = calendar_sync.sync_source(self.source["id"], AUJOURD_HUI)

        self.assertFalse(issue["ok"])
        self.assertEqual(len(self._importes()), 1)
        source = source_model.get_source(self.source["id"])
        self.assertIn("injoignable", source["last_error"])
        self.assertEqual(source["last_count"], 1)

    def test_le_passe_hors_fenetre_est_conserve(self):
        self._synchro(reunion("v", "Vieille", "20260920T090000Z", "20260920T100000Z"))
        # Ancienne ligne d'un passage precedent, avant la fenetre (J-7).
        get_db().execute(
            "INSERT INTO event (title, starts_at, ends_at, kind, repeat, source_id)"
            " VALUES ('Vieille', '2026-09-20 09:00', '2026-09-20 10:00', 'meeting', 'none', ?)",
            (self.source["id"],),
        )
        get_db().commit()
        self._synchro()
        self.assertEqual([e["title"] for e in self._importes()], ["Vieille"])

    def test_conges_outlook_vident_le_plan(self):
        self._synchro(CONGES_OUTLOOK)
        plan = planner.build_plan(date(2026, 10, 21))
        self.assertEqual(plan["day_off"], "Conges")
        self.assertEqual(plan["capacity_min"], 0)

        absence = day_off_model.list_between(date(2026, 10, 20), date(2026, 10, 20))[0]
        self.assertTrue(absence["read_only"])
        with self.assertRaises(DayOffError):
            day_off_model.delete_day_off(absence["id"])

    def test_reunion_importee_retire_du_temps(self):
        jour = date(2026, 10, 6)
        sans = planner.build_plan(jour)["capacity_min"]
        debut = datetime(2026, 10, 6, 10, 0).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fin = datetime(2026, 10, 6, 11, 0).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._synchro(reunion("r", "Revue", debut, fin))
        self.assertEqual(planner.build_plan(jour)["capacity_min"], sans - 60)

    def test_supprimer_la_source_retire_ses_lignes(self):
        self._synchro(
            reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z"), CONGES_OUTLOOK
        )
        source_model.delete_source(self.source["id"])
        self.assertEqual(self._importes(), [])
        self.assertEqual(day_off_model.list_between(date(2026, 10, 1), date(2026, 10, 31)), [])

    def test_le_chat_ne_peut_pas_annuler(self):
        self._synchro(reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z"))
        evenement = self._importes()[0]

        liste = tools.execute("list_events", {"date": "2026-10-06", "days": 1})
        self.assertTrue(liste["data"]["events"][0]["read_only"])

        resultat = tools.execute("cancel_event", {"event_id": evenement["id"]})
        self.assertFalse(resultat["ok"])
        self.assertIn("Outlook", resultat["error"])

    def test_echeance_de_synchro(self):
        with patch.object(calendar_sync, "sync_source") as synchro:
            calendar_sync.sync_due(datetime(2026, 10, 2, 9, 0))
            self.assertEqual(synchro.call_count, 1)

        source_model.record_sync(self.source["id"], "2026-10-02 09:00", count=0)
        with patch.object(calendar_sync, "sync_source") as synchro:
            calendar_sync.sync_due(datetime(2026, 10, 2, 9, 10))
            self.assertEqual(synchro.call_count, 0)
            calendar_sync.sync_due(datetime(2026, 10, 2, 9, 20))
            self.assertEqual(synchro.call_count, 1)

    def test_desactiver_retire_les_importes(self):
        self._synchro(
            reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z"), CONGES_OUTLOOK
        )
        source_model.update_source(self.source["id"], {"enabled": False})

        self.assertEqual(self._importes(), [])
        self.assertEqual(day_off_model.list_between(date(2026, 10, 1), date(2026, 10, 31)), [])

    def test_synchro_manuelle_refusee_si_desactivee(self):
        source_model.update_source(self.source["id"], {"enabled": False})
        reponse = self.client().post(
            f"/api/calendar-sources/{self.source['id']}/sync", headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_source_desactivee_ignoree(self):
        source_model.update_source(self.source["id"], {"enabled": False})
        with patch.object(calendar_sync, "sync_source") as synchro:
            calendar_sync.sync_due(datetime(2026, 10, 2, 9, 0))
        synchro.assert_not_called()


class ApiAgendaExterneTest(NomaiaTestCase):
    """Routes /api/calendar-sources."""

    def test_creer_synchronise_aussitot(self):
        with patch.object(
            calendar_sync, "fetch",
            return_value=flux(reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z")),
        ):
            reponse = self.client().post(
                "/api/calendar-sources", json={"name": "Outlook", "url": URL},
                headers=self.write_headers(),
            )
        self.assertEqual(reponse.status_code, 201)
        corps = reponse.get_json()
        self.assertTrue(corps["sync"]["ok"])
        self.assertNotIn(URL, reponse.get_data(as_text=True))

    def test_adresse_invalide(self):
        reponse = self.client().post(
            "/api/calendar-sources", json={"name": "Outlook", "url": "ftp://x"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.status_code, 400)

    def test_synchro_en_echec_reste_une_creation(self):
        with patch.object(calendar_sync, "fetch", side_effect=SyncError("HTTP 404.")):
            reponse = self.client().post(
                "/api/calendar-sources", json={"name": "Outlook", "url": URL},
                headers=self.write_headers(),
            )
        self.assertEqual(reponse.status_code, 201)
        self.assertFalse(reponse.get_json()["sync"]["ok"])

    def test_supprimer_un_evenement_importe_repond_400(self):
        source = source_model.create_source({"name": "Outlook", "url": URL})
        with patch.object(
            calendar_sync, "fetch",
            return_value=flux(reunion("r", "Revue", "20261006T090000Z", "20261006T100000Z")),
        ):
            calendar_sync.sync_source(source["id"], AUJOURD_HUI)
        evenement = event_model.list_events(date(2026, 10, 6), date(2026, 10, 6))[0]

        reponse = self.client().delete(
            f"/api/events/{evenement['id']}", headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 400)

    def test_lister_supprimer(self):
        source = source_model.create_source({"name": "Outlook", "url": URL})
        client = self.client()
        self.assertEqual(len(client.get("/api/calendar-sources").get_json()["sources"]), 1)
        reponse = client.delete(
            f"/api/calendar-sources/{source['id']}", headers=self.write_headers()
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(client.get("/api/calendar-sources").get_json()["sources"], [])
