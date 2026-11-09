"""Règles métier élémentaires du moteur NORTH (contraintes dures).

Fonctions pures, sans état : elles disent si une demande est à nous, si elle
est exploitable, et si un camion peut la prendre. Les identifiants de règles
renvoient au catalogue ``3 - Documentation/regles-acceptation.docx``.

Une demande préparée par :func:`preparer_demande` est un dictionnaire qui
garde les champs de l'ERP et ajoute : ``id``, ``h_reception`` et ``h_dep``
(``datetime``, heure réelle), ``qte`` (``int``), ``spec`` (``dict``),
``nature`` (``"frais"`` ou ``"sec"``), ``pk_dep``, ``pk_arr``, ``etat`` et ``motif``.
"""

import datetime

import hypotheses
import referentiels
from collecteur import decouper_spec

#: Types de palettes et de marchandises connus (sujet p. 11).
TYPES_PAL = ("EPAL", "EPALh", "ISO")
TYPES_MAT = ("MP", "MP Frais", "PF", "PF Frais")

#: Clés SPEC et champ correspondant de la fiche camion.
CHAMPS_SPEC = {"PTAC": "PTAC", "ENR": "ENERGIE", "COL": "COULEUR"}


def lire_h_dep(h_dep: str):
    """Lit ``H_DEP`` (``JJ/MM/AA hh:mm:ss``, heure réelle).

    :param h_dep: Valeur reçue de l'ERP.
    :type h_dep: str
    :return: Date et heure, ou ``None`` si le format est inattendu.
    :rtype: datetime.datetime or None
    """
    try:
        return datetime.datetime.strptime(str(h_dep), "%d/%m/%y %H:%M:%S")
    except ValueError:
        return None


def preparer_demande(id_demande: str, champs: dict, h_reception: datetime.datetime, ref: dict) -> dict:
    """Complète une demande de l'ERP avec les champs calculés du moteur.

    :param id_demande: Identifiant unique de la demande.
    :param champs: Champs envoyés par l'ERP.
    :param h_reception: Heure de réception (réelle).
    :param ref: Référentiel (pour retrouver les lieux).
    :return: Demande préparée.
    """
    demande = dict(champs)
    try:
        qte = int(champs.get("QTE_PAL"))
    except (TypeError, ValueError):
        qte = None
    demande.update({"id": id_demande, "h_reception": h_reception, "h_dep": lire_h_dep(champs.get("H_DEP", "")),
                    "qte": qte, "spec": decouper_spec(champs.get("SPEC", "") or ""),
                    "nature": "frais" if str(champs.get("TYPE_MAT", "")).endswith("Frais") else "sec",
                    "pk_dep": referentiels.pk_lieu(ref, champs.get("LOC_DEP", "")),
                    "pk_arr": referentiels.pk_lieu(ref, champs.get("LOC_ARR", "")),
                    "etat": "RECUE", "motif": ""})
    return demande


def est_demande_north(demande: dict) -> bool:
    """Dit si la demande relève de la structure NORTH.

    Applique les règles R-PER-01 (export depuis une usine NORTH) et R-PER-02
    (import d'un port vers une usine NORTH, si :data:`hypotheses.IMPORTS_NORTH`).
    Amsterdam est à la fois usine et port : on décide sur ``TYPE_DEP`` et
    ``TYPE_ARR``, jamais sur ``LOC_DEP`` seul.

    :param demande: Demande (champs ERP au minimum).
    :return: ``True`` si la demande est à nous.
    """
    usines = {referentiels.normaliser(nom) for nom in hypotheses.USINES_NORTH}
    if demande.get("TYPE_DEP") == "USINE" and referentiels.normaliser(demande.get("LOC_DEP", "")) in usines:
        return True
    return (hypotheses.IMPORTS_NORTH and demande.get("TYPE_MVT") == "Import" and demande.get("TYPE_DEP") == "PORT"
            and demande.get("TYPE_ARR") == "USINE" and referentiels.normaliser(demande.get("LOC_ARR", "")) in usines)


def valider_demande(demande: dict, maintenant: datetime.datetime) -> bool:
    """Dit si la demande est exploitable.

    Applique la règle R-VAL-01 : lieux connus de ``LOCATIONS_NAMES``,
    ``QTE_PAL`` entier positif, ``TYPE_PAL`` et ``TYPE_MAT`` connus,
    ``H_DEP`` lisible et pas encore dépassée. Un échec donne le motif ``REF-DONNEES``.

    :param demande: Demande préparée.
    :param maintenant: Heure réelle courante.
    :return: ``True`` si la demande peut être examinée.
    """
    return (demande["pk_dep"] is not None and demande["pk_arr"] is not None
            and demande["qte"] is not None and demande["qte"] > 0
            and demande.get("TYPE_PAL") in TYPES_PAL and demande.get("TYPE_MAT") in TYPES_MAT
            and demande["h_dep"] is not None and demande["h_dep"] > maintenant)


def tonnes(ptac: str):
    """Convertit un PTAC (``"19t"``) en nombre de tonnes.

    :param ptac: PTAC au format des fiches et des SPEC.
    :type ptac: str
    :return: Tonnes, ou ``None`` si illisible.
    :rtype: float or None
    """
    try:
        return float(str(ptac).lower().replace("t", "").replace(",", ".").strip())
    except ValueError:
        return None


def verifier_spec(demande: dict, camion: dict) -> list:
    """Liste les exigences SPEC de la demande que le camion ne respecte pas.

    Applique les règles R-SPEC-01 et R-SPEC-02 : chaque clé de ``SPEC`` (PTAC,
    ENR, COL) est vérifiée ; selon :data:`hypotheses.SPEC_BLOQUANTE`, un écart
    donne un refus ou seulement une gamme plus basse. Les valeurs ``ENR=`` et ``COL=`` de l'ERP sont celles
    des champs ``ENERGIE`` et ``COULEUR`` des fiches : on compare sans tenir
    compte de la casse. ``PTAC`` est exact ou minimal selon
    :data:`hypotheses.PTAC_SPEC_MINIMUM` (question A5). Une liste non vide
    donne le motif ``REF-SPEC`` (SPEC bloquante) ou un déclassement de gamme.

    :param demande: Demande préparée (``demande["spec"]`` déjà découpé).
    :param camion: Camion de la flotte (champs de sa fiche).
    :return: Clés non respectées, par exemple ``["COL"]`` ; vide si le camion convient.
    """
    ecarts = []
    for cle, valeur in demande["spec"].items():
        champ = CHAMPS_SPEC.get(cle)
        if champ is None:
            if hypotheses.CLES_SPEC_INCONNUES_BLOQUANTES:
                ecarts.append(cle)
        elif cle == "PTAC" and hypotheses.PTAC_SPEC_MINIMUM:
            exige, reel = tonnes(valeur), tonnes(camion["PTAC"])
            if exige is None or reel is None or reel < exige:
                ecarts.append(cle)
        elif str(camion.get(champ, "")).strip().lower() != str(valeur).strip().lower():
            ecarts.append(cle)
    return ecarts


def verifier_frigo(demande: dict, camion: dict) -> bool:
    """Dit si le camion convient à la nature (frais ou sec) de la demande.

    Applique les règles R-FRA-01 (le frais exige une remorque réfrigérée) et
    R-FRA-02 (pas de sec en frigo : :data:`hypotheses.SEC_EN_FRIGO` vaut
    ``"interdit"`` depuis le 2026-10-21 ; en ``"dernier_recours"``, c'est le
    coût qui l'écarterait). Le non-mélange frais /
    sec dans un même camion est vérifié au niveau de la tournée.

    :param demande: Demande préparée.
    :param camion: Camion de la flotte.
    :return: ``True`` si le camion peut transporter cette nature de marchandise.
    """
    if demande["nature"] == "frais":
        return camion["frigo"]
    return not (camion["frigo"] and hypotheses.SEC_EN_FRIGO == "interdit")


def equivalent_plancher(demandes: list, ptac: str, ref: dict):
    """Part du plancher occupée par des demandes chargées ensemble.

    Applique la règle R-CAP-01 (``methode-contrat.md`` §1.1) : chaque demande
    occupe ``QTE_PAL / capacité(PTAC, TYPE_PAL)`` ; chaque type de palette
    supplémentaire coûte une rangée, ``ρ = 3 / capacité(PTAC, EPAL)``.
    Le chargement est admissible si le résultat ne dépasse pas 1.

    :param demandes: Demandes préparées, chargées en même temps.
    :type demandes: list
    :param ptac: PTAC du camion.
    :type ptac: str
    :param ref: Référentiel (``TRUCKS_CAPACITY``).
    :type ref: dict
    :return: Part du plancher (0 si aucune demande), ou ``None`` si une capacité est inconnue.
    :rtype: float or None
    """
    total = 0.0
    for demande in demandes:
        cap = referentiels.capacite(ref, ptac, demande["TYPE_PAL"])
        if not cap:
            return None
        total += demande["qte"] / cap
    types = {demande["TYPE_PAL"] for demande in demandes}
    if len(types) > 1:
        total += (len(types) - 1) * 3 / referentiels.capacite(ref, ptac, "EPAL")
    return total
