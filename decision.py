"""Décision sur une demande de transport NORTH : table D-ACC.

Pour chaque demande reçue, :func:`traiter_demande` parcourt la table D-ACC
(première ligne qui correspond) puis, si la demande est réalisable, choisit
le meilleur plan : l'insérer dans une tournée ouverte ou ouvrir une tournée
avec un camion libre.

Règles non couvertes à ce stade : R-ARB-01 (ne pas bloquer une commande
plus rentable : demande de connaître les demandes futures) et R-CAB-01
(cabotage, question C14).
"""

import datetime

import hypotheses
import planification
import regles
import tracabilite


def refuser(demande: dict, motif: str, maintenant: datetime.datetime, journal: dict) -> str:
    """Refuse une demande et écrit le refus avec son motif.

    :param demande: Demande préparée.
    :param motif: Motif ``REF-…``.
    :param maintenant: Heure réelle.
    :param journal: Journal de traçabilité.
    :return: ``"refuser"``.
    """
    demande.update({"etat": "REFUSEE", "motif": motif})
    tracabilite.noter(journal, maintenant, "REFUS", demande=demande, motif=motif)
    return "refuser"


def compatible(tournee: dict, demande: dict, etat: dict, ref: dict) -> bool:
    """Dit si une demande peut rejoindre une tournée ouverte (filtre rapide).

    Applique les règles R-FRA-01 (même nature, jamais frais et sec ensemble),
    R-SPEC-01 (le camion de la tournée respecte la SPEC de la nouvelle
    demande, seulement si :data:`hypotheses.SPEC_BLOQUANTE`), R-CAP-01 (le plancher reste ≤ 1) et l'hypothèse C13 (même lieu
    de chargement si :data:`hypotheses.REGROUPER_ENTRE_LIEUX_DEP` est faux).
    Les délais sont vérifiés ensuite par le plan.

    :param tournee: Tournée ouverte.
    :param demande: Nouvelle demande.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :return: ``True`` si l'insertion mérite d'être évaluée.
    """
    camion = etat["flotte"][tournee["camion"]]
    spec_ko = hypotheses.SPEC_BLOQUANTE and regles.verifier_spec(demande, camion)
    if tournee["nature"] != demande["nature"] or spec_ko or not regles.verifier_frigo(demande, camion):
        return False
    a_bord = [etat["demandes"][i] for i in tournee["demandes"]]
    if not hypotheses.REGROUPER_ENTRE_LIEUX_DEP and any((d["pk_dep"], d["TYPE_DEP"]) != (demande["pk_dep"], demande["TYPE_DEP"]) for d in a_bord):
        return False
    plancher = regles.equivalent_plancher(a_bord + [demande], camion["PTAC"], ref)
    return plancher is not None and plancher <= 1 + 1e-9


def signature(camion: dict) -> tuple:
    """Clé qui regroupe les camions interchangeables pour une demande.

    Deux camions de même signature donnent le même plan : on n'en évalue qu'un.

    :param camion: Camion de la flotte.
    :return: Tuple (position, heure de disponibilité, PTAC, énergie, couleur, frigo, usine, conduite).
    """
    return (camion["position"], camion["libre_a"], camion["PTAC"], camion["ENERGIE"], camion["COULEUR"].lower(),
            camion["frigo"], camion["usine"], round(camion["conduite_jour"], 2))


def motif_sans_candidat(demande: dict, echecs: list) -> str:
    """Choisit le motif de refus quand aucun plan n'est réalisable.

    Lignes 4 à 7 de D-ACC : aucun camion libre et compatible → REF-FRIGO
    (demande fraîche) ou REF-CAMION ; des plans existent mais arrivent trop
    tard → REF-CONDUCTEUR si seules les règles de conduite les en empêchent
    (R-CON-01), sinon REF-DELAI (R-DEL-01).

    :param demande: Demande préparée.
    :param echecs: Motifs d'échec des plans évalués (``"DELAI"``, ``"CONDUCTEUR"``…).
    :return: Motif ``REF-…``.
    """
    if not echecs or all(echec == "CAPACITE" for echec in echecs):
        return "REF-FRIGO" if demande["nature"] == "frais" else "REF-CAMION"
    if "CONDUCTEUR" in echecs:
        return "REF-CONDUCTEUR"
    return "REF-DELAI"


def traiter_demande(demande: dict, etat: dict, ref: dict, maintenant: datetime.datetime,
                    parametres: dict, journal: dict) -> str:
    """Décide d'une demande reçue : ignorer, refuser ou accepter.

    Implémente la table D-ACC (première ligne qui correspond) :

    1. hors périmètre NORTH (R-PER-01/02) → ignorer ;
    2. données inexploitables (R-VAL-01) → REF-DONNEES ;
    3. aucun camion de la flotte ne respecte la SPEC (R-SPEC-01) → REF-SPEC,
       seulement si :data:`hypotheses.SPEC_BLOQUANTE` ; sinon la demande
       continue et sa gamme sera abaissée (R-SPEC-02) ;
    4. frais sans aucun frigo compatible dans la flotte (R-FRA-01) → REF-FRIGO ;
    5. à 7. aucun plan réalisable → REF-CAMION / REF-FRIGO, REF-DELAI ou
       REF-CONDUCTEUR (voir :func:`motif_sans_candidat`) ;
    8. insertion possible dans une tournée ouverte → accepter ;
    9. sinon nouvelle tournée avec un camion libre → accepter.

    Entre les lignes 8 et 9, on retient d'abord un plan dont le camion respecte
    la SPEC (R-SPEC-02, pour garder la meilleure gamme), puis le coût marginal
    le plus bas, puis le remplissage le plus haut (R-OPT-01, R-OPT-02, R-OPT-03).

    :param demande: Demande préparée par :func:`regles.preparer_demande`.
    :param etat: État du moteur (``flotte``, ``tournees``, ``demandes``, ``compteur``), modifié sur place.
    :param ref: Référentiel.
    :param maintenant: Heure réelle courante.
    :param parametres: ``couts`` (taux horaires), ``seuil``, ``marge_h``.
    :param journal: Journal de traçabilité.
    :return: ``"ignorer"``, ``"refuser"`` ou ``"accepter"``.
    """
    etat["demandes"][demande["id"]] = demande
    if not regles.est_demande_north(demande):
        demande["etat"] = "IGNOREE"
        return "ignorer"
    if not regles.valider_demande(demande, maintenant):
        return refuser(demande, "REF-DONNEES", maintenant, journal)
    flotte = etat["flotte"]
    if hypotheses.SPEC_BLOQUANTE:
        conformes = [c for c in flotte.values() if not regles.verifier_spec(demande, c)]
        if not conformes:
            return refuser(demande, "REF-SPEC", maintenant, journal)
    else:
        # SPEC non bloquante : tout camion est candidat, ceux qui la respectent passent en premier
        conformes = sorted(flotte.values(), key=lambda c: bool(regles.verifier_spec(demande, c)))
    if demande["nature"] == "frais" and not any(c["frigo"] for c in conformes):
        return refuser(demande, "REF-FRIGO", maintenant, journal)

    couts = parametres["couts"]
    candidats, echecs = [], []
    # a) Insertion dans une tournée ouverte (consolidation)
    for tournee in etat["tournees"].values():
        if tournee["statut"] != "ouverte" or not compatible(tournee, demande, etat, ref):
            continue
        camion = flotte[tournee["camion"]]
        a_bord = [etat["demandes"][i] for i in tournee["demandes"]]
        debut = max(maintenant, camion["libre_a"])
        plan = planification.evaluer_avec_motif(ref, camion, a_bord + [demande], debut, maintenant, couts)
        if plan["ok"]:
            avant = planification.evaluer_plan(ref, camion, a_bord, debut, maintenant, couts)
            cout_avant = avant["cout"] if avant["ok"] else 0.0
            candidats.append((bool(regles.verifier_spec(demande, camion)), plan["cout"] - cout_avant,
                              -plan["remplissage"], tournee["id"], None, plan))
        else:
            echecs.append(plan["motif"])
    # b) Nouvelle tournée : un plan par groupe de camions interchangeables
    vus = set()
    for camion in conformes:
        if camion["tournee"] is not None or not regles.verifier_frigo(demande, camion) or signature(camion) in vus:
            continue
        vus.add(signature(camion))
        plan = planification.evaluer_avec_motif(ref, camion, [demande], max(maintenant, camion["libre_a"]), maintenant, couts)
        if plan["ok"]:
            candidats.append((bool(regles.verifier_spec(demande, camion)), plan["cout"], -plan["remplissage"],
                              None, camion["IMMAT"], plan))
        else:
            echecs.append(plan["motif"])

    if not candidats:
        return refuser(demande, motif_sans_candidat(demande, echecs), maintenant, journal)

    _, _, _, id_tournee, immat, plan = min(candidats, key=lambda c: (c[0], c[1], c[2]))
    if id_tournee is None:
        etat["compteur"] += 1
        id_tournee = "T%05d" % etat["compteur"]
        etat["tournees"][id_tournee] = {"id": id_tournee, "camion": immat, "nature": demande["nature"],
                                        "demandes": [], "statut": "ouverte", "plan": plan}
        flotte[immat]["tournee"] = id_tournee
    tournee = etat["tournees"][id_tournee]
    tournee["demandes"].append(demande["id"])
    tournee["plan"] = plan
    demande.update({"etat": "ACCEPTEE", "tournee": id_tournee})
    tracabilite.noter(journal, maintenant, "ACQUITTEMENT", demande=demande, tournee=tournee, remplissage=plan["remplissage"],
                      detail="départ au plus tard dans %.1f h projet" % plan["marge_h"])
    return "accepter"
