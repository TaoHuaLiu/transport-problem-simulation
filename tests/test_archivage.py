"""Tests de l'archivage dans les tables ``*_NORTH`` (:mod:`archivage`).

Reprend le référentiel fictif de :mod:`test_decision`. La base est simulée
(:class:`FausseConnexion`) : on vérifie les lignes produites, les contraintes
``CHECK`` de ``tables-nord.sql``, l'ordre des écritures et la reprise sur erreur.

Lancement, depuis ``1 - DEV/`` : ``python3 -m unittest discover -s tests``.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archivage  # noqa: E402
import decision  # noqa: E402
import moteur  # noqa: E402
import planification  # noqa: E402
import tracabilite  # noqa: E402
from test_decision import COUTS, T0, camion_test, demande_test, referentiel_test  # noqa: E402

#: Statuts autorisés par les contraintes CHECK de ``tables-nord.sql``.
STATUTS_TRAJET = ("EN_CONSTITUTION", "VIDE_VERS_PORT", "ATTENTE_PORT", "ENTRE_CHARGEMENTS", "EN_LIVRAISON", "TERMINE", "ANNULE")
STATUTS_COMMANDE = ("ACCEPTEE", "PARTIE", "REFUSEE", "EXPIREE")


def apres(heures: float) -> datetime.datetime:
    """Heure réelle ``heures`` heures projet après :data:`T0`.

    :param heures: Durée en heures projet (1 h projet = 1 min réelle).
    :return: Date et heure réelles.
    """
    return T0 + datetime.timedelta(minutes=heures)


class ErreurLigne(Exception):
    """Erreur simulée due au contenu d'une ligne (comme ``psycopg2.IntegrityError``)."""


class FausseConnexion:
    """Connexion DB-API simulée : enregistre les requêtes, peut refuser certaines lignes.

    :param rejets: Clés primaires dont l'écriture lève :class:`ErreurLigne`.
    :param deja: Identifiants renvoyés par le ``SELECT "ID_COMMANDE"`` de :func:`archivage.reprendre`.
    """

    def __init__(self, rejets: tuple = (), deja: tuple = ()) -> None:
        """Initialise le journal des requêtes.

        :param rejets: Clés primaires refusées.
        :param deja: Commandes déjà archivées.
        """
        self.requetes, self.rejets, self.deja = [], set(rejets), list(deja)
        self.commits, self.rollbacks = 0, 0

    def cursor(self) -> "FausseConnexion":
        """Renvoie la connexion elle-même, qui sert aussi de curseur.

        :return: La connexion.
        """
        return self

    def execute(self, requete: str, parametres: tuple = ()) -> None:
        """Enregistre une requête ; refuse une ligne dont la clé est dans ``rejets``.

        :param requete: Requête SQL.
        :param parametres: Paramètres.
        :raises ErreurLigne: si la clé primaire est refusée.
        """
        if parametres and parametres[0] in self.rejets:
            raise ErreurLigne("contrainte violée : %s" % parametres[0])
        self.requetes.append((requete, parametres))

    def executemany(self, requete: str, lignes: list) -> None:
        """Enregistre un lot, refusé entier si une ligne l'est.

        :param requete: Requête SQL.
        :param lignes: Paramètres de chaque ligne.
        :raises ErreurLigne: si une clé primaire est refusée.
        """
        if any(ligne[0] in self.rejets for ligne in lignes):
            raise ErreurLigne("contrainte violée dans le lot")
        self.requetes += [(requete, ligne) for ligne in lignes]

    def fetchall(self) -> list:
        """Résultat du ``SELECT`` de reprise.

        :return: Lignes ``(ID_COMMANDE,)``.
        """
        return [(i,) for i in self.deja]

    def commit(self) -> None:
        """Compte les validations."""
        self.commits += 1

    def rollback(self) -> None:
        """Compte les annulations."""
        self.rollbacks += 1

    def close(self) -> None:
        """Ne fait rien."""

    def tables_ecrites(self) -> list:
        """Tables visées par les INSERT, dans l'ordre.

        :return: Noms de tables.
        """
        return [r.split('"')[1] for r, _ in self.requetes if r.startswith("INSERT")]


def verifier_contraintes(test: unittest.TestCase, table: str, ligne: tuple) -> None:
    """Vérifie qu'une ligne respecte les contraintes NOT NULL et CHECK de ``tables-nord.sql``.

    :param test: Cas de test en cours.
    :param table: Table visée.
    :param ligne: Tuple dans l'ordre de :data:`archivage.COLONNES`.
    """
    valeurs = dict(zip(archivage.COLONNES[table], ligne))
    if table == "TRAJET_NORTH":
        test.assertIn(valeurs["STATUT"], STATUTS_TRAJET)
        test.assertTrue(valeurs["TAUX_REMPL"] is None or 0 <= valeurs["TAUX_REMPL"] <= 100)
    elif table == "ARCHIVAGE_COMMANDE_NORTH":
        test.assertIn(valeurs["STATUT"], STATUTS_COMMANDE)
        test.assertIn(valeurs["TYPE_MV"], ("Export", "Import"))
        test.assertGreater(valeurs["QTE_PAL"], 0)
        for colonne in ("LOC_DEP", "LOC_ARR", "TYPE_DEP", "TYPE_ARR", "TYPE_MAT", "TYPE_PAL", "H_DEP"):
            test.assertIsNotNone(valeurs[colonne], colonne)
        if valeurs["STATUT"] in ("ACCEPTEE", "PARTIE"):
            test.assertIsNotNone(valeurs["ID_TRAJET"])
        else:
            test.assertTrue(valeurs["JUSTI_REFUS"] and len(valeurs["JUSTI_REFUS"]) <= 200)
    else:
        test.assertLessEqual(len(valeurs["PTAC"]), 3)


class BaseArchivage(unittest.TestCase):
    """Référentiel, flotte d'un frigo et journal temporaire communs aux tests."""

    def setUp(self) -> None:
        """Prépare l'état du moteur à :data:`T0`."""
        self.ref = referentiel_test()
        self.etat = {"flotte": {"F1": camion_test("F1")}, "tournees": {}, "demandes": {}, "compteur": 0}
        self.parametres = {"couts": COUTS, "seuil": 0.6, "marge_h": 1.0}
        self.journal = tracabilite.ouvrir_journal(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_build", "tests"), T0, "_test")

    def decider(self, demande: dict) -> dict:
        """Applique D-ACC à une demande à :data:`T0`.

        :param demande: Demande préparée.
        :return: La même demande, décidée.
        """
        decision.traiter_demande(demande, self.etat, self.ref, T0, self.parametres, self.journal)
        return demande

    def partir(self, demande: dict) -> dict:
        """Fait partir à :data:`T0` la tournée de la demande.

        :param demande: Demande acceptée.
        :return: Tournée partie.
        """
        tournee = self.etat["tournees"][demande["tournee"]]
        planification.faire_partir(tournee, self.etat, self.ref, T0, COUTS, self.journal)
        return tournee

    def commande(self, demande: dict) -> dict:
        """Ligne ``ARCHIVAGE_COMMANDE_NORTH`` de la demande, en dictionnaire.

        :param demande: Demande décidée.
        :return: ``{colonne: valeur}``.
        """
        ligne = archivage.ligne_commande(demande, self.etat, self.ref, COUTS)
        verifier_contraintes(self, "ARCHIVAGE_COMMANDE_NORTH", ligne)
        return dict(zip(archivage.COLONNES["ARCHIVAGE_COMMANDE_NORTH"], ligne))


class TestLignes(BaseArchivage):
    """Contenu des lignes et statuts."""

    def test_refus_donnees_non_archivable(self) -> None:
        """R-VAL-01 : lieu d'arrivée inconnu → REF-DONNEES, pas de ligne (LOC_ARR NOT NULL), compté à part."""
        demande = self.decider(demande_test(self.ref, loc_arr="Atlantis"))
        self.assertEqual(demande["motif"], "REF-DONNEES")
        self.assertIsNone(archivage.ligne_commande(demande, self.etat, self.ref, COUTS))
        archive = archivage.nouvelle_archive(FausseConnexion, (ErreurLigne,))
        self.assertEqual(archivage.lignes_modifiees(archive, self.etat, self.ref, COUTS, T0)["ARCHIVAGE_COMMANDE_NORTH"], [])
        self.assertEqual(archive["non_archivables"], {"D1"})

    def test_hors_perimetre_ignore(self) -> None:
        """R-PER-01 : une demande d'une usine WEST n'est pas archivée."""
        demande = self.decider(demande_test(self.ref, loc_dep="Brest"))
        self.assertIsNone(archivage.ligne_commande(demande, self.etat, self.ref, COUTS))

    def test_refus_avec_motif(self) -> None:
        """Refus REF-DELAI : statut REFUSEE, justification, pas de trajet ni de SPEC."""
        valeurs = self.commande(self.decider(demande_test(self.ref, h_dep_h=1.0)))
        self.assertEqual(valeurs["STATUT"], "REFUSEE")
        self.assertTrue(valeurs["JUSTI_REFUS"].startswith("REF-DELAI : "))
        self.assertIsNone(valeurs["ID_TRAJET"])
        self.assertIsNone(valeurs["SPEC_COL"])

    def test_acceptee(self) -> None:
        """Acceptée : trajet EN_CONSTITUTION, pas encore de départ réel ni de prix."""
        demande = self.decider(demande_test(self.ref, qte=4, h_dep_h=12.0))
        valeurs = self.commande(demande)
        self.assertEqual((valeurs["STATUT"], valeurs["ID_TRAJET"]), ("ACCEPTEE", "T00001"))
        self.assertIsNone(valeurs["H_DEP_R"])
        self.assertIsNone(valeurs["COUT_PREST"])
        trajet = archivage.ligne_trajet(self.etat["tournees"]["T00001"], self.etat, T0)
        verifier_contraintes(self, "TRAJET_NORTH", trajet)
        self.assertEqual(trajet[1:], ("F1", 20.0, "EN_CONSTITUTION"))

    def test_spec(self) -> None:
        """R-SPEC-02 : COL demandée et non tenue → SPEC_COL faux ; clés non demandées vides."""
        valeurs = self.commande(self.decider(demande_test(self.ref, spec="COL=Violet", h_dep_h=12.0)))
        self.assertIs(valeurs["SPEC_COL"], False)
        self.assertIsNone(valeurs["SPEC_PTAC"])
        valeurs = self.commande(self.decider(demande_test(self.ref, id_demande="D2", spec="PTAC=19t", h_dep_h=12.0)))
        self.assertIs(valeurs["SPEC_PTAC"], True)

    def test_partie_export(self) -> None:
        """Export parti : départ réel ≤ H_DEP (R-DEL-01), prix facturé, puis statuts du trajet dans le temps."""
        demande = self.decider(demande_test(self.ref, qte=14, h_dep_h=12.0))
        tournee = self.partir(demande)
        valeurs = self.commande(demande)
        self.assertEqual(valeurs["STATUT"], "PARTIE")
        self.assertLessEqual(valeurs["H_DEP_R"], valeurs["H_DEP"])
        self.assertGreater(valeurs["COUT_PREST"], 0)
        depart = tournee["plan"]["suivi"]["D1"]["depart"]
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, T0), "EN_CONSTITUTION")
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, depart), "EN_LIVRAISON")
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, tournee["plan"]["fin"]), "TERMINE")

    def test_partie_import(self) -> None:
        """Import Anvers → Amsterdam : à vide vers le port (3 h), attente et chargement, livraison, fin."""
        demande = self.decider(demande_test(self.ref, loc_dep="Anvers", loc_arr="Amsterdam", type_mat="MP Frais", h_dep_h=5.0,
                                            type_mvt="Import", type_dep="PORT", type_arr="USINE"))
        tournee = self.partir(demande)
        depart = tournee["plan"]["suivi"]["D1"]["depart"]
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, apres(1.0)), "VIDE_VERS_PORT")
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, apres(3.5)), "ATTENTE_PORT")
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, depart), "EN_LIVRAISON")
        self.assertEqual(archivage.statut_trajet(tournee, self.etat, tournee["plan"]["fin"]), "TERMINE")
        self.assertEqual(self.commande(demande)["H_DEP_R"], T0)

    def test_tournee_annulee(self) -> None:
        """Tournée annulée (D-DEPART, REF-RENTABILITE) : trajet ANNULE, commande REFUSEE liée au trajet."""
        demande = self.decider(demande_test(self.ref, h_dep_h=12.0))
        tournee = self.etat["tournees"]["T00001"]
        decision.refuser(demande, "REF-RENTABILITE", T0, self.journal)
        tournee["statut"] = "annulee"
        self.assertEqual(archivage.ligne_trajet(tournee, self.etat, T0)[-1], "ANNULE")
        valeurs = self.commande(demande)
        self.assertEqual((valeurs["STATUT"], valeurs["ID_TRAJET"]), ("REFUSEE", "T00001"))

    def test_camion_disponible(self) -> None:
        """DISPO : libre au départ, occupé pendant la tournée, libre de nouveau à sa fin."""
        camion = self.etat["flotte"]["F1"]
        self.assertTrue(archivage.ligne_camion(camion, T0)[-1])
        tournee = self.partir(self.decider(demande_test(self.ref, qte=14, h_dep_h=12.0)))
        self.assertFalse(archivage.ligne_camion(camion, T0)[-1])
        self.assertTrue(archivage.ligne_camion(camion, tournee["plan"]["fin"])[-1])
        verifier_contraintes(self, "CAMION_NORTH", archivage.ligne_camion(camion, T0))

    def test_requete_upsert(self) -> None:
        """La clé primaire sert au ON CONFLICT et n'est jamais mise à jour."""
        requete = archivage.requete_upsert("TRAJET_NORTH")
        self.assertIn('ON CONFLICT ("ID_TRAJET") DO UPDATE', requete)
        self.assertNotIn('"ID_TRAJET" = EXCLUDED', requete)
        self.assertEqual(requete.count("%s"), 4)


class TestPrix(BaseArchivage):
    """Prix facturé (``methode-contrat.md`` §3, R-GAM)."""

    def test_ordre_des_gammes(self) -> None:
        """Haute > médiane > basse, à demande égale."""
        demande, camion = demande_test(self.ref), self.etat["flotte"]["F1"]
        prix = [planification.prix_prestation(demande, camion, self.ref, COUTS, g) for g in ("haute", "mediane", "basse")]
        self.assertGreater(prix[0], prix[1])
        self.assertGreater(prix[1], prix[2])

    def test_proportionnel_aux_palettes(self) -> None:
        """Deux fois plus de palettes, deux fois le prix."""
        camion = self.etat["flotte"]["F1"]
        p4 = planification.prix_prestation(demande_test(self.ref, qte=4), camion, self.ref, COUTS, "mediane")
        p8 = planification.prix_prestation(demande_test(self.ref, qte=8), camion, self.ref, COUTS, "mediane")
        self.assertAlmostEqual(p8, 2 * p4, places=1)

    def test_sans_annexe2(self) -> None:
        """Taux par défaut (pas de fichier confidentiel) : COUT_PREST reste vide."""
        demande = self.decider(demande_test(self.ref, qte=14, h_dep_h=12.0))
        self.partir(demande)
        ligne = archivage.ligne_commande(demande, self.etat, self.ref, dict(COUTS, source="defaut"))
        self.assertIsNone(ligne[archivage.COLONNES["ARCHIVAGE_COMMANDE_NORTH"].index("COUT_PREST")])


class TestSynchronisation(BaseArchivage):
    """Écritures en base : ordre, écarts seulement, erreurs, reprise."""

    def archive(self, connexion: FausseConnexion) -> dict:
        """Archive branchée sur une connexion simulée.

        :param connexion: Connexion simulée.
        :return: Archive.
        """
        return archivage.nouvelle_archive(lambda: connexion, (ErreurLigne,))

    def test_ordre_et_ecarts(self) -> None:
        """Ordre des clés étrangères ; rien à réécrire si rien ne change ; puis seulement ce qui a changé."""
        connexion = FausseConnexion()
        archive = self.archive(connexion)
        demande = self.decider(demande_test(self.ref, qte=14, h_dep_h=12.0))
        self.assertEqual(archivage.synchroniser(archive, self.etat, self.ref, COUTS, T0), 3)
        self.assertEqual(connexion.tables_ecrites(), archivage.TABLES)
        self.assertEqual(archivage.synchroniser(archive, self.etat, self.ref, COUTS, T0), 0)
        tournee = self.partir(demande)
        connexion.requetes.clear()
        # Décision de départ : la commande passe PARTIE ; le trajet reste EN_CONSTITUTION pendant le chargement
        archivage.synchroniser(archive, self.etat, self.ref, COUTS, T0)
        self.assertEqual(connexion.tables_ecrites(), ["ARCHIVAGE_COMMANDE_NORTH"])
        connexion.requetes.clear()
        archivage.synchroniser(archive, self.etat, self.ref, COUTS, tournee["plan"]["suivi"]["D1"]["depart"])
        self.assertEqual(connexion.tables_ecrites(), ["TRAJET_NORTH"])
        connexion.requetes.clear()
        archivage.synchroniser(archive, self.etat, self.ref, COUTS, tournee["plan"]["fin"])
        self.assertEqual(connexion.tables_ecrites(), ["CAMION_NORTH", "TRAJET_NORTH"])
        self.assertEqual(archive["cache"]["TRAJET_NORTH"]["T00001"][-1], "TERMINE")

    def test_base_injoignable(self) -> None:
        """Base injoignable : le moteur continue, rien n'est mis en cache, tout est écrit au retour de la base."""
        connexion, disponible = FausseConnexion(), [False]

        def connecter() -> FausseConnexion:
            """Connexion qui échoue tant que la base est indisponible.

            :return: Connexion simulée.
            :raises OSError: si la base est indisponible.
            """
            if not disponible[0]:
                raise OSError("base injoignable")
            return connexion

        archive = archivage.nouvelle_archive(connecter, (ErreurLigne,))
        self.decider(demande_test(self.ref, h_dep_h=12.0))
        self.assertEqual(archivage.synchroniser(archive, self.etat, self.ref, COUTS, T0), 0)
        self.assertEqual(archive["cache"]["CAMION_NORTH"], {})
        disponible[0] = True
        self.assertEqual(archivage.synchroniser(archive, self.etat, self.ref, COUTS, apres(0.1)), 3)

    def test_ligne_rejetee(self) -> None:
        """Une ligne refusée par la base est mise de côté ; les autres sont écrites."""
        connexion = FausseConnexion(rejets=("D1",))
        archive = self.archive(connexion)
        self.decider(demande_test(self.ref, h_dep_h=12.0))
        self.assertEqual(archivage.synchroniser(archive, self.etat, self.ref, COUTS, T0), 2)
        self.assertIn(("ARCHIVAGE_COMMANDE_NORTH", "D1"), archive["rejetees"])
        self.assertEqual(archivage.lignes_modifiees(archive, self.etat, self.ref, COUTS, T0)["ARCHIVAGE_COMMANDE_NORTH"], [])

    def test_reprise_apres_arret(self) -> None:
        """E18 : les commandes déjà en base ne sont ni redécidées ni réécrites."""
        connexion = FausseConnexion(deja=("D1",))
        archive = self.archive(connexion)
        archivage.synchroniser(archive, self.etat, self.ref, COUTS, T0)
        self.assertTrue(connexion.requetes[0][0].startswith('UPDATE "ARCHIVAGE_COMMANDE_NORTH"'))
        self.assertEqual(self.etat["deja_archivees"], {"D1"})
        champs = {"TYPE_MVT": "Export", "LOC_DEP": "Amsterdam", "LOC_ARR": "Paris", "TYPE_DEP": "USINE", "TYPE_ARR": "GMS",
                  "TYPE_MAT": "PF Frais", "QTE_PAL": 4, "TYPE_PAL": "EPAL", "H_DEP": apres(12).strftime("%d/%m/%y %H:%M:%S"), "SPEC": ""}
        moteur.executer_tick(self.etat, self.ref, [(T0, "D1", champs)], T0, self.parametres, self.journal)
        self.assertNotIn("D1", self.etat["demandes"])
        # Même présente dans l'état, une commande protégée n'est pas réécrite
        self.decider(demande_test(self.ref, h_dep_h=12.0))
        self.assertEqual(archivage.lignes_modifiees(archive, self.etat, self.ref, COUTS, T0)["ARCHIVAGE_COMMANDE_NORTH"], [])


if __name__ == "__main__":
    unittest.main()
