"""Tests du recalcul des indicateurs d'audit (:mod:`audit`).

Journal et demandes fictifs, construits à la main : chaque cas vérifie un
statut final (R-KPI-TA), le remplissage moyen (R-KPI-TR), un contrôle ou le
barème. Lancement, depuis ``1 - DEV/`` : ``python3 -m unittest tests.test_audit``.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import audit  # noqa: E402

#: Heure réelle de référence des tests.
T0 = datetime.datetime(2026, 10, 21, 16, 0, 0)


def minute(n: float) -> datetime.datetime:
    """Heure réelle ``T0 + n`` minutes.

    :param n: Minutes réelles après ``T0``.
    :return: Date.
    """
    return T0 + datetime.timedelta(minutes=n)


def demande(id_demande: str, recu: float, h_dep: float, usine: str = "AMSTERDAM", sens: str = "Export") -> dict:
    """Demande telle que lue par :func:`audit._lire_fichier_demandes`.

    :param id_demande: Identifiant.
    :param recu: Réception, en minutes après ``T0``.
    :param h_dep: ``H_DEP``, en minutes après ``T0``.
    :param usine: Usine NORTH (``""`` pour une demande hors périmètre).
    :param sens: ``Export`` ou ``Import``.
    :return: Demande.
    """
    return {"id": id_demande, "h_reception": minute(recu), "h_dep": minute(h_dep), "type_mvt": sens,
            "usine": usine, "loc_dep": "Amsterdam", "loc_arr": "Paris", "qte_pal": "5"}


def evenement(n: float, type_evenement: str, id_demande: str = "", tournee: str = "", **champs) -> dict:
    """Ligne du journal du moteur.

    :param n: Heure de l'événement, en minutes après ``T0``.
    :param type_evenement: ``ACQUITTEMENT``, ``REFUS``, ``DEPART``…
    :param id_demande: Demande concernée.
    :param tournee: Tournée concernée.
    :param champs: Autres colonnes (``REMPLISSAGE``, ``GAMME``, ``DETAIL``…).
    :type champs: str
    :return: Événement, avec ``H`` déjà lue.
    """
    ligne = {colonne: "" for colonne in ("ID_TOURNEE", "IMMAT", "LIEU_DEP", "LIEU_ARR", "H_DEBUT", "H_FIN", "DUREE_H",
                                         "REMPLISSAGE", "DEMANDES_A_BORD", "GAMME", "MOTIF", "DETAIL")}
    ligne.update({"H": minute(n), "H_EVENEMENT": minute(n).strftime(audit.FORMAT_DATE), "EVENEMENT": type_evenement,
                  "ID_DEMANDE": id_demande, "ID_TOURNEE": tournee, "IMMAT": "AA-1"})
    ligne.update(champs)
    return ligne


def troncon(n: float, tournee: str, debut: float, fin: float, remplissage: float) -> dict:
    """Événement ``TRONCON`` d'une tournée.

    :param n: Heure de l'événement (minutes après ``T0``).
    :param tournee: Tournée.
    :param debut: Début du trajet (minutes après ``T0``).
    :param fin: Fin du trajet (minutes après ``T0``).
    :param remplissage: Part du plancher occupée sur le trajet.
    :return: Événement.
    """
    return evenement(n, "TRONCON", tournee=tournee, LIEU_DEP="Amsterdam", LIEU_ARR="Paris",
                     H_DEBUT=minute(debut).strftime(audit.FORMAT_DATE), H_FIN=minute(fin).strftime(audit.FORMAT_DATE),
                     DUREE_H="%.2f" % (fin - debut), REMPLISSAGE="%.3f" % remplissage)


def sources_test() -> dict:
    """Jeu de cinq demandes NORTH (une par statut final) et une hors périmètre.

    - D1 : acquittée puis partie (gamme haute) dans T1, remplie à 80 % ;
    - D2 : refusée REF-DELAI ;
    - D3 : acquittée puis expirée (compte comme refus) ;
    - D4 : acquittée, ``H_DEP`` dépassée sans départ (en retard) ;
    - D5 : reçue, aucune décision ;
    - X1 : hors périmètre mais présente au journal.

    :return: Sources au format de :func:`audit.charger_sources`.
    """
    demandes = {"D1": demande("D1", 0, 8), "D2": demande("D2", 1, 9), "D3": demande("D3", 2, 10),
                "D4": demande("D4", 3, 5), "D5": demande("D5", 4, 12, "MANCHESTER", "Import"),
                "X1": demande("X1", 4, 12, "")}
    evenements = [evenement(0.1, "ACQUITTEMENT", "D1", "T1", REMPLISSAGE="0.500"),
                  evenement(1.1, "REFUS", "D2", MOTIF="REF-DELAI"),
                  evenement(2.1, "ACQUITTEMENT", "D3", "T2"),
                  evenement(3.1, "ACQUITTEMENT", "D4", "T3"),
                  evenement(4.1, "ACQUITTEMENT", "X1", "T4"),
                  evenement(6.0, "DEPART", tournee="T1", REMPLISSAGE="0.800"),
                  troncon(6.0, "T1", 6.0, 16.0, 0.8),
                  troncon(6.0, "T1", 16.0, 26.0, 0.0),
                  evenement(6.0, "PARTIE", "D1", "T1", GAMME="haute", DETAIL="départ 16:06:00, arrivée 16:16:00"),
                  evenement(9.0, "EXPIRATION", "D3", "T2", MOTIF="REF-DELAI")]
    return {"journal": "test", "evenements": evenements, "fichiers_demandes": [], "demandes": demandes, "maj_journal": None}


class TestPerimetre(unittest.TestCase):
    """Règles R-PER-01 et R-PER-02, recalculées sans :mod:`regles`."""

    def test_export_usine_north(self) -> None:
        """Un export depuis une usine NORTH est à nous, même avec un accent."""
        self.assertEqual(audit.usine_north({"TYPE_DEP": "USINE", "LOC_DEP": "Düsseldorf"}), "DUSSELDORF")

    def test_import_vers_usine_north(self) -> None:
        """Un import d'un port vers une usine NORTH est à nous."""
        champs = {"TYPE_DEP": "PORT", "TYPE_ARR": "USINE", "LOC_DEP": "Athenes", "LOC_ARR": "Manchester"}
        self.assertEqual(audit.usine_north(champs), "MANCHESTER")

    def test_import_depuis_le_port_d_amsterdam(self) -> None:
        """Amsterdam est aussi un port : un import qui en part vers Séville n'est pas à nous."""
        champs = {"TYPE_DEP": "PORT", "TYPE_ARR": "USINE", "LOC_DEP": "Amsterdam", "LOC_ARR": "Seville"}
        self.assertEqual(audit.usine_north(champs), "")


class TestStatuts(unittest.TestCase):
    """Statut final de chaque demande (R-KPI-TA)."""

    def setUp(self) -> None:
        """Calcule les statuts du jeu de test à ``T0 + 10 min``."""
        sources = sources_test()
        self.statuts = audit.statuts_demandes(sources["evenements"], sources["demandes"], minute(10))

    def test_un_statut_par_cas(self) -> None:
        """Partie, refusée, expirée = refusée, en retard, sans décision."""
        attendus = {"D1": "partie", "D2": "refusee", "D3": "refusee", "D4": "en_retard", "D5": "sans_decision"}
        self.assertEqual({i: self.statuts[i]["statut"] for i in attendus}, attendus)

    def test_acquittee_puis_expiree(self) -> None:
        """Une demande acquittée puis expirée est marquée pour le contrôle ACQ_PUIS_REFUS."""
        self.assertTrue(self.statuts["D3"]["acq_puis_refus"])
        self.assertFalse(self.statuts["D2"]["acq_puis_refus"])

    def test_hors_perimetre(self) -> None:
        """Une demande hors NORTH présente au journal est signalée."""
        self.assertTrue(self.statuts["X1"]["hors_perimetre"])

    def test_depart_controle(self) -> None:
        """L'heure de départ comparée à H_DEP est lue dans le détail de PARTIE."""
        self.assertEqual(self.statuts["D1"]["depart"], minute(6))


class TestCalcul(unittest.TestCase):
    """Indicateurs et contrôles sur la fenêtre."""

    def setUp(self) -> None:
        """Calcule le rapport sur tout le jeu de test, cible TA 50 %."""
        self.rapport = audit.calculer(sources_test(), minute(10), "journal", seuil=0.6, cibles={"ta": 0.5})

    def test_ta(self) -> None:
        """TA = 1 partie / 5 demandes NORTH reçues ; la demande hors périmètre ne compte pas."""
        self.assertEqual(self.rapport["acceptation"]["recues"], 5)
        self.assertAlmostEqual(self.rapport["acceptation"]["ta"], 0.2)

    def test_tr(self) -> None:
        """TR = remplissage au départ (80 %), pas le remplissage pondéré des tronçons (40 %)."""
        self.assertAlmostEqual(self.rapport["remplissage"]["tr"], 0.8)
        self.assertAlmostEqual(self.rapport["remplissage"]["tr_troncons"], 0.4)
        self.assertAlmostEqual(self.rapport["flotte"]["part_heures_vide"], 0.5)

    def test_gammes_et_motifs(self) -> None:
        """La partie est en gamme haute ; deux refus REF-DELAI (dont l'expiration)."""
        self.assertEqual(self.rapport["gammes"]["comptes"]["haute"], 1)
        self.assertEqual(self.rapport["acceptation"]["motifs"], {"REF-DELAI": 2})

    def test_controles(self) -> None:
        """Les contrôles bloquants voient le retard, la demande sans décision et le hors-périmètre."""
        defauts = {c["code"]: c["n"] for c in self.rapport["controles"] if not c["ok"]}
        self.assertEqual(defauts.get("EN_RETARD"), 1)
        self.assertEqual(defauts.get("SANS_DECISION"), 1)
        self.assertEqual(defauts.get("HORS_PERIMETRE"), 1)
        self.assertNotIn("PARTIE_APRES_HDEP", defauts)

    def test_fenetre(self) -> None:
        """Une fenêtre de 2 min ne garde que les demandes reçues dedans (D1, D2, D3)."""
        rapport = audit.calculer(sources_test(), minute(10), "journal", debut=T0, duree_min=2)
        self.assertEqual(rapport["acceptation"]["recues"], 3)


class TestBareme(unittest.TestCase):
    """Écart relatif unilatéral et barème du sujet."""

    def test_au_dessus_de_la_cible(self) -> None:
        """Faire mieux que la cible : écart nul, note maximale."""
        self.assertEqual(audit.ecart_et_note(0.9, 0.85), {"cible": 0.85, "ecart": 0.0, "note": 10.0})

    def test_paliers(self) -> None:
        """Écart 5,9 % → 8 (contrat) ; 20 % → 3 ; 60 % → 0."""
        self.assertEqual(audit.ecart_et_note(0.80, 0.85)["note"], 8.0)
        self.assertEqual(audit.ecart_et_note(0.68, 0.85)["note"], 3.0)
        self.assertEqual(audit.ecart_et_note(0.34, 0.85)["note"], 0.0)

    def test_bareme_ta_plus_severe(self) -> None:
        """TA (§4.3) : un écart de 20 % donne 0, contre 3 pour le TR (§4.1)."""
        self.assertEqual(audit.ecart_et_note(0.68, 0.85, bareme=audit.BAREME_TA)["note"], 0.0)
        self.assertEqual(audit.ecart_et_note(0.75, 0.85, bareme=audit.BAREME_TA)["note"], 6.0)

    def test_sans_cible(self) -> None:
        """Sans cible saisie, pas de note."""
        self.assertIsNone(audit.ecart_et_note(0.8, None)["note"])



def flotte_test(ptac: str = "19t", frigo: bool = True, libre_a: float = 0) -> dict:
    """État de la flotte du moteur avec le camion ``AA-1`` des tests et un camion sec libre.

    :param ptac: PTAC du camion ``AA-1``.
    :param frigo: ``True`` si ``AA-1`` est frigorifique.
    :param libre_a: Fin de sa tournée, en minutes après ``T0``.
    :return: Contenu de ``flotte_moteur.json``.
    """
    camion = {"PTAC": ptac, "ENERGIE": "Diesel", "COULEUR": "Rouge", "frigo": frigo, "usine": 1, "position": 1,
              "libre_a": minute(libre_a).isoformat(timespec="seconds"), "conduite_jour": 0.0, "reserve": False}
    return {"jour": T0.strftime("%Y-%m-%d"),
            "camions": {"AA-1": camion, "BB-2": dict(camion, frigo=False, libre_a=T0.isoformat(timespec="seconds"))}}


class TestContrat(unittest.TestCase):
    """Cibles du jour, PTAC dépassé, gammes et note du contrat (§4.1 à §4.3)."""

    def test_cible_ta_lignes_du_contrat(self) -> None:
        """Tableau du §4.3, 2 116 demandes en 2 h dont 84 % fraîches : 500 camions → 23,3 %, 3 000 → plafond 75 %."""
        frais, sec = 1777, 339
        self.assertAlmostEqual(audit.cible_ta_contrat(frais, sec, 49, 56, 2.0)["valeur"], 0.236, places=3)
        self.assertAlmostEqual(audit.cible_ta_contrat(frais, sec, 294, 336, 2.0)["valeur"], 0.75, places=3)
        self.assertAlmostEqual(audit.cible_ta_contrat(frais, sec, 490, 560, 2.0)["valeur"], 0.75, places=3)

    def test_cible_ta_sans_flotte(self) -> None:
        """Sans N_F / N_S, pas de cible TA calculée."""
        self.assertIsNone(audit.cible_ta_contrat(10, 2, None, None, 2.0)["valeur"])

    def test_cible_tr_paliers(self) -> None:
        """§4.1 : 62 % dès 1 000 demandes/h, 55 % dès 500, 46 % dès 250, 40 % en dessous."""
        self.assertEqual([audit.cible_tr_contrat(q) for q in (1000, 999, 500, 250, 249)], [0.62, 0.55, 0.55, 0.46, 0.40])

    def test_ptac_depasse_compte_refusee(self) -> None:
        """D1 (SPEC PTAC=12t) partie dans un 19 t : refusée PTAC-DEPASSE, hors gamme, TA à 0."""
        sources = sources_test()
        sources["demandes"]["D1"].update({"spec_ptac": 12.0, "spec": {"PTAC": "12t"}})
        rapport = audit.calculer(sources, minute(10), "journal", flotte=flotte_test("19t"))
        self.assertEqual(rapport["acceptation"]["partie"], 0)
        self.assertEqual(rapport["acceptation"]["motifs"].get(audit.MOTIF_PTAC_DEPASSE), 1)
        self.assertEqual(rapport["gammes"]["comptes"]["hors"], 1)
        self.assertEqual({c["code"]: c["n"] for c in rapport["controles"]}["PTAC_DEPASSE"], 1)

    def test_ptac_inferieur_seulement_declasse(self) -> None:
        """Un camion plus léger que la SPEC n'est pas un PTAC dépassé : la demande reste partie, gamme basse."""
        sources = sources_test()
        sources["demandes"]["D1"].update({"spec_ptac": 26.0, "spec": {"PTAC": "26t"}})
        rapport = audit.calculer(sources, minute(10), "journal", flotte=flotte_test("19t"))
        self.assertEqual(rapport["acceptation"]["partie"], 1)
        self.assertEqual(rapport["gammes"]["comptes"]["basse"], 1)

    def test_gamme_contrat(self) -> None:
        """Le plus bas des critères : avance ≤ 1 h → médiane ; une SPEC sur deux respectée → médiane."""
        camion = {"PTAC": "19t", "ENERGIE": "Diesel", "COULEUR": "Rouge"}
        self.assertEqual(audit.gamme_contrat(0.5, {}, camion)["gamme"], "mediane")
        self.assertEqual(audit.gamme_contrat(0.0, {}, camion)["gamme"], "basse")
        self.assertEqual(audit.gamme_contrat(5.0, {"COL": "rouge", "ENR": "GPL"}, camion)["gamme"], "mediane")
        self.assertEqual(audit.gamme_contrat(5.0, {"COL": "Bleu"}, None)["gamme"], "haute")  # SPEC non vérifiable

    def test_cibles_du_jour_et_note(self) -> None:
        """Avec la flotte du moteur : N_F = 1, N_S = 1, cible TA calculée ; note = TR + gammes + TA."""
        rapport = audit.calculer(sources_test(), minute(10), "journal", flotte=flotte_test(libre_a=30))
        cible = rapport["cibles"]["ta"]
        self.assertEqual((cible["camions_frigo"], cible["camions_sec"], cible["source"]), (1, 1, "contrat"))
        self.assertIsNotNone(cible["valeur"])
        self.assertEqual(rapport["score"]["max"], 40)
        self.assertEqual(rapport["camions"]["types"]["frigo"]["en_route"], 1)
        self.assertEqual(rapport["camions"]["types"]["sec"]["libres"], 1)

    def test_carte_camions(self) -> None:
        """À 11 min, AA-1 roule sur son premier tronçon (6 → 16 min) ; BB-2 est à son usine (Amsterdam, PK 1)."""
        flotte = flotte_test()
        carte = audit.carte_camions(flotte, sources_test()["evenements"], minute(11))
        self.assertEqual([c["immat"] for c in carte["en_route"]], ["AA-1"])
        self.assertTrue(carte["en_route"][0]["charge"])
        amsterdam = [lieu for lieu in carte["lieux"] if lieu["pk"] == 1][0]
        self.assertEqual(amsterdam["sec"], 1)
        # Après la dernière arrivée (26 min), AA-1 est à l'arrêt au lieu d'arrivée (Paris)
        carte = audit.carte_camions(flotte, sources_test()["evenements"], minute(30))
        self.assertEqual(carte["en_route"], [])
        self.assertEqual([lieu["frigo"] for lieu in carte["lieux"] if lieu["nom"] == "Paris"], [1])

    def test_cible_imposee(self) -> None:
        """Une cible TA donnée remplace la formule ; N_F / N_S imposés remplacent la flotte."""
        rapport = audit.calculer(sources_test(), minute(10), "journal",
                                 cibles={"ta": 0.6, "camions_frigo": 3, "camions_sec": 4})
        self.assertEqual(rapport["cibles"]["ta"]["valeur"], 0.6)
        self.assertEqual(rapport["cibles"]["ta"]["source"], "fixee")
        self.assertEqual(rapport["cibles"]["ta"]["camions_frigo"], 3)


if __name__ == "__main__":
    unittest.main()
