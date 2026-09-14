#!/bin/bash
# setup_nopasswd.sh — accorde le sudo SANS mot de passe a l'user de chaque Jetson
# (reseau labo isole). Une fois fait, la gestion de flotte est autonome (docker,
# nvpmodel, systemctl... sans retaper le mot de passe).
#
# A lancer UNE fois depuis le PC :   bash setup_nopasswd.sh
# Demande le mot de passe sudo (123456789) une fois par unite.
#
# Sur : ecrit un fichier temporaire, le VALIDE avec 'visudo -cf' AVANT de le
# mettre en place -> impossible de casser sudo meme en cas de souci.
CONFIG="${1:-config.json}"

mapfile -t H < <(python3 -c "
import json
c = json.load(open('$CONFIG')); du = c.get('default_ssh_user','zed')
for h in c['hosts']:
    print(h['ip'], h.get('user', du))
")

for entry in "${H[@]}"; do
    read -r ip user <<< "$entry"
    echo "==================== ${user}@${ip} ===================="
    if ! ping -c1 -W1 "$ip" >/dev/null 2>&1; then
        echo "  INJOIGNABLE -> skip"; echo; continue
    fi
    ssh -t -o ConnectTimeout=8 "${user}@${ip}" \
        "echo '${user} ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/.nopasswd-tmp >/dev/null \
         && sudo visudo -cf /etc/sudoers.d/.nopasswd-tmp >/dev/null 2>&1 \
         && sudo chmod 440 /etc/sudoers.d/.nopasswd-tmp \
         && sudo mv /etc/sudoers.d/.nopasswd-tmp /etc/sudoers.d/90-nopasswd-zed \
         && echo '  -> NOPASSWD sudo OK' \
         || { sudo rm -f /etc/sudoers.d/.nopasswd-tmp; echo '  -> ECHEC (sudoers inchange)'; }"
    echo
done
echo "Termine. (le fichier temporaire contient un point -> ignore par sudo, donc sans risque)"
