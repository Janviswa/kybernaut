#!/usr/bin/env bash
# ==============================================================
# KYBERNAUT | AGENTIC AIOPS
# Full-screen two-column infrastructure dashboard
# In-place value updates every 10 seconds; no screen blink.
# ==============================================================

REFRESH=10
STORAGE_ROOT="/srv/kybernaut-data"
BACKUP_DIR="$STORAGE_ROOT/Kybernaut/backups"
BACKUP_FILE="$BACKUP_DIR/kybernaut-backup.zip"
CACHE_DIR="$STORAGE_ROOT/Kybernaut/cache"
APP_CACHE="$CACHE_DIR/application"
DEV_CACHE="$CACHE_DIR/development"

RESET=$(tput sgr0 2>/dev/null)
BOLD=$(tput bold 2>/dev/null)
CYAN=$(tput setaf 6 2>/dev/null)
GREEN=$(tput setaf 2 2>/dev/null)
RED=$(tput setaf 1 2>/dev/null)
YELLOW=$(tput setaf 3 2>/dev/null)
DIM=$(tput dim 2>/dev/null)
WHITE=$(tput setaf 7 2>/dev/null)

WIDTH=$(tput cols 2>/dev/null || echo 120)
HEIGHT=$(tput lines 2>/dev/null || echo 42)
(( WIDTH < 100 )) && WIDTH=100
(( WIDTH > 200 )) && WIDTH=200
(( HEIGHT < 38 )) && HEIGHT=38

LEFT_WIDTH=$(( (WIDTH - 3) / 2 ))
RIGHT_START=$(( LEFT_WIDTH + 4 ))
RIGHT_WIDTH=$(( WIDTH - RIGHT_START - 1 ))
LEFT_VALUE_WIDTH=$(( LEFT_WIDTH - 23 ))
RIGHT_VALUE_WIDTH=$(( RIGHT_WIDTH - 22 ))
BORDER=$(printf '%*s' "$WIDTH" '' | tr ' ' '=')
RULE=$(printf '%*s' "$WIDTH" '' | tr ' ' '-')

move_to() { printf '\033[%d;%dH' "$1" "$2"; }

# Print one complete row as two independent label/value pairs.
# Keeping each value inside its own column prevents overwriting labels.
row_pair() {
    local row="$1" ll="$2" lv="$3" rl="$4" rv="$5"
    local left_text right_text

    printf -v left_text '%-17.17s : %-*.*s' \
        "$ll" "$LEFT_VALUE_WIDTH" "$LEFT_VALUE_WIDTH" "$lv"
    printf -v right_text '%-19.19s : %-*.*s' \
        "$rl" "$RIGHT_VALUE_WIDTH" "$RIGHT_VALUE_WIDTH" "$rv"

    move_to "$row" 2
    printf '%s' "$left_text"
    move_to "$row" "$RIGHT_START"
    printf '%s' "$right_text"
}

section() {
    local row="$1" title="$2"
    move_to "$row" 2
    printf '%b%s%b' "$CYAN$BOLD" "$title" "$RESET"
    move_to "$((row+1))" 1
    printf '%s' "$RULE"
}

center() {
    local row="$1" text="$2" color="${3:-$WHITE}"
    local pad=$(( (WIDTH - ${#text}) / 2 + 1 ))
    (( pad < 1 )) && pad=1
    move_to "$row" "$pad"
    printf '%b%s%b' "$color" "$text" "$RESET"
}

health() {
    if curl -fsS --max-time 2 "$1" >/dev/null 2>&1; then
        printf 'UP'
    else
        printf 'DOWN'
    fi
}

layout() {
    # Base dashboard uses 42 rows. Extra rows are distributed between
    # sections so the content fills the available height more evenly.
    local extra=$((HEIGHT - 42))
    (( extra < 0 )) && extra=0
    local each=$((extra / 4)) rem=$((extra % 4))
    local g1=$each g2=$each g3=$each g4=$each
    (( rem > 0 )) && ((g1++))
    (( rem > 1 )) && ((g2++))
    (( rem > 2 )) && ((g3++))

    SERVER_SEC=6
    RESOURCE_SEC=$((16 + g1))
    INFRA_SEC=$((22 + g1 + g2))
    STORAGE_SEC=$((31 + g1 + g2 + g3))
    FOOTER_TOP=$((HEIGHT - 3))
}

draw_frame() {
    layout
    tput civis 2>/dev/null
    printf '\033[2J\033[H'

    move_to 1 1; printf '%b%s%b' "$CYAN" "$BORDER" "$RESET"
    center 2 "KYBERNAUT | AGENTIC AIOPS" "$CYAN$BOLD"
    center 3 "SERVER INFRASTRUCTURE & RELIABILITY MONITOR" "$DIM"
    center 4 "HOST: $(hostname)  |  LIVE VALUES  |  REFRESH: ${REFRESH}s" "$WHITE"
    move_to 5 1; printf '%b%s%b' "$CYAN" "$BORDER" "$RESET"

    section "$SERVER_SEC" "SERVER OVERVIEW"
    section "$RESOURCE_SEC" "SYSTEM RESOURCE MONITORING"
    section "$INFRA_SEC" "INFRASTRUCTURE & SERVICES"
    section "$STORAGE_SEC" "STORAGE, BACKUPS & CACHE"

    move_to "$FOOTER_TOP" 1; printf '%b%s%b' "$CYAN" "$BORDER" "$RESET"
    center "$((FOOTER_TOP+1))" "KYBERNAUT SERVER MONITORING ACTIVE" "$GREEN$BOLD"
    center "$((FOOTER_TOP+2))" "VALUES UPDATE EVERY ${REFRESH} SECONDS  |  CTRL+C TO EXIT" "$DIM"
    move_to "$((FOOTER_TOP+3))" 1; printf '%b%s%b' "$CYAN" "$BORDER" "$RESET"
}

update_values() {
    local host now uptime boot os kernel arch timezone ip cpu_model cores load
    host=$(hostname)
    now=$(date '+%d %b %Y | %H:%M:%S')
    uptime=$(uptime -p | sed 's/^up //')
    boot=$(uptime -s)
    os=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}")
    kernel=$(uname -r)
    arch=$(uname -m)
    timezone=$(timedatectl show -p Timezone --value 2>/dev/null)
    [[ -z "$timezone" ]] && timezone=$(date +%Z)
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    [[ -z "$ip" ]] && ip="Unavailable"
    cpu_model=$(awk -F: '/model name/ {gsub(/^[ \t]+/,"",$2); print $2; exit}' /proc/cpuinfo)
    [[ -z "$cpu_model" ]] && cpu_model=$(uname -m)
    cores=$(nproc 2>/dev/null || echo N/A)
    load=$(awk '{print $1 " / " $2 " / " $3}' /proc/loadavg)
    processes=$(ps -e --no-headers 2>/dev/null | wc -l)
    users=$(who 2>/dev/null | awk '{print $1}' | sort -u | paste -sd ',' -)
    [[ -z "$users" ]] && users="None"

    ram=$(free -h | awk '/Mem:/ {print $3 " / " $2}')
    ram_pct=$(free | awk '/Mem:/ {if($2>0) printf "%.1f%%",($3/$2)*100}')
    swap=$(free -h | awk '/Swap:/ {print $3 " / " $2}')
    disk=$(df -h / | awk 'NR==2 {print $3 " / " $2}')
    disk_pct=$(df -h / | awk 'NR==2 {print $5}')

    running=$(docker ps -q 2>/dev/null | wc -l)
    total=$(docker ps -aq 2>/dev/null | wc -l)
    stopped=$((total-running))
    docker_version=$(docker --version 2>/dev/null | sed 's/Docker version //')
    [[ -z "$docker_version" ]] && docker_version="Unavailable"

    kube=$(kubectl get pods -n kybernaut-lab --no-headers 2>/dev/null |
        awk '{total++; split($2,r,"/"); if($3=="Running" && r[1]==r[2]) ready++}
             END {printf "%d/%d ready",ready,total}')
    [[ -z "$kube" ]] && kube="Unavailable"
    nodes=$(kubectl get nodes --no-headers 2>/dev/null | wc -l)

    api=$(health http://localhost:8000/health)
    order=$(health http://localhost:8001/health)
    payment=$(health http://localhost:8002/health)
    user=$(health http://localhost:8003/health)
    inventory=$(health http://localhost:8004/health)
    prom=$(health http://localhost:9090/-/healthy)
    grafana=$(health http://localhost:3000/api/health)

    if mountpoint -q "$STORAGE_ROOT"; then
        hdd_status="MOUNTED"
        hdd_used=$(df -h "$STORAGE_ROOT" | awk 'NR==2 {print $3 " used / " $2 " total"}')
        hdd_free=$(df -h "$STORAGE_ROOT" | awk 'NR==2 {print $4}')
    else
        hdd_status="OFFLINE"; hdd_used="Not mounted"; hdd_free="N/A"
    fi

    backup_dir="MISSING"; backup_size="N/A"
    if mountpoint -q "$STORAGE_ROOT" && [[ -d "$BACKUP_DIR" ]]; then backup_dir="AVAILABLE"; fi
    if mountpoint -q "$STORAGE_ROOT" && [[ -f "$BACKUP_FILE" ]]; then
        backup_size=$(du -h "$BACKUP_FILE" 2>/dev/null | awk '{print $1}')
    fi

    app_size=$(du -sh "$APP_CACHE" 2>/dev/null | awk '{print $1}')
    dev_size=$(du -sh "$DEV_CACHE" 2>/dev/null | awk '{print $1}')
    cache_size=$(du -sh "$CACHE_DIR" 2>/dev/null | awk '{print $1}')
    [[ -z "$app_size" ]] && app_size="N/A"
    [[ -z "$dev_size" ]] && dev_size="N/A"
    [[ -z "$cache_size" ]] && cache_size="N/A"

    runbook="MISSING"; hit_age="N/A"
    if [[ -f "$APP_CACHE/runbook-index.json" ]]; then
        runbook="READY"
        if [[ -f "$APP_CACHE/runbook-index.last-hit" ]]; then
            now_epoch=$(date +%s)
            last_hit=$(stat -c %Y "$APP_CACHE/runbook-index.last-hit" 2>/dev/null || echo 0)
            age=$((now_epoch-last_hit))
            hit_age="${age}s ago"
            (( age >= 0 && age <= 300 )) && runbook="ACTIVE"
        fi
    fi

    # Update the header clock in place.
    move_to 4 1
    printf '%-*.*s' "$WIDTH" "$WIDTH" "HOST: $host  |  $now"

    # Server overview: every label and its value share one fixed row.
    r=$((SERVER_SEC+2))
    row_pair "$r" "HOST" "$host" "OPERATING SYSTEM" "$os"; ((r++))
    row_pair "$r" "IP ADDRESS" "$ip" "KERNEL" "$kernel"; ((r++))
    row_pair "$r" "CPU MODEL" "$cpu_model" "TIMEZONE" "$timezone"; ((r++))
    row_pair "$r" "CPU CORES" "$cores" "BOOT TIME" "$boot"; ((r++))
    row_pair "$r" "UPTIME" "$uptime" "CPU LOAD" "$load"; ((r++))
    row_pair "$r" "ARCHITECTURE" "$arch" "PROCESSES" "$processes"; ((r++))
    row_pair "$r" "LOGIN USERS" "$users" "HOST STATUS" "ONLINE"

    # System resources
    r=$((RESOURCE_SEC+2))
    row_pair "$r" "RAM USED" "$ram" "RAM UTILIZATION" "$ram_pct"; ((r++))
    row_pair "$r" "SWAP USED" "$swap" "ROOT DISK" "$disk"; ((r++))
    row_pair "$r" "DISK USAGE" "$disk_pct" "DISK CAPACITY" "$disk"

    # Infrastructure: the four microservices are consecutive, top to bottom.
    r=$((INFRA_SEC+2))
    row_pair "$r" "DOCKER RUNNING" "$running" "MAIN API" "$api"; ((r++))
    row_pair "$r" "DOCKER STOPPED" "$stopped" "ORDER SERVICE" "$order"; ((r++))
    row_pair "$r" "DOCKER VERSION" "$docker_version" "PAYMENT SERVICE" "$payment"; ((r++))
    row_pair "$r" "KUBERNETES PODS" "$kube" "USER SERVICE" "$user"; ((r++))
    row_pair "$r" "KUBERNETES NODES" "$nodes" "INVENTORY SERVICE" "$inventory"; ((r++))
    row_pair "$r" "PROMETHEUS" "$prom" "GRAFANA" "$grafana"

    # Storage, backups and cache
    r=$((STORAGE_SEC+2))
    row_pair "$r" "HDD STATUS" "$hdd_status" "BACKUP DIRECTORY" "$backup_dir"; ((r++))
    row_pair "$r" "HDD USED" "$hdd_used" "BACKUP ARCHIVE" "$backup_size"; ((r++))
    row_pair "$r" "HDD FREE" "$hdd_free" "TOTAL CACHE" "$cache_size"; ((r++))
    row_pair "$r" "APPLICATION CACHE" "$app_size" "DEVELOPMENT CACHE" "$dev_size"; ((r++))
    row_pair "$r" "RUNBOOK STATUS" "$runbook" "LAST RUNBOOK HIT" "$hit_age"

    move_to "$HEIGHT" 1
}

draw_frame
while true; do
    update_values
    sleep "$REFRESH"
done
