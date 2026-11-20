"""Moteur d'acceptation NORTH : boucle principale, en direct ou en rejeu.

À chaque tick (:data:`hypotheses.TICK_S` secondes réelles), le moteur :

1. rafraîchit le référentiel (mode ``--bdd``, toutes les minutes réelles) ;
2. lit les nouvelles demandes ;
3. décide chaque demande (table D-ACC, :mod:`decision`) ;
4. décide le départ de chaque tournée ouverte (table D-DEPART, :mod:`planification`) ;
5. écrit les événements dans le journal de traçabilité ;
6. avec ``--archiver`` (mode direct), met à jour les tables ``*_NORTH`` de la base (:mod:`archivage`) ;
7. avec ``--controle`` (mode direct), remplit les tables de traçabilité du Contrôle
   ``TRACABILITE_COMMANDE`` et ``TRACABILITE_TRAJET`` (:mod:`tracabilite_controle`).

Deux modes :

- **direct** (VM) : lit au fil de l'eau le CSV du jour écrit par
  ``collecteur.py`` ; l'horloge est l'heure réelle ;
- **rejeu** : relit un fichier ``.jsonl`` ou ``.csv`` du collecteur avec une
  horloge simulée, pour tester et régler les paramètres hors du VLAN.

Utilisation :

.. code-block:: bash

    python3 moteur.py --rejeu "../../6 - Data/Briefcase/demandes_2026-10-19.jsonl"   # local
    python3 moteur.py --direct --bdd --archiver --controle                              # VM
    python3 moteur.py --audit                                                           # VM, audit (production)

Le mode **audit** (règle R-ROB-05) est le mode de production : direct, base,
archivage, tables du Contrôle, fiches camion de la VM et Annexe 2 obligatoires. Il refuse de
démarrer plutôt que de partir sur une copie JSON, un référentiel d'exemple,
les fiches locales ou des coûts fictifs.
"""

import argparse
import csv
import datetime
import io
import json
import os
import time

import archivage
import decision
import flotte as module_flotte
import hypotheses
import planification
import referentiels
import regles
import robustesse
import tracabilite
import tracabilite_controle
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


def lire_suite(lecteur: dict, maintenant: datetime.datetime) -> list:
    """Lit les lignes complètes ajoutées au fichier courant du lecteur depuis la lecture précédente.

    Applique la règle R-ROB-01 : une ligne illisible (heure de réception ou
    identifiant absent ou mal formé) est ignorée et signalée, sans arrêter le
    moteur ; le collecteur garde de toute façon le message brut (R-COL-01).

    :param lecteur: ``{"chemin", "position", "entete", "messages"}``, modifié sur place.
    :param maintenant: Heure réelle (pour les messages d'erreur).
    :return: Liste de ``(h_reception, id_demande, champs)``.
    """
    chemin = lecteur.get("chemin")
    if chemin is None or not os.path.exists(chemin):
        return []
    with open(chemin, "rb") as fichier:
        fichier.seek(lecteur["position"])
        contenu = fichier.read()
    # On ne garde que les lignes complètes ; la dernière peut être en cours d'écriture
    fin = contenu.rfind(b"\n") + 1
    lecteur["position"] += fin
    texte = contenu[:fin].decode("utf-8", errors="replace")
    nouvelles = []
    # io.StringIO(newline="") : le module csv relit correctement un champ entre guillemets qui contient un saut de ligne
    for ligne in csv.reader(io.StringIO(texte, newline=""), delimiter=";"):
        if not ligne:
            continue
        if lecteur["entete"] is None:
            lecteur["entete"] = ligne
            continue
        champs = dict(zip(lecteur["entete"], ligne))
        try:
            h_reception = datetime.datetime.strptime(champs["H_RECEPTION"], "%Y-%m-%d %H:%M:%S")
            if not champs["ID_DEMANDE"]:
                raise ValueError("ID_DEMANDE vide")
        except (KeyError, ValueError) as erreur:
            lecteur["ignorees"] = lecteur.get("ignorees", 0) + 1
            robustesse.signaler(lecteur.setdefault("messages", {}), "ligne", maintenant,
                                "ligne illisible ignorée dans %s (%s)" % (os.path.basename(chemin), erreur))
            continue
        nouvelles.append((h_reception, champs["ID_DEMANDE"], champs))
    return nouvelles


def lire_nouvelles_lignes(lecteur: dict, maintenant: datetime.datetime) -> list:
    """Lit les demandes ajoutées au CSV du jour depuis la lecture précédente.

    Change de fichier quand le jour réel change (le collecteur fait de même),
    après avoir lu la fin du fichier de la veille : une demande écrite juste
    avant minuit n'est pas perdue.

    :param lecteur: ``{"repertoire", "chemin", "position", "entete", "messages"}``, modifié sur place.
    :param maintenant: Heure réelle.
    :return: Liste de ``(h_reception, id_demande, champs)``.
    """
    chemin = os.path.join(lecteur["repertoire"], "demandes_%s.csv" % maintenant.strftime("%Y-%m-%d"))
    nouvelles = []
    if chemin != lecteur.get("chemin"):
        nouvelles += lire_suite(lecteur, maintenant)
        lecteur.update({"chemin": chemin, "position": 0, "entete": None})
    return nouvelles + lire_suite(lecteur, maintenant)


def decider_demande(id_demande: str, champs: dict, h_reception: datetime.datetime, etat: dict, ref: dict,
                    maintenant: datetime.datetime, parametres: dict, journal: dict) -> None:
    """Décide une demande (table D-ACC) sans que son erreur arrête le moteur.

    Applique la règle R-ROB-01 : si la préparation ou la décision lève une
    exception, la demande est refusée avec le motif ``REF-DONNEES`` (ou
    ignorée si elle n'est pas dans le périmètre NORTH) et l'erreur est
    signalée ; les demandes suivantes sont traitées normalement. Une demande
    déjà acceptée avant l'erreur reste acceptée.

    :param id_demande: Identifiant de la demande.
    :param champs: Champs envoyés par l'ERP.
    :param h_reception: Heure de réception (réelle).
    :param etat: État du moteur, modifié sur place.
    :param ref: Référentiel.
    :param maintenant: Heure réelle (ou simulée en rejeu).
    :param parametres: ``couts``, ``seuil``, ``marge_h``.
    :param journal: Journal de traçabilité.
    """
    try:
        demande = regles.preparer_demande(id_demande, champs, h_reception, ref)
        decision.traiter_demande(demande, etat, ref, maintenant, parametres, journal)
        return
    except Exception as erreur:  # R-ROB-01 : une demande en erreur ne doit pas arrêter le moteur
        robustesse.signaler(etat.setdefault("messages", {}), "demande", maintenant,
                            "demande %s en erreur (%r) : refusée REF-DONNEES" % (id_demande, erreur))
    demande = etat["demandes"].get(id_demande)
    if demande is None:
        demande = dict(champs)
        demande.update({"id": id_demande, "h_reception": h_reception, "etat": "RECUE", "motif": ""})
        etat["demandes"][id_demande] = demande
    if demande.get("etat") not in ("RECUE", None):
        return
    try:
        north = regles.est_demande_north(demande)
    except Exception:  # périmètre indécidable : on la compte comme refusée, c'est le choix prudent pour le TA
        north = True
    if not north:
        demande["etat"] = "IGNOREE"
        return
    try:
        decision.refuser(demande, "REF-DONNEES", maintenant, journal)
    except Exception:  # journal inaccessible : l'état reste juste, l'événement manque
        demande.update({"etat": "REFUSEE", "motif": "REF-DONNEES"})


def decider_tournee(tournee: dict, etat: dict, ref: dict, maintenant: datetime.datetime,
                    parametres: dict, journal: dict) -> None:
    """Décide le départ d'une tournée ouverte (table D-DEPART) sans que son erreur arrête le moteur.

    Si D-DEPART répond « refuser » (ligne 3), les demandes à bord sont
    refusées avec le motif ``REF-RENTABILITE`` (R-RENT-01). Applique la règle
    R-ROB-01 : une exception est signalée et la tournée reste ouverte ; elle
    est réexaminée au tick suivant.

    :param tournee: Tournée ouverte.
    :param etat: État du moteur, modifié sur place.
    :param ref: Référentiel.
    :param maintenant: Heure réelle (ou simulée en rejeu).
    :param parametres: ``couts``, ``seuil``, ``marge_h``.
    :param journal: Journal de traçabilité.
    """
    try:
        choix = planification.decider_depart(tournee, etat, ref, maintenant, parametres)
        if choix == "partir":
            planification.faire_partir(tournee, etat, ref, maintenant, parametres["couts"], journal)
        elif choix == "refuser":
            # D-DEPART ligne 3, option « refuser » : les demandes à bord sont refusées
            for id_demande in tournee["demandes"]:
                decision.refuser(etat["demandes"][id_demande], "REF-RENTABILITE", maintenant, journal)
            tournee["statut"] = "annulee"
            etat["flotte"][tournee["camion"]]["tournee"] = None
    except Exception as erreur:  # R-ROB-01 : la tournée sera réexaminée au tick suivant
        robustesse.signaler(etat.setdefault("messages", {}), "tournee", maintenant,
                            "tournée %s en erreur (%r) : nouvel essai au prochain tick" % (tournee.get("id"), erreur))


def executer_tick(etat: dict, ref: dict, nouvelles: list, maintenant: datetime.datetime,
                  parametres: dict, journal: dict) -> None:
    """Exécute un tick du moteur : décisions sur les demandes, puis départs.

    Enchaîne la table D-ACC (:func:`decider_demande`) pour chaque nouvelle
    demande, puis la table D-DEPART (:func:`decider_tournee`) pour chaque
    tournée ouverte. Chaque demande et chaque tournée est protégée : une
    erreur sur l'une n'empêche pas de traiter les autres (R-ROB-01).

    :param etat: État du moteur, modifié sur place.
    :param ref: Référentiel.
    :param nouvelles: Demandes reçues depuis le tick précédent, ``(h_reception, id, champs)``.
    :param maintenant: Heure réelle (ou simulée en rejeu).
    :param parametres: ``couts``, ``seuil`` (θ, taux entre 0 et 1), ``marge_h`` (W, heures projet).
    :param journal: Journal de traçabilité.
    """
    for h_reception, id_demande, champs in nouvelles:
        # Déjà vue, ou déjà décidée par un lancement précédent (archivée en base)
        if id_demande in etat["demandes"] or id_demande in etat.get("deja_archivees", ()):
            continue
        decider_demande(id_demande, champs, h_reception, etat, ref, maintenant, parametres, journal)

    for tournee in [t for t in etat["tournees"].values() if t["statut"] == "ouverte"]:
        decider_tournee(tournee, etat, ref, maintenant, parametres, journal)


def rafraichir_referentiel(ref: dict, messages: dict, maintenant: datetime.datetime) -> dict:
    """Relit le référentiel dans la base, ou garde le précédent si elle ne répond pas.

    Applique la règle R-ROB-02 : une base injoignable ou lente (délais bornés
    par :data:`referentiels.BDD`) n'arrête pas le moteur ; il garde le dernier
    référentiel lu et réessaie à la minute suivante.

    :param ref: Référentiel courant.
    :param messages: Messages d'erreur déjà affichés (:func:`robustesse.signaler`).
    :param maintenant: Heure réelle.
    :return: Nouveau référentiel, ou ``ref`` en cas d'erreur.
    """
    try:
        return referentiels.charger_depuis_bdd()
    except Exception as erreur:  # R-ROB-02 : base en panne, on garde le référentiel précédent
        robustesse.signaler(messages, "referentiel", maintenant, "base injoignable (%s) : référentiel lu à %s gardé"
                            % (str(erreur).strip() or type(erreur).__name__, ref.get("lu_a", "?")))
        return ref


def charger_bdd_ou_copie(copie: str = None) -> dict:
    """Charge le référentiel depuis la base, ou depuis sa copie JSON si la base ne répond pas au démarrage.

    Applique la règle R-ROB-02 : ``nord.sh demarrer`` écrit la copie
    ``../donnees/referentiels.json`` à chaque lancement réussi ; le moteur
    repart dessus et revient à la base au premier rafraîchissement réussi.

    :param copie: Copie JSON (défaut : :data:`referentiels.FICHIER_REFERENTIELS`).
    :return: Référentiel.
    :raises Exception: L'erreur de la base, s'il n'y a pas de copie.
    """
    copie = copie or referentiels.FICHIER_REFERENTIELS
    try:
        return referentiels.charger_depuis_bdd()
    except Exception as erreur:
        if not os.path.exists(copie):
            raise
        print("! base injoignable au démarrage (%s) : copie %s, remplacée dès que la base répond"
              % (str(erreur).strip() or type(erreur).__name__, os.path.abspath(copie)), flush=True)
        return referentiels.charger_depuis_json(copie)


class ErreurAudit(Exception):
    """Une condition du mode audit (R-ROB-05) n'est pas remplie : le moteur ne démarre pas."""


def verifier_conditions_audit(fiches: str, couts: dict) -> None:
    """Vérifie les conditions de démarrage du mode audit, hors base (lue juste avant).

    Applique la règle R-ROB-05 : en production, les fiches camion sont celles
    de la VM et les coûts ceux de l'Annexe 2 (le prix ``COUT_PREST`` n'est
    écrit qu'avec eux).

    :param fiches: Dossier des fiches camion retenu.
    :param couts: Coûts lus par :func:`hypotheses.charger_confidentiel`.
    :raises ErreurAudit: Si une condition manque.
    """
    if os.path.realpath(fiches) != os.path.realpath(module_flotte.REPERTOIRE_FICHES_VM) or not os.path.isdir(fiches):
        raise ErreurAudit("mode audit : fiches camion de la VM introuvables (%s)" % module_flotte.REPERTOIRE_FICHES_VM)
    if couts["source"] == "defaut":
        raise ErreurAudit("mode audit : %s absent (coûts et prix de l'Annexe 2 obligatoires)" % hypotheses.FICHIER_CONFIDENTIEL)


def preparer(arguments: argparse.Namespace, debut: datetime.datetime) -> tuple:
    """Charge le référentiel, la flotte et les paramètres de politique.

    :param arguments: Options de la ligne de commande.
    :param debut: Heure réelle (ou simulée) de démarrage.
    :return: ``(etat, ref, parametres)``.
    """
    audit = getattr(arguments, "audit", False)
    if audit:
        # R-ROB-05 : la base, sans copie de secours ; si elle ne répond pas, nord.sh réessaie toutes les 30 s
        try:
            ref = referentiels.charger_depuis_bdd()
        except Exception as erreur:
            raise ErreurAudit("mode audit : base injoignable au démarrage (%s)" % (str(erreur).strip() or type(erreur).__name__))
    elif arguments.bdd:
        ref = charger_bdd_ou_copie(arguments.referentiels)
    else:
        chemin = arguments.referentiels
        if chemin is None:
            chemin = referentiels.FICHIER_REFERENTIELS
            if not os.path.exists(chemin):
                chemin = os.path.join(DOSSIER, "_build", "referentiels_exemple.json")
                referentiels.sauver_json(referentiels.generer_exemple(), chemin)
        ref = referentiels.charger_depuis_json(chemin)
    if audit:
        fiches = module_flotte.REPERTOIRE_FICHES_VM
    else:
        fiches = arguments.fiches or (module_flotte.REPERTOIRE_FICHES_VM if os.path.isdir(module_flotte.REPERTOIRE_FICHES_VM) else REPERTOIRE_FICHES_LOCAL)
    couts = hypotheses.charger_confidentiel()
    if audit:
        verifier_conditions_audit(fiches, couts)
    usines = [referentiels.pk_lieu(ref, nom) for nom in hypotheses.USINES_NORTH]
    flotte = module_flotte.charger_flotte(fiches, ref, usines, debut)
    if audit and not flotte:
        raise ErreurAudit("mode audit : aucun camion NORTH exploitable dans %s" % fiches)
    parametres = {"couts": couts, "seuil": arguments.seuil if arguments.seuil is not None else hypotheses.SEUIL_DEPART_DEFAUT,
                  "marge_h": arguments.marge if arguments.marge is not None else hypotheses.MARGE_DEPART_H}
    if audit:
        print("== MODE AUDIT (production) : base, archivage, fiches de la VM, Annexe 2 ==")
    print("Référentiel : %s" % ref["source"])
    for remarque in ref.get("remarques", []):
        print("  ! %s" % remarque)
    print("Fiches camion : %s\nFlotte NORTH : %d camions\n%s" % (os.path.abspath(fiches), len(flotte), module_flotte.resume_flotte(flotte, ref)))
    print("Coûts : %s ; seuil de départ θ = %.2f ; marge W = %.1f h projet"
          % ("Annexe 2" if couts["source"] != "defaut" else "taux par défaut", parametres["seuil"], parametres["marge_h"]))
    # Le préfixe des tournées (heure de démarrage) les rend uniques d'un lancement à l'autre (TRAJET_NORTH)
    etat = {"flotte": flotte, "tournees": {}, "demandes": {}, "compteur": 0,
            "prefixe": debut.strftime("T%y%m%d%H%M%S-"), "deja_archivees": set(), "messages": {}}
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
    if not demandes:
        raise SystemExit("Aucune demande lisible dans %s" % arguments.rejeu)
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
    """Fait tourner le moteur en direct sur le CSV du collecteur, jusqu'à ``Ctrl+C`` ou SIGTERM.

    Applique les règles R-ROB-01 à R-ROB-03 : une erreur pendant un tick est
    signalée et le moteur passe au tick suivant ; la base est réessayée une
    fois par minute ; à l'arrêt, la dernière synchronisation des tables
    ``*_NORTH`` est faite. Un tick plus long que :data:`hypotheses.TICK_S`
    est signalé (le moteur prend du retard sur l'ERP).

    :param arguments: Options de la ligne de commande.
    """
    debut = datetime.datetime.now()
    etat, ref, parametres = preparer(arguments, debut)
    messages = etat["messages"]
    journal = tracabilite.ouvrir_journal(arguments.sortie, debut)
    lecteur = {"repertoire": arguments.demandes, "messages": messages}
    dernier_rafraichissement = debut
    archive = archivage.ouvrir() if arguments.archiver else None
    if archive is not None:
        # Avant la première lecture : reprise après arrêt (commandes déjà archivées) et flotte en base
        archivage.synchroniser(archive, etat, ref, parametres["couts"], debut)
        print("Archivage dans CAMION_NORTH, TRAJET_NORTH, ARCHIVAGE_COMMANDE_NORTH : %d ligne(s) écrite(s)" % archive["ecrites"])
    controle = tracabilite_controle.ouvrir() if getattr(arguments, "controle", False) else None
    if controle is not None:
        # Avant la première lecture : nos commandes déjà dans les tables du Contrôle sont protégées (R-CTL-01)
        tracabilite_controle.synchroniser(controle, etat, ref, parametres["couts"], debut)
        print("Traçabilité Contrôle (TRACABILITE_COMMANDE, TRACABILITE_TRAJET, ID_ENTRE %s) active"
              % hypotheses.ID_ENTREPRISE, flush=True)
    print("Moteur en direct sur %s ; 'Ctrl+C' pour arrêter" % os.path.abspath(arguments.demandes), flush=True)
    try:
        while True:
            debut_tick = time.monotonic()
            maintenant = datetime.datetime.now()
            try:
                # TRAVEL_TIME_MATRIX et PORTS_AVAILABILITY changent toutes les heures projet (1 min réelle)
                if arguments.bdd and maintenant - dernier_rafraichissement >= datetime.timedelta(minutes=1):
                    dernier_rafraichissement = maintenant  # même en échec : un seul essai par minute
                    ref = rafraichir_referentiel(ref, messages, maintenant)
                nouvelles = lire_nouvelles_lignes(lecteur, maintenant)
                executer_tick(etat, ref, nouvelles, maintenant, parametres, journal)
                if archive is not None:
                    archivage.synchroniser(archive, etat, ref, parametres["couts"], maintenant)
                if controle is not None:
                    tracabilite_controle.synchroniser(controle, etat, ref, parametres["couts"], maintenant)
                if nouvelles:
                    kpi = tracabilite.calculer_kpi(etat)
                    print("[%s] %d nouvelle(s) ; TA %.1f %%, TR %.1f %%, tournées ouvertes %d"
                          % (maintenant.strftime("%H:%M:%S"), len(nouvelles), 100 * kpi["ta"], 100 * kpi["tr"],
                             sum(1 for t in etat["tournees"].values() if t["statut"] == "ouverte")), flush=True)
            except Exception as erreur:  # R-ROB-01 : dernier filet, le moteur passe au tick suivant
                robustesse.signaler(messages, "tick", maintenant, "tick en erreur (%r) : le moteur continue" % erreur)
            duree = time.monotonic() - debut_tick
            if duree > hypotheses.TICK_S:
                robustesse.signaler(messages, "lenteur", maintenant, "tick de %.1f s (> %d s) : le moteur prend du retard"
                                    % (duree, hypotheses.TICK_S))
            time.sleep(max(0.0, hypotheses.TICK_S - duree))
    except KeyboardInterrupt:
        print("\nArrêt du moteur (%d incident(s) signalé(s), %d ligne(s) illisible(s) ignorée(s)).\n%s"
              % (messages.get("nb", 0), lecteur.get("ignorees", 0), tracabilite.resume_kpi(tracabilite.calculer_kpi(etat))))
    finally:
        if archive is not None:
            archivage.synchroniser(archive, etat, ref, parametres["couts"], datetime.datetime.now())
            archivage.fermer(archive)
            print("Archivage : %d ligne(s) écrite(s), %d demande(s) non archivable(s) (données invalides, au journal seulement)"
                  % (archive["ecrites"], len(archive["non_archivables"])), flush=True)
        if controle is not None:
            tracabilite_controle.synchroniser(controle, etat, ref, parametres["couts"], datetime.datetime.now())
            tracabilite_controle.fermer(controle)
            print("Traçabilité Contrôle : %d ligne(s) écrite(s), %d commande(s) non écrivable(s) (quantité ou H_DEP illisible)"
                  % (controle["ecrites"], len(controle["non_ecrivables"])), flush=True)


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Moteur d'acceptation des demandes de transport NORTH")
    mode = parseur.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rejeu", help="fichier .jsonl ou .csv du collecteur à rejouer")
    mode.add_argument("--direct", action="store_true", help="lit en continu le CSV du jour du collecteur")
    mode.add_argument("--audit", action="store_true",
                      help="production (audit) : --direct --bdd --archiver --controle, sans copie de secours ni fiches locales ; "
                           "Annexe 2 et fiches de la VM obligatoires")
    parseur.add_argument("--bdd", action="store_true", help="lit le référentiel dans la base (VM)")
    parseur.add_argument("--archiver", action="store_true",
                         help="mode direct : écrit CAMION_NORTH, TRAJET_NORTH et ARCHIVAGE_COMMANDE_NORTH dans la base (VM)")
    parseur.add_argument("--controle", action="store_true",
                         help="mode direct : remplit TRACABILITE_COMMANDE et TRACABILITE_TRAJET (tables du Contrôle, VM)")
    parseur.add_argument("--referentiels", default=None, help="référentiel JSON (défaut : ../donnees/referentiels.json, sinon exemple) ; "
                              "avec --bdd, copie de secours si la base ne répond pas au démarrage")
    parseur.add_argument("--fiches", default=None, help="dossier des fiches camion")
    parseur.add_argument("--demandes", default=REPERTOIRE_DEMANDES, help="dossier des CSV du collecteur (mode direct)")
    parseur.add_argument("--sortie", default=REPERTOIRE_SORTIE, help="dossier des journaux")
    parseur.add_argument("--seuil", type=float, default=None, help="seuil de départ θ (taux de remplissage, 0-1)")
    parseur.add_argument("--marge", type=float, default=None, help="marge W avant le départ au plus tard (h projet)")
    options = parseur.parse_args()
    robustesse.arreter_sur_sigterm()  # R-ROB-03 : « nord.sh arreter » envoie SIGTERM
    if options.audit:
        # R-ROB-05 : le mode audit impose la base et l'archivage, et ignore les sources de remplacement
        if options.referentiels or options.fiches:
            parseur.error("--audit lit la base et les fiches de la VM : --referentiels et --fiches sont interdits")
        options.direct = options.bdd = options.archiver = options.controle = True
    if (options.archiver or options.controle) and options.rejeu:
        # Un rejeu réécrirait les lignes du moteur en direct (mêmes identifiants de demandes)
        parseur.error("--archiver et --controle ne s'utilisent qu'avec --direct")
    try:
        if options.rejeu:
            rejouer(options)
        else:
            en_direct(options)
    except ErreurAudit as erreur:
        raise SystemExit("! %s : le moteur ne démarre pas" % erreur)
