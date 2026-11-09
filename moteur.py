"""Moteur d'acceptation NORTH : boucle principale, en direct ou en rejeu.

À chaque tick (:data:`hypotheses.TICK_S` secondes réelles), le moteur :

1. rafraîchit le référentiel (mode ``--bdd``, toutes les minutes réelles) ;
2. lit les nouvelles demandes ;
3. décide chaque demande (table D-ACC, :mod:`decision`) ;
4. décide le départ de chaque tournée ouverte (table D-DEPART, :mod:`planification`) ;
5. écrit les événements dans le journal de traçabilité.

Deux modes :

- **direct** (VM) : lit au fil de l'eau le CSV du jour écrit par
  ``collecteur.py`` ; l'horloge est l'heure réelle ;
- **rejeu** : relit un fichier ``.jsonl`` ou ``.csv`` du collecteur avec une
  horloge simulée, pour tester et régler les paramètres hors du VLAN.

Utilisation :

.. code-block:: bash

    python3 moteur.py --rejeu "../../6 - Data/Briefcase/demandes_2026-10-19.jsonl"   # local
    python3 moteur.py --direct --bdd                                                    # VM
"""

import argparse
import csv
import datetime
import json
import os
import time

import decision
import flotte as module_flotte
import hypotheses
import planification
import referentiels
import regles
import tracabilite
from collecteur import decoder_message

DOSSIER = os.path.dirname(os.path.abspath(__file__))

#: Fiches camion locales (copie de la VM), si le dossier de la VM est absent.
REPERTOIRE_FICHES_LOCAL = os.path.join(DOSSIER, "..", "..", "6 - Data", "camions")

#: Dossier des demandes écrites par le collecteur.
REPERTOIRE_DEMANDES = os.path.join(DOSSIER, "..", "donnees", "demandes")

#: Dossier des journaux du moteur.
REPERTOIRE_SORTIE = os.path.join(DOSSIER, "..", "donnees", "moteur")


def lire_rejeu(chemin: str) -> list:
    """Lit les demandes d'un fichier du collecteur, triées par heure de réception.

    Accepte le journal brut ``.jsonl`` (un message UDP par ligne) ou le
    ``.csv`` structuré (une demande par ligne).

    :param chemin: Fichier ``.jsonl`` ou ``.csv``.
    :return: Liste de ``(h_reception, id_demande, champs)``.
    """
    demandes = []
    with open(chemin, newline="", encoding="utf-8") as fichier:
        if chemin.endswith(".jsonl"):
            for ligne in fichier:
                entree = json.loads(ligne)
                h_reception = datetime.datetime.strptime(entree["H_RECEPTION"], "%Y-%m-%d %H:%M:%S")
                try:
                    message = decoder_message(entree["MESSAGE"])
                except ValueError:
                    continue
                demandes += [(h_reception, id_demande, champs) for id_demande, champs in message.items() if isinstance(champs, dict)]
        else:
            for ligne in csv.DictReader(fichier, delimiter=";"):
                h_reception = datetime.datetime.strptime(ligne["H_RECEPTION"], "%Y-%m-%d %H:%M:%S")
                demandes.append((h_reception, ligne["ID_DEMANDE"], ligne))
    return sorted(demandes, key=lambda element: element[0])


def lire_nouvelles_lignes(lecteur: dict, maintenant: datetime.datetime) -> list:
    """Lit les demandes ajoutées au CSV du jour depuis la lecture précédente.

    Change de fichier quand le jour réel change (le collecteur fait de même).

    :param lecteur: ``{"repertoire", "chemin", "position", "entete"}``, modifié sur place.
    :param maintenant: Heure réelle.
    :return: Liste de ``(h_reception, id_demande, champs)``.
    """
    chemin = os.path.join(lecteur["repertoire"], "demandes_%s.csv" % maintenant.strftime("%Y-%m-%d"))
    if chemin != lecteur.get("chemin"):
        lecteur.update({"chemin": chemin, "position": 0, "entete": None})
    if not os.path.exists(chemin):
        return []
    with open(chemin, "rb") as fichier:
        fichier.seek(lecteur["position"])
        contenu = fichier.read()
    # On ne garde que les lignes complètes ; la dernière peut être en cours d'écriture
    fin = contenu.rfind(b"\n") + 1
    lecteur["position"] += fin
    nouvelles = []
    for ligne in csv.reader(contenu[:fin].decode("utf-8").splitlines(), delimiter=";"):
        if lecteur["entete"] is None:
            lecteur["entete"] = ligne
            continue
        champs = dict(zip(lecteur["entete"], ligne))
        h_reception = datetime.datetime.strptime(champs["H_RECEPTION"], "%Y-%m-%d %H:%M:%S")
        nouvelles.append((h_reception, champs["ID_DEMANDE"], champs))
    return nouvelles


def executer_tick(etat: dict, ref: dict, nouvelles: list, maintenant: datetime.datetime,
                  parametres: dict, journal: dict) -> None:
    """Exécute un tick du moteur : décisions sur les demandes, puis départs.

    :param etat: État du moteur, modifié sur place.
    :param ref: Référentiel.
    :param nouvelles: Demandes reçues depuis le tick précédent, ``(h_reception, id, champs)``.
    :param maintenant: Heure réelle (ou simulée en rejeu).
    :param parametres: ``couts``, ``seuil``, ``marge_h``.
    :param journal: Journal de traçabilité.
    """
    for h_reception, id_demande, champs in nouvelles:
        if id_demande in etat["demandes"]:
            continue
        demande = regles.preparer_demande(id_demande, champs, h_reception, ref)
        decision.traiter_demande(demande, etat, ref, maintenant, parametres, journal)

    for tournee in [t for t in etat["tournees"].values() if t["statut"] == "ouverte"]:
        choix = planification.decider_depart(tournee, etat, ref, maintenant, parametres)
        if choix == "partir":
            planification.faire_partir(tournee, etat, ref, maintenant, parametres["couts"], journal)
        elif choix == "refuser":
            # D-DEPART ligne 3, option « refuser » : les demandes à bord sont refusées
            for id_demande in tournee["demandes"]:
                decision.refuser(etat["demandes"][id_demande], "REF-RENTABILITE", maintenant, journal)
            tournee["statut"] = "annulee"
            etat["flotte"][tournee["camion"]]["tournee"] = None


def preparer(arguments: argparse.Namespace, debut: datetime.datetime) -> tuple:
    """Charge le référentiel, la flotte et les paramètres de politique.

    :param arguments: Options de la ligne de commande.
    :param debut: Heure réelle (ou simulée) de démarrage.
    :return: ``(etat, ref, parametres)``.
    """
    if arguments.bdd:
        ref = referentiels.charger_depuis_bdd()
    else:
        chemin = arguments.referentiels
        if chemin is None:
            chemin = referentiels.FICHIER_REFERENTIELS
            if not os.path.exists(chemin):
                chemin = os.path.join(DOSSIER, "_build", "referentiels_exemple.json")
                referentiels.sauver_json(referentiels.generer_exemple(), chemin)
        ref = referentiels.charger_depuis_json(chemin)
    fiches = arguments.fiches or (module_flotte.REPERTOIRE_FICHES_VM if os.path.isdir(module_flotte.REPERTOIRE_FICHES_VM) else REPERTOIRE_FICHES_LOCAL)
    usines = [referentiels.pk_lieu(ref, nom) for nom in hypotheses.USINES_NORTH]
    flotte = module_flotte.charger_flotte(fiches, ref, usines, debut)
    couts = hypotheses.charger_confidentiel()
    parametres = {"couts": couts, "seuil": arguments.seuil if arguments.seuil is not None else couts["seuil_rentabilite"],
                  "marge_h": arguments.marge if arguments.marge is not None else hypotheses.MARGE_DEPART_H}
    print("Référentiel : %s" % ref["source"])
    for remarque in ref.get("remarques", []):
        print("  ! %s" % remarque)
    print("Fiches camion : %s\nFlotte NORTH : %d camions\n%s" % (os.path.abspath(fiches), len(flotte), module_flotte.resume_flotte(flotte, ref)))
    print("Coûts : %s ; seuil de départ θ = %.2f ; marge W = %.1f h projet"
          % ("Annexe 2" if couts["source"] != "defaut" else "taux par défaut", parametres["seuil"], parametres["marge_h"]))
    etat = {"flotte": flotte, "tournees": {}, "demandes": {}, "compteur": 0}
    return etat, ref, parametres


def rejouer(arguments: argparse.Namespace) -> dict:
    """Rejoue un fichier de demandes avec une horloge simulée.

    L'horloge démarre à la première réception et avance d'un tick à la fois ;
    le rejeu continue après la dernière demande jusqu'au départ de toutes les
    tournées ouvertes.

    :param arguments: Options de la ligne de commande (``rejeu``…).
    :return: Indicateurs (:func:`tracabilite.calculer_kpi`).
    """
    demandes = lire_rejeu(arguments.rejeu)
    horloge = demandes[0][0]
    etat, ref, parametres = preparer(arguments, horloge)
    journal = tracabilite.ouvrir_journal(arguments.sortie, horloge, "_rejeu")
    pas = datetime.timedelta(seconds=hypotheses.TICK_S)
    indice = 0
    while indice < len(demandes) or any(t["statut"] == "ouverte" for t in etat["tournees"].values()):
        horloge += pas
        nouvelles = []
        while indice < len(demandes) and demandes[indice][0] <= horloge:
            nouvelles.append(demandes[indice])
            indice += 1
        executer_tick(etat, ref, nouvelles, horloge, parametres, journal)
    kpi = tracabilite.calculer_kpi(etat)
    print("\nRejeu de %s (%d demandes, fin %s)" % (os.path.basename(arguments.rejeu), len(demandes), horloge.strftime("%H:%M:%S")))
    print(tracabilite.resume_kpi(kpi))
    print("Journal : %s (%d événements)" % (os.path.abspath(journal["chemin"]), journal["nb"]))
    return kpi


def en_direct(arguments: argparse.Namespace) -> None:
    """Fait tourner le moteur en direct sur le CSV du collecteur, jusqu'à ``Ctrl+C``.

    :param arguments: Options de la ligne de commande.
    """
    debut = datetime.datetime.now()
    etat, ref, parametres = preparer(arguments, debut)
    journal = tracabilite.ouvrir_journal(arguments.sortie, debut)
    lecteur = {"repertoire": arguments.demandes}
    dernier_rafraichissement = debut
    print("Moteur en direct sur %s ; 'Ctrl+C' pour arrêter" % os.path.abspath(arguments.demandes))
    try:
        while True:
            maintenant = datetime.datetime.now()
            # TRAVEL_TIME_MATRIX et PORTS_AVAILABILITY changent toutes les heures projet (1 min réelle)
            if arguments.bdd and maintenant - dernier_rafraichissement >= datetime.timedelta(minutes=1):
                ref = referentiels.charger_depuis_bdd()
                dernier_rafraichissement = maintenant
            nouvelles = lire_nouvelles_lignes(lecteur, maintenant)
            executer_tick(etat, ref, nouvelles, maintenant, parametres, journal)
            if nouvelles:
                kpi = tracabilite.calculer_kpi(etat)
                print("[%s] %d nouvelle(s) ; TA %.1f %%, TR %.1f %%, tournées ouvertes %d"
                      % (maintenant.strftime("%H:%M:%S"), len(nouvelles), 100 * kpi["ta"], 100 * kpi["tr"],
                         sum(1 for t in etat["tournees"].values() if t["statut"] == "ouverte")), flush=True)
            time.sleep(hypotheses.TICK_S)
    except KeyboardInterrupt:
        print("\nArrêt du moteur.\n" + tracabilite.resume_kpi(tracabilite.calculer_kpi(etat)))


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Moteur d'acceptation des demandes de transport NORTH")
    mode = parseur.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rejeu", help="fichier .jsonl ou .csv du collecteur à rejouer")
    mode.add_argument("--direct", action="store_true", help="lit en continu le CSV du jour du collecteur")
    parseur.add_argument("--bdd", action="store_true", help="lit le référentiel dans la base (VM)")
    parseur.add_argument("--referentiels", default=None, help="référentiel JSON (défaut : ../donnees/referentiels.json, sinon exemple)")
    parseur.add_argument("--fiches", default=None, help="dossier des fiches camion")
    parseur.add_argument("--demandes", default=REPERTOIRE_DEMANDES, help="dossier des CSV du collecteur (mode direct)")
    parseur.add_argument("--sortie", default=REPERTOIRE_SORTIE, help="dossier des journaux")
    parseur.add_argument("--seuil", type=float, default=None, help="seuil de départ θ (part du plancher)")
    parseur.add_argument("--marge", type=float, default=None, help="marge W avant le départ au plus tard (h projet)")
    options = parseur.parse_args()
    if options.rejeu:
        rejouer(options)
    else:
        en_direct(options)
