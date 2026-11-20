"""Tableau de bord d'audit NORTH : serveur web local (sockets bruts, comme ``RSCR_SERVEUR_TCP2HTML.py``).

Le serveur relit à chaque requête le journal du moteur et les fichiers du
collecteur (:mod:`audit`) et sert :

- ``/`` : la page ``tableau_bord.html`` (Chart.js local, rafraîchie toutes les 5 s) ;
- ``/api/kpi?debut=AAAA-MM-JJTHH:MM&duree=120`` : les indicateurs en JSON ;
- ``/api/rapport.json`` : le même rapport, à télécharger (pièce pour l'audit) ;
- ``/api/statuts.csv`` : statut final de chaque demande NORTH ;
- ``/chart.js`` : Chart.js 2.5 (copie locale, la VM n'a pas internet).

Les valeurs tirées de l'Annexe 2 (seuil de rentabilité, coût horaire) ne sont envoyées
qu'avec ``--confidentiel`` : sans cette option, la page peut être montrée au
groupe Contrôle ou ouverte depuis le VLAN.

Utilisation, depuis ``1 - DEV/`` :

.. code-block:: bash

    python3 tableau_bord.py                                   # VM : dernier journal du moteur, CSV du collecteur
    python3 tableau_bord.py --hote 0.0.0.0                    # accessible depuis le VLAN (http://IP_VM:10000/)
    python3 tableau_bord.py --journal "../../_build/rejeu-audit/balayage/x10-th0.85-w0.5" \\
        --demandes "../../_build/rejeu-audit/demandes_2h.jsonl"   # local, sur un rejeu
    python3 tableau_bord.py --instantane ../donnees/audit/rapport.json   # un rapport, sans serveur
"""

import argparse
import datetime
import json
import os
import socket
import urllib.parse

import audit
import hypotheses
import robustesse

DOSSIER = os.path.dirname(os.path.abspath(__file__))

#: Page HTML du tableau de bord.
FICHIER_PAGE = os.path.join(DOSSIER, "tableau_bord.html")

#: Chart.js : copie dans le dépôt, sinon celle des ressources de l'enseignant.
FICHIERS_CHARTJS = [os.path.join(DOSSIER, "static", "Chart_min.js"),
                    os.path.join(DOSSIER, "..", "RSCR-20261017", "lib", "Chart_min.js")]

#: Au-delà de cette ancienneté (secondes réelles) du journal, on le considère figé (rejeu terminé).
JOURNAL_FIGE_S = 120


def lire_parametres() -> argparse.Namespace:
    """Lit les options de la ligne de commande.

    :return: Options.
    """
    parseur = argparse.ArgumentParser(description="Tableau de bord d'audit du moteur NORTH")
    parseur.add_argument("--journal", default=audit.REPERTOIRE_JOURNAUX,
                         help="journal evenements_*.csv, ou dossier (le plus récent est lu)")
    parseur.add_argument("--demandes", default=audit.REPERTOIRE_DEMANDES,
                         help="fichier .csv/.jsonl du collecteur, ou dossier des demandes_AAAA-MM-JJ")
    parseur.add_argument("--hote", default="127.0.0.1", help="adresse d'écoute (0.0.0.0 pour le VLAN)")
    parseur.add_argument("--port", type=int, default=10000, help="port TCP")
    parseur.add_argument("--cibles", default=audit.FICHIER_CIBLES, help="cibles du contrat (JSON)")
    parseur.add_argument("--cible-ta", type=float, default=None, help="cible du taux d'acceptation (0-1)")
    parseur.add_argument("--cible-tr", type=float, default=None, help="cible du taux de remplissage (0-1)")
    parseur.add_argument("--cible-haute", type=float, default=None, help="cible de la part de gamme haute (0-1)")
    parseur.add_argument("--horloge", choices=["auto", "reelle", "journal"], default="auto",
                         help="heure de référence : réelle (direct), dernier événement (rejeu), ou auto")
    parseur.add_argument("--confidentiel", action="store_true",
                         help="affiche le seuil de rentabilité et le coût estimé (Annexe 2) : usage interne seulement")
    parseur.add_argument("--instantane", default=None, help="écrit un rapport JSON dans ce fichier et s'arrête")
    return parseur.parse_args()


def preparer_config(options: argparse.Namespace) -> dict:
    """Rassemble les cibles et les données confidentielles éventuelles.

    :param options: Options de la ligne de commande.
    :return: ``{"cibles", "seuil", "taux_horaire"}``.
    """
    cibles = audit.charger_cibles(options.cibles)
    for cle, valeur in (("ta", options.cible_ta), ("tr", options.cible_tr), ("part_haute", options.cible_haute)):
        if valeur is not None:
            cibles[cle] = valeur
    seuil = taux = None
    if options.confidentiel:
        couts = hypotheses.charger_confidentiel()
        seuil = couts["seuil_rentabilite"]
        taux = couts["taux_conducteur_h"] + couts["taux_vehicule_h"] if couts["source"] != "defaut" else None
    return {"cibles": cibles, "seuil": seuil, "taux_horaire": taux}


def heure_reference(sources: dict, mode: str) -> tuple:
    """Choisit l'heure de référence des calculs.

    En direct, c'est l'heure réelle. Pour un journal figé (rejeu, moteur
    arrêté), c'est l'heure de son dernier événement : sinon toutes les
    demandes acquittées passeraient « en retard ».

    :param sources: Sources lues par :func:`audit.charger_sources`.
    :param mode: ``"auto"``, ``"reelle"`` ou ``"journal"``.
    :return: ``(maintenant, horloge)`` avec ``horloge`` = ``"reelle"`` ou ``"journal"``.
    """
    maintenant = datetime.datetime.now()
    evenements = sources["evenements"]
    if mode == "auto":
        fige = sources["maj_journal"] is None or (maintenant - sources["maj_journal"]).total_seconds() > JOURNAL_FIGE_S
        mode = "journal" if fige and evenements else "reelle"
    if mode == "journal" and evenements:
        return evenements[-1]["H"], "journal"
    return maintenant, "reelle"


def calculer_rapport(options: argparse.Namespace, config: dict, requete: dict) -> tuple:
    """Lit les sources et calcule le rapport pour une fenêtre demandée.

    :param options: Options de la ligne de commande.
    :param config: Cibles et données confidentielles (:func:`preparer_config`).
    :param requete: Paramètres de l'URL (``debut`` au format ``AAAA-MM-JJTHH:MM[:SS]``, ``duree`` en minutes).
    :return: ``(rapport, sources, maintenant)``.
    """
    sources = audit.charger_sources(options.journal, options.demandes)
    maintenant, horloge = heure_reference(sources, options.horloge)
    debut = None
    if requete.get("debut"):
        texte = requete["debut"].replace("T", " ")
        debut = audit.lire_heure(texte if len(texte) > 16 else texte + ":00")
    duree = None
    try:
        duree = float(requete["duree"]) if requete.get("duree") else None
    except ValueError:
        duree = None
    rapport = audit.calculer(sources, maintenant, horloge, debut, duree, config["seuil"],
                             config["cibles"], config["taux_horaire"])
    rapport["confidentiel"] = config["seuil"] is not None
    return rapport, sources, maintenant


def reponse_http(statut: str, type_contenu: str, corps: bytes, entetes: dict = None) -> bytes:
    """Construit une réponse HTTP/1.0 complète.

    :param statut: Ligne de statut, par exemple ``"200 OK"``.
    :param type_contenu: Type MIME.
    :param corps: Contenu.
    :param entetes: En-têtes supplémentaires.
    :return: Réponse prête à envoyer.
    """
    lignes = ["HTTP/1.0 %s" % statut, "Content-Type: %s" % type_contenu, "Content-Length: %d" % len(corps),
              "Cache-Control: no-store", "Connection: close"]
    lignes += ["%s: %s" % element for element in (entetes or {}).items()]
    return ("\r\n".join(lignes) + "\r\n\r\n").encode("utf-8") + corps


def lire_fichier(chemins: list) -> bytes:
    """Renvoie le contenu du premier fichier existant.

    :param chemins: Chemins candidats.
    :return: Contenu, ou ``b""`` si aucun n'existe.
    """
    for chemin in chemins:
        if os.path.exists(chemin):
            with open(chemin, "rb") as fichier:
                return fichier.read()
    return b""


def traiter_requete(ligne: str, options: argparse.Namespace, config: dict) -> bytes:
    """Répond à une requête HTTP ``GET``.

    :param ligne: Première ligne de la requête (``GET /chemin?x=1 HTTP/1.1``).
    :param options: Options de la ligne de commande.
    :param config: Cibles et données confidentielles.
    :return: Réponse HTTP.
    """
    morceaux = ligne.split()
    if len(morceaux) < 2 or morceaux[0] != "GET":
        return reponse_http("405 Method Not Allowed", "text/plain; charset=utf-8", b"GET seulement")
    url = urllib.parse.urlsplit(morceaux[1])
    requete = {cle: valeurs[0] for cle, valeurs in urllib.parse.parse_qs(url.query).items()}
    if url.path in ("/", "/index.html"):
        return reponse_http("200 OK", "text/html; charset=utf-8", lire_fichier([FICHIER_PAGE]))
    if url.path == "/chart.js":
        return reponse_http("200 OK", "application/javascript", lire_fichier(FICHIERS_CHARTJS))
    if url.path in ("/api/kpi", "/api/rapport.json"):
        rapport, _, _ = calculer_rapport(options, config, requete)
        corps = json.dumps(rapport, ensure_ascii=False, indent=1 if url.path.endswith(".json") else None).encode("utf-8")
        entetes = {}
        if url.path.endswith(".json"):
            nom = "rapport_audit_NORTH_%s.json" % datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            entetes["Content-Disposition"] = 'attachment; filename="%s"' % nom
        return reponse_http("200 OK", "application/json; charset=utf-8", corps, entetes)
    if url.path == "/api/statuts.csv":
        _, sources, maintenant = calculer_rapport(options, config, requete)
        nom = "statuts_NORTH_%s.csv" % datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return reponse_http("200 OK", "text/csv; charset=utf-8", audit.statuts_csv(sources, maintenant).encode("utf-8"),
                            {"Content-Disposition": 'attachment; filename="%s"' % nom})
    return reponse_http("404 Not Found", "text/plain; charset=utf-8", b"introuvable")


def lire_entete(connexion: socket.socket) -> str:
    """Lit l'en-tête d'une requête HTTP (jusqu'à la ligne vide).

    :param connexion: Socket du client.
    :return: En-tête décodé (vide si le client n'a rien envoyé).
    """
    donnees = b""
    while b"\r\n\r\n" not in donnees and len(donnees) < 65536:
        morceau = connexion.recv(4096)
        if not morceau:
            break
        donnees += morceau
    return donnees.decode("utf-8", errors="replace")


def servir(options: argparse.Namespace, config: dict) -> None:
    """Fait tourner le serveur web jusqu'à ``Ctrl+C``.

    Un client à la fois : chaque requête est courte (lecture de fichiers en
    cache et calculs en mémoire), ce qui suffit pour quelques navigateurs.
    Une erreur sur une requête est affichée sans arrêter le serveur.

    :param options: Options de la ligne de commande.
    :param config: Cibles et données confidentielles.
    """
    serveur = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serveur.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serveur.bind((options.hote, options.port))
    serveur.listen(5)
    adresse = "localhost" if options.hote in ("127.0.0.1", "0.0.0.0") else options.hote
    print("Tableau de bord : http://%s:%d/  ('Ctrl+C' pour arrêter)" % (adresse, options.port))
    print("Journal : %s\nDemandes : %s" % (os.path.abspath(options.journal), os.path.abspath(options.demandes)))
    print("Cibles : %s ; données confidentielles : %s" % (config["cibles"], "affichées" if config["seuil"] is not None else "masquées"))
    try:
        while True:
            connexion, client = serveur.accept()
            connexion.settimeout(5)
            try:
                entete = lire_entete(connexion)
                if entete:
                    connexion.sendall(traiter_requete(entete.split("\r\n", 1)[0], options, config))
            except Exception as erreur:  # le serveur doit survivre à une requête en erreur
                print("[%s] erreur pour %s : %r" % (datetime.datetime.now().strftime("%H:%M:%S"), client[0], erreur))
                try:
                    connexion.sendall(reponse_http("500 Internal Server Error", "text/plain; charset=utf-8",
                                                   ("erreur : %r" % erreur).encode("utf-8")))
                except OSError:
                    pass
            finally:
                connexion.close()
    except KeyboardInterrupt:
        print("\nArrêt du tableau de bord.")
    finally:
        serveur.close()


def ecrire_instantane(options: argparse.Namespace, config: dict) -> dict:
    """Calcule un rapport sur tout le journal, l'écrit en JSON et affiche un résumé.

    :param options: Options de la ligne de commande (``instantane`` = fichier de sortie).
    :param config: Cibles et données confidentielles.
    :return: Rapport.
    """
    rapport, _, _ = calculer_rapport(options, config, {})
    dossier = os.path.dirname(os.path.abspath(options.instantane))
    os.makedirs(dossier, exist_ok=True)
    with open(options.instantane, "w", encoding="utf-8") as fichier:
        json.dump(rapport, fichier, ensure_ascii=False, indent=1)
    acceptation, remplissage = rapport["acceptation"], rapport["remplissage"]
    pourcent = lambda x: "—" if x is None else "%.1f %%" % (100 * x)  # noqa: E731
    print("Fenêtre %s → %s (horloge %s)" % (rapport["fenetre"]["debut"], rapport["fenetre"]["fin"], rapport["horloge"]))
    print("TA %s (%d parties / %d reçues) ; TR %s ; gamme haute %s"
          % (pourcent(acceptation["ta"]), acceptation["partie"], acceptation["recues"],
             pourcent(remplissage["tr"]), pourcent(rapport["gammes"]["part_haute"])))
    for controle in rapport["controles"]:
        print("  [%s] %s : %d" % ("OK" if controle["ok"] else "KO", controle["libelle"], controle["n"]))
    print("Rapport : %s" % os.path.abspath(options.instantane))
    return rapport


if __name__ == "__main__":
    robustesse.arreter_sur_sigterm()  # R-ROB-03 : « nord.sh arreter » envoie SIGTERM
    arguments = lire_parametres()
    configuration = preparer_config(arguments)
    if arguments.instantane:
        ecrire_instantane(arguments, configuration)
    else:
        servir(arguments, configuration)
