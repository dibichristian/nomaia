"""Tests du plan de modifications : confirmation, execution, doublons.

La garantie centrale est que la regle de confirmation vit cote serveur : un
modele qui l'ignore ne doit pas pouvoir modifier la base en douce.
"""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from models import chat as chat_model
from models import event as event_model
from models import person as person_model
from models import task as task_model
from services import assistant, tools
from tests.base import NomaiaTestCase


def scenario(*reponses):
    """Retourne un faux llm.ask rendant les reponses fournies, dans l'ordre."""
    etat = {"appels": 0, "prompts": []}

    def faux_ask(prompt, **kwargs):
        etat["prompts"].append(prompt)
        index = min(etat["appels"], len(reponses) - 1)
        etat["appels"] += 1
        return reponses[index]

    faux_ask.etat = etat
    return faux_ask


class NormalizePlanTest(NomaiaTestCase):
    """Le plan propose par le modele est une donnee hostile."""

    def test_etape_valide_retenue(self):
        etapes = assistant._normalize_plan(
            [{"tool": "create_task", "args": {"title": "X"}, "label": "Creer X"}]
        )
        self.assertEqual(len(etapes), 1)
        self.assertEqual(etapes[0]["label"], "Creer X")
        self.assertFalse(etapes[0]["destructive"])

    def test_outil_de_lecture_ecarte(self):
        # Un plan n'est pas un moyen detourne de lire.
        self.assertEqual(assistant._normalize_plan([{"tool": "list_tasks", "args": {}}]), [])

    def test_outil_inconnu_ecarte(self):
        self.assertEqual(assistant._normalize_plan([{"tool": "lancer_missiles"}]), [])

    def test_entree_mal_typee_ecartee(self):
        self.assertEqual(assistant._normalize_plan(["pas un objet", 42]), [])
        self.assertEqual(assistant._normalize_plan("pas une liste"), [])
        self.assertEqual(assistant._normalize_plan(None), [])

    def test_outil_destructif_marque(self):
        etapes = assistant._normalize_plan([{"tool": "delete_task", "args": {"task_id": 1}}])
        self.assertTrue(etapes[0]["destructive"])

    def test_label_fabrique_quand_le_modele_l_oublie(self):
        # L'utilisateur doit toujours lire ce qu'il valide, jamais un nom d'outil nu.
        etapes = assistant._normalize_plan(
            [{"tool": "create_task", "args": {"title": "Relire le devis"}}]
        )
        self.assertIn("Relire le devis", etapes[0]["label"])

    def test_nombre_d_etapes_plafonne(self):
        brut = [{"tool": "create_task", "args": {"title": f"T{i}"}} for i in range(20)]
        self.assertEqual(len(assistant._normalize_plan(brut)), assistant.MAX_PLAN_STEPS)


class ConfirmationRuleTest(NomaiaTestCase):
    """La regle vit cote serveur, pas dans le prompt."""

    @staticmethod
    def _etape(nom="create_task", destructive=False):
        return {"tool": nom, "args": {}, "label": nom, "destructive": destructive}

    def test_une_ecriture_simple_part_sans_question(self):
        self.assertFalse(assistant.needs_confirmation([self._etape()]))

    def test_deux_ecritures_exigent_confirmation(self):
        self.assertTrue(assistant.needs_confirmation([self._etape(), self._etape()]))

    def test_une_ecriture_destructive_exige_confirmation(self):
        self.assertTrue(assistant.needs_confirmation([self._etape("delete_task", True)]))

    def test_plan_vide_ne_demande_rien(self):
        self.assertFalse(assistant.needs_confirmation([]))


class ConfirmationWordsTest(unittest.TestCase):
    """Un oui ne doit couter ni jeton ni attente."""

    def test_oui_reconnus(self):
        for mot in ("oui", "OK", "vas-y", "d'accord", "confirme", "Oui."):
            self.assertIs(assistant._confirmation(mot), True, mot)

    def test_non_reconnus(self):
        for mot in ("non", "annule", "Stop", "laisse"):
            self.assertIs(assistant._confirmation(mot), False, mot)

    def test_autre_message_indecis(self):
        self.assertIsNone(assistant._confirmation("plutot 11h"))
        self.assertIsNone(assistant._confirmation("oui mais decale a jeudi"))


class PlanFlowTest(NomaiaTestCase):
    """Parcours complet : proposition, attente, confirmation."""

    def test_une_ecriture_simple_s_execute_directement(self):
        faux = scenario(
            {
                "plan": [{"tool": "create_task", "args": {"title": "Relire le devis"},
                          "label": "Creer la tache"}],
                "reply": "Je cree la tache.",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("cree une tache relire le devis")

        self.assertIsNone(resultat["pending"])
        self.assertEqual(len(task_model.list_tasks()), 1)
        self.assertIsNone(chat_model.pending_plan())

    def test_deux_ecritures_attendent_le_feu_vert(self):
        tache = task_model.create_task({"title": "Planifier la reunion"})
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": tache["id"]},
                     "label": "Terminer « Planifier la reunion »"},
                    {"tool": "create_task", "args": {"title": "Preparer le support"},
                     "label": "Creer « Preparer le support »"},
                ],
                "reply": "Je vais faire deux choses. J'y vais ?",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("la reunion est pour demain 10h")

        self.assertIsNotNone(resultat["pending"])
        self.assertEqual(len(resultat["pending"]["steps"]), 2)
        # Rien n'a encore bouge.
        self.assertEqual(task_model.get_task(tache["id"])["status"], "todo")
        self.assertEqual(len(task_model.list_tasks()), 1)

    def test_le_oui_applique_sans_rappeler_le_modele(self):
        tache = task_model.create_task({"title": "Planifier la reunion"})
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": tache["id"]},
                     "label": "Terminer la planification"},
                    {"tool": "create_task", "args": {"title": "Preparer le support"},
                     "label": "Creer la preparation"},
                ],
                "reply": "J'y vais ?",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            assistant.reply_to("la reunion est pour demain 10h")
            appels_avant = faux.etat["appels"]
            resultat = assistant.reply_to("oui")

        self.assertEqual(faux.etat["appels"], appels_avant, "le oui ne doit rien coute")
        self.assertEqual(task_model.get_task(tache["id"])["status"], "done")
        self.assertEqual(len(task_model.list_tasks(status="todo")), 1)
        self.assertIsNone(chat_model.pending_plan())

    def test_le_non_n_applique_rien(self):
        tache = task_model.create_task({"title": "Planifier la reunion"})
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": tache["id"]}, "label": "A"},
                    {"tool": "create_task", "args": {"title": "B"}, "label": "B"},
                ],
                "reply": "J'y vais ?",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            assistant.reply_to("fais le necessaire")
            resultat = assistant.reply_to("non")

        self.assertIn("Annule", resultat["reply"])
        self.assertEqual(task_model.get_task(tache["id"])["status"], "todo")
        self.assertIsNone(chat_model.pending_plan())

    def test_un_autre_message_abandonne_le_plan(self):
        tache = task_model.create_task({"title": "Planifier la reunion"})
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": tache["id"]}, "label": "A"},
                    {"tool": "create_task", "args": {"title": "B"}, "label": "B"},
                ],
                "reply": "J'y vais ?",
            },
            {"reply": "D'accord, autre chose."},
        )
        with patch.object(assistant.llm, "ask", faux):
            assistant.reply_to("fais le necessaire")
            assistant.reply_to("plutot 11h en fait")

        self.assertIsNone(chat_model.pending_plan())
        self.assertEqual(task_model.get_task(tache["id"])["status"], "todo")

    def test_une_ecriture_directe_reste_soumise_a_la_regle(self):
        # Le modele oublie le format du plan et appelle l'outil destructeur
        # directement : la confirmation doit quand meme s'imposer.
        tache = task_model.create_task({"title": "Une tache"})
        faux = scenario({"tool": "delete_task", "args": {"task_id": tache["id"]}})

        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("supprime la tache 1")

        self.assertIsNotNone(resultat["pending"])
        self.assertIsNotNone(task_model.get_task(tache["id"]))

    def test_une_ecriture_directe_anodine_passe(self):
        faux = scenario({"tool": "create_task", "args": {"title": "Directe"}})
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("cree une tache")

        self.assertIsNone(resultat["pending"])
        self.assertEqual(len(task_model.list_tasks()), 1)

    def test_une_etape_en_echec_n_arrete_pas_les_suivantes(self):
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": 999}, "label": "Tache absente"},
                    {"tool": "create_task", "args": {"title": "Celle-ci passe"}, "label": "Creer"},
                ],
                "reply": "J'y vais ?",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            assistant.reply_to("fais les deux")
            resultat = assistant.reply_to("oui")

        self.assertIn("ECHEC", resultat["reply"])
        self.assertEqual(len(task_model.list_tasks()), 1)

    def test_plan_perime_ne_s_applique_pas(self):
        tache = task_model.create_task({"title": "Une tache"})
        chat_model.set_plan(
            [{"tool": "delete_task", "args": {"task_id": tache["id"]},
              "label": "Supprimer", "destructive": True}],
            "J'y vais ?",
        )

        vieux = datetime.now() + timedelta(minutes=chat_model.PLAN_MAX_AGE_MIN + 5)
        self.assertIsNone(chat_model.pending_plan(now=vieux))

        faux = scenario({"reply": "Je ne sais pas de quoi tu parles."})
        with patch.object(assistant.llm, "ask", faux):
            with patch.object(chat_model, "pending_plan", lambda now=None, conversation_id=None: None):
                assistant.reply_to("oui")

        self.assertIsNotNone(task_model.get_task(tache["id"]))

    def test_le_texte_du_plan_detaille_les_etapes(self):
        # Sur Telegram il n'y a que ce texte : « J'y vais ? » tout seul ne dit
        # pas de quoi il s'agit.
        tache = task_model.create_task({"title": "Planifier"})
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": tache["id"]},
                     "label": "Terminer « Planifier »"},
                    {"tool": "delete_task", "args": {"task_id": tache["id"]},
                     "label": "Supprimer « Planifier »"},
                ],
                "reply": "Deux actions.",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            resultat = assistant.reply_to("fais le necessaire")

        texte = resultat["reply"]
        self.assertIn("Terminer « Planifier »", texte)
        self.assertIn("Supprimer « Planifier »", texte)
        self.assertIn("destructif", texte)
        self.assertIn("oui", texte)

    def test_un_nouveau_plan_remplace_le_precedent(self):
        chat_model.set_plan([{"tool": "create_task", "args": {}, "label": "A"}], "un")
        second = chat_model.set_plan([{"tool": "create_task", "args": {}, "label": "B"}], "deux")
        en_attente = chat_model.pending_plan()
        self.assertEqual(en_attente["id"], second["id"])


class DuplicateTest(NomaiaTestCase):
    """Detection et fusion : le temps chronometre ne doit jamais disparaitre."""

    def test_titres_proches_rapproches(self):
        task_model.create_task({"title": "Corriger le crash de la paie"})
        task_model.create_task({"title": "Corriger crash paie"})
        groupes = task_model.find_duplicates()
        self.assertEqual(len(groupes), 1)
        self.assertEqual(len(groupes[0]), 2)

    def test_titres_sans_rapport_non_rapproches(self):
        task_model.create_task({"title": "Corriger le crash de la paie"})
        task_model.create_task({"title": "Preparer la demo client"})
        self.assertEqual(task_model.find_duplicates(), [])

    def test_les_mots_vides_ne_creent_pas_de_faux_doublons(self):
        task_model.create_task({"title": "Voir le point avec le client"})
        task_model.create_task({"title": "Faire la tache pour le sujet"})
        self.assertEqual(task_model.find_duplicates(), [])

    def test_taches_terminees_ignorees(self):
        premiere = task_model.create_task({"title": "Corriger le crash paie"})
        task_model.create_task({"title": "Corriger crash paie"})
        task_model.toggle_task(premiere["id"])
        self.assertEqual(task_model.find_duplicates(), [])

    def test_la_fusion_conserve_le_temps_chronometre(self):
        gardee = task_model.create_task({"title": "Corriger le crash paie"})
        doublon = task_model.create_task({"title": "Corriger crash paie"})
        self.complete_task(doublon["id"], tracked_min=45)

        task_model.merge_tasks(gardee["id"], [doublon["id"]])

        survivante = task_model.get_task(gardee["id"])
        self.assertIsNotNone(survivante)
        self.assertEqual(survivante["tracked_min"], 45)
        self.assertIsNone(task_model.get_task(doublon["id"]))

    def test_la_fusion_garde_l_echeance_la_plus_proche(self):
        proche = (date.today() + timedelta(days=1)).isoformat()
        lointaine = (date.today() + timedelta(days=10)).isoformat()
        gardee = task_model.create_task({"title": "Corriger le crash", "deadline": lointaine})
        doublon = task_model.create_task({"title": "Corriger crash", "deadline": proche})

        fusionnee = task_model.merge_tasks(gardee["id"], [doublon["id"]])
        self.assertEqual(fusionnee["deadline"], proche)

    def test_la_fusion_garde_la_priorite_la_plus_haute(self):
        gardee = task_model.create_task({"title": "Corriger le crash", "priority": 4})
        doublon = task_model.create_task({"title": "Corriger crash", "priority": 1})
        self.assertEqual(task_model.merge_tasks(gardee["id"], [doublon["id"]])["priority"], 1)

    def test_la_fusion_concatene_les_descriptions(self):
        gardee = task_model.create_task({"title": "Corriger", "description": "Vu en recette"})
        doublon = task_model.create_task({"title": "Corriger", "description": "Signale par ORION"})
        fusionnee = task_model.merge_tasks(gardee["id"], [doublon["id"]])
        self.assertIn("Vu en recette", fusionnee["description"])
        self.assertIn("Signale par ORION", fusionnee["description"])

    def test_la_fusion_recupere_projet_et_destinataire(self):
        from models import project as project_model

        projet = project_model.create_project("ORION")
        personne = person_model.create_person({"name": "Paul"})
        gardee = task_model.create_task({"title": "Corriger"})
        doublon = task_model.create_task(
            {"title": "Corriger", "project_id": projet["id"], "person_id": personne["id"]}
        )

        fusionnee = task_model.merge_tasks(gardee["id"], [doublon["id"]])
        self.assertEqual(fusionnee["project_id"], projet["id"])
        self.assertEqual(fusionnee["person_id"], personne["id"])

    def test_la_fusion_reattribue_les_attentes_bloquees(self):
        personne = person_model.create_person({"name": "Paul"})
        gardee = task_model.create_task({"title": "Corriger"})
        doublon = task_model.create_task({"title": "Corriger"})
        attente = person_model.create_waiting(
            {"person_id": personne["id"], "subject": "Les specs", "task_id": doublon["id"]}
        )

        task_model.merge_tasks(gardee["id"], [doublon["id"]])
        self.assertEqual(person_model.get_waiting(attente["id"])["task_id"], gardee["id"])

    def test_fusionner_avec_soi_meme_refuse(self):
        tache = task_model.create_task({"title": "Corriger"})
        with self.assertRaises(task_model.ValidationError):
            task_model.merge_tasks(tache["id"], [tache["id"]])

    def test_fusionner_une_tache_absente_refuse(self):
        tache = task_model.create_task({"title": "Corriger"})
        with self.assertRaises(task_model.ValidationError):
            task_model.merge_tasks(tache["id"], [999])

    def test_trois_doublons_forment_un_seul_groupe(self):
        for _ in range(3):
            task_model.create_task({"title": "Corriger le crash de la paie"})
        groupes = task_model.find_duplicates()
        self.assertEqual(len(groupes), 1)
        self.assertEqual(len(groupes[0]), 3)


class NewToolsTest(NomaiaTestCase):
    """Outils ajoutes : doublons, suppression, edition des interlocuteurs."""

    def test_detection_par_l_outil(self):
        task_model.create_task({"title": "Corriger le crash de la paie"})
        task_model.create_task({"title": "Corriger crash paie"})
        resultat = tools.execute("find_duplicates", {})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["count"], 1)

    def test_aucun_doublon_n_est_pas_une_erreur(self):
        task_model.create_task({"title": "Une seule tache"})
        resultat = tools.execute("find_duplicates", {})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(resultat["data"]["count"], 0)

    def test_fusion_par_l_outil(self):
        gardee = task_model.create_task({"title": "Corriger le crash"})
        doublon = task_model.create_task({"title": "Corriger crash"})
        resultat = tools.execute(
            "merge_tasks", {"keep_id": gardee["id"], "merge_ids": [doublon["id"]]}
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertIsNone(task_model.get_task(doublon["id"]))

    def test_fusion_accepte_un_identifiant_seul(self):
        gardee = task_model.create_task({"title": "Corriger le crash"})
        doublon = task_model.create_task({"title": "Corriger crash"})
        resultat = tools.execute(
            "merge_tasks", {"keep_id": gardee["id"], "merge_ids": doublon["id"]}
        )
        self.assertTrue(resultat["ok"], resultat)

    def test_suppression_par_l_outil(self):
        tache = task_model.create_task({"title": "A supprimer"})
        resultat = tools.execute("delete_task", {"task_id": tache["id"]})
        self.assertTrue(resultat["ok"], resultat)
        self.assertIsNone(task_model.get_task(tache["id"]))

    def test_modification_d_un_interlocuteur(self):
        person_model.create_person({"name": "Paul"})
        resultat = tools.execute("update_person", {"name": "Paul", "role": "Directeur"})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(person_model.find_by_name("Paul")["role"], "Directeur")

    def test_interlocuteur_inconnu_oriente_vers_la_liste(self):
        resultat = tools.execute("update_person", {"name": "Fantome", "role": "X"})
        self.assertFalse(resultat["ok"])
        self.assertIn("list_people", resultat["error"])

    def test_modification_d_une_attente(self):
        personne = person_model.create_person({"name": "Paul"})
        attente = person_model.create_waiting(
            {"person_id": personne["id"], "subject": "Les specs"}
        )
        resultat = tools.execute(
            "update_waiting", {"waiting_id": attente["id"], "subject": "Les specs v2"}
        )
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(person_model.get_waiting(attente["id"])["subject"], "Les specs v2")

    def test_le_catalogue_cite_les_nouveaux_outils(self):
        catalogue = tools.describe()
        for nom in ("find_duplicates", "merge_tasks", "delete_task", "update_person", "update_waiting"):
            self.assertIn(nom, catalogue)

    def test_le_catalogue_signale_les_outils_destructifs(self):
        # Sans ce marqueur, le modele annonce une suppression comme faite
        # alors qu'elle attend encore un feu vert.
        for ligne in tools.describe().split("\n"):
            if ligne.startswith(("merge_tasks(", "delete_task(")):
                self.assertIn("destructif", ligne, ligne)

    def test_alias_nom_vise_la_personne_et_non_le_projet(self):
        # "nom" tombait sur project avant name : update_person(nom="Paul")
        # envoyait le nom de la personne dans la resolution de projet.
        person_model.create_person({"name": "Paul"})
        resultat = tools.execute("update_person", {"nom": "Paul", "role": "Directeur"})
        self.assertTrue(resultat["ok"], resultat)
        self.assertEqual(person_model.find_by_name("Paul")["role"], "Directeur")


class ChatPlanApiTest(NomaiaTestCase):
    """Le plan en attente doit etre visible par l'interface."""

    def test_pending_absent_par_defaut(self):
        self.assertIsNone(self.client().get("/api/chat").get_json()["pending"])

    def test_pending_expose_apres_proposition(self):
        tache = task_model.create_task({"title": "Planifier"})
        faux = scenario(
            {
                "plan": [
                    {"tool": "complete_task", "args": {"task_id": tache["id"]}, "label": "A"},
                    {"tool": "create_task", "args": {"title": "B"}, "label": "B"},
                ],
                "reply": "J'y vais ?",
            }
        )
        with patch.object(assistant.llm, "ask", faux):
            reponse = self.client().post(
                "/api/chat", json={"message": "fais le necessaire"}, headers=self.write_headers()
            )

        corps = reponse.get_json()
        self.assertIsNotNone(corps["pending"])
        self.assertEqual(len(corps["pending"]["steps"]), 2)
        # Et il est retrouve au rechargement, meme sans nouvel envoi.
        self.assertIsNotNone(self.client().get("/api/chat").get_json()["pending"])

    def test_effacer_le_fil_abandonne_le_plan(self):
        chat_model.set_plan([{"tool": "create_task", "args": {}, "label": "A"}], "un")
        self.client().delete("/api/chat", headers=self.write_headers())
        self.assertIsNone(chat_model.pending_plan())


if __name__ == "__main__":
    unittest.main()
