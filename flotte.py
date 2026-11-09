"""Flotte de camions NORTH : lecture des fiches et état de chaque camion.

Rien ne donne la position réelle des camions : le moteur la tient à jour
lui-même. Un camion est représenté par un dictionnaire :

- champs utiles de la fiche : ``IMMAT``, ``REFERENCE``, ``PTAC``, ``ENERGIE``,
  ``COULEUR`` et ``frigo`` (booléen tiré de ``REMORQUE REFRIGEREE``) ;
- ``usine`` : usine d'attache (``ASSIGNED_TRUCKS``) ;
- ``position`` et ``libre_a`` : lieu et heure réelle où il sera libre ;
- ``conduite_jour`` : heures projet conduites depuis le dernier repos quotidien ;
- ``tournee`` : identifiant de la tournée ouverte qui le réserve, ou ``None``.
"""

import csv
import datetime
import json
import os

import referentiels

#: Répertoire des fiches camion sur la VM (lecture seule).
REPERTOIRE_FICHES_VM = "/mnt/tp4010/DuCapteurJusquauCloud/FichesCamion/"


def lire_fiche(chemin: str):
    """Lit une fiche camion, au format JSON ou CSV (``;``, une ligne d'en-tête).

    :param chemin: Chemin du fichier.
    :type chemin: str
    :return: Champs de la fiche (valeurs en texte), ou ``None`` si le fichier est illisible.
    :rtype: dict or None
    """
    try:
        with open(chemin, newline="", encoding="utf-8") as fichier:
            if chemin.lower().endswith(".json"):
                return json.load(fichier)
            if chemin.lower().endswith(".csv"):
                return next(csv.DictReader(fichier, delimiter=";"), None)
    except (OSError, ValueError):
        return None
    return None


def charger_flotte(repertoire: str, ref: dict, usines: list, debut: datetime.datetime) -> dict:
    """Charge les camions rattachés aux usines NORTH.

    Applique la règle R-FLO-01 : un camion dont la fiche n'a pas de ``PTAC``
    ou d'``ENERGIE``, ou dont le PTAC est absent de ``TRUCKS_CAPACITY``, est
    écarté (on ne peut calculer ni sa capacité ni son coût). Au démarrage,
    chaque camion est libre à son usine d'attache (hypothèse C9).

    :param repertoire: Dossier des fiches camion.
    :param ref: Référentiel.
    :param usines: Clés ``PK_LOCATIONS`` des usines NORTH.
    :param debut: Heure réelle de démarrage.
    :return: ``{IMMAT: camion}``.
    """
    flotte = {}
    for nom in sorted(os.listdir(repertoire)):
        chemin = os.path.join(repertoire, nom)
        if not os.path.isfile(chemin) or nom.startswith("__exemple__"):
            continue
        fiche = lire_fiche(chemin)
        if not fiche or not fiche.get("PTAC") or not fiche.get("ENERGIE") or fiche["PTAC"] not in ref["capacite"]:
            continue
        try:
            reference = int(fiche.get("REFERENCE", ""))
        except ValueError:
            continue
        usine = referentiels.usine_attache(ref, reference)
        if usine not in usines:
            continue
        immat = fiche.get("IMMAT") or os.path.splitext(nom)[0]
        flotte[immat] = {"IMMAT": immat, "REFERENCE": reference, "PTAC": fiche["PTAC"], "ENERGIE": fiche["ENERGIE"],
                         "COULEUR": fiche.get("COULEUR", ""),
                         "frigo": str(fiche.get("REMORQUE REFRIGEREE", "")).strip().lower() == "oui",
                         "usine": usine, "position": usine, "libre_a": debut, "conduite_jour": 0.0, "tournee": None}
    return flotte


def resume_flotte(flotte: dict, ref: dict) -> str:
    """Résume la flotte par usine : nombre de camions et de frigos.

    :param flotte: Flotte chargée.
    :param ref: Référentiel (noms des usines).
    :return: Texte d'une ligne par usine.
    """
    lignes = []
    for usine in sorted({camion["usine"] for camion in flotte.values()}):
        camions = [c for c in flotte.values() if c["usine"] == usine]
        lignes.append("  %-11s %3d camions, dont %3d frigorifiques"
                      % (ref["lieux"][usine]["NAME"], len(camions), sum(c["frigo"] for c in camions)))
    return "\n".join(lignes)
