"""Journal de traçabilité du moteur NORTH et calcul des indicateurs.

Chaque décision et chaque mouvement est écrit dans un journal CSV local
(séparateur ``;``), une ligne par événement :

- ``ACQUITTEMENT`` / ``REFUS`` (avec motif ``REF-…``) : décision D-ACC ;
- ``DEPART`` : une tournée part ; ``TRONCON`` : un trajet de cette tournée ;
- ``PARTIE`` : une demande quitte son lieu de départ (avec sa gamme) ;
- ``REAFFECTATION`` : une demande acceptée, retirée d'un plan devenu impossible,
  est replacée dans une autre tournée (R-DEL-02) ;
- ``EXPIRATION`` : une demande acceptée n'est pas partie avant ``H_DEP``, ou
  n'a trouvé aucun autre plan après son retrait (R-DEL-02, motif de D-ACC).

Les demandes hors périmètre NORTH ne sont pas journalisées : elles restent
seulement dans l'état du moteur (``etat = "IGNOREE"``).

Ce journal est la source de référence. Les tables ``LOG_NORTH_*`` de la base
(camions, trajets, commandes) en sont le reflet, écrit par :mod:`archivage`.
L'écriture dans la table traçabilité partagée attend son schéma, fixé par le
Contrôle (question A1).

Règle R-ROB-08 : :func:`noter` ne lève jamais d'erreur. Une ligne qui ne peut
pas être écrite (disque plein, droits, fichier supprimé) reste en mémoire et
est réécrite, dans l'ordre, à l'événement suivant ; l'échec est journalisé en
erreur, compté dans ``journal["sante"]`` (repris dans ``sante_moteur.json``
sous la clé ``evenements``) et le rétablissement est annoncé.
"""

import csv
import datetime
import logging
import os

import robustesse

#: Colonnes du journal, dans l'ordre.
COLONNES = ["H_EVENEMENT", "EVENEMENT", "ID_DEMANDE", "ID_TOURNEE", "IMMAT", "LIEU_DEP", "LIEU_ARR",
            "H_DEBUT", "H_FIN", "DUREE_H", "REMPLISSAGE", "DEMANDES_A_BORD", "GAMME", "MOTIF", "DETAIL"]

#: Format des dates du journal (heure réelle).
FORMAT_DATE = "%Y-%m-%d %H:%M:%S"

#: Nombre maximal de lignes gardées en mémoire quand le fichier ne peut pas être écrit (R-ROB-08) ;
#: au-delà, les plus anciennes sont perdues et comptées dans ``journal["perdus"]``.
MAX_EN_ATTENTE = 100000


def ouvrir_journal(repertoire: str, debut: datetime.datetime, suffixe: str = "") -> dict:
    """Crée le fichier du journal et son en-tête.

    :param repertoire: Dossier de sortie (créé si besoin).
    :param debut: Heure réelle de démarrage, qui nomme le fichier.
    :param suffixe: Complément du nom (par exemple ``"_rejeu"``).
    :return: Journal : ``{"chemin", "nb", "en_attente", "perdus", "messages", "sante"}`` ; ``nb`` = lignes
        écrites, ``en_attente`` = lignes pas encore écrites (R-ROB-08), ``sante`` = :func:`robustesse.nouvel_etat_bdd`.
    """
    os.makedirs(repertoire, exist_ok=True)
    chemin = os.path.join(repertoire, "evenements_%s%s.csv" % (debut.strftime("%Y-%m-%d_%H%M%S"), suffixe))
    with open(chemin, "w", newline="", encoding="utf-8") as fichier:
        csv.DictWriter(fichier, fieldnames=COLONNES, delimiter=";").writeheader()
    return {"chemin": chemin, "nb": 0, "en_attente": [], "perdus": 0, "messages": {}, "sante": robustesse.nouvel_etat_bdd()}


def noter(journal: dict, maintenant: datetime.datetime, evenement: str, demande: dict = None, tournee: dict = None,
          troncon: dict = None, motif: str = "", gamme: str = "", remplissage: float = None, detail: str = "") -> None:
    """Ajoute un événement au journal, sans jamais lever d'erreur.

    Applique la règle R-ROB-08 : si le fichier ne peut pas être écrit, la
    ligne est gardée en mémoire (au plus :data:`MAX_EN_ATTENTE`) et réécrite
    avec les suivantes ; l'échec est signalé en erreur (au plus une fois par
    minute) et noté dans ``journal["sante"]``.

    :param journal: Journal ouvert par :func:`ouvrir_journal`.
    :param maintenant: Heure réelle de l'événement.
    :param evenement: Type d'événement (voir l'en-tête du module).
    :param demande: Demande concernée, s'il y en a une.
    :param tournee: Tournée concernée, s'il y en a une.
    :param troncon: Tronçon (événement ``TRONCON``) : lieux en ``PK_LOCATIONS``.
    :param motif: Motif de refus ``REF-…``.
    :param gamme: Gamme de la demande.
    :param remplissage: Taux de remplissage (surface au sol, R-KPI-TR).
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
    en_attente = journal.setdefault("en_attente", [])
    en_attente.append(ligne)
    if len(en_attente) > MAX_EN_ATTENTE:
        trop = len(en_attente) - MAX_EN_ATTENTE
        del en_attente[:trop]
        journal["perdus"] = journal.get("perdus", 0) + trop
    ecrire_en_attente(journal, maintenant)


def ecrire_en_attente(journal: dict, maintenant: datetime.datetime) -> bool:
    """Écrit les lignes en attente du journal ; ne lève jamais d'erreur.

    Applique la règle R-ROB-08.

    :param journal: Journal ouvert par :func:`ouvrir_journal`.
    :param maintenant: Heure réelle (ou simulée en rejeu), pour les messages et la santé.
    :return: ``True`` s'il ne reste aucune ligne en attente.
    """
    en_attente = journal.setdefault("en_attente", [])
    sante = journal.setdefault("sante", robustesse.nouvel_etat_bdd())
    if not en_attente:
        return True
    nombre = len(en_attente)
    try:
        with open(journal["chemin"], "a", newline="", encoding="utf-8") as fichier:
            csv.DictWriter(fichier, fieldnames=COLONNES, delimiter=";").writerows(en_attente)
    except (OSError, ValueError, csv.Error) as erreur:  # disque plein, droits, fichier supprimé…
        robustesse.noter_echec_bdd(sante, maintenant, erreur, nombre)
        robustesse.signaler(journal.setdefault("messages", {}), "journal", maintenant,
                            "journal des événements %s EN ÉCHEC (%r) : %d ligne(s) gardée(s) en mémoire, %d perdue(s)"
                            % (os.path.basename(journal["chemin"]), erreur, nombre, journal.get("perdus", 0)),
                            logging.ERROR, "tracabilite")
        return False
    del en_attente[:]
    journal["nb"] = journal.get("nb", 0) + nombre
    robustesse.noter_succes_bdd(sante, maintenant, nombre, "journal des événements")
    return True


def calculer_kpi(etat: dict) -> dict:
    """Calcule les indicateurs du contrat à partir de l'état du moteur.

    Applique les formules proposées (``methode-contrat.md`` §1) :

    - TA (R-KPI-TA) = demandes NORTH parties à temps / demandes NORTH reçues ;
    - TR (R-KPI-TR) = moyenne, sur les tournées parties, du remplissage au
      départ du premier lieu de chargement, mesuré par la surface au sol des
      palettes (:func:`regles.taux_remplissage`) ;
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
    # Le plan garde le remplissage au départ du premier lieu de chargement (R-KPI-TR), même si la tournée charge ailleurs ensuite
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
