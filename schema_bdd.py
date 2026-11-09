"""Affiche le schéma de la base de données PostgreSQL du projet (UE_ENS_DCJC).

Pour chaque table ou vue visible par l'utilisateur ``dcjc`` :

* ses colonnes (ordre, nom, type, NULL autorisé, valeur par défaut) ;
* les clés primaires / étrangères déclarées ;
* les droits de l'utilisateur (SELECT, INSERT, UPDATE...).

Les requêtes interrogent le catalogue standard ``information_schema`` :
aucune table du projet n'est modifiée.

Lancement depuis un terminal (sur la VM ENSIBS) : ``python3 schema_bdd.py``
"""

import psycopg2

from referentiels import BDD  # paramètres de connexion, définis une seule fois

##Requêtes SQL sur le catalogue (les schémas système sont exclus)
REQ_COLONNES = """
    SELECT c.table_schema, c.table_name, t.table_type,
           c.ordinal_position, c.column_name, c.data_type,
           c.character_maximum_length, c.is_nullable, c.column_default
    FROM information_schema.columns c
    JOIN information_schema.tables t
         ON t.table_schema = c.table_schema AND t.table_name = c.table_name
    WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

REQ_CLES = """
    SELECT tc.table_schema, tc.table_name, tc.constraint_type, kcu.column_name,
           ccu.table_name AS ref_table, ccu.column_name AS ref_colonne
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
         ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
    LEFT JOIN information_schema.constraint_column_usage ccu
         ON ccu.constraint_name = tc.constraint_name AND tc.constraint_type = 'FOREIGN KEY'
    WHERE tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')
      AND tc.table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
"""

REQ_DROITS = """
    SELECT table_schema, table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type)
    FROM information_schema.table_privileges
    WHERE grantee IN (current_user, 'PUBLIC')
      AND table_schema NOT IN ('pg_catalog', 'information_schema')
    GROUP BY table_schema, table_name
"""


def executer(cur, reqSQL):
    """Exécute une requête SQL et retourne toutes les lignes de la réponse.

    :param cur: curseur ouvert sur la base
    :type cur: psycopg2.extensions.cursor
    :param reqSQL: requête SQL à exécuter
    :type reqSQL: str
    :return: les lignes de la réponse, une ligne par tuple
    :rtype: list[tuple]
    :raises psycopg2.Error: si la requête échoue
    """
    cur.execute(reqSQL)
    return cur.fetchall()


def lire_schema(cur):
    """Construit le schéma de la base sous forme de dictionnaire.

    :param cur: curseur ouvert sur la base
    :type cur: psycopg2.extensions.cursor
    :return: ``{(schema, table): {"type", "colonnes", "cles", "droits"}}`` où
        ``colonnes`` est une liste de ``[position, nom, type, nullable, defaut]``,
        ``cles`` une liste de chaines (``"PK col"`` ou ``"FK col -> table.col"``)
        et ``droits`` une chaine (ex. ``"INSERT, SELECT"``)
    :rtype: dict
    """
    schema = {}

    #Colonnes de chaque table / vue
    for sch, table, type_table, pos, nom, type_col, taille, nullable, defaut in executer(cur, REQ_COLONNES):
        entree = schema.setdefault((sch, table), {"type": type_table, "colonnes": [], "cles": [], "droits": ""})
        if taille is not None:      #Ex : character varying(50)
            type_col = "%s(%d)" % (type_col, taille)
        entree["colonnes"].append([pos, nom, type_col, nullable, defaut])

    #Clés primaires et étrangères
    for sch, table, type_cle, colonne, ref_table, ref_col in executer(cur, REQ_CLES):
        if (sch, table) in schema:
            if type_cle == 'FOREIGN KEY':
                schema[(sch, table)]["cles"].append("FK %s -> %s.%s" % (colonne, ref_table, ref_col))
            else:
                schema[(sch, table)]["cles"].append("PK %s" % colonne)

    #Droits de l'utilisateur connecté
    for sch, table, droits in executer(cur, REQ_DROITS):
        if (sch, table) in schema:
            schema[(sch, table)]["droits"] = droits

    return schema


def afficher_schema(schema):
    """Affiche le schéma dans le terminal, une section par table.

    :param schema: dictionnaire retourné par :func:`lire_schema`
    :type schema: dict
    :return: rien (affichage uniquement)
    :rtype: None
    """
    for (sch, table), entree in schema.items():
        print("\n=== %s.\"%s\" (%s) ===" % (sch, table, entree["type"]))
        print("Droits : %s" % (entree["droits"] or "aucun"))
        if entree["cles"]:
            print("Clés   : %s" % " ; ".join(entree["cles"]))
        print("  %-4s %-25s %-30s %-6s %s" % ("#", "COLONNE", "TYPE", "NULL", "DEFAUT"))
        for pos, nom, type_col, nullable, defaut in entree["colonnes"]:
            print("  %-4d %-25s %-30s %-6s %s" % (pos, nom, type_col, nullable, defaut if defaut is not None else ""))


##Programme principal
if __name__ == "__main__":
    ##Etablissement de la connexion au serveur de base de données
    conn = psycopg2.connect(**BDD)
    cur = conn.cursor()
    print("\nConnexion à la Base de données '%s' du serveur '%s'" % (BDD["dbname"], BDD["host"]))

    schema = lire_schema(cur)
    afficher_schema(schema)
    print("\n%d tables/vues trouvées" % len(schema))

    ##Fermeture de la connexion au serveur
    conn.close()
    print("\nFin du programme")
