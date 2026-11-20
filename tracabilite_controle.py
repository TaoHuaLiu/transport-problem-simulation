"""Interface avec les tables de traçabilité du groupe Contrôle.

Le Contrôle a spécifié deux tables partagées, créées dans la base
``UE_ENS_DCJC`` ; elles sont le seul canal de données entre groupes
(sujet). Ce module les remplit pour **nos commandes seulement** (périmètre
NORTH, règle R-CTL-01) :

- ``TRACABILITE_COMMANDE`` : une ligne par commande NORTH décidée
  (acceptée, partie, refusée ou expirée), clé ``ID_COMMANDE``, notre code
  ``ID_ENTRE`` (:data:`hypotheses.ID_ENTREPRISE`) ; ``JUSTIF_REFUS`` porte le
  code de refus du Contrôle (table D-JUSTIF, :func:`code_justif`) ;
- ``TRACABILITE_TRAJET`` : une ligne par commande partie : camion, type,
  capacité, prix de la prestation, SPEC tenues, remplissage du trajet.

Il est séparé de :mod:`archivage` (nos propres tables ``*_NORTH``) : autre
connexion, autres règles. Le moteur l'appelle à chaque tick avec l'option
``--controle`` (imposée en mode audit) ; comme l'archivage, il n'envoie que
les lignes modifiées et ne lève jamais d'erreur (règle R-ROB-02) : base
injoignable ou table absente, le moteur continue et les lignes en retard
partent à la connexion suivante.

Les tables n'ont pas forcément de contrainte d'unicité exploitable (pas de
clé déclarée sur ``TRACABILITE_TRAJET``) : chaque ligne est écrite par un
``UPDATE`` limité à notre ligne, suivi d'un ``INSERT`` si rien n'a été mis à jour.
Aucune ligne d'un autre groupe n'est lue pour être modifiée ni supprimée.

Utilisation, sur la VM (lecture seule) :

.. code-block:: bash

    python3 tracabilite_controle.py --verifier   # les deux tables existent-elles avec les colonnes attendues ?
    python3 tracabilite_controle.py --etat       # nos lignes par statut et par code de refus
"""

import argparse
import datetime
import json
import sys

import archivage
import hypotheses
import planification
import referentiels
import regles
import robustesse

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
    "REF-SPEC": 2,
    "REF-DELAI": 4,
    "REF-CONDUCTEUR": 4,
    "REF-RENTABILITE": 6,
}

#: Motifs « pas de camion » : CAPACITE si des camions compatibles étaient libres sans avoir la place, MOYEN sinon.
MOTIFS_SANS_CAMION = ("REF-CAMION", "REF-FRIGO")

#: Statuts finals : une ligne écrite avec ce statut ne change plus.
STATUTS_FINAUX = ("PARTIE", "REFUSEE", "EXPIREE")


# ---------------------------------------------------------------- Lignes (fonctions pures)

def code_justif(demande: dict):
    """Code ``JUSTIF_REFUS`` d'une demande refusée ou expirée (table D-JUSTIF).

    Applique la table D-JUSTIF (``processus.md`` §2.7) :

    1. REF-DONNEES → 7 DONNEES ;
    2. REF-SPEC → 2 SPECIFICITES ;
    3. REF-CAMION ou REF-FRIGO, des camions compatibles libres mais aucun
       avec la place (``capacite_seule``) → 3 CAPACITE ;
    4. REF-CAMION ou REF-FRIGO, aucun camion compatible libre → 8 MOYEN ;
    5. REF-DELAI (refus, expiration, redémarrage) et REF-CONDUCTEUR → 4 DELAI ;
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


def ligne_commande(demande: dict):
    """Ligne ``TRACABILITE_COMMANDE`` d'une demande NORTH décidée.

    Applique la règle R-CTL-01 : champs de l'ERP tels que reçus (lieux en
    nom, comme le flux), ``SPEC`` en JSON (``{"PTAC": "19t"}``, ``{}`` si
    vide), ``ID_ENTRE`` = :data:`hypotheses.ID_ENTREPRISE`, ``H_DEP_R`` =
    départ comparé à ``H_DEP`` (R-DEL-01) une fois la demande partie,
    ``STATUT`` du moteur, ``JUSTIF_REFUS`` = code D-JUSTIF.

    :param demande: Demande du moteur.
    :return: Tuple dans l'ordre de :data:`COLONNES`, ou ``None`` si la demande n'est pas décidée
        ou si une colonne obligatoire manque (quantité ou ``H_DEP`` illisible, identifiant trop long).
    :rtype: tuple or None
    """
    statut = archivage.statut_commande(demande)
    if statut is None or demande.get("qte") is None or demande.get("h_dep") is None:
        return None
    if len(str(demande["id"])) > LONGUEURS[TABLE_COMMANDE]["ID_COMMANDE"]:
        return None
    t = TABLE_COMMANDE
    return (str(demande["id"]), _texte(t, "TYPE_MVT", demande.get("TYPE_MVT")),
            _texte(t, "LOC_DEP", demande.get("LOC_DEP")), _texte(t, "LOC_ARR", demande.get("LOC_ARR")),
            _texte(t, "TYPE_DEP", demande.get("TYPE_DEP")), _texte(t, "TYPE_ARR", demande.get("TYPE_ARR")),
            _texte(t, "TYPE_MAT", demande.get("TYPE_MAT")), int(demande["qte"]), _texte(t, "TYPE_PAL", demande.get("TYPE_PAL")),
            demande["h_dep"], json.dumps(demande.get("spec") or {}, ensure_ascii=False),
            _texte(t, "ID_ENTRE", hypotheses.ID_ENTREPRISE),
            demande.get("h_depart") if statut == "PARTIE" else None, statut, code_justif(demande))


def ligne_trajet(demande: dict, etat: dict, ref: dict, couts: dict):
    """Ligne ``TRACABILITE_TRAJET`` d'une demande partie.

    Applique la règle R-CTL-01 : le camion de la tournée, son type
    (:data:`hypotheses.TYPES_CAMION`), sa capacité (PTAC de la fiche), le prix
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
            _texte(t, "CAPA_CAMI", camion["PTAC"]), float(prix),
            spec.get("PTAC"), spec.get("ENR"), spec.get("COL"), taux)


def lignes_modifiees(lien: dict, etat: dict, ref: dict, couts: dict) -> dict:
    """Lignes à écrire : celles qui diffèrent de ce qui a déjà été écrit.

    Les demandes hors périmètre (ignorées) ne sont jamais écrites (R-CTL-01).
    Une commande déjà écrite avec un statut final n'est plus recalculée ;
    une ligne de trajet n'est écrite qu'une fois. Les commandes d'un
    lancement précédent (:func:`reprendre`) ne sont jamais réécrites.

    :param lien: Lien ouvert par :func:`ouvrir`.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil.
    :return: ``{table: [tuples]}`` pour chacune des :data:`TABLES`.
    """
    cache, rejetees = lien["cache"], lien["rejetees"]
    indice_statut = COLONNES[TABLE_COMMANDE].index("STATUT")
    candidates = {table: [] for table in TABLES}
    for demande in etat["demandes"].values():
        id_demande = str(demande["id"])
        if demande.get("etat") in ("IGNOREE", "RECUE", None) or id_demande in lien["proteges"]:
            continue
        ancienne = cache[TABLE_COMMANDE].get(id_demande)
        if ancienne is None or ancienne[indice_statut] not in STATUTS_FINAUX:
            ligne = ligne_commande(demande)
            if ligne is None:
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
    ``TRACABILITE_COMMANDE``, aussi à notre ``ID_ENTRE`` : une ligne d'un
    autre groupe n'est jamais modifiée (son insertion échoue sur la clé).

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
        ``non_ecrivables``, ``repris``, ``ecrites``, ``messages``.
    """
    return {"connecter": connecter, "connexion": None, "erreurs_ligne": erreurs_ligne,
            "cache": {table: {} for table in TABLES}, "rejetees": {}, "proteges": set(), "non_ecrivables": set(),
            "repris": False, "ecrites": 0, "messages": {}}


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

    Nos commandes laissées acceptées par le lancement précédent passent
    expirées, code 4 DELAI (elles ne partiront plus) ; toutes nos commandes
    déjà écrites sont protégées : le moteur ne les redécide pas et ne les
    réécrit pas.

    :param lien: Lien connecté.
    :return: Identifiants de nos commandes déjà présentes.
    """
    curseur = lien["connexion"].cursor()
    curseur.execute('UPDATE "%s" SET "STATUT" = \'EXPIREE\', "JUSTIF_REFUS" = %%s '
                    'WHERE "STATUT" = \'ACCEPTEE\' AND "ID_ENTRE" = %%s' % TABLE_COMMANDE,
                    (MOTIF_VERS_CODE["REF-DELAI"], hypotheses.ID_ENTREPRISE))
    curseur.execute('SELECT "ID_COMMANDE" FROM "%s" WHERE "ID_ENTRE" = %%s' % TABLE_COMMANDE, (hypotheses.ID_ENTREPRISE,))
    ids = {ligne[0] for ligne in curseur.fetchall()}
    lien["connexion"].commit()
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
        for ligne in lignes[table]:
            try:
                ecrire_ligne(connexion.cursor(), table, ligne)
                connexion.commit()
            except lien["erreurs_ligne"] as erreur:
                connexion.rollback()
                lien["rejetees"][(table, ligne[indice])] = ligne
                robustesse.signaler(lien["messages"], "ligne", maintenant, "traçabilité Contrôle : ligne %s de %s rejetée : %s"
                                    % (ligne[indice], table, str(erreur).strip()))
                continue
            lien["cache"][table][ligne[indice]] = ligne
            ecrites += 1
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
    try:
        if lien["connexion"] is None:
            lien["connexion"] = lien["connecter"]()
        if not lien["repris"]:
            ids = reprendre(lien)
            etat.setdefault("deja_archivees", set()).update(ids)
            print("Traçabilité Contrôle : %d commande(s) %s déjà en base, laissées telles quelles"
                  % (len(ids), hypotheses.ID_ENTREPRISE), flush=True)
        lignes = lignes_modifiees(lien, etat, ref, couts)
        if not any(lignes.values()):
            return 0
        ecrites = ecrire(lien, lignes, maintenant)
    except Exception as erreur:  # R-ROB-02 : base injoignable, table absente… : le moteur ne doit pas s'arrêter
        robustesse.signaler(lien["messages"], "base", maintenant, "traçabilité Contrôle : %s (nouvel essai au prochain tick)"
                            % (str(erreur).strip() or type(erreur).__name__))
        deconnecter(lien)
        return 0
    lien["ecrites"] += ecrites
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
        curseur.execute('SELECT "STATUT", COUNT(*) FROM "%s" WHERE "ID_ENTRE" = %%s GROUP BY 1 ORDER BY 1' % TABLE_COMMANDE,
                        (hypotheses.ID_ENTREPRISE,))
        print("%s (%s) : %s" % (TABLE_COMMANDE, hypotheses.ID_ENTREPRISE,
                                ", ".join("%s %d" % ligne for ligne in curseur.fetchall()) or "vide"))
        curseur.execute('SELECT "JUSTIF_REFUS", COUNT(*) FROM "%s" WHERE "ID_ENTRE" = %%s AND "JUSTIF_REFUS" IS NOT NULL '
                        'GROUP BY 1 ORDER BY 1' % TABLE_COMMANDE, (hypotheses.ID_ENTREPRISE,))
        for code, nombre in curseur.fetchall():
            print("  refus %s %-12s : %d" % (code, CODES_REFUS.get(code, ("?",))[0], nombre))
        curseur.execute('SELECT COUNT(*), AVG(t."TAUX_REMPLI"), SUM(t."COUT_PRESTA") FROM "%s" t JOIN "%s" c '
                        'ON t."ID_COMMANDE" = c."ID_COMMANDE" WHERE c."ID_ENTRE" = %%s' % (TABLE_TRAJET, TABLE_COMMANDE),
                        (hypotheses.ID_ENTREPRISE,))
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
