#!/bin/bash
# set_perf_fleet.sh — applique un mode nvpmodel a toute la flotte + jetson_clocks.
#   bash set_perf_fleet.sh [MODE] [CONFIG]
#   MODE : 2 = 15W 6-core (defaut, stable) | 8 = 20W 6-core
#   ex:  bash set_perf_fleet.sh 2        # revenir en 15W
#        bash set_perf_fleet.sh 8        # passer en 20W
# Demande le mot de passe sudo de CHAQUE Jetson (123456789) — ssh -t interactif.
# nvpmodel persiste au reboot ; jetson_clocks non (a relancer apres reboot).
# NB: .12 (zed12) ne tient PAS le 20W (brownout) -> la laisser en 15W (mode 2).
set -u
MODE="${1:-2}"
CONFIG="${2:-config.json}"

mapfile -t HOST_LINES < <(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
du = cfg.get('default_ssh_user','zed')
for h in cfg['hosts']:
    print(h['ip'], h.get('user', du))
")

for entry in "${HOST_LINES[@]}"; do
    read -r ip user <<< "$entry"
    echo "==================== ${user}@${ip} ===================="
    ssh -t "${user}@${ip}" "sudo bash -c 'nvpmodel -m ${MODE}; jetson_clocks; echo -n \"mode courant: \"; cat /var/lib/nvpmodel/status'"
    echo
done
printf "Termine. Verifie que chaque ligne affiche pmode:%04d.\n" "$MODE"
