# Logistique NORTH : collecte des demandes et moteur d'acceptation

Code du groupe Logistique NORTH (structure NORTH-EU d'EURO-TransFRET) pour le projet « Du capteur jusqu'au Cloud » (ENSIBS, 2026-2027). La chaîne a deux étapes :

1. `collecteur.py` écoute l'ERP de Garden Food (broadcast UDP) et enregistre chaque demande de transport ;
2. `moteur.py` décide de chaque demande NORTH (accepter, refuser avec un motif `REF-…`, ignorer), construit les tournées, les fait partir, tient le journal de traçabilité et, sur la VM, met à jour nos tables `LOG_NORTH_CAMION`, `LOG_NORTH_TRAJET` et `LOG_NORTH_ARCHIVAGE_COMMANDE` (`archivage.py`).

Python 3.10+, bibliothèque standard uniquement ; `psycopg2` seulement sur la VM, pour lire la base et écrire nos tables `LOG_NORTH_*`. Toutes les commandes se lancent depuis ce dossier.

Ce dossier est `4 - Spécifications et code/1 - DEV/` du projet (copie publique : https://github.com/TaoHuaLiu/transport-problem-simulation). Les chemins en `../` renvoient au reste du projet, qui n'est pas dans le dépôt.

## Fichiers

| Fichier | Rôle | Règles appliquées |
|---|---|---|
| `collecteur.py` | Reçoit les messages UDP de l'ERP, les sauvegarde (JSONL brut + CSV) | R-COL-01, R-ROB-03/04 |
| `robustesse.py` | Garde-fous communs : SIGTERM traité comme `Ctrl+C`, erreurs répétées journalisées une fois par minute (masquées comptées), santé des liens avec la base | R-ROB-01/02/03/06 |
| `journalisation.py` | Journaux communs : console, `../donnees/logs/<service>.jsonl` (rotation), journal SQL `sql.jsonl`, santé `../donnees/run/sante_<service>.json` ; `--sante`, `--sql N`, `--erreurs N` | R-ROB-06 |
| `simulateur_erp.py` | Imite l'ERP en local, pour tester hors du VLAN de l'ENSIBS | — |
| `schema_bdd.py` | Affiche le schéma de la base `UE_ENS_DCJC` (lecture seule) | — |
| `hypotheses.py` | Toutes les valeurs que le sujet ne fixe pas, avec leur question ouverte (A1–E18) ; lecture des données confidentielles (Annexe 2) | — |
| `referentiels.py` | Tables de la base (copie JSON par `--export`) ou référentiel d'exemple (`--exemple`) ; durées, attente au port, capacités | R-VAL-01, R-DEL-01, R-CAP-01, R-FLO-01 |
| `regles.py` | Contraintes dures : périmètre, validité, SPEC, frigo, plancher ; taux de remplissage par surface | R-PER-01/02, R-VAL-01/02, R-SPEC-01/02, R-FRA-01/02, R-CAP-01, R-KPI-TR |
| `flotte.py` | Fiches camion → flotte NORTH (position, disponibilité, conduite) ; état gardé d'un lancement à l'autre | R-FLO-01, R-ROB-07 |
| `planification.py` | Plan d'une tournée (chargements puis livraisons), pauses, gamme, coût, prix ; table D-DEPART | D-DEPART, R-RENT-01, R-DEL-01, R-CON-01, R-CAP-01, R-DUR-01, R-GAM, R-SPEC-02, R-OPT-01/03 |
| `decision.py` | Table D-ACC : ignorer, refuser ou accepter dans le meilleur plan | D-ACC, R-OPT-01/02/03 |
| `tracabilite.py` | Journal CSV des événements ; calcul de TA et TR (surface au sol) | R-KPI-TA, R-KPI-TR |
| `archivage.py` | Écrit l'état du moteur dans `LOG_NORTH_CAMION`, `LOG_NORTH_TRAJET`, `LOG_NORTH_ARCHIVAGE_COMMANDE` (`../3 - Documentation/tables-nord.sql`) ; `--etat` pour compter les lignes, `--reinitialiser` pour les vider (moteur arrêté, sauvegarde JSON avant) | R-SPEC-02, R-DEL-01, R-GAM, R-KPI-TR |
| `tracabilite_controle.py` | Remplit les tables du Contrôle `TRACABILITE_COMMANDE` et `TRACABILITE_TRAJET` pour nos commandes (code de refus D-JUSTIF dans `JUSTIF_REFUS`) ; `--verifier` (schéma) et `--etat` (nos lignes), en lecture seule | R-CTL-01, D-JUSTIF, R-ROB-02 |
| `moteur.py` | Boucle principale, en rejeu (horloge simulée) ou en direct ; enchaîne D-ACC puis D-DEPART, chaque demande et chaque tournée protégée | R-ROB-01/02/03/07 |
| `audit.py` | Recalcule les indicateurs d'audit depuis le journal et les demandes reçues, sans passer par l'état du moteur ; contrôles de traçabilité | R-KPI-TA, R-KPI-TR, R-PER-01/02 |
| `tableau_bord.py` | Serveur web local (sockets) du tableau de bord d'audit ; rapport JSON ponctuel (`--instantane`) | — |
| `nord.sh` | Lancement sur la VM : démarrer, arrêter, état, surveillance des trois processus ; `audit` = production | R-ROB-05 |
| `tableau_bord.html`, `static/Chart_min.js` | Page du tableau de bord et Chart.js 2.5 (copie locale, licence MIT) | — |
| `tests/test_decision.py` | Un test par ligne des tables D-ACC et D-DEPART ; taux de remplissage par surface | — |
| `tests/test_audit.py` | Statuts finals, TA, TR, contrôles et barème du tableau de bord | — |
| `tests/test_robustesse.py` | Garde-fous d'exploitation : erreurs sur une demande, une tournée, la base, le CSV, le collecteur ; SIGTERM ; journaux, santé et alertes ; redémarrage (échéances passées, état des camions) | R-ROB-01 à 04, 06, 07, R-VAL-02 |
| `tests/test_mode_audit.py` | Mode audit : démarrage sur la base seulement, fiches de la VM et Annexe 2 obligatoires, options interdites (moteur et `nord.sh`) | R-ROB-05 |
| `tests/test_tracabilite_controle.py` | Un test par ligne de D-JUSTIF ; lignes des deux tables du Contrôle ; écriture limitée à nos lignes, reprise après arrêt (base simulée) ; schéma | R-CTL-01, D-JUSTIF |
| `tests/test_archivage.py` | Lignes et contraintes des tables `LOG_NORTH_*`, statuts des trajets dans le temps, prix, écritures (base simulée), reprise après arrêt | — |

Les identifiants `R-…`, `D-…` et `REF-…` renvoient au catalogue `../3 - Documentation/processus.md` §2.5 ; chaque docstring cite la règle qu'elle applique, et `../3 - Documentation/tracabilite.md` relie chaque règle à sa fonction et à son test.

## Documentation

Documentation en cinq couches (`../3 - Documentation/standard-documentation.md`) ; les fichiers Markdown et les deux pages web suivent le code (état au 20/11/2026). Sur le dépôt GitHub, `processus.md` et `tracabilite.md` sont copiés dans `docs/`.

| Document | Contenu |
|---|---|
| `../3 - Documentation/standard-documentation.md` | Standard de documentation de l'équipe |
| `../3 - Documentation/processus.md` | Couches 1 à 3 : correspondance avec le pseudo-BPMN du sujet (p. 24) et RACI ; tables D-ACC et D-DEPART, machines à états, catalogue des règles `R-…` et des motifs `REF-…` ; glossaire et diagramme de classes |
| `../3 - Documentation/tracabilite.md` | Couche 4 : exigence → règle → fonction → test → preuve → état ; test de chaque ligne des tables de décision |
| `../3 - Documentation/tables-nord.sql`, `schema.md`, `schema-bdd.md` | Nos tables `LOG_NORTH_*` ; schéma des tables fournies |
| [Architecture du moteur NORTH](https://claude.ai/artifact/EjLcKZ13a82uCpUaLsTpMp) | Modules et dépendances, boucle du moteur, logigrammes D-ACC et D-DEPART, calcul d'un plan, état en mémoire, hypothèses A1–E18 |
| [Pile documentaire NORTH](https://claude.ai/artifact/WwhHYhgT4Jkip186zLZKF9) | Les cinq couches du standard, suivies sur une demande d'exemple |

## Données hors du dossier de code

Le code lit et écrit dans `../donnees/` (hors git) :

| Chemin | Contenu | Produit par |
|---|---|---|
| `../donnees/demandes/demandes_AAAA-MM-JJ.{jsonl,csv}` | Demandes reçues de l'ERP | `collecteur.py` |
| `../donnees/referentiels.json` | Copie des tables de la base | `referentiels.py --export` |
| `../donnees/annexe2_confidentiel.json` | Taux horaires et seuil de rentabilité (Annexe 2, **confidentiel, à ne pas publier**) | à créer à la main |
| `../donnees/moteur/evenements_*.csv` | Journal du moteur | `moteur.py` |

Sans `annexe2_confidentiel.json`, le moteur tourne avec des taux fictifs (seul le classement des plans compte). Le seuil de départ θ et la marge W ne dépendent pas de ce fichier : par défaut θ = 0,85 (`hypotheses.SEUIL_DEPART_DEFAUT`, comparé au remplissage au départ du premier lieu de chargement) et W = 0,25 h projet (`hypotheses.MARGE_DEPART_H`), valeurs du balayage du 2026-11-16 (`../../_build/rejeu-c13/`) ; `--seuil` et `--marge` les remplacent. Une tournée peut charger dans plusieurs lieux (`hypotheses.REGROUPER_ENTRE_LIEUX_DEP`) : tous les chargements, puis toutes les livraisons. Sans `referentiels.json`, il génère un référentiel d'exemple dans `_build/` (valeurs du sujet + estimations, signalées au démarrage).

## Sur la VM ENSIBS

`nord.sh` lance et surveille la chaîne (collecteur, moteur `--direct --bdd --archiver`, tableau de bord sur le VLAN) :

```bash
sh nord.sh audit                          # PRODUCTION pour l'audit (dans tmux) : vérifie base, fiches VM, Annexe 2, puis lance et surveille
sh nord.sh demarrer                       # collecteur + copie du référentiel + moteur + tableau de bord (http://IP_VM:10000/)
sh nord.sh demarrer --seuil 0.85 --marge 0.5   # politique θ / W du balayage (sinon valeurs de hypotheses.py)
sh nord.sh etat                           # processus, dernières lignes des logs, nombre de demandes et d'événements
sh nord.sh surveiller                     # relance toutes les 30 s ce qui s'est arrêté (pendant l'audit)
sh nord.sh redemarrer                     # moteur et tableau de bord seulement : le collecteur continue
sh nord.sh arreter [--tout]               # --tout arrête aussi le collecteur
sh nord.sh journaux                       # suit les logs
```

Autres options : `--sans-controle` (n'écrit pas les tables du Contrôle ; par défaut, le moteur les remplit dès qu'il lit la base), `--sans-bdd` (référentiel `../donnees/referentiels.json`, si la base est indisponible ; le moteur n'écrit alors pas les tables `LOG_NORTH_*`), `--sans-archive` (lit la base sans écrire les tables `LOG_NORTH_*`), `--confidentiel` (valeurs de l'Annexe 2 dans le tableau de bord : usage interne), `--port N`, `--local` (tableau de bord sur `127.0.0.1` seulement). Les options du dernier lancement sont mémorisées dans `../donnees/run/options` : `etat`, `redemarrer` et `surveiller` sans option les reprennent. PID dans `../donnees/run/`, logs dans `../donnees/logs/`. Relancer le moteur ouvre un nouveau journal : les tournées en constitution sont perdues (question E18), mais l'état des camions est repris (R-ROB-07) ; le tableau de bord lit le journal le plus récent. Pour repartir de camions tous libres à leur usine le même jour (après des essais, avant l'audit), supprimer `../donnees/run/flotte_moteur.json` moteur arrêté.

À la main, sans le script :

```bash
mkdir -p ../donnees
nohup python3 collecteur.py >> ../donnees/collecteur.log 2>&1 &   # en continu
python3 referentiels.py --export                                   # copie la base dans ../donnees/referentiels.json
python3 moteur.py --direct --bdd --archiver --controle                     # lit le CSV du jour, rafraîchit la base chaque minute réelle, écrit les tables LOG_NORTH_*
python3 tracabilite_controle.py --verifier                         # tables du Contrôle présentes et conformes à leur spécification ?
python3 tracabilite_controle.py --etat                             # nos lignes par statut et code de refus (lecture seule)
python3 archivage.py --etat                                        # lignes par statut dans les tables LOG_NORTH_* (lecture seule)
python3 archivage.py --reinitialiser                               # vide LOG_NORTH_* (moteur arrêté ; demande OUI ; sauvegarde ../donnees/sauvegardes/)
python3 tableau_bord.py --hote 0.0.0.0                             # tableau de bord : http://IP_VM:10000/
```

### Mode audit (production, R-ROB-05)

Pour l'audit, lancer `sh nord.sh audit` dans `tmux` ou `screen` (options permises : `--seuil`, `--marge`, `--port`). Le script :

1. refuse de démarrer, sans rien arrêter, si la base ne répond pas, si nos tables `LOG_NORTH_*` manquent ou sont incomplètes (`archivage.py --verifier` ; les créer ou migrer avec `../3 - Documentation/tables-nord.sql`), si `/mnt/tp4010/DuCapteurJusquauCloud/FichesCamion/` manque ou si `../donnees/annexe2_confidentiel.json` est absent (`paquet_vm.sh --confidentiel`) ;
2. relance moteur et tableau de bord en mode audit (le collecteur continue s'il tourne) : `moteur.py --audit` = `--direct --bdd --archiver`, sans copie JSON, sans fiches locales, sans coûts fictifs ; tableau de bord sur `0.0.0.0`, jamais `--confidentiel` ;
3. surveille et relance toutes les 30 s ce qui s'arrête (un moteur refusé faute de base est relancé dès qu'elle répond).

`--sans-bdd`, `--sans-archive`, `--confidentiel` et `--local` sont refusés ; `--debug` est permis (collecteur et moteur seulement : le tableau de bord public n'expose pas les journaux). `sh nord.sh etat` affiche « mode : AUDIT ». Après l'audit, `demarrer` ou `redemarrer` avec des options sortent du mode audit (avertissement).

### Journaux, santé et débogage (R-ROB-06)

| Fichier (dans `../donnees/`) | Contenu | Écrit par |
|---|---|---|
| `logs/<service>.log` | Sortie console (lignes courtes ; `!` avertissement, `!!` erreur) | `nord.sh` (sortie standard) |
| `logs/<service>.jsonl` | Une ligne JSON par message : heure à la ms, niveau, module, PID, trace d'exception ; rotation 10 Mo × 5 | `journalisation.configurer()` |
| `logs/sql.jsonl` | Événements SQL du moteur : opération, table, lignes, durée, erreur (jamais les valeurs) | `journalisation.evenement_sql()` |
| `run/sante_<service>.json` | Santé courante : dernier tick ou message, référentiel, archivage, traçabilité Contrôle (dernier succès, échecs, en attente, rejetées) | moteur à chaque tick, collecteur toutes les 5 s |

Consultation : `sh nord.sh sante`, `sh nord.sh erreurs [N]`, `sh nord.sh sql [N]`, `sh nord.sh etat` (résumé). Sur le tableau de bord : carte « Santé de la chaîne » (alertes critiques : moteur ou collecteur arrêté ou muet, écriture en base en échec) et carte « Journal SQL » (filtre « erreurs seulement »).

Mode débogage : `--debug` sur `collecteur.py`, `moteur.py`, `tableau_bord.py`, ou `sh nord.sh demarrer --debug`. La console passe au niveau DEBUG (millisecondes, module), le moteur écrit chaque décision, chaque départ, chaque tick et chaque requête SQL, le collecteur chaque message, le tableau de bord chaque requête HTTP avec sa durée. Le tableau de bord en débogage ouvre `/api/debug` et un panneau (derniers avertissements et erreurs de tous les services, avec trace ; dernières requêtes HTTP ; santé brute) ; `?debug=1` dans l'URL affiche le panneau ou dit que le serveur n'est pas en débogage.

### Garde-fous d'exploitation (R-ROB-01 à R-ROB-07)

Un moteur relancé perd son état en mémoire : ses commandes acceptées et pas encore parties passent expirées (REF-DELAI), ce qui fait baisser le TA. Un collecteur arrêté perd les messages de l'ERP pour toujours. Les services ne s'arrêtent donc que sur demande :

| Panne | Comportement | Où le voir |
|---|---|---|
| Exception sur une demande | Refusée REF-DONNEES (ignorée si hors NORTH) ; les suivantes sont décidées | `moteur.log`, journal |
| Exception sur une tournée | Reste ouverte, réexaminée au tick suivant | `moteur.log` |
| Ligne illisible dans le CSV du jour | Ignorée et comptée | `moteur.log`, résumé à l'arrêt |
| Base injoignable ou lente | Délais bornés (connexion 5 s, requête 10 s, `referentiels.BDD`) ; dernier référentiel gardé, nouvel essai chaque minute ; tables `LOG_NORTH_*` rattrapées au retour de la base | `moteur.log` (« base injoignable ») |
| Base injoignable au démarrage | Le moteur part sur `../donnees/referentiels.json` (écrit par `nord.sh demarrer`, ou `--referentiels`) | première ligne de `moteur.log` |
| Tick plus long que 5 s | Signalé (« le moteur prend du retard ») | `moteur.log` |
| Erreur d'écriture ou de socket du collecteur | Signalée, la collecte continue ; le brut est écrit avant toute lecture du CSV | `collecteur.log` |
| Moteur relancé (plantage ou `redemarrer`) | Commandes acceptées et pas parties de l'ancien lancement refusées code 4 ; demandes déjà écrites protégées ; camions partis gardés occupés jusqu'à la fin de leur tournée, à leur dernier lieu (`../donnees/run/flotte_moteur.json`, même jour réel, R-ROB-07) ; le CSV du jour est relu : celles reçues pendant l'arrêt encore valables sont décidées, celles déjà échues refusées REF-EXPIREE (code 4, R-VAL-02) | `moteur.log` (« reprise »), journal |
| `nord.sh arreter` (SIGTERM) | Arrêt propre : dernière synchronisation des tables `LOG_NORTH_*`, résumé | fin de `moteur.log` |
| Écriture `LOG_NORTH_*` ou `TRACABILITE_*` en échec (table absente, base tombée) | Erreur journalisée avec la cause et les lignes en attente ; nouvel essai à chaque tick ; rétablissement annoncé ; lignes jamais écrites signalées à l'arrêt (R-ROB-06) | tableau de bord (alerte critique, tuile rouge, journal SQL), `sh nord.sh sante`, `moteur.log` (« EN ÉCHEC ») |
| Ligne refusée par une contrainte | Mise de côté, les autres sont écrites ; comptée en « rejetées » | journal SQL, `sh nord.sh sql` |
| Disque des journaux plein ou interdit | Le service continue avec la console seule | `<service>.log` |

Une même erreur n'est affichée qu'une fois par minute ; les suivantes sont comptées, annoncées au message suivant (« N message(s) semblable(s) masqué(s) ») et écrites au niveau DEBUG ; toutes sont comptées dans le résumé à l'arrêt. `tests/test_robustesse.py` vérifie chacun de ces comportements, et `paquet_vm.sh` refuse d'emballer si un test échoue.

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

## Calcul des indicateurs (KPI)

Indicateurs de l'audit, calculés deux fois : par le moteur (`tracabilite.calculer_kpi`, sur son état) et par `audit.py` (sur le journal et les fichiers du collecteur seulement, comme le Contrôle). Formules détaillées : `../../3 - Contractualisation/methode-contrat.md` §1 et §3.

### Taux d'acceptation (TA, R-KPI-TA)

```
TA = demandes NORTH parties à temps / demandes NORTH reçues
```

- **Reçues** : demandes dont l'usine de départ (export) ou d'arrivée (import) est Amsterdam, Düsseldorf ou Manchester, reçues dans la fenêtre d'audit.
- **Parties** : statut final `PARTIE`, départ contrôlé avant `H_DEP` (pour un import portuaire, `H_DEP` porte sur le départ du camion vers le port).
- Une demande acquittée puis refusée ou expirée compte comme **refusée**. Une demande encore sans décision compte au dénominateur seulement.
- `audit.py` donne aussi `ta_decidees` (sans les demandes en attente) et le TA par usine et par sens.

### Taux de remplissage (TR, R-KPI-TR)

Le TR d'une tournée se mesure à son départ du **premier lieu de chargement** (le retour des camions n'est pas géré) ; le TR de la fenêtre est la moyenne sur les tournées parties. Une tournée qui charge ensuite dans un autre lieu garde le TR de son premier départ (`plan["remplissage"]`) ; le remplissage le plus haut de la tournée est gardé à part (`plan["remplissage_max"]`, information).

Méthode par surface au sol :

```
S_palette  : EPAL 1,20 × 0,80 = 0,96 m² ; EPALh 0,80 × 0,60 = 0,48 m² ; ISO 1,20 × 1,00 = 1,20 m²
S_camion   = max sur les types (capacité TRUCKS_CAPACITY(PTAC, type) × S_palette)
           = capacité EPAL × 0,96 m² pour tous les PTAC de la table
TR tournée = Σ (QTE_PAL × S_palette) des demandes chargées / S_camion
```

| PTAC | S_camion | TR camion plein en EPAL | en ISO | en EPALh |
|---|---|---|---|---|
| 12t | 18 × 0,96 = 17,28 m² | 100 % | 14 × 1,20 → 97,2 % | 33 × 0,48 → 91,7 % |
| 19t | 21 × 0,96 = 20,16 m² | 100 % | 16 × 1,20 → 95,2 % | 39 × 0,48 → 92,9 % |
| 26t | 22 × 0,96 = 21,12 m² | 100 % | 17 × 1,20 → 96,6 % | 41 × 0,48 → 93,2 % |
| 32t, 44t | 33 × 0,96 = 31,68 m² | 100 % | 26 × 1,20 → 98,5 % | 63 × 0,48 → 95,5 % |

- La surface de référence est celle du type qui occupe le plus de plancher (EPAL). Prendre la capacité EPALh × 0,48 m² donnerait une surface plus petite que celle d'un camion plein en EPAL ou en ISO, donc des TR au-dessus de 100 % (jusqu'à 109 % pour un 12t plein en EPAL).
- Le sujet ne donne pas les dimensions des palettes : celles d'EPALh supposent une demi-palette 800 × 600 (hypothèse à faire valider par le Contrôle).
- Le TR ne sert qu'à mesurer. L'admissibilité d'un chargement reste la règle R-CAP-01 (`regles.equivalent_plancher`) : Σ QTE_PAL / capacité(PTAC, type) ≤ 1, plus une rangée perdue par type de palette en plus. Le contrôle par surface laisserait passer 36 EPALh dans un 12t, alors que la table en autorise 33.
- **Dans le code** : `regles.surface_camion` et `regles.taux_remplissage` (surfaces dans `hypotheses.SURFACE_PALETTE_M2`). Le plan de tournée (`planification.evaluer_plan`) s'en sert pour le remplissage de chaque tronçon et au départ ; ce taux est écrit dans la colonne `REMPLISSAGE` du journal, que `audit.py` relit, et dans `LOG_NORTH_TRAJET.TAUX_REMPL`. Le seuil de départ θ (D-DEPART) et le prix facturé (part de surface de la demande / seuil) utilisent la même mesure.
- `tr_troncons` (information) : Σ remplissage × durée / Σ durée sur tous les tronçons, trajets à vide compris.

### Gammes de prix (R-GAM)

Chaque demande partie reçoit la gamme la plus haute dont les deux limites sont respectées (`hypotheses.GAMMES`) :

| Gamme | Arrêts entre chargement et livraison | Allongement (durée réelle / trajet direct) |
|---|---|---|
| haute | 0 | ≤ 1,1 |
| médiane | ≤ 1 | ≤ 1,5 |
| basse | ≤ 3 | ≤ 2,5 |

Au-delà, la demande est hors contrat (pas de plan). Une SPEC non respectée fait descendre la demande d'une gamme (R-SPEC-02). Indicateur suivi : part de gamme haute = demandes en gamme haute / demandes parties.

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

## Tables de traçabilité du Contrôle (R-CTL-01, D-JUSTIF)

`tracabilite_controle.py`, séparé de l'archivage, remplit les deux tables spécifiées par le Contrôle, pour nos commandes seulement (`moteur.py --controle`, imposé en mode audit) :

- `TRACABILITE_COMMANDE` : une ligne par commande NORTH décidée, tenue à jour ; lieux en nom (comme l'ERP), `SPEC` en JSON, `ID_ENTRE` = `N1`, `N2`, `N3`… (lettre de la zone puis numéro de la commande chez nous, dans l'ordre d'écriture, repris après un redémarrage), `STATUT` = `ACCEPTEE` ou `REFUSEE` seulement (une commande partie reste `ACCEPTEE`, une acceptée expirée avant son départ devient `REFUSEE` code 4), `H_DEP_R` = heure réelle de départ (celle comparée à `H_DEP`), `JUSTIF_REFUS` = code 1 à 8 du Contrôle d'après la table D-JUSTIF (`processus.md` §2.7), en attendant le tableur commun annoncé ;
- `TRACABILITE_TRAJET` : une ligne par commande partie : immatriculation, `TYPE_CAMI` (`FRAIS`/`SEC`), `CAPA_CAMI` (PTAC sans espace, ex. `19t`, format confirmé), `COUT_PRESTA` (prix facturé, avec l'Annexe 2 seulement), SPEC tenues, `TAUX_REMPLI` du trajet (en %).

Chaque ligne est écrite par un `UPDATE` limité à notre ligne (même `ID_COMMANDE` et même `ID_ENTRE` `N…`), puis un `INSERT` s'il n'y avait rien : les tables n'ont pas besoin de contrainte d'unicité, et une ligne d'un autre groupe n'est jamais modifiée. Après un redémarrage, nos commandes laissées acceptées sans `H_DEP_R` passent `REFUSEE` (code 4), nos lignes existantes ne sont pas réécrites et la numérotation reprend après le plus grand `N…`. Base en panne ou tables absentes : le moteur continue et réessaie à chaque tick. Les valeurs non fixées par la spécification (libellés de `TYPE_CAMI`, `CAPA_CAMI` = PTAC, `TAUX_REMPLI` en %) sont dans `hypotheses.py` (question A1) : à confirmer avec le Contrôle.

## Tables `LOG_NORTH_*` (archivage)

Avec `--archiver` (mode direct seulement), le moteur tient à jour les trois tables créées par `../3 - Documentation/tables-nord.sql`. À chaque tick, `archivage.py` recalcule les lignes depuis l'état du moteur et n'écrit que celles qui ont changé (`INSERT … ON CONFLICT DO UPDATE`, une transaction, ordre camions → trajets → commandes). Aucune table fournie n'est modifiée.

| Table | Contenu | Remarques |
|---|---|---|
| `LOG_NORTH_CAMION` | Un camion de la flotte NORTH par ligne, écrit au démarrage | `DISPO` : ni réservé par une tournée ouverte, ni en tournée |
| `LOG_NORTH_TRAJET` | Une tournée par ligne ; `ID_TRAJET` = `T<AAMMJJhhmmss>-<n°>` (heure de démarrage du moteur : unique d'un lancement à l'autre) | `TAUX_REMPL` en % (remplissage du plan) ; `STATUT` suit le plan dans le temps : `EN_CONSTITUTION` (ouverte, ou export encore en chargement à l'usine), `VIDE_VERS_PORT`, `ATTENTE_PORT`, `ENTRE_CHARGEMENTS`, `EN_LIVRAISON`, `TERMINE`, `ANNULE` |
| `LOG_NORTH_ARCHIVAGE_COMMANDE` | Une demande NORTH décidée par ligne | `STATUT` = état du moteur ; `JUSTI_REFUS` = motif `REF-…` et libellé ; `H_DEP_R` = instant comparé à `H_DEP` (R-DEL-01) ; `SPEC_*` vide si non demandée, sinon respectée ou non par le camion ; `COUT_PREST` = **prix facturé** (part d'un camion au seuil de rentabilité × coefficient de gamme, `methode-contrat.md` §3), seulement pour une demande partie et avec `annexe2_confidentiel.json` |

- Les refus REF-DONNEES dont un champ viole une contrainte (lieu inconnu, type invalide…) ne sont pas archivés : ils restent au journal CSV.
- Base injoignable : le moteur continue, les lignes en retard sont écrites quand la connexion revient (un message par minute au plus dans le log). Une ligne refusée par une contrainte est mise de côté et affichée, sans bloquer les autres.
- Redémarrage : les commandes encore `ACCEPTEE` du lancement précédent passent `EXPIREE` (« moteur redémarré avant le départ »), ses trajets `EN_CONSTITUTION` passent `ANNULE`, et les demandes déjà en base ne sont ni redécidées ni réécrites. Les trajets déjà partis gardent leur dernier statut.

## Journal du moteur

Une ligne par événement (`ACQUITTEMENT`, `REFUS` avec motif, `DEPART`, `TRONCON`, `PARTIE` avec gamme, `EXPIRATION`), séparateur `;`. Les demandes hors périmètre NORTH ne sont pas journalisées. Ce journal reste la source de référence (les tables `LOG_NORTH_*` en sont le reflet en base). La table de traçabilité partagée, dont le schéma est fixé par le groupe Contrôle (question A1), n'est pas encore écrite.

Unités : 1 minute réelle = 1 heure projet. Les dates sont en heure réelle, les durées en heures projet.
