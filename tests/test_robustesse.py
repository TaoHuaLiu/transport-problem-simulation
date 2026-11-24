"""Tests des garde-fous d'exploitation (règles R-ROB-01 à R-ROB-04).

Ces tests protègent ce qui empêche un service de s'arrêter sur la VM : si un
garde-fou est retiré, un test échoue et ``paquet_vm.sh`` refuse d'emballer.
Base, socket et signaux sont simulés (``unittest.mock``) : rien ne sort du
processus de test.

Lancement, depuis ``1 - DEV/`` : ``python3 -m unittest discover -s tests``.
"""

import contextlib
import datetime
import io
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import collecteur  # noqa: E402
import journalisation  # noqa: E402
import logging  # noqa: E402
import socket  # noqa: E402
import tableau_bord  # noqa: E402
import decision  # noqa: E402
import moteur  # noqa: E402
import planification  # noqa: E402
import referentiels  # noqa: E402
import regles  # noqa: E402
import robustesse  # noqa: E402
import tracabilite  # noqa: E402
import tracabilite_controle  # noqa: E402
import tables_direct  # noqa: E402
import flotte  # noqa: E402
from test_decision import COUTS, T0, camion_test, referentiel_test, tournee_a_reparer  # noqa: E402

DOSSIER_CODE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

#: Paramètres de politique des tests (θ = 1 : une tournée attend tant que l'heure limite n'est pas atteinte).
PARAMETRES = {"couts": COUTS, "seuil": 1.0, "marge_h": 0.5}


def champs_test(loc_dep: str = "Amsterdam", h_dep_h: float = 10.0) -> dict:
    """Champs bruts d'une demande, tels que le collecteur les écrit.

    :param loc_dep: Ville de départ (Amsterdam : NORTH ; Brest : hors périmètre).
    :param h_dep_h: ``H_DEP`` en heures projet après :data:`T0`.
    :return: Champs de la demande.
    """
    h_dep = T0 + datetime.timedelta(minutes=h_dep_h)
    return {"TYPE_MVT": "Export", "LOC_DEP": loc_dep, "LOC_ARR": "Paris", "TYPE_DEP": "USINE", "TYPE_ARR": "GMS",
            "TYPE_MAT": "PF Frais", "QTE_PAL": "4", "TYPE_PAL": "EPAL", "H_DEP": h_dep.strftime("%d/%m/%y %H:%M:%S"), "SPEC": ""}


class MoteurTest(unittest.TestCase):
    """Base commune : référentiel fictif, un camion frigo, journal temporaire, sorties console masquées."""

    def setUp(self) -> None:
        """Prépare l'état du moteur et un journal dans un dossier temporaire."""
        self.ref = referentiel_test()
        self.etat = {"flotte": {"F1": camion_test("F1")}, "tournees": {}, "demandes": {}, "compteur": 0, "messages": {}}
        self.dossier = tempfile.TemporaryDirectory()
        self.journal = tracabilite.ouvrir_journal(self.dossier.name, T0)
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()

    def tearDown(self) -> None:
        """Supprime le dossier temporaire et rétablit la console."""
        self.console.__exit__(None, None, None)
        self.dossier.cleanup()

    def tick(self, nouvelles: list, maintenant: datetime.datetime = T0) -> None:
        """Exécute un tick du moteur.

        :param nouvelles: ``(h_reception, id, champs)``.
        :param maintenant: Heure du tick.
        """
        moteur.executer_tick(self.etat, self.ref, nouvelles, maintenant, PARAMETRES, self.journal)


class TestDemandeEnErreur(MoteurTest):
    """R-ROB-01 : une demande ou une tournée en erreur n'arrête pas le moteur."""

    def test_decision_en_erreur_refusee_et_suivantes_traitees(self) -> None:
        """D-ACC lève une exception pour D1 : D1 est refusée REF-DONNEES, D2 est décidée normalement."""
        originale = decision.traiter_demande

        def traiter(demande: dict, *args: object) -> str:
            """D-ACC simulée : plante pour D1.

            :param demande: Demande préparée.
            :param args: Autres arguments de :func:`decision.traiter_demande`.
            :return: Décision de la vraie table D-ACC.
            """
            if demande["id"] == "D1":
                raise RuntimeError("bogue simulé")
            return originale(demande, *args)

        with mock.patch.object(decision, "traiter_demande", side_effect=traiter):
            self.tick([(T0, "D1", champs_test()), (T0, "D2", champs_test())])
        self.assertEqual((self.etat["demandes"]["D1"]["etat"], self.etat["demandes"]["D1"]["motif"]), ("REFUSEE", "REF-DONNEES"))
        self.assertEqual(self.etat["demandes"]["D2"]["etat"], "ACCEPTEE")
        self.assertEqual(self.etat["messages"]["nb"], 1)

    def test_preparation_en_erreur(self) -> None:
        """La préparation de la demande lève une exception : refus REF-DONNEES au journal."""
        with mock.patch.object(regles, "preparer_demande", side_effect=KeyError("QTE_PAL")):
            self.tick([(T0, "D1", champs_test())])
        self.assertEqual(self.etat["demandes"]["D1"]["etat"], "REFUSEE")
        self.assertEqual(self.journal["nb"], 1)

    def test_hors_perimetre_en_erreur_ignoree(self) -> None:
        """Une demande hors NORTH en erreur reste ignorée : elle ne compte pas dans le TA."""
        with mock.patch.object(decision, "traiter_demande", side_effect=RuntimeError("bogue simulé")):
            self.tick([(T0, "D1", champs_test(loc_dep="Brest"))])
        self.assertEqual(self.etat["demandes"]["D1"]["etat"], "IGNOREE")

    def test_demande_acceptee_avant_erreur_reste_acceptee(self) -> None:
        """Une erreur levée après l'acceptation ne transforme pas la demande en refus."""
        originale = decision.traiter_demande

        def traiter(demande: dict, *args: object) -> str:
            """D-ACC simulée : accepte, puis plante.

            :param demande: Demande préparée.
            :param args: Autres arguments de :func:`decision.traiter_demande`.
            :return: Jamais.
            """
            originale(demande, *args)
            raise RuntimeError("bogue après acceptation")

        with mock.patch.object(decision, "traiter_demande", side_effect=traiter):
            self.tick([(T0, "D1", champs_test())])
        self.assertEqual(self.etat["demandes"]["D1"]["etat"], "ACCEPTEE")

    def test_tournee_en_erreur_reste_ouverte(self) -> None:
        """D-DEPART lève une exception : la tournée reste ouverte et part au tick suivant sans erreur."""
        self.tick([(T0, "D1", champs_test())])
        tournee = next(iter(self.etat["tournees"].values()))
        with mock.patch.object(planification, "decider_depart", side_effect=RuntimeError("bogue simulé")):
            self.tick([])
        self.assertEqual(tournee["statut"], "ouverte")
        # Tick suivant avec θ = 0 : la tournée part normalement
        moteur.executer_tick(self.etat, self.ref, [], T0, dict(PARAMETRES, seuil=0.0), self.journal)
        self.assertEqual(tournee["statut"], "partie")

    def test_reaffectation_en_erreur_expire_la_demande(self) -> None:
        """R-DEL-02 + R-ROB-01 : la réaffectation de D1 plante → D1 expire REF-DONNEES, D2 reste dans sa tournée."""
        tournee = tournee_a_reparer(self.ref, self.etat)
        self.ref["trajets"][(2, 13)] = 20.0
        with mock.patch.object(decision, "reaffecter", side_effect=RuntimeError("bogue simulé")):
            self.tick([])
        d1 = self.etat["demandes"]["D1"]
        self.assertEqual((d1["etat"], d1["motif"]), ("EXPIREE", "REF-DONNEES"))
        self.assertEqual((tournee["statut"], tournee["demandes"]), ("ouverte", ["D2"]))
        self.assertEqual(self.etat["messages"]["nb"], 1)

    def test_messages_limites_a_un_par_minute(self) -> None:
        """Une erreur répétée est comptée à chaque fois mais affichée une fois par minute réelle."""
        messages = {}
        self.assertTrue(robustesse.signaler(messages, "x", T0, "a"))
        self.assertFalse(robustesse.signaler(messages, "x", T0 + datetime.timedelta(seconds=30), "a"))
        self.assertTrue(robustesse.signaler(messages, "y", T0, "b"))
        self.assertTrue(robustesse.signaler(messages, "x", T0 + datetime.timedelta(minutes=1), "a"))
        self.assertEqual(messages["nb"], 4)


class TestRedemarrage(MoteurTest):
    """R-VAL-02 : au redémarrage, le moteur relit le CSV du jour ; les demandes échues pendant l'arrêt sont des refus DELAI."""

    def test_demandes_recues_pendant_l_arret(self) -> None:
        """Relecture après 20 min d'arrêt : D1 échue → REF-EXPIREE, code 4 ; D2 encore valable → décidée normalement."""
        redemarrage = T0 + datetime.timedelta(minutes=20)
        self.tick([(T0, "D1", champs_test(h_dep_h=10.0)), (T0, "D2", champs_test(h_dep_h=30.0))], redemarrage)
        d1, d2 = self.etat["demandes"]["D1"], self.etat["demandes"]["D2"]
        self.assertEqual((d1["etat"], d1["motif"]), ("REFUSEE", "REF-EXPIREE"))
        self.assertEqual(tracabilite_controle.code_justif(d1), 4)
        self.assertEqual(d2["etat"], "ACCEPTEE")

    def test_donnees_invalides_restent_code_7(self) -> None:
        """Une demande illisible relue au redémarrage reste REF-DONNEES, code 7."""
        champs = dict(champs_test(h_dep_h=10.0), QTE_PAL="")
        self.tick([(T0, "D1", champs)], T0 + datetime.timedelta(minutes=20))
        self.assertEqual(tracabilite_controle.code_justif(self.etat["demandes"]["D1"]), 7)


class TestJournalEvenements(MoteurTest):
    """R-ROB-08 : une écriture du journal des événements en échec n'arrête rien, n'est pas perdue et se voit."""

    def casser(self) -> str:
        """Rend le journal impossible à écrire (dossier absent) ; renvoie le vrai chemin.

        :return: Chemin d'origine du journal.
        """
        chemin = self.journal["chemin"]
        self.journal["chemin"] = os.path.join(self.dossier.name, "absent", "evenements.csv")
        return chemin

    def lignes(self, chemin: str) -> list:
        """Événements écrits dans un journal.

        :param chemin: Fichier du journal.
        :return: Colonne ``EVENEMENT`` de chaque ligne, en-tête exclu.
        """
        with open(chemin, encoding="utf-8") as fichier:
            return [ligne.split(";")[1] for ligne in fichier.read().splitlines()[1:]]

    def test_echec_garde_la_ligne_et_le_signale(self) -> None:
        """Fichier impossible à écrire : pas d'exception, ligne en attente, échec compté et journalisé en erreur."""
        self.casser()
        with self.assertLogs("nord.tracabilite", level="ERROR"):
            tracabilite.noter(self.journal, T0, "REFUS", demande={"id": "D1"}, motif="REF-DONNEES")
        self.assertEqual((self.journal["nb"], len(self.journal["en_attente"])), (0, 1))
        self.assertEqual((self.journal["sante"]["echecs"], self.journal["sante"]["en_attente"]), (1, 1))
        self.assertEqual(self.journal["messages"]["nb"], 1)

    def test_retablissement_ecrit_dans_l_ordre(self) -> None:
        """Fichier revenu : les lignes en attente sont écrites avant la nouvelle, le rétablissement est annoncé."""
        chemin = self.casser()
        tracabilite.noter(self.journal, T0, "REFUS", demande={"id": "D1"}, motif="REF-DONNEES")
        self.journal["chemin"] = chemin
        with self.assertLogs("nord.robustesse", level="WARNING"):
            tracabilite.noter(self.journal, T0, "ACQUITTEMENT", demande={"id": "D2"})
        self.assertEqual(self.lignes(chemin), ["REFUS", "ACQUITTEMENT"])
        self.assertEqual((self.journal["nb"], self.journal["en_attente"], self.journal["sante"]["echecs"]), (2, [], 0))

    def test_depart_coherent_si_journal_en_panne(self) -> None:
        """Départ pendant la panne : tournée partie ET demande partie ; les événements sont écrits au retour."""
        self.tick([(T0, "D1", champs_test())])
        chemin = self.casser()
        moteur.executer_tick(self.etat, self.ref, [], T0, dict(PARAMETRES, seuil=0.0), self.journal)
        tournee = next(iter(self.etat["tournees"].values()))
        self.assertEqual((tournee["statut"], self.etat["demandes"]["D1"]["etat"]), ("partie", "PARTIE"))
        self.journal["chemin"] = chemin
        self.assertTrue(tracabilite.ecrire_en_attente(self.journal, T0))
        evenements = self.lignes(chemin)
        self.assertEqual((evenements[0], evenements[1], evenements[-1]), ("ACQUITTEMENT", "DEPART", "PARTIE"))

    def test_memoire_bornee(self) -> None:
        """Au-delà de MAX_EN_ATTENTE lignes en attente, les plus anciennes sont perdues et comptées."""
        self.casser()
        with mock.patch.object(tracabilite, "MAX_EN_ATTENTE", 2):
            for numero in range(5):
                tracabilite.noter(self.journal, T0, "REFUS", demande={"id": "D%d" % numero})
        self.assertEqual((len(self.journal["en_attente"]), self.journal["perdus"]), (2, 3))
        self.assertEqual(self.journal["en_attente"][0]["ID_DEMANDE"], "D3")

    def test_message_erreur_apres_acceptation_dit_l_etat_garde(self) -> None:
        """Erreur levée après l'acceptation : le message dit « reste ACCEPTEE », pas « refusée »."""
        originale = decision.traiter_demande

        def traiter(demande: dict, *args: object) -> str:
            """D-ACC simulée : accepte, puis plante.

            :param demande: Demande préparée.
            :param args: Autres arguments de :func:`decision.traiter_demande`.
            :return: Jamais.
            """
            originale(demande, *args)
            raise RuntimeError("bogue après acceptation")

        with mock.patch.object(decision, "traiter_demande", side_effect=traiter):
            with self.assertLogs("nord.moteur", level="WARNING") as journaux:
                self.tick([(T0, "D1", champs_test())])
        self.assertIn("reste ACCEPTEE", journaux.output[0])
        self.assertNotIn("refusée", journaux.output[0])


class TestFichesEcartees(unittest.TestCase):
    """R-FLO-01 : chaque fiche camion écartée au chargement est comptée avec son motif et journalisée."""

    def test_motifs_comptes_et_journalises(self) -> None:
        """Fiche illisible, sans PTAC, PTAC inconnu, REFERENCE illisible, autre zone : un motif chacune."""
        fiches = {"AA-illisible.json": "{pas du json",
                  "BB-sans-ptac.json": json.dumps({"PTAC": "", "ENERGIE": "Diesel", "REFERENCE": "100001"}),
                  "CC-ptac-inconnu.json": json.dumps({"PTAC": "99t", "ENERGIE": "Diesel", "REFERENCE": "100002"}),
                  "DD-reference.json": json.dumps({"PTAC": "19t", "ENERGIE": "Diesel", "REFERENCE": "x"}),
                  "EE-autre-zone.json": json.dumps({"PTAC": "19t", "ENERGIE": "Diesel", "REFERENCE": "900000"}),
                  "FF-ok.json": json.dumps({"PTAC": "19t", "ENERGIE": "Diesel", "REFERENCE": "100003",
                                            "REMORQUE REFRIGEREE": "oui"})}
        with tempfile.TemporaryDirectory() as dossier:
            for nom, contenu in fiches.items():
                with open(os.path.join(dossier, nom), "w", encoding="utf-8") as fichier:
                    fichier.write(contenu)
            ecartees = {}
            camions = flotte.charger_flotte(dossier, referentiel_test(), [1], T0, ecartees)
        self.assertEqual(list(camions), ["FF-ok"])
        self.assertEqual({motif: noms for motif, noms in ecartees.items()},
                         {"illisible": ["AA-illisible.json"], "sans PTAC ou ENERGIE": ["BB-sans-ptac.json"],
                          "PTAC absent de TRUCKS_CAPACITY": ["CC-ptac-inconnu.json"],
                          "REFERENCE illisible": ["DD-reference.json"], "hors usines NORTH": ["EE-autre-zone.json"]})
        with self.assertLogs("nord.moteur", level="INFO") as journaux:
            moteur.journaliser_fiches_ecartees(ecartees)
        avertissements = [ligne for ligne in journaux.output if ligne.startswith("WARNING")]
        self.assertEqual(len(avertissements), 4)  # « hors usines NORTH » n'est pas un problème


class TestEtatFlotte(MoteurTest):
    """R-ROB-07 : un moteur relancé ne redonne pas un camion parti avant l'arrêt."""

    def setUp(self) -> None:
        """Ajoute un fichier d'état de la flotte dans le dossier temporaire."""
        super().setUp()
        self.chemin = os.path.join(self.dossier.name, "run", "flotte_moteur.json")

    def faire_partir_puis_relancer(self) -> dict:
        """Fait partir une tournée sur F1, écrit l'état, puis recharge une flotte neuve et la reprend.

        :return: Flotte du moteur relancé.
        """
        self.tick([(T0, "D1", champs_test(h_dep_h=3.0))], T0)
        for minute in range(1, 4):  # D-DEPART : sous le seuil θ = 1, la tournée part à l'approche de H_DEP
            self.tick([], T0 + datetime.timedelta(minutes=minute))
        self.assertEqual(self.etat["demandes"]["D1"]["etat"], "PARTIE")
        moteur.garder_flotte(self.etat, self.chemin, T0 + datetime.timedelta(minutes=3), {})
        relance = {"flotte": {"F1": camion_test("F1")}, "messages": {}}
        moteur.reprendre_flotte(relance, self.ref, self.chemin, T0 + datetime.timedelta(minutes=4))
        return relance["flotte"]

    def test_camion_parti_reste_occupe(self) -> None:
        """F1 part avec D1 ; après relance, F1 reste occupé jusqu'à la fin de sa tournée, à son dernier lieu."""
        f1 = self.faire_partir_puis_relancer()["F1"]
        avant = self.etat["flotte"]["F1"]
        self.assertGreater(f1["libre_a"], T0 + datetime.timedelta(minutes=4))
        self.assertEqual((f1["libre_a"], f1["position"]), (avant["libre_a"], avant["position"]))
        self.assertNotEqual(f1["position"], 1)
        self.assertIsNone(f1["tournee"])

    def test_camion_occupe_pas_reutilise(self) -> None:
        """Après relance, une demande à partir avant la fin de la tournée de F1 n'obtient pas F1."""
        relance = {"flotte": self.faire_partir_puis_relancer(), "tournees": {}, "demandes": {}, "compteur": 0, "messages": {}}
        maintenant = T0 + datetime.timedelta(minutes=4)
        moteur.executer_tick(relance, self.ref, [(maintenant, "D2", champs_test(h_dep_h=8.0))], maintenant, PARAMETRES,
                             self.journal)
        self.assertEqual(relance["demandes"]["D2"]["etat"], "REFUSEE")

    def test_etat_d_un_autre_jour_ignore(self) -> None:
        """Un fichier de la veille est ignoré : les fiches sont régénérées chaque jour réel."""
        moteur.garder_flotte(self.etat, self.chemin, T0 - datetime.timedelta(days=1), {})
        bilan = flotte.restaurer_etat({"F1": camion_test("F1")}, self.ref, self.chemin, T0)
        self.assertEqual(bilan["restaures"], 0)
        self.assertIn("pas d'aujourd'hui", bilan["raison"])

    def test_fichier_illisible_ou_entrees_invalides(self) -> None:
        """Fichier corrompu : pas d'erreur, état des fiches gardé ; camion ou lieu inconnu : entrée ignorée."""
        os.makedirs(os.path.dirname(self.chemin))
        with open(self.chemin, "w", encoding="utf-8") as fichier:
            fichier.write("{pas du json")
        bilan = moteur.reprendre_flotte({"flotte": {"F1": camion_test("F1")}}, self.ref, self.chemin, T0)
        self.assertIn("illisible", bilan["raison"])
        with open(self.chemin, "w", encoding="utf-8") as fichier:
            json.dump({"jour": T0.strftime("%Y-%m-%d"), "camions": {
                "INCONNU": {"position": 1, "libre_a": T0.isoformat(), "conduite_jour": 0},
                "F1": {"position": 999, "libre_a": T0.isoformat(), "conduite_jour": 0}}}, fichier)
        camions = {"F1": camion_test("F1")}
        bilan = flotte.restaurer_etat(camions, self.ref, self.chemin, T0)
        self.assertEqual((bilan["restaures"], bilan["ignores"]), (0, 2))
        self.assertEqual(camions["F1"]["position"], 1)

    def test_ecriture_en_echec_signalee_sans_arret(self) -> None:
        """Disque plein : pas d'exception, erreur comptée, nouvel essai au tick suivant."""
        suivi = {}
        with mock.patch("os.replace", side_effect=OSError("disque plein")):
            moteur.garder_flotte(self.etat, self.chemin, T0, suivi)
        self.assertEqual(self.etat["messages"]["nb"], 1)
        self.assertIsNone(suivi["flotte_ecrite"])
        moteur.garder_flotte(self.etat, self.chemin, T0, suivi)
        self.assertTrue(os.path.isfile(self.chemin))


class TestBaseIndisponible(MoteurTest):
    """R-ROB-02 : base injoignable ou lente."""

    def test_rafraichissement_garde_le_referentiel(self) -> None:
        """La base ne répond pas pendant le rafraîchissement : le moteur garde le référentiel précédent."""
        with mock.patch.object(referentiels, "charger_depuis_bdd", side_effect=OSError("connexion refusée")):
            self.assertIs(moteur.rafraichir_referentiel(self.ref, self.etat["messages"], T0), self.ref)
        self.assertEqual(self.etat["messages"]["nb"], 1)

    def test_demarrage_sur_la_copie_json(self) -> None:
        """La base ne répond pas au démarrage : le moteur part sur la copie écrite par ``nord.sh``."""
        copie = os.path.join(self.dossier.name, "referentiels.json")
        referentiels.sauver_json(self.ref, copie)
        with mock.patch.object(referentiels, "charger_depuis_bdd", side_effect=OSError("connexion refusée")):
            ref = moteur.charger_bdd_ou_copie(copie)
        self.assertEqual(ref["source"], copie)
        self.assertEqual(ref["trajets"], self.ref["trajets"])

    def test_demarrage_sans_copie_echoue(self) -> None:
        """Sans copie, l'erreur de la base remonte (pas de référentiel fictif en production)."""
        with mock.patch.object(referentiels, "charger_depuis_bdd", side_effect=OSError("connexion refusée")):
            with self.assertRaises(OSError):
                moteur.charger_bdd_ou_copie(os.path.join(self.dossier.name, "absent.json"))

    def test_tables_du_controle_base_injoignable(self) -> None:
        """Tables du Contrôle : base injoignable ou table absente, la synchronisation ne lève jamais d'erreur."""
        def injoignable() -> object:
            """Connexion simulée qui échoue.

            :return: Jamais.
            :raises OSError: Toujours.
            """
            raise OSError("connexion refusée")

        class TableAbsente:
            """Connexion simulée dont chaque requête échoue (table pas encore créée)."""

            def cursor(self) -> "TableAbsente":
                """Renvoie la connexion elle-même.

                :return: La connexion.
                """
                return self

            def execute(self, *args: object) -> None:
                """Échoue comme PostgreSQL sur une table absente.

                :param args: Requête et paramètres (ignorés).
                :raises RuntimeError: Toujours.
                """
                raise RuntimeError('relation "TRACABILITE_COMMANDE" does not exist')

            def rollback(self) -> None:
                """Ne fait rien."""

            def close(self) -> None:
                """Ne fait rien."""

        for connecter in (injoignable, TableAbsente):
            lien = tracabilite_controle.nouveau_lien(connecter, (ValueError,))
            self.assertEqual(tracabilite_controle.synchroniser(lien, self.etat, self.ref, COUTS, T0), 0)
            self.assertIsNone(lien["connexion"])

    def test_delais_de_connexion_bornes(self) -> None:
        """Connexion et requêtes ont un délai maximal (un serveur muet bloquerait le moteur 2 min)."""
        self.assertLessEqual(referentiels.BDD.get("connect_timeout", 0), 10)
        self.assertGreater(referentiels.BDD.get("connect_timeout", 0), 0)
        self.assertIn("statement_timeout", referentiels.BDD.get("options", ""))


class TestLectureCSV(MoteurTest):
    """R-ROB-01 : lecture du CSV du collecteur par le moteur en direct."""

    ENTETE = "ID_DEMANDE;H_RECEPTION;LOC_DEP;SPEC\n"

    def chemin(self, jour: datetime.date) -> str:
        """Chemin du CSV d'un jour dans le dossier temporaire.

        :param jour: Jour réel.
        :return: Chemin du fichier.
        """
        return os.path.join(self.dossier.name, "demandes_%s.csv" % jour.strftime("%Y-%m-%d"))

    def test_lignes_illisibles_ignorees(self) -> None:
        """Heure mal formée et identifiant vide : lignes ignorées et comptées, les autres lues."""
        with open(self.chemin(T0.date()), "w", encoding="utf-8") as fichier:
            fichier.write(self.ENTETE + "D1;2026-10-19 15:00:00;Amsterdam;\nD2;n'importe quoi;Amsterdam;\n"
                          ";2026-10-19 15:00:00;Amsterdam;\nD3;2026-10-19 15:00:01;Amsterdam;\n")
        lecteur = {"repertoire": self.dossier.name}
        lues = moteur.lire_nouvelles_lignes(lecteur, T0)
        self.assertEqual([id_demande for _, id_demande, _ in lues], ["D1", "D3"])
        self.assertEqual(lecteur["ignorees"], 2)

    def test_champ_avec_saut_de_ligne(self) -> None:
        """Un champ entre guillemets qui contient un saut de ligne reste une seule demande."""
        with open(self.chemin(T0.date()), "w", encoding="utf-8", newline="") as fichier:
            fichier.write(self.ENTETE + 'D1;2026-10-19 15:00:00;Amsterdam;"PTAC=12t,\nCOL=Rouge"\n')
        lues = moteur.lire_nouvelles_lignes({"repertoire": self.dossier.name}, T0)
        self.assertEqual(len(lues), 1)
        self.assertIn("COL=Rouge", lues[0][2]["SPEC"])

    def test_fin_du_fichier_de_la_veille_lue_a_minuit(self) -> None:
        """Une demande écrite juste avant minuit, après la dernière lecture, n'est pas perdue."""
        veille = datetime.datetime(2026, 10, 19, 23, 59, 50)
        lendemain = veille + datetime.timedelta(seconds=15)
        with open(self.chemin(veille.date()), "w", encoding="utf-8") as fichier:
            fichier.write(self.ENTETE + "D1;2026-10-19 23:59:40;Amsterdam;\n")
        lecteur = {"repertoire": self.dossier.name}
        self.assertEqual(len(moteur.lire_nouvelles_lignes(lecteur, veille)), 1)
        with open(self.chemin(veille.date()), "a", encoding="utf-8") as fichier:
            fichier.write("D2;2026-10-19 23:59:58;Amsterdam;\n")
        with open(self.chemin(lendemain.date()), "w", encoding="utf-8") as fichier:
            fichier.write(self.ENTETE + "D3;2026-10-20 00:00:03;Amsterdam;\n")
        lues = moteur.lire_nouvelles_lignes(lecteur, lendemain)
        self.assertEqual([id_demande for _, id_demande, _ in lues], ["D2", "D3"])


class TestArretPropre(unittest.TestCase):
    """R-ROB-03 : SIGTERM (``nord.sh arreter``) passe par les blocs ``finally``."""

    def test_sigterm_leve_keyboardinterrupt(self) -> None:
        """Après :func:`robustesse.arreter_sur_sigterm`, SIGTERM se comporte comme ``Ctrl+C``."""
        ancien = signal.getsignal(signal.SIGTERM)
        try:
            robustesse.arreter_sur_sigterm()
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
        finally:
            signal.signal(signal.SIGTERM, ancien)

    def test_services_installent_le_gestionnaire(self) -> None:
        """Les trois services lancés par ``nord.sh`` installent le gestionnaire au démarrage."""
        for nom in ("moteur.py", "collecteur.py", "tableau_bord.py"):
            with open(os.path.join(DOSSIER_CODE, nom), encoding="utf-8") as fichier:
                principal = fichier.read().split('if __name__ == "__main__":')[-1]
            self.assertIn("robustesse.arreter_sur_sigterm()", principal, nom)


class FausseSocket:
    """Socket UDP simulée : renvoie une suite de résultats de ``recvfrom``.

    :param resultats: Datagrammes ``(data, adresse)`` ou exceptions à lever, dans l'ordre.
    """

    def __init__(self, resultats: list) -> None:
        """Mémorise les résultats.

        :param resultats: Résultats successifs.
        """
        self.resultats = list(resultats)

    def recvfrom(self, taille: int) -> tuple:
        """Renvoie le résultat suivant, ou lève l'exception prévue.

        :param taille: Taille maximale (ignorée).
        :return: ``(data, adresse)``.
        """
        resultat = self.resultats.pop(0)
        if isinstance(resultat, BaseException):
            raise resultat
        return resultat


class TestCollecteur(unittest.TestCase):
    """R-ROB-04 : le collecteur ne s'arrête que sur demande ; R-COL-01 : le brut d'abord."""

    def setUp(self) -> None:
        """Dossier de sortie temporaire, console masquée."""
        self.dossier = tempfile.TemporaryDirectory()
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()

    def tearDown(self) -> None:
        """Supprime le dossier temporaire et rétablit la console."""
        self.console.__exit__(None, None, None)
        self.dossier.cleanup()

    def message(self, id_demande: str) -> bytes:
        """Datagramme de l'ERP (simples quotes) avec une demande.

        :param id_demande: Identifiant de la demande.
        :return: Contenu du datagramme.
        """
        return str({id_demande: champs_test()}).encode("utf-8")

    def test_csv_du_jour_abime_brut_conserve(self) -> None:
        """Un CSV du jour sans colonne ID_DEMANDE n'empêche ni l'écriture du brut ni celle de la demande."""
        contexte = {"repertoire": self.dossier.name, "jour": None, "ids": set(), "nb": 0}
        chemin_jsonl, chemin_csv = collecteur.chemins_du_jour(self.dossier.name, T0.date())
        with open(chemin_csv, "w", encoding="utf-8") as fichier:
            fichier.write("COLONNE_INATTENDUE\nx\n")
        self.assertEqual(collecteur.traiter_datagramme(self.message("D1"), ("1.2.3.4", 40040), T0, contexte), 1)
        with open(chemin_jsonl, encoding="utf-8") as fichier:
            self.assertIn("D1", json.loads(fichier.readline())["MESSAGE"])

    def test_boucle_survit_aux_erreurs(self) -> None:
        """Erreur de socket puis erreur sur un message : la collecte continue jusqu'à l'arrêt demandé."""
        socket_simulee = FausseSocket([OSError("réseau"), (self.message("D1"), ("1.2.3.4", 40040)),
                                       (self.message("D2"), ("1.2.3.4", 40040)), KeyboardInterrupt()])
        originale = collecteur.traiter_datagramme
        appels = []

        def traiter(data: bytes, *args: object) -> int:
            """Traitement simulé : le premier message plante (disque plein).

            :param data: Datagramme.
            :param args: Autres arguments de :func:`collecteur.traiter_datagramme`.
            :return: Nombre de demandes écrites.
            """
            appels.append(data)
            if len(appels) == 1:
                raise OSError("disque plein")
            return originale(data, *args)

        with mock.patch.object(collecteur, "creer_client_udp", return_value=socket_simulee), \
                mock.patch.object(collecteur, "traiter_datagramme", side_effect=traiter), \
                mock.patch.object(collecteur.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                collecteur.collecter(40040, self.dossier.name)
        self.assertEqual(len(appels), 2)
        _, chemin_csv = collecteur.chemins_du_jour(self.dossier.name, datetime.date.today())
        self.assertEqual(collecteur.charger_ids_existants(chemin_csv), {"D2"})


class TestSante(unittest.TestCase):
    """R-ROB-06 : une erreur n'est jamais silencieuse ; santé et journaux ne bloquent jamais un service."""

    def setUp(self) -> None:
        """Dossier temporaire."""
        self.dossier = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        """Supprime le dossier temporaire."""
        self.dossier.cleanup()

    def test_messages_masques_annonces(self) -> None:
        """Les messages masqués sont comptés puis annoncés au message suivant de la clé."""
        messages = {}
        with self.assertLogs("nord", level="DEBUG") as journaux:
            robustesse.signaler(messages, "x", T0, "panne", logging.ERROR)
            robustesse.signaler(messages, "x", T0 + datetime.timedelta(seconds=10), "panne", logging.ERROR)
            robustesse.signaler(messages, "x", T0 + datetime.timedelta(seconds=20), "panne", logging.ERROR)
            robustesse.signaler(messages, "x", T0 + datetime.timedelta(minutes=2), "panne", logging.ERROR)
        erreurs = [l for l in journaux.output if l.startswith("ERROR")]
        self.assertEqual(len(erreurs), 2)
        self.assertIn("2 message(s) semblable(s) masqué(s)", erreurs[1])
        self.assertEqual(sum(1 for l in journaux.output if "(masqué)" in l), 2)

    def test_echec_puis_retablissement(self) -> None:
        """L'échec est daté et compté ; le rétablissement est annoncé et remet les compteurs à zéro."""
        sante = robustesse.nouvel_etat_bdd()
        robustesse.noter_echec_bdd(sante, T0, OSError("relation \"LOG_NORTH_CAMION\" does not exist\nLINE 1"), 12)
        robustesse.noter_echec_bdd(sante, T0 + datetime.timedelta(seconds=5), OSError("idem"), 14)
        self.assertEqual((sante["echecs"], sante["en_attente"], sante["depuis"]), (2, 14, T0.strftime("%Y-%m-%d %H:%M:%S")))
        with self.assertLogs("nord", level="WARNING") as journaux:
            robustesse.noter_succes_bdd(sante, T0 + datetime.timedelta(seconds=10), 14, "archivage")
        self.assertIn("rétabli après 2 échec(s)", journaux.output[0])
        self.assertEqual((sante["echecs"], sante["en_attente"], sante["ecrites"]), (0, 0, 14))

    def test_ecrire_sante_ne_leve_jamais(self) -> None:
        """Dossier de santé impossible à créer : pas d'exception ; dossier valide : relu avec son âge."""
        fichier = os.path.join(self.dossier.name, "fichier")
        open(fichier, "w").close()
        self.assertFalse(journalisation.ecrire_sante("moteur", {"statut": "en marche"}, os.path.join(fichier, "run")))
        self.assertTrue(journalisation.ecrire_sante("moteur", {"statut": "en marche", "h": T0}, self.dossier.name))
        sante = journalisation.lire_sante(self.dossier.name)
        self.assertEqual(sante["moteur"]["statut"], "en marche")
        self.assertLess(sante["moteur"]["age_s"], 60)

    def test_journaux_indisponibles_console_seule(self) -> None:
        """Dossier des journaux impossible à créer : le service démarre quand même, avec la console."""
        fichier = os.path.join(self.dossier.name, "fichier")
        open(fichier, "w").close()
        with contextlib.redirect_stdout(io.StringIO()) as console:
            journalisation.configurer("test", repertoire=os.path.join(fichier, "logs"))
            journalisation.journal("test").info("bonjour")
        self.assertIn("console seule", console.getvalue())
        self.assertIn("bonjour", console.getvalue())
        self._reinitialiser()

    def test_evenements_sql_dans_leur_fichier(self) -> None:
        """Événement SQL réussi : journal SQL seulement ; en erreur : aussi console et journal du service."""
        with contextlib.redirect_stdout(io.StringIO()) as console:
            journalisation.configurer("test", repertoire=self.dossier.name, sql=True)
            journalisation.evenement_sql("UPSERT", "LOG_NORTH_TRAJET", 3, 1.5)
            journalisation.evenement_sql("UPSERT", "LOG_NORTH_TRAJET", 0, erreur=OSError("table absente"))
        self._reinitialiser()
        sql = journalisation.lire_fin_jsonl(os.path.join(self.dossier.name, journalisation.FICHIER_SQL))
        self.assertEqual([e["statut"] for e in sql], ["ok", "erreur"])
        self.assertEqual(sql[0]["lignes"], 3)
        service = journalisation.lire_fin_jsonl(os.path.join(self.dossier.name, "test.jsonl"))
        self.assertTrue(any("table absente" in l["message"] for l in service))
        self.assertNotIn("3 ligne(s)", console.getvalue())
        self.assertIn("table absente", console.getvalue())

    def test_alertes_du_tableau_de_bord(self) -> None:
        """Archivage en échec : alerte critique ; moteur muet : critique ; ERP muet : attention."""
        maintenant = datetime.datetime(2026, 11, 20, 14, 0, 0)
        archivage_ko = dict(robustesse.nouvel_etat_bdd(), echecs=3, depuis="2026-11-20 13:59:00",
                            derniere_erreur="relation does not exist", en_attente=40)
        services = {"moteur": {"statut": "en marche", "age_s": 2, "archivage": archivage_ko,
                               "controle": robustesse.nouvel_etat_bdd(actif=False), "referentiel": robustesse.nouvel_etat_bdd()},
                    "collecteur": {"statut": "en marche", "age_s": 2, "dernier_message": "2026-11-20T13:50:00"}}
        alertes = tableau_bord.alertes_sante(services, maintenant)
        self.assertEqual(alertes[0]["niveau"], "critique")
        self.assertIn("LOG_NORTH_*", alertes[0]["texte"])
        self.assertTrue(any(a["service"] == "collecteur" and a["niveau"] == "attention" for a in alertes))
        services["moteur"]["age_s"] = 300
        self.assertTrue(any("muet" in a["texte"] for a in tableau_bord.alertes_sante(services, maintenant)))

    def test_collecteur_ecrit_sa_sante_sans_message(self) -> None:
        """Attente de réception expirée : la collecte continue et la santé est écrite, puis « arrêté »."""
        socket_simulee = FausseSocket([socket.timeout(), socket.timeout(), KeyboardInterrupt()])
        with mock.patch.object(collecteur, "creer_client_udp", return_value=socket_simulee):
            with self.assertRaises(KeyboardInterrupt):
                collecteur.collecter(40040, os.path.join(self.dossier.name, "demandes"), self.dossier.name)
        sante = journalisation.lire_sante(self.dossier.name)["collecteur"]
        self.assertEqual((sante["statut"], sante["messages_recus"]), ("arrêté", 0))

    @staticmethod
    def _reinitialiser() -> None:
        """Retire les gestionnaires installés par :func:`journalisation.configurer` (retour à l'état des tests)."""
        for nom in (journalisation.RACINE, journalisation.NOM_SQL):
            for gestionnaire in list(logging.getLogger(nom).handlers):
                logging.getLogger(nom).removeHandler(gestionnaire)
                gestionnaire.close()
        logging.getLogger(journalisation.RACINE).addHandler(logging.NullHandler())
        logging.getLogger(journalisation.RACINE).setLevel(logging.NOTSET)
        sys.excepthook = sys.__excepthook__



class _CurseurFactice:
    """Curseur PostgreSQL factice : rend des lignes fixes et garde les requêtes reçues."""

    def __init__(self, requetes: list) -> None:
        """Prépare le curseur.

        :param requetes: Liste où noter chaque requête exécutée.
        """
        self.requetes, self.derniere = requetes, ""

    def execute(self, sql: str, parametres: dict = None) -> None:
        """Note la requête.

        :param sql: Texte SQL.
        :param parametres: Paramètres.
        """
        self.requetes.append(sql)
        self.derniere = sql

    def fetchall(self) -> list:
        """Lignes : une ligne de commande, ou les groupes par statut.

        :return: Lignes.
        """
        if "GROUP BY" in self.derniere:
            return [("ACCEPTEE", 2), ("REFUSEE", 1)]
        return [tuple(["N1"] + [datetime.datetime(2026, 11, 24, 15, 0)] * 2)]

    def fetchone(self) -> tuple:
        """Nombre de lignes.

        :return: ``(3,)``.
        """
        return (3,)

    def close(self) -> None:
        """Rien à fermer."""


class _ConnexionFactice:
    """Connexion factice : vérifie la session en lecture seule."""

    def __init__(self, requetes: list) -> None:
        """Prépare la connexion.

        :param requetes: Liste partagée avec le curseur.
        """
        self.requetes, self.session = requetes, None

    def set_session(self, **options) -> None:
        """Garde les options de session.

        :param options: ``readonly``, ``autocommit``.
        :type options: bool
        """
        self.session = options

    def cursor(self) -> _CurseurFactice:
        """Nouveau curseur.

        :return: Curseur factice.
        """
        return _CurseurFactice(self.requetes)

    def close(self) -> None:
        """Rien à fermer."""


class TestTablesDirect(unittest.TestCase):
    """Onglet Tables du tableau de bord : lecture seule, jamais d'exception, pause après un échec (R-ROB-02, R-ROB-06)."""

    def test_base_injoignable(self) -> None:
        """Une connexion en échec rend une erreur, met le lien en pause et ne relance pas avant la fin de la pause."""
        lien = tables_direct.nouveau_lien()
        appels = []

        def connecteur(**parametres) -> None:
            """Échoue comme un serveur muet.

            :param parametres: Paramètres de connexion.
            :type parametres: object
            :raises OSError: Toujours.
            """
            appels.append(parametres)
            raise OSError("délai dépassé")

        reponse = tables_direct.lire_table(lien, "LOG_NORTH_TRAJET", connecteur=connecteur)
        self.assertFalse(reponse["ok"])
        self.assertIn("délai dépassé", reponse["erreur"])
        self.assertEqual(lien["sante"]["echecs"], 1)
        self.assertIn("connect_timeout", appels[0])  # R-ROB-02 : délais bornés
        tables_direct.lire_table(lien, "LOG_NORTH_TRAJET", connecteur=connecteur)
        self.assertEqual(len(appels), 1)  # en pause : pas de deuxième essai

    def test_lecture_seule_et_nos_lignes(self) -> None:
        """Session en lecture seule ; tables du Contrôle filtrées sur nos ID_ENTRE ; uniquement des SELECT."""
        lien, requetes = tables_direct.nouveau_lien(), []
        connexion = _ConnexionFactice(requetes)
        reponse = tables_direct.lire_table(lien, "TRACABILITE_COMMANDE", connecteur=lambda **p: connexion)
        self.assertTrue(reponse["ok"])
        self.assertEqual(connexion.session, {"readonly": True, "autocommit": True})
        self.assertEqual((reponse["total"], reponse["groupes"]), (3, {"ACCEPTEE": 2, "REFUSEE": 1}))
        self.assertTrue(all(r.lstrip().upper().startswith("SELECT") for r in requetes))
        self.assertTrue(all('"ID_ENTRE" ~ %(motif)s' in r for r in requetes))
        self.assertEqual(reponse["lignes"][0][1], "2026-11-24 15:00:00")

    def test_table_inconnue_refusee(self) -> None:
        """Un nom de table hors de la liste n'atteint jamais la base."""
        lien = tables_direct.nouveau_lien()
        reponse = tables_direct.lire_table(lien, 'X"; DROP TABLE "Y', connecteur=lambda **p: self.fail("connexion"))
        self.assertFalse(reponse["ok"])

    def test_onglet_desactive(self) -> None:
        """``--sans-tables`` : aucune connexion."""
        lien = tables_direct.nouveau_lien(actif=False)
        reponse = tables_direct.lire_table(lien, "LOG_NORTH_TRAJET", connecteur=lambda **p: self.fail("connexion"))
        self.assertIn("désactivé", reponse["erreur"])


if __name__ == "__main__":
    unittest.main()
