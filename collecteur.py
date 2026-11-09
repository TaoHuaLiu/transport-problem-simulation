"""Collecte des demandes de transport envoyées par l'ERP de Garden Food.

L'ERP est un serveur UDP qui émet en broadcast sur le port 40040. Il ne stocke
rien et ne renvoie pas les messages manqués : ce programme sauvegarde donc
chaque message dès sa réception.

Deux fichiers sont écrits par jour *réel* dans le répertoire de sortie :

- ``demandes_AAAA-MM-JJ.jsonl`` : journal brut, une ligne par message reçu
  (copie fidèle, même si le message est illisible) ;
- ``demandes_AAAA-MM-JJ.csv`` : une ligne par demande de transport, champs
  tels qu'envoyés, plus ``H_DEP_ISO`` et ``SPEC`` découpé en colonnes
  (séparateur ``;``, comme les fiches camion).

Inspiré de ``../RSCR-20261017/RSCR_ERP_CLIENT_UDP.py``.

Utilisation :

.. code-block:: bash

    python3 collecteur.py [--port 40040] [--sortie <répertoire>]

.. seealso:: :mod:`simulateur_erp` pour tester hors du VLAN de l'ENSIBS.
"""

import argparse
import ast
import csv
import datetime
import json
import os
import socket

#: Port UDP sur lequel l'ERP émet les demandes de transport.
PORT_ERP = 40040

#: Taille de réception d'un datagramme, en octets. Un message peut contenir
#: plusieurs demandes : les 2048 octets du client fourni peuvent ne pas suffire.
TAILLE_MAX_DATAGRAMME = 65535

#: Répertoire de sortie par défaut, relatif à ce fichier.
REPERTOIRE_SORTIE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "donnees", "demandes")

#: Clefs connues du champ ``SPEC`` ; chacune obtient sa propre colonne,
#: les autres sont regroupées dans ``SPEC_AUTRES``.
CLEFS_SPEC = ["PTAC", "ENR", "COL"]

#: Colonnes du fichier CSV, dans l'ordre.
COLONNES_CSV = ["ID_DEMANDE", "H_RECEPTION",
                "TYPE_MVT", "LOC_DEP", "LOC_ARR", "TYPE_DEP", "TYPE_ARR",
                "TYPE_MAT", "QTE_PAL", "TYPE_PAL",
                "H_DEP", "H_DEP_ISO",
                "SPEC"] + ["SPEC_" + clef for clef in CLEFS_SPEC] + ["SPEC_AUTRES"]


def creer_client_udp(port: int) -> socket.socket:
    """Crée le client UDP qui écoute les messages broadcast de l'ERP.

    :param port: Port d'écoute.
    :return: Socket UDP liée au port, prête à recevoir.
    """
    clientUDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    clientUDP.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    clientUDP.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    clientUDP.bind(("", port))
    return clientUDP


def decoder_message(texte: str) -> dict:
    """Transforme le texte reçu en dictionnaire ``{id_demande: {champs}}``.

    Le serveur envoie des simples quotes, refusées par JSON : on les remplace
    comme dans ``RSCR_ERP_CLIENT_UDP.py``. Si une valeur contient elle-même une
    apostrophe (``L'Aquila``), ce remplacement casse le JSON : on retente alors
    avec :func:`ast.literal_eval`, qui lit directement la syntaxe d'un
    dictionnaire Python.

    :param texte: Texte du message reçu.
    :return: Dictionnaire des demandes, indexé par identifiant unique.
    :raises ValueError: Si le message est illisible ou n'est pas un dictionnaire.
    """
    try:
        message = json.loads(texte.replace("'", '"'))
    except json.JSONDecodeError:
        try:
            message = ast.literal_eval(texte)
        except (ValueError, SyntaxError) as erreur:
            raise ValueError("message illisible : %s" % erreur)
    if not isinstance(message, dict):
        raise ValueError("le message n'est pas un dictionnaire")
    return message


def decouper_spec(spec: str) -> dict:
    """Découpe le champ ``SPEC`` en dictionnaire.

    >>> decouper_spec("PTAC=12t, COL=Rouge")
    {'PTAC': '12t', 'COL': 'Rouge'}
    >>> decouper_spec("")
    {}

    :param spec: Chaîne de couples ``CLEF=valeur`` séparés par des virgules, éventuellement vide.
    :return: Dictionnaire ``{clef: valeur}``, valeurs en ``str``.
    """
    specs = {}
    for element in spec.split(","):
        element = element.strip()
        if element == "":
            continue
        clef, _, valeur = element.partition("=")
        specs[clef.strip()] = valeur.strip()
    return specs


def convertir_h_dep(h_dep: str) -> str:
    """Convertit ``H_DEP`` au format ISO, plus simple à trier et à comparer.

    >>> convertir_h_dep("18/07/26 21:19:07")
    '2026-07-18 21:19:07'

    :param h_dep: Date au format ``JJ/MM/AA hh:mm:ss`` (heure réelle).
    :return: Date au format ``AAAA-MM-JJ hh:mm:ss``, ou ``""`` si le format est inattendu.
    """
    try:
        return datetime.datetime.strptime(h_dep, "%d/%m/%y %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ""


def demande_vers_ligne(id_demande: str, demande: dict, h_reception: str) -> dict:
    """Met une demande de transport à plat pour l'écrire dans le CSV.

    Le champ ``SPEC`` est conservé tel quel et aussi découpé : une colonne par
    clef de :data:`CLEFS_SPEC`, le reste dans ``SPEC_AUTRES``.

    :param id_demande: Identifiant unique de la demande.
    :param demande: Champs de la demande tels qu'envoyés par l'ERP.
    :param h_reception: Heure de réception (``AAAA-MM-JJ hh:mm:ss``).
    :return: Dictionnaire ``{colonne: valeur}`` conforme à :data:`COLONNES_CSV`.
    """
    ligne = {"ID_DEMANDE": id_demande, "H_RECEPTION": h_reception}
    for champ in ["TYPE_MVT", "LOC_DEP", "LOC_ARR", "TYPE_DEP", "TYPE_ARR", "TYPE_MAT", "QTE_PAL", "TYPE_PAL", "H_DEP"]:
        ligne[champ] = demande.get(champ, "")
    ligne["H_DEP_ISO"] = convertir_h_dep(ligne["H_DEP"])

    spec = demande.get("SPEC", "") or ""
    specs = decouper_spec(spec)
    ligne["SPEC"] = spec
    for clef in CLEFS_SPEC:
        ligne["SPEC_" + clef] = specs.pop(clef, "")
    ligne["SPEC_AUTRES"] = ", ".join("%s=%s" % (clef, valeur) for clef, valeur in specs.items())
    return ligne


def chemins_du_jour(repertoire: str, jour: datetime.date) -> tuple[str, str]:
    """Donne les chemins des fichiers du jour *réel*.

    :param repertoire: Répertoire de sortie.
    :param jour: Date du jour.
    :return: Couple (chemin du ``.jsonl``, chemin du ``.csv``).
    """
    base = os.path.join(repertoire, "demandes_" + jour.strftime("%Y-%m-%d"))
    return base + ".jsonl", base + ".csv"


def charger_ids_existants(chemin_csv: str) -> set[str]:
    """Relit les identifiants déjà enregistrés dans le CSV du jour.

    Évite de dupliquer une demande si le programme est relancé dans la journée.

    :param chemin_csv: Chemin du CSV du jour.
    :return: Ensemble des ``ID_DEMANDE`` déjà présents (vide si le fichier n'existe pas).
    """
    if not os.path.exists(chemin_csv):
        return set()
    with open(chemin_csv, newline="", encoding="utf-8") as fichier:
        return {ligne["ID_DEMANDE"] for ligne in csv.DictReader(fichier, delimiter=";")}


def ecrire_brut(chemin_jsonl: str, h_reception: str, adresse: tuple[str, int], texte: str) -> None:
    """Ajoute le message brut au journal ``.jsonl``.

    Appelée avant tout décodage : rien n'est perdu si le message est illisible.

    :param chemin_jsonl: Chemin du journal du jour.
    :param h_reception: Heure de réception (``AAAA-MM-JJ hh:mm:ss``).
    :param adresse: Couple (IP, port) de l'émetteur.
    :param texte: Texte du message, sans modification.
    """
    entree = {"H_RECEPTION": h_reception, "EMETTEUR": "%s:%d" % adresse, "MESSAGE": texte}
    with open(chemin_jsonl, "a", encoding="utf-8") as fichier:
        fichier.write(json.dumps(entree, ensure_ascii=False) + "\n")


def ecrire_demandes(chemin_csv: str, lignes: list[dict]) -> None:
    """Ajoute des demandes au CSV du jour.

    Le fichier est créé avec son en-tête s'il n'existe pas encore.

    :param chemin_csv: Chemin du CSV du jour.
    :param lignes: Lignes produites par :func:`demande_vers_ligne`.
    """
    nouveau_fichier = not os.path.exists(chemin_csv)
    with open(chemin_csv, "a", newline="", encoding="utf-8") as fichier:
        ecrivain = csv.DictWriter(fichier, fieldnames=COLONNES_CSV, delimiter=";")
        if nouveau_fichier:
            ecrivain.writeheader()
        ecrivain.writerows(lignes)


def collecter(port: int, repertoire: str) -> None:
    """Reçoit, archive puis structure chaque message, jusqu'à ``Ctrl+C``.

    :param port: Port UDP d'écoute.
    :param repertoire: Répertoire de sortie (créé s'il n'existe pas).
    """
    os.makedirs(repertoire, exist_ok=True)
    clientUDP = creer_client_udp(port)

    jour_courant = None
    ids_connus = set()
    nb_demandes = 0

    print("\nCollecte des demandes de transport sur le port %d -> %s" % (port, os.path.abspath(repertoire)))
    print("'Ctrl+C' pour quitter le programme")
    while True:
        data, adresse = clientUDP.recvfrom(TAILLE_MAX_DATAGRAMME)
        maintenant = datetime.datetime.now()
        h_reception = maintenant.strftime("%Y-%m-%d %H:%M:%S")

        # Changement de jour réel : on passe à de nouveaux fichiers.
        if maintenant.date() != jour_courant:
            jour_courant = maintenant.date()
            chemin_jsonl, chemin_csv = chemins_du_jour(repertoire, jour_courant)
            ids_connus = charger_ids_existants(chemin_csv)

        texte = data.decode("utf-8", errors="replace")
        ecrire_brut(chemin_jsonl, h_reception, adresse, texte)

        try:
            message = decoder_message(texte)
        except ValueError as erreur:
            print("[%s] ! Message ignoré (%s), conservé dans %s" % (h_reception, erreur, os.path.basename(chemin_jsonl)), flush=True)
            continue

        # Une demande déjà vue n'est pas réécrite.
        lignes = []
        for id_demande, demande in message.items():
            if id_demande in ids_connus or not isinstance(demande, dict):
                continue
            lignes.append(demande_vers_ligne(id_demande, demande, h_reception))
            ids_connus.add(id_demande)

        if lignes:
            ecrire_demandes(chemin_csv, lignes)
            nb_demandes += len(lignes)
        print("[%s] %d demande(s) reçue(s), %d nouvelle(s) - total session : %d"
              % (h_reception, len(message), len(lignes), nb_demandes), flush=True)


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Sauvegarde les demandes de transport de l'ERP (UDP) en JSONL et CSV")
    parseur.add_argument("--port", type=int, default=PORT_ERP, help="port UDP d'écoute (défaut : %d)" % PORT_ERP)
    parseur.add_argument("--sortie", default=REPERTOIRE_SORTIE, help="répertoire des fichiers produits")
    arguments = parseur.parse_args()
    try:
        collecter(arguments.port, arguments.sortie)
    except KeyboardInterrupt:
        print("\nArrêt de la collecte.")
