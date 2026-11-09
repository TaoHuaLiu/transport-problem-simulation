"""Référentiels du moteur NORTH : lieux, flotte, capacités, consommations, trajets, ports.

Les données viennent de la base ``UE_ENS_DCJC`` (lecture seule, requêtes
SELECT uniquement) ou d'un fichier JSON qui en est la copie. Le JSON permet
de rejouer le moteur hors du VLAN de l'ENSIBS.

Structure en mémoire (un dictionnaire ``ref``) :

- ``lieux`` : ``{PK_LOCATIONS: {"NAME", "COUNTRY"}}`` ;
- ``index`` : ``{nom normalisé: PK_LOCATIONS}`` ;
- ``affectations`` : liste ``(FK_LOCATIONS, REF_L, REF_H)`` ;
- ``capacite`` : ``{PTAC: {"EPAL", "EPAL (half)", "ISO"}}`` ;
- ``consommation`` : ``{PTAC: {colonne: valeur aux 100 km}}`` ;
- ``trajets`` : ``{(a, b): heures projet}`` avec ``a <= b`` ;
- ``ports`` : ``{PK_LOCATIONS: [T0, T+4, T+8]}`` ;
- ``source`` : origine des données (``"bdd"``, chemin du JSON…).

Utilisation :

.. code-block:: bash

    python3 referentiels.py --export     # sur la VM : copie la base dans ../donnees/referentiels.json
    python3 referentiels.py --exemple    # en local : référentiel d'exemple (valeurs du sujet + estimations)
"""

import argparse
import datetime
import json
import math
import os
import unicodedata

import hypotheses

#: Paramètres de connexion à la base du projet.
BDD = {"host": "192.168.40.117", "port": 5432, "dbname": "UE_ENS_DCJC", "user": "dcjc", "password": "dcjc"}

#: Fichier JSON par défaut (copie de la base), hors du dossier de code.
FICHIER_REFERENTIELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "donnees", "referentiels.json")

#: Colonne de ``TRUCKS_CAPACITY`` pour chaque ``TYPE_PAL`` des demandes.
COLONNE_CAPACITE = {"EPAL": "EPAL", "EPALh": "EPAL (half)", "ISO": "ISO"}


def normaliser(nom: str) -> str:
    """Normalise un nom de ville pour la jointure avec ``LOCATIONS_NAMES``.

    Retire les accents, les espaces en trop et la casse (la base écrit
    « athenes » en minuscules, l'ERP « Athenes »).

    >>> normaliser(" Düsseldorf ")
    'dusseldorf'

    :param nom: Nom de ville tel que reçu.
    :return: Nom sans accents, en minuscules.
    """
    sans_accents = unicodedata.normalize("NFKD", str(nom)).encode("ascii", "ignore").decode("ascii")
    return sans_accents.strip().lower()


def construire_index(ref: dict) -> None:
    """Ajoute à ``ref`` l'index ``{nom normalisé: PK_LOCATIONS}``.

    :param ref: Référentiel, modifié sur place.
    """
    ref["index"] = {normaliser(lieu["NAME"]): pk for pk, lieu in ref["lieux"].items() if lieu["NAME"] not in ("", "-")}


def pk_lieu(ref: dict, nom: str):
    """Donne la clé ``PK_LOCATIONS`` d'une ville.

    Applique la règle R-VAL-01 (ville connue de ``LOCATIONS_NAMES``).

    :param ref: Référentiel.
    :type ref: dict
    :param nom: Nom de ville, avec ou sans accents.
    :type nom: str
    :return: Clé du lieu, ou ``None`` si la ville est inconnue.
    :rtype: int or None
    """
    return ref["index"].get(normaliser(nom))


def duree(ref: dict, a: int, b: int):
    """Durée de trajet entre deux lieux, en heures projet.

    ``TRAVEL_TIME_MATRIX`` est triangulaire supérieure : on lit la ligne
    ``min(a, b)`` et la colonne ``max(a, b)``. Un trajet absent vaut
    :data:`hypotheses.DUREE_TRAJET_INCONNU_H` (question B7).

    :param ref: Référentiel.
    :type ref: dict
    :param a: Lieu de départ.
    :type a: int
    :param b: Lieu d'arrivée.
    :type b: int
    :return: Durée en heures projet, ou ``None`` si le trajet est inconnu.
    :rtype: float or None
    """
    valeur = ref["trajets"].get((min(a, b), max(a, b)))
    return hypotheses.DUREE_TRAJET_INCONNU_H if valeur is None else valeur


def attente_port_h(ref: dict, pk: int, delai_h: float) -> float:
    """Attente avant chargement ou déchargement dans un port, en heures projet.

    Applique la règle R-DEL-01. ``PORTS_AVAILABILITY`` donne l'attente
    actuelle (T0) et prévue à 4 h et 8 h projet : on prend la colonne la plus
    proche de l'heure d'arrivée. Un lieu absent de la table (usine, GMS)
    n'a pas d'attente. L'unité des valeurs est fixée par
    :data:`hypotheses.UNITE_ATTENTE_PORT` (question A4).

    :param ref: Référentiel.
    :param pk: Lieu d'arrivée.
    :param delai_h: Délai entre maintenant et l'arrivée, en heures projet.
    :return: Attente en heures projet.
    """
    valeurs = ref["ports"].get(pk)
    if not valeurs:
        return 0.0
    indice = 0 if delai_h < 2 else (1 if delai_h < 6 else 2)
    # Colonnes T+4 / T+8 éventuellement vides : on retombe sur la plus proche connue
    connues = [v for v in valeurs[:indice + 1] if v is not None]
    minutes = connues[-1] if connues else 0.0
    # 1 heure projet = 60 minutes projet = 1 minute réelle
    return minutes / 60 if hypotheses.UNITE_ATTENTE_PORT == "minutes_projet" else minutes


def capacite(ref: dict, ptac: str, type_pal: str):
    """Nombre maximal de palettes d'un type pour un PTAC (``TRUCKS_CAPACITY``).

    :param ref: Référentiel.
    :type ref: dict
    :param ptac: PTAC du camion, par exemple ``"19t"``.
    :type ptac: str
    :param type_pal: ``EPAL``, ``EPALh`` ou ``ISO``.
    :type type_pal: str
    :return: Capacité, ou ``None`` si le PTAC ou le type est inconnu.
    :rtype: int or None
    """
    ligne = ref["capacite"].get(ptac)
    colonne = COLONNE_CAPACITE.get(type_pal)
    if ligne is None or colonne is None:
        return None
    return ligne.get(colonne)


def usine_attache(ref: dict, reference: int):
    """Usine d'attache d'un camion d'après sa référence (``ASSIGNED_TRUCKS``).

    :param ref: Référentiel.
    :type ref: dict
    :param reference: Champ ``REFERENCE`` de la fiche camion.
    :type reference: int
    :return: Clé du lieu, ou ``None`` si aucune plage ne contient la référence.
    :rtype: int or None
    """
    for fk, ref_l, ref_h in ref["affectations"]:
        if ref_l <= reference <= ref_h:
            return fk
    return None


def lire_table(curseur, table: str) -> list:
    """Lit toute une table de la base (SELECT uniquement).

    :param curseur: Curseur psycopg2 ouvert.
    :type curseur: psycopg2.extensions.cursor
    :param table: Nom de la table, sans guillemets.
    :return: Liste de dictionnaires ``{colonne: valeur}``.
    """
    curseur.execute('SELECT * FROM "%s"' % table)
    colonnes = [description[0] for description in curseur.description]
    return [dict(zip(colonnes, ligne)) for ligne in curseur.fetchall()]


def charger_depuis_bdd(parametres: dict = None) -> dict:
    """Lit les six tables du projet dans la base ``UE_ENS_DCJC``.

    Les tables fournies sont en lecture seule : cette fonction ne fait que
    des SELECT. À rappeler toutes les heures projet pour ``TRAVEL_TIME_MATRIX``
    et ``PORTS_AVAILABILITY``.

    :param parametres: Paramètres de connexion (défaut : :data:`BDD`).
    :return: Référentiel.
    """
    import psycopg2  # disponible sur la VM uniquement

    connexion = psycopg2.connect(**(parametres or BDD))
    try:
        curseur = connexion.cursor()
        ref = {"lieux": {}, "affectations": [], "capacite": {}, "consommation": {}, "trajets": {}, "ports": {}}
        for ligne in lire_table(curseur, "LOCATIONS_NAMES"):
            ref["lieux"][ligne["PK_LOCATIONS"]] = {"NAME": ligne["NAME"], "COUNTRY": ligne["COUNTRY"]}
        for ligne in lire_table(curseur, "ASSIGNED_TRUCKS"):
            ref["affectations"].append((ligne["FK_LOCATIONS"], ligne["REF_L"], ligne["REF_H"]))
        for ligne in lire_table(curseur, "TRUCKS_CAPACITY"):
            ref["capacite"][ligne.pop("PTAC")] = ligne
        for ligne in lire_table(curseur, "TRUCKS_CONSUMPTION"):
            ref["consommation"][ligne.pop("PTAC")] = ligne
        for ligne in lire_table(curseur, "TRAVEL_TIME_MATRIX"):
            a = ligne.pop("FK_LOCATIONS")
            for colonne, valeur in ligne.items():
                if valeur is not None:
                    ref["trajets"][(min(a, int(colonne)), max(a, int(colonne)))] = float(valeur)
        for ligne in lire_table(curseur, "PORTS_AVAILABILITY"):
            ref["ports"][ligne["FK_LOCATIONS"]] = [ligne["AVAILABILITY_T0"], ligne["AVAILABILITY_T+4"], ligne["AVAILABILITY_T+8"]]
    finally:
        connexion.close()
    ref["source"] = "bdd"
    ref["lu_a"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    construire_index(ref)
    return ref


def vers_json(ref: dict) -> dict:
    """Met le référentiel sous une forme sérialisable en JSON (clés en texte).

    :param ref: Référentiel.
    :return: Dictionnaire sérialisable.
    """
    return {"source": ref.get("source", ""), "lu_a": ref.get("lu_a", ""), "remarques": ref.get("remarques", []),
            "lieux": {str(pk): lieu for pk, lieu in ref["lieux"].items()},
            "affectations": [list(plage) for plage in ref["affectations"]],
            "capacite": ref["capacite"], "consommation": ref["consommation"],
            "trajets": [[a, b, h] for (a, b), h in sorted(ref["trajets"].items())],
            "ports": {str(pk): valeurs for pk, valeurs in ref["ports"].items()}}


def charger_depuis_json(chemin: str) -> dict:
    """Relit un référentiel écrit par :func:`sauver_json`.

    :param chemin: Chemin du fichier JSON.
    :return: Référentiel.
    """
    with open(chemin, encoding="utf-8") as fichier:
        brut = json.load(fichier)
    ref = {"lieux": {int(pk): lieu for pk, lieu in brut["lieux"].items()},
           "affectations": [tuple(plage) for plage in brut["affectations"]],
           "capacite": brut["capacite"], "consommation": brut["consommation"],
           "trajets": {(min(a, b), max(a, b)): h for a, b, h in brut["trajets"]},
           "ports": {int(pk): valeurs for pk, valeurs in brut["ports"].items()},
           "source": chemin, "lu_a": brut.get("lu_a", ""), "remarques": brut.get("remarques", [])}
    construire_index(ref)
    return ref


def sauver_json(ref: dict, chemin: str) -> None:
    """Écrit le référentiel en JSON.

    :param ref: Référentiel.
    :param chemin: Fichier de sortie (le dossier est créé si besoin).
    """
    os.makedirs(os.path.dirname(os.path.abspath(chemin)), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as fichier:
        json.dump(vers_json(ref), fichier, ensure_ascii=False, indent=1)


def generer_exemple() -> dict:
    """Construit un référentiel d'exemple pour travailler hors du VLAN.

    Reprend les valeurs données en exemple dans le sujet (p. 15 à 17) et
    **estime** les autres : trajets d'après la distance à vol d'oiseau
    (0,5 h + 0,0185 h/km, calé sur les exemples du sujet), capacités 19 t et
    32 t interpolées, attente de 30 min dans les ports sans exemple, plages
    ``ASSIGNED_TRUCKS`` fictives pour 10 des 12 usines. À remplacer par
    ``--export`` dès qu'on a accès à la VM (question B6).

    :return: Référentiel d'exemple, avec la liste des approximations dans ``remarques``.
    """
    # (nom, pays, latitude, longitude) ; clés et noms de LOCATIONS_NAMES
    villes = {1: ("Amsterdam", "Pays-Bas", 52.37, 4.90), 2: ("Anvers", "Belgique", 51.22, 4.40),
              3: ("athenes", "Grèce", 37.98, 23.73), 4: ("Brest", "France", 48.39, -4.49),
              5: ("Bucarest", "Roumanie", 44.43, 26.10), 6: ("Dusseldorf", "Allemagne", 51.23, 6.77),
              7: ("Florence", "Italie", 43.77, 11.26), 8: ("Le Havre", "France", 49.49, 0.11),
              9: ("Lyon", "France", 45.76, 4.84), 10: ("Manchester", "Angleterre", 53.48, -2.24),
              11: ("Marseille", "France", 43.30, 5.37), 12: ("Munich", "Allemagne", 48.14, 11.58),
              13: ("Paris", "France", 48.86, 2.35), 14: ("Rotterdam", "Pays-Bas", 51.92, 4.48),
              15: ("Seville", "Espagne", 37.39, -5.98), 16: ("Valence", "Espagne", 39.47, -0.38),
              17: ("Vienne", "Autriche", 48.21, 16.37), 18: ("Zagreb", "Croatie", 45.81, 15.98)}
    ref = {"lieux": {0: {"NAME": "-", "COUNTRY": "-"}}, "trajets": {}, "source": "exemple",
           "lu_a": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    for pk, (nom, pays, _, _) in villes.items():
        ref["lieux"][pk] = {"NAME": nom, "COUNTRY": pays}

    # Trajets estimés (distance du grand cercle), puis valeurs exactes du sujet p. 16
    for a, (_, _, lat_a, lon_a) in villes.items():
        for b, (_, _, lat_b, lon_b) in villes.items():
            if a <= b:
                p1, p2 = math.radians(lat_a), math.radians(lat_b)
                dp, dl = p2 - p1, math.radians(lon_b - lon_a)
                km = 2 * 6371 * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))
                ref["trajets"][(a, b)] = 1.0 if a == b else round(0.5 + 0.0185 * km, 2)
    ref["trajets"].update({(1, 1): 1.1, (1, 2): 3.29, (1, 3): 38.67, (1, 17): 18.6, (1, 18): 17.09,
                           (3, 3): 0.7, (3, 17): 16.51, (3, 18): 18.99, (10, 17): 25.75, (10, 18): 27.22})

    # Capacités : 12 t, 26 t, 44 t du sujet p. 15 ; 19 t et 32 t interpolées
    ref["capacite"] = {"12t": {"EPAL": 18, "EPAL (half)": 33, "ISO": 14}, "19t": {"EPAL": 20, "EPAL (half)": 37, "ISO": 15},
                       "26t": {"EPAL": 22, "EPAL (half)": 41, "ISO": 17}, "32t": {"EPAL": 26, "EPAL (half)": 48, "ISO": 20},
                       "44t": {"EPAL": 33, "EPAL (half)": 63, "ISO": 26}}
    # Consommations : table complète relevée sur la VM (3 - Contractualisation/consumption.md)
    colonnes = ["DIESEL (L)", "GASOLINE (L)", "LPG (kg)", "ELECTRIC (kWh)", "HYDROGEN (kg)"]
    lignes = {"12t": [22.8, 27.4, 23.1, 121.6, 7.3], "19t": [25.2, 30.2, 24.6, 130.2, 8.5], "26t": [29.2, 34.2, 26, 142.4, 9],
              "32t": [32.5, 37.4, 27.3, 160.7, 9.8], "44t": [37.9, 43.6, 29.7, 171.9, 10.1]}
    ref["consommation"] = {ptac: dict(zip(colonnes, valeurs)) for ptac, valeurs in lignes.items()}
    # Ports : Le Havre, Rotterdam, Valence du sujet p. 17 ; 30 min ailleurs
    ref["ports"] = {8: [53.4, 52.3, 51.2], 14: [12.7, 17.8, 46.5], 16: [47.4, 48.9, 45.5],
                    1: [30.0, 30.0, 30.0], 2: [30.0, 30.0, 30.0], 3: [30.0, 30.0, 30.0], 11: [30.0, 30.0, 30.0]}

    # ASSIGNED_TRUCKS : Florence (7) et Munich (12) du sujet p. 15 ; les 10 autres usines
    # (Lyon en compte deux) se partagent le reste de [100000, 999999] à parts égales
    ref["affectations"] = [(7, 732192, 797142), (12, 569815, 732191)]
    usines = [1, 4, 5, 6, 9, 9, 10, 15, 16, 18]
    libres = [(100000, 569814), (797143, 999999)]
    part = sum(h - l + 1 for l, h in libres) // len(usines)
    i, courant = 0, libres[0][0]
    for n, fk in enumerate(usines):
        # La dernière usine prend tout le reste ; une part peut chevaucher les deux intervalles
        besoin = part if n < len(usines) - 1 else math.inf
        while besoin > 0 and i < len(libres):
            fin = int(min(libres[i][1], courant + besoin - 1))
            ref["affectations"].append((fk, courant, fin))
            besoin -= fin - courant + 1
            if fin == libres[i][1]:
                i += 1
                courant = libres[i][0] if i < len(libres) else courant
            else:
                courant = fin + 1
    ref["remarques"] = ["Référentiel d'EXEMPLE : trajets estimés sauf 10 valeurs du sujet p. 16",
                        "Capacités 19t et 32t interpolées ; 12t, 26t, 44t du sujet p. 15",
                        "Attente portuaire 30 min (minutes projet) sauf Le Havre, Rotterdam, Valence (sujet p. 17)",
                        "Plages ASSIGNED_TRUCKS fictives sauf Florence et Munich (sujet p. 15)"]
    construire_index(ref)
    return ref


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Copie ou génère le référentiel du moteur NORTH")
    groupe = parseur.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--export", action="store_true", help="lit la base UE_ENS_DCJC (VM) et l'écrit en JSON")
    groupe.add_argument("--exemple", action="store_true", help="écrit un référentiel d'exemple (hors VLAN)")
    parseur.add_argument("--sortie", default=None, help="fichier JSON produit")
    arguments = parseur.parse_args()
    if arguments.export:
        referentiel = charger_depuis_bdd()
        sortie = arguments.sortie or FICHIER_REFERENTIELS
    else:
        referentiel = generer_exemple()
        sortie = arguments.sortie or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_build", "referentiels_exemple.json")
    sauver_json(referentiel, sortie)
    print("Référentiel (%s) écrit dans %s" % (referentiel["source"], os.path.abspath(sortie)))
