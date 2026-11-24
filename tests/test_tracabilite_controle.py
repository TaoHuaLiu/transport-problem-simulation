"""Tests de l'interface avec les tables de traçabilité du Contrôle (:mod:`tracabilite_controle`).

Un test par ligne de la table D-JUSTIF (codes ``JUSTIF_REFUS``), puis les
lignes produites et l'écriture dans une base simulée (:class:`BaseControle`)
qui reproduit les deux tables, y compris des lignes d'un autre groupe.

Lancement, depuis ``1 - DEV/`` : ``python3 -m unittest discover -s tests``.
"""

import contextlib
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archivage  # noqa: E402
import hypotheses  # noqa: E402
import moteur  # noqa: E402
import tracabilite  # noqa: E402
import tracabilite_controle as tc  # noqa: E402
from test_decision import COUTS, T0, camion_test, referentiel_test  # noqa: E402

#: Paramètres de politique : θ = 0, la tournée part au premier tick.
PARTIR = {"couts": COUTS, "seuil": 0.0, "marge_h": 0.5}
#: θ = 1 : la tournée attend.
ATTENDRE = {"couts": COUTS, "seuil": 1.0, "marge_h": 0.5}


def champs(loc_dep: str = "Amsterdam", loc_arr: str = "Paris", type_mat: str = "PF Frais", qte: str = "4",
           spec: str = "") -> dict:
    """Champs bruts d'une demande, tels que le collecteur les écrit.

    :param loc_dep: Ville de départ.
    :param loc_arr: Ville d'arrivée.
    :param type_mat: Type de marchandise.
    :param qte: Nombre de palettes (texte, comme dans le CSV).
    :param spec: Champ ``SPEC``.
    :return: Champs de la demande.
    """
    h_dep = T0 + datetime.timedelta(minutes=10)
    return {"TYPE_MVT": "Export", "LOC_DEP": loc_dep, "LOC_ARR": loc_arr, "TYPE_DEP": "USINE", "TYPE_ARR": "GMS",
            "TYPE_MAT": type_mat, "QTE_PAL": qte, "TYPE_PAL": "EPAL", "H_DEP": h_dep.strftime("%d/%m/%y %H:%M:%S"), "SPEC": spec}


class ErreurLigne(Exception):
    """Erreur simulée due au contenu d'une ligne (comme ``psycopg2.IntegrityError``)."""


class BaseControle:
    """Base simulée : les deux tables du Contrôle en mémoire, connexion et curseur à la fois.

    :param rejets: Identifiants de commande dont l'écriture lève :class:`ErreurLigne`.
    """

    def __init__(self, rejets: tuple = ()) -> None:
        """Crée les tables vides.

        :param rejets: Identifiants refusés.
        """
        self.tables = {tc.TABLE_COMMANDE: {}, tc.TABLE_TRAJET: {}}
        self.rejets, self.requetes, self.rowcount, self.resultat = set(rejets), [], 0, []
        self.doublons = 0

    def cursor(self) -> "BaseControle":
        """Renvoie la base elle-même, qui sert de curseur.

        :return: La base.
        """
        return self

    def execute(self, requete: str, parametres: tuple = ()) -> None:
        """Exécute les quelques requêtes émises par le module.

        :param requete: Requête SQL.
        :param parametres: Paramètres.
        :raises ErreurLigne: Clé refusée, ou insertion d'une commande déjà présente.
        """
        self.requetes.append((requete, tuple(parametres)))
        table = requete.split('"')[1]
        commandes = self.tables[tc.TABLE_COMMANDE]
        if requete.startswith("UPDATE") and '"H_DEP_R" IS NULL' in requete:
            # Reprise : nos acceptées pas encore parties passent refusées
            code, motif = parametres
            cibles = [l for l in commandes.values() if l["STATUT"] == "ACCEPTEE" and l["H_DEP_R"] is None
                      and re.match(motif, l["ID_ENTRE"])]
            for ligne in cibles:
                ligne.update({"STATUT": "REFUSEE", "JUSTIF_REFUS": code})
            self.rowcount = len(cibles)
        elif requete.startswith("SELECT"):
            self.resultat = [(i, l["ID_ENTRE"]) for i, l in commandes.items() if re.match(parametres[0], l["ID_ENTRE"])]
        elif requete.startswith("UPDATE"):
            autres = [c for c in tc.COLONNES[table] if c != tc.CLE]
            valeurs = dict(zip(autres, parametres))
            cle = parametres[len(autres)]
            if cle in self.rejets:
                raise ErreurLigne("contrainte violée : %s" % cle)
            ligne = self.tables[table].get(cle)
            if ligne is not None and (table != tc.TABLE_COMMANDE or ligne["ID_ENTRE"] == parametres[len(autres) + 1]):
                ligne.update(valeurs)
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif requete.startswith("INSERT"):
            ligne = dict(zip(tc.COLONNES[table], parametres))
            if ligne[tc.CLE] in self.tables[table]:
                if table == tc.TABLE_COMMANDE:
                    raise ErreurLigne("clé primaire déjà présente : %s" % ligne[tc.CLE])
                self.doublons += 1
            self.tables[table][ligne[tc.CLE]] = ligne

    def fetchall(self) -> list:
        """Résultat du dernier ``SELECT``.

        :return: Lignes.
        """
        return self.resultat

    def commit(self) -> None:
        """Ne fait rien (pas de transaction simulée)."""

    def rollback(self) -> None:
        """Ne fait rien (pas de transaction simulée)."""

    def close(self) -> None:
        """Ne fait rien."""


class Contexte(unittest.TestCase):
    """Base commune : état du moteur (un camion frigo 19 t), journal temporaire, console masquée."""

    def setUp(self) -> None:
        """Prépare l'état du moteur."""
        self.ref = referentiel_test()
        self.etat = {"flotte": {"F1": camion_test("F1")}, "tournees": {}, "demandes": {}, "compteur": 0, "messages": {}}
        self.dossier = tempfile.TemporaryDirectory()
        self.journal = tracabilite.ouvrir_journal(self.dossier.name, T0)
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()

    def tearDown(self) -> None:
        """Rétablit la console et supprime le dossier temporaire."""
        self.console.__exit__(None, None, None)
        self.dossier.cleanup()

    def tick(self, nouvelles: list, parametres: dict = ATTENDRE) -> None:
        """Exécute un tick du moteur à :data:`T0`.

        :param nouvelles: ``(id, champs)``.
        :param parametres: Politique de départ.
        """
        moteur.executer_tick(self.etat, self.ref, [(T0, i, c) for i, c in nouvelles], T0, parametres, self.journal)


class TestDJustif(Contexte):
    """Table D-JUSTIF : un test par ligne (motif du moteur → code du Contrôle)."""

    def code(self, motif: str, etat: str = "REFUSEE", **autres: object) -> int:
        """Code d'une demande fictive.

        :param motif: Motif ``REF-…``.
        :param etat: État de la demande.
        :param autres: Autres champs (``capacite_seule``).
        :return: Code ``JUSTIF_REFUS``.
        """
        return tc.code_justif(dict({"etat": etat, "motif": motif}, **autres))

    def test_ligne_1_donnees(self) -> None:
        """REF-DONNEES → 7 DONNEES."""
        self.assertEqual(self.code("REF-DONNEES"), 7)

    def test_ligne_2_specificites(self) -> None:
        """REF-SPEC → 2 SPECIFICITES."""
        self.assertEqual(self.code("REF-SPEC"), 2)

    def test_ligne_3_capacite(self) -> None:
        """Camions compatibles libres, aucun avec la place → 3 CAPACITE (sec ou frais)."""
        self.assertEqual(self.code("REF-CAMION", capacite_seule=True), 3)
        self.assertEqual(self.code("REF-FRIGO", capacite_seule=True), 3)

    def test_ligne_4_moyen(self) -> None:
        """Aucun camion compatible libre → 8 MOYEN (dont « pas de camion frais »)."""
        self.assertEqual(self.code("REF-FRIGO"), 8)
        self.assertEqual(self.code("REF-CAMION", capacite_seule=False), 8)

    def test_ligne_5_delai(self) -> None:
        """REF-DELAI (refus ou expiration) et REF-CONDUCTEUR → 4 DELAI."""
        self.assertEqual(self.code("REF-DELAI"), 4)
        self.assertEqual(self.code("REF-DELAI", etat="EXPIREE"), 4)
        self.assertEqual(self.code("REF-CONDUCTEUR"), 4)

    def test_ligne_5_echeance_depassee_a_la_lecture(self) -> None:
        """REF-EXPIREE (H_DEP passée à la lecture, R-VAL-02) → 4 DELAI, pas 7 DONNEES."""
        self.assertEqual(self.code("REF-EXPIREE"), 4)

    def test_ligne_6_remplissage(self) -> None:
        """REF-RENTABILITE (tournée sous le seuil θ) → 6 REMPLISSAGE."""
        self.assertEqual(self.code("REF-RENTABILITE"), 6)

    def test_pas_de_code_sans_refus(self) -> None:
        """Acceptée ou partie : pas de code ; motif inconnu : 7."""
        self.assertIsNone(self.code("", etat="ACCEPTEE"))
        self.assertIsNone(self.code("", etat="PARTIE"))
        self.assertEqual(self.code("REF-INCONNU"), 7)

    def test_tous_les_motifs_du_moteur_ont_un_code(self) -> None:
        """Garde-fou : un nouveau motif REF-… doit recevoir un code dans D-JUSTIF."""
        traites = set(tc.MOTIF_VERS_CODE) | set(tc.MOTIFS_SANS_CAMION)
        self.assertEqual(set(archivage.LIBELLES_MOTIFS) - traites, set())
        self.assertTrue(set(tc.MOTIF_VERS_CODE.values()) <= set(tc.CODES_REFUS))

    def test_frais_sans_frigo_dans_le_moteur(self) -> None:
        """Bout en bout : frais, flotte sans frigo → REF-FRIGO, code 8."""
        self.etat["flotte"] = {"S1": camion_test("S1", frigo=False)}
        self.tick([("D1", champs())])
        self.assertEqual(tc.code_justif(self.etat["demandes"]["D1"]), 8)

    def test_trop_de_palettes_dans_le_moteur(self) -> None:
        """Bout en bout : 30 EPAL pour des camions de 20 → refus faute de place, code 3."""
        self.tick([("D1", champs(qte="30"))])
        self.assertEqual(self.etat["demandes"]["D1"]["etat"], "REFUSEE")
        self.assertEqual(tc.code_justif(self.etat["demandes"]["D1"]), 3)


class TestLignes(Contexte):
    """R-CTL-01 : lignes de TRACABILITE_COMMANDE et TRACABILITE_TRAJET."""

    def commande(self, id_demande: str) -> dict:
        """Ligne commande d'une demande, en dictionnaire.

        :param id_demande: Identifiant.
        :return: ``{colonne: valeur}`` ou ``{}`` si la demande n'est pas écrivable.
        """
        ligne = tc.ligne_commande(self.etat["demandes"][id_demande], "N1")
        return dict(zip(tc.COLONNES[tc.TABLE_COMMANDE], ligne)) if ligne else {}

    def test_commande_acceptee(self) -> None:
        """Acceptée : champs de l'ERP, SPEC en JSON, ID_ENTRE donné, pas de code ni de départ réel."""
        self.tick([("D1", champs(spec="PTAC=19t"))])
        ligne = self.commande("D1")
        self.assertEqual((ligne["STATUT"], ligne["JUSTIF_REFUS"], ligne["H_DEP_R"]), ("ACCEPTEE", None, None))
        self.assertEqual((ligne["LOC_DEP"], ligne["LOC_ARR"], ligne["QTE_MAT"]), ("Amsterdam", "Paris", 4))
        self.assertEqual(json.loads(ligne["SPEC"]), {"PTAC": "19t"})
        self.assertEqual(ligne["ID_ENTRE"], "N1")

    def test_commande_ville_inconnue(self) -> None:
        """Ville inconnue : refusée REF-DONNEES, écrite quand même avec le nom reçu et le code 7."""
        self.tick([("D1", champs(loc_arr="Atlantis"))])
        ligne = self.commande("D1")
        self.assertEqual((ligne["STATUT"], ligne["JUSTIF_REFUS"], ligne["LOC_ARR"]), ("REFUSEE", 7, "Atlantis"))

    def test_commande_quantite_illisible(self) -> None:
        """Quantité illisible : colonne obligatoire impossible à remplir, ligne non écrite (journal seulement)."""
        self.tick([("D1", champs(qte="abc"))])
        self.assertEqual(self.commande("D1"), {})

    def test_hors_perimetre_jamais_ecrite(self) -> None:
        """Commande d'une autre région : jamais écrite (pas de code 1 GEOGRAPHIE de notre part)."""
        self.tick([("D1", champs(loc_dep="Brest"))])
        lien = tc.nouveau_lien(BaseControle, (ErreurLigne,))
        self.assertEqual(tc.lignes_modifiees(lien, self.etat, self.ref, COUTS), {tc.TABLE_COMMANDE: [], tc.TABLE_TRAJET: []})

    def test_commande_et_trajet_partis(self) -> None:
        """Partie : STATUT ACCEPTEE, départ réel connu ; ligne trajet avec camion, type, capacité, prix et remplissage en %."""
        self.tick([("D1", champs())], PARTIR)
        self.assertEqual(self.etat["demandes"]["D1"]["etat"], "PARTIE")
        self.assertEqual(self.commande("D1")["STATUT"], "ACCEPTEE")
        self.assertIsNotNone(self.commande("D1")["H_DEP_R"])
        trajet = dict(zip(tc.COLONNES[tc.TABLE_TRAJET], tc.ligne_trajet(self.etat["demandes"]["D1"], self.etat, self.ref, COUTS)))
        self.assertEqual((trajet["IMMAT_CAMI"], trajet["TYPE_CAMI"], trajet["CAPA_CAMI"]), ("F1", "FRAIS", "19t"))
        self.assertGreater(trajet["COUT_PRESTA"], 0)
        self.assertTrue(0 < trajet["TAUX_REMPLI"] <= 100)
        self.assertEqual((trajet["SPEC_PTAC"], trajet["SPEC_ENER"], trajet["SPEC_COL"]), (None, None, None))

    def test_capacite_sans_espace(self) -> None:
        """CAPA_CAMI au format « 19t », sans espace, même si la fiche en contenait un."""
        self.assertEqual(tc.capacite_controle("19t"), "19t")
        self.assertEqual(tc.capacite_controle("19 t"), "19t")
        self.assertEqual(tc.capacite_controle(" 44 t "), "44t")

    def test_expiree_ecrite_refusee(self) -> None:
        """Acceptée puis expirée avant le départ : STATUT REFUSEE, code 4, pas de départ réel."""
        self.tick([("D1", champs())])
        self.etat["demandes"]["D1"].update({"etat": "EXPIREE", "motif": "REF-DELAI"})
        ligne = self.commande("D1")
        self.assertEqual((ligne["STATUT"], ligne["JUSTIF_REFUS"], ligne["H_DEP_R"]), ("REFUSEE", 4, None))

    def test_statuts_limites_a_la_specification(self) -> None:
        """Seuls ACCEPTEE et REFUSEE sont écrits, quel que soit l'état du moteur."""
        self.assertEqual(set(tc.STATUTS_CONTROLE.values()), {"ACCEPTEE", "REFUSEE"})
        self.assertEqual(set(tc.STATUTS_CONTROLE), {"ACCEPTEE", "PARTIE", "REFUSEE", "EXPIREE"})

    def test_trajet_sans_annexe2(self) -> None:
        """Taux par défaut : pas de prix réel, donc pas de ligne trajet (COUT_PRESTA obligatoire)."""
        self.tick([("D1", champs())], PARTIR)
        self.assertIsNone(tc.ligne_trajet(self.etat["demandes"]["D1"], self.etat, self.ref, dict(COUTS, source="defaut")))


class TestEcriture(Contexte):
    """R-CTL-01 : écriture dans les tables simulées, limitée à nos lignes."""

    def lien(self, base: BaseControle) -> dict:
        """Lien du module vers la base simulée.

        :param base: Base simulée.
        :return: Lien.
        """
        return tc.nouveau_lien(lambda: base, (ErreurLigne,))

    def synchro(self, lien: dict) -> int:
        """Synchronise à :data:`T0`.

        :param lien: Lien.
        :return: Lignes écrites.
        """
        return tc.synchroniser(lien, self.etat, self.ref, COUTS, T0)

    def test_insertion_puis_mise_a_jour(self) -> None:
        """Acceptée → insérée ; rien de neuf → rien écrit ; partie → commande mise à jour et trajet inséré."""
        base = BaseControle()
        lien = self.lien(base)
        self.tick([("D1", champs())])
        self.assertEqual(self.synchro(lien), 1)
        self.assertEqual(self.synchro(lien), 0)
        moteur.executer_tick(self.etat, self.ref, [], T0, PARTIR, self.journal)
        self.assertEqual(self.synchro(lien), 2)
        self.assertEqual(base.tables[tc.TABLE_COMMANDE]["D1"]["STATUT"], "ACCEPTEE")
        self.assertIsNotNone(base.tables[tc.TABLE_COMMANDE]["D1"]["H_DEP_R"])
        self.assertEqual(base.tables[tc.TABLE_COMMANDE]["D1"]["ID_ENTRE"], "N1")
        self.assertEqual(base.tables[tc.TABLE_TRAJET]["D1"]["IMMAT_CAMI"], "F1")
        self.assertEqual(base.doublons, 0)

    def test_id_entre_numerote(self) -> None:
        """ID_ENTRE = N1, N2… dans l'ordre d'écriture ; une commande garde son numéro à la mise à jour."""
        base = BaseControle()
        lien = self.lien(base)
        self.tick([("D1", champs()), ("D2", champs())])
        self.synchro(lien)
        self.tick([("D3", champs())])
        moteur.executer_tick(self.etat, self.ref, [], T0, PARTIR, self.journal)
        self.synchro(lien)
        numeros = {i: l["ID_ENTRE"] for i, l in base.tables[tc.TABLE_COMMANDE].items()}
        self.assertEqual(sorted(numeros.values()), ["N1", "N2", "N3"])
        self.assertEqual(numeros["D3"], "N3")
        self.assertTrue(all(re.match(hypotheses.MOTIF_ID_ENTRE, v) for v in numeros.values()))

    def test_commande_non_ecrivable_sans_numero(self) -> None:
        """Une commande non écrivable ne consomme pas de numéro."""
        base = BaseControle()
        self.tick([("D1", champs(qte="abc")), ("D2", champs())])
        self.synchro(self.lien(base))
        self.assertEqual(base.tables[tc.TABLE_COMMANDE]["D2"]["ID_ENTRE"], "N1")

    def test_reprise_apres_arret(self) -> None:
        """Redémarrage : nos acceptées non parties passent refusées (code 4), protégées ; numérotation reprise ; autres groupes intacts."""
        base = BaseControle()
        nous = dict.fromkeys(tc.COLONNES[tc.TABLE_COMMANDE], "")
        commandes = base.tables[tc.TABLE_COMMANDE]
        commandes["V1"] = dict(nous, ID_COMMANDE="V1", STATUT="ACCEPTEE", H_DEP_R=None, ID_ENTRE="N7")
        commandes["V2"] = dict(nous, ID_COMMANDE="V2", STATUT="ACCEPTEE", H_DEP_R=T0, ID_ENTRE="N3")
        commandes["W1"] = dict(nous, ID_COMMANDE="W1", STATUT="ACCEPTEE", H_DEP_R=None, ID_ENTRE="O40")
        self.tick([("D1", champs())])
        self.synchro(self.lien(base))
        self.assertEqual((commandes["V1"]["STATUT"], commandes["V1"]["JUSTIF_REFUS"]), ("REFUSEE", 4))
        self.assertEqual(commandes["V2"]["STATUT"], "ACCEPTEE")
        self.assertEqual(commandes["W1"]["STATUT"], "ACCEPTEE")
        self.assertEqual(commandes["D1"]["ID_ENTRE"], "N8")
        self.assertIn("V1", self.etat["deja_archivees"])
        self.assertNotIn("W1", self.etat["deja_archivees"])

    def test_ligne_d_un_autre_groupe_jamais_ecrasee(self) -> None:
        """Même identifiant déjà écrit par un autre groupe : notre écriture est refusée, sa ligne reste intacte."""
        base = BaseControle()
        base.tables[tc.TABLE_COMMANDE]["D1"] = dict.fromkeys(tc.COLONNES[tc.TABLE_COMMANDE], "")
        base.tables[tc.TABLE_COMMANDE]["D1"].update({"ID_COMMANDE": "D1", "ID_ENTRE": "O1", "STATUT": "ACCEPTEE"})
        lien = self.lien(base)
        lien["repris"] = True  # sans reprise : on teste seulement l'écriture
        self.tick([("D1", champs())])
        self.assertEqual(self.synchro(lien), 0)
        self.assertEqual(base.tables[tc.TABLE_COMMANDE]["D1"]["ID_ENTRE"], "O1")
        self.assertIn((tc.TABLE_COMMANDE, "D1"), lien["rejetees"])

    def test_ligne_rejetee_les_autres_ecrites(self) -> None:
        """Une ligne refusée par la base n'empêche pas les autres, et n'est pas renvoyée telle quelle."""
        base = BaseControle(rejets=("D1",))
        lien = self.lien(base)
        self.tick([("D1", champs()), ("D2", champs())])
        self.assertEqual(self.synchro(lien), 1)
        self.assertIn("D2", base.tables[tc.TABLE_COMMANDE])
        nb = len(base.requetes)
        self.assertEqual(self.synchro(lien), 0)
        self.assertEqual(len(base.requetes), nb)

    def test_mises_a_jour_limitees_a_notre_entreprise(self) -> None:
        """Toute mise à jour de TRACABILITE_COMMANDE porte la condition sur un de nos ID_ENTRE (N…)."""
        base = BaseControle()
        lien = self.lien(base)
        self.tick([("D1", champs())])
        self.synchro(lien)
        moteur.executer_tick(self.etat, self.ref, [], T0, PARTIR, self.journal)
        self.synchro(lien)
        updates = [(r, p) for r, p in base.requetes if r.startswith('UPDATE "%s"' % tc.TABLE_COMMANDE)]
        self.assertTrue(updates)
        for requete, parametres in updates:
            self.assertTrue('"ID_ENTRE" = %s' in requete or '"ID_ENTRE" ~ %s' in requete)
            self.assertTrue(any(isinstance(p, str) and (re.match(hypotheses.MOTIF_ID_ENTRE, p) or p == hypotheses.MOTIF_ID_ENTRE)
                                for p in parametres))


class TestSchema(unittest.TestCase):
    """``--verifier`` : comparaison avec la spécification du Contrôle."""

    def test_tables_conformes(self) -> None:
        """Toutes les colonnes au bon type : aucun écart."""
        colonnes = [(t, c, ty) for t, cols in tc.COLONNES_TYPES.items() for c, ty in cols]
        self.assertEqual(tc.ecarts_schema(colonnes), [])

    def test_ecarts_signales(self) -> None:
        """Table absente, colonne absente, mauvais type : chacun est signalé."""
        colonnes = [(tc.TABLE_COMMANDE, c, ty) for c, ty in tc.COLONNES_TYPES[tc.TABLE_COMMANDE]
                    if c != "JUSTIF_REFUS"]
        colonnes = [(t, c, "text" if c == "SPEC" else ty) for t, c, ty in colonnes]
        ecarts = tc.ecarts_schema(colonnes)
        self.assertEqual(len(ecarts), 3)
        self.assertTrue(any("TRACABILITE_TRAJET : table absente" in e for e in ecarts))


if __name__ == "__main__":
    unittest.main()
