# Logistique NORTH : collecte des demandes et moteur d'acceptation

Code du groupe Logistique NORTH (structure NORTH-EU d'EURO-TransFRET) pour le projet « Du capteur jusqu'au Cloud » (ENSIBS, 2026-2027). La chaîne a deux étapes :

1. `collecteur.py` écoute l'ERP de Garden Food (broadcast UDP) et enregistre chaque demande de transport ;
2. `moteur.py` décide de chaque demande NORTH (accepter, refuser avec un motif `REF-…`, ignorer), construit les tournées, les fait partir et tient le journal de traçabilité.

Python 3.10+, bibliothèque standard uniquement ; `psycopg2` seulement pour lire la base sur la VM. Toutes les commandes se lancent depuis ce dossier.

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
| `moteur.py` | Boucle principale, en rejeu (horloge simulée) ou en direct | — |
| `tests/test_decision.py` | Un test par ligne des tables D-ACC et D-DEPART | — |

Les identifiants `R-…`, `D-…` et `REF-…` renvoient au catalogue `../3 - Documentation/regles-acceptation.docx` ; chaque docstring cite la règle qu'elle applique. Architecture du moteur : https://claude.ai/artifact/EjLcKZ13a82uCpUaLsTpMp

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

```bash
mkdir -p ../donnees
nohup python3 collecteur.py >> ../donnees/collecteur.log 2>&1 &   # en continu
python3 referentiels.py --export                                   # copie la base dans ../donnees/referentiels.json
python3 moteur.py --direct --bdd                                   # lit le CSV du jour, rafraîchit la base chaque minute réelle
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

## Journal du moteur

Une ligne par événement (`ACQUITTEMENT`, `REFUS` avec motif, `DEPART`, `TRONCON`, `PARTIE` avec gamme, `EXPIRATION`), séparateur `;`. Les demandes hors périmètre NORTH ne sont pas journalisées. Ce journal sert de source à la table de traçabilité partagée, dont le schéma est fixé par le groupe Contrôle (question A1) : l'écriture dans cette table n'est pas encore codée.

Unités : 1 minute réelle = 1 heure projet. Les dates sont en heure réelle, les durées en heures projet.
