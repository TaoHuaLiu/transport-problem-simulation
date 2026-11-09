"""Tests des tables de décision D-ACC et D-DEPART : un test par ligne de table.

Le référentiel est fictif et réduit (Amsterdam usine, Anvers port, Paris GMS,
Brest usine hors NORTH) pour que chaque cas soit calculable à la main.
Durées en heures projet ; 1 heure projet = 1 minute réelle.

Lancement, depuis ``1 - DEV/`` : ``python3 -m unittest tests.test_decision``.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import decision  # noqa: E402
import hypotheses  # noqa: E402
import planification  # noqa: E402
import referentiels  # noqa: E402
import regles  # noqa: E402
import tracabilite  # noqa: E402

#: Heure réelle de référence des tests.
T0 = datetime.datetime(2026, 10, 19, 15, 0, 0)

#: Taux horaires fictifs : seul le classement des plans compte ici.
COUTS = {"taux_conducteur_h": 1.0, "taux_vehicule_h": 1.0, "seuil_rentabilite": 0.6, "source": "test"}


def referentiel_test() -> dict:
    """Construit le référentiel fictif des tests.

    :return: Référentiel au format de :mod:`referentiels`.
    """
    ref = {"lieux": {1: {"NAME": "Amsterdam", "COUNTRY": "Pays-Bas"}, 2: {"NAME": "Anvers", "COUNTRY": "Belgique"},
                     4: {"NAME": "Brest", "COUNTRY": "France"}, 13: {"NAME": "Paris", "COUNTRY": "France"},
                     6: {"NAME": "Dusseldorf", "COUNTRY": "Allemagne"}, 10: {"NAME": "Manchester", "COUNTRY": "Angleterre"}},
           "affectations": [(1, 100000, 199999)],
           "capacite": {"19t": {"EPAL": 20, "EPAL (half)": 37, "ISO": 15}},
           "consommation": {"19t": {"DIESEL (L)": 25.2, "GASOLINE (L)": 30.2, "LPG (kg)": 24.6, "ELECTRIC (kWh)": 130.2}},
           "trajets": {(1, 1): 1.1, (1, 2): 3.0, (1, 13): 6.0, (2, 2): 1.0, (2, 13): 4.0, (13, 13): 1.0,
                       (1, 4): 10.0, (4, 13): 8.0, (2, 4): 9.0, (4, 4): 1.0},
           "ports": {2: [30.0, 30.0, 30.0]}, "source": "test"}
    referentiels.construire_index(ref)
    return ref


def camion_test(immat: str, frigo: bool = True, couleur: str = "Blanc", position: int = 1) -> dict:
    """Construit un camion de 19 t diesel rattaché à Amsterdam.

    :param immat: Immatriculation.
    :param frigo: Remorque réfrigérée.
    :param couleur: Couleur.
    :param position: Lieu où le camion est libre.
    :return: Camion au format de :mod:`flotte`.
    """
    return {"IMMAT": immat, "REFERENCE": 150000, "PTAC": "19t", "ENERGIE": "Diesel", "COULEUR": couleur, "frigo": frigo,
            "usine": 1, "position": position, "libre_a": T0, "conduite_jour": 0.0, "tournee": None}


def demande_test(ref: dict, id_demande: str = "D1", loc_dep: str = "Amsterdam", loc_arr: str = "Paris",
                 type_mat: str = "PF Frais", qte: int = 4, h_dep_h: float = 10.0, spec: str = "",
                 type_mvt: str = "Export", type_dep: str = "USINE", type_arr: str = "GMS") -> dict:
    """Construit une demande préparée, reçue à :data:`T0`.

    :param ref: Référentiel.
    :param id_demande: Identifiant.
    :param loc_dep: Ville de départ.
    :param loc_arr: Ville d'arrivée.
    :param type_mat: Type de marchandise.
    :param qte: Nombre de palettes EPAL.
    :param h_dep_h: ``H_DEP`` en heures projet après :data:`T0`.
    :param spec: Champ ``SPEC``.
    :param type_mvt: ``Export`` ou ``Import``.
    :param type_dep: Type du lieu de départ.
    :param type_arr: Type du lieu d'arrivée.
    :return: Demande préparée par :func:`regles.preparer_demande`.
    """
    h_dep = T0 + datetime.timedelta(minutes=h_dep_h)
    champs = {"TYPE_MVT": type_mvt, "LOC_DEP": loc_dep, "LOC_ARR": loc_arr, "TYPE_DEP": type_dep, "TYPE_ARR": type_arr,
              "TYPE_MAT": type_mat, "QTE_PAL": qte, "TYPE_PAL": "EPAL", "H_DEP": h_dep.strftime("%d/%m/%y %H:%M:%S"), "SPEC": spec}
    return regles.preparer_demande(id_demande, champs, T0, ref)


class TestDACC(unittest.TestCase):
    """Table D-ACC : une méthode par ligne (première ligne qui correspond)."""

    def setUp(self) -> None:
        """Prépare un référentiel, une flotte d'un frigo et un journal temporaire."""
        self.ref = referentiel_test()
        self.etat = {"flotte": {"F1": camion_test("F1")}, "tournees": {}, "demandes": {}, "compteur": 0}
        self.parametres = {"couts": COUTS, "seuil": 0.6, "marge_h": 1.0}
        self.journal = tracabilite.ouvrir_journal(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_build", "tests"), T0, "_test")

    def decider(self, demande: dict) -> str:
        """Applique D-ACC à une demande, à l'heure :data:`T0`.

        :param demande: Demande préparée.
        :return: ``"ignorer"``, ``"refuser"`` ou ``"accepter"``.
        """
        return decision.traiter_demande(demande, self.etat, self.ref, T0, self.parametres, self.journal)

    def test_ligne_1_hors_perimetre(self) -> None:
        """D-ACC ligne 1 (R-PER-01) : un export de Brest n'est pas à NORTH."""
        demande = demande_test(self.ref, loc_dep="Brest")
        self.assertEqual(self.decider(demande), "ignorer")
        self.assertEqual(demande["etat"], "IGNOREE")

    def test_ligne_2_donnees_invalides(self) -> None:
        """D-ACC ligne 2 (R-VAL-01) : ville d'arrivée inconnue → REF-DONNEES."""
        demande = demande_test(self.ref, loc_arr="Atlantis")
        self.assertEqual(self.decider(demande), "refuser")
        self.assertEqual(demande["motif"], "REF-DONNEES")

    def test_ligne_3_spec_impossible(self) -> None:
        """D-ACC ligne 3 (R-SPEC-01), SPEC bloquante : aucune couleur Violet dans la flotte → REF-SPEC."""
        ancien, hypotheses.SPEC_BLOQUANTE = hypotheses.SPEC_BLOQUANTE, True
        try:
            demande = demande_test(self.ref, spec="COL=Violet")
            self.decider(demande)
            self.assertEqual(demande["motif"], "REF-SPEC")
        finally:
            hypotheses.SPEC_BLOQUANTE = ancien

    def test_import_lointain_depart_usine(self) -> None:
        """R-DEL-01 (import) : port à 10 h, H_DEP dans 5 h → accepté, car le camion quitte l'usine à vide avant H_DEP."""
        demande = demande_test(self.ref, loc_dep="Brest", loc_arr="Amsterdam", type_mat="MP Frais", h_dep_h=5.0,
                               type_mvt="Import", type_dep="PORT", type_arr="USINE")
        self.assertEqual(self.decider(demande), "accepter")
        suivi = self.etat["tournees"][demande["tournee"]]["plan"]["suivi"][demande["id"]]
        self.assertEqual(suivi["depart_controle"], T0)
        self.assertGreater(suivi["depart"], demande["h_dep"])

    def test_import_camion_libre_trop_tard(self) -> None:
        """R-DEL-01 (import) : le seul frigo n'est libre qu'après H_DEP → REF-DELAI."""
        self.etat["flotte"]["F1"]["libre_a"] = T0 + datetime.timedelta(minutes=6)
        demande = demande_test(self.ref, loc_dep="Brest", loc_arr="Amsterdam", type_mat="MP Frais", h_dep_h=5.0,
                               type_mvt="Import", type_dep="PORT", type_arr="USINE")
        self.decider(demande)
        self.assertEqual(demande["motif"], "REF-DELAI")

    def test_export_controle_au_chargement(self) -> None:
        """R-DEL-01 (export) : l'instant comparé à H_DEP reste le départ chargé de LOC_DEP."""
        demande = demande_test(self.ref)
        self.decider(demande)
        suivi = self.etat["tournees"][demande["tournee"]]["plan"]["suivi"][demande["id"]]
        self.assertEqual(suivi["depart_controle"], suivi["depart"])

    def test_spec_non_tenue_acceptee(self) -> None:
        """R-SPEC-02, SPEC non bloquante : aucune couleur Violet → la demande est acceptée quand même."""
        ancien, hypotheses.SPEC_BLOQUANTE = hypotheses.SPEC_BLOQUANTE, False
        try:
            self.assertEqual(self.decider(demande_test(self.ref, spec="COL=Violet")), "accepter")
        finally:
            hypotheses.SPEC_BLOQUANTE = ancien

    def test_spec_tenue_preferee(self) -> None:
        """R-SPEC-02 : entre deux camions libres, celui qui respecte la SPEC est choisi."""
        self.etat["flotte"]["R2"] = camion_test("R2", couleur="Rouge")
        demande = demande_test(self.ref, spec="COL=Rouge")
        self.decider(demande)
        self.assertEqual(self.etat["tournees"][demande["tournee"]]["camion"], "R2")

    def test_ligne_4_pas_de_frigo(self) -> None:
        """D-ACC ligne 4 (R-FRA-01) : produit frais, flotte sans frigo → REF-FRIGO."""
        self.etat["flotte"] = {"S1": camion_test("S1", frigo=False)}
        demande = demande_test(self.ref)
        self.decider(demande)
        self.assertEqual(demande["motif"], "REF-FRIGO")

    def test_ligne_4_sec_sans_camion_sec(self) -> None:
        """R-FRA-02 : produit sec, flotte d'un seul frigo libre → REF-CAMION (pas de sec en frigo)."""
        demande = demande_test(self.ref, type_mat="PF")
        self.decider(demande)
        self.assertEqual(demande["motif"], "REF-CAMION")

    def test_ligne_5_aucun_camion_libre(self) -> None:
        """D-ACC ligne 5 (R-FLO-01) : le seul camion est réservé par une tournée de frais → REF-CAMION."""
        self.assertEqual(self.decider(demande_test(self.ref, id_demande="D0")), "accepter")
        demande = demande_test(self.ref, id_demande="D1", type_mat="PF")
        self.decider(demande)
        self.assertEqual(demande["motif"], "REF-CAMION")

    def test_ligne_6_delai(self) -> None:
        """D-ACC ligne 6 (R-DEL-01) : H_DEP dans 1 h projet, chargement de 1 h 47 → REF-DELAI."""
        demande = demande_test(self.ref, h_dep_h=1.0)
        self.decider(demande)
        self.assertEqual(demande["motif"], "REF-DELAI")

    def test_ligne_7_conducteur(self) -> None:
        """D-ACC ligne 7 (R-CON-01) : 6 h de route vers l'usine ; la pause de 45 min fait manquer H_DEP → REF-CONDUCTEUR.

        Sans pause : 6 + 1 h 47 = 7,78 h ; avec pause : 8,53 h ; H_DEP à 8 h.
        """
        self.etat["flotte"] = {"F1": camion_test("F1", position=13)}
        demande = demande_test(self.ref, h_dep_h=8.0)
        self.decider(demande)
        self.assertEqual(demande["motif"], "REF-CONDUCTEUR")

    def test_ligne_8_insertion(self) -> None:
        """D-ACC ligne 8 (R-OPT-03) : une seconde demande compatible rejoint la tournée ouverte."""
        self.etat["flotte"]["F2"] = camion_test("F2")
        self.assertEqual(self.decider(demande_test(self.ref, id_demande="D1")), "accepter")
        self.assertEqual(self.decider(demande_test(self.ref, id_demande="D2")), "accepter")
        self.assertEqual(len(self.etat["tournees"]), 1)
        self.assertEqual(self.etat["tournees"]["T00001"]["demandes"], ["D1", "D2"])

    def test_ligne_9_nouvelle_tournee(self) -> None:
        """D-ACC ligne 9 : sans tournée ouverte, la demande ouvre une tournée avec le camion libre."""
        demande = demande_test(self.ref)
        self.assertEqual(self.decider(demande), "accepter")
        self.assertEqual(demande["tournee"], "T00001")
        self.assertEqual(self.etat["flotte"]["F1"]["tournee"], "T00001")


class TestDDEPART(unittest.TestCase):
    """Table D-DEPART : une méthode par ligne."""

    def setUp(self) -> None:
        """Ouvre une tournée d'une demande avec le camion F1."""
        self.ref = referentiel_test()
        self.etat = {"flotte": {"F1": camion_test("F1")}, "tournees": {}, "demandes": {}, "compteur": 0}
        self.parametres = {"couts": COUTS, "seuil": 0.6, "marge_h": 1.0}
        self.journal = tracabilite.ouvrir_journal(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_build", "tests"), T0, "_test")

    def ouvrir(self, qte: int, h_dep_h: float) -> dict:
        """Accepte une demande et renvoie la tournée ouverte.

        :param qte: Palettes EPAL (capacité du 19 t : 20).
        :param h_dep_h: ``H_DEP`` en heures projet après :data:`T0`.
        :return: Tournée ouverte.
        """
        decision.traiter_demande(demande_test(self.ref, qte=qte, h_dep_h=h_dep_h), self.etat, self.ref, T0, self.parametres, self.journal)
        return self.etat["tournees"]["T00001"]

    def test_ligne_1_seuil_atteint(self) -> None:
        """D-DEPART ligne 1 (R-RENT-01) : 14 EPAL sur 20 = 70 % ≥ 60 % → partir."""
        tournee = self.ouvrir(qte=14, h_dep_h=12.0)
        self.assertEqual(planification.decider_depart(tournee, self.etat, self.ref, T0, self.parametres), "partir")

    def test_ligne_2_attendre(self) -> None:
        """D-DEPART ligne 2 : 4 EPAL = 20 %, départ au plus tard dans plus de W → attendre."""
        tournee = self.ouvrir(qte=4, h_dep_h=12.0)
        self.assertEqual(planification.decider_depart(tournee, self.etat, self.ref, T0, self.parametres), "attendre")

    def test_ligne_3_heure_limite(self) -> None:
        """D-DEPART ligne 3 : sous le seuil, marge inférieure à W → partir (ou refuser selon SOUS_SEUIL)."""
        tournee = self.ouvrir(qte=4, h_dep_h=2.5)
        self.assertEqual(planification.decider_depart(tournee, self.etat, self.ref, T0, self.parametres), "partir")
        ancien, hypotheses.SOUS_SEUIL = hypotheses.SOUS_SEUIL, "refuser"
        try:
            self.assertEqual(planification.decider_depart(tournee, self.etat, self.ref, T0, self.parametres), "refuser")
        finally:
            hypotheses.SOUS_SEUIL = ancien

    def test_depart_ecrit_les_troncons(self) -> None:
        """Après le départ (R-DEL-01), la demande est partie avant H_DEP ; sans retour à l'usine (C10), le dernier tronçon est la livraison."""
        tournee = self.ouvrir(qte=14, h_dep_h=12.0)
        planification.faire_partir(tournee, self.etat, self.ref, T0, COUTS, self.journal)
        demande = self.etat["demandes"]["D1"]
        self.assertEqual(demande["etat"], "PARTIE")
        self.assertLessEqual(demande["h_depart"], demande["h_dep"])
        self.assertGreater(tournee["plan"]["troncons"][-1]["remplissage"], 0.0)
        self.assertEqual(self.etat["flotte"][tournee["camion"]]["position"], tournee["plan"]["lieu_fin"])
        self.assertEqual(demande["gamme"], "haute")


class TestDeclassementSpec(unittest.TestCase):
    """Règle R-SPEC-02 : une SPEC non tenue abaisse la gamme."""

    def test_un_cran(self) -> None:
        """Mode « un_cran » : haute → médiane, médiane → basse, basse reste basse."""
        ancien, hypotheses.DECLASSEMENT_SPEC = hypotheses.DECLASSEMENT_SPEC, "un_cran"
        try:
            self.assertEqual(planification.declasser_gamme("haute", True), "mediane")
            self.assertEqual(planification.declasser_gamme("mediane", True), "basse")
            self.assertEqual(planification.declasser_gamme("basse", True), "basse")
            self.assertEqual(planification.declasser_gamme("haute", False), "haute")
        finally:
            hypotheses.DECLASSEMENT_SPEC = ancien

    def test_toujours_basse(self) -> None:
        """Mode « basse » : une SPEC non tenue donne toujours la gamme basse."""
        ancien, hypotheses.DECLASSEMENT_SPEC = hypotheses.DECLASSEMENT_SPEC, "basse"
        try:
            self.assertEqual(planification.declasser_gamme("haute", True), "basse")
        finally:
            hypotheses.DECLASSEMENT_SPEC = ancien


class TestConduite(unittest.TestCase):
    """Règle R-CON-01 : pauses et repos insérés dans un trajet."""

    def test_pause_apres_4h30(self) -> None:
        """6 h de conduite → une pause de 45 min."""
        self.assertAlmostEqual(planification.conduire(6.0, {"jour": 0.0, "continue": 0.0}), 6.75)

    def test_repos_apres_12h(self) -> None:
        """13 h de conduite → deux pauses, puis 11 h de repos avant la 13e heure."""
        self.assertAlmostEqual(planification.conduire(13.0, {"jour": 0.0, "continue": 0.0}), 13 + 2 * 0.75 + 11)


if __name__ == "__main__":
    unittest.main()
