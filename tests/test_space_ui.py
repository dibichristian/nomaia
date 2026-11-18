"""Tests du selecteur d'espace de l'interface (Tout / Pro / Perso).

Le choix est un reglage global en base. Il ne filtre que les ecrans qui le
demandent (`space=current` : Taches, Projets) ; plan du jour, chat et Bilan
continuent de tout voir.
"""
from datetime import date

from models import project as project_model
from models import setting, stats
from models import task as task_model
from services import project_board
from tests.base import NomaiaTestCase


class SpaceFilterTest(NomaiaTestCase):
    """Un projet et une tache dans chaque espace, plus une tache perso sans projet."""

    def setUp(self):
        super().setUp()
        self.perso = project_model.create_project("NOMAIA", space="perso")
        self.pro = project_model.create_project("ALPHA")
        self.t_perso = task_model.create_task({"title": "Ajouter le selecteur", "project_id": self.perso["id"]})
        self.t_pro = task_model.create_task({"title": "Migrer les factures", "project_id": self.pro["id"]})
        self.t_libre = task_model.create_task({"title": "Courses", "space": "perso"})

    def _choisir(self, space):
        return self.client().patch(
            "/api/settings", json={"space_filter": space}, headers=self.write_headers()
        )

    # --- Reglage ---------------------------------------------------------

    def test_defaut_tout(self):
        self.assertIsNone(setting.space_filter())
        self.assertIsNone(setting.resolve_space("current"))

    def test_choix_enregistre_et_valeur_inconnue_refusee(self):
        self.assertEqual(self._choisir("perso").status_code, 200)
        self.assertEqual(setting.space_filter(), "perso")
        self.assertEqual(self._choisir("vacances").status_code, 400)
        self.assertEqual(setting.space_filter(), "perso")

    def test_reglage_corrompu_vaut_tout(self):
        setting.set_value("space_filter", "n'importe quoi")
        self.assertIsNone(setting.space_filter())

    def test_resolve_space(self):
        setting.set_value("space_filter", "pro")
        self.assertEqual(setting.resolve_space("current"), "pro")
        self.assertEqual(setting.resolve_space("perso"), "perso")
        self.assertIsNone(setting.resolve_space(None))
        self.assertIsNone(setting.resolve_space("autre"))

    # --- Barre du haut ---------------------------------------------------

    def test_barre_du_haut_marque_l_espace_choisi(self):
        setting.set_value("space_filter", "perso")
        html = self.client().get("/plan").get_data(as_text=True)
        self.assertIn('id="space-switch"', html)
        self.assertIn('data-current="perso"', html)
        self.assertIn('class="chip is-on"\n                      data-space="perso"', html)

    # --- Taches ----------------------------------------------------------

    def test_liste_des_taches_filtree_sur_demande(self):
        setting.set_value("space_filter", "perso")
        donnees = self.client().get("/api/tasks?space=current").get_json()
        titres = {tache["title"] for tache in donnees["tasks"]}
        self.assertEqual(titres, {"Ajouter le selecteur", "Courses"})
        self.assertEqual(donnees["stats"]["todo_count"], 2)

    def test_sans_parametre_le_perimetre_s_applique(self):
        # Depuis la separation complete (lot 3), toute requete de l'interface
        # suit le selecteur ; space=current reste accepte.
        setting.set_value("space_filter", "perso")
        donnees = self.client().get("/api/tasks").get_json()
        self.assertEqual(len(donnees["tasks"]), 2)
        self.assertEqual(donnees["stats"]["todo_count"], 2)

    def test_tout_affiche_tout(self):
        donnees = self.client().get("/api/tasks?space=current").get_json()
        self.assertEqual(len(donnees["tasks"]), 3)

    def test_menus_deroulants_de_taches_filtres(self):
        setting.set_value("space_filter", "pro")
        html = self.client().get("/taches").get_data(as_text=True)
        self.assertIn(">ALPHA</option>", html)
        self.assertNotIn(">NOMAIA</option>", html)

    def test_tache_sans_projet_creee_dans_l_espace_demande(self):
        reponse = self.client().post(
            "/api/tasks", json={"title": "Reviser le velo", "space": "perso"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["space"], "perso")

    def test_espace_d_une_tache_sans_projet_modifiable(self):
        reponse = self.client().patch(
            f"/api/tasks/{self.t_libre['id']}", json={"space": "pro"},
            headers=self.write_headers(),
        )
        self.assertEqual(reponse.get_json()["space"], "pro")

    # --- Projets ---------------------------------------------------------

    def test_tableau_des_projets_filtre(self):
        setting.set_value("space_filter", "pro")
        donnees = self.client().get("/api/projects/board?space=current").get_json()
        self.assertEqual([carte["name"] for carte in donnees["projects"]], ["ALPHA"])
        self.assertEqual(donnees["kpis"]["open_tasks"], 1)

    def test_tableau_des_projets_complet_sans_parametre(self):
        setting.set_value("space_filter", "pro")
        noms = {carte["name"] for carte in project_board.overview()["projects"]}
        self.assertEqual(noms, {"ALPHA", "NOMAIA"})

    def test_liste_des_projets_filtree(self):
        setting.set_value("space_filter", "perso")
        donnees = self.client().get("/api/projects?space=current").get_json()
        self.assertEqual([projet["name"] for projet in donnees["projects"]], ["NOMAIA"])

    def test_resume_de_periode_par_espace(self):
        jour = date.today().isoformat()
        self.complete_task(self.t_perso["id"], 45)
        tout = stats.period_summary(jour, jour)
        perso = stats.period_summary(jour, jour, space="perso")
        pro = stats.period_summary(jour, jour, space="pro")
        self.assertEqual((tout["done"], tout["tracked_min"], tout["open_tasks"]), (1, 45, 2))
        self.assertEqual((perso["done"], perso["tracked_min"], perso["open_tasks"]), (1, 45, 1))
        self.assertEqual((pro["done"], pro["tracked_min"], pro["open_tasks"]), (0, 0, 1))
        self.assertEqual(tout["created"], perso["created"] + pro["created"])

    # --- Plan du jour ----------------------------------------------------

    def test_plan_du_jour_calcule_sur_tout_et_masque(self):
        setting.set_value("space_filter", "pro")
        donnees = self.client().get("/api/plan").get_json()
        espaces = {}

        def parcourir(noeud):
            if isinstance(noeud, dict):
                tache = noeud.get("task")
                if isinstance(tache, dict) and "space" in tache:
                    espaces[tache["title"]] = tache["space"]
                for valeur in noeud.values():
                    parcourir(valeur)
            elif isinstance(noeud, list):
                for valeur in noeud:
                    parcourir(valeur)

        parcourir(donnees)
        # Depuis le lot 3, le creneau perso garde sa place mais perd son detail.
        self.assertEqual(espaces.get("Occupé · Perso"), "perso")
        self.assertNotIn("Ajouter le selecteur", espaces)
        self.assertEqual(espaces.get("Migrer les factures"), "pro")
