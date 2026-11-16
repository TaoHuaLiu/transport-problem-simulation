"""Archivage du moteur NORTH dans les tables ``*_NORTH`` de la base.

Tient à jour les trois tables propres au transporteur, créées par
``3 - Documentation/tables-nord.sql`` :

- ``CAMION_NORTH`` : une ligne par camion de la flotte NORTH, avec sa disponibilité ;
- ``TRAJET_NORTH`` : une ligne par tournée, avec son remplissage et son statut ;
- ``ARCHIVAGE_COMMANDE_NORTH`` : une ligne par demande NORTH décidée
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

    python3 archivage.py --etat     # nombre de lignes par statut dans les trois tables
"""

import argparse
import datetime

import planification
import referentiels
import regles

#: Tables écrites, dans l'ordre des clés étrangères.
TABLES = ["CAMION_NORTH", "TRAJET_NORTH", "ARCHIVAGE_COMMANDE_NORTH"]

#: Colonnes de chaque table, dans l'ordre des tuples ; la première est la clé primaire.
COLONNES = {
    "CAMION_NORTH": ["IMMAT_CAMI", "REFERENCE", "REMORQUE_REFRIGEREE", "PTAC", "SPEC_ENR", "SPEC_COL", "DISPO"],
    "TRAJET_NORTH": ["ID_TRAJET", "IMMAT_CAMI", "TAUX_REMPL", "STATUT"],
    "ARCHIVAGE_COMMANDE_NORTH": ["ID_COMMANDE", "TYPE_MV", "LOC_DEP", "LOC_ARR", "TYPE_DEP", "TYPE_ARR", "TYPE_MAT",
                                 "QTE_PAL", "TYPE_PAL", "H_DEP", "H_DEP_R", "SPEC_ENR", "SPEC_PTAC", "SPEC_COL",
                                 "COUT_PREST", "STATUT", "JUSTI_REFUS", "ID_TRAJET"],
}

#: Statuts finals : une ligne écrite avec ce statut ne change plus.
STATUTS_FINAUX = {"TRAJET_NORTH": ("TERMINE", "ANNULE"),
                  "ARCHIVAGE_COMMANDE_NORTH": ("PARTIE", "REFUSEE", "EXPIREE")}

#: Libellé de chaque motif de refus, pour la colonne ``JUSTI_REFUS``.
LIBELLES_MOTIFS = {
    "REF-DONNEES": "données de la demande inexploitables",
    "REF-SPEC": "aucun camion ne respecte la SPEC",
    "REF-FRIGO": "aucun camion frigorifique compatible disponible",
    "REF-CAMION": "aucun camion compatible disponible",
    "REF-DELAI": "départ impossible avant H_DEP",
    "REF-CONDUCTEUR": "règles de conduite incompatibles avec H_DEP",
    "REF-RENTABILITE": "tournée annulée, remplissage sous le seuil",
}

#: Justification écrite par :func:`reprendre` pour les commandes laissées acceptées par un moteur arrêté.
JUSTI_REDEMARRAGE = "REF-DELAI : moteur redémarré avant le départ"

#: Délai minimal entre deux messages d'erreur de la base dans la console.
DELAI_MESSAGES = datetime.timedelta(minutes=1)


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
    """Ligne ``CAMION_NORTH`` d'un camion de la flotte.

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
    """Statut ``TRAJET_NORTH`` d'une tournée à l'instant donné (machine à états).

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
    """Ligne ``TRAJET_NORTH`` d'une tournée.

    Le taux de remplissage est celui du plan (R-KPI-TR : remplissage au départ
    du lieu de chargement), en pour cent ; il suit la tournée pendant qu'elle
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
    """Statut ``ARCHIVAGE_COMMANDE_NORTH`` d'une demande.

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
    """Ligne ``ARCHIVAGE_COMMANDE_NORTH`` d'une demande décidée.

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
    candidates["CAMION_NORTH"] = [ligne_camion(c, maintenant) for c in etat["flotte"].values()]
    for tournee in etat["tournees"].values():
        ancienne = cache["TRAJET_NORTH"].get(tournee["id"])
        if ancienne is None or ancienne[-1] not in STATUTS_FINAUX["TRAJET_NORTH"]:
            candidates["TRAJET_NORTH"].append(ligne_trajet(tournee, etat, maintenant))
    for demande in etat["demandes"].values():
        if demande["id"] in archive["proteges"]:
            continue
        ancienne = cache["ARCHIVAGE_COMMANDE_NORTH"].get(demande["id"])
        if ancienne is not None and ancienne[COLONNES["ARCHIVAGE_COMMANDE_NORTH"].index("STATUT")] in STATUTS_FINAUX["ARCHIVAGE_COMMANDE_NORTH"]:
            continue
        ligne = ligne_commande(demande, etat, ref, couts)
        if ligne is None:
            if statut_commande(demande) is not None:
                archive["non_archivables"].add(demande["id"])
            continue
        candidates["ARCHIVAGE_COMMANDE_NORTH"].append(ligne)
    return {table: [ligne for ligne in lignes
                    if cache[table].get(ligne[0]) != ligne and rejetees.get((table, ligne[0])) != ligne]
            for table, lignes in candidates.items()}


# ---------------------------------------------------------------- Base de données

def ouvrir(parametres: dict = None) -> dict:
    """Prépare l'archive, sans se connecter (la connexion est ouverte à la première synchronisation).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    :return: Archive : ``connecter``, ``connexion``, ``erreurs_ligne`` (exceptions propres à une ligne),
        ``cache``, ``rejetees``, ``proteges``, ``non_archivables``, ``repris``, ``ecrites``, ``dernier_message``.
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
            "repris": False, "ecrites": 0, "dernier_message": None}


def signaler(archive: dict, maintenant: datetime.datetime, message: str) -> None:
    """Affiche une erreur de la base, au plus une fois par :data:`DELAI_MESSAGES`.

    :param archive: Archive.
    :param maintenant: Heure réelle courante.
    :param message: Texte à afficher.
    """
    if archive["dernier_message"] is None or maintenant - archive["dernier_message"] >= DELAI_MESSAGES:
        print("[%s] ! archivage : %s" % (maintenant.strftime("%H:%M:%S"), message), flush=True)
        archive["dernier_message"] = maintenant


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
    acceptées passent expirées (:data:`JUSTI_REDEMARRAGE`) et ses tournées en
    constitution passent annulées. Les commandes déjà archivées sont
    protégées : le moteur ne les redécide pas et ne les réécrit pas (sinon,
    relire le CSV du jour referait des refus REF-DONNEES sur des demandes passées).

    :param archive: Archive connectée.
    :return: Identifiants des commandes déjà archivées.
    """
    curseur = archive["connexion"].cursor()
    curseur.execute('UPDATE "ARCHIVAGE_COMMANDE_NORTH" SET "STATUT" = \'EXPIREE\', "JUSTI_REFUS" = %s '
                    'WHERE "STATUT" = \'ACCEPTEE\'', (JUSTI_REDEMARRAGE,))
    curseur.execute('UPDATE "TRAJET_NORTH" SET "STATUT" = \'ANNULE\' WHERE "STATUT" = \'EN_CONSTITUTION\'')
    curseur.execute('SELECT "ID_COMMANDE" FROM "ARCHIVAGE_COMMANDE_NORTH"')
    ids = {ligne[0] for ligne in curseur.fetchall()}
    archive["connexion"].commit()
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
                print("[%s] ! archivage : ligne %s de %s rejetée : %s"
                      % (maintenant.strftime("%H:%M:%S"), ligne[0], table, str(erreur).strip()), flush=True)
                continue
            archive["cache"][table][ligne[0]] = ligne
            ecrites += 1
    return ecrites


def synchroniser(archive: dict, etat: dict, ref: dict, couts: dict, maintenant: datetime.datetime) -> int:
    """Écrit dans les tables ``*_NORTH`` ce qui a changé depuis la dernière fois.

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
    try:
        if archive["connexion"] is None:
            archive["connexion"] = archive["connecter"]()
        if not archive["repris"]:
            ids = reprendre(archive)
            etat.setdefault("deja_archivees", set()).update(ids)
            print("Archivage : %d commande(s) déjà en base, laissées telles quelles" % len(ids), flush=True)
        lignes = lignes_modifiees(archive, etat, ref, couts, maintenant)
        if not any(lignes.values()):
            return 0
        curseur = archive["connexion"].cursor()
        try:
            for table in TABLES:
                if lignes[table]:
                    curseur.executemany(requete_upsert(table), lignes[table])
            archive["connexion"].commit()
        except archive["erreurs_ligne"]:
            archive["connexion"].rollback()
            ecrites = ecrire_ligne_a_ligne(archive, lignes, maintenant)
            archive["ecrites"] += ecrites
            return ecrites
    except Exception as erreur:  # base injoignable, table absente… : le moteur ne doit pas s'arrêter
        signaler(archive, maintenant, "%s (nouvel essai au prochain tick)" % str(erreur).strip())
        deconnecter(archive)
        return 0
    for table in TABLES:
        archive["cache"][table].update((ligne[0], ligne) for ligne in lignes[table])
    ecrites = sum(len(l) for l in lignes.values())
    archive["ecrites"] += ecrites
    return ecrites


def fermer(archive: dict) -> None:
    """Ferme la connexion de l'archive.

    :param archive: Archive.
    """
    deconnecter(archive)


def afficher_etat(parametres: dict = None) -> None:
    """Affiche le nombre de lignes par statut des trois tables (SELECT uniquement).

    :param parametres: Paramètres de connexion (défaut : :data:`referentiels.BDD`).
    """
    import psycopg2  # disponible sur la VM uniquement

    connexion = psycopg2.connect(**(parametres or referentiels.BDD))
    try:
        curseur = connexion.cursor()
        curseur.execute('SELECT "DISPO", COUNT(*) FROM "CAMION_NORTH" GROUP BY "DISPO" ORDER BY 1')
        print("CAMION_NORTH : " + ", ".join("%s %d" % ("disponibles" if d else "occupés", n) for d, n in curseur.fetchall()))
        for table in ("TRAJET_NORTH", "ARCHIVAGE_COMMANDE_NORTH"):
            curseur.execute('SELECT "STATUT", COUNT(*) FROM "%s" GROUP BY "STATUT" ORDER BY 1' % table)
            print("%s : %s" % (table, ", ".join("%s %d" % ligne for ligne in curseur.fetchall()) or "vide"))
        curseur.execute('SELECT COUNT(*), COUNT("COUT_PREST"), AVG("TAUX_REMPL") FROM "ARCHIVAGE_COMMANDE_NORTH" a '
                        'LEFT JOIN "TRAJET_NORTH" t ON a."ID_TRAJET" = t."ID_TRAJET" WHERE a."STATUT" = \'PARTIE\'')
        parties, avec_prix, remplissage = curseur.fetchone()
        print("Commandes parties : %d, dont %d avec prix ; remplissage moyen de leur trajet : %s %%"
              % (parties, avec_prix, "%.1f" % remplissage if remplissage is not None else "-"))
    finally:
        connexion.close()


if __name__ == "__main__":
    parseur = argparse.ArgumentParser(description="Tables *_NORTH : état de l'archivage du moteur NORTH")
    parseur.add_argument("--etat", action="store_true", help="nombre de lignes par statut (lecture seule)")
    options = parseur.parse_args()
    if options.etat:
        afficher_etat()
    else:
        parseur.print_help()
