"""Garde-fous d'exploitation communs au collecteur, au moteur et au tableau de bord.

Sur la VM, une erreur passagère (base injoignable, ligne illisible, disque
plein un instant) ne doit pas arrêter un service : un moteur relancé perd son
état en mémoire et ses commandes acceptées passent expirées (``REF-DELAI``),
un collecteur arrêté perd les messages de l'ERP pour toujours. Ce module
regroupe les deux outils qu'ils partagent :

- :func:`arreter_sur_sigterm` : ``nord.sh arreter`` envoie SIGTERM ; sans ce
  gestionnaire, Python s'arrête sans exécuter les blocs ``finally``
  (règle R-ROB-03) ;
- :func:`signaler` : affiche une erreur répétée au plus une fois par minute
  réelle et par clé, pour ne pas noyer les logs quand la base reste en panne
  (règles R-ROB-01 et R-ROB-02).
"""

import datetime
import signal

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
    moteur fait sa dernière synchronisation des tables ``*_NORTH`` et affiche
    son résumé, le collecteur et le tableau de bord s'arrêtent proprement.
    """
    signal.signal(signal.SIGTERM, _interrompre)


def signaler(messages: dict, cle: str, maintenant: datetime.datetime, texte: str) -> bool:
    """Affiche une erreur, au plus une fois par :data:`DELAI_MESSAGES` pour une même clé.

    Les erreurs sont comptées même quand elles ne sont pas affichées :
    ``messages["nb"]`` donne le total, repris dans le résumé à l'arrêt.

    :param messages: ``{clé: dernière heure d'affichage, "nb": total}``, modifié sur place.
    :param cle: Famille d'erreur (``"referentiel"``, ``"demande"``, ``"tournee"``…).
    :param maintenant: Heure réelle courante.
    :param texte: Message à afficher.
    :return: ``True`` si le message a été affiché.
    """
    messages["nb"] = messages.get("nb", 0) + 1
    dernier = messages.get(cle)
    if dernier is not None and maintenant - dernier < DELAI_MESSAGES:
        return False
    messages[cle] = maintenant
    print("[%s] ! %s" % (maintenant.strftime("%H:%M:%S"), texte), flush=True)
    return True
