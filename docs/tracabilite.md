# Matrice de traçabilité — Logistique NORTH

Couche 4 du standard de documentation (`standard-documentation.md`) : pour chaque exigence, la règle, la fonction qui la code, le test qui la vérifie, la preuve consultable et l'état. État du code de `1 - DEV/` au 20/11/2026. Les règles sont définies dans `processus.md` §2.5.

Toute nouvelle règle ou fonction met ce fichier à jour dans le même changement.

*Confidentialité : ce document est lisible par le groupe Contrôle. Aucun chiffre de l'Annexe 2 n'y figure.*

Tests : `python3 -m unittest discover -s tests` depuis `1 - DEV/` (110 tests) ; « doctest » : `python3 -m doctest collecteur.py referentiels.py`. « indirect » : la fonction est exercée par les tests d'une autre règle, sans test qui lui soit propre.

## Exigence → règle → fonction → test → preuve

| Exigence | Source | Règle | Fonction | Test | Preuve consultable | État |
|---|---|---|---|---|---|---|
| Aucune demande perdue à la réception | Sujet p. 12 | R-COL-01 | `collecteur.ecrire_brut()`, `decoder_message()`, `traiter_datagramme()` | doctest (`decouper_spec`, `convertir_h_dep`) ; `test_robustesse.TestCollecteur.test_csv_du_jour_abime_brut_conserve` | Journal brut `demandes_AAAA-MM-JJ.jsonl` ; contrôle « absentes du collecteur » du tableau de bord | Codé, testé |
| Le collecteur tourne sans interruption | Sujet p. 12 | R-ROB-04 | `collecteur.collecter()`, `traiter_datagramme()` | `test_robustesse.TestCollecteur.test_boucle_survit_aux_erreurs` | `donnees/logs/collecteur.log` (erreurs signalées) | Codé, testé |
| Le moteur ne s'arrête pas sur une erreur | Exploitation | R-ROB-01 | `moteur.decider_demande()`, `decider_tournee()`, `lire_suite()`, boucle de `en_direct()` ; `robustesse.signaler()` | `test_robustesse.TestDemandeEnErreur` (6 tests), `TestLectureCSV` (3 tests) | Motif REF-DONNEES au journal ; `donnees/logs/moteur.log` (incidents comptés à l'arrêt) | Codé, testé |
| Base indisponible sans arrêt du moteur | Exploitation | R-ROB-02 | `moteur.rafraichir_referentiel()`, `charger_bdd_ou_copie()` ; `referentiels.BDD` (délais) ; `archivage.synchroniser()` | `test_robustesse.TestBaseIndisponible` (4 tests) ; `test_archivage` (reprise sur erreur) | `donnees/logs/moteur.log` (« base injoignable ») | Codé, testé |
| Arrêt propre sur SIGTERM | Exploitation | R-ROB-03 | `robustesse.arreter_sur_sigterm()` (moteur, collecteur, tableau de bord) | `test_robustesse.TestArretPropre` | Fin de `moteur.log` : résumé et « Archivage : n ligne(s) écrite(s) » | Codé, testé |
| L'audit tourne toujours sur la base (production) | Audit de 2 h (sujet) | R-ROB-05 | `moteur.preparer()`, `verifier_conditions_audit()`, option `--audit` ; `nord.sh audit` (`lire_options_audit`, `verifier_audit`) | `test_mode_audit` (8 tests) | Première ligne de `moteur.log` : « MODE AUDIT (production) » ; `sh nord.sh etat` : « mode : AUDIT » | Codé, testé |
| Tables de traçabilité du Contrôle remplies pour nos commandes | Sujet (seul canal entre groupes) ; spécification du Contrôle | R-CTL-01, D-JUSTIF | `tracabilite_controle.ligne_commande()`, `ligne_trajet()`, `code_justif()`, `synchroniser()`, `reprendre()` ; `moteur.py --controle` (imposé par `--audit`) ; `decision.traiter_demande()` (`capacite_seule`) | `test_tracabilite_controle` (23 tests) ; `test_robustesse.TestBaseIndisponible.test_tables_du_controle_base_injoignable` ; rejeu du 2026-10-19 sur une base PostgreSQL locale | `TRACABILITE_COMMANDE` (`ID_ENTRE` = notre code) et `TRACABILITE_TRAJET` ; `python3 tracabilite_controle.py --etat` ; `--verifier` pour le schéma | Codé, testé ; pas encore lancé sur la VM ; valeurs de A1 à confirmer avec le Contrôle |
| Périmètre NORTH (base du TA) | Sujet p. 20 ; contrat (TA) | R-PER-01, R-PER-02 | `regles.est_demande_north()` ; recalcul indépendant `audit.usine_north()` | `TestDACC.test_ligne_1_hors_perimetre` ; `test_audit.TestPerimetre` | Journal : demandes acquittées ou refusées ; `/api/statuts.csv` | Codé, testé |
| Demandes exploitables | Sujet p. 11 | R-VAL-01 | `regles.valider_demande()`, `lire_h_dep()`, `referentiels.pk_lieu()` | `TestDACC.test_ligne_2_donnees_invalides` | Motif REF-DONNEES au journal | Codé, testé |
| Camion exploitable | Sujet p. 13–15 | R-FLO-01 | `flotte.charger_flotte()`, `referentiels.usine_attache()` | indirect | `CAMION_NORTH` ; IMMAT de la tournée ; motif REF-CAMION | Codé, test à écrire |
| Frais en frigo, sans mélange ; pas de sec en frigo | Sujet p. 9 ; décision d'équipe | R-FRA-01, R-FRA-02 | `regles.verifier_frigo()`, `decision.compatible()` | `TestDACC.test_ligne_4_pas_de_frigo`, `test_ligne_4_sec_sans_camion_sec` | `CAMION_NORTH.REMORQUE_REFRIGEREE` du camion de la tournée ; motifs REF-FRIGO, REF-CAMION | Codé, testé |
| SPEC tenues si possible, sinon gamme abaissée | Sujet p. 11 et 21 ; précision du 21/10/2026 | R-SPEC-01, R-SPEC-02 | `regles.verifier_spec()`, `planification.declasser_gamme()` | `TestDACC.test_ligne_3_spec_impossible`, `test_spec_non_tenue_acceptee`, `test_spec_tenue_preferee` ; `TestDeclassementSpec` ; `TestLignes.test_spec` | GAMME de la ligne PARTIE ; colonnes `SPEC_*` d'`ARCHIVAGE_COMMANDE_NORTH` | Codé, testé |
| Capacité respectée | `TRUCKS_CAPACITY` ; sujet p. 15 | R-CAP-01 | `regles.equivalent_plancher()`, `referentiels.capacite()`, `planification.evaluer_plan()` | indirect (`TestDACC.test_ligne_8_insertion`) | REMPLISSAGE et DEMANDES_A_BORD des lignes TRONCON | Codé, test propre à écrire |
| Départ avant `H_DEP` | Sujet p. 11–12 ; précision du 21/10/2026 | R-DEL-01 | `planification.evaluer_plan()`, `est_import_port()`, `faire_partir()` ; `referentiels.duree()`, `attente_port_h()` | `TestDACC.test_ligne_6_delai`, `test_import_lointain_depart_usine`, `test_import_camion_libre_trop_tard`, `test_export_controle_au_chargement` ; `test_audit.TestStatuts.test_depart_controle` | DETAIL de la ligne PARTIE et `H_DEP_R` comparés à `H_DEP` ; EXPIRATION ; contrôle « parties après H_DEP » | Codé, testé |
| Réglementation conducteur | Sujet, Annexe 1 | R-CON-01 | `planification.conduire()`, `evaluer_avec_motif()` | `TestDACC.test_ligne_7_conducteur` ; `TestConduite` | H_DEBUT / H_FIN des lignes TRONCON ; motif REF-CONDUCTEUR | Codé, testé |
| Durée de la commande estimée avant d'accepter | Note d'équipe | R-DUR-01 | `planification.evaluer_plan()` | indirect (`TestDACC` l. 6 à 9) | H_DEBUT / H_FIN des lignes TRONCON | Codé |
| Accepter dans une tournée ouverte ou une nouvelle | Sujet p. 10 (mutualiser) | D-ACC l. 8–9, R-OPT-03 | `decision.traiter_demande()`, `compatible()`, `planification.ordonner_arrets()` | `TestDACC.test_ligne_8_insertion`, `test_ligne_9_nouvelle_tournee` ; `TestRemplissage.test_tournee_deux_lieux` | ID_TOURNEE des lignes ACQUITTEMENT | Codé, testé |
| Rentabilité des départs | Sujet p. 20 ; Annexe 2, confidentielle | R-RENT-01, D-DEPART | `planification.decider_depart()`, `moteur.decider_tournee()` | `TestDDEPART` (lignes 1 à 3) | REMPLISSAGE de la ligne DEPART ; `TRAJET_NORTH.TAUX_REMPL` | Codé, testé ; sous le seuil à l'heure limite, la tournée part (`SOUS_SEUIL = "partir"`) |
| Choix du plan le moins coûteux, puis le mieux rempli | Contrat (coût, RSE) | R-OPT-01, R-OPT-02 | `decision.traiter_demande()`, `planification.cout_energie()` | indirect (`TestDACC` l. 8–9) | Coût du plan (interne, non journalisé) | Codé, test propre à écrire |
| Gamme par demande | Contrat (gammes) ; `methode-contrat.md` §3 | R-GAM | `planification.attribuer_gamme()` | indirect (`TestDACC` l. 8–9) ; `test_audit.TestCalcul.test_gammes_et_motifs` | GAMME de la ligne PARTIE | Codé, test propre à écrire |
| Prix de la prestation | Sujet p. 20, 23 ; `methode-contrat.md` §3 | R-GAM | `planification.prix_prestation()`, `archivage.ligne_commande()` | `test_archivage.TestPrix` | `ARCHIVAGE_COMMANDE_NORTH.COUT_PREST` | Codé, testé ; écrit seulement avec les taux de l'Annexe 2 |
| Prix accepté par Garden Food avant l'acquittement | Sujet p. 24 | D-PRIX | — | — | — | Non couvert (question A2) |
| Taux d'acceptation | Contrat (performance) | R-KPI-TA | `tracabilite.calculer_kpi()` ; recalcul indépendant `audit.statuts_demandes()`, `audit.calculer()` | `test_audit.TestStatuts`, `TestCalcul.test_ta`, `test_fenetre` | Tableau de bord ; `/api/rapport.json` ; `/api/statuts.csv` | Codé, testé |
| Taux de remplissage | Contrat (RSE) | R-KPI-TR | `regles.surface_camion()`, `taux_remplissage()` ; `tracabilite.calculer_kpi()` ; `audit.calculer()` | `TestRemplissage` ; `test_audit.TestCalcul.test_tr` | REMPLISSAGE de la ligne DEPART ; `TRAJET_NORTH.TAUX_REMPL` | Codé, testé |
| Écart aux cibles et note du barème | Sujet p. 21 (méthode régressive) | — | `audit.ecart_et_note()` | `test_audit.TestBareme` | Tableau de bord, bloc Indicateurs | Codé, testé |
| Traçabilité des décisions | Sujet p. 17, 22 | — | `tracabilite.noter()` ; `planification.faire_partir()` | `TestDDEPART.test_depart_ecrit_les_troncons` | Journal `evenements_*.csv` | Codé, testé |
| Trajets et commandes consultables en base | Sujet p. 19 (tables propres) | — | `archivage.synchroniser()`, `ligne_camion()`, `ligne_trajet()`, `ligne_commande()`, `statut_trajet()`, `reprendre()` | `test_archivage` (`TestLignes`, `TestSynchronisation`) | Tables `CAMION_NORTH`, `TRAJET_NORTH`, `ARCHIVAGE_COMMANDE_NORTH` (`tables-nord.sql`) ; `python3 archivage.py --etat` | Codé, testé |
| Contrôles d'audit (demandes sans décision, acquittées en retard, hors périmètre…) | Contrat (professionnalisme) | R-KPI-TA, R-PER-01/02, R-DEL-01 | `audit._controles()` | `test_audit.TestCalcul.test_controles` | Tableau de bord, bloc Contrôles | Codé, testé |
| Reprise de l'état après un arrêt du moteur | — (question E18) | — | `archivage.reprendre()` (base seulement) | `TestSynchronisation.test_reprise_apres_arret` | Commandes EXPIREE « moteur redémarré avant le départ » | Partiel : positions des camions et tournées ouvertes perdues |
| Limiter le détour | Note d'équipe | R-OPT-04 | par les seuils de gamme (`attribuer_gamme()`) | — | GAMME | Partiel |
| Retour chargé ; commande plus rentable ; cabotage | Note d'équipe ; Annexe 1 | R-OPT-05, R-ARB-01, R-CAB-01 | — | — | — | Non codées |

## Lignes des tables de décision → tests

Le standard demande au moins un test par ligne de table de décision (`tests/test_decision.py`, `tests/test_tracabilite_controle.py`).

| Table | Ligne | Test |
|---|---|---|
| D-ACC | 1 | `TestDACC.test_ligne_1_hors_perimetre` |
| D-ACC | 2 | `TestDACC.test_ligne_2_donnees_invalides` |
| D-ACC | 3 | `TestDACC.test_ligne_3_spec_impossible`, `test_spec_non_tenue_acceptee` |
| D-ACC | 4 | `TestDACC.test_ligne_4_pas_de_frigo`, `test_ligne_4_sec_sans_camion_sec` |
| D-ACC | 5 | `TestDACC.test_ligne_5_aucun_camion_libre` |
| D-ACC | 6 | `TestDACC.test_ligne_6_delai` |
| D-ACC | 7 | `TestDACC.test_ligne_7_conducteur` |
| D-ACC | 8 | `TestDACC.test_ligne_8_insertion` |
| D-ACC | 9 | `TestDACC.test_ligne_9_nouvelle_tournee` |
| D-DEPART | 1 | `TestDDEPART.test_ligne_1_seuil_atteint` |
| D-DEPART | 2 | `TestDDEPART.test_ligne_2_attendre` |
| D-DEPART | 3 | `TestDDEPART.test_ligne_3_heure_limite` |
| D-JUSTIF | 1 | `TestDJustif.test_ligne_1_donnees` |
| D-JUSTIF | 2 | `TestDJustif.test_ligne_2_specificites` |
| D-JUSTIF | 3 | `TestDJustif.test_ligne_3_capacite`, `test_trop_de_palettes_dans_le_moteur` |
| D-JUSTIF | 4 | `TestDJustif.test_ligne_4_moyen`, `test_frais_sans_frigo_dans_le_moteur` |
| D-JUSTIF | 5 | `TestDJustif.test_ligne_5_delai` |
| D-JUSTIF | 6 | `TestDJustif.test_ligne_6_remplissage` |

## Fonctions sans règle métier

Ces modules sont de l'outillage : `simulateur_erp.py` (faux ERP local), `schema_bdd.py` (lecture du schéma), `tableau_bord.py` (serveur web), `nord.sh` (lancement sur la VM), ainsi que les fonctions de lecture et d'écriture de fichiers de `referentiels.py`, `moteur.py` et `audit.py`. Les garde-fous d'exploitation (`robustesse.py`, protections de `moteur.py` et `collecteur.py`, mode audit de `nord.sh`) citent les règles R-ROB-01 à R-ROB-05 et sont dans le premier tableau. Leurs docstrings décrivent entrées, sorties et unités sans citer de règle.
