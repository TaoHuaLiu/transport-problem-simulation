"""Indicateurs d'audit NORTH recalculés à partir des fichiers seulement.

Le tableau de bord (:mod:`tableau_bord`) et la commande ``--instantane``
lisent deux sources, comme le fera le groupe Contrôle :

- le **journal du moteur** (``../donnees/moteur/evenements_*.csv``, écrit par
  :mod:`tracabilite`) : décisions, départs, tronçons, gammes ;
- les **demandes reçues** (``../donnees/demandes/demandes_*.csv`` ou ``.jsonl``,
  écrites par :mod:`collecteur`) : dénominateur du taux d'acceptation et
  ``H_DEP`` de chaque demande.

Le périmètre NORTH est recalculé ici sans passer par :mod:`regles` : si le
moteur oublie une demande ou en traite une qui n'est pas à nous, le
tableau de bord le montre.

Unités : les dates sont en heure réelle ; 1 minute réelle = 1 heure projet,
donc une durée en minutes réelles se lit directement en heures projet.
"""

import csv
import datetime
import glob
import json
import os
import statistics
import unicodedata

from collecteur import decoder_message

DOSSIER = os.path.dirname(os.path.abspath(__file__))

#: Dossier des journaux du moteur.
REPERTOIRE_JOURNAUX = os.path.join(DOSSIER, "..", "donnees", "moteur")

#: Dossier des demandes écrites par le collecteur.
REPERTOIRE_DEMANDES = os.path.join(DOSSIER, "..", "donnees", "demandes")

#: Fichier des cibles du contrat (hors git), voir :func:`charger_cibles`.
FICHIER_CIBLES = os.path.join(DOSSIER, "..", "donnees", "cibles_contrat.json")

#: Usines NORTH, noms normalisés (majuscules, sans accents).
USINES_NORTH = ("AMSTERDAM", "DUSSELDORF", "MANCHESTER")

#: Format des dates du journal et du CSV du collecteur.
FORMAT_DATE = "%Y-%m-%d %H:%M:%S"

#: Barème du sujet : écart relatif unilatéral maximal → note sur 10 (``methode-contrat.md`` §1.4).
BAREME = [(0.03, 10.0), (0.07, 8.5), (0.15, 6.0), (0.35, 3.0), (0.50, 1.0)]

#: Cibles par défaut : TA = notre proposition (85 % sur 2 h), les autres restent à saisir.
CIBLES_DEFAUT = {"ta": 0.85, "tr": None, "part_haute": None}

#: Délai (secondes réelles) après la réception au-delà duquel une demande sans décision est une anomalie.
DELAI_DECISION_S = 30

#: Ancienneté (secondes réelles) du dernier événement au-delà de laquelle le moteur est jugé arrêté.
SILENCE_MAX_S = 60

#: Pas possibles des séries temporelles, en minutes réelles.
PAS_POSSIBLES_MIN = [1, 2, 5, 10, 15, 30, 60]

# Cache des fichiers déjà lus : chemin → (date de modification, taille, contenu décodé)
_CACHE = {}


def normaliser_lieu(nom: str) -> str:
    """Met un nom de lieu en majuscules sans accents, pour comparer sans ambiguïté.

    >>> normaliser_lieu(" Düsseldorf ")
    'DUSSELDORF'

    :param nom: Nom tel qu'envoyé par l'ERP.
    :return: Nom normalisé.
    """
    decompose = unicodedata.normalize("NFKD", nom or "")
    return "".join(c for c in decompose if not unicodedata.combining(c)).strip().upper()


def usine_north(champs: dict) -> str:
    """Donne l'usine NORTH concernée par une demande, ou ``""`` si elle n'est pas à nous.

    Applique les règles R-PER-01 (export depuis une usine NORTH) et R-PER-02
    (import d'un port vers une usine NORTH). Le lieu de départ seul ne suffit
    pas : Amsterdam est à la fois usine et port.

    :param champs: Champs de la demande (``TYPE_DEP``, ``TYPE_ARR``, ``LOC_DEP``, ``LOC_ARR``).
    :return: Nom normalisé de l'usine, ou ``""``.
    """
    type_dep = normaliser_lieu(champs.get("TYPE_DEP", ""))
    type_arr = normaliser_lieu(champs.get("TYPE_ARR", ""))
    depart = normaliser_lieu(champs.get("LOC_DEP", ""))
    arrivee = normaliser_lieu(champs.get("LOC_ARR", ""))
    if type_dep == "USINE" and depart in USINES_NORTH:
        return depart
    if type_dep == "PORT" and type_arr == "USINE" and arrivee in USINES_NORTH:
        return arrivee
    return ""


def lire_heure(texte: str) -> datetime.datetime:
    """Lit une date ``AAAA-MM-JJ hh:mm:ss`` ou ``JJ/MM/AA hh:mm:ss``.

    :param texte: Date en texte, éventuellement vide.
    :return: Date, ou ``None`` si le texte est vide ou illisible.
    :rtype: datetime.datetime
    """
    for format_date in (FORMAT_DATE, "%d/%m/%y %H:%M:%S"):
        try:
            return datetime.datetime.strptime((texte or "").strip(), format_date)
        except ValueError:
            continue
    return None


def _lire_avec_cache(chemin: str, lecteur) -> object:
    """Relit un fichier seulement s'il a changé depuis la lecture précédente.

    :param chemin: Fichier à lire.
    :param lecteur: Fonction ``chemin → contenu décodé``.
    :type lecteur: callable
    :return: Contenu décodé.
    """
    infos = os.stat(chemin)
    cle = (infos.st_mtime, infos.st_size)
    if chemin not in _CACHE or _CACHE[chemin][0] != cle:
        _CACHE[chemin] = (cle, lecteur(chemin))
    return _CACHE[chemin][1]


def _decoder_demande(id_demande: str, champs: dict, h_reception: datetime.datetime) -> dict:
    """Garde d'une demande les champs utiles à l'audit.

    :param id_demande: Identifiant unique.
    :param champs: Champs bruts (JSONL ou CSV du collecteur).
    :param h_reception: Heure réelle de réception.
    :return: ``{"id", "h_reception", "h_dep", "type_mvt", "usine", "loc_dep", "loc_arr", "qte_pal"}``.
    """
    return {"id": id_demande, "h_reception": h_reception,
            "h_dep": lire_heure(champs.get("H_DEP_ISO") or champs.get("H_DEP", "")),
            "type_mvt": champs.get("TYPE_MVT", ""), "usine": usine_north(champs),
            "loc_dep": champs.get("LOC_DEP", ""), "loc_arr": champs.get("LOC_ARR", ""),
            "qte_pal": champs.get("QTE_PAL", "")}


def _lire_fichier_demandes(chemin: str) -> dict:
    """Lit un fichier du collecteur, brut (``.jsonl``) ou structuré (``.csv``).

    :param chemin: Fichier ``demandes_*.jsonl`` ou ``demandes_*.csv``.
    :return: ``{id: demande}`` (voir :func:`_decoder_demande`).
    """
    demandes = {}
    with open(chemin, newline="", encoding="utf-8") as fichier:
        if chemin.endswith(".jsonl"):
            for ligne in fichier:
                try:
                    entree = json.loads(ligne)
                    message = decoder_message(entree["MESSAGE"])
                except (ValueError, KeyError):
                    continue  # message illisible : il reste dans le .jsonl, sans demande exploitable
                h_reception = lire_heure(entree.get("H_RECEPTION", ""))
                for id_demande, champs in message.items():
                    if isinstance(champs, dict) and id_demande not in demandes:
                        demandes[id_demande] = _decoder_demande(id_demande, champs, h_reception)
        else:
            for ligne in csv.DictReader(fichier, delimiter=";"):
                if ligne.get("ID_DEMANDE"):
                    demandes[ligne["ID_DEMANDE"]] = _decoder_demande(ligne["ID_DEMANDE"], ligne, lire_heure(ligne.get("H_RECEPTION", "")))
    return demandes


def _lire_fichier_journal(chemin: str) -> list:
    """Lit le journal du moteur (séparateur ``;``).

    Une dernière ligne incomplète (en cours d'écriture) est ignorée.

    :param chemin: Fichier ``evenements_*.csv``.
    :return: Liste de dictionnaires, un par événement, avec ``H`` (date lue).
    """
    evenements = []
    with open(chemin, newline="", encoding="utf-8") as fichier:
        for ligne in csv.DictReader(fichier, delimiter=";"):
            ligne["H"] = lire_heure(ligne.get("H_EVENEMENT", ""))
            if ligne["H"] is not None and ligne.get("EVENEMENT"):
                evenements.append(ligne)
    return evenements


def dernier_journal(source: str) -> str:
    """Choisit le journal à lire : le fichier donné, ou le plus récent d'un dossier.

    :param source: Fichier ``evenements_*.csv`` ou dossier qui en contient.
    :return: Chemin du journal, ou ``""`` s'il n'y en a aucun.
    """
    if os.path.isfile(source):
        return source
    candidats = glob.glob(os.path.join(source, "evenements_*.csv"))
    return max(candidats, key=os.path.getmtime) if candidats else ""


def fichiers_demandes(source: str, jours: list) -> list:
    """Liste les fichiers du collecteur à lire.

    Dans un dossier, on préfère le CSV de chaque jour réel couvert par le
    journal (le ``.jsonl`` s'il n'y a pas de CSV) : relire tous les jours
    passés serait inutile.

    :param source: Fichier ``.csv``/``.jsonl`` ou dossier du collecteur.
    :param jours: Jours réels couverts par le journal.
    :return: Chemins existants.
    """
    if os.path.isfile(source):
        return [source]
    chemins = []
    for jour in jours:
        for extension in ("csv", "jsonl"):
            chemin = os.path.join(source, "demandes_%s.%s" % (jour.strftime("%Y-%m-%d"), extension))
            if os.path.exists(chemin):
                chemins.append(chemin)
                break
    return chemins


def charger_sources(source_journal: str, source_demandes: str) -> dict:
    """Lit le journal et les demandes reçues (avec cache).

    :param source_journal: Fichier ou dossier des journaux du moteur.
    :param source_demandes: Fichier ou dossier du collecteur.
    :return: ``{"journal", "evenements", "fichiers_demandes", "demandes", "maj_journal"}``.
    """
    chemin = dernier_journal(source_journal)
    evenements = _lire_avec_cache(chemin, _lire_fichier_journal) if chemin else []
    jours = []
    if evenements:
        jour, fin = evenements[0]["H"].date(), evenements[-1]["H"].date()
        while jour <= fin:
            jours.append(jour)
            jour += datetime.timedelta(days=1)
    else:
        jours = [datetime.date.today()]
    chemins = fichiers_demandes(source_demandes, jours)
    demandes = {}
    for chemin_demandes in chemins:
        demandes.update(_lire_avec_cache(chemin_demandes, _lire_fichier_demandes))
    return {"journal": chemin, "evenements": evenements, "fichiers_demandes": chemins, "demandes": demandes,
            "maj_journal": datetime.datetime.fromtimestamp(os.path.getmtime(chemin)) if chemin else None}


def heure_depart_controle(evenement: dict) -> datetime.datetime:
    """Lit l'heure de départ comparée à ``H_DEP`` dans le détail d'un événement ``PARTIE``.

    Le détail commence par ``départ hh:mm:ss`` : départ chargé pour un export,
    départ à vide de l'usine pour un import (règle R-DEL-01). Le départ n'est
    jamais avant l'événement : une heure plus petite veut dire le lendemain.

    :param evenement: Ligne ``PARTIE`` du journal.
    :return: Date du départ, ou ``None`` si le détail est illisible.
    :rtype: datetime.datetime
    """
    morceaux = evenement.get("DETAIL", "").replace(",", " ").split()
    if len(morceaux) < 2 or morceaux[0] != "départ":
        return None
    try:
        heure = datetime.datetime.strptime(morceaux[1], "%H:%M:%S").time()
    except ValueError:
        return None
    depart = datetime.datetime.combine(evenement["H"].date(), heure)
    if depart < evenement["H"] - datetime.timedelta(minutes=1):
        depart += datetime.timedelta(days=1)
    return depart


def statuts_demandes(evenements: list, demandes: dict, maintenant: datetime.datetime) -> dict:
    """Donne le statut final de chaque demande NORTH, d'après le journal.

    Applique la règle R-KPI-TA : seule une demande **partie** compte comme
    acceptée ; une demande acquittée puis refusée, ou expirée, compte comme
    refusée. Statuts : ``partie``, ``refusee``, ``en_attente`` (acquittée,
    ``H_DEP`` pas encore passée), ``en_retard`` (acquittée, ``H_DEP`` passée
    sans départ), ``sans_decision``.

    :param evenements: Journal du moteur.
    :param demandes: Demandes reçues par le collecteur.
    :param maintenant: Heure réelle de référence.
    :return: ``{id: {"statut", "motif", "gamme", "usine", "type_mvt", "h_reception", "h_dep", "h_decision",
        "h_partie", "depart", "acq_puis_refus", "hors_perimetre", "inconnue"}}``.
    """
    statuts = {}
    for id_demande, demande in demandes.items():
        if demande["usine"]:
            statuts[id_demande] = {"statut": "sans_decision", "motif": "", "gamme": "", "usine": demande["usine"],
                                   "type_mvt": demande["type_mvt"], "h_reception": demande["h_reception"],
                                   "h_dep": demande["h_dep"], "h_decision": None, "h_partie": None, "depart": None,
                                   "acq_puis_refus": False, "hors_perimetre": False, "inconnue": False}
    for evenement in evenements:
        id_demande = evenement.get("ID_DEMANDE", "")
        if not id_demande or evenement["EVENEMENT"] not in ("ACQUITTEMENT", "REFUS", "EXPIRATION", "PARTIE"):
            continue
        if id_demande not in statuts:
            # Demande journalisée mais absente du collecteur, ou hors périmètre NORTH
            connue = demandes.get(id_demande)
            # Sans le collecteur, le sens se lit à la fin de l'identifiant ERP (…E export, …I import)
            sens = "Import" if id_demande.endswith("I") else "Export"
            if sens == "Import":
                usine = usine_north({"TYPE_DEP": "PORT", "TYPE_ARR": "USINE", "LOC_ARR": evenement.get("LIEU_ARR", "")})
            else:
                usine = usine_north({"TYPE_DEP": "USINE", "LOC_DEP": evenement.get("LIEU_DEP", "")})
            statuts[id_demande] = {"statut": "sans_decision", "motif": "", "gamme": "",
                                   "usine": connue["usine"] if connue else usine,
                                   "type_mvt": connue["type_mvt"] if connue else sens,
                                   "h_reception": connue["h_reception"] if connue else evenement["H"],
                                   "h_dep": connue["h_dep"] if connue else None, "h_decision": None, "h_partie": None,
                                   "depart": None, "acq_puis_refus": False,
                                   "hors_perimetre": connue is not None, "inconnue": connue is None}
        suivi = statuts[id_demande]
        if suivi["h_decision"] is None:
            suivi["h_decision"] = evenement["H"]
        if evenement["EVENEMENT"] == "ACQUITTEMENT" and suivi["statut"] == "sans_decision":
            suivi["statut"] = "en_attente"
        elif evenement["EVENEMENT"] in ("REFUS", "EXPIRATION") and suivi["statut"] != "partie":
            suivi["acq_puis_refus"] = suivi["acq_puis_refus"] or suivi["statut"] == "en_attente"
            suivi.update({"statut": "refusee", "motif": evenement.get("MOTIF", "") or "REF-?"})
        elif evenement["EVENEMENT"] == "PARTIE":
            suivi.update({"statut": "partie", "gamme": evenement.get("GAMME", ""), "h_partie": evenement["H"],
                          "depart": heure_depart_controle(evenement), "motif": ""})
    for suivi in statuts.values():
        if suivi["statut"] == "en_attente" and suivi["h_dep"] is not None and maintenant > suivi["h_dep"]:
            suivi["statut"] = "en_retard"
    return statuts


def ecart_et_note(realise: float, cible: float, sens: str = "haut") -> dict:
    """Calcule l'écart relatif unilatéral à la cible et la note du barème.

    Faire mieux que la cible ne rapporte rien de plus (``methode-contrat.md`` §1.4).

    >>> ecart_et_note(0.80, 0.85)["note"]
    8.5

    :param realise: Valeur mesurée.
    :param cible: Cible du contrat (``None`` : pas de note).
    :param sens: ``"haut"`` si plus c'est haut mieux c'est, ``"bas"`` sinon.
    :return: ``{"cible", "ecart", "note"}`` (``None`` si pas de cible ou pas de mesure).
    """
    if cible is None or realise is None or cible <= 0:
        return {"cible": cible, "ecart": None, "note": None}
    ecart = max(0.0, (cible - realise) / cible) if sens == "haut" else max(0.0, (realise - cible) / cible)
    note = 0.0
    for ecart_max, valeur in BAREME:
        if ecart <= ecart_max + 1e-9:
            note = valeur
            break
    return {"cible": cible, "ecart": ecart, "note": note}


def quantiles(valeurs: list) -> dict:
    """Résume une série de nombres.

    :param valeurs: Nombres.
    :return: ``{"n", "moy", "med", "p10", "p90", "min", "max"}`` (``None`` si la série est vide).
    """
    if not valeurs:
        return {"n": 0, "moy": None, "med": None, "p10": None, "p90": None, "min": None, "max": None}
    triees = sorted(valeurs)
    rang = lambda p: triees[min(len(triees) - 1, int(p * len(triees)))]  # noqa: E731
    return {"n": len(triees), "moy": statistics.mean(triees), "med": statistics.median(triees),
            "p10": rang(0.1), "p90": rang(0.9), "min": triees[0], "max": triees[-1]}


def choisir_pas(debut: datetime.datetime, fin: datetime.datetime) -> int:
    """Choisit le pas des séries temporelles : une quarantaine de points au plus.

    :param debut: Début de la fenêtre.
    :param fin: Fin de la fenêtre.
    :return: Pas en minutes réelles (= heures projet).
    """
    minutes = max(1.0, (fin - debut).total_seconds() / 60)
    for pas in PAS_POSSIBLES_MIN:
        if minutes / pas <= 40:
            return pas
    return PAS_POSSIBLES_MIN[-1]


def _case(heure: datetime.datetime, debut: datetime.datetime, pas: int) -> int:
    """Indice de la case de la série temporelle qui contient une heure.

    :param heure: Heure réelle.
    :param debut: Début de la série.
    :param pas: Pas en minutes.
    :return: Indice (0 pour la première case).
    """
    return int((heure - debut).total_seconds() // (60 * pas))


def _tournees(evenements: list, debut: datetime.datetime, fin: datetime.datetime) -> list:
    """Regroupe les événements ``DEPART`` et ``TRONCON`` par tournée partie dans la fenêtre.

    :param evenements: Journal du moteur.
    :param debut: Début de la fenêtre.
    :param fin: Fin de la fenêtre.
    :return: Liste de ``{"id", "immat", "h", "remplissage", "lieu", "troncons", "duree_h", "heures_vide"}``.
    """
    tournees = {}
    for evenement in evenements:
        id_tournee = evenement.get("ID_TOURNEE", "")
        if evenement["EVENEMENT"] == "DEPART" and debut <= evenement["H"] <= fin:
            tournees[id_tournee] = {"id": id_tournee, "immat": evenement.get("IMMAT", ""), "h": evenement["H"],
                                    "remplissage": float(evenement.get("REMPLISSAGE") or 0), "lieu": "",
                                    "troncons": [], "duree_h": 0.0, "heures_vide": 0.0}
        elif evenement["EVENEMENT"] == "TRONCON" and id_tournee in tournees:
            tournees[id_tournee]["troncons"].append(evenement)
    for tournee in tournees.values():
        troncons = tournee["troncons"]
        if troncons:
            # Durée de la tournée : du premier départ à la dernière arrivée (minutes réelles = heures projet)
            h_debut = min(lire_heure(t["H_DEBUT"]) for t in troncons)
            h_fin = max(lire_heure(t["H_FIN"]) for t in troncons)
            tournee["duree_h"] = (h_fin - h_debut).total_seconds() / 60
            tournee["heures_vide"] = sum(float(t["DUREE_H"] or 0) for t in troncons if float(t["REMPLISSAGE"] or 0) == 0)
            chargement = [t for t in troncons if float(t["REMPLISSAGE"] or 0) > 0]
            tournee["lieu"] = chargement[0]["LIEU_DEP"] if chargement else troncons[0]["LIEU_DEP"]
    return sorted(tournees.values(), key=lambda t: t["h"])


def _controles(statuts_fenetre: dict, statuts: dict, tournees: list, sources: dict, maintenant: datetime.datetime,
               horloge: str, seuil: float) -> list:
    """Liste les contrôles de traçabilité et de délai que le Contrôle peut faire.

    :param statuts_fenetre: Statuts des demandes reçues dans la fenêtre.
    :param statuts: Statuts de toutes les demandes journalisées.
    :param tournees: Tournées parties dans la fenêtre.
    :param sources: Sources lues (:func:`charger_sources`).
    :param maintenant: Heure réelle de référence.
    :param horloge: ``"reelle"`` (moteur en direct) ou ``"journal"`` (journal figé).
    :param seuil: Seuil de rentabilité (Annexe 2), ou ``None`` s'il n'est pas affiché.
    :return: Liste de ``{"code", "libelle", "n", "ok", "bloquant", "exemples"}``.
    """
    def controle(code: str, libelle: str, ids: list, bloquant: bool = True) -> dict:
        """Met un contrôle en forme.

        :param code: Code court.
        :param libelle: Description.
        :param ids: Identifiants en défaut.
        :param bloquant: ``True`` si un défaut coûte des points à l'audit.
        :return: Contrôle.
        """
        return {"code": code, "libelle": libelle, "n": len(ids), "ok": not ids, "bloquant": bloquant,
                "exemples": sorted(ids)[:15]}

    delai = datetime.timedelta(seconds=DELAI_DECISION_S)
    liste = [
        controle("SANS_DECISION", "Demandes NORTH reçues sans aucune décision au journal (plus de %d s)" % DELAI_DECISION_S,
                 [i for i, s in statuts_fenetre.items() if s["statut"] == "sans_decision" and s["h_reception"]
                  and maintenant - s["h_reception"] > delai]),
        controle("EN_RETARD", "Acquittées, H_DEP passée, toujours pas parties",
                 [i for i, s in statuts_fenetre.items() if s["statut"] == "en_retard"]),
        controle("PARTIE_APRES_HDEP", "Parties après H_DEP (départ contrôlé du détail PARTIE)",
                 [i for i, s in statuts_fenetre.items() if s["statut"] == "partie" and s["depart"] and s["h_dep"]
                  and s["depart"] > s["h_dep"]]),
        controle("ACQ_PUIS_REFUS", "Acquittées puis refusées ou expirées (comptent comme refus)",
                 [i for i, s in statuts_fenetre.items() if s["acq_puis_refus"]]),
        controle("HORS_PERIMETRE", "Demandes hors périmètre NORTH présentes au journal",
                 [i for i, s in statuts.items() if s["hors_perimetre"]]),
        controle("INCONNUE", "Demandes du journal absentes des fichiers du collecteur",
                 [i for i, s in statuts.items() if s["inconnue"]], bloquant=False),
        controle("DECISION_LENTE", "Décision plus de %d s après la réception" % DELAI_DECISION_S,
                 [i for i, s in statuts_fenetre.items() if s["h_decision"] and s["h_reception"]
                  and s["h_decision"] - s["h_reception"] > delai], bloquant=False),
    ]
    if seuil is not None:
        liste.append(controle("SOUS_SEUIL", "Tournées parties sous le seuil de rentabilité (Annexe 2)",
                              [t["id"] for t in tournees if t["remplissage"] < seuil], bloquant=False))
    if horloge == "reelle":
        evenements = sources["evenements"]
        silence = (maintenant - evenements[-1]["H"]).total_seconds() if evenements else None
        liste.append(controle("MOTEUR_SILENCIEUX", "Aucun événement du moteur depuis plus de %d s" % SILENCE_MAX_S,
                              ["dernier événement il y a %s s" % ("∞" if silence is None else int(silence))]
                              if silence is None or silence > SILENCE_MAX_S else []))
        receptions = [d["h_reception"] for d in sources["demandes"].values() if d["h_reception"]]
        silence_c = (maintenant - max(receptions)).total_seconds() if receptions else None
        liste.append(controle("COLLECTEUR_SILENCIEUX", "Aucune demande reçue par le collecteur depuis plus de %d s" % SILENCE_MAX_S,
                              ["dernière réception il y a %s s" % ("∞" if silence_c is None else int(silence_c))]
                              if silence_c is None or silence_c > SILENCE_MAX_S else []))
    return liste


def calculer(sources: dict, maintenant: datetime.datetime, horloge: str, debut: datetime.datetime = None,
             duree_min: float = None, seuil: float = None, cibles: dict = None, taux_horaire: float = None) -> dict:
    """Calcule tous les indicateurs d'audit sur une fenêtre de temps.

    Applique R-KPI-TA (TA = demandes NORTH parties / demandes NORTH reçues
    dans la fenêtre) et R-KPI-TR (TR = moyenne du remplissage des tournées au
    départ du premier lieu de chargement, tournées parties dans la fenêtre ;
    le remplissage est lu dans la colonne ``REMPLISSAGE`` du journal, surface
    au sol des palettes / surface du camion).
    Une demande reçue dans la fenêtre et partie après sa fin compte comme partie.

    :param sources: Sources lues par :func:`charger_sources`.
    :param maintenant: Heure réelle de référence.
    :param horloge: ``"reelle"`` ou ``"journal"`` (affiché, et active les contrôles de silence).
    :param debut: Début de la fenêtre (défaut : premier événement ou première réception).
    :param duree_min: Durée de la fenêtre en minutes réelles (défaut : jusqu'à ``maintenant``).
    :param seuil: Seuil de rentabilité de l'Annexe 2, affiché seulement si donné (donnée confidentielle).
    :param cibles: Cibles du contrat ``{"ta", "tr", "part_haute"}``.
    :param taux_horaire: Coût horaire hors énergie (confidentiel) ; ``None`` : pas d'estimation en euros.
    :return: Rapport JSON-sérialisable (dates en texte).
    """
    cibles = dict(CIBLES_DEFAUT, **(cibles or {}))
    evenements, demandes = sources["evenements"], sources["demandes"]
    premieres = [e["H"] for e in evenements[:1]] + [d["h_reception"] for d in demandes.values() if d["usine"] and d["h_reception"]]
    if debut is None:
        debut = min(premieres) if premieres else maintenant
    fin = debut + datetime.timedelta(minutes=duree_min) if duree_min else maintenant
    fin_mesure = min(fin, maintenant)

    statuts = statuts_demandes(evenements, demandes, maintenant)
    fenetre = {i: s for i, s in statuts.items() if not s["hors_perimetre"] and s["h_reception"]
               and debut <= s["h_reception"] <= fin}
    compte = {k: sum(1 for s in fenetre.values() if s["statut"] == k)
              for k in ("partie", "refusee", "en_attente", "en_retard", "sans_decision")}
    recues = len(fenetre)
    ta = compte["partie"] / recues if recues else None
    ta_decidees = compte["partie"] / (compte["partie"] + compte["refusee"]) if compte["partie"] + compte["refusee"] else None
    # Projection : les acquittées encore dans les temps partiront (hypothèse optimiste, affichée comme telle)
    ta_projete = (compte["partie"] + compte["en_attente"]) / recues if recues else None

    tournees = _tournees(evenements, debut, fin)
    remplissages = [t["remplissage"] for t in tournees]
    tr = statistics.mean(remplissages) if remplissages else None
    troncons = [t for tournee in tournees for t in tournee["troncons"]]
    heures = sum(float(t["DUREE_H"] or 0) for t in troncons)
    tr_troncons = sum(float(t["REMPLISSAGE"] or 0) * float(t["DUREE_H"] or 0) for t in troncons) / heures if heures else None
    heures_vide = sum(t["heures_vide"] for t in tournees)
    heures_camion = sum(t["duree_h"] for t in tournees)
    parties_tournees = sum(1 for e in evenements if e["EVENEMENT"] == "PARTIE" and e.get("ID_TOURNEE") in {t["id"] for t in tournees})

    gammes = {"haute": 0, "mediane": 0, "basse": 0}
    for suivi in fenetre.values():
        if suivi["statut"] == "partie":
            gammes[suivi["gamme"] or "?"] = gammes.get(suivi["gamme"] or "?", 0) + 1
    part_haute = gammes["haute"] / compte["partie"] if compte["partie"] else None

    motifs = {}
    for suivi in fenetre.values():
        if suivi["statut"] == "refusee":
            motifs[suivi["motif"]] = motifs.get(suivi["motif"], 0) + 1

    # Détail par usine et par sens (export / import)
    par_usine = []
    for usine in USINES_NORTH:
        for sens in ("Export", "Import"):
            groupe = [s for s in fenetre.values() if s["usine"] == usine and s["type_mvt"] == sens]
            if not groupe:
                continue
            ligne = {"usine": usine.capitalize(), "sens": sens, "recues": len(groupe)}
            for statut in ("partie", "refusee", "en_attente", "en_retard", "sans_decision"):
                ligne[statut] = sum(1 for s in groupe if s["statut"] == statut)
            ligne["ta"] = ligne["partie"] / len(groupe)
            par_usine.append(ligne)

    # Séries temporelles : par heure de réception (statut final) et par heure de départ (remplissage)
    pas = choisir_pas(debut, fin_mesure if fin_mesure > debut else fin)
    nb_cases = max(1, _case(max(fin_mesure, debut), debut, pas) + 1)
    serie = {"pas_min": pas, "etiquettes": [(debut + datetime.timedelta(minutes=pas * k)).strftime("%H:%M") for k in range(nb_cases)],
             "partie": [0] * nb_cases, "refusee": [0] * nb_cases, "attente": [0] * nb_cases,
             "ta_cumule": [None] * nb_cases, "tr": [None] * nb_cases, "departs": [0] * nb_cases}
    for suivi in fenetre.values():
        k = min(nb_cases - 1, _case(suivi["h_reception"], debut, pas))
        cle = {"partie": "partie", "refusee": "refusee"}.get(suivi["statut"], "attente")
        serie[cle][k] += 1
    cumul_p = cumul_n = 0
    for k in range(nb_cases):
        cumul_p += serie["partie"][k]
        cumul_n += serie["partie"][k] + serie["refusee"][k] + serie["attente"][k]
        serie["ta_cumule"][k] = round(cumul_p / cumul_n, 4) if cumul_n else None
    remplissage_cases = [[] for _ in range(nb_cases)]
    for tournee in tournees:
        k = _case(tournee["h"], debut, pas)
        if 0 <= k < nb_cases:
            remplissage_cases[k].append(tournee["remplissage"])
    serie["tr"] = [round(statistics.mean(v), 4) if v else None for v in remplissage_cases]
    serie["departs"] = [len(v) for v in remplissage_cases]

    histogramme = [0] * 10
    for valeur in remplissages:
        histogramme[min(9, int(valeur * 10))] += 1

    duree_fenetre = (fin - debut).total_seconds() / 60
    derniers = [{"h": e["H"].strftime("%H:%M:%S"), "evenement": e["EVENEMENT"], "demande": e.get("ID_DEMANDE", ""),
                 "tournee": e.get("ID_TOURNEE", ""), "immat": e.get("IMMAT", ""), "de": e.get("LIEU_DEP", ""),
                 "vers": e.get("LIEU_ARR", ""), "remplissage": e.get("REMPLISSAGE", ""), "gamme": e.get("GAMME", ""),
                 "motif": e.get("MOTIF", "")}
                for e in evenements[-25:] if e["EVENEMENT"] != "TRONCON"][::-1]

    return {
        "genere": datetime.datetime.now().strftime(FORMAT_DATE),
        "horloge": horloge, "maintenant": maintenant.strftime(FORMAT_DATE),
        "sources": {"journal": sources["journal"], "evenements": len(evenements),
                    "fichiers_demandes": sources["fichiers_demandes"], "demandes_recues": len(demandes),
                    "dernier_evenement": evenements[-1]["H"].strftime(FORMAT_DATE) if evenements else None,
                    "derniere_reception": max((d["h_reception"] for d in demandes.values() if d["h_reception"]),
                                              default=datetime.datetime.min).strftime(FORMAT_DATE) if demandes else None},
        "fenetre": {"debut": debut.strftime(FORMAT_DATE), "fin": fin.strftime(FORMAT_DATE), "duree_min": duree_fenetre,
                    "ecoule_min": max(0.0, (fin_mesure - debut).total_seconds() / 60),
                    "restant_min": max(0.0, (fin - maintenant).total_seconds() / 60) if duree_min else None},
        "acceptation": {"recues": recues, **compte, "ta": ta, "ta_decidees": ta_decidees, "ta_projete": ta_projete,
                        "objectif": ecart_et_note(ta, cibles["ta"]), "motifs": motifs, "par_usine": par_usine},
        "remplissage": {"tr": tr, "objectif": ecart_et_note(tr, cibles["tr"]), "tr_troncons": tr_troncons,
                        "quantiles": quantiles(remplissages), "histogramme": histogramme, "seuil": seuil,
                        "sous_seuil": sum(1 for v in remplissages if seuil is not None and v < seuil)},
        "gammes": {"comptes": gammes, "part_haute": part_haute, "objectif": ecart_et_note(part_haute, cibles["part_haute"])},
        "flotte": {"tournees": len(tournees), "camions": len({t["immat"] for t in tournees}),
                   "demandes_par_tournee": parties_tournees / len(tournees) if tournees else None,
                   "heures_camion": heures_camion, "heures_vide": heures_vide,
                   "part_heures_vide": heures_vide / heures if heures else None,
                   "heures_camion_par_demande": heures_camion / parties_tournees if parties_tournees else None,
                   "cout_par_demande": heures_camion * taux_horaire / parties_tournees
                   if taux_horaire is not None and parties_tournees else None},
        "series": serie, "controles": _controles(fenetre, statuts, tournees, sources, maintenant, horloge, seuil),
        "derniers_evenements": derniers,
    }


def statuts_csv(sources: dict, maintenant: datetime.datetime) -> str:
    """Exporte le statut final de chaque demande NORTH, pour comparer avec l'audit du Contrôle.

    :param sources: Sources lues par :func:`charger_sources`.
    :param maintenant: Heure réelle de référence.
    :return: Texte CSV (séparateur ``;``).
    """
    lignes = ["ID_DEMANDE;USINE;TYPE_MVT;H_RECEPTION;H_DEP;STATUT;MOTIF;GAMME;H_DECISION;DEPART_CONTROLE"]
    format_h = lambda h: h.strftime(FORMAT_DATE) if h else ""  # noqa: E731
    for id_demande, s in sorted(statuts_demandes(sources["evenements"], sources["demandes"], maintenant).items(),
                                key=lambda element: element[1]["h_reception"] or datetime.datetime.min):
        lignes.append(";".join([id_demande, s["usine"].capitalize(), s["type_mvt"], format_h(s["h_reception"]),
                                format_h(s["h_dep"]), "hors_perimetre" if s["hors_perimetre"] else s["statut"],
                                s["motif"], s["gamme"], format_h(s["h_decision"]), format_h(s["depart"])]))
    return "\n".join(lignes) + "\n"


def charger_cibles(chemin: str = FICHIER_CIBLES) -> dict:
    """Lit les cibles négociées avec le Contrôle.

    Fichier JSON facultatif, par exemple ``{"ta": 0.85, "tr": 0.75, "part_haute": 0.3}``
    (parts entre 0 et 1). Sans fichier, :data:`CIBLES_DEFAUT`.

    :param chemin: Chemin du fichier.
    :return: Cibles.
    """
    cibles = dict(CIBLES_DEFAUT)
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as fichier:
            cibles.update({k: v for k, v in json.load(fichier).items() if k in CIBLES_DEFAUT})
    return cibles
