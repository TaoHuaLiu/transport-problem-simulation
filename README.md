# Logistique NORTH : collecte des demandes et moteur d'acceptation

Code du groupe Logistique NORTH (structure NORTH-EU d'EURO-TransFRET) pour le projet « Du capteur jusqu'au Cloud » (ENSIBS, 2026-2027). La chaîne a deux étapes :

1. `collecteur.py` écoute l'ERP de Garden Food (broadcast UDP) et enregistre chaque demande de transport ;
2. `moteur.py` décide de chaque demande NORTH (accepter, refuser avec un motif `REF-…`, ignorer), construit les tournées, les fait partir, tient le journal de traçabilité et, sur la VM, met à jour nos tables `CAMION_NORTH`, `TRAJET_NORTH` et `ARCHIVAGE_COMMANDE_NORTH` (`archivage.py`).

Python 3.10+, bibliothèque standard uniquement ; `psycopg2` seulement sur la VM, pour lire la base et écrire nos tables `*_NORTH`. Toutes les commandes se lancent depuis ce dossier.

Ce dossier est `4 - Spécifications et code/1 - DEV/` du projet (copie publique : https://github.com/TaoHuaLiu/transport-problem-simulation). Les chemins en `../` renvoient au reste du projet, qui n'est pas dans le dépôt.

## Fichiers

| Fichier | Rôle | Règles appliquées |
|---|---|---|
| `collecteur.py` | Reçoit les messages UDP de l'ERP, les sauvegarde (JSONL brut + CSV) | — |
| `simulateur_erp.py` | Imite l'ERP en local, pour tester hors du VLAN de l'ENSIBS | — |
| `schema_bdd.py` | Affiche le schéma de la base `UE_ENS_DCJC` (lecture seule) | — |
| `hypotheses.py` | Toutes les valeurs que le sujet ne fixe pas, avec leur question ouverte (A1–E18) ; lecture des données confidentielles (Annexe 2) | — |
| `referentiels.py` | Tables de la base (copie JSON par `--export`) ou référentiel d'exemple (`--exemple`) ; durées, attente au port, capacités | R-VAL-01, R-DEL-01 |
| `regles.py` | Contraintes dures : périmètre, validité, SPEC, frigo, plancher | R-PER-01/02, R-VAL-01, R-SPEC-01/02, R-FRA-01/02, R-CAP-01 |
| `flotte.py` | Fiches camion → flotte NORTH (position, disponibilité, conduite) | R-FLO-01 |
| `planification.py` | Plan d'une tournée (chargements puis livraisons), pauses, gamme, coût ; table D-DEPART | R-DEL-01, R-CON-01, R-GAM, R-SPEC-02 |
| `decision.py` | Table D-ACC : ignorer, refuser ou accepter dans le meilleur plan | D-ACC, R-OPT-01/02/03 |
| `tracabilite.py` | Journal CSV des événements ; calcul de TA et TR | R-KPI-TA, R-KPI-TR |
| `archivage.py` | Écrit l'état du moteur dans `CAMION_NORTH`, `TRAJET_NORTH`, `ARCHIVAGE_COMMANDE_NORTH` (`../3 - Documentation/tables-nord.sql`) ; `--etat` pour compter les lignes | R-SPEC-02, R-DEL-01, R-GAM, R-KPI-TR |
| `moteur.py` | Boucle principale, en rejeu (horloge simulée) ou en direct | — |
| `audit.py` | Recalcule les indicateurs d'audit depuis le journal et les demandes reçues, sans passer par l'état du moteur ; contrôles de traçabilité | R-KPI-TA, R-KPI-TR, R-PER-01/02 |
| `tableau_bord.py` | Serveur web local (sockets) du tableau de bord d'audit ; rapport JSON ponctuel (`--instantane`) | — |
| `nord.sh` | Lancement sur la VM : démarrer, arrêter, état, surveillance des trois processus | — |
| `tableau_bord.html`, `static/Chart_min.js` | Page du tableau de bord et Chart.js 2.5 (copie locale, licence MIT) | — |
| `tests/test_decision.py` | Un test par ligne des tables D-ACC et D-DEPART | — |
| `tests/test_audit.py` | Statuts finals, TA, TR, contrôles et barème du tableau de bord | — |
| `tests/test_archivage.py` | Lignes et contraintes des tables `*_NORTH`, statuts des trajets dans le temps, prix, écritures (base simulée), reprise après arrêt | — |

Les identifiants `R-…`, `D-…` et `REF-…` renvoient au catalogue `../3 - Documentation/regles-acceptation.docx` ; chaque docstring cite la règle qu'elle applique.

## Documentation

| Document | Contenu |
|---|---|
| [Architecture du moteur NORTH](https://claude.ai/artifact/EjLcKZ13a82uCpUaLsTpMp) | Modules et dépendances, boucle du moteur, tables D-ACC et D-DEPART, calcul d'un plan de tournée, état en mémoire, hypothèses A1–E18 et écarts connus |
| [Pile documentaire NORTH](https://claude.ai/artifact/WwhHYhgT4Jkip186zLZKF9) | Les cinq couches du standard de documentation : processus et RACI, règles métier, glossaire et modèle du domaine, matrice de traçabilité (règle → fonction → test → preuve), code |
| `../3 - Documentation/regles-acceptation.docx` | Catalogue des règles `R-…` et motifs `REF-…` (sa matrice de traçabilité date d'avant le codage : celle de la pile documentaire suit le code) |
| `../3 - Documentation/standard-documentation.md` | Standard de documentation de l'équipe |

## Données hors du dossier de code

Le code lit et écrit dans `../donnees/` (hors git) :

| Chemin | Contenu | Produit par |
|---|---|---|
| `../donnees/demandes/demandes_AAAA-MM-JJ.{jsonl,csv}` | Demandes reçues de l'ERP | `collecteur.py` |
| `../donnees/referentiels.json` | Copie des tables de la base | `referentiels.py --export` |
| `../donnees/annexe2_confidentiel.json` | Taux horaires et seuil de rentabilité (Annexe 2, **confidentiel, à ne pas publier**) | à créer à la main |
| `../donnees/moteur/evenements_*.csv` | Journal du moteur | `moteur.py` |

Sans `annexe2_confidentiel.json`, le moteur tourne avec des taux fictifs (seul le classement des plans compte) et le seuil θ de `hypotheses.SEUIL_DEPART_DEFAUT`. Sans `referentiels.json`, il génère un référentiel d'exemple dans `_build/` (valeurs du sujet + estimations, signalées au démarrage).

## Sur la VM ENSIBS

`nord.sh` lance et surveille la chaîne (collecteur, moteur `--direct --bdd --archiver`, tableau de bord sur le VLAN) :

```bash
sh nord.sh demarrer                       # collecteur + copie du référentiel + moteur + tableau de bord (http://IP_VM:10000/)
sh nord.sh demarrer --seuil 0.85 --marge 0.5   # politique θ / W du balayage (sinon valeurs de hypotheses.py)
sh nord.sh etat                           # processus, dernières lignes des logs, nombre de demandes et d'événements
sh nord.sh surveiller                     # relance toutes les 30 s ce qui s'est arrêté (pendant l'audit)
sh nord.sh redemarrer                     # moteur et tableau de bord seulement : le collecteur continue
sh nord.sh arreter [--tout]               # --tout arrête aussi le collecteur
sh nord.sh journaux                       # suit les logs
```

Autres options : `--sans-bdd` (référentiel `../donnees/referentiels.json`, si la base est indisponible ; le moteur n'écrit alors pas les tables `*_NORTH`), `--sans-archive` (lit la base sans écrire les tables `*_NORTH`), `--confidentiel` (valeurs de l'Annexe 2 dans le tableau de bord : usage interne), `--port N`, `--local` (tableau de bord sur `127.0.0.1` seulement). Les options du dernier lancement sont mémorisées dans `../donnees/run/options` : `etat`, `redemarrer` et `surveiller` sans option les reprennent. PID dans `../donnees/run/`, logs dans `../donnees/logs/`. Relancer le moteur ouvre un nouveau journal (l'état n'est pas repris, question E18) : le tableau de bord lit le plus récent.

À la main, sans le script :

```bash
mkdir -p ../donnees
nohup python3 collecteur.py >> ../donnees/collecteur.log 2>&1 &   # en continu
python3 referentiels.py --export                                   # copie la base dans ../donnees/referentiels.json
python3 moteur.py --direct --bdd --archiver                        # lit le CSV du jour, rafraîchit la base chaque minute réelle, écrit les tables *_NORTH
python3 archivage.py --etat                                        # lignes par statut dans les tables *_NORTH (lecture seule)
python3 tableau_bord.py --hote 0.0.0.0                             # tableau de bord : http://IP_VM:10000/
```

Le collecteur doit tourner sans interruption : l'ERP ne renvoie pas les messages manqués. Le relancer le même jour ne crée pas de doublons (identifiants déjà présents dans le CSV ignorés). Fiches camion lues dans `/mnt/tp4010/DuCapteurJusquauCloud/FichesCamion/`.

## En local

```bash
python3 moteur.py --rejeu "../../6 - Data/Briefcase/demandes_2026-10-19.jsonl"
python3 -m unittest discover -s tests
```

Le rejeu relit un fichier du collecteur (`.jsonl` ou `.csv`) avec une horloge simulée et affiche TA, TR, refus par motif et gammes. Si le dossier des fiches de la VM est absent, il prend `../../6 - Data/camions/` (`--fiches` pour un autre dossier). Autres options : `--referentiels`, `--sortie`, `--seuil` (θ), `--marge` (W, heures projet).

Pour tester le collecteur sans le VLAN, dans deux terminaux :

```bash
python3 collecteur.py --sortie _build/test   # terminal 1
python3 simulateur_erp.py                    # terminal 2 : 1 à 3 demandes toutes les 1 à 3 s vers 127.0.0.1:40040
```

## Tableau de bord d'audit

`tableau_bord.py` sert une page web (sockets bruts, comme `RSCR_SERVEUR_TCP2HTML.py` ; Chart.js local, sans internet) qui se met à jour toutes les 5 secondes. Les indicateurs sont recalculés par `audit.py` à partir des **fichiers seulement** : le dernier journal `../donnees/moteur/evenements_*.csv` et les demandes du collecteur `../donnees/demandes/`. Le périmètre NORTH y est recalculé sans `regles.py`. Le tableau de bord voit donc les mêmes chiffres que le Contrôle, et repère une demande oubliée ou traitée à tort par le moteur.

| Bloc | Contenu |
|---|---|
| Indicateurs | TA (parties / reçues, R-KPI-TA), TR au départ (R-KPI-TR), part de gamme haute, contrôles bloquants passés ; pour chacun, écart à la cible et note du barème (écart ≤ 3 % → 10, ≤ 7 % → 8,5, ≤ 15 % → 6, ≤ 35 % → 3, ≤ 50 % → 1) |
| Fenêtre d'audit | Début et durée (2 h par défaut) ; « Démarrer l'audit maintenant » ; barre de progression. Une demande reçue dans la fenêtre et partie après sa fin compte comme partie |
| Graphiques | Statut final des demandes par heure de réception, TA cumulé, remplissage par heure de départ, distribution du remplissage, gammes, motifs de refus |
| Contrôles | Demandes sans décision, acquittées en retard, parties après `H_DEP`, acquittées puis refusées, hors périmètre au journal, absentes du collecteur, décisions lentes ; en direct, moteur ou collecteur silencieux depuis plus de 60 s |
| Exports | `/api/rapport.json` (rapport de la fenêtre, pièce pour l'audit) et `/api/statuts.csv` (statut final de chaque demande NORTH, à comparer avec le décompte du Contrôle) |

```bash
python3 tableau_bord.py                                    # http://localhost:10000/
python3 tableau_bord.py --hote 0.0.0.0 --port 10000       # accessible depuis le VLAN 40
python3 tableau_bord.py --cible-ta 0.85 --cible-tr 0.75   # cibles du contrat (sinon ../donnees/cibles_contrat.json)
python3 tableau_bord.py --instantane ../donnees/audit/rapport.json   # un rapport JSON, sans serveur
python3 tableau_bord.py --journal "../../_build/rejeu-audit/balayage/x10-th0.85-w0.5" \
    --demandes "../../_build/rejeu-audit/demandes_2h.jsonl"         # sur un rejeu, en local
```

- **Cibles** : `../donnees/cibles_contrat.json` (hors git), par exemple `{"ta": 0.85, "tr": 0.75, "part_haute": 0.3}`, ou les options `--cible-…`. Sans cible, seule celle du TA (notre proposition, 85 %) est affichée.
- **Horloge** : en direct, l'heure réelle ; si le journal n'a pas bougé depuis 2 minutes (rejeu, moteur arrêté), l'heure de son dernier événement. On peut forcer avec `--horloge reelle|journal`.
- **Données confidentielles** : le seuil de rentabilité et le coût estimé par demande (hors énergie) tirés de l'Annexe 2 ne s'affichent qu'avec `--confidentiel`. Sans cette option, on peut ouvrir la page depuis le VLAN ou la montrer au Contrôle.
- **Mise en forme** : système de design ENSIBS (charte UBS 2020) : verts ENSIBS et bleu UBS, thème clair, typographie française ; polices Avant Garde Gothic, sinon Jost, URW Gothic ou Century Gothic, et Arial pour le texte (aucune police téléchargée : la VM n’a pas internet).
- Une requête à la fois : le serveur relit les fichiers seulement s'ils ont changé (≈ 0,1 s pour un audit de 2 h à 10 fois la flotte).

## Fichiers du collecteur

Une paire de fichiers par jour **réel** :

- `demandes_AAAA-MM-JJ.jsonl` : une ligne par message reçu, copié tel quel (`H_RECEPTION`, `EMETTEUR`, `MESSAGE`), écrit avant le décodage : un message illisible y reste ;
- `demandes_AAAA-MM-JJ.csv` : une ligne par demande, séparateur `;`, UTF-8.

| Colonne | Contenu |
|---|---|
| `ID_DEMANDE` | Identifiant unique envoyé par l'ERP |
| `H_RECEPTION` | Heure de réception (`AAAA-MM-JJ hh:mm:ss`, heure réelle) |
| `TYPE_MVT` … `TYPE_PAL` | Champs de la demande, tels qu'envoyés |
| `H_DEP` | Heure limite, format d'origine `JJ/MM/AA hh:mm:ss` (heure réelle) |
| `H_DEP_ISO` | La même au format `AAAA-MM-JJ hh:mm:ss` |
| `SPEC` | Spécificités, telles qu'envoyées |
| `SPEC_PTAC`, `SPEC_ENR`, `SPEC_COL` | Valeur de chaque clef connue de `SPEC` (vide si absente) |
| `SPEC_AUTRES` | Clefs inconnues de `SPEC`, au format `CLEF=valeur` |

Les noms de villes sont recopiés tels qu'envoyés (sans accents dans l'échantillon du 2026-10-19) ; le moteur les normalise quand même avant la jointure avec `LOCATIONS_NAMES`. Volume attendu : environ 2 500 demandes par heure réelle.

## Tables `*_NORTH` (archivage)

Avec `--archiver` (mode direct seulement), le moteur tient à jour les trois tables créées par `../3 - Documentation/tables-nord.sql`. À chaque tick, `archivage.py` recalcule les lignes depuis l'état du moteur et n'écrit que celles qui ont changé (`INSERT … ON CONFLICT DO UPDATE`, une transaction, ordre camions → trajets → commandes). Aucune table fournie n'est modifiée.

| Table | Contenu | Remarques |
|---|---|---|
| `CAMION_NORTH` | Un camion de la flotte NORTH par ligne, écrit au démarrage | `DISPO` : ni réservé par une tournée ouverte, ni en tournée |
| `TRAJET_NORTH` | Une tournée par ligne ; `ID_TRAJET` = `T<AAMMJJhhmmss>-<n°>` (heure de démarrage du moteur : unique d'un lancement à l'autre) | `TAUX_REMPL` en % (remplissage du plan) ; `STATUT` suit le plan dans le temps : `EN_CONSTITUTION` (ouverte, ou export encore en chargement à l'usine), `VIDE_VERS_PORT`, `ATTENTE_PORT`, `ENTRE_CHARGEMENTS`, `EN_LIVRAISON`, `TERMINE`, `ANNULE` |
| `ARCHIVAGE_COMMANDE_NORTH` | Une demande NORTH décidée par ligne | `STATUT` = état du moteur ; `JUSTI_REFUS` = motif `REF-…` et libellé ; `H_DEP_R` = instant comparé à `H_DEP` (R-DEL-01) ; `SPEC_*` vide si non demandée, sinon respectée ou non par le camion ; `COUT_PREST` = **prix facturé** (part d'un camion au seuil de rentabilité × coefficient de gamme, `methode-contrat.md` §3), seulement pour une demande partie et avec `annexe2_confidentiel.json` |

- Les refus REF-DONNEES dont un champ viole une contrainte (lieu inconnu, type invalide…) ne sont pas archivés : ils restent au journal CSV.
- Base injoignable : le moteur continue, les lignes en retard sont écrites quand la connexion revient (un message par minute au plus dans le log). Une ligne refusée par une contrainte est mise de côté et affichée, sans bloquer les autres.
- Redémarrage : les commandes encore `ACCEPTEE` du lancement précédent passent `EXPIREE` (« moteur redémarré avant le départ »), ses trajets `EN_CONSTITUTION` passent `ANNULE`, et les demandes déjà en base ne sont ni redécidées ni réécrites. Les trajets déjà partis gardent leur dernier statut.

## Journal du moteur

Une ligne par événement (`ACQUITTEMENT`, `REFUS` avec motif, `DEPART`, `TRONCON`, `PARTIE` avec gamme, `EXPIRATION`), séparateur `;`. Les demandes hors périmètre NORTH ne sont pas journalisées. Ce journal reste la source de référence (les tables `*_NORTH` en sont le reflet en base). La table de traçabilité partagée, dont le schéma est fixé par le groupe Contrôle (question A1), n'est pas encore écrite.

Unités : 1 minute réelle = 1 heure projet. Les dates sont en heure réelle, les durées en heures projet.
