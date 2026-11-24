"""Hypothèses et paramètres du moteur d'acceptation NORTH.

Toutes les valeurs que le sujet ne fixe pas sont réunies ici. Chacune renvoie
à une question ouverte de la page d'architecture (identifiants A1 à E18) ou à
une règle du catalogue (``3 - Documentation/processus.md``, §2.5) : quand
la réponse arrive, on ne modifie que ce fichier.

Unités : sauf mention contraire, les durées sont en **heures projet**
(1 heure projet = 1 minute réelle).

Les données confidentielles de l'Annexe 2 (taux horaires, seuil de
rentabilité) ne sont pas écrites ici : elles sont lues dans
:data:`FICHIER_CONFIDENTIEL` (``../donnees/`` ou ``donnees/``).
"""

import json
import os

#: Usines de la structure NORTH (noms sans accents, comme ``LOCATIONS_NAMES``).
USINES_NORTH = ["Amsterdam", "Dusseldorf", "Manchester"]

# ---------------------------------------------------------------- Périmètre
#: A3 : les imports port → usine NORTH sont à nous (règle R-PER-02).
IMPORTS_NORTH = True

# ---------------------------------------------------------------- SPEC et frigo
#: R-SPEC-01 : une SPEC non respectée entraîne un refus (``True``) ou seulement
#: une gamme plus basse (``False``). Précision du 2026-10-21 : gamme plus basse.
SPEC_BLOQUANTE = False

#: R-SPEC-02 : gamme d'une demande dont la SPEC n'est pas tenue. ``"un_cran"`` :
#: un cran sous la gamme due au trajet (haute → médiane → basse) ; ``"basse"`` :
#: toujours basse. Le sujet (p. 21) réserve la haute aux demandes dont « toutes
#: les conditions sont remplies » sans dire jusqu'où descendre.
DECLASSEMENT_SPEC = "un_cran"

#: A5 : ``PTAC=12t`` exige exactement 12 t (``False``) ou au moins 12 t (``True``).
PTAC_SPEC_MINIMUM = False

#: R-SPEC-03 : un camion de PTAC supérieur au PTAC demandé dans la SPEC n'est jamais
#: utilisé, même si la SPEC est souple (contrat §4.2 : « pour un PTAC dépassé, c'est
#: éliminatoire »). Un camion plus léger reste possible, gamme abaissée (R-SPEC-02).
PTAC_DEPASSE_INTERDIT = True

#: Une clé SPEC inconnue (autre que PTAC, ENR, COL) ne peut pas être vérifiée :
#: ``True`` la traite comme non respectée (gamme abaissée, R-SPEC-02 ; refus REF-SPEC
#: seulement si :data:`SPEC_BLOQUANTE`).
CLES_SPEC_INCONNUES_BLOQUANTES = True

#: R-FRA-02 : produits secs dans un camion frigorifique.
#: ``"interdit"``, ``"dernier_recours"`` (pénalité de coût) ou ``"autorise"``.
#: Décision d'équipe du 2026-10-21 : interdit.
SEC_EN_FRIGO = "interdit"

#: Pénalité ajoutée au coût d'un plan qui met du sec en frigo (mode ``dernier_recours``).
PENALITE_SEC_EN_FRIGO = 1000.0

# ---------------------------------------------------------------- Temps
#: Annexe 2 (partie publique) et C12 : chargement ou déchargement, à chaque arrêt.
DUREE_CHARGEMENT_H = 1 + 47 / 60

#: A4 : unité de ``PORTS_AVAILABILITY``. Le sujet (p. 17) parle de « 46.5 min »
#: d'attente ; on suppose des minutes **projet**. ``"minutes_reelles"`` sinon.
UNITE_ATTENTE_PORT = "minutes_projet"

#: B7 : durée d'un trajet absent de ``TRAVEL_TIME_MATRIX`` ; ``None`` = trajet impossible.
DUREE_TRAJET_INCONNU_H = None

#: Annexe 1 (R-CON-01) : conduite continue maximale, pause, conduite par jour, repos quotidien.
CONDUITE_CONTINUE_MAX_H = 4.5
PAUSE_H = 0.75
CONDUITE_JOUR_MAX_H = 12.0
REPOS_QUOTIDIEN_H = 11.0

#: Annexe 2 (partie publique) : vitesses moyennes, en km/h, pour estimer les distances.
VITESSE_TRAJET_COURT_KMH = 66.5   # trajets < 5 h
VITESSE_TRAJET_LONG_KMH = 74.9    # trajets > 7 h

# ---------------------------------------------------------------- Modèle du monde
#: C10 / R-OPT-05 : retour à vide à l'usine d'attache après la dernière livraison.
#: Précision du 2026-10-21 : le retour n'est pas à gérer (``False``) ; la tournée
#: s'arrête à la dernière livraison et le camion reste libre à cet endroit.
#: (C9, au démarrage chaque camion est libre à son usine : :func:`flotte.charger_flotte`.)
RETOUR_A_L_USINE = False

#: C13 : une tournée peut-elle charger dans deux lieux différents ? Oui depuis le 2026-11-16 :
#: tous les chargements, puis toutes les livraisons (:func:`planification.ordonner_arrets`).
#: Le TR reste mesuré au départ du premier lieu de chargement (R-KPI-TR).
REGROUPER_ENTRE_LIEUX_DEP = True

# ---------------------------------------------------------------- Taux de remplissage (R-KPI-TR)
#: Surface au sol de chaque type de palette, en m² (README « Calcul des indicateurs »).
#: Le sujet ne donne pas les dimensions : EPAL 1,20 × 0,80 et ISO 1,20 × 1,00 sont
#: les normes ; EPALh est supposée être une demi-palette 0,80 × 0,60 (à faire valider).
#: Surface d'un camion = max sur les types (capacité × surface), soit capacité EPAL × 0,96.
SURFACE_PALETTE_M2 = {"EPAL": 0.96, "EPALh": 0.48, "ISO": 1.20}

# ---------------------------------------------------------------- Gammes (methode-contrat.md §3)
#: (gamme, arrêts intermédiaires max, allongement max), de la plus haute à la plus basse.
GAMMES = [("haute", 0, 1.1), ("mediane", 1, 1.5), ("basse", 3, 2.5)]

#: Coefficient de prix de chaque gamme (prix facturé, ``methode-contrat.md`` §3) : valeurs de départ, à négocier.
COEF_PRIX_GAMME = {"haute": 1.15, "mediane": 1.00, "basse": 0.85}

# ---------------------------------------------------------------- Politique (D15, D16, E17)
#: W : marge gardée avant le départ au plus tard d'une tournée (D-DEPART), heures projet.
#: Valeur retenue par le balayage θ × W du 2026-11-16 (``_build/rejeu-c13/``) : 0,25 h projet
#: = 15 s réelles = 3 ticks ; 0,1 h fait à peine mieux en rejeu mais ne laisse qu'un tick sur la VM.
MARGE_DEPART_H = 0.25

#: θ : seuil de départ de la règle R-RENT-01 (taux de remplissage, R-KPI-TR), sauf option ``--seuil`` du moteur.
#: Valeur retenue par le balayage θ × W du 2026-11-16 ; ne dépend plus du seuil de rentabilité.
SEUIL_DEPART_DEFAUT = 0.85

#: Remplissage comparé à θ : ``"premier_depart"`` (celui du KPI, au départ du premier lieu
#: de chargement) ou ``"maximum"`` (le plus haut sur la tournée, après le dernier chargement).
SEUIL_MESURE = "premier_depart"

#: Seuil de rentabilité utilisé pour le prix si le fichier confidentiel est absent.
SEUIL_RENTABILITE_DEFAUT = 0.5

#: D-DEPART ligne 3 : sous le seuil à l'heure limite, ``"partir"`` ou ``"refuser"`` (motif REF-RENTABILITE).
SOUS_SEUIL = "partir"

#: E17 : durée d'un tick du moteur, en secondes réelles.
TICK_S = 5

# ---------------------------------------------------------------- Tables de traçabilité du Contrôle (A1)
#: Spécification du Contrôle : ``TRACABILITE_COMMANDE.ID_ENTRE`` vaut ``Xx``, X = première lettre
#: de la zone (N, S, E, O), x = numéro de la commande dans l'entreprise (1, 2, 3…). Notre lettre :
PREFIXE_ENTREPRISE = "N"

#: Motif (expression régulière PostgreSQL et Python) de nos ``ID_ENTRE`` : toutes nos mises à jour
#: et lectures dans ``TRACABILITE_COMMANDE`` sont limitées aux lignes qui le respectent.
MOTIF_ID_ENTRE = "^" + PREFIXE_ENTREPRISE + "[0-9]+$"

#: ``TRACABILITE_TRAJET.TYPE_CAMI`` selon que la remorque est réfrigérée (varchar(20)) : « frais ou sec »
#: dans la spécification du Contrôle, écrit en capitales comme ``STATUT`` (décision d'équipe du 24/11/2026).
TYPES_CAMION = {True: "FRAIS", False: "SEC"}

#: ``TRACABILITE_TRAJET.TAUX_REMPLI`` en pour cent (0-100, une décimale, comme ``LOG_NORTH_TRAJET.TAUX_REMPL``),
#: format confirmé par l'équipe le 24/11/2026 ;
#: ``False`` : en fraction (0-1).
TAUX_REMPLI_EN_POURCENT = True

# ---------------------------------------------------------------- Coûts (B8)
#: Prix de l'énergie par unité de ``TRUCKS_CONSUMPTION`` (L, kg ou kWh), en euros.
#: Valeurs de repli : l'API carburants n'est pas joignable depuis la VM.
PRIX_ENERGIE = {"Diesel": 1.70, "Essence": 1.85, "GPL": 1.00, "Electrique": 0.25}

#: Colonne de ``TRUCKS_CONSUMPTION`` pour chaque valeur du champ ``ENERGIE`` des fiches.
COLONNE_CONSOMMATION = {"Diesel": "DIESEL (L)", "Essence": "GASOLINE (L)",
                        "GPL": "LPG (kg)", "Electrique": "ELECTRIC (kWh)"}

#: Dossier du code (``1 - DEV/`` en local, ``nord/DEV/`` sur la VM, racine du dépôt GitHub).
DOSSIER_CODE = os.path.dirname(os.path.abspath(__file__))


def chemin_donnees_contrat(nom: str, dossier_code: str = DOSSIER_CODE) -> str:
    """Chemin d'un fichier de données du contrat (Annexe 2, cibles), selon la disposition.

    Le paquet VM et le dépôt local rangent ces fichiers dans ``../donnees/``
    (à côté du dossier de code) ; le miroir GitHub les range dans
    ``donnees/`` (dans le dossier de code). On prend ``../donnees/<nom>`` s'il
    existe, sinon ``donnees/<nom>`` s'il existe, sinon ``../donnees/<nom>``
    (chemin attendu, cité dans les messages d'absence).
    Applique la règle R-ROB-05 (préalable de l'audit : Annexe 2 trouvée dans
    les deux dispositions).

    :param nom: Nom du fichier, par exemple ``annexe2_confidentiel.json``.
    :param dossier_code: Dossier du code (par défaut celui de ce module).
    :return: Chemin du fichier retenu.
    """
    voisin = os.path.join(dossier_code, "..", "donnees", nom)
    interne = os.path.join(dossier_code, "donnees", nom)
    if not os.path.exists(voisin) and os.path.exists(interne):
        return interne
    return voisin


#: Fichier des données de l'Annexe 2 (voir :func:`chemin_donnees_contrat`).
FICHIER_CONFIDENTIEL = chemin_donnees_contrat("annexe2_confidentiel.json")


def charger_confidentiel(chemin: str = FICHIER_CONFIDENTIEL) -> dict:
    """Lit les taux horaires et le seuil de rentabilité de l'Annexe 2.

    Le fichier attendu contient les clés ``taux_conducteur_h``,
    ``taux_vehicule_h`` (euros par heure projet) et ``seuil_rentabilite``
    (taux de remplissage R-KPI-TR, entre 0 et 1). S'il est absent, les coûts sont
    calculés avec un taux horaire total de 1 (0,5 + 0,5 : seul le classement
    des plans compte)
    et le seuil vaut :data:`SEUIL_RENTABILITE_DEFAUT`.

    :param chemin: Chemin du fichier JSON.
    :return: Dictionnaire avec les trois clés, et ``source`` (chemin ou ``"defaut"``).
    """
    valeurs = {"taux_conducteur_h": 0.5, "taux_vehicule_h": 0.5,
               "seuil_rentabilite": SEUIL_RENTABILITE_DEFAUT, "source": "defaut"}
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as fichier:
            valeurs.update(json.load(fichier))
        valeurs["source"] = chemin
    return valeurs
