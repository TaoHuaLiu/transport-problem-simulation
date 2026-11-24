"""Interface avec les tables de traçabilité du groupe Contrôle.

Le Contrôle a spécifié deux tables partagées, créées dans la base
``UE_ENS_DCJC`` ; elles sont le seul canal de données entre groupes
(sujet). Ce module les remplit pour **nos commandes seulement** (périmètre
NORTH, règle R-CTL-01) :

- ``TRACABILITE_COMMANDE`` : une ligne par commande NORTH décidée, clé
  ``ID_COMMANDE`` ; ``ID_ENTRE`` = ``N1``, ``N2``… (lettre de la zone puis
  numéro de la commande chez nous, :func:`id_entre`) ; ``STATUT`` ``ACCEPTEE``
  ou ``REFUSEE`` (:data:`STATUTS_CONTROLE`) ; ``H_DEP_R`` = heure réelle de
  départ ; ``JUSTIF_REFUS`` = code de refus du Contrôle (table D-JUSTIF,
  :func:`code_justif`) ;
- ``TRACABILITE_TRAJET`` : une ligne par commande partie : camion, type,
  capacité, prix de la prestation, SPEC tenues, remplissage du trajet.

Il est séparé de :mod:`archivage` (nos propres tables ``LOG_NORTH_*``) : autre
connexion, autres règles. Le moteur l'appelle à chaque tick avec l'option
``--controle`` (imposée en mode audit) ; comme l'archivage, il n'envoie que
les lignes modifiées et ne lève jamais d'erreur (règle R-ROB-02) : base
injoignable ou table absente, le moteur continue et les lignes en retard
partent à la connexion suivante.

Les tables n'ont pas forcément de contrainte d'unicité exploitable (pas de
clé déclarée sur ``TRACABILITE_TRAJET``) : chaque ligne est écrite par un
``UPDATE`` limité à notre ligne (même ``ID_COMMANDE`` et même ``ID_ENTRE``),
suivi d'un ``INSERT`` si rien n'a été mis à jour.
Aucune ligne d'un autre groupe n'est lue pour être modifiée ni supprimée.

Utilisation, sur la VM (lecture seule) :

.. code-block:: bash

    python3 tracabilite_controle.py --verifier   # les deux tables existent-elles avec les colonnes attendues ?
    python3 tracabilite_controle.py --etat       # nos lignes par statut et par code de refus
"""

import argparse
import datetime
import json
import logging
import sys
import time

import archivage
import hypotheses
import journalisation
import planification
import referentiels
import regles
import robustesse

JOURNAL = journalisation.journal("controle")

TABLE_COMMANDE = "TRACABILITE_COMMANDE"
TABLE_TRAJET = "TRACABILITE_TRAJET"

#: Tables écrites, dans l'ordre d'écriture.
TABLES = [TABLE_COMMANDE, TABLE_TRAJET]

#: Colonnes de chaque table (spécification du Contrôle), dans l'ordre des tuples, avec leur type
#: PostgreSQL tel que ``information_schema.columns.data_type`` le donne.
COLONNES_TYPES = {
    TABLE_COMMANDE: [("ID_COMMANDE", "character varying"), ("TYPE_MVT", "character varying"),
                     ("LOC_DEP", "character varying"), ("LOC_ARR", "character varying"),
                     ("TYPE_DEP", "character varying"), ("TYPE_ARR", "character varying"),
                     ("TYPE_MAT", "character varying"), ("QTE_MAT", "integer"), ("TYPE_PAL", "character varying"),
                     ("H_DEP", "timestamp without time zone"), ("SPEC", "json"), ("ID_ENTRE", "character varying"),
                     ("H_DEP_R", "timestamp without time zone"), ("STATUT", "character varying"),
                     ("JUSTIF_REFUS", "integer")],
    TABLE_TRAJET: [("IMMAT_CAMI", "character varying"), ("ID_COMMANDE", "character varying"),
                   ("TYPE_CAMI", "character varying"), ("CAPA_CAMI", "character varying"),
                   ("COUT_PRESTA", "double precision"), ("SPEC_PTAC", "boolean"), ("SPEC_ENER", "boolean"),
                   ("SPEC_COL", "boolean"), ("TAUX_REMPLI", "double precision")],
}
COLONNES = {table: [nom for nom, _ in colonnes] for table, colonnes in COLONNES_TYPES.items()}

#: Longueur maximale des colonnes ``character varying(n)`` (spécification du Contrôle).
LONGUEURS = {TABLE_COMMANDE: {"ID_COMMANDE": 500, "TYPE_MVT": 10, "LOC_DEP": 100, "LOC_ARR": 100, "TYPE_DEP": 100,
                              "TYPE_ARR": 100, "TYPE_MAT": 20, "ID_ENTRE": 20, "STATUT": 20},
             TABLE_TRAJET: {"IMMAT_CAMI": 20, "ID_COMMANDE": 500, "TYPE_CAMI": 20, "CAPA_CAMI": 20}}

#: Clé de chaque ligne (la même dans les deux tables : une ligne par commande).
CLE = "ID_COMMANDE"

#: Codes de refus du Contrôle (colonne ``JUSTIF_REFUS``) : ``{code: (motif, description)}``.
CODES_REFUS = {
    1: ("GEOGRAPHIE", "la commande ne se situe pas dans le périmètre géographique"),
    2: ("SPECIFICITES", "les spécificités demandées par la commande ne peuvent pas être respectées"),
    3: ("CAPACITE", "capacité de la commande dépassant celle des camions disponibles"),
    4: ("DELAI", "délai (livraison) dépassé ou impossible à respecter"),
    5: ("PRIX", "pas rentable"),
    6: ("REMPLISSAGE", "objectif de taux de remplissage non respecté"),
    7: ("DONNEES", "erreurs de données sur la commande"),
    8: ("MOYEN", "moyen insuffisant pour assurer la commande (ex. : pas de camion frais)"),
}

#: Table D-JUSTIF : code du Contrôle pour chacun de nos motifs ``REF-…`` (hors REF-CAMION et
#: REF-FRIGO, qui dépendent de la cause, voir :func:`code_justif`).
MOTIF_VERS_CODE = {
    "REF-DONNEES": 7,
    "REF-EXPIREE": 4,
    "REF-SPEC": 2,
    "REF-DELAI": 4,
    "REF-CONDUCTEUR": 4,
    "REF-RENTABILITE": 6,
}

#: Motifs « pas de camion » : CAPACITE si des camions compatibles étaient libres sans avoir la place, MOYEN sinon.
MOTIFS_SANS_CAMION = ("REF-CAMION", "REF-FRIGO")

#: Spécification du Contrôle : ``STATUT`` vaut ``ACCEPTEE`` ou ``REFUSEE``. État du moteur → statut écrit :
#: une demande partie reste acceptée (``H_DEP_R`` renseignée) ; une acceptée expirée avant son départ
#: devient refusée (code 4 DELAI), comme pour le TA.
STATUTS_CONTROLE = {"ACCEPTEE": "ACCEPTEE", "PARTIE": "ACCEPTEE", "REFUSEE": "REFUSEE", "EXPIREE": "REFUSEE"}

#: Motif de nos ``ID_ENTRE`` (``N1``, ``N2``…), pour les requêtes ``~`` de PostgreSQL.
MOTIF_ID_ENTRE = hypotheses.MOTIF_ID_ENTRE


# ---------------------------------------------------------------- Lignes (fonctions pures)

def code_justif(demande: dict):
    """Code ``JUSTIF_REFUS`` d'une demande refusée ou expirée (table D-JUSTIF).

    Applique la table D-JUSTIF (``processus.md`` §2.7) :

    1. REF-DONNEES → 7 DONNEES ;
    2. REF-SPEC → 2 SPECIFICITES ;
    3. REF-CAMION ou REF-FRIGO, des camions compatibles libres mais aucun
       avec la place (``capacite_seule``) → 3 CAPACITE ;
    4. REF-CAMION ou REF-FRIGO, aucun camion compatible libre → 8 MOYEN ;
    5. REF-DELAI (refus, expiration, redémarrage), REF-EXPIREE (échéance
       déjà passée à la lecture, R-VAL-02) et REF-CONDUCTEUR → 4 DELAI ;
    6. REF-RENTABILITE (tournée sous le seuil θ à l'heure limite) → 6 REMPLISSAGE.

    Les codes 1 GEOGRAPHIE (nous n'écrivons que nos commandes) et 5 PRIX
    (D-PRIX non couverte) ne sont jamais produits. Un motif inconnu donne 7.

    :param demande: Demande du moteur.
    :return: Code de :data:`CODES_REFUS`, ou ``None`` si la demande n'est ni refusée ni expirée.
    :rtype: int or None
    """
    if demande.get("etat") not in ("REFUSEE", "EXPIREE"):
        return None
    motif = demande.get("motif") or ""
    if motif in MOTIFS_SANS_CAMION:
        return 3 if demande.get("capacite_seule") else 8
    return MOTIF_VERS_CODE.get(motif, 7)


def _texte(table: str, colonne: str, valeur: object) -> str:
    """Valeur texte d'une colonne, tronquée à la longueur de la colonne.

    :param table: Table.
    :param colonne: Colonne ``character varying``.
    :param valeur: Valeur (``None`` donne une chaîne vide).
    :return: Texte d'au plus ``LONGUEURS[table][colonne]`` caractères.
    """
    texte = "" if valeur is None else str(valeur)
    longueur = LONGUEURS[table].get(colonne)
    return texte[:longueur] if longueur else texte


def spec_tenue(demande: dict, camion: dict) -> dict:
    """SPEC respectées par le camion, clé par clé (R-SPEC-02).

    :param demande: Demande préparée (``demande["spec"]``).
    :param camion: Camion de la tournée.
    :return: ``{"PTAC", "ENR", "COL": bool}`` pour les clés demandées seulement.
    """
    ecarts = regles.verifier_spec(demande, camion)
    return {cle: cle not in ecarts for cle in demande.get("spec", {})}


def id_entre(lien: dict, id_demande: str) -> str:
    """``ID_ENTRE`` d'une commande : ``N`` puis son numéro chez nous (spécification du Contrôle).

    Une commande garde le numéro qu'elle a reçu ; une nouvelle commande prend
    le numéro suivant le plus grand déjà attribué (y compris par un lancement
    précédent, lu par :func:`reprendre`). Ne modifie pas le lien : le numéro
    n'est réservé que par :func:`attribuer_id_entre`.

    :param lien: Lien (``numeros``, ``dernier_numero``).
    :param id_demande: Identifiant de la commande.
    :return: Par exemple ``"N12"``.
    """
    return lien["numeros"].get(id_demande) or "%s%d" % (hypotheses.PREFIXE_ENTREPRISE, lien["dernier_numero"] + 1)


def attribuer_id_entre(lien: dict, id_demande: str, valeur: str) -> None:
    """Réserve l'``ID_ENTRE`` d'une commande (numéros 1, 2, 3… sans doublon).

    :param lien: Lien.
    :param id_demande: Identifiant de la commande.
    :param valeur: Valeur donnée par :func:`id_entre`.
    """
    if id_demande not in lien["numeros"]:
        lien["numeros"][id_demande] = valeur
        lien["dernier_numero"] = max(lien["dernier_numero"], int(valeur[len(hypotheses.PREFIXE_ENTREPRISE):]))


def ligne_finale(ligne: tuple) -> bool:
    """Dit si une ligne ``TRACABILITE_COMMANDE`` ne changera plus.

    Refusée, ou acceptée avec son heure réelle de départ : le moteur ne la
    recalcule plus.

    :param ligne: Tuple dans l'ordre de :data:`COLONNES`.
    :return: ``True`` si la ligne est définitive.
    """
    valeurs = dict(zip(COLONNES[TABLE_COMMANDE], ligne))
    return valeurs["STATUT"] == "REFUSEE" or valeurs["H_DEP_R"] is not None


def ligne_commande(demande: dict, identifiant_entreprise: str):
    """Ligne ``TRACABILITE_COMMANDE`` d'une demande NORTH décidée.

    Applique la règle R-CTL-01 : champs de l'ERP tels que reçus (lieux en
    nom, comme le flux), ``SPEC`` en JSON (``{"PTAC": "19t"}``, ``{}`` si
    vide), ``ID_ENTRE`` = ``N`` + numéro de la commande (:func:`id_entre`),
    ``H_DEP_R`` = heure réelle de départ, celle comparée à ``H_DEP``
    (R-DEL-01), une fois la demande partie, ``STATUT`` ``ACCEPTEE`` ou
    ``REFUSEE`` (:data:`STATUTS_CONTROLE`), ``JUSTIF_REFUS`` = code D-JUSTIF.

    :param demande: Demande du moteur.
    :param identifiant_entreprise: ``ID_ENTRE`` de la commande (:func:`id_entre`).
    :return: Tuple dans l'ordre de :data:`COLONNES`, ou ``None`` si la demande n'est pas décidée
        ou si une colonne obligatoire manque (quantité ou ``H_DEP`` illisible, identifiant trop long).
    :rtype: tuple or None
    """
    etat_moteur = archivage.statut_commande(demande)
    if etat_moteur is None or demande.get("qte") is None or demande.get("h_dep") is None:
        return None
    if len(str(demande["id"])) > LONGUEURS[TABLE_COMMANDE]["ID_COMMANDE"]:
        return None
    t = TABLE_COMMANDE
    return (str(demande["id"]), _texte(t, "TYPE_MVT", demande.get("TYPE_MVT")),
            _texte(t, "LOC_DEP", demande.get("LOC_DEP")), _texte(t, "LOC_ARR", demande.get("LOC_ARR")),
            _texte(t, "TYPE_DEP", demande.get("TYPE_DEP")), _texte(t, "TYPE_ARR", demande.get("TYPE_ARR")),
            _texte(t, "TYPE_MAT", demande.get("TYPE_MAT")), int(demande["qte"]), _texte(t, "TYPE_PAL", demande.get("TYPE_PAL")),
            demande["h_dep"], json.dumps(demande.get("spec") or {}, ensure_ascii=False),
            _texte(t, "ID_ENTRE", identifiant_entreprise),
            demande.get("h_depart") if etat_moteur == "PARTIE" else None, STATUTS_CONTROLE[etat_moteur],
            code_justif(demande))


def capacite_controle(ptac: str) -> str:
    """Valeur ``CAPA_CAMI`` d'un camion : son PTAC au format ``19t``, sans espace.

    Applique la règle R-CTL-01 (format « xx t » de la spécification du
    Contrôle, écrit sans espace : décision d'équipe du 24/11/2026). Les fiches
    donnent déjà ``19t`` ; un espace éventuel est retiré.

    :param ptac: PTAC de la fiche camion (``"19t"``).
    :return: PTAC sans espace.
    """
    return str(ptac).replace(" ", "")


def ligne_trajet(demande: dict, etat: dict, ref: dict, couts: dict):
    """Ligne ``TRACABILITE_TRAJET`` d'une demande partie.

    Applique la règle R-CTL-01 : le camion de la tournée, son type
    (:data:`hypotheses.TYPES_CAMION`), sa capacité (PTAC de la fiche au format
    ``19t``, sans espace : format confirmé par l'équipe le 24/11/2026), le prix
    facturé (:func:`planification.prix_prestation`, R-GAM), les SPEC tenues
    (vide si la clé n'est pas demandée) et le remplissage du trajet au départ
    du premier lieu de chargement (R-KPI-TR).

    :param demande: Demande du moteur.
    :param etat: État du moteur (``tournees``, ``flotte``).
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil ; avec les taux par défaut, il n'y a pas de prix réel.
    :return: Tuple dans l'ordre de :data:`COLONNES`, ou ``None`` si la demande n'est pas partie
        ou si le prix est inconnu (colonne obligatoire).
    :rtype: tuple or None
    """
    if demande.get("etat") != "PARTIE" or couts.get("source") == "defaut":
        return None
    tournee = etat["tournees"].get(demande.get("tournee"))
    camion = etat["flotte"].get(tournee["camion"]) if tournee else None
    if camion is None:
        return None
    prix = planification.prix_prestation(demande, camion, ref, couts, demande.get("gamme"))
    if prix is None:
        return None
    remplissage = min(max(tournee.get("plan", {}).get("remplissage") or 0.0, 0.0), 1.0)
    taux = round(100 * remplissage, 1) if hypotheses.TAUX_REMPLI_EN_POURCENT else round(remplissage, 3)
    spec = spec_tenue(demande, camion)
    t = TABLE_TRAJET
    return (_texte(t, "IMMAT_CAMI", camion["IMMAT"]), str(demande["id"]),
            _texte(t, "TYPE_CAMI", hypotheses.TYPES_CAMION[bool(camion["frigo"])]),
            _texte(t, "CAPA_CAMI", capacite_controle(camion["PTAC"])), float(prix),
            spec.get("PTAC"), spec.get("ENR"), spec.get("COL"), taux)


def lignes_modifiees(lien: dict, etat: dict, ref: dict, couts: dict) -> dict:
    """Lignes à écrire : celles qui diffèrent de ce qui a déjà été écrit.

    Les demandes hors périmètre (ignorées) ne sont jamais écrites (R-CTL-01).
    Une commande écrivable reçoit son ``ID_ENTRE`` (:func:`id_entre`) la
    première fois. Une commande déjà écrite définitive (:func:`ligne_finale`) n'est plus recalculée ;
    une ligne de trajet n'est écrite qu'une fois. Les commandes d'un
    lancement précédent (:func:`reprendre`) ne sont jamais réécrites.

    :param lien: Lien ouvert par :func:`ouvrir`.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil.
    :return: ``{table: [tuples]}`` pour chacune des :data:`TABLES`.
    """
    cache, rejetees = lien["cache"], lien["rejetees"]
    candidates = {table: [] for table in TABLES}
    for demande in etat["demandes"].values():
        id_demande = str(demande["id"])
        if demande.get("etat") in ("IGNOREE", "RECUE", None) or id_demande in lien["proteges"]:
            continue
        ancienne = cache[TABLE_COMMANDE].get(id_demande)
        if ancienne is None or not ligne_finale(ancienne):
            valeur = id_entre(lien, id_demande)
            ligne = ligne_commande(demande, valeur)
            if ligne is not None:
                attribuer_id_entre(lien, id_demande, valeur)
            if ligne is None:
                if id_demande not in lien["non_ecrivables"]:
                    JOURNAL.warning("traçabilité Contrôle : commande %s non écrivable (quantité ou H_DEP illisible)",
                                    id_demande)
                lien["non_ecrivables"].add(id_demande)
            else:
                candidates[TABLE_COMMANDE].append(ligne)
        if demande.get("etat") == "PARTIE" and id_demande not in cache[TABLE_TRAJET]:
            ligne = ligne_trajet(demande, etat, ref, couts)
            if ligne is not None:
                candidates[TABLE_TRAJET].append(ligne)
    indices = {table: COLONNES[table].index(CLE) for table in TABLES}
    return {table: [ligne for ligne in lignes
                    if cache[table].get(ligne[indices[table]]) != ligne and rejetees.get((table, ligne[indices[table]])) != ligne]
            for table, lignes in candidates.items()}


# ---------------------------------------------------------------- Base de données

def requetes(table: str) -> tuple:
    """Requêtes de mise à jour puis d'insertion d'une ligne.

    La mise à jour est limitée à la ligne de la commande ; dans
    ``TRACABILITE_COMMANDE``, aussi à l'``ID_ENTRE`` de notre ligne (``N…``) :
    une ligne d'un autre groupe n'est jamais modifiée (son insertion échoue sur la clé).

    :param table: Une des :data:`TABLES`.
    :return: ``(update, insert)`` avec des paramètres ``%s`` ; ``update`` prend les colonnes hors clé, puis la clé
        (et ``ID_ENTRE`` pour les commandes).
    """
    colonnes = COLONNES[table]
    autres = [c for c in colonnes if c != CLE]
    condition = '"%s" = %%s' % CLE + (' AND "ID_ENTRE" = %s' if table == TABLE_COMMANDE else "")
    update = 'UPDATE "%s" SET %s WHERE %s' % (table, ", ".join('"%s" = %%s' % c for c in autres), condition)
    insert = 'INSERT INTO "%s" (%s) VALUES (%s)' % (table, ", ".join('"%s"' % c for c in colonnes),
                                                   ", ".join(["%s"] * len(colonnes)))
    return update, insert


def ecrire_ligne(curseur: object, table: str, ligne: tuple) -> None:
    """Écrit une ligne : mise à jour de notre ligne si elle existe, insertion sinon.

    :param curseur: Curseur DB-API.
    :param table: Une des :data:`TABLES`.
    :param ligne: Tuple dans l'ordre de :data:`COLONNES`.
    """
    colonnes = COLONNES[table]
    valeurs = dict(zip(colonnes, ligne))
    update, insert = requetes(table)
    parametres = [valeurs[c] for c in colonnes if c != CLE] + [valeurs[CLE]]
    if table == TABLE_COMMANDE:
        parametres.append(valeurs["ID_ENTRE"])
    curseur.execute(update, parametres)
    if curseur.rowcount == 0:
        curseur.execute(insert, list(ligne))


def ouvrir(parametres: dict = None) -> dict:
    """Prépare le lien avec les tables du Contrôle, sans se connecter.

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`, délais bornés).
    :return: Lien (voir :func:`nouveau_lien`).
    """
    import psycopg2  # disponible sur la VM uniquement

    return nouveau_lien(lambda: psycopg2.connect(**(parametres or referentiels.BDD)),
                        (psycopg2.IntegrityError, psycopg2.DataError))


def nouveau_lien(connecter: object, erreurs_ligne: tuple) -> dict:
    """Crée un lien vide autour d'une fonction de connexion (séparée de :func:`ouvrir` pour les tests).

    :param connecter: Fonction sans argument qui renvoie une connexion DB-API.
    :type connecter: callable
    :param erreurs_ligne: Exceptions dues au contenu d'une ligne (clé, contrainte, type) plutôt qu'à la connexion.
    :return: Lien : ``connecter``, ``connexion``, ``erreurs_ligne``, ``cache``, ``rejetees``, ``proteges``,
        ``non_ecrivables``, ``numeros`` (``ID_ENTRE`` attribués), ``dernier_numero``, ``repris``, ``ecrites``,
        ``messages``, ``sante`` (:func:`robustesse.nouvel_etat_bdd`).
    """
    return {"connecter": connecter, "connexion": None, "erreurs_ligne": erreurs_ligne,
            "cache": {table: {} for table in TABLES}, "rejetees": {}, "proteges": set(), "non_ecrivables": set(),
            "numeros": {}, "dernier_numero": 0, "repris": False, "ecrites": 0, "messages": {},
            "sante": robustesse.nouvel_etat_bdd()}


def deconnecter(lien: dict) -> None:
    """Annule la transaction en cours et ferme la connexion, sans lever d'erreur.

    :param lien: Lien.
    """
    connexion, lien["connexion"] = lien["connexion"], None
    if connexion is None:
        return
    for action in (connexion.rollback, connexion.close):
        try:
            action()
        except Exception:  # connexion déjà perdue : rien d'autre à faire
            pass


def reprendre(lien: dict) -> set:
    """Reprend après un arrêt du moteur (question E18), dans nos lignes seulement.

    Nos commandes laissées acceptées et pas encore parties (``H_DEP_R``
    vide) par le lancement précédent passent refusées, code 4 DELAI (elles ne
    partiront plus) ; toutes nos commandes déjà écrites sont protégées : le
    moteur ne les redécide pas et ne les réécrit pas. La numérotation des
    ``ID_ENTRE`` reprend après le plus grand numéro trouvé.

    :param lien: Lien connecté.
    :return: Identifiants de nos commandes déjà présentes.
    """
    debut = time.monotonic()
    curseur = lien["connexion"].cursor()
    curseur.execute('UPDATE "%s" SET "STATUT" = \'REFUSEE\', "JUSTIF_REFUS" = %%s '
                    'WHERE "STATUT" = \'ACCEPTEE\' AND "H_DEP_R" IS NULL AND "ID_ENTRE" ~ %%s' % TABLE_COMMANDE,
                    (MOTIF_VERS_CODE["REF-DELAI"], MOTIF_ID_ENTRE))
    curseur.execute('SELECT "ID_COMMANDE", "ID_ENTRE" FROM "%s" WHERE "ID_ENTRE" ~ %%s' % TABLE_COMMANDE,
                    (MOTIF_ID_ENTRE,))
    lignes = curseur.fetchall()
    ids = {ligne[0] for ligne in lignes}
    numeros = [int(ligne[1][len(hypotheses.PREFIXE_ENTREPRISE):]) for ligne in lignes]
    lien["dernier_numero"] = max([lien["dernier_numero"]] + numeros)
    lien["connexion"].commit()
    journalisation.evenement_sql("REPRISE", TABLE_COMMANDE, len(ids), journalisation.duree_ms(debut))
    lien["proteges"] |= ids
    lien["repris"] = True
    return ids


def ecrire(lien: dict, lignes: dict, maintenant: datetime.datetime) -> int:
    """Écrit les lignes, chacune dans sa transaction ; une ligne refusée par la base est mise de côté.

    :param lien: Lien connecté.
    :param lignes: ``{table: [tuples]}``.
    :param maintenant: Heure réelle courante.
    :return: Nombre de lignes écrites.
    """
    connexion, ecrites = lien["connexion"], 0
    for table in TABLES:
        indice = COLONNES[table].index(CLE)
        debut, ecrites_table = time.monotonic(), 0
        for ligne in lignes[table]:
            try:
                ecrire_ligne(connexion.cursor(), table, ligne)
                connexion.commit()
            except lien["erreurs_ligne"] as erreur:
                connexion.rollback()
                lien["rejetees"][(table, ligne[indice])] = ligne
                lien["sante"]["rejetees"] += 1
                journalisation.evenement_sql("UPSERT", table, 0, erreur="ligne %s rejetée : %s"
                                             % (ligne[indice], journalisation.texte_erreur(erreur)))
                robustesse.signaler(lien["messages"], "ligne", maintenant, "traçabilité Contrôle : ligne %s de %s rejetée : %s"
                                    % (ligne[indice], table, journalisation.texte_erreur(erreur)), logging.ERROR, "controle")
                continue
            lien["cache"][table][ligne[indice]] = ligne
            ecrites += 1
            ecrites_table += 1
        if ecrites_table:
            journalisation.evenement_sql("UPSERT", table, ecrites_table, journalisation.duree_ms(debut))
    return ecrites


def synchroniser(lien: dict, etat: dict, ref: dict, couts: dict, maintenant: datetime.datetime) -> int:
    """Écrit dans les tables du Contrôle ce qui a changé depuis la dernière fois.

    Applique les règles R-CTL-01 et R-ROB-02 : se connecte si besoin ; à la
    première connexion, appelle :func:`reprendre` et ajoute nos commandes déjà
    présentes à ``etat["deja_archivees"]``. Ne lève jamais d'erreur : base
    injoignable ou table absente, la connexion est fermée, le cache n'est pas
    modifié et tout est réessayé au tick suivant.

    :param lien: Lien ouvert par :func:`ouvrir`.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil.
    :param maintenant: Heure réelle (ou simulée) courante.
    :return: Nombre de lignes écrites (0 si rien n'a changé ou si la base est indisponible).
    """
    etape, nb_lignes = "connexion", 0
    try:
        if lien["connexion"] is None:
            debut = time.monotonic()
            lien["connexion"] = lien["connecter"]()
            journalisation.evenement_sql("CONNEXION", "-", 0, journalisation.duree_ms(debut), niveau=logging.DEBUG)
        if not lien["repris"]:
            etape = "reprise"
            ids = reprendre(lien)
            etat.setdefault("deja_archivees", set()).update(ids)
            JOURNAL.info("traçabilité Contrôle : %d commande(s) %s… déjà en base, laissées telles quelles ; "
                         "numérotation reprise à %s%d", len(ids), hypotheses.PREFIXE_ENTREPRISE,
                         hypotheses.PREFIXE_ENTREPRISE, lien["dernier_numero"] + 1)
        etape = "calcul des lignes"
        lignes = lignes_modifiees(lien, etat, ref, couts)
        nb_lignes = sum(len(l) for l in lignes.values())
        if not nb_lignes:
            robustesse.noter_succes_bdd(lien["sante"], maintenant, 0, "traçabilité Contrôle")
            return 0
        etape = "écriture"
        ecrites = ecrire(lien, lignes, maintenant)
    except Exception as erreur:  # R-ROB-02 / R-ROB-06 : base injoignable, table absente… : le moteur continue, mais le dit
        robustesse.noter_echec_bdd(lien["sante"], maintenant, erreur, nb_lignes)
        journalisation.evenement_sql(etape.upper(), ",".join(TABLES), nb_lignes, erreur=erreur)
        robustesse.signaler(lien["messages"], "base", maintenant,
                            "traçabilité Contrôle EN ÉCHEC (%s, %d échec(s) consécutif(s) depuis %s) : %s ; %d ligne(s) en "
                            "attente, nouvel essai au prochain tick" % (etape, lien["sante"]["echecs"], lien["sante"]["depuis"],
                                                                        journalisation.texte_erreur(erreur), nb_lignes),
                            logging.ERROR, "controle")
        deconnecter(lien)
        return 0
    lien["ecrites"] += ecrites
    robustesse.noter_succes_bdd(lien["sante"], maintenant, ecrites, "traçabilité Contrôle")
    return ecrites


def fermer(lien: dict) -> None:
    """Ferme la connexion du lien.

    :param lien: Lien.
    """
    deconnecter(lien)


# ---------------------------------------------------------------- Outils en ligne de commande (lecture seule)

def ecarts_schema(colonnes_base: list) -> list:
    """Compare les colonnes trouvées en base à la spécification du Contrôle.

    :param colonnes_base: ``(table, colonne, data_type)`` lus dans ``information_schema.columns``.
    :return: Écarts lisibles (vide si les deux tables sont conformes).
    """
    trouvees = {}
    for table, colonne, type_base in colonnes_base:
        trouvees.setdefault(table, {})[colonne] = type_base
    ecarts = []
    for table, colonnes in COLONNES_TYPES.items():
        if table not in trouvees:
            ecarts.append("%s : table absente" % table)
            continue
        for colonne, type_attendu in colonnes:
            type_base = trouvees[table].get(colonne)
            if type_base is None:
                ecarts.append("%s.%s : colonne absente" % (table, colonne))
            elif type_base != type_attendu:
                ecarts.append("%s.%s : type %s au lieu de %s" % (table, colonne, type_base, type_attendu))
    return ecarts


def verifier_tables(parametres: dict = None) -> list:
    """Lit les colonnes des deux tables dans ``information_schema`` (SELECT uniquement).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    :return: Écarts avec la spécification (:func:`ecarts_schema`).
    """
    import psycopg2  # disponible sur la VM uniquement

    connexion = psycopg2.connect(**(parametres or referentiels.BDD))
    try:
        curseur = connexion.cursor()
        curseur.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                        "WHERE table_name IN %s", (tuple(TABLES),))
        return ecarts_schema(curseur.fetchall())
    finally:
        connexion.close()


def afficher_etat(parametres: dict = None) -> None:
    """Affiche nos lignes par statut et par code de refus (SELECT uniquement).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    """
    import psycopg2  # disponible sur la VM uniquement

    connexion = psycopg2.connect(**(parametres or referentiels.BDD))
    try:
        curseur = connexion.cursor()
        curseur.execute('SELECT "STATUT", COUNT(*) FROM "%s" WHERE "ID_ENTRE" ~ %%s GROUP BY 1 ORDER BY 1' % TABLE_COMMANDE,
                        (MOTIF_ID_ENTRE,))
        print("%s (%s…) : %s" % (TABLE_COMMANDE, hypotheses.PREFIXE_ENTREPRISE,
                                 ", ".join("%s %d" % ligne for ligne in curseur.fetchall()) or "vide"))
        curseur.execute('SELECT "JUSTIF_REFUS", COUNT(*) FROM "%s" WHERE "ID_ENTRE" ~ %%s AND "JUSTIF_REFUS" IS NOT NULL '
                        'GROUP BY 1 ORDER BY 1' % TABLE_COMMANDE, (MOTIF_ID_ENTRE,))
        for code, nombre in curseur.fetchall():
            print("  refus %s %-12s : %d" % (code, CODES_REFUS.get(code, ("?",))[0], nombre))
        curseur.execute('SELECT COUNT(*), AVG(t."TAUX_REMPLI"), SUM(t."COUT_PRESTA") FROM "%s" t JOIN "%s" c '
                        'ON t."ID_COMMANDE" = c."ID_COMMANDE" WHERE c."ID_ENTRE" ~ %%s' % (TABLE_TRAJET, TABLE_COMMANDE),
                        (MOTIF_ID_ENTRE,))
        nombre, remplissage, total = curseur.fetchone()
        print("%s : %d ligne(s), remplissage moyen %s, prix total %s"
              % (TABLE_TRAJET, nombre, "%.1f" % remplissage if remplissage is not None else "-",
                 "%.2f" % total if total is not None else "-"))
    finally:
        connexion.close()


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Tables de traçabilité du Contrôle : vérification et état (lecture seule)")
    parseur.add_argument("--verifier", action="store_true",
                         help="vérifie que les deux tables existent avec les colonnes attendues (code de sortie 1 sinon)")
    parseur.add_argument("--etat", action="store_true", help="nos lignes par statut et par code de refus")
    options = parseur.parse_args()
    if options.verifier:
        ecarts = verifier_tables()
        for ecart in ecarts:
            print("! " + ecart)
        print("Tables du Contrôle conformes" if not ecarts else "%d écart(s) avec la spécification" % len(ecarts))
        sys.exit(1 if ecarts else 0)
    if options.etat:
        afficher_etat()
    else:
        parseur.print_help()
