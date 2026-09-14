#!/bin/bash
# Lance le dashboard web de controle de la flotte ZED.
# Usage :  bash start_dashboard.sh        (Ctrl-C pour arreter)
cd "$(dirname "$0")"
# Libere le port si une instance precedente tourne encore (relance propre).
fuser -k 8080/tcp 2>/dev/null && sleep 1
echo "=================================================="
echo " ZED Fleet Control — dashboard"
echo " Ouvre dans ton navigateur :  http://localhost:8080"
echo " (Ctrl-C ici pour arreter le serveur)"
echo "=================================================="
exec python3 webgui.py --config config.json --port 8080 --data data
