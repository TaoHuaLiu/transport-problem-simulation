"""Journal de traçabilité du moteur NORTH et calcul des indicateurs.

Chaque décision et chaque mouvement est écrit dans un journal CSV local
(séparateur ``;``), une ligne par événement :

- ``ACQUITTEMENT`` / ``REFUS`` (avec motif ``REF-…``) : décision D-ACC ;
- ``DEPART`` : une tournée part ; ``TRONCON`` : un trajet de cette tournée ;
- ``PARTIE`` : une demande quitte son lieu de départ (avec sa gamme) ;
- ``EXPIRATION`` : une demande acceptée n'est pas partie avant ``H_DEP``.

Les demandes hors périmètre NORTH ne sont pas journalisées : elles restent
seulement dans l'état du moteur (``etat = "IGNOREE"``).

Ce journal sert à reprendre après un arrêt (question E18) et de source à
la table traçabilité partagée. L'écriture dans cette table attend son
schéma, fixé par le Contrôle (question A1).
"""

import csv
import datetime
import os

#: Colonnes du journal, dans l'ordre.
COLONNES = ["H_EVENEMENT", "EVENEMENT", "ID_DEMANDE", "ID_TOURNEE", "IMMAT", "LIEU_DEP", "LIEU_ARR",
            "H_DEBUT", "H_FIN", "DUREE_H", "REMPLISSAGE", "DEMANDES_A_BORD", "GAMME", "MOTIF", "DETAIL"]

#: Format des dates du journal (heure réelle).
FORMAT_DATE = "%Y-%m-%d %H:%M:%S"


def ouvrir_journal(repertoire: str, debut: datetime.datetime, suffixe: str = "") -> dict:
    """Crée le fichier du journal et son en-tête.

    :param repertoire: Dossier de sortie (créé si besoin).
    :param debut: Heure réelle de démarrage, qui nomme le fichier.
    :param suffixe: Complément du nom (par exemple ``"_rejeu"``).
    :return: Journal : ``{"chemin", "nb"}``.
    """
    os.makedirs(repertoire, exist_ok=True)
    chemin = os.path.join(repertoire, "evenements_%s%s.csv" % (debut.strftime("%Y-%m-%d_%H%M%S"), suffixe))
    with open(chemin, "w", newline="", encoding="utf-8") as fichier:
        csv.DictWriter(fichier, fieldnames=COLONNES, delimiter=";").writeheader()
    return {"chemin": chemin, "nb": 0}


def noter(journal: dict, maintenant: datetime.datetime, evenement: str, demande: dict = None, tournee: dict = None,
          troncon: dict = None, motif: str = "", gamme: str = "", remplissage: float = None, detail: str = "") -> None:
    """Ajoute un événement au journal.

    :param journal: Journal ouvert par :func:`ouvrir_journal`.
    :param maintenant: Heure réelle de l'événement.
    :param evenement: Type d'événement (voir l'en-tête du module).
    :param demande: Demande concernée, s'il y en a une.
    :param tournee: Tournée concernée, s'il y en a une.
    :param troncon: Tronçon (événement ``TRONCON``) : lieux en ``PK_LOCATIONS``.
    :param motif: Motif de refus ``REF-…``.
    :param gamme: Gamme de la demande.
    :param remplissage: Part du plancher occupée.
    :param detail: Texte libre.
    """
    ligne = {"H_EVENEMENT": maintenant.strftime(FORMAT_DATE), "EVENEMENT": evenement,
             "MOTIF": motif, "GAMME": gamme or "", "DETAIL": detail}
    if demande is not None:
        ligne.update({"ID_DEMANDE": demande["id"], "LIEU_DEP": demande.get("LOC_DEP", ""), "LIEU_ARR": demande.get("LOC_ARR", "")})
    if tournee is not None:
        ligne.update({"ID_TOURNEE": tournee["id"], "IMMAT": tournee["camion"]})
    if troncon is not None:
        ligne.update({"LIEU_DEP": troncon["de"], "LIEU_ARR": troncon["a"],
                      "H_DEBUT": troncon["debut"].strftime(FORMAT_DATE), "H_FIN": troncon["fin"].strftime(FORMAT_DATE),
                      "DUREE_H": "%.2f" % troncon["duree_h"], "DEMANDES_A_BORD": ",".join(troncon["demandes"])})
        remplissage = troncon["remplissage"]
    if remplissage is not None:
        ligne["REMPLISSAGE"] = "%.3f" % remplissage
    with open(journal["chemin"], "a", newline="", encoding="utf-8") as fichier:
        csv.DictWriter(fichier, fieldnames=COLONNES, delimiter=";").writerow(ligne)
    journal["nb"] += 1


def calculer_kpi(etat: dict) -> dict:
    """Calcule les indicateurs du contrat à partir de l'état du moteur.

    Applique les formules proposées (``methode-contrat.md`` §1) :

    - TA (R-KPI-TA) = demandes NORTH parties à temps / demandes NORTH reçues ;
    - TR (R-KPI-TR) = moyenne, sur les tournées parties, du remplissage au
      départ du premier lieu de chargement ;
    - TR tronçons (information) = Σ remplissage × durée / Σ durée, tronçons à
      vide compris.

    Ce calcul est le nôtre : le Contrôle le refait indépendamment à partir
    du journal.

    :param etat: État du moteur (``demandes``, ``tournees``).
    :return: ``ta``, ``tr``, ``tr_troncons``, ``recues``, ``parties``, ``refus`` (par motif), ``gammes``,
        ``tournees``, ``en_attente`` (acceptées pas encore parties).
    """
    north = [d for d in etat["demandes"].values() if d["etat"] != "IGNOREE"]
    parties = [d for d in north if d["etat"] == "PARTIE"]
    refus = {}
    for demande in north:
        if demande["etat"] in ("REFUSEE", "EXPIREE"):
            refus[demande["motif"]] = refus.get(demande["motif"], 0) + 1
    gammes = {}
    for demande in parties:
        gammes[demande["gamme"]] = gammes.get(demande["gamme"], 0) + 1
    parties_t = [t for t in etat["tournees"].values() if t["statut"] == "partie"]
    troncons = [t for tournee in parties_t for t in tournee["plan"]["troncons"]]
    duree = sum(t["duree_h"] for t in troncons)
    # Une tournée charge en un seul lieu (C13) : son remplissage au départ est celui du plan
    remplissages = [t["plan"]["remplissage"] for t in parties_t]
    return {"recues": len(north), "parties": len(parties), "ta": len(parties) / len(north) if north else 0.0,
            "tr": sum(remplissages) / len(remplissages) if remplissages else 0.0,
            "tr_troncons": sum(t["remplissage"] * t["duree_h"] for t in troncons) / duree if duree else 0.0,
            "refus": refus, "gammes": gammes,
            "tournees": sum(1 for t in etat["tournees"].values() if t["statut"] == "partie"),
            "en_attente": sum(1 for d in north if d["etat"] == "ACCEPTEE")}


def resume_kpi(kpi: dict) -> str:
    """Met les indicateurs en forme pour la console.

    :param kpi: Résultat de :func:`calculer_kpi`.
    :return: Texte sur plusieurs lignes.
    """
    lignes = ["Demandes NORTH reçues : %d, parties à temps : %d" % (kpi["recues"], kpi["parties"]),
              "TA (taux d'acceptation) : %.1f %%" % (100 * kpi["ta"]),
              "TR (remplissage au premier départ) : %.1f %% ; pondéré par tronçon : %.1f %%"
              % (100 * kpi["tr"], 100 * kpi["tr_troncons"]),
              "Tournées parties : %d ; demandes acceptées en attente : %d" % (kpi["tournees"], kpi["en_attente"]),
              "Refus par motif : " + (", ".join("%s %d" % m for m in sorted(kpi["refus"].items())) or "aucun"),
              "Gammes : " + (", ".join("%s %d" % g for g in sorted(kpi["gammes"].items())) or "aucune")]
    return "\n".join(lignes)
