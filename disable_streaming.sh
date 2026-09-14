#!/bin/bash
# disable_streaming.sh — stoppe + desactive l'auto-restart des conteneurs Docker
# (ancien streaming ZED "sender1") qui monopolisent la camera sur les images clonees.
# Reversible : 'docker start <c>' + 'docker update --restart=always <c>' pour revenir.
# A lancer depuis le PC :  bash disable_streaming.sh
# Demande le mot de passe sudo (123456789) une fois par unite.
for pair in "20 zed" "21 zed" "22 zed" "23 zed" "25 zed"; do
    ip="192.168.0.${pair%% *}"
    user="${pair##* }"
    echo "==================== ${user}@${ip} ===================="
    ssh -t "${user}@${ip}" "sudo bash -c '
        echo \"--- conteneurs actifs ---\"; docker ps --format \"{{.Names}} ({{.Image}}) {{.Status}}\"
        for c in \$(docker ps -q); do
            docker update --restart=no \$c >/dev/null && docker stop \$c >/dev/null && echo \"stoppe: \$c\"
        done
        echo \"--- apres ---\"; docker ps --format \"{{.Names}}\" | grep . || echo \"(plus aucun conteneur actif -> camera libre)\"
    '"
    echo
done
echo "Termine sur les 5. Je re-teste les open ensuite."
