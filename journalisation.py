"""Journalisation commune aux services NORTH : console, fichiers JSON, événements SQL et santé.

Chaque service (collecteur, moteur, tableau de bord) appelle :func:`configurer`
dans son ``__main__``. Les modules écrivent ensuite par :func:`journal`
(bibliothèque standard ``logging``, enregistreur ``nord.<module>``) :

- **console** (sortie standard, que ``nord.sh`` copie dans ``../donnees/logs/<service>.log``) :
  une ligne courte ``[hh:mm:ss] message`` ; ``! `` devant un avertissement, ``!! `` devant une erreur ;
- **fichier JSON** ``../donnees/logs/<service>.jsonl`` : une ligne JSON par message
  (heure à la milliseconde, niveau, module, PID, trace de l'exception), avec rotation
  (:data:`TAILLE_MAX` octets, :data:`NB_ARCHIVES` fichiers gardés) ;
- **journal SQL** ``../donnees/logs/sql.jsonl`` (moteur seulement, ``sql=True``) : un événement par
  opération sur la base (connexion, lecture du référentiel, écriture d'un lot, ligne rejetée)
  avec table, nombre de lignes, durée et erreur ; le tableau de bord l'affiche (:func:`evenement_sql`).
  Il ne contient aucune valeur de ligne : seulement des identifiants et des messages d'erreur ;
- **santé** ``../donnees/run/sante_<service>.json`` : état courant du service (dernier tick, base,
  écritures en attente), réécrit à chaque tick (:func:`ecrire_sante`), lu par le tableau de bord
  et par ``nord.sh etat``.

Mode débogage (``--debug``) : niveau ``DEBUG`` partout, et la console affiche aussi
les événements SQL réussis, le module et les millisecondes.

Sans :func:`configurer` (tests, import depuis un autre script), les messages ne
sont affichés nulle part : l'enregistreur ``nord`` n'a qu'un ``NullHandler``.

Utilisation en ligne de commande, depuis ``1 - DEV/`` (lecture seule) :

.. code-block:: bash

    python3 journalisation.py --sante          # santé de chaque service
    python3 journalisation.py --sql 30         # 30 derniers événements SQL
    python3 journalisation.py --erreurs 20     # 20 derniers avertissements et erreurs, tous services
"""

import argparse
import datetime
import glob
import json
import logging
import logging.handlers
import os
import sys
import time

DOSSIER = os.path.dirname(os.path.abspath(__file__))

#: Dossier des journaux (le même que ``nord.sh``).
REPERTOIRE_LOGS = os.path.join(DOSSIER, "..", "donnees", "logs")

#: Dossier des fichiers d'état des services (PID de ``nord.sh``, santé).
REPERTOIRE_RUN = os.path.join(DOSSIER, "..", "donnees", "run")

#: Nom du journal des événements SQL.
FICHIER_SQL = "sql.jsonl"

#: Taille maximale d'un fichier JSON avant rotation, en octets.
TAILLE_MAX = 10 * 1024 * 1024

#: Nombre d'anciens fichiers gardés par la rotation.
NB_ARCHIVES = 5

#: Enregistreur racine de nos modules.
RACINE = "nord"

#: Enregistreur des événements SQL.
NOM_SQL = RACINE + ".sql"

#: Champs propres aux événements SQL, recopiés dans la ligne JSON.
CHAMPS_SQL = ("operation", "table", "lignes", "duree_ms", "statut", "erreur")

#: Longueur maximale d'un message d'erreur de la base dans les journaux.
LONGUEUR_ERREUR = 500

logging.getLogger(RACINE).addHandler(logging.NullHandler())

#: Configuration courante (service, mode débogage, dossier), remplie par :func:`configurer`.
CONFIG = {"service": None, "debug": False, "repertoire": None}


class FormatJson(logging.Formatter):
    """Met chaque message sur une ligne JSON (fichiers ``*.jsonl``)."""

    def __init__(self, service: str) -> None:
        """Retient le nom du service écrit dans chaque ligne.

        :param service: Nom du service (``moteur``, ``collecteur``, ``tableau``).
        """
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        """Construit la ligne JSON d'un message.

        :param record: Message.
        :return: Ligne JSON, sans retour à la ligne.
        """
        ligne = {"h": datetime.datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
                 "niveau": record.levelname, "service": self.service, "module": record.name,
                 "message": record.getMessage(), "pid": record.process}
        for champ in CHAMPS_SQL:
            if hasattr(record, champ):
                ligne[champ] = getattr(record, champ)
        if record.exc_info:
            ligne["trace"] = self.formatException(record.exc_info)
        return json.dumps(ligne, ensure_ascii=False, default=str)


class FormatConsole(logging.Formatter):
    """Ligne courte pour la console ; détaillée en mode débogage."""

    def __init__(self, debug: bool) -> None:
        """Choisit le format.

        :param debug: Mode débogage (millisecondes, niveau et module affichés).
        """
        super().__init__()
        self.debug = debug

    def format(self, record: logging.LogRecord) -> str:
        """Construit la ligne affichée.

        :param record: Message.
        :return: Ligne de console (avec la trace de l'exception éventuelle).
        """
        heure = datetime.datetime.fromtimestamp(record.created)
        if self.debug:
            texte = "[%s] %-7s %s : %s" % (heure.strftime("%H:%M:%S.%f")[:-3], record.levelname, record.name,
                                           record.getMessage())
        else:
            marque = "!! " if record.levelno >= logging.ERROR else "! " if record.levelno >= logging.WARNING else ""
            texte = "[%s] %s%s" % (heure.strftime("%H:%M:%S"), marque, record.getMessage())
        if record.exc_info:
            texte += "\n" + self.formatException(record.exc_info)
        return texte


class FiltreSql(logging.Filter):
    """Écarte les événements SQL réussis de la console et du journal du service (ils ont leur fichier)."""

    def __init__(self, garder_sql: bool) -> None:
        """Choisit le comportement.

        :param garder_sql: ``True`` pour tout laisser passer (console en mode débogage).
        """
        super().__init__()
        self.garder_sql = garder_sql

    def filter(self, record: logging.LogRecord) -> bool:
        """Dit si le message passe.

        :param record: Message.
        :return: ``True`` sauf pour un événement SQL sous le niveau ``WARNING`` quand ``garder_sql`` est faux.
        """
        return self.garder_sql or record.name != NOM_SQL or record.levelno >= logging.WARNING


def _fichier_rotatif(chemin: str, format_ligne: logging.Formatter) -> logging.Handler:
    """Crée un gestionnaire de fichier avec rotation.

    :param chemin: Fichier à écrire.
    :param format_ligne: Format des lignes.
    :return: Gestionnaire.
    """
    gestionnaire = logging.handlers.RotatingFileHandler(chemin, maxBytes=TAILLE_MAX, backupCount=NB_ARCHIVES,
                                                        encoding="utf-8")
    gestionnaire.setFormatter(format_ligne)
    return gestionnaire


def configurer(service: str, debug: bool = False, repertoire: str = None, sql: bool = False,
               fichiers: bool = True) -> logging.Logger:
    """Installe la console et les fichiers de journal du service (à appeler dans ``__main__``).

    Peut être rappelée (les gestionnaires précédents sont retirés). Si le
    dossier des journaux n'est pas inscriptible, le service continue avec la
    console seule et le signale : un journal en panne ne doit pas arrêter un
    service (règle R-ROB-01). Les exceptions non rattrapées sont journalisées
    au niveau ``CRITICAL`` avant l'arrêt.

    :param service: Nom du service (nom des fichiers ``<service>.jsonl`` et ``sante_<service>.json``).
    :param debug: Mode débogage : niveau ``DEBUG`` et événements SQL réussis à la console.
    :param repertoire: Dossier des journaux (défaut : :data:`REPERTOIRE_LOGS`).
    :param sql: Écrit aussi le journal SQL :data:`FICHIER_SQL` (un seul service doit l'écrire : le moteur).
    :param fichiers: ``False`` pour la console seule (outils en ligne de commande).
    :return: Enregistreur racine ``nord``.
    """
    repertoire = repertoire or REPERTOIRE_LOGS
    racine = logging.getLogger(RACINE)
    for gestionnaire in list(racine.handlers):
        racine.removeHandler(gestionnaire)
        gestionnaire.close()
    for gestionnaire in list(logging.getLogger(NOM_SQL).handlers):
        logging.getLogger(NOM_SQL).removeHandler(gestionnaire)
        gestionnaire.close()
    racine.setLevel(logging.DEBUG if debug else logging.INFO)
    racine.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(FormatConsole(debug))
    console.addFilter(FiltreSql(debug))
    racine.addHandler(console)

    probleme = None
    if fichiers:
        try:
            os.makedirs(repertoire, exist_ok=True)
            fichier = _fichier_rotatif(os.path.join(repertoire, service + ".jsonl"), FormatJson(service))
            fichier.addFilter(FiltreSql(False))
            racine.addHandler(fichier)
            if sql:
                logging.getLogger(NOM_SQL).addHandler(_fichier_rotatif(os.path.join(repertoire, FICHIER_SQL),
                                                                       FormatJson(service)))
        except OSError as erreur:  # disque plein, droits : la console suffit pour continuer
            probleme = erreur
    CONFIG.update({"service": service, "debug": debug, "repertoire": repertoire})

    def journaliser_exception(type_erreur: type, erreur: BaseException, trace: object) -> None:
        """Journalise une exception non rattrapée (sauf Ctrl+C) puis laisse Python l'afficher.

        :param type_erreur: Classe de l'exception.
        :param erreur: Exception.
        :param trace: Pile d'appels.
        """
        if not issubclass(type_erreur, KeyboardInterrupt):
            racine.critical("arrêt sur exception non rattrapée : %r", erreur, exc_info=(type_erreur, erreur, trace))
        sys.__excepthook__(type_erreur, erreur, trace)

    sys.excepthook = journaliser_exception
    if probleme is not None:
        racine.warning("journaux fichiers indisponibles dans %s (%s) : console seule", os.path.abspath(repertoire), probleme)
    racine.debug("journalisation de %s : niveau %s, fichiers dans %s", service, "DEBUG" if debug else "INFO",
                 os.path.abspath(repertoire) if fichiers else "-")
    return racine


def journal(nom: str) -> logging.Logger:
    """Enregistreur d'un module.

    :param nom: Nom court du module (``moteur``, ``archivage``…).
    :return: Enregistreur ``nord.<nom>``.
    """
    return logging.getLogger(RACINE + "." + nom)


def texte_erreur(erreur: BaseException) -> str:
    """Message d'une exception sur une seule ligne, tronqué.

    :param erreur: Exception (psycopg2 met souvent un ``DETAIL`` sur une seconde ligne).
    :return: Message lisible, au plus :data:`LONGUEUR_ERREUR` caractères.
    """
    texte = " | ".join(morceau.strip() for morceau in str(erreur).strip().splitlines() if morceau.strip())
    texte = texte or type(erreur).__name__
    return texte[:LONGUEUR_ERREUR]


def duree_ms(debut: float) -> float:
    """Millisecondes écoulées depuis un instant de ``time.monotonic()``.

    :param debut: Instant de départ.
    :return: Durée en millisecondes, arrondie au dixième.
    """
    return round(1000 * (time.monotonic() - debut), 1)


def evenement_sql(operation: str, table: str, lignes: int = 0, duree: float = None, erreur: object = None,
                  niveau: int = None) -> None:
    """Écrit un événement dans le journal SQL.

    Un événement en erreur passe aussi dans la console et le journal du
    service (niveau ``ERROR`` par défaut). Aucune valeur de ligne n'est écrite.

    :param operation: ``CONNEXION``, ``LECTURE``, ``UPSERT``, ``INSERT``, ``UPDATE``, ``REPRISE``, ``VERIFICATION``…
    :param table: Table(s) concernée(s), ``-`` si aucune.
    :param lignes: Nombre de lignes lues ou écrites.
    :param duree: Durée en millisecondes (:func:`duree_ms`).
    :param erreur: Exception ou texte d'erreur ; ``None`` si l'opération a réussi.
    :param niveau: Niveau ``logging`` (défaut : ``INFO`` si réussi, ``ERROR`` sinon).
    """
    texte = texte_erreur(erreur) if isinstance(erreur, BaseException) else (str(erreur)[:LONGUEUR_ERREUR] if erreur else None)
    if niveau is None:
        niveau = logging.INFO if texte is None else logging.ERROR
    message = "SQL %s %s : %d ligne(s)%s%s" % (operation, table, lignes, "" if duree is None else " en %.0f ms" % duree,
                                              "" if texte is None else " ; ERREUR " + texte)
    logging.getLogger(NOM_SQL).log(niveau, message, extra={"operation": operation, "table": table, "lignes": lignes,
                                                           "duree_ms": duree, "statut": "ok" if texte is None else "erreur",
                                                           "erreur": texte})


def lire_fin_jsonl(chemin: str, nombre: int = 200, octets: int = 512 * 1024) -> list:
    """Lit les dernières lignes d'un fichier JSON (lignes illisibles ignorées).

    :param chemin: Fichier ``.jsonl``.
    :param nombre: Nombre maximal de lignes renvoyées.
    :param octets: Quantité lue à la fin du fichier.
    :return: Dictionnaires, du plus ancien au plus récent ; liste vide si le fichier n'existe pas.
    """
    if not os.path.exists(chemin):
        return []
    with open(chemin, "rb") as fichier:
        fichier.seek(0, os.SEEK_END)
        taille = fichier.tell()
        fichier.seek(max(0, taille - octets))
        brut = fichier.read().decode("utf-8", errors="replace").splitlines()
    if taille > octets and brut:
        brut = brut[1:]  # première ligne probablement coupée
    lignes = []
    for texte in brut[-nombre:]:
        try:
            lignes.append(json.loads(texte))
        except ValueError:
            continue
    return lignes


def ecrire_sante(service: str, donnees: dict, repertoire: str = None) -> bool:
    """Écrit l'état de santé d'un service (fichier remplacé d'un coup, jamais à moitié écrit).

    Ne lève jamais d'erreur : un disque plein ne doit pas arrêter le service ;
    l'échec est journalisé au niveau ``DEBUG``.

    :param service: Nom du service.
    :param donnees: État (sérialisable en JSON ; les dates sont converties en texte).
    :param repertoire: Dossier (défaut : :data:`REPERTOIRE_RUN`).
    :return: ``True`` si le fichier a été écrit.
    """
    repertoire = repertoire or REPERTOIRE_RUN
    chemin = os.path.join(repertoire, "sante_%s.json" % service)
    try:
        os.makedirs(repertoire, exist_ok=True)
        contenu = dict(donnees, service=service, pid=os.getpid(),
                       ecrit_a=datetime.datetime.now().isoformat(timespec="seconds"))
        with open(chemin + ".tmp", "w", encoding="utf-8") as fichier:
            json.dump(contenu, fichier, ensure_ascii=False, default=str, indent=1)
        os.replace(chemin + ".tmp", chemin)
        return True
    except (OSError, TypeError, ValueError) as erreur:
        journal("sante").debug("santé de %s non écrite : %r", service, erreur)
        return False


def lire_sante(repertoire: str = None) -> dict:
    """Lit l'état de santé de chaque service.

    :param repertoire: Dossier (défaut : :data:`REPERTOIRE_RUN`).
    :return: ``{service: état}`` ; chaque état reçoit ``age_s``, secondes depuis sa dernière écriture.
    """
    repertoire = repertoire or REPERTOIRE_RUN
    resultat = {}
    for chemin in sorted(glob.glob(os.path.join(repertoire, "sante_*.json"))):
        try:
            with open(chemin, encoding="utf-8") as fichier:
                etat = json.load(fichier)
            etat["age_s"] = round(time.time() - os.path.getmtime(chemin), 1)
        except (OSError, ValueError):
            continue
        resultat[etat.get("service") or os.path.basename(chemin)[6:-5]] = etat
    return resultat


def derniers_problemes(repertoire: str = None, nombre: int = 50) -> list:
    """Derniers avertissements et erreurs de tous les services, du plus récent au plus ancien.

    :param repertoire: Dossier des journaux (défaut : :data:`REPERTOIRE_LOGS`).
    :param nombre: Nombre maximal de lignes.
    :return: Lignes JSON de niveau ``WARNING`` ou plus.
    """
    repertoire = repertoire or REPERTOIRE_LOGS
    lignes = []
    for chemin in glob.glob(os.path.join(repertoire, "*.jsonl")):
        if os.path.basename(chemin) == FICHIER_SQL:
            continue
        lignes += [l for l in lire_fin_jsonl(chemin, 2000) if l.get("niveau") in ("WARNING", "ERROR", "CRITICAL")]
    lignes.sort(key=lambda l: l.get("h", ""), reverse=True)
    return lignes[:nombre]


def resume_sante(sante: dict) -> list:
    """Résumé lisible de la santé des services (pour ``nord.sh etat``).

    :param sante: Résultat de :func:`lire_sante`.
    :return: Lignes de texte.
    """
    lignes = []
    for service, etat in sante.items():
        lignes.append("%s : %s (écrit il y a %.0f s)" % (service, etat.get("statut", "?"), etat.get("age_s", 0)))
        for nom in ("referentiel", "archivage", "controle", "evenements"):
            bdd = etat.get(nom)
            if not isinstance(bdd, dict) or not bdd.get("actif", True):
                continue
            ligne = "  %-11s %s ; dernier succès %s ; %d ligne(s) écrite(s), %d en attente, %d rejetée(s)" % (
                nom, "EN ÉCHEC (%d essai(s))" % bdd["echecs"] if bdd.get("echecs") else "ok",
                bdd.get("dernier_succes") or "jamais", bdd.get("ecrites", 0), bdd.get("en_attente", 0),
                bdd.get("rejetees", 0))
            lignes.append(ligne)
            if bdd.get("echecs") and bdd.get("derniere_erreur"):
                lignes.append("              erreur : %s" % bdd["derniere_erreur"])
    return lignes or ["aucun fichier de santé dans %s" % os.path.abspath(REPERTOIRE_RUN)]


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Journaux et santé des services NORTH (lecture seule)")
    parseur.add_argument("--sante", action="store_true", help="santé de chaque service")
    parseur.add_argument("--sql", type=int, default=None, metavar="N", help="N derniers événements SQL")
    parseur.add_argument("--erreurs", type=int, default=None, metavar="N", help="N derniers avertissements et erreurs")
    options = parseur.parse_args()
    if options.sante:
        print("\n".join(resume_sante(lire_sante())))
    if options.sql:
        for evenement in lire_fin_jsonl(os.path.join(REPERTOIRE_LOGS, FICHIER_SQL), options.sql):
            print("%s %-5s %s" % (evenement.get("h", "?"), evenement.get("statut", "?"), evenement.get("message", "")))
    if options.erreurs:
        for ligne in reversed(derniers_problemes(nombre=options.erreurs)):
            print("%s %-8s %-10s %s" % (ligne.get("h", "?"), ligne.get("niveau"), ligne.get("service"), ligne.get("message")))
    if not (options.sante or options.sql or options.erreurs):
        parseur.print_help()
