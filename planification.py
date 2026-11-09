"""Planification des tournées NORTH : calcul d'un plan, coût, gamme et départ.

Une tournée est un dictionnaire : ``id``, ``camion`` (IMMAT), ``nature``
(``"frais"`` ou ``"sec"``), ``demandes`` (identifiants), ``statut``
(``"ouverte"``, ``"partie"``, ``"annulee"``) et ``plan`` (dernier plan calculé).

Modèle de tournée (hypothèse de travail) : le camion charge d'abord toutes
les demandes, dans l'ordre des ``H_DEP``, puis livre en allant chaque fois au
lieu de livraison le plus proche. Il ne rentre à son usine d'attache que si
:data:`hypotheses.RETOUR_A_L_USINE` est vrai (hypothèse C10, faux depuis le
2026-10-21).

Heure limite (R-DEL-01) : pour un export, le camion quitte ``LOC_DEP`` chargé
au plus tard à ``H_DEP`` ; pour un import depuis un port, il quitte l'usine à
vide vers le port au plus tard à ``H_DEP``.

Hypothèses (non tirées du sujet) :

- **H-PLA-01** : un camion réutilisé après une tournée part de là où il a fini ;
  pour un import, c'est ce départ-là qui est comparé à ``H_DEP``.

Unités : durées en heures projet ; dates en heure réelle (``datetime``).
1 heure projet = 1 minute réelle.
"""

import datetime
import math

import hypotheses
import referentiels
import regles
import tracabilite


def vers_delta(heures: float) -> datetime.timedelta:
    """Convertit une durée en heures projet en durée réelle.

    :param heures: Durée en heures projet.
    :return: Durée réelle (1 heure projet = 1 minute réelle).
    """
    return datetime.timedelta(minutes=heures)


def vers_heures(delta: datetime.timedelta) -> float:
    """Convertit une durée réelle en heures projet.

    :param delta: Durée réelle.
    :return: Durée en heures projet.
    """
    return delta.total_seconds() / 60


def conduire(heures: float, conduite: dict, regles_conducteur: bool = True) -> float:
    """Durée réelle d'un trajet, pauses et repos réglementaires compris.

    Applique la règle R-CON-01 (Annexe 1) : 45 min de pause après 4 h 30 de
    conduite continue, 11 h de repos après 12 h de conduite dans la journée.
    ``conduite`` (clés ``jour`` et ``continue``, en heures projet) est mis à
    jour sur place.

    :param heures: Temps de conduite pur, en heures projet.
    :param conduite: État du conducteur, modifié sur place.
    :param regles_conducteur: ``False`` ignore les pauses (sert à attribuer le motif REF-CONDUCTEUR).
    :return: Durée écoulée, en heures projet.
    """
    ecoule = 0.0
    while heures > 1e-9:
        avant_arret = min(hypotheses.CONDUITE_CONTINUE_MAX_H - conduite["continue"],
                          hypotheses.CONDUITE_JOUR_MAX_H - conduite["jour"])
        if regles_conducteur and avant_arret <= 1e-9:
            if conduite["jour"] >= hypotheses.CONDUITE_JOUR_MAX_H - 1e-9:
                ecoule += hypotheses.REPOS_QUOTIDIEN_H
                conduite["jour"] = 0.0
            else:
                ecoule += hypotheses.PAUSE_H
            conduite["continue"] = 0.0
            continue
        pas = heures if not regles_conducteur else min(heures, avant_arret)
        ecoule += pas
        heures -= pas
        conduite["jour"] += pas
        conduite["continue"] += pas
    return ecoule


def vitesse_kmh(heures: float) -> float:
    """Vitesse moyenne d'un trajet selon sa durée (Annexe 2, partie publique).

    Interpolation linéaire entre 5 h et 7 h, où le sujet ne donne rien.

    :param heures: Durée de conduite du trajet, en heures projet.
    :return: Vitesse en km/h.
    """
    court, long_ = hypotheses.VITESSE_TRAJET_COURT_KMH, hypotheses.VITESSE_TRAJET_LONG_KMH
    if heures <= 5:
        return court
    if heures >= 7:
        return long_
    return court + (long_ - court) * (heures - 5) / 2


def grouper(demandes: list, cle_lieu: str, cle_type: str) -> dict:
    """Regroupe des demandes par arrêt (lieu et type de lieu).

    Amsterdam est à la fois usine et port : l'arrêt est le couple
    ``(PK_LOCATIONS, USINE/PORT/GMS)``.

    :param demandes: Demandes préparées.
    :param cle_lieu: ``"pk_dep"`` ou ``"pk_arr"``.
    :param cle_type: ``"TYPE_DEP"`` ou ``"TYPE_ARR"``.
    :return: ``{(lieu, type): [demandes]}``.
    """
    groupes = {}
    for demande in demandes:
        groupes.setdefault((demande[cle_lieu], demande[cle_type]), []).append(demande)
    return groupes


def ordonner_arrets(ref: dict, demandes: list) -> list:
    """Ordonne les arrêts d'une tournée : chargements puis livraisons.

    Chargements dans l'ordre de la plus proche ``H_DEP`` ; livraisons au plus
    proche voisin depuis le dernier chargement.

    :param ref: Référentiel.
    :param demandes: Demandes préparées de la tournée.
    :return: Liste de ``(sens, lieu, type, demandes)`` avec ``sens`` = ``"C"`` (chargement) ou ``"L"`` (livraison).
    """
    chargements = sorted(grouper(demandes, "pk_dep", "TYPE_DEP").items(),
                         key=lambda element: min(d["h_dep"] for d in element[1]))
    arrets = [("C", lieu, type_, groupe) for (lieu, type_), groupe in chargements]
    restants = grouper(demandes, "pk_arr", "TYPE_ARR")
    position = arrets[-1][1]
    while restants:
        # Plus proche voisin ; un trajet inconnu passe en dernier
        cle = min(restants, key=lambda c: referentiels.duree(ref, position, c[0]) if referentiels.duree(ref, position, c[0]) is not None else math.inf)
        arrets.append(("L", cle[0], cle[1], restants.pop(cle)))
        position = cle[0]
    return arrets


def attribuer_gamme(arrets_intermediaires: int, allongement: float):
    """Gamme d'une demande d'après le service rendu (``methode-contrat.md`` §3).

    Applique la règle R-GAM : la gamme la plus haute dont les deux limites
    sont respectées.

    :param arrets_intermediaires: Arrêts entre le chargement et la livraison de la demande.
    :type arrets_intermediaires: int
    :param allongement: Durée réelle / durée en trajet direct.
    :type allongement: float
    :return: ``"haute"``, ``"mediane"``, ``"basse"``, ou ``None`` si hors contrat.
    :rtype: str or None
    """
    for gamme, arrets_max, allongement_max in hypotheses.GAMMES:
        if arrets_intermediaires <= arrets_max and allongement <= allongement_max + 1e-9:
            return gamme
    return None


def declasser_gamme(gamme: str, spec_non_tenue: bool) -> str:
    """Abaisse la gamme d'une demande dont la SPEC n'est pas respectée.

    Applique la règle R-SPEC-02 selon :data:`hypotheses.DECLASSEMENT_SPEC`.

    :param gamme: Gamme due au trajet (``"haute"``, ``"mediane"`` ou ``"basse"``).
    :param spec_non_tenue: Le camion ne respecte pas toute la SPEC de la demande.
    :return: Gamme finale.
    """
    if not spec_non_tenue:
        return gamme
    if hypotheses.DECLASSEMENT_SPEC == "basse":
        return "basse"
    ordre = [nom for nom, _, _ in hypotheses.GAMMES]
    return ordre[min(ordre.index(gamme) + 1, len(ordre) - 1)]


def est_import_port(demande: dict) -> bool:
    """Dit si l'heure limite de la demande porte sur le départ à vide vers le port.

    Applique la règle R-DEL-01 (précision du 2026-10-21) : pour un import qui
    part d'un port, ``H_DEP`` est l'heure à laquelle le camion quitte l'usine
    (ou le lieu où il se trouve, H-PLA-01) pour aller au port, pas l'heure à
    laquelle il quitte le port chargé.

    :param demande: Demande préparée.
    :return: ``True`` pour un import depuis un port.
    """
    return demande["TYPE_DEP"] == "PORT"


def evaluer_plan(ref: dict, camion: dict, demandes: list, debut: datetime.datetime,
                 maintenant: datetime.datetime, couts: dict, regles_conducteur: bool = True) -> dict:
    """Calcule le plan d'une tournée et vérifie qu'il est réalisable.

    Applique les règles R-DEL-01 (à ``H_DEP`` au plus tard, un export quitte
    son lieu de départ chargé — trajet, chargement — et, pour un import, le
    camion quitte l'usine à vide vers le port, voir :func:`est_import_port`), R-CON-01
    (pauses et repos, voir :func:`conduire`), R-CAP-01 (plancher ≤ 1 sur
    chaque tronçon), R-GAM (une demande hors gamme basse est interdite) et
    R-SPEC-02 (une SPEC non tenue abaisse la gamme, :func:`declasser_gamme`).

    :param ref: Référentiel.
    :param camion: Camion qui fait la tournée.
    :param demandes: Demandes préparées de la tournée.
    :param debut: Heure réelle où le camion se met en route.
    :param maintenant: Heure réelle courante (pour les attentes portuaires prévues).
    :param couts: Taux horaires (:func:`hypotheses.charger_confidentiel`).
    :param regles_conducteur: ``False`` ignore les pauses (diagnostic du motif).
    :return: Plan : ``ok``, ``motif`` (``None``, ``"DELAI"``, ``"TRAJET"``, ``"CAPACITE"``, ``"GAMME"``),
        ``marge_h``, ``debut``, ``fin``, ``troncons``, ``suivi`` (par demande : ``depart`` du
        lieu de chargement, ``depart_controle`` comparé à ``H_DEP``…), ``cout``,
        ``remplissage``, ``lieu_fin``, ``conduite_jour``.
    """
    plan = {"ok": False, "motif": None, "marge_h": math.inf, "debut": debut, "fin": debut, "troncons": [],
            "suivi": {d["id"]: {} for d in demandes}, "cout": math.inf, "remplissage": 0.0,
            "lieu_fin": camion["position"], "conduite_jour": camion["conduite_jour"]}
    # Un camion arrêté au moins le temps du repos quotidien repart reposé
    repos = vers_heures(debut - camion["libre_a"]) >= hypotheses.REPOS_QUOTIDIEN_H
    conduite = {"jour": 0.0 if repos else camion["conduite_jour"], "continue": 0.0}

    # La flotte ne garde que le lieu du camion, pas son type : on le suppose à l'usine. Un camion
    # resté au port d'Amsterdam paie donc le trajet intra-ville vers l'usine (estimation prudente).
    t, lieu, type_lieu = debut, camion["position"], "USINE"
    a_bord, energie, arrets = [], 0.0, ordonner_arrets(ref, demandes)
    etapes = arrets + ([("R", camion["usine"], "USINE", [])] if hypotheses.RETOUR_A_L_USINE else [])
    for numero, (sens, cible, type_cible, groupe) in enumerate(etapes):
        # Instant où le camion se met en route vers cette étape (départ à vide vers le port pour un import)
        en_route = t
        # Trajet : nul si on reste sur place ; durée intra-ville (diagonale) entre usine et port d'une même ville
        if cible == lieu and type_cible == type_lieu:
            route = 0.0
        else:
            route = referentiels.duree(ref, lieu, cible)
            if route is None:
                plan["motif"] = "TRAJET"
                return plan
        if route > 0:
            ecoule = conduire(route, conduite, regles_conducteur)
            remplissage = regles.equivalent_plancher(a_bord, camion["PTAC"], ref) or 0.0
            plan["troncons"].append({"de": lieu, "a": cible, "debut": t, "fin": t + vers_delta(ecoule),
                                     "duree_h": route, "demandes": [d["id"] for d in a_bord], "remplissage": remplissage})
            conso = ref["consommation"].get(camion["PTAC"], {}).get(hypotheses.COLONNE_CONSOMMATION.get(camion["ENERGIE"], ""), 0) or 0
            energie += conso / 100 * route * vitesse_kmh(route) * hypotheses.PRIX_ENERGIE.get(camion["ENERGIE"], 0)
            t += vers_delta(ecoule)
        lieu, type_lieu = cible, type_cible
        if sens == "R":
            break
        if type_cible == "PORT":
            t += vers_delta(referentiels.attente_port_h(ref, cible, vers_heures(t - maintenant)))
        t += vers_delta(hypotheses.DUREE_CHARGEMENT_H)
        if sens == "C":
            a_bord += groupe
            for demande in groupe:
                # R-DEL-01 : l'instant comparé à H_DEP dépend du sens de la demande
                controle = en_route if est_import_port(demande) else t
                plan["suivi"][demande["id"]].update({"depart": t, "depart_controle": controle, "arret_c": numero})
                plan["marge_h"] = min(plan["marge_h"], vers_heures(demande["h_dep"] - controle))
        else:
            for demande in groupe:
                a_bord.remove(demande)
                plan["suivi"][demande["id"]].update({"arrivee": t, "arret_l": numero})

    plan.update({"fin": t, "lieu_fin": lieu, "conduite_jour": conduite["jour"],
                 "remplissage": max([tr["remplissage"] for tr in plan["troncons"]] + [regles.equivalent_plancher(demandes, camion["PTAC"], ref) or 0.0])})
    if regles.equivalent_plancher(demandes, camion["PTAC"], ref) is None or plan["remplissage"] > 1 + 1e-9:
        plan["motif"] = "CAPACITE"
        return plan
    if plan["marge_h"] < 0:
        plan["motif"] = "DELAI"
        return plan
    # Gamme de chaque demande : arrêts intermédiaires et allongement par rapport au trajet direct
    for demande in demandes:
        suivi = plan["suivi"][demande["id"]]
        direct = conduire(referentiels.duree(ref, demande["pk_dep"], demande["pk_arr"]) or 0.0, {"jour": 0.0, "continue": 0.0}) \
            + 2 * hypotheses.DUREE_CHARGEMENT_H
        reel = vers_heures(suivi["arrivee"] - suivi["depart"]) + hypotheses.DUREE_CHARGEMENT_H
        suivi["arrets"] = suivi["arret_l"] - suivi["arret_c"] - 1
        suivi["allongement"] = reel / direct if direct > 0 else 1.0
        suivi["gamme"] = attribuer_gamme(suivi["arrets"], suivi["allongement"])
        if suivi["gamme"] is None:
            plan["motif"] = "GAMME"
            return plan
        suivi["spec_non_tenue"] = bool(regles.verifier_spec(demande, camion))
        suivi["gamme"] = declasser_gamme(suivi["gamme"], suivi["spec_non_tenue"])

    # Coût : heures totales × (conducteur + véhicule) + énergie ; pénalité du sec en frigo (R-FRA-02)
    plan["cout"] = vers_heures(t - debut) * (couts["taux_conducteur_h"] + couts["taux_vehicule_h"]) + energie
    if camion["frigo"] and demandes and demandes[0]["nature"] == "sec" and hypotheses.SEC_EN_FRIGO == "dernier_recours":
        plan["cout"] += hypotheses.PENALITE_SEC_EN_FRIGO
    plan["ok"] = True
    return plan


def evaluer_avec_motif(ref: dict, camion: dict, demandes: list, debut: datetime.datetime,
                       maintenant: datetime.datetime, couts: dict) -> dict:
    """Évalue un plan et précise le motif d'échec lié aux règles conducteur.

    Si le plan échoue sur le délai mais tiendrait sans pauses ni repos, le
    motif devient ``"CONDUCTEUR"`` (refus REF-CONDUCTEUR, règle R-CON-01).

    :param ref: Référentiel.
    :param camion: Camion.
    :param demandes: Demandes de la tournée.
    :param debut: Heure réelle de mise en route.
    :param maintenant: Heure réelle courante.
    :param couts: Taux horaires.
    :return: Plan (voir :func:`evaluer_plan`).
    """
    plan = evaluer_plan(ref, camion, demandes, debut, maintenant, couts)
    if not plan["ok"] and plan["motif"] == "DELAI":
        if evaluer_plan(ref, camion, demandes, debut, maintenant, couts, regles_conducteur=False)["ok"]:
            plan["motif"] = "CONDUCTEUR"
    return plan


def decider_depart(tournee: dict, etat: dict, ref: dict, maintenant: datetime.datetime, parametres: dict) -> str:
    """Décide si une tournée ouverte part maintenant.

    Implémente la table D-DEPART (première ligne qui correspond) :

    1. remplissage prévu ≥ seuil θ → ``"partir"`` ;
    2. sous le seuil, heure limite pas atteinte → ``"attendre"`` ;
    3. sous le seuil, heure limite atteinte → ``"partir"`` ou ``"refuser"``
       selon :data:`hypotheses.SOUS_SEUIL` (motif REF-RENTABILITE).

    L'heure limite est le départ au plus tard (marge du plan) moins la marge W.
    Le seuil vient de l'Annexe 2 (confidentielle) ou de :data:`hypotheses.SEUIL_DEPART_DEFAUT`.

    :param tournee: Tournée ouverte.
    :param etat: État du moteur (``flotte``, ``demandes``).
    :param ref: Référentiel.
    :param maintenant: Heure réelle courante.
    :param parametres: ``seuil``, ``marge_h``, ``couts``.
    :return: ``"partir"``, ``"attendre"`` ou ``"refuser"``.
    """
    camion = etat["flotte"][tournee["camion"]]
    demandes = [etat["demandes"][i] for i in tournee["demandes"]]
    plan = evaluer_plan(ref, camion, demandes, max(maintenant, camion["libre_a"]), maintenant, parametres["couts"])
    tournee["plan"] = plan
    if plan["ok"] and plan["remplissage"] >= parametres["seuil"]:
        return "partir"
    if plan["ok"] and plan["marge_h"] > parametres["marge_h"]:
        return "attendre"
    return "refuser" if hypotheses.SOUS_SEUIL == "refuser" else "partir"


def faire_partir(tournee: dict, etat: dict, ref: dict, maintenant: datetime.datetime, couts: dict, journal: dict) -> None:
    """Fait partir une tournée et écrit son départ et ses tronçons.

    Les demandes qui ne peuvent plus partir à temps (``H_DEP`` dépassée au
    départ contrôlé par R-DEL-01) sont retirées et passent « expirées » : elles comptent comme
    refusées dans le taux d'acceptation (règle R-DEL-01). Le camion est
    ensuite libre à la fin de la tournée, à son dernier lieu.

    :param tournee: Tournée à faire partir.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :param maintenant: Heure réelle courante.
    :param couts: Taux horaires.
    :param journal: Journal de traçabilité.
    """
    camion = etat["flotte"][tournee["camion"]]
    debut = max(maintenant, camion["libre_a"])
    while tournee["demandes"]:
        demandes = [etat["demandes"][i] for i in tournee["demandes"]]
        plan = evaluer_plan(ref, camion, demandes, debut, maintenant, couts)
        if plan["ok"]:
            break
        # Retire les demandes en retard (toutes si le plan échoue pour une autre raison)
        en_retard = [d["id"] for d in demandes
                     if "depart_controle" in plan["suivi"][d["id"]] and plan["suivi"][d["id"]]["depart_controle"] > d["h_dep"]]
        for id_demande in (en_retard or list(tournee["demandes"])):
            tournee["demandes"].remove(id_demande)
            etat["demandes"][id_demande].update({"etat": "EXPIREE", "motif": "REF-DELAI"})
            tracabilite.noter(journal, maintenant, "EXPIRATION", demande=etat["demandes"][id_demande],
                              tournee=tournee, motif="REF-DELAI")
    camion["tournee"] = None
    if not tournee["demandes"]:
        tournee["statut"] = "annulee"
        return
    tournee.update({"statut": "partie", "plan": plan})
    camion.update({"position": plan["lieu_fin"], "libre_a": plan["fin"], "conduite_jour": plan["conduite_jour"]})
    tracabilite.noter(journal, maintenant, "DEPART", tournee=tournee, remplissage=plan["remplissage"],
                      detail="fin %s, %d demande(s)" % (plan["fin"].strftime("%H:%M:%S"), len(tournee["demandes"])))
    for troncon in plan["troncons"]:
        tracabilite.noter(journal, maintenant, "TRONCON", tournee=tournee, troncon=troncon)
    for id_demande in tournee["demandes"]:
        demande = etat["demandes"][id_demande]
        suivi = plan["suivi"][id_demande]
        demande.update({"etat": "PARTIE", "gamme": suivi["gamme"], "h_depart": suivi["depart_controle"]})
        # Le premier horaire du détail est celui comparé à H_DEP (départ à vide de l'usine pour un import)
        if est_import_port(demande):
            detail = "départ %s (usine, à vide vers le port), chargé au port %s, arrivée %s" % (
                suivi["depart_controle"].strftime("%H:%M:%S"), suivi["depart"].strftime("%H:%M:%S"), suivi["arrivee"].strftime("%H:%M:%S"))
        else:
            detail = "départ %s, arrivée %s" % (suivi["depart"].strftime("%H:%M:%S"), suivi["arrivee"].strftime("%H:%M:%S"))
        tracabilite.noter(journal, maintenant, "PARTIE", demande=demande, tournee=tournee, gamme=suivi["gamme"], detail=detail)
