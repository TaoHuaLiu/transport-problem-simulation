"""Lecture en direct de nos tables de la base, pour l'onglet « Tables » du tableau de bord.

Lit seulement (session PostgreSQL en lecture seule, ``SELECT`` uniquement) :

- nos tables ``LOG_NORTH_CAMION``, ``LOG_NORTH_TRAJET``, ``LOG_NORTH_ARCHIVAGE_COMMANDE``
  (écrites par :mod:`archivage`) ;
- nos lignes des tables du Contrôle ``TRACABILITE_COMMANDE`` et
  ``TRACABILITE_TRAJET`` (``ID_ENTRE`` = ``N1``, ``N2``…, écrites par
  :mod:`tracabilite_controle`) ; les lignes des autres groupes ne sont jamais lues.

Le serveur du tableau de bord ne traite qu'une requête à la fois : une base
lente ou injoignable ne doit pas le bloquer. Applique donc la règle R-ROB-02
(délais de :data:`referentiels.BDD` : connexion 5 s, requête 10 s), garde le
résultat :data:`CACHE_S` secondes et, après un échec, n'essaie plus pendant
:data:`PAUSE_APRES_ECHEC_S` secondes. Applique R-ROB-06 : santé du lien
(:func:`robustesse.noter_echec_bdd`) et journal SQL (:func:`journalisation.evenement_sql`).
"""

import datetime
import decimal
import time

import archivage
import hypotheses
import journalisation
import referentiels
import robustesse
import tracabilite_controle

JOURNAL = journalisation.journal("tableau")

#: Durée (secondes réelles) pendant laquelle une lecture est resservie sans interroger la base.
CACHE_S = 5

#: Après un échec, durée (secondes réelles) sans nouvel essai : le serveur reste réactif si la base ne répond pas.
PAUSE_APRES_ECHEC_S = 30

#: Nombre de lignes renvoyées au plus par table.
LIGNES_MAX = 200

#: Expression SQL du numéro de nos ``ID_ENTRE`` (``N12`` → 12, alias ``c``), pour trier les commandes du Contrôle.
_NUMERO = 'CAST(substring(c."ID_ENTRE" from %d) AS INTEGER)' % (len(hypotheses.PREFIXE_ENTREPRISE) + 1)

#: Tables lisibles : nom → colonnes, ``FROM`` (alias ``t``), filtre ``WHERE``, tri, colonne de regroupement.
#: Liste fermée : le nom de table reçu dans l'URL n'entre jamais tel quel dans le SQL.
TABLES = {
    "LOG_NORTH_ARCHIVAGE_COMMANDE": {
        "colonnes": archivage.COLONNES["LOG_NORTH_ARCHIVAGE_COMMANDE"],
        "depuis": '"LOG_NORTH_ARCHIVAGE_COMMANDE" t', "filtre": "", "tri": 't."H_DEP" DESC NULLS LAST',
        "groupe": "STATUT", "libelle": "nos commandes (archivage)"},
    "LOG_NORTH_TRAJET": {
        "colonnes": archivage.COLONNES["LOG_NORTH_TRAJET"],
        "depuis": '"LOG_NORTH_TRAJET" t', "filtre": "", "tri": 't."ID_TRAJET" DESC',
        "groupe": "STATUT", "libelle": "nos tournées (archivage)"},
    "LOG_NORTH_CAMION": {
        "colonnes": archivage.COLONNES["LOG_NORTH_CAMION"],
        "depuis": '"LOG_NORTH_CAMION" t', "filtre": "", "tri": 't."IMMAT_CAMI"',
        "groupe": "DISPO", "libelle": "notre flotte (archivage)"},
    "TRACABILITE_COMMANDE": {
        "colonnes": tracabilite_controle.COLONNES[tracabilite_controle.TABLE_COMMANDE],
        "depuis": '"TRACABILITE_COMMANDE" t CROSS JOIN LATERAL (SELECT t."ID_ENTRE") c',
        "filtre": 'c."ID_ENTRE" ~ %(motif)s', "tri": _NUMERO + " DESC",
        "groupe": "STATUT", "libelle": "nos lignes de la table du Contrôle"},
    "TRACABILITE_TRAJET": {
        "colonnes": tracabilite_controle.COLONNES[tracabilite_controle.TABLE_TRAJET],
        "depuis": '"TRACABILITE_TRAJET" t JOIN "TRACABILITE_COMMANDE" c ON c."ID_COMMANDE" = t."ID_COMMANDE"',
        "filtre": 'c."ID_ENTRE" ~ %(motif)s', "tri": _NUMERO + " DESC",
        "groupe": None, "libelle": "nos trajets dans la table du Contrôle"},
}


def nouveau_lien(actif: bool = True) -> dict:
    """État du lien de lecture : connexion, cache, santé.

    :param actif: ``False`` si l'onglet est désactivé (``--sans-tables``).
    :return: ``{"connexion", "cache", "pause_jusqu_a", "sante", "messages", "actif"}``.
    """
    return {"connexion": None, "cache": {}, "pause_jusqu_a": 0.0, "sante": robustesse.nouvel_etat_bdd(actif=actif),
            "messages": {}, "actif": actif}


def _connecter(lien: dict, connecteur=None) -> object:
    """Ouvre (ou réutilise) la connexion, en lecture seule et en autocommit.

    :param lien: Lien (:func:`nouveau_lien`).
    :param connecteur: Fonction ``connect(**parametres)`` (tests) ; défaut ``psycopg2.connect``.
    :type connecteur: callable
    :return: Connexion.
    """
    if lien["connexion"] is None:
        if connecteur is None:
            import psycopg2  # disponible sur la VM uniquement
            connecteur = psycopg2.connect
        connexion = connecteur(**referentiels.BDD)
        connexion.set_session(readonly=True, autocommit=True)  # aucune écriture possible par ce lien
        lien["connexion"] = connexion
    return lien["connexion"]


def _texte(valeur: object) -> object:
    """Rend une valeur de la base sérialisable en JSON.

    :param valeur: Valeur lue (date, décimal, texte, nombre, dict…).
    :return: Valeur JSON.
    """
    if isinstance(valeur, (datetime.datetime, datetime.date)):
        return valeur.strftime("%Y-%m-%d %H:%M:%S") if isinstance(valeur, datetime.datetime) else valeur.isoformat()
    if isinstance(valeur, decimal.Decimal):
        return float(valeur)
    if valeur is None or isinstance(valeur, (str, int, float, bool)):
        return valeur
    return str(valeur)


def _requetes(nom: str, n: int) -> tuple:
    """Construit les trois requêtes de lecture d'une table de :data:`TABLES`.

    :param nom: Nom de table (clé de :data:`TABLES`).
    :param n: Nombre de lignes voulu.
    :return: ``(lignes, total, groupes)`` : textes SQL (``groupes`` vaut ``None`` sans colonne de regroupement).
    """
    table = TABLES[nom]
    ou = " WHERE " + table["filtre"] if table["filtre"] else ""
    colonnes = ", ".join('t."%s"' % c for c in table["colonnes"])
    lignes = "SELECT %s FROM %s%s ORDER BY %s LIMIT %d" % (colonnes, table["depuis"], ou, table["tri"], n)
    total = "SELECT COUNT(*) FROM %s%s" % (table["depuis"], ou)
    groupes = ('SELECT t."%s", COUNT(*) FROM %s%s GROUP BY 1 ORDER BY 1' % (table["groupe"], table["depuis"], ou)
               if table["groupe"] else None)
    return lignes, total, groupes


def lire_table(lien: dict, nom: str, n: int = 50, connecteur=None) -> dict:
    """Lit les dernières lignes d'une de nos tables, sans jamais lever d'erreur.

    Applique les règles R-ROB-02 et R-ROB-06 (voir le module).

    :param lien: Lien (:func:`nouveau_lien`).
    :param nom: Table demandée ; refusée si elle n'est pas dans :data:`TABLES`.
    :param n: Nombre de lignes (borné à :data:`LIGNES_MAX`).
    :param connecteur: Fonction de connexion (tests).
    :type connecteur: callable
    :return: ``{"table", "ok", "colonnes", "lignes", "total", "groupes", "lu_a", "erreur", "cache"}``.
    """
    reponse = {"table": nom, "ok": False, "colonnes": [], "lignes": [], "total": None, "groupes": {},
               "lu_a": None, "erreur": None, "cache": False}
    if nom not in TABLES:
        reponse["erreur"] = "table inconnue (tables lisibles : %s)" % ", ".join(sorted(TABLES))
        return reponse
    if not lien["actif"]:
        reponse["erreur"] = "onglet désactivé (--sans-tables)"
        return reponse
    n = max(1, min(LIGNES_MAX, int(n)))
    instant = time.monotonic()
    deja = lien["cache"].get((nom, n))
    if deja is not None and instant - deja[0] < CACHE_S:
        return dict(deja[1], cache=True)
    if instant < lien["pause_jusqu_a"]:
        reponse["erreur"] = "base en échec, nouvel essai dans %d s : %s" % (
            lien["pause_jusqu_a"] - instant, lien["sante"].get("derniere_erreur"))
        return reponse
    maintenant = datetime.datetime.now()
    sql_lignes, sql_total, sql_groupes = _requetes(nom, n)
    parametres = {"motif": hypotheses.MOTIF_ID_ENTRE}
    try:
        curseur = _connecter(lien, connecteur).cursor()
        curseur.execute(sql_lignes, parametres)
        lignes = [[_texte(v) for v in ligne] for ligne in curseur.fetchall()]
        curseur.execute(sql_total, parametres)
        total = curseur.fetchone()[0]
        groupes = {}
        if sql_groupes:
            curseur.execute(sql_groupes, parametres)
            groupes = {str(cle): nombre for cle, nombre in curseur.fetchall()}
        curseur.close()
    except Exception as erreur:  # R-ROB-02 : base lente ou injoignable, psycopg2 absent… le serveur continue
        lien["pause_jusqu_a"] = instant + PAUSE_APRES_ECHEC_S
        try:
            if lien["connexion"] is not None:
                lien["connexion"].close()
        except Exception as fermeture:  # connexion déjà rompue
            JOURNAL.debug("fermeture impossible : %r", fermeture)
        lien["connexion"] = None
        robustesse.noter_echec_bdd(lien["sante"], maintenant, erreur)
        robustesse.signaler(lien["messages"], "tables", maintenant, "lecture de %s en échec (%r) : nouvel essai dans %d s"
                            % (nom, erreur, PAUSE_APRES_ECHEC_S), module="tableau")
        journalisation.evenement_sql("LECTURE", nom, 0, journalisation.duree_ms(instant), erreur)
        reponse["erreur"] = "%s : %s" % (type(erreur).__name__, erreur)
        return reponse
    robustesse.noter_succes_bdd(lien["sante"], maintenant, nom="lecture des tables")
    journalisation.evenement_sql("LECTURE", nom, len(lignes), journalisation.duree_ms(instant))
    reponse.update({"ok": True, "colonnes": TABLES[nom]["colonnes"], "lignes": lignes, "total": total,
                    "groupes": groupes, "lu_a": maintenant.strftime("%Y-%m-%d %H:%M:%S")})
    lien["cache"][(nom, n)] = (instant, reponse)
    return reponse


def liste_tables() -> list:
    """Tables lisibles, pour le menu de l'onglet.

    :return: Liste de ``{"nom", "libelle"}``.
    """
    return [{"nom": nom, "libelle": table["libelle"]} for nom, table in TABLES.items()]
