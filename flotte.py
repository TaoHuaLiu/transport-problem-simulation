"""Flotte de camions NORTH : lecture des fiches et état de chaque camion.

Rien ne donne la position réelle des camions : le moteur la tient à jour
lui-même. Un camion est représenté par un dictionnaire :

- champs utiles de la fiche : ``IMMAT``, ``REFERENCE``, ``PTAC``, ``ENERGIE``,
  ``COULEUR`` et ``frigo`` (booléen tiré de ``REMORQUE REFRIGEREE``) ;
- ``usine`` : usine d'attache (``ASSIGNED_TRUCKS``) ;
- ``position`` et ``libre_a`` : lieu et heure réelle où il sera libre ;
- ``conduite_jour`` : heures projet conduites depuis le dernier repos quotidien ;
- ``tournee`` : identifiant de la tournée ouverte qui le réserve, ou ``None``.

Cet état n'existe qu'en mémoire : pour qu'un moteur relancé ne redonne pas
un camion déjà parti (règle R-ROB-07), :func:`sauvegarder_etat` l'écrit à
chaque changement dans ``../donnees/run/flotte_moteur.json`` et
:func:`restaurer_etat` le relit au démarrage, le même jour réel.
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


#: Motifs d'écart d'une fiche camion (R-FLO-01), dans l'ordre où ils sont testés.
MOTIFS_ECART = ("illisible", "sans PTAC ou ENERGIE", "PTAC absent de TRUCKS_CAPACITY", "REFERENCE illisible",
                "hors usines NORTH")


def charger_flotte(repertoire: str, ref: dict, usines: list, debut: datetime.datetime, ecartees: dict = None) -> dict:
    """Charge les camions rattachés aux usines NORTH.

    Applique la règle R-FLO-01 : un camion dont la fiche n'a pas de ``PTAC``
    ou d'``ENERGIE``, ou dont le PTAC est absent de ``TRUCKS_CAPACITY``, est
    écarté (on ne peut calculer ni sa capacité ni son coût). Au démarrage,
    chaque camion est libre à son usine d'attache (hypothèse C9).

    :param repertoire: Dossier des fiches camion.
    :param ref: Référentiel.
    :param usines: Clés ``PK_LOCATIONS`` des usines NORTH.
    :param debut: Heure réelle de démarrage.
    :param ecartees: Si donné, rempli sur place : ``{motif: [nom de fichier]}`` pour chaque fiche
        écartée, motifs de :data:`MOTIFS_ECART` (journalisés par l'appelant).
    :return: ``{IMMAT: camion}``.
    """
    flotte = {}
    if ecartees is None:
        ecartees = {}

    def ecarter(motif: str, nom: str) -> None:
        """Note une fiche écartée.

        :param motif: Motif, parmi :data:`MOTIFS_ECART`.
        :param nom: Nom du fichier.
        """
        ecartees.setdefault(motif, []).append(nom)

    for nom in sorted(os.listdir(repertoire)):
        chemin = os.path.join(repertoire, nom)
        if not os.path.isfile(chemin) or nom.startswith("__exemple__"):
            continue
        fiche = lire_fiche(chemin)
        if not fiche:
            ecarter("illisible", nom)
            continue
        if not fiche.get("PTAC") or not fiche.get("ENERGIE"):
            ecarter("sans PTAC ou ENERGIE", nom)
            continue
        if fiche["PTAC"] not in ref["capacite"]:
            ecarter("PTAC absent de TRUCKS_CAPACITY", nom)
            continue
        try:
            reference = int(fiche.get("REFERENCE", ""))
        except (TypeError, ValueError):
            ecarter("REFERENCE illisible", nom)
            continue
        usine = referentiels.usine_attache(ref, reference)
        if usine not in usines:
            ecarter("hors usines NORTH", nom)
            continue
        immat = fiche.get("IMMAT") or os.path.splitext(nom)[0]
        flotte[immat] = {"IMMAT": immat, "REFERENCE": reference, "PTAC": fiche["PTAC"], "ENERGIE": fiche["ENERGIE"],
                         "COULEUR": fiche.get("COULEUR", ""),
                         "frigo": str(fiche.get("REMORQUE REFRIGEREE", "")).strip().lower() == "oui",
                         "usine": usine, "position": usine, "libre_a": debut, "conduite_jour": 0.0, "tournee": None}
    return flotte


def etat_a_sauvegarder(flotte: dict, maintenant: datetime.datetime) -> dict:
    """État des camions à garder d'un lancement du moteur à l'autre.

    Applique la règle R-ROB-07 : position, heure de fin de la tournée en
    cours (``libre_a``) et heures conduites, pour chaque camion de la flotte.
    La réservation par une tournée ouverte (``tournee``) n'est pas gardée :
    au redémarrage, les tournées ouvertes sont perdues et leurs commandes
    refusées (``archivage.reprendre``), le camion reste donc libre.
    Le fichier porte aussi, pour le tableau de bord seulement (:func:`audit.resume_flotte`),
    le type de camion (``frigo``), ``PTAC``, ``ENERGIE``, ``COULEUR`` (gammes du contrat), l'``usine`` et ``reserve``
    (affecté à une tournée ouverte) : :func:`restaurer_etat` ne les relit pas.

    :param flotte: Flotte du moteur.
    :param maintenant: Heure réelle courante.
    :return: ``{"jour": "AAAA-MM-JJ", "camions": {immat: {"position", "libre_a", "conduite_jour", "frigo", "PTAC",
        "ENERGIE", "COULEUR", "usine", "reserve"}}}``.
    """
    return {"jour": maintenant.strftime("%Y-%m-%d"),
            "camions": {immat: {"position": camion["position"], "libre_a": camion["libre_a"].isoformat(timespec="seconds"),
                                "conduite_jour": round(float(camion["conduite_jour"]), 4),
                                "frigo": bool(camion.get("frigo")), "PTAC": camion.get("PTAC", ""),
                                "ENERGIE": camion.get("ENERGIE", ""), "COULEUR": camion.get("COULEUR", ""),
                                "usine": camion.get("usine"), "reserve": camion.get("tournee") is not None}
                        for immat, camion in flotte.items()}}


def sauvegarder_etat(flotte: dict, chemin: str, maintenant: datetime.datetime, precedent: str = None):
    """Écrit l'état des camions sur disque s'il a changé (fichier remplacé d'un coup).

    Applique la règle R-ROB-07. Ne lève jamais d'erreur : un disque plein
    n'arrête pas le moteur, l'appelant journalise l'échec.

    :param flotte: Flotte du moteur.
    :param chemin: Fichier JSON.
    :param maintenant: Heure réelle courante.
    :param precedent: Contenu écrit la fois précédente (rien n'est écrit s'il est identique).
    :return: ``(contenu, erreur)`` : contenu écrit (ou ``precedent``) et ``None``, ou ``precedent`` et l'exception.
    :rtype: tuple
    """
    try:
        contenu = json.dumps(etat_a_sauvegarder(flotte, maintenant), ensure_ascii=False, sort_keys=True)
        if contenu == precedent:
            return precedent, None
        os.makedirs(os.path.dirname(os.path.abspath(chemin)), exist_ok=True)
        with open(chemin + ".tmp", "w", encoding="utf-8") as fichier:
            fichier.write(contenu)
        os.replace(chemin + ".tmp", chemin)
        return contenu, None
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as erreur:
        return precedent, erreur


def restaurer_etat(flotte: dict, ref: dict, chemin: str, maintenant: datetime.datetime) -> dict:
    """Reprend l'état des camions écrit par le lancement précédent du même jour réel.

    Applique la règle R-ROB-07 : un camion encore en tournée au moment de
    l'arrêt reste occupé jusqu'à la fin prévue de cette tournée, et repart
    du lieu où il la finit (hypothèse C10) ; ses heures de conduite sont
    gardées. Sans cela, le moteur relancé le croirait libre à son usine et
    pourrait lui donner une deuxième tournée en même temps
    (``TRACABILITE_TRAJET`` montrerait la même immatriculation sur deux
    trajets simultanés). Les fiches camion sont régénérées chaque jour réel :
    un fichier d'un autre jour est ignoré. Un camion inconnu de la flotte ou
    une valeur illisible est ignoré, le camion garde l'état des fiches. Ne
    lève jamais d'erreur.

    :param flotte: Flotte chargée par :func:`charger_flotte`, modifiée sur place.
    :param ref: Référentiel (lieux connus).
    :param chemin: Fichier écrit par :func:`sauvegarder_etat`.
    :param maintenant: Heure réelle de démarrage.
    :return: ``{"restaures", "occupes", "ignores", "raison"}`` ; ``raison`` vaut ``None`` si le fichier a été repris.
    """
    bilan = {"restaures": 0, "occupes": 0, "ignores": 0, "raison": None}
    if not os.path.isfile(chemin):
        bilan["raison"] = "aucun état précédent"
        return bilan
    try:
        with open(chemin, encoding="utf-8") as fichier:
            sauvegarde = json.load(fichier)
        if sauvegarde.get("jour") != maintenant.strftime("%Y-%m-%d"):
            bilan["raison"] = "état du %s, pas d'aujourd'hui" % sauvegarde.get("jour")
            return bilan
        camions = sauvegarde["camions"]
        if not isinstance(camions, dict):
            raise ValueError("champ camions illisible")
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as erreur:
        bilan["raison"] = "fichier illisible (%r)" % erreur
        return bilan
    for immat, valeurs in camions.items():
        camion = flotte.get(immat)
        try:
            position = valeurs["position"]
            libre_a = datetime.datetime.fromisoformat(valeurs["libre_a"])
            conduite = float(valeurs["conduite_jour"])
            if camion is None or position not in ref["lieux"] or conduite < 0:
                raise ValueError("camion ou lieu inconnu")
        except (KeyError, TypeError, ValueError):
            bilan["ignores"] += 1
            continue
        camion.update({"position": position, "libre_a": max(libre_a, camion["libre_a"]), "conduite_jour": conduite})
        bilan["restaures"] += 1
        bilan["occupes"] += libre_a > maintenant
    return bilan


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
