"""Tableau de bord d'audit NORTH : serveur web local (sockets bruts, comme ``RSCR_SERVEUR_TCP2HTML.py``).

Le serveur relit à chaque requête le journal du moteur et les fichiers du
collecteur (:mod:`audit`) et sert :

- ``/`` : la page ``tableau_bord.html`` (Chart.js local, rafraîchie toutes les 5 s) ;
- ``/api/kpi?debut=AAAA-MM-JJTHH:MM&duree=120`` : les indicateurs en JSON ;
- ``/api/rapport.json`` : le même rapport, à télécharger (pièce pour l'audit) ;
- ``/api/statuts.csv`` : statut final de chaque demande NORTH ;
- ``/api/sante`` : santé de la chaîne (fichiers ``../donnees/run/sante_*.json``) et alertes
  (moteur muet, écriture en base en échec, ERP muet…), règle R-ROB-06 ;
- ``/api/tables`` et ``/api/table?nom=TRACABILITE_COMMANDE&n=50`` : nos tables de la base, en lecture
  seule (:mod:`tables_direct`), avec le nombre de commandes décidées au journal pour comparer ;
- ``/api/carte`` : position de chaque camion NORTH (lieu, ou point d'un tronçon en cours) ;
- ``/api/sql?n=100&erreurs=1`` : derniers événements du journal SQL du moteur (``../donnees/logs/sql.jsonl``) ;
- ``/api/debug`` (seulement avec ``--debug``) : configuration, temps de réponse des dernières
  requêtes, derniers avertissements et erreurs de tous les services ;
- ``/chart.js`` : Chart.js 2.5 (copie locale, la VM n'a pas internet).

Mode débogage (``--debug``) : chaque requête HTTP est journalisée avec sa durée,
et la page affiche un panneau de débogage. Sans ``--debug``, ``/api/debug`` répond 404 :
les journaux des services ne sont pas exposés sur le VLAN.

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
import platform
import socket
import time
import urllib.parse

import audit
import hypotheses
import journalisation
import robustesse
import tables_direct

JOURNAL = journalisation.journal("tableau")

DOSSIER = os.path.dirname(os.path.abspath(__file__))

#: Page HTML du tableau de bord.
FICHIER_PAGE = os.path.join(DOSSIER, "tableau_bord.html")

#: Chart.js : copie dans le dépôt, sinon celle des ressources de l'enseignant.
FICHIERS_CHARTJS = [os.path.join(DOSSIER, "static", "Chart_min.js"),
                    os.path.join(DOSSIER, "..", "RSCR-20261017", "lib", "Chart_min.js")]

#: Au-delà de cette ancienneté (secondes réelles) du journal, on le considère figé (rejeu terminé).
JOURNAL_FIGE_S = 120

#: Au-delà de cette ancienneté (secondes réelles), un fichier de santé signale un service muet ou arrêté.
SANTE_MUETTE_S = 30

#: Au-delà de cette durée (secondes réelles) sans message de l'ERP, le collecteur est signalé.
ERP_MUET_S = 120

#: Nombre de requêtes HTTP gardées pour le panneau de débogage.
NB_REQUETES_GARDEES = 50


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
    parseur.add_argument("--cible-ta", type=float, default=None,
                         help="cible TA fixe (0-1) ; par défaut, cible du jour calculée par le contrat §4.3")
    parseur.add_argument("--cible-tr", type=float, default=None,
                         help="cible TR fixe (0-1) ; par défaut, cible du jour selon le débit, contrat §4.1")
    parseur.add_argument("--cible-haute", type=float, default=None, help="cible de la part de gamme haute (0-1)")
    parseur.add_argument("--camions-frigo", type=int, default=None,
                         help="N_F imposé (camions NORTH frigorifiques) ; par défaut, lu dans l'état de la flotte du moteur")
    parseur.add_argument("--camions-sec", type=int, default=None, help="N_S imposé (camions NORTH non frigorifiques)")
    parseur.add_argument("--flotte", default=None,
                         help="état de la flotte écrit par le moteur (défaut : <run>/flotte_moteur.json)")
    parseur.add_argument("--horloge", choices=["auto", "reelle", "journal"], default="auto",
                         help="heure de référence : réelle (direct), dernier événement (rejeu), ou auto")
    parseur.add_argument("--confidentiel", action="store_true",
                         help="affiche le seuil de rentabilité et le coût estimé (Annexe 2) : usage interne seulement")
    parseur.add_argument("--instantane", default=None, help="écrit un rapport JSON dans ce fichier et s'arrête")
    parseur.add_argument("--debug", action="store_true",
                         help="mode débogage : requêtes journalisées avec leur durée, panneau et /api/debug (pas en audit)")
    parseur.add_argument("--sans-tables", action="store_true",
                         help="désactive l'onglet Tables (aucune lecture de la base par le tableau de bord)")
    parseur.add_argument("--logs", default=journalisation.REPERTOIRE_LOGS, help="dossier des journaux")
    parseur.add_argument("--run", default=journalisation.REPERTOIRE_RUN, help="dossier des fichiers de santé")
    return parseur.parse_args()


def preparer_config(options: argparse.Namespace) -> dict:
    """Rassemble les cibles et les données confidentielles éventuelles.

    :param options: Options de la ligne de commande.
    :return: ``{"cibles", "seuil", "taux_horaire"}``.
    """
    cibles = audit.charger_cibles(options.cibles)
    for cle, valeur in (("ta", options.cible_ta), ("tr", options.cible_tr), ("part_haute", options.cible_haute),
                        ("camions_frigo", options.camions_frigo), ("camions_sec", options.camions_sec)):
        if valeur is not None:
            cibles[cle] = valeur
    seuil = taux = None
    if options.confidentiel:
        couts = hypotheses.charger_confidentiel()
        seuil = couts["seuil_rentabilite"]
        taux = couts["taux_conducteur_h"] + couts["taux_vehicule_h"] if couts["source"] != "defaut" else None
    return {"cibles": cibles, "seuil": seuil, "taux_horaire": taux, "requetes": [], "debut": datetime.datetime.now(),
            "tables": tables_direct.nouveau_lien(actif=not getattr(options, "sans_tables", False))}


def _age(texte: str, maintenant: datetime.datetime):
    """Secondes écoulées depuis une heure ISO.

    :param texte: Heure ``AAAA-MM-JJTHH:MM:SS`` ou ``AAAA-MM-JJ HH:MM:SS`` (``None`` accepté).
    :param maintenant: Heure réelle courante.
    :return: Secondes, ou ``None`` si l'heure est absente ou illisible.
    :rtype: float or None
    """
    try:
        return (maintenant - datetime.datetime.fromisoformat(str(texte).replace(" ", "T"))).total_seconds()
    except (TypeError, ValueError):
        return None


def alertes_sante(services: dict, maintenant: datetime.datetime) -> list:
    """Alertes tirées des fichiers de santé (règle R-ROB-06).

    :param services: Résultat de :func:`journalisation.lire_sante`.
    :param maintenant: Heure réelle courante.
    :return: ``[{"niveau": "critique"|"attention", "service", "texte"}]``, les critiques d'abord.
    """
    alertes = []

    def ajouter(niveau: str, service: str, texte: str) -> None:
        """Ajoute une alerte.

        :param niveau: ``critique`` ou ``attention``.
        :param service: Service concerné.
        :param texte: Message.
        """
        alertes.append({"niveau": niveau, "service": service, "texte": texte})

    moteur = services.get("moteur")
    if moteur is None:
        ajouter("attention", "moteur", "aucun fichier de santé du moteur (jamais lancé en direct sur cette machine)")
    elif moteur.get("statut") != "en marche":
        ajouter("critique", "moteur", "moteur %s" % moteur.get("statut"))
    elif moteur.get("age_s", 0) > SANTE_MUETTE_S:
        ajouter("critique", "moteur", "moteur muet depuis %.0f s (bloqué ou arrêté brutalement)" % moteur["age_s"])
    if moteur is not None and moteur.get("statut") == "en marche":
        for cle, nom, niveau in (("archivage", "écriture LOG_NORTH_*", "critique"),
                                 ("controle", "écriture TRACABILITE_*", "critique"),
                                 ("referentiel", "lecture du référentiel", "attention"),
                                 ("evenements", "journal des événements (CSV)", "critique")):
            bdd = moteur.get(cle) or {}
            if bdd.get("actif") and bdd.get("echecs"):
                ajouter(niveau, "moteur", "%s EN ÉCHEC depuis %s (%d essai(s), %d ligne(s) en attente) : %s"
                        % (nom, bdd.get("depuis"), bdd["echecs"], bdd.get("en_attente", 0), bdd.get("derniere_erreur")))
            elif bdd.get("actif") and bdd.get("rejetees"):
                ajouter("attention", "moteur", "%s : %d ligne(s) rejetée(s) par la base (voir journal SQL)" % (nom, bdd["rejetees"]))
        if (moteur.get("evenements") or {}).get("perdus"):
            ajouter("critique", "moteur", "journal des événements : %d ligne(s) perdue(s) (mémoire pleine, R-ROB-08)"
                    % moteur["evenements"]["perdus"])
        if moteur.get("duree_tick_s", 0) > moteur.get("tick_s", hypotheses.TICK_S):
            ajouter("attention", "moteur", "tick de %.1f s : le moteur prend du retard" % moteur["duree_tick_s"])
    collecteur = services.get("collecteur")
    if collecteur is None:
        ajouter("attention", "collecteur", "aucun fichier de santé du collecteur")
    elif collecteur.get("statut") != "en marche" or collecteur.get("age_s", 0) > SANTE_MUETTE_S:
        ajouter("critique", "collecteur", "collecteur %s : les messages de l'ERP sont perdus"
                % (collecteur.get("statut") if collecteur.get("statut") != "en marche" else "muet"))
    else:
        silence = _age(collecteur.get("dernier_message") or collecteur.get("debut"), maintenant)
        if silence is not None and silence > ERP_MUET_S:
            ajouter("attention", "collecteur", "aucun message de l'ERP depuis %.0f s" % silence)
    alertes.sort(key=lambda a: a["niveau"] != "critique")
    return alertes


def rapport_sante(options: argparse.Namespace) -> dict:
    """Santé de la chaîne pour ``/api/sante``.

    :param options: Options de la ligne de commande (``run``, ``debug``).
    :return: ``{"maintenant", "services", "alertes", "debug"}``.
    """
    maintenant = datetime.datetime.now()
    services = journalisation.lire_sante(options.run)
    return {"maintenant": maintenant.isoformat(timespec="seconds"), "services": services,
            "alertes": alertes_sante(services, maintenant), "debug": bool(options.debug)}


def rapport_table(options: argparse.Namespace, config: dict, requete: dict) -> dict:
    """Lignes d'une de nos tables pour ``/api/table``, avec le décompte du journal pour comparer.

    Pour les tables de commandes, ajoute le nombre de demandes NORTH décidées
    et parties d'après le journal du moteur (tout le journal) : un écart avec
    le nombre de lignes de la table montre des écritures en retard ou perdues
    (le contrat ne compte une commande acceptée que si elle est dans la table).

    :param options: Options de la ligne de commande.
    :param config: Configuration (``tables`` : lien de :func:`tables_direct.nouveau_lien`).
    :param requete: Paramètres de l'URL : ``nom`` (table), ``n`` (nombre de lignes, défaut 50).
    :return: Réponse de :func:`tables_direct.lire_table` plus ``journal`` (``{"decidees", "parties"}`` ou ``None``).
    """
    try:
        n = int(requete.get("n", 50))
    except ValueError:
        n = 50
    nom = requete.get("nom", "")
    reponse = tables_direct.lire_table(config["tables"], nom, n)
    reponse["journal"] = None
    if nom in ("TRACABILITE_COMMANDE", "LOG_NORTH_ARCHIVAGE_COMMANDE"):
        sources = audit.charger_sources(options.journal, options.demandes)
        maintenant, _ = heure_reference(sources, options.horloge)
        statuts = [s for s in audit.statuts_demandes(sources["evenements"], sources["demandes"], maintenant).values()
                   if not s["hors_perimetre"] and s["h_decision"] is not None]
        reponse["journal"] = {"decidees": len(statuts), "parties": sum(1 for s in statuts if s["statut"] == "partie")}
    return reponse


def rapport_sql(options: argparse.Namespace, requete: dict) -> dict:
    """Derniers événements du journal SQL pour ``/api/sql``.

    :param options: Options de la ligne de commande (``logs``).
    :param requete: Paramètres de l'URL : ``n`` (nombre, défaut 100, au plus 1000), ``erreurs=1`` (erreurs seulement).
    :return: ``{"evenements" (du plus récent au plus ancien), "erreurs", "ok", "fichier"}``.
    """
    try:
        nombre = max(1, min(1000, int(requete.get("n", 100))))
    except ValueError:
        nombre = 100
    chemin = os.path.join(options.logs, journalisation.FICHIER_SQL)
    evenements = journalisation.lire_fin_jsonl(chemin, 2000)
    erreurs = sum(1 for e in evenements if e.get("statut") == "erreur")
    if requete.get("erreurs") == "1":
        evenements = [e for e in evenements if e.get("statut") == "erreur"]
    return {"evenements": list(reversed(evenements[-nombre:])), "erreurs": erreurs, "ok": len(evenements) - erreurs
            if requete.get("erreurs") != "1" else None, "fichier": os.path.abspath(chemin)}


def rapport_debug(options: argparse.Namespace, config: dict) -> dict:
    """Informations de débogage pour ``/api/debug`` (seulement avec ``--debug``).

    :param options: Options de la ligne de commande.
    :param config: Configuration (``requetes`` = dernières requêtes HTTP).
    :return: Processus, options, temps de réponse, derniers problèmes de tous les services.
    """
    return {"pid": os.getpid(), "python": platform.python_version(), "machine": platform.node(),
            "debut": config["debut"].isoformat(timespec="seconds"),
            "options": {cle: valeur for cle, valeur in vars(options).items()},
            "requetes": list(reversed(config["requetes"])),
            "problemes": journalisation.derniers_problemes(options.logs, 50)}


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


def fichier_flotte(options: argparse.Namespace) -> str:
    """Chemin de l'état de la flotte écrit par le moteur (R-ROB-07), lu pour N_F / N_S et les PTAC.

    :param options: Options de la ligne de commande.
    :return: ``--flotte``, sinon ``<run>/flotte_moteur.json``.
    """
    return options.flotte or os.path.join(options.run, "flotte_moteur.json")


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
                             config["cibles"], config["taux_horaire"], audit.charger_flotte(fichier_flotte(options)))
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
    if url.path == "/api/sante":
        return reponse_http("200 OK", "application/json; charset=utf-8",
                            json.dumps(rapport_sante(options), ensure_ascii=False, default=str).encode("utf-8"))
    if url.path == "/api/tables":
        corps = {"tables": tables_direct.liste_tables(), "actif": config["tables"]["actif"],
                 "sante": config["tables"]["sante"]}
        return reponse_http("200 OK", "application/json; charset=utf-8",
                            json.dumps(corps, ensure_ascii=False, default=str).encode("utf-8"))
    if url.path == "/api/table":
        return reponse_http("200 OK", "application/json; charset=utf-8",
                            json.dumps(rapport_table(options, config, requete), ensure_ascii=False, default=str).encode("utf-8"))
    if url.path == "/api/carte":
        sources = audit.charger_sources(options.journal, options.demandes)
        maintenant, _ = heure_reference(sources, options.horloge)
        corps = audit.carte_camions(audit.charger_flotte(fichier_flotte(options)), sources["evenements"], maintenant)
        return reponse_http("200 OK", "application/json; charset=utf-8", json.dumps(corps, ensure_ascii=False).encode("utf-8"))
    if url.path == "/api/sql":
        return reponse_http("200 OK", "application/json; charset=utf-8",
                            json.dumps(rapport_sql(options, requete), ensure_ascii=False, default=str).encode("utf-8"))
    if url.path == "/api/debug" and options.debug:
        return reponse_http("200 OK", "application/json; charset=utf-8",
                            json.dumps(rapport_debug(options, config), ensure_ascii=False, default=str).encode("utf-8"))
    if url.path == "/api/statuts.csv":
        _, sources, maintenant = calculer_rapport(options, config, requete)
        nom = "statuts_NORTH_%s.csv" % datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return reponse_http("200 OK", "text/csv; charset=utf-8", audit.statuts_csv(sources, maintenant, audit.charger_flotte(fichier_flotte(options))).encode("utf-8"),
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
    JOURNAL.info("tableau de bord : http://%s:%d/  ('Ctrl+C' pour arrêter)%s", adresse, options.port,
                 " ; MODE DÉBOGAGE (/api/debug ouvert)" if options.debug else "")
    JOURNAL.info("journal : %s ; demandes : %s", os.path.abspath(options.journal), os.path.abspath(options.demandes))
    JOURNAL.info("cibles : %s ; données confidentielles : %s", config["cibles"],
                 "affichées" if config["seuil"] is not None else "masquées")
    try:
        while True:
            connexion, client = serveur.accept()
            connexion.settimeout(5)
            debut, ligne, statut = time.monotonic(), "", "-"
            try:
                entete = lire_entete(connexion)
                if entete:
                    ligne = entete.split("\r\n", 1)[0]
                    reponse = traiter_requete(ligne, options, config)
                    statut = reponse[9:12].decode("ascii", errors="replace")
                    connexion.sendall(reponse)
            except Exception as erreur:  # le serveur doit survivre à une requête en erreur
                statut = "500"
                JOURNAL.error("erreur pour %s (%s) : %r", client[0], ligne, erreur, exc_info=True)
                try:
                    connexion.sendall(reponse_http("500 Internal Server Error", "text/plain; charset=utf-8",
                                                   ("erreur : %r" % erreur).encode("utf-8")))
                except OSError:
                    pass
            finally:
                connexion.close()
                if ligne:
                    duree = journalisation.duree_ms(debut)
                    config["requetes"] = (config["requetes"] + [{"h": datetime.datetime.now().isoformat(timespec="seconds"),
                                                                 "client": client[0], "requete": ligne[:200],
                                                                 "statut": statut, "duree_ms": duree}])[-NB_REQUETES_GARDEES:]
                    JOURNAL.debug("%s %s -> %s en %.0f ms", client[0], ligne[:200], statut, duree)
    except KeyboardInterrupt:
        JOURNAL.info("arrêt du tableau de bord")
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
    score, cibles = rapport["score"], rapport["cibles"]
    print("Cibles du jour : TA %s (%s), TR %s (%s, q = %s demandes/h)"
          % (pourcent(cibles["ta"]["valeur"]), cibles["ta"]["source"], pourcent(cibles["tr"]["valeur"]),
             cibles["tr"]["source"], "—" if cibles["tr"]["debit"] is None else "%.0f" % cibles["tr"]["debit"]))
    print("Note : %s / %d mesurés (TR %s, gammes %s, TA %s ; professionnalisme noté par le Contrôle)"
          % ("—" if score["total"] is None else "%.1f" % score["total"], score["max_mesure"],
             *("—" if score["notes"][k] is None else "%.1f" % score["notes"][k] for k in ("tr", "gammes", "ta"))))
    for controle in rapport["controles"]:
        print("  [%s] %s : %d" % ("OK" if controle["ok"] else "KO", controle["libelle"], controle["n"]))
    print("Rapport : %s" % os.path.abspath(options.instantane))
    return rapport


if __name__ == "__main__":
    arguments = lire_parametres()
    journalisation.configurer("tableau", arguments.debug, arguments.logs, fichiers=not arguments.instantane)
    robustesse.arreter_sur_sigterm()  # R-ROB-03 : « nord.sh arreter » envoie SIGTERM
    configuration = preparer_config(arguments)
    if arguments.instantane:
        ecrire_instantane(arguments, configuration)
    else:
        servir(arguments, configuration)
