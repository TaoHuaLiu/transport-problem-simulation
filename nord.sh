#!/bin/sh
# Lancement de la chaîne NORTH sur la VM ENSIBS : collecteur, moteur, tableau de bord.
#
# Usage, depuis le dossier du code (nord/DEV sur la VM) :
#   sh nord.sh demarrer [options]   lance ce qui ne tourne pas déjà
#   sh nord.sh arreter [--tout]     arrête le moteur et le tableau de bord (--tout : aussi le collecteur)
#   sh nord.sh redemarrer [options] arrête puis relance moteur et tableau de bord (le collecteur continue)
#   sh nord.sh etat                 processus, dernières lignes des journaux, adresse du tableau de bord
#   sh nord.sh surveiller [options] relance toutes les 30 s ce qui s'est arrêté (Ctrl+C pour quitter)
#   sh nord.sh journaux             suit les fichiers de log (Ctrl+C pour quitter)
#
# Options de demarrer / redemarrer / surveiller (mémorisées dans ../donnees/run/options :
# sans option, une commande reprend celles du dernier lancement) :
#   --sans-bdd        moteur sans la base (lit ../donnees/referentiels.json) ; hors VLAN ou base en panne
#   --seuil X         seuil de départ θ du moteur (part du plancher, 0-1)
#   --marge W         marge W avant le départ au plus tard (heures projet)
#   --confidentiel    le tableau de bord affiche les valeurs de l'Annexe 2 : à ne pas montrer au Contrôle
#   --port N          port du tableau de bord (défaut 10000)
#   --local           tableau de bord sur 127.0.0.1 seulement (défaut : 0.0.0.0, visible du VLAN 40)
#
# Le collecteur doit tourner sans interruption (l'ERP ne renvoie pas les messages
# perdus) : « arreter » et « redemarrer » le laissent tourner. Relancer le moteur
# ouvre un nouveau journal ../donnees/moteur/evenements_*.csv ; le tableau de bord
# lit toujours le plus récent.
# Fichiers : PID dans ../donnees/run/, logs dans ../donnees/logs/.

set -u

DEV=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$DEV/../donnees"
DONNEES=$(cd "$DEV/../donnees" && pwd)
RUN="$DONNEES/run"
LOGS="$DONNEES/logs"
SERVICES="collecteur moteur tableau"

# ---------------------------------------------------------------- Options
SANS_BDD=0
CONFIDENTIEL=0
PORT=10000
HOTE=0.0.0.0
SEUIL=""
MARGE=""
TOUT=0

lire_options() {
    # Sans option : celles du dernier lancement (etat, redemarrer, surveiller restent cohérents)
    if [ $# -eq 0 ] && [ -f "$RUN/options" ]; then
        . "$RUN/options"
        return 0
    fi
    while [ $# -gt 0 ]; do
        case "$1" in
            --sans-bdd) SANS_BDD=1 ;;
            --confidentiel) CONFIDENTIEL=1 ;;
            --local) HOTE=127.0.0.1 ;;
            --tout) TOUT=1 ;;
            --port) PORT=$2; shift ;;
            --seuil) SEUIL=$2; shift ;;
            --marge) MARGE=$2; shift ;;
            *) echo "Option inconnue : $1 (sh nord.sh aide)" >&2; exit 2 ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------- Processus
# PID d'un service s'il tourne, rien sinon
pid_de() {
    fichier="$RUN/$1.pid"
    [ -f "$fichier" ] || return 1
    pid=$(cat "$fichier")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
        return 0
    fi
    rm -f "$fichier"
    return 1
}

# Commande Python de chaque service (-u : logs écrits au fil de l'eau)
commande_de() {
    case "$1" in
        collecteur)
            echo "python3 -u collecteur.py" ;;
        moteur)
            cmd="python3 -u moteur.py --direct"
            [ "$SANS_BDD" -eq 1 ] || cmd="$cmd --bdd"
            [ -z "$SEUIL" ] || cmd="$cmd --seuil $SEUIL"
            [ -z "$MARGE" ] || cmd="$cmd --marge $MARGE"
            echo "$cmd" ;;
        tableau)
            cmd="python3 -u tableau_bord.py --hote $HOTE --port $PORT"
            [ "$CONFIDENTIEL" -eq 0 ] || cmd="$cmd --confidentiel"
            echo "$cmd" ;;
    esac
}

lancer() {
    service=$1
    if pid=$(pid_de "$service"); then
        echo "  = $service tourne déjà (PID $pid)"
        return 0
    fi
    cmd=$(commande_de "$service")
    {
        echo
        echo "=== $(date '+%Y-%m-%d %H:%M:%S') lancement : $cmd"
    } >> "$LOGS/$service.log"
    # nohup : le processus survit à la fermeture du terminal (ou de la session SSH)
    # Le « & » ne porte que sur nohup : $! est alors le PID de Python lui-même
    # (avec « cd && nohup … & », ce serait celui d'un sous-shell intermédiaire)
    (cd "$DEV" || exit 1
     nohup $cmd >> "$LOGS/$service.log" 2>&1 &
     echo $! > "$RUN/$service.pid")
    sleep 2
    if pid=$(pid_de "$service"); then
        echo "  + $service lancé (PID $pid) : $cmd"
    else
        echo "  ! $service s'est arrêté au démarrage ; fin de $LOGS/$service.log :" >&2
        tail -n 15 "$LOGS/$service.log" | sed 's/^/      /' >&2
        return 1
    fi
}

arreter_service() {
    service=$1
    if ! pid=$(pid_de "$service"); then
        echo "  = $service n'était pas lancé"
        return 0
    fi
    # SIGTERM (un processus lancé en arrière-plan par sh ignore SIGINT), puis SIGKILL après 10 s.
    # Les enfants aussi : sur macOS, python3 se relance dans un processus fils.
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    attente=0
    while kill -0 "$pid" 2>/dev/null && [ $attente -lt 10 ]; do
        sleep 1
        attente=$((attente + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
        pkill -KILL -P "$pid" 2>/dev/null
        kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$RUN/$service.pid"
    echo "  - $service arrêté (PID $pid)"
}

# ---------------------------------------------------------------- Vérifications
verifier() {
    command -v python3 >/dev/null 2>&1 || { echo "python3 introuvable" >&2; exit 1; }
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
        echo "Python 3.10 ou plus récent requis ($(python3 --version 2>&1))" >&2; exit 1; }
    if [ "$SANS_BDD" -eq 0 ] && ! python3 -c 'import psycopg2' 2>/dev/null; then
        echo "psycopg2 absent : relancer avec --sans-bdd (référentiel ../donnees/referentiels.json)" >&2
        exit 1
    fi
    [ -d /mnt/tp4010/DuCapteurJusquauCloud/FichesCamion ] || \
        echo "  ! fiches camion de la VM introuvables : le moteur prendra ../../6 - Data/camions/ s'il existe"
    if [ "$SANS_BDD" -eq 1 ] && [ ! -f "$DONNEES/referentiels.json" ]; then
        echo "  ! ../donnees/referentiels.json absent : le moteur utilisera un référentiel d'exemple"
    fi
    [ -f "$DONNEES/annexe2_confidentiel.json" ] || \
        echo "  ! ../donnees/annexe2_confidentiel.json absent : coûts fictifs et seuil θ par défaut"
}

adresse_tableau() {
    if [ "$HOTE" = "127.0.0.1" ]; then
        echo "http://localhost:$PORT/"
        return
    fi
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo "http://${ip:-IP_DE_LA_VM}:$PORT/   (et http://localhost:$PORT/ sur la VM)"
}

# ---------------------------------------------------------------- Commandes
memoriser_options() {
    cat > "$RUN/options" <<FIN
SANS_BDD=$SANS_BDD
CONFIDENTIEL=$CONFIDENTIEL
PORT=$PORT
HOTE=$HOTE
SEUIL=$SEUIL
MARGE=$MARGE
FIN
}

demarrer() {
    verifier
    memoriser_options
    echo "== Démarrage de la chaîne NORTH ($(date '+%H:%M:%S'))"
    lancer collecteur || exit 1
    if [ "$SANS_BDD" -eq 0 ]; then
        # Copie de secours de la base, pour pouvoir repartir avec --sans-bdd si la base tombe
        if (cd "$DEV" && python3 referentiels.py --export >> "$LOGS/referentiels.log" 2>&1); then
            echo "  + référentiel copié dans ../donnees/referentiels.json"
        else
            echo "  ! copie du référentiel en échec (voir $LOGS/referentiels.log)"
        fi
    fi
    lancer moteur || exit 1
    lancer tableau || exit 1
    echo "== Tableau de bord : $(adresse_tableau)"
    [ "$CONFIDENTIEL" -eq 0 ] || echo "   (données confidentielles affichées : ne pas montrer au Contrôle)"
}

arreter() {
    echo "== Arrêt ($(date '+%H:%M:%S'))"
    arreter_service tableau
    arreter_service moteur
    if [ "$TOUT" -eq 1 ]; then
        arreter_service collecteur
    elif pid=$(pid_de collecteur); then
        echo "  = collecteur laissé en marche (PID $pid) ; --tout pour l'arrêter aussi"
    fi
}

etat() {
    echo "== État de la chaîne NORTH ($(date '+%Y-%m-%d %H:%M:%S'))"
    for service in $SERVICES; do
        if pid=$(pid_de "$service"); then
            duree=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
            printf "  %-10s en marche  PID %-7s depuis %s\n" "$service" "$pid" "$duree"
        else
            printf "  %-10s ARRÊTÉ\n" "$service"
        fi
        if [ -f "$LOGS/$service.log" ]; then
            tail -n 2 "$LOGS/$service.log" | sed 's/^/      | /'
        fi
    done
    jour=$(date +%Y-%m-%d)
    csv="$DONNEES/demandes/demandes_$jour.csv"
    if [ -f "$csv" ]; then
        echo "  demandes du jour : $(($(wc -l < "$csv") - 1)) dans $csv"
    else
        echo "  demandes du jour : aucun fichier $csv"
    fi
    journal=$(ls -t "$DONNEES"/moteur/evenements_*.csv 2>/dev/null | head -n 1)
    [ -z "$journal" ] || echo "  journal du moteur : $(($(wc -l < "$journal") - 1)) événements dans $journal"
    pid_de tableau >/dev/null && echo "  tableau de bord : $(adresse_tableau)"
}

surveiller() {
    demarrer
    echo "== Surveillance : relance toutes les 30 s ce qui s'est arrêté (Ctrl+C pour quitter, les services continuent)"
    while true; do
        sleep 30
        for service in $SERVICES; do
            if ! pid_de "$service" >/dev/null; then
                echo "[$(date '+%H:%M:%S')] $service arrêté : relance"
                lancer "$service"
            fi
        done
    done
}

mkdir -p "$RUN" "$LOGS"
action=${1:-aide}
[ $# -gt 0 ] && shift
if [ "$action" = "arreter" ]; then
    for arg in "$@"; do [ "$arg" = "--tout" ] && TOUT=1; done
    set --
fi
lire_options "$@"
case "$action" in
    demarrer) demarrer ;;
    arreter) arreter ;;
    redemarrer) arreter; demarrer ;;
    etat) etat ;;
    surveiller) surveiller ;;
    journaux) tail -n 5 -F "$LOGS"/collecteur.log "$LOGS"/moteur.log "$LOGS"/tableau.log ;;
    *) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
