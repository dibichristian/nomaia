"""Coquille de l'interface (lot 1 du design) : gabarit, navigation, palette, badge."""
import json
import re

from models import capture as capture_model
from models import get_db
from tests.base import NomaiaTestCase

PAGES = ["/", "/chat", "/plan", "/agenda", "/projets", "/contacts", "/bilan", "/config", "/taches"]


class CoquilleTest(NomaiaTestCase):
    def test_toutes_les_pages_rendent_la_coquille(self):
        client = self.client()
        for url in PAGES:
            with self.subTest(url=url):
                reponse = client.get(url)
                self.assertEqual(reponse.status_code, 200)
                html = reponse.get_data(as_text=True)
                for marqueur in ('class="sidebar"', 'id="cmdk"', 'id="theme-toggle"', 'id="chrono-pill"',
                                 'id="main-nav"', "css/tokens.css", "js/shell.js"):
                    self.assertIn(marqueur, html)
                # L'ancienne couche d'ambiance (orbes flous) a disparu.
                self.assertNotIn('class="orb', html)

    def test_page_active_marquee_dans_la_barre_laterale(self):
        html = self.client().get("/plan").get_data(as_text=True)
        actifs = re.findall(r'class="side-link active"[^>]*aria-current="page"', html)
        self.assertEqual(len(actifs), 1)
        self.assertIn("Plan du jour", html[html.index('class="side-link active"'):][:600])

    def test_bulle_mobile_garde_ses_quatre_pages(self):
        html = self.client().get("/").get_data(as_text=True)
        nav = html[html.index('id="main-nav"'):html.index('id="nav-sheet"')]
        self.assertEqual(nav.count('class="nav-link'), 5)  # 4 pages + « Plus »
        for court in ("Réception", "Chat", "Plan", "Tâches"):
            self.assertIn(court, nav)

    def test_palette_connait_les_pages_du_menu(self):
        html = self.client().get("/").get_data(as_text=True)
        bloc = re.search(r'<script id="cmdk-pages" type="application/json">(.*?)</script>', html, re.S)
        pages = json.loads(bloc.group(1))
        self.assertEqual({p["url"] for p in pages},
                         {"/plan", "/taches", "/agenda", "/", "/projets", "/bilan", "/contacts", "/notes",
                          "/comptes-rendus", "/chat", "/config"})
        self.assertTrue(all(p["label"] and p["group"] for p in pages))

    def test_theme_applique_avant_le_rendu(self):
        html = self.client().get("/").get_data(as_text=True)
        tete = html[:html.index("css/tokens.css")]
        self.assertIn('localStorage.getItem("nomaia.theme")', tete)

    def test_config_propose_les_preferences_d_affichage(self):
        html = self.client().get("/config").get_data(as_text=True)
        for pref in ('data-pref="theme"', 'data-pref="ambient"', 'data-pref="motion"'):
            self.assertIn(pref, html)

    def test_config_rangee_en_sections(self):
        html = self.client().get("/config").get_data(as_text=True)
        sections = ("general", "intelligence", "journee", "suivi", "connexions", "consommation")
        for section in sections:
            self.assertIn(f'href="#{section}" data-group="{section}"', html)
            self.assertIn(f'id="cfg-{section}"', html)
        # Les dix-sept cartes restent, chacune dans une section.
        self.assertEqual(html.count('class="card config-card"'), 17)
        self.assertEqual(html.count('class="cfg-group"'), len(sections))

    def test_statut_compte_les_entrees_a_traiter(self):
        client = self.client()
        self.assertEqual(client.get("/api/status").get_json()["inbox_pending"], 0)
        db = get_db()
        capture_id = db.execute("INSERT INTO capture (raw_text) VALUES ('test')").lastrowid
        db.execute("INSERT INTO proposal (capture_id, payload_json, status) VALUES (?, '{}', 'pending')", (capture_id,))
        db.execute("INSERT INTO proposal (capture_id, payload_json, status) VALUES (?, '{}', 'applied')", (capture_id,))
        db.commit()
        self.assertEqual(capture_model.count_pending_proposals(), 1)
        self.assertEqual(client.get("/api/status").get_json()["inbox_pending"], 1)

    def test_page_acces_refuse_utilise_le_nouveau_fond(self):
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('class="aurora"', html)
        self.assertNotIn('class="orb', html)


class FeuillesParPageTest(NomaiaTestCase):
    ATTENDU = {"/": ["inbox"], "/plan": ["plan"], "/agenda": ["agenda"], "/contacts": ["contacts"],
               "/chat": ["chat"], "/config": ["config"], "/projets": ["pilot", "projets"],
               "/bilan": ["pilot", "projets", "bilan"], "/taches": []}

    def test_chaque_page_charge_ses_feuilles_et_les_communes(self):
        client = self.client()
        for url, feuilles in self.ATTENDU.items():
            with self.subTest(url=url):
                html = client.get(url).get_data(as_text=True)
                for commune in ("pages/shared.css", "pages/tasks.css"):
                    self.assertIn(commune, html)
                chargees = sorted(set(re.findall(r"css/pages/(\w+)\.css", html)) - {"shared", "tasks"})
                self.assertEqual(chargees, sorted(feuilles))
                self.assertNotIn("css/style.css", html)


class ImpressionTest(NomaiaTestCase):
    def test_feuille_d_impression_et_boutons(self):
        client = self.client()
        self.assertIn('css/print.css" media="print"', client.get("/plan").get_data(as_text=True))
        self.assertIn("window.print()", client.get("/bilan").get_data(as_text=True))


class CloCheEtBarreLateraleTest(NomaiaTestCase):
    def test_notifications(self):
        from datetime import date, datetime, timedelta

        from models import get_db

        db = get_db()
        today = date.today().isoformat()
        db.execute("INSERT INTO task (title, deadline) VALUES ('Livrer le PV', ?)", (today,))
        db.execute("INSERT INTO task (title, deadline) VALUES ('Vieux', ?)", ((date.today() - timedelta(days=2)).isoformat(),))
        soon = (datetime.now() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M")
        if soon[:10] == today:  # pas d'evenement a cheval sur minuit
            db.execute("INSERT INTO event (title, starts_at, ends_at) VALUES ('Point client', ?, ?)", (soon, soon))
        db.commit()
        items = self.client().get("/api/notifications").get_json()["items"]
        textes = [item["text"] for item in items]
        self.assertEqual(items[0]["level"], "danger")
        self.assertIn("1 tâche en retard", textes)
        self.assertIn("Échéance aujourd'hui : Livrer le PV", textes)
        if soon[:10] == today:
            self.assertTrue(any(t.startswith("Point client dans") for t in textes))

    def test_logo_vivant_dans_la_coquille(self):
        html = self.client().get("/plan").get_data(as_text=True)
        # Logo de la barre laterale et anneau de la barre du haut, chacun avec
        # son propre degrade : deux identifiants distincts dans la page.
        self.assertEqual(html.count('class="brand brand-live"'), 2)
        self.assertIn('id="logo-side-g"', html)
        self.assertIn('id="logo-top-g"', html)
        self.assertIn('class="o logo-ring"', html)
        self.assertIn('class="i logo-dot"', html)
        self.assertNotIn('class="brand-mark"', html)

    def test_page_d_acces_porte_le_logo_complet(self):
        html = self.app.test_client().get("/plan").get_data(as_text=True)
        self.assertIn('class="access-logo"', html)
        self.assertIn('class="check logo-detail"', html)

    def test_barre_laterale_porte_le_nom_et_l_interrupteur(self):
        from models import setting

        setting.set_value("display_name", "Lucie Bernard")
        html = self.client().get("/plan").get_data(as_text=True)
        self.assertIn("Lucie Bernard", html)
        self.assertIn(">LB<", html)
        self.assertIn('class="switch side-switch"', html)
        self.assertIn('id="notif-button"', html)
