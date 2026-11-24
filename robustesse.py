"""Garde-fous d'exploitation communs au collecteur, au moteur et au tableau de bord.

Sur la VM, une erreur passagère (base injoignable, ligne illisible, disque
plein un instant) ne doit pas arrêter un service : un moteur relancé perd son
état en mémoire et ses commandes acceptées passent expirées (``REF-DELAI``),
un collecteur arrêté perd les messages de l'ERP pour toujours. Ce module
regroupe les deux outils qu'ils partagent :

- :func:`arreter_sur_sigterm` : ``nord.sh arreter`` envoie SIGTERM ; sans ce
  gestionnaire, Python s'arrête sans exécuter les blocs ``finally``
  (règle R-ROB-03) ;
- :func:`signaler` : journalise une erreur répétée au plus une fois par minute
  réelle et par clé, pour ne pas noyer les logs quand la base reste en panne
  (règles R-ROB-01 et R-ROB-02) ; les messages masqués sont comptés et annoncés
  au message suivant, et écrits au niveau ``DEBUG`` (jamais perdus en mode débogage) ;
- :func:`nouvel_etat_bdd`, :func:`noter_succes_bdd`, :func:`noter_echec_bdd` : santé
  d'un lien avec la base (règle R-ROB-06) : une écriture en échec n'est jamais
  silencieuse, elle est comptée, datée, journalisée en erreur, visible dans
  ``sante_moteur.json`` et sur le tableau de bord, et son rétablissement est annoncé.
"""

import datetime
import logging
import signal

import journalisation

#: Délai minimal (temps réel) entre deux messages d'une même clé.
DELAI_MESSAGES = datetime.timedelta(minutes=1)


def _interrompre(numero: int, cadre: object) -> None:
    """Gestionnaire de SIGTERM : lève ``KeyboardInterrupt`` comme un ``Ctrl+C``.

    :param numero: Numéro du signal reçu.
    :param cadre: Cadre d'exécution interrompu (inutilisé).
    :raises KeyboardInterrupt: Toujours, pour passer par les blocs ``except KeyboardInterrupt`` et ``finally``.
    """
    raise KeyboardInterrupt("signal %d" % numero)


def arreter_sur_sigterm() -> None:
    """Traite SIGTERM comme ``Ctrl+C`` dans le processus courant.

    Applique la règle R-ROB-03 : à l'arrêt demandé par ``nord.sh``, le
    moteur fait sa dernière synchronisation des tables ``LOG_NORTH_*`` et affiche
    son résumé, le collecteur et le tableau de bord s'arrêtent proprement.
    """
    signal.signal(signal.SIGTERM, _interrompre)


def signaler(messages: dict, cle: str, maintenant: datetime.datetime, texte: str, niveau: int = logging.WARNING,
             module: str = "robustesse") -> bool:
    """Journalise une erreur, au plus une fois par :data:`DELAI_MESSAGES` pour une même clé.

    Les erreurs sont comptées même quand elles ne sont pas affichées :
    ``messages["nb"]`` donne le total, repris dans le résumé à l'arrêt ; le
    nombre de messages masqués d'une clé est ajouté au message suivant de
    cette clé, et chaque message masqué est écrit au niveau ``DEBUG``.

    :param messages: ``{clé: dernière heure d'affichage, "nb": total, "masques": {clé: nombre}}``, modifié sur place.
    :param cle: Famille d'erreur (``"referentiel"``, ``"demande"``, ``"tournee"``…).
    :param maintenant: Heure réelle courante.
    :param texte: Message à journaliser.
    :param niveau: Niveau ``logging`` (``WARNING`` par défaut, ``ERROR`` pour une écriture perdue).
    :param module: Module émetteur (enregistreur ``nord.<module>``).
    :return: ``True`` si le message a été journalisé au niveau demandé.
    """
    messages["nb"] = messages.get("nb", 0) + 1
    masques = messages.setdefault("masques", {})
    enregistreur = journalisation.journal(module)
    dernier = messages.get(cle)
    if dernier is not None and maintenant - dernier < DELAI_MESSAGES:
        masques[cle] = masques.get(cle, 0) + 1
        enregistreur.debug("(masqué) %s", texte)
        return False
    messages[cle] = maintenant
    nombre = masques.pop(cle, 0)
    if nombre:
        texte += " (%d message(s) semblable(s) masqué(s) depuis le précédent)" % nombre
    enregistreur.log(niveau, texte)
    return True


def nouvel_etat_bdd(actif: bool = True) -> dict:
    """État de santé vide d'un lien avec la base.

    :param actif: ``False`` si le lien n'est pas utilisé dans ce lancement.
    :return: ``{"actif", "connecte", "dernier_succes", "derniere_erreur", "h_erreur", "echecs",
        "depuis", "ecrites", "rejetees", "en_attente"}`` ; ``echecs`` = échecs consécutifs.
    """
    return {"actif": actif, "connecte": False, "dernier_succes": None, "derniere_erreur": None, "h_erreur": None,
            "echecs": 0, "depuis": None, "ecrites": 0, "rejetees": 0, "en_attente": 0}


def noter_succes_bdd(sante: dict, maintenant: datetime.datetime, ecrites: int = 0, nom: str = "base") -> None:
    """Note une opération réussie ; annonce le rétablissement après des échecs.

    Applique la règle R-ROB-06.

    :param sante: État créé par :func:`nouvel_etat_bdd`, modifié sur place.
    :param maintenant: Heure réelle courante.
    :param ecrites: Lignes écrites par l'opération.
    :param nom: Nom du lien dans les messages (``archivage``, ``traçabilité Contrôle``, ``référentiel``).
    """
    if sante["echecs"]:
        journalisation.journal("robustesse").warning(
            "%s rétabli après %d échec(s) consécutif(s) depuis %s ; %d ligne(s) écrite(s) maintenant",
            nom, sante["echecs"], sante["depuis"], ecrites)
    sante.update({"connecte": True, "dernier_succes": maintenant.strftime("%Y-%m-%d %H:%M:%S"), "echecs": 0,
                  "depuis": None, "en_attente": 0})
    sante["ecrites"] += ecrites


def noter_echec_bdd(sante: dict, maintenant: datetime.datetime, erreur: object, en_attente: int = 0) -> None:
    """Note une opération en échec (règle R-ROB-06).

    :param sante: État créé par :func:`nouvel_etat_bdd`, modifié sur place.
    :param maintenant: Heure réelle courante.
    :param erreur: Exception ou texte.
    :param en_attente: Lignes qui restent à écrire.
    """
    texte = journalisation.texte_erreur(erreur) if isinstance(erreur, BaseException) else str(erreur)
    if not sante["echecs"]:
        sante["depuis"] = maintenant.strftime("%Y-%m-%d %H:%M:%S")
    sante.update({"connecte": False, "derniere_erreur": texte, "h_erreur": maintenant.strftime("%Y-%m-%d %H:%M:%S"),
                  "en_attente": en_attente})
    sante["echecs"] += 1
