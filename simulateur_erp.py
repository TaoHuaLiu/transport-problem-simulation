"""Simulateur local de l'ERP de Garden Food.

Permet de tester :mod:`collecteur` (et le moteur en mode direct) hors du
VLAN de l'ENSIBS. Toutes les 1 à 3 secondes *réelles*, il envoie un message
contenant 1 à 3 demandes de transport aléatoires, au même format que le vrai
serveur (dictionnaire Python avec simples quotes).

Les valeurs tirées imitent l'échantillon réel du 2026-10-19
(``6 - Data/Briefcase/``) : noms de villes sans accents, imports toujours
port → usine en ``MP Frais``, 1 à 19 palettes, ``H_DEP`` 6 à 13 minutes
réelles après l'envoi, environ 10 % d'imports. Les autres proportions ne
sont pas calées.

Utilisation :

.. code-block:: bash

    python3 simulateur_erp.py [--hote 127.0.0.1] [--port 40040]
"""

import argparse
import datetime
import random
import socket
import string
import time

#: Usines de Garden Food (départ des exports, arrivée des imports), sans accents comme l'ERP.
USINES = ["Amsterdam", "Dusseldorf", "Manchester", "Brest", "Lyon", "Bucarest", "Munich", "Zagreb",
          "Florence", "Valence", "Seville"]

#: Ports (arrivée des exports, départ des imports) et GMS (arrivée des exports) observés.
PORTS = ["Amsterdam", "Anvers", "Athenes", "Le Havre", "Marseille", "Rotterdam", "Valence"]
GMS = ["Manchester", "Paris", "Seville", "Vienne", "Zagreb"]

#: Types de marchandise d'un export (un import est toujours ``MP Frais``).
TYPES_MAT_EXPORT = ["PF", "PF Frais", "MP Frais"]

#: Types de palette possibles.
TYPES_PAL = ["EPAL", "EPALh", "ISO"]

#: Valeurs de ``SPEC`` tirées des exemples du sujet ; la chaîne vide est
#: répétée pour qu'une majorité de demandes n'ait pas de contrainte.
SPECS = ["", "", "", "", "PTAC=12t", "PTAC=19t", "ENR=Electrique, COL=Vert", "PTAC=12t, COL=Rouge"]


def generer_demande() -> tuple[str, dict]:
    """Génère une demande de transport aléatoire.

    :return: Couple (identifiant de 20 caractères, champs de la demande).
    """
    export = random.random() < 0.9
    usine = random.choice(USINES)
    type_arr = random.choice(["GMS", "PORT"]) if export else "USINE"
    h_dep = datetime.datetime.now() + datetime.timedelta(minutes=random.uniform(6, 13))
    demande = {"TYPE_MVT": "Export" if export else "Import",
               "LOC_DEP": usine if export else random.choice(PORTS),
               "LOC_ARR": random.choice(GMS if type_arr == "GMS" else PORTS) if export else usine,
               "TYPE_DEP": "USINE" if export else "PORT",
               "TYPE_ARR": type_arr,
               "TYPE_MAT": random.choice(TYPES_MAT_EXPORT) if export else "MP Frais",
               "QTE_PAL": random.randint(1, 19),
               "TYPE_PAL": random.choice(TYPES_PAL),
               "H_DEP": h_dep.strftime("%d/%m/%y %H:%M:%S"),
               "SPEC": random.choice(SPECS)}
    identifiant = "".join(random.choices(string.ascii_uppercase + string.digits, k=20))
    return identifiant, demande


def simuler(hote: str, port: int) -> None:
    """Envoie des messages de demandes en boucle, jusqu'à ``Ctrl+C``.

    :param hote: Adresse IP de destination.
    :param port: Port UDP de destination.
    """
    serveurUDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    serveurUDP.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    print("Simulation de l'ERP vers %s:%d. 'Ctrl+C' pour quitter" % (hote, port))
    while True:
        message = dict(generer_demande() for _ in range(random.randint(1, 3)))
        serveurUDP.sendto(str(message).encode("utf-8"), (hote, port))  # str() produit les simples quotes de l'ERP.
        print("Envoyé : %d demande(s)" % len(message))
        time.sleep(random.uniform(1, 3))


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Simulateur local de l'ERP Garden Food (UDP)")
    parseur.add_argument("--hote", default="127.0.0.1", help="adresse de destination (défaut : 127.0.0.1)")
    parseur.add_argument("--port", type=int, default=40040, help="port de destination (défaut : 40040)")
    arguments = parseur.parse_args()
    try:
        simuler(arguments.hote, arguments.port)
    except KeyboardInterrupt:
        print("\nArrêt du simulateur.")
