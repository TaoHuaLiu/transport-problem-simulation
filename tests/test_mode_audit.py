"""Tests du mode audit, le mode de production (règle R-ROB-05).

Le mode audit lit toujours la base : il refuse de démarrer sur une copie
JSON, sur les fiches camion locales ou avec des coûts fictifs. La base, le
dossier des fiches de la VM et l'Annexe 2 sont simulés.

Lancement, depuis ``1 - DEV/`` : ``python3 -m unittest discover -s tests``.
"""

import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flotte  # noqa: E402
import hypotheses  # noqa: E402
import moteur  # noqa: E402
import referentiels  # noqa: E402
from test_decision import T0, referentiel_test  # noqa: E402

DOSSIER_CODE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def options_audit() -> argparse.Namespace:
    """Options de la ligne de commande telles que ``moteur.py --audit`` les fixe.

    :return: Options du moteur.
    """
    return argparse.Namespace(audit=True, direct=True, bdd=True, archiver=True, rejeu=None,
                              referentiels=None, fiches=None, seuil=None, marge=None)


class TestPreparerAudit(unittest.TestCase):
    """R-ROB-05 : conditions de démarrage du moteur en mode audit."""

    def setUp(self) -> None:
        """Simule les fiches de la VM (un camion NORTH), l'Annexe 2 et une copie JSON du référentiel."""
        self.dossier = tempfile.mkdtemp()
        self.fiches = os.path.join(self.dossier, "FichesCamion")
        os.makedirs(self.fiches)
        with open(os.path.join(self.fiches, "AA-123-AA.json"), "w", encoding="utf-8") as fichier:
            json.dump({"IMMAT": "AA-123-AA", "REFERENCE": "150000", "PTAC": "19t", "ENERGIE": "Diesel",
                       "REMORQUE REFRIGEREE": "oui", "COULEUR": "Blanc"}, fichier)
        self.annexe = os.path.join(self.dossier, "annexe2_confidentiel.json")
        with open(self.annexe, "w", encoding="utf-8") as fichier:
            json.dump({"taux_conducteur_h": 1.0, "taux_vehicule_h": 1.0, "seuil_rentabilite": 0.5}, fichier)
        self.copie = os.path.join(self.dossier, "referentiels.json")
        referentiels.sauver_json(referentiel_test(), self.copie)
        # charger_confidentiel fixe son chemin par défaut à l'import : on le redirige vers le fichier simulé
        lire_annexe = hypotheses.charger_confidentiel
        self.patches = [mock.patch.object(flotte, "REPERTOIRE_FICHES_VM", self.fiches),
                        mock.patch.object(referentiels, "FICHIER_REFERENTIELS", self.copie),
                        mock.patch.object(hypotheses, "charger_confidentiel", side_effect=lambda: lire_annexe(self.annexe))]
        for patch in self.patches:
            patch.start()
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()

    def tearDown(self) -> None:
        """Retire les simulations et le dossier temporaire."""
        self.console.__exit__(None, None, None)
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.dossier)

    def test_demarre_sur_la_base(self) -> None:
        """Base, fiches de la VM et Annexe 2 présentes : le moteur démarre sur la base."""
        ref_bdd = dict(referentiel_test(), source="bdd")
        with mock.patch.object(referentiels, "charger_depuis_bdd", return_value=ref_bdd):
            etat, ref, parametres = moteur.preparer(options_audit(), T0)
        self.assertEqual(ref["source"], "bdd")
        self.assertEqual(list(etat["flotte"]), ["AA-123-AA"])
        self.assertEqual(parametres["couts"]["source"], self.annexe)

    def test_base_injoignable_sans_copie_de_secours(self) -> None:
        """Base injoignable : refus, même si la copie JSON existe (elle sert hors audit seulement)."""
        with mock.patch.object(referentiels, "charger_depuis_bdd", side_effect=OSError("connexion refusée")):
            with self.assertRaises(moteur.ErreurAudit):
                moteur.preparer(options_audit(), T0)

    def test_fiches_de_la_vm_obligatoires(self) -> None:
        """Fiches de la VM absentes : refus (pas de repli sur ``6 - Data/camions``)."""
        shutil.rmtree(self.fiches)
        with mock.patch.object(referentiels, "charger_depuis_bdd", return_value=referentiel_test()):
            with self.assertRaises(moteur.ErreurAudit):
                moteur.preparer(options_audit(), T0)

    def test_annexe2_obligatoire(self) -> None:
        """Annexe 2 absente : refus (pas de coûts fictifs ni de prix manquants en production)."""
        os.remove(self.annexe)
        with mock.patch.object(referentiels, "charger_depuis_bdd", return_value=referentiel_test()):
            with self.assertRaises(moteur.ErreurAudit):
                moteur.preparer(options_audit(), T0)

    def test_aucun_camion_north(self) -> None:
        """Aucun camion NORTH exploitable dans les fiches : refus."""
        os.remove(os.path.join(self.fiches, "AA-123-AA.json"))
        with mock.patch.object(referentiels, "charger_depuis_bdd", return_value=referentiel_test()):
            with self.assertRaises(moteur.ErreurAudit):
                moteur.preparer(options_audit(), T0)

    def test_hors_audit_la_copie_reste_permise(self) -> None:
        """Hors audit (``--direct --bdd``), la copie JSON reste le secours de R-ROB-02."""
        options = options_audit()
        options.audit = False
        with mock.patch.object(referentiels, "charger_depuis_bdd", side_effect=OSError("connexion refusée")):
            _, ref, _ = moteur.preparer(options, T0)
        self.assertEqual(ref["source"], self.copie)


class TestLigneDeCommande(unittest.TestCase):
    """R-ROB-05 : options interdites en mode audit (moteur et ``nord.sh``)."""

    def lancer(self, commande: list, dossier: str = DOSSIER_CODE) -> subprocess.CompletedProcess:
        """Lance une commande et renvoie son résultat.

        :param commande: Commande et arguments.
        :param dossier: Dossier de travail.
        :return: Résultat (code de sortie, sorties).
        """
        return subprocess.run(commande, cwd=dossier, capture_output=True, text=True, timeout=60)

    def test_moteur_refuse_les_sources_de_remplacement(self) -> None:
        """``moteur.py --audit`` refuse ``--referentiels`` et ``--fiches``."""
        for option in ("--referentiels", "--fiches"):
            resultat = self.lancer([sys.executable, "moteur.py", "--audit", option, "x"])
            self.assertEqual(resultat.returncode, 2, option)
            self.assertIn("interdits", resultat.stderr)

    def test_audit_impose_base_archivage_et_controle(self) -> None:
        """``--audit`` impose ``--bdd``, ``--archiver`` et ``--controle`` (tables du Contrôle remplies)."""
        with open(os.path.join(DOSSIER_CODE, "moteur.py"), encoding="utf-8") as fichier:
            principal = fichier.read().split('if __name__ == "__main__":')[-1]
        self.assertIn("options.direct = options.bdd = options.archiver = options.controle = True", principal)

    def test_nord_sh_refuse_les_options_degradees(self) -> None:
        """``nord.sh audit`` refuse les options qui retirent la base, l'archivage ou exposent l'Annexe 2."""
        with tempfile.TemporaryDirectory() as dossier:
            # Copie isolée : nord.sh crée ../donnees/run et ../donnees/logs à côté de lui
            dev = os.path.join(dossier, "DEV")
            os.makedirs(dev)
            shutil.copy(os.path.join(DOSSIER_CODE, "nord.sh"), dev)
            for option in ("--sans-bdd", "--sans-archive", "--sans-controle", "--confidentiel", "--local"):
                resultat = self.lancer(["sh", "nord.sh", "audit", option], dev)
                self.assertEqual(resultat.returncode, 2, option)
                self.assertIn("interdite en mode audit", resultat.stderr)
            self.assertFalse(os.path.exists(os.path.join(dossier, "donnees", "run", "options")))



class TestDispositionDonnees(unittest.TestCase):
    """R-ROB-05 : l'Annexe 2 est trouvée dans le paquet VM (``../donnees/``) et dans le miroir GitHub (``donnees/``)."""

    def setUp(self) -> None:
        """Crée un dossier ``racine/DEV`` vide (ni ``racine/donnees`` ni ``DEV/donnees``)."""
        self.racine = tempfile.mkdtemp()
        self.code = os.path.join(self.racine, "DEV")
        os.makedirs(self.code)
        self.voisin = os.path.join(self.racine, "donnees", "annexe2_confidentiel.json")
        self.interne = os.path.join(self.code, "donnees", "annexe2_confidentiel.json")

    def tearDown(self) -> None:
        """Supprime le dossier temporaire."""
        shutil.rmtree(self.racine)

    def poser(self, chemin: str) -> None:
        """Écrit un fichier Annexe 2 minimal.

        :param chemin: Chemin du fichier à créer.
        """
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        with open(chemin, "w", encoding="utf-8") as fichier:
            json.dump({"taux_conducteur_h": 1.0}, fichier)

    def chemin(self) -> str:
        """Chemin retenu par :func:`hypotheses.chemin_donnees_contrat`, normalisé.

        :return: Chemin absolu normalisé.
        """
        return os.path.normpath(hypotheses.chemin_donnees_contrat("annexe2_confidentiel.json", self.code))

    def test_paquet_vm(self) -> None:
        """Fichier dans ``../donnees/`` : il est retenu."""
        self.poser(self.voisin)
        self.assertEqual(self.chemin(), os.path.normpath(self.voisin))

    def test_miroir_github(self) -> None:
        """Fichier seulement dans ``donnees/`` du dossier de code : il est retenu."""
        self.poser(self.interne)
        self.assertEqual(self.chemin(), os.path.normpath(self.interne))
        self.assertEqual(hypotheses.charger_confidentiel(self.chemin())["source"], self.chemin())

    def test_les_deux_priorite_paquet(self) -> None:
        """Fichier aux deux endroits : ``../donnees/`` l'emporte."""
        self.poser(self.voisin)
        self.poser(self.interne)
        self.assertEqual(self.chemin(), os.path.normpath(self.voisin))

    def test_absent(self) -> None:
        """Fichier absent : chemin attendu ``../donnees/``, coûts par défaut (refusés en audit)."""
        self.assertEqual(self.chemin(), os.path.normpath(self.voisin))
        self.assertEqual(hypotheses.charger_confidentiel(self.chemin())["source"], "defaut")

    def lancer_nord_audit(self) -> str:
        """Lance ``nord.sh audit`` dans la copie isolée, avec un faux ``psycopg2`` (connexion refusée).

        Le faux module fait passer la vérification « psycopg2 présent » pour atteindre
        les conditions bloquantes ; la base injoignable et les fiches absentes font
        toujours refuser l'audit.

        :return: Sorties standard et d'erreur réunies.
        """
        shutil.copy(os.path.join(DOSSIER_CODE, "nord.sh"), self.code)
        faux = os.path.join(self.racine, "faux")
        os.makedirs(faux)
        with open(os.path.join(faux, "psycopg2.py"), "w", encoding="utf-8") as fichier:
            fichier.write("def connect(**options):\n    raise OSError('base simulée injoignable')\n")
        env = dict(os.environ, PYTHONPATH=faux)
        sortie = subprocess.run(["sh", "nord.sh", "audit"], cwd=self.code, capture_output=True, text=True,
                                timeout=60, env=env)
        texte = sortie.stdout + sortie.stderr
        self.assertNotEqual(sortie.returncode, 0)
        self.assertIn("Mode audit refusé", texte)
        return texte

    def test_nord_sh_audit_trouve_le_miroir(self) -> None:
        """``nord.sh audit`` ne signale pas l'Annexe 2 absente quand elle est dans ``donnees/`` (miroir)."""
        self.poser(self.interne)
        self.assertNotIn("annexe2_confidentiel.json absent", self.lancer_nord_audit())

    def test_nord_sh_audit_signale_l_absence(self) -> None:
        """``nord.sh audit`` signale l'Annexe 2 absente des deux emplacements."""
        self.assertIn("annexe2_confidentiel.json absent", self.lancer_nord_audit())


if __name__ == "__main__":
    unittest.main()
