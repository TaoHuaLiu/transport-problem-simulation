"""Hypothèses et paramètres du moteur d'acceptation NORTH.

Toutes les valeurs que le sujet ne fixe pas sont réunies ici. Chacune renvoie
à une question ouverte de la page d'architecture (identifiants A1 à E18) ou à
une règle du catalogue (``3 - Documentation/regles-acceptation.docx``) : quand
la réponse arrive, on ne modifie que ce fichier.

Unités : sauf mention contraire, les durées sont en **heures projet**
(1 heure projet = 1 minute réelle).

Les données confidentielles de l'Annexe 2 (taux horaires, seuil de
rentabilité) ne sont pas écrites ici : elles sont lues dans
:data:`FICHIER_CONFIDENTIEL`, hors du code partagé.
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

#: Une clé SPEC inconnue (autre que PTAC, ENR, COL) ne peut pas être vérifiée :
#: ``True`` la traite comme non respectée (refus REF-SPEC).
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

#: C13 : une tournée peut-elle charger dans deux lieux différents ?
REGROUPER_ENTRE_LIEUX_DEP = False

# ---------------------------------------------------------------- Gammes (methode-contrat.md §3)
#: (gamme, arrêts intermédiaires max, allongement max), de la plus haute à la plus basse.
GAMMES = [("haute", 0, 1.1), ("mediane", 1, 1.5), ("basse", 3, 2.5)]

# ---------------------------------------------------------------- Politique (D15, D16, E17)
#: W : marge gardée avant le départ au plus tard d'une tournée (D-DEPART).
MARGE_DEPART_H = 1.0

#: θ par défaut, si le fichier confidentiel est absent. À régler par rejeu.
SEUIL_DEPART_DEFAUT = 0.5

#: D-DEPART ligne 3 : sous le seuil à l'heure limite, ``"partir"`` ou ``"refuser"``.
SOUS_SEUIL = "partir"

#: E17 : durée d'un tick du moteur, en secondes réelles.
TICK_S = 5

# ---------------------------------------------------------------- Coûts (B8)
#: Prix de l'énergie par unité de ``TRUCKS_CONSUMPTION`` (L, kg ou kWh), en euros.
#: Valeurs de repli : l'API carburants n'est pas joignable depuis la VM.
PRIX_ENERGIE = {"Diesel": 1.70, "Essence": 1.85, "GPL": 1.00, "Electrique": 0.25}

#: Colonne de ``TRUCKS_CONSUMPTION`` pour chaque valeur du champ ``ENERGIE`` des fiches.
COLONNE_CONSOMMATION = {"Diesel": "DIESEL (L)", "Essence": "GASOLINE (L)",
                        "GPL": "LPG (kg)", "Electrique": "ELECTRIC (kWh)"}

#: Fichier des données confidentielles (Annexe 2), hors du dossier de code.
FICHIER_CONFIDENTIEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "donnees", "annexe2_confidentiel.json")


def charger_confidentiel(chemin: str = FICHIER_CONFIDENTIEL) -> dict:
    """Lit les taux horaires et le seuil de rentabilité de l'Annexe 2.

    Le fichier attendu contient les clés ``taux_conducteur_h``,
    ``taux_vehicule_h`` (euros par heure projet) et ``seuil_rentabilite``
    (part du plancher, entre 0 et 1). S'il est absent, les coûts sont
    calculés avec un taux horaire total de 1 (0,5 + 0,5 : seul le classement
    des plans compte)
    et le seuil vaut :data:`SEUIL_DEPART_DEFAUT`.

    :param chemin: Chemin du fichier JSON.
    :return: Dictionnaire avec les trois clés, et ``source`` (chemin ou ``"defaut"``).
    """
    valeurs = {"taux_conducteur_h": 0.5, "taux_vehicule_h": 0.5,
               "seuil_rentabilite": SEUIL_DEPART_DEFAUT, "source": "defaut"}
    if os.path.exists(chemin):
        with open(chemin, encoding="utf-8") as fichier:
            valeurs.update(json.load(fichier))
        valeurs["source"] = chemin
    return valeurs
