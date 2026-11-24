"""Archivage du moteur NORTH dans les tables ``LOG_NORTH_*`` de la base.

Tient à jour les trois tables propres au transporteur, créées par
``3 - Documentation/tables-nord.sql`` :

- ``LOG_NORTH_CAMION`` : une ligne par camion de la flotte NORTH, avec sa disponibilité ;
- ``LOG_NORTH_TRAJET`` : une ligne par tournée, avec son remplissage et son statut ;
- ``LOG_NORTH_ARCHIVAGE_COMMANDE`` : une ligne par demande NORTH décidée
  (acceptée, partie, refusée ou expirée), avec son motif de refus et son prix.

Principe : à chaque tick, :func:`synchroniser` recalcule les lignes à partir
de l'état du moteur, les compare à ce qui a déjà été écrit (cache en
mémoire) et n'envoie que les lignes modifiées, en une transaction, dans
l'ordre des clés étrangères (camions, trajets, commandes). Les écritures
sont des ``INSERT … ON CONFLICT … DO UPDATE``. Aucune table fournie n'est
modifiée.

Le journal CSV (:mod:`tracabilite`) reste la source de référence : si la
base est indisponible, le moteur continue et les lignes en retard sont
écrites dès que la connexion revient.

Utilisation, sur la VM (lecture seule) :

.. code-block:: bash

    python3 archivage.py --etat       # nombre de lignes par statut dans les trois tables
    python3 archivage.py --verifier   # les trois tables existent-elles avec les colonnes attendues ?
    python3 archivage.py --reinitialiser   # vide les trois tables (moteur arrêté, sauvegarde JSON avant)

Une écriture en échec n'est jamais silencieuse (règle R-ROB-06) : chaque lot et
chaque erreur vont au journal SQL (:func:`journalisation.evenement_sql`), l'échec est
journalisé en erreur avec le nombre de lignes en attente, et ``archive["sante"]``
(repris dans ``sante_moteur.json`` et sur le tableau de bord) donne le dernier succès,
la dernière erreur et le nombre d'échecs consécutifs. À la première connexion, les
trois tables sont vérifiées : une table absente ou renommée donne un message explicite.
"""

import argparse
import datetime
import json
import logging
import os
import sys
import time

import journalisation
import planification
import referentiels
import regles
import robustesse

JOURNAL = journalisation.journal("archivage")

#: Tables écrites, dans l'ordre des clés étrangères.
TABLES = ["LOG_NORTH_CAMION", "LOG_NORTH_TRAJET", "LOG_NORTH_ARCHIVAGE_COMMANDE"]

#: Colonnes de chaque table, dans l'ordre des tuples ; la première est la clé primaire.
COLONNES = {
    "LOG_NORTH_CAMION": ["IMMAT_CAMI", "REFERENCE", "REMORQUE_REFRIGEREE", "PTAC", "SPEC_ENR", "SPEC_COL", "DISPO"],
    "LOG_NORTH_TRAJET": ["ID_TRAJET", "IMMAT_CAMI", "TAUX_REMPL", "STATUT"],
    "LOG_NORTH_ARCHIVAGE_COMMANDE": ["ID_COMMANDE", "TYPE_MV", "LOC_DEP", "LOC_ARR", "TYPE_DEP", "TYPE_ARR", "TYPE_MAT",
                                 "QTE_PAL", "TYPE_PAL", "H_DEP", "H_DEP_R", "SPEC_ENR", "SPEC_PTAC", "SPEC_COL",
                                 "COUT_PREST", "STATUT", "JUSTI_REFUS", "ID_TRAJET"],
}

#: Statuts finals : une ligne écrite avec ce statut ne change plus.
STATUTS_FINAUX = {"LOG_NORTH_TRAJET": ("TERMINE", "ANNULE"),
                  "LOG_NORTH_ARCHIVAGE_COMMANDE": ("PARTIE", "REFUSEE", "EXPIREE")}

#: Libellé de chaque motif de refus, pour la colonne ``JUSTI_REFUS``.
LIBELLES_MOTIFS = {
    "REF-DONNEES": "données de la demande inexploitables",
    "REF-EXPIREE": "H_DEP déjà dépassée à la lecture (moteur arrêté)",
    "REF-SPEC": "aucun camion ne respecte la SPEC",
    "REF-FRIGO": "aucun camion frigorifique compatible disponible",
    "REF-CAMION": "aucun camion compatible disponible",
    "REF-DELAI": "départ impossible avant H_DEP",
    "REF-CONDUCTEUR": "règles de conduite incompatibles avec H_DEP",
    "REF-RENTABILITE": "tournée annulée, remplissage sous le seuil",
}

#: Dossier des sauvegardes JSON faites par :func:`reinitialiser` avant de vider les tables.
DOSSIER_SAUVEGARDES = os.path.join(journalisation.DOSSIER, "..", "donnees", "sauvegardes")

#: Justification écrite par :func:`reprendre` pour les commandes laissées acceptées par un moteur arrêté.
JUSTI_REDEMARRAGE = "REF-DELAI : moteur redémarré avant le départ"


# ---------------------------------------------------------------- Lignes (fonctions pures)

def requete_upsert(table: str) -> str:
    """Construit la requête d'insertion ou de mise à jour d'une ligne.

    :param table: Une des :data:`TABLES`.
    :return: ``INSERT … ON CONFLICT (clé) DO UPDATE SET …`` avec des paramètres ``%s``.
    """
    colonnes = COLONNES[table]
    return 'INSERT INTO "%s" (%s) VALUES (%s) ON CONFLICT ("%s") DO UPDATE SET %s' % (
        table, ", ".join('"%s"' % c for c in colonnes), ", ".join(["%s"] * len(colonnes)), colonnes[0],
        ", ".join('"%s" = EXCLUDED."%s"' % (c, c) for c in colonnes[1:]))


def ligne_camion(camion: dict, maintenant: datetime.datetime) -> tuple:
    """Ligne ``LOG_NORTH_CAMION`` d'un camion de la flotte.

    Un camion est disponible s'il n'est réservé par aucune tournée ouverte
    et qu'il a fini sa tournée précédente.

    :param camion: Camion de la flotte (:mod:`flotte`).
    :param maintenant: Heure réelle courante.
    :return: Tuple dans l'ordre de :data:`COLONNES`.
    """
    return (str(camion["IMMAT"])[:20], str(camion["REFERENCE"])[:20], bool(camion["frigo"]), str(camion["PTAC"])[:3],
            str(camion["ENERGIE"])[:12], str(camion.get("COULEUR", ""))[:30],
            camion["tournee"] is None and camion["libre_a"] <= maintenant)


def statut_trajet(tournee: dict, etat: dict, maintenant: datetime.datetime) -> str:
    """Statut ``LOG_NORTH_TRAJET`` d'une tournée à l'instant donné (machine à états).

    - tournée ouverte → ``EN_CONSTITUTION`` ; annulée → ``ANNULE`` ;
    - tournée partie, d'après son plan : fin du plan passée → ``TERMINE`` ;
      dernier lieu de chargement quitté → ``EN_LIVRAISON`` ; premier lieu de
      chargement quitté → ``ENTRE_CHARGEMENTS`` ;
    - avant le premier chargement : import depuis un port → ``VIDE_VERS_PORT``
      jusqu'à l'arrivée au port, puis ``ATTENTE_PORT`` (attente et chargement) ;
      export → ``EN_CONSTITUTION`` (hypothèse : le camion charge encore à
      l'usine, il n'est pas parti).

    :param tournee: Tournée du moteur.
    :param etat: État du moteur (``demandes``).
    :param maintenant: Heure réelle courante.
    :return: Statut autorisé par ``CK_TRAJET_STATUT``.
    """
    if tournee["statut"] == "ouverte":
        return "EN_CONSTITUTION"
    if tournee["statut"] != "partie":
        return "ANNULE"
    plan = tournee["plan"]
    departs = [s["depart"] for s in plan["suivi"].values() if "depart" in s]
    if maintenant >= plan["fin"]:
        return "TERMINE"
    if not departs:
        return "EN_CONSTITUTION"
    if maintenant >= max(departs):
        return "EN_LIVRAISON"
    premier = min(departs)
    if maintenant >= premier:
        return "ENTRE_CHARGEMENTS"
    if any(planification.est_import_port(etat["demandes"][i]) for i in tournee["demandes"]):
        # Arrivée au port : fin du trajet à vide (s'il y en a un) qui précède le chargement
        arrivees = [t["fin"] for t in plan["troncons"] if t["fin"] <= premier]
        return "ATTENTE_PORT" if maintenant >= max(arrivees, default=plan["debut"]) else "VIDE_VERS_PORT"
    return "EN_CONSTITUTION"


def ligne_trajet(tournee: dict, etat: dict, maintenant: datetime.datetime) -> tuple:
    """Ligne ``LOG_NORTH_TRAJET`` d'une tournée.

    Le taux de remplissage est celui du plan (R-KPI-TR : remplissage au départ
    du lieu de chargement, par la surface au sol des palettes), en pour cent ; il suit la tournée pendant qu'elle
    se remplit.

    :param tournee: Tournée du moteur.
    :param etat: État du moteur.
    :param maintenant: Heure réelle courante.
    :return: Tuple dans l'ordre de :data:`COLONNES`.
    """
    remplissage = tournee.get("plan", {}).get("remplissage")
    taux = None if remplissage is None else round(100 * min(max(remplissage, 0.0), 1.0), 1)
    return (tournee["id"], tournee["camion"], taux, statut_trajet(tournee, etat, maintenant))


def statut_commande(demande: dict):
    """Statut ``LOG_NORTH_ARCHIVAGE_COMMANDE`` d'une demande.

    :param demande: Demande préparée.
    :return: ``ACCEPTEE``, ``PARTIE``, ``REFUSEE`` ou ``EXPIREE`` ; ``None`` si la demande n'est pas à
        archiver (hors périmètre ou pas encore décidée).
    :rtype: str or None
    """
    return demande["etat"] if demande["etat"] in ("ACCEPTEE", "PARTIE", "REFUSEE", "EXPIREE") else None


def justification(demande: dict) -> str:
    """Texte de la colonne ``JUSTI_REFUS`` : motif ``REF-…`` et libellé.

    :param demande: Demande refusée ou expirée.
    :return: Texte d'au plus 200 caractères (vide si la demande n'est pas refusée).
    """
    motif = demande.get("motif") or ""
    if not motif:
        return ""
    if demande["etat"] == "EXPIREE":
        texte = "%s : acceptée mais non partie avant H_DEP" % motif
    else:
        texte = "%s : %s" % (motif, LIBELLES_MOTIFS.get(motif, "refus"))
    return texte[:200]


def archivable(demande: dict, statut: str) -> bool:
    """Dit si la ligne de la demande respecte les contraintes de la table.

    Les demandes refusées pour données inexploitables (R-VAL-01, REF-DONNEES)
    peuvent avoir un lieu inconnu, une quantité ou un type invalide : elles
    restent au journal CSV seulement.

    :param demande: Demande préparée.
    :param statut: Statut calculé par :func:`statut_commande`.
    :return: ``True`` si toutes les colonnes ``NOT NULL`` et ``CHECK`` sont respectées.
    """
    return (len(str(demande["id"])) <= 50 and demande.get("TYPE_MVT") in ("Export", "Import")
            and demande["pk_dep"] is not None and demande["pk_arr"] is not None
            and demande.get("TYPE_DEP") in ("USINE", "GMS", "PORT") and demande.get("TYPE_ARR") in ("USINE", "GMS", "PORT")
            and demande.get("TYPE_MAT") in regles.TYPES_MAT and demande.get("TYPE_PAL") in regles.TYPES_PAL
            and demande["qte"] is not None and demande["qte"] > 0 and demande["h_dep"] is not None
            and (statut in ("REFUSEE", "EXPIREE") or demande.get("tournee") is not None)
            and (statut in ("ACCEPTEE", "PARTIE") or bool(demande.get("motif"))))


def ligne_commande(demande: dict, etat: dict, ref: dict, couts: dict):
    """Ligne ``LOG_NORTH_ARCHIVAGE_COMMANDE`` d'une demande décidée.

    - ``SPEC_PTAC``, ``SPEC_ENR``, ``SPEC_COL`` : vide si la clé n'est pas
      demandée ou si aucun camion n'est affecté ; sinon, la SPEC est-elle
      respectée par le camion de la tournée (R-SPEC-02) ;
    - ``H_DEP_R`` : instant comparé à ``H_DEP`` (R-DEL-01), connu au départ ;
    - ``COUT_PREST`` : prix facturé (:func:`planification.prix_prestation`),
      une fois la demande partie et seulement avec les taux de l'Annexe 2
      (les taux par défaut ne donnent pas un prix réel) ;
    - ``JUSTI_REFUS`` : motif ``REF-…`` (:func:`justification`).

    :param demande: Demande préparée.
    :param etat: État du moteur (``flotte``, ``tournees``).
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil (``hypotheses.charger_confidentiel``).
    :return: Tuple dans l'ordre de :data:`COLONNES`, ou ``None`` si la demande n'est pas à archiver.
    :rtype: tuple or None
    """
    statut = statut_commande(demande)
    if statut is None or not archivable(demande, statut):
        return None
    tournee = etat["tournees"].get(demande.get("tournee"))
    camion = etat["flotte"].get(tournee["camion"]) if tournee else None
    spec = {}
    if camion is not None:
        ecarts = regles.verifier_spec(demande, camion)
        spec = {cle: cle not in ecarts for cle in demande["spec"]}
    prix = None
    if statut == "PARTIE" and camion is not None and couts.get("source") != "defaut":
        prix = planification.prix_prestation(demande, camion, ref, couts, demande.get("gamme"))
    return (str(demande["id"]), demande["TYPE_MVT"], demande["pk_dep"], demande["pk_arr"], demande["TYPE_DEP"],
            demande["TYPE_ARR"], demande["TYPE_MAT"], demande["qte"], demande["TYPE_PAL"], demande["h_dep"],
            demande.get("h_depart") if statut == "PARTIE" else None,
            spec.get("ENR"), spec.get("PTAC"), spec.get("COL"), prix, statut, justification(demande) or None,
            demande.get("tournee"))


def lignes_modifiees(archive: dict, etat: dict, ref: dict, couts: dict, maintenant: datetime.datetime) -> dict:
    """Lignes à écrire : celles qui diffèrent de ce qui a déjà été écrit.

    Les lignes déjà écrites avec un statut final (:data:`STATUTS_FINAUX`) ne
    sont pas recalculées. Les demandes protégées (archivées par un lancement
    précédent, voir :func:`reprendre`) ne sont jamais réécrites, ni une ligne
    identique à une ligne déjà rejetée par la base.

    :param archive: Archive ouverte par :func:`ouvrir`.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil.
    :param maintenant: Heure réelle courante.
    :return: ``{table: [tuples]}`` pour chacune des :data:`TABLES`.
    """
    cache, rejetees = archive["cache"], archive["rejetees"]
    candidates = {table: [] for table in TABLES}
    candidates["LOG_NORTH_CAMION"] = [ligne_camion(c, maintenant) for c in etat["flotte"].values()]
    for tournee in etat["tournees"].values():
        ancienne = cache["LOG_NORTH_TRAJET"].get(tournee["id"])
        if ancienne is None or ancienne[-1] not in STATUTS_FINAUX["LOG_NORTH_TRAJET"]:
            candidates["LOG_NORTH_TRAJET"].append(ligne_trajet(tournee, etat, maintenant))
    for demande in etat["demandes"].values():
        if demande["id"] in archive["proteges"]:
            continue
        ancienne = cache["LOG_NORTH_ARCHIVAGE_COMMANDE"].get(demande["id"])
        if ancienne is not None and ancienne[COLONNES["LOG_NORTH_ARCHIVAGE_COMMANDE"].index("STATUT")] in STATUTS_FINAUX["LOG_NORTH_ARCHIVAGE_COMMANDE"]:
            continue
        ligne = ligne_commande(demande, etat, ref, couts)
        if ligne is None:
            if statut_commande(demande) is not None and demande["id"] not in archive["non_archivables"]:
                archive["non_archivables"].add(demande["id"])
                JOURNAL.warning("demande %s (%s, motif %s) non archivable : données hors contraintes de la table, "
                                "au journal CSV seulement", demande["id"], statut_commande(demande), demande.get("motif") or "-")
            continue
        candidates["LOG_NORTH_ARCHIVAGE_COMMANDE"].append(ligne)
    return {table: [ligne for ligne in lignes
                    if cache[table].get(ligne[0]) != ligne and rejetees.get((table, ligne[0])) != ligne]
            for table, lignes in candidates.items()}


# ---------------------------------------------------------------- Base de données

def ouvrir(parametres: dict = None) -> dict:
    """Prépare l'archive, sans se connecter (la connexion est ouverte à la première synchronisation).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    :return: Archive : ``connecter``, ``connexion``, ``erreurs_ligne`` (exceptions propres à une ligne),
        ``cache``, ``rejetees``, ``proteges``, ``non_archivables``, ``repris``, ``verifie``, ``ecrites``,
        ``messages`` (:func:`robustesse.signaler`), ``sante`` (:func:`robustesse.nouvel_etat_bdd`).
    """
    import psycopg2  # disponible sur la VM uniquement

    return nouvelle_archive(lambda: psycopg2.connect(**(parametres or referentiels.BDD)),
                            (psycopg2.IntegrityError, psycopg2.DataError))


def nouvelle_archive(connecter, erreurs_ligne: tuple) -> dict:
    """Crée une archive vide autour d'une fonction de connexion.

    Séparée de :func:`ouvrir` pour les tests (connexion simulée).

    :param connecter: Fonction sans argument qui renvoie une connexion DB-API.
    :type connecter: callable
    :param erreurs_ligne: Exceptions dues au contenu d'une ligne (contrainte, type) plutôt qu'à la connexion.
    :return: Archive (voir :func:`ouvrir`).
    """
    return {"connecter": connecter, "connexion": None, "erreurs_ligne": erreurs_ligne,
            "cache": {table: {} for table in TABLES}, "rejetees": {}, "proteges": set(), "non_archivables": set(),
            "repris": False, "verifie": False, "ecrites": 0, "messages": {}, "sante": robustesse.nouvel_etat_bdd()}


def ecarts_schema(colonnes_base: list) -> list:
    """Compare les colonnes trouvées en base à :data:`COLONNES`.

    :param colonnes_base: ``(table, colonne)`` lus dans ``information_schema.columns``.
    :return: Écarts lisibles (vide si les trois tables ont toutes leurs colonnes).
    """
    trouvees = {}
    for table, colonne in colonnes_base:
        trouvees.setdefault(table, set()).add(colonne)
    ecarts = []
    for table in TABLES:
        if table not in trouvees:
            ecarts.append("%s : table absente (la créer avec 3 - Documentation/tables-nord.sql)" % table)
            continue
        ecarts += ["%s.%s : colonne absente" % (table, c) for c in COLONNES[table] if c not in trouvees[table]]
    return ecarts


def lire_ecarts(curseur: object) -> list:
    """Lit les colonnes des trois tables dans ``information_schema`` (SELECT uniquement).

    :param curseur: Curseur DB-API.
    :return: Écarts (:func:`ecarts_schema`).
    """
    curseur.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_name IN %s",
                    (tuple(TABLES),))
    return ecarts_schema([(ligne[0], ligne[1]) for ligne in curseur.fetchall()])


def deconnecter(archive: dict) -> None:
    """Annule la transaction en cours et ferme la connexion, sans lever d'erreur.

    :param archive: Archive.
    """
    connexion, archive["connexion"] = archive["connexion"], None
    if connexion is None:
        return
    for action in (connexion.rollback, connexion.close):
        try:
            action()
        except Exception:  # connexion déjà perdue : rien d'autre à faire
            pass


def reprendre(archive: dict) -> set:
    """Reprend après un arrêt du moteur (question E18).

    L'état du lancement précédent est perdu : ses commandes encore
    acceptées passent expirées (motif ``REF-DELAI``, :data:`JUSTI_REDEMARRAGE`) et ses tournées en
    constitution passent annulées. Les commandes déjà archivées sont
    protégées : le moteur ne les redécide pas et ne les réécrit pas (sinon,
    relire le CSV du jour referait des refus REF-DONNEES sur des demandes passées).

    :param archive: Archive connectée.
    :return: Identifiants des commandes déjà archivées.
    """
    debut = time.monotonic()
    curseur = archive["connexion"].cursor()
    curseur.execute('UPDATE "LOG_NORTH_ARCHIVAGE_COMMANDE" SET "STATUT" = \'EXPIREE\', "JUSTI_REFUS" = %s '
                    'WHERE "STATUT" = \'ACCEPTEE\'', (JUSTI_REDEMARRAGE,))
    expirees = curseur.rowcount
    curseur.execute('UPDATE "LOG_NORTH_TRAJET" SET "STATUT" = \'ANNULE\' WHERE "STATUT" = \'EN_CONSTITUTION\'')
    annules = curseur.rowcount
    curseur.execute('SELECT "ID_COMMANDE" FROM "LOG_NORTH_ARCHIVAGE_COMMANDE"')
    ids = {ligne[0] for ligne in curseur.fetchall()}
    archive["connexion"].commit()
    journalisation.evenement_sql("REPRISE", "LOG_NORTH_ARCHIVAGE_COMMANDE", expirees, journalisation.duree_ms(debut))
    if expirees or annules:
        JOURNAL.warning("reprise : %d commande(s) acceptée(s) par le lancement précédent passées EXPIREE, "
                        "%d trajet(s) en constitution passés ANNULE", expirees, annules)
    archive["proteges"] |= ids
    archive["repris"] = True
    return ids


def ecrire_ligne_a_ligne(archive: dict, lignes: dict, maintenant: datetime.datetime) -> int:
    """Écrit les lignes une par une, chacune dans sa transaction.

    Sert quand le lot a été refusé pour une contrainte : les lignes en
    cause sont mises de côté (et affichées), les autres sont écrites.

    :param archive: Archive connectée.
    :param lignes: ``{table: [tuples]}``.
    :param maintenant: Heure réelle courante.
    :return: Nombre de lignes écrites.
    """
    connexion, ecrites = archive["connexion"], 0
    for table in TABLES:
        requete = requete_upsert(table)
        for ligne in lignes[table]:
            try:
                connexion.cursor().execute(requete, ligne)
                connexion.commit()
            except archive["erreurs_ligne"] as erreur:
                connexion.rollback()
                archive["rejetees"][(table, ligne[0])] = ligne
                archive["sante"]["rejetees"] += 1
                journalisation.evenement_sql("UPSERT", table, 0, erreur="ligne %s rejetée : %s"
                                             % (ligne[0], journalisation.texte_erreur(erreur)))
                robustesse.signaler(archive["messages"], "ligne", maintenant, "archivage : ligne %s de %s rejetée : %s"
                                    % (ligne[0], table, journalisation.texte_erreur(erreur)), logging.ERROR, "archivage")
                continue
            archive["cache"][table][ligne[0]] = ligne
            ecrites += 1
    return ecrites


def synchroniser(archive: dict, etat: dict, ref: dict, couts: dict, maintenant: datetime.datetime) -> int:
    """Écrit dans les tables ``LOG_NORTH_*`` ce qui a changé depuis la dernière fois.

    Se connecte si besoin ; à la première connexion, appelle :func:`reprendre`
    et ajoute les commandes protégées à ``etat["deja_archivees"]``. Si la base
    est injoignable, le moteur continue : la transaction est annulée, le cache
    n'est pas modifié et tout est réécrit à la connexion suivante. Si une
    ligne viole une contrainte, le lot est repris ligne à ligne
    (:func:`ecrire_ligne_a_ligne`).

    :param archive: Archive ouverte par :func:`ouvrir`.
    :param etat: État du moteur.
    :param ref: Référentiel.
    :param couts: Taux horaires et seuil.
    :param maintenant: Heure réelle (ou simulée) courante.
    :return: Nombre de lignes écrites (0 si rien n'a changé ou si la base est indisponible).
    """
    etape, nb_lignes = "connexion", 0
    try:
        if archive["connexion"] is None:
            debut = time.monotonic()
            archive["connexion"] = archive["connecter"]()
            journalisation.evenement_sql("CONNEXION", "-", 0, journalisation.duree_ms(debut), niveau=logging.DEBUG)
        if not archive["verifie"]:
            etape = "vérification des tables"
            ecarts = lire_ecarts(archive["connexion"].cursor())
            archive["connexion"].rollback()  # fin de la transaction de lecture
            if ecarts:
                raise RuntimeError("tables LOG_NORTH_* non conformes : " + " ; ".join(ecarts))
            archive["verifie"] = True
        if not archive["repris"]:
            etape = "reprise"
            ids = reprendre(archive)
            etat.setdefault("deja_archivees", set()).update(ids)
            JOURNAL.info("archivage : %d commande(s) déjà en base, laissées telles quelles", len(ids))
        etape = "calcul des lignes"
        lignes = lignes_modifiees(archive, etat, ref, couts, maintenant)
        nb_lignes = sum(len(l) for l in lignes.values())
        if not nb_lignes:
            robustesse.noter_succes_bdd(archive["sante"], maintenant, 0, "archivage")
            return 0
        etape = "écriture"
        curseur = archive["connexion"].cursor()
        try:
            for table in TABLES:
                if lignes[table]:
                    debut = time.monotonic()
                    curseur.executemany(requete_upsert(table), lignes[table])
                    journalisation.evenement_sql("UPSERT", table, len(lignes[table]), journalisation.duree_ms(debut))
            archive["connexion"].commit()
        except archive["erreurs_ligne"] as erreur:
            archive["connexion"].rollback()
            journalisation.evenement_sql("UPSERT", "lot", nb_lignes, erreur=erreur, niveau=logging.WARNING)
            JOURNAL.warning("archivage : lot de %d ligne(s) refusé (%s), reprise ligne à ligne",
                            nb_lignes, journalisation.texte_erreur(erreur))
            ecrites = ecrire_ligne_a_ligne(archive, lignes, maintenant)
            archive["ecrites"] += ecrites
            robustesse.noter_succes_bdd(archive["sante"], maintenant, ecrites, "archivage")
            return ecrites
    except Exception as erreur:  # R-ROB-02 / R-ROB-06 : base injoignable, table absente… : le moteur continue, mais le dit
        robustesse.noter_echec_bdd(archive["sante"], maintenant, erreur, nb_lignes)
        journalisation.evenement_sql(etape.upper(), ",".join(TABLES), nb_lignes, erreur=erreur)
        robustesse.signaler(archive["messages"], "base", maintenant,
                            "archivage EN ÉCHEC (%s, %d échec(s) consécutif(s) depuis %s) : %s ; %d ligne(s) en attente, "
                            "nouvel essai au prochain tick" % (etape, archive["sante"]["echecs"], archive["sante"]["depuis"],
                                                               journalisation.texte_erreur(erreur), nb_lignes),
                            logging.ERROR, "archivage")
        deconnecter(archive)
        return 0
    for table in TABLES:
        archive["cache"][table].update((ligne[0], ligne) for ligne in lignes[table])
    archive["ecrites"] += nb_lignes
    robustesse.noter_succes_bdd(archive["sante"], maintenant, nb_lignes, "archivage")
    return nb_lignes


def fermer(archive: dict) -> None:
    """Ferme la connexion de l'archive.

    :param archive: Archive.
    """
    deconnecter(archive)


def verifier_tables(parametres: dict = None) -> list:
    """Vérifie que les trois tables existent avec leurs colonnes (SELECT uniquement).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    :return: Écarts (:func:`ecarts_schema`).
    """
    import psycopg2  # disponible sur la VM uniquement

    connexion = psycopg2.connect(**(parametres or referentiels.BDD))
    try:
        return lire_ecarts(connexion.cursor())
    finally:
        connexion.close()


def afficher_etat(parametres: dict = None) -> None:
    """Affiche le nombre de lignes par statut des trois tables (SELECT uniquement).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    """
    import psycopg2  # disponible sur la VM uniquement

    connexion = psycopg2.connect(**(parametres or referentiels.BDD))
    try:
        curseur = connexion.cursor()
        curseur.execute('SELECT "DISPO", COUNT(*) FROM "LOG_NORTH_CAMION" GROUP BY "DISPO" ORDER BY 1')
        print("LOG_NORTH_CAMION : " + ", ".join("%s %d" % ("disponibles" if d else "occupés", n) for d, n in curseur.fetchall()))
        for table in ("LOG_NORTH_TRAJET", "LOG_NORTH_ARCHIVAGE_COMMANDE"):
            curseur.execute('SELECT "STATUT", COUNT(*) FROM "%s" GROUP BY "STATUT" ORDER BY 1' % table)
            print("%s : %s" % (table, ", ".join("%s %d" % ligne for ligne in curseur.fetchall()) or "vide"))
        curseur.execute('SELECT COUNT(*), COUNT("COUT_PREST"), AVG("TAUX_REMPL") FROM "LOG_NORTH_ARCHIVAGE_COMMANDE" a '
                        'LEFT JOIN "LOG_NORTH_TRAJET" t ON a."ID_TRAJET" = t."ID_TRAJET" WHERE a."STATUT" = \'PARTIE\'')
        parties, avec_prix, remplissage = curseur.fetchone()
        print("Commandes parties : %d, dont %d avec prix ; remplissage moyen de leur trajet : %s %%"
              % (parties, avec_prix, "%.1f" % remplissage if remplissage is not None else "-"))
    finally:
        connexion.close()


# ---------------------------------------------------------------- Remise à zéro (hors moteur)

def moteur_en_marche(repertoire_run: str = None):
    """PID du moteur s'il tourne d'après son fichier PID (``nord.sh``), ``None`` sinon.

    :param repertoire_run: Dossier des fichiers PID (défaut : :data:`journalisation.REPERTOIRE_RUN`).
    :return: PID du moteur en marche, ou ``None``.
    :rtype: int or None
    """
    chemin = os.path.join(repertoire_run or journalisation.REPERTOIRE_RUN, "moteur.pid")
    try:
        with open(chemin, encoding="utf-8") as fichier:
            pid = int(fichier.read().strip())
        os.kill(pid, 0)  # signal 0 : teste seulement l'existence du processus
    except (OSError, ValueError):  # pas de fichier, contenu illisible ou processus disparu
        return None
    return pid


def sauvegarder_tables(curseur: object, chemin: str) -> dict:
    """Copie le contenu des trois tables dans un fichier JSON (SELECT uniquement).

    :param curseur: Curseur DB-API.
    :param chemin: Fichier JSON à écrire (le dossier est créé si besoin).
    :return: Nombre de lignes sauvegardées par table.
    """
    contenu, comptes = {}, {}
    for table in TABLES:
        curseur.execute('SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in COLONNES[table]), table))
        lignes = curseur.fetchall()
        contenu[table] = [dict(zip(COLONNES[table], ligne)) for ligne in lignes]
        comptes[table] = len(lignes)
    os.makedirs(os.path.dirname(os.path.abspath(chemin)), exist_ok=True)
    with open(chemin, "w", encoding="utf-8") as fichier:
        json.dump(contenu, fichier, ensure_ascii=False, indent=1, default=str)  # dates en texte ISO
    return comptes


def reinitialiser(connexion: object, chemin_sauvegarde: str = None) -> dict:
    """Vide les trois tables ``LOG_NORTH_*``, après en avoir fait une sauvegarde JSON.

    Ne touche qu'aux :data:`TABLES` (les nôtres) ; aucune table fournie ni table
    du Contrôle n'est modifiée. Les ``DELETE`` partent des commandes vers les
    camions (ordre inverse des clés étrangères) dans une seule transaction :
    en cas d'erreur, rien n'est supprimé. ``TRUNCATE … CASCADE`` n'est pas
    utilisé, car il viderait aussi toute autre table qui référencerait les nôtres.
    Sans sauvegarde réussie, rien n'est supprimé.

    :param connexion: Connexion DB-API ouverte.
    :param chemin_sauvegarde: Fichier JSON de sauvegarde ; ``None`` pour ne pas sauvegarder.
    :return: Nombre de lignes supprimées par table.
    :raises Exception: toute erreur de la base ou d'écriture de la sauvegarde (transaction annulée).
    """
    curseur, supprimees = connexion.cursor(), {}
    try:
        if chemin_sauvegarde:
            sauvegarder_tables(curseur, chemin_sauvegarde)
        for table in reversed(TABLES):
            debut = time.monotonic()
            curseur.execute('DELETE FROM "%s"' % table)
            supprimees[table] = curseur.rowcount
            journalisation.evenement_sql("DELETE", table, curseur.rowcount, journalisation.duree_ms(debut))
        connexion.commit()
    except Exception as erreur:
        connexion.rollback()
        journalisation.evenement_sql("DELETE", ", ".join(TABLES), 0, erreur=erreur)
        raise
    JOURNAL.warning("tables LOG_NORTH_* vidées : %s", ", ".join("%s %d" % t for t in supprimees.items()))
    return supprimees


def lancer_reinitialisation(confirme: bool, sauvegarde: bool = True, parametres: dict = None) -> int:
    """Remise à zéro en ligne de commande : vérifications, confirmation, sauvegarde puis suppression.

    Refuse si le moteur tourne : il garde en mémoire ce qu'il a déjà écrit et
    ne réécrirait que les lignes modifiées ensuite (tables incohérentes).

    :param confirme: ``True`` pour ne pas demander de taper ``OUI``.
    :param sauvegarde: ``False`` pour ne pas écrire de sauvegarde JSON.
    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    :return: Code de sortie (0 si les tables ont été vidées).
    """
    import psycopg2  # disponible sur la VM uniquement

    pid = moteur_en_marche()
    if pid is not None:
        print("Refusé : le moteur tourne (PID %d). L'arrêter d'abord : sh nord.sh arreter" % pid)
        return 1
    connexion = psycopg2.connect(**(parametres or referentiels.BDD))
    try:
        curseur = connexion.cursor()
        for table in TABLES:
            curseur.execute('SELECT COUNT(*) FROM "%s"' % table)
            print("  %-30s %d ligne(s)" % (table, curseur.fetchone()[0]))
        connexion.rollback()  # fin de la transaction de lecture
        if not confirme and input("Supprimer toutes ces lignes ? Taper OUI : ").strip() != "OUI":
            print("Annulé, rien n'a été supprimé.")
            return 1
        chemin = None
        if sauvegarde:
            chemin = os.path.join(DOSSIER_SAUVEGARDES,
                                  datetime.datetime.now().strftime("log_north_%Y%m%d-%H%M%S.json"))
        supprimees = reinitialiser(connexion, chemin)
    finally:
        connexion.close()
    print("Tables vidées : " + ", ".join("%s %d" % t for t in supprimees.items()))
    if chemin:
        print("Sauvegarde : " + os.path.abspath(chemin))
    return 0


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Tables LOG_NORTH_* : état de l'archivage du moteur NORTH")
    parseur.add_argument("--etat", action="store_true", help="nombre de lignes par statut (lecture seule)")
    parseur.add_argument("--verifier", action="store_true",
                         help="vérifie que les trois tables existent avec leurs colonnes (code de sortie 1 sinon)")
    parseur.add_argument("--reinitialiser", action="store_true",
                         help="vide les trois tables LOG_NORTH_* (moteur arrêté ; sauvegarde JSON dans ../donnees/sauvegardes/)")
    parseur.add_argument("--oui", action="store_true", help="avec --reinitialiser : ne pas demander de confirmation")
    parseur.add_argument("--sans-sauvegarde", action="store_true", help="avec --reinitialiser : pas de sauvegarde JSON")
    options = parseur.parse_args()
    if options.reinitialiser:
        journalisation.configurer("archivage", False, sql=True)  # moteur arrêté : seul écrivain de sql.jsonl
        sys.exit(lancer_reinitialisation(options.oui, not options.sans_sauvegarde))
    if options.verifier:
        ecarts_trouves = verifier_tables()
        for ecart in ecarts_trouves:
            print("! " + ecart)
        print("Tables LOG_NORTH_* conformes" if not ecarts_trouves else "%d écart(s)" % len(ecarts_trouves))
        sys.exit(1 if ecarts_trouves else 0)
    if options.etat:
        afficher_etat()
    else:
        parseur.print_help()
