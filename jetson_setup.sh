# ============================================================
#  jetson_setup.sh  --  config reseau + SSH pour un Jetson neuf
#  (marche sur Nano J10 ET Xavier NX J20 — c'est juste du reseau)
#
#  A LANCER SUR LE JETSON, dans un terminal :
#      sudo bash jetson_setup.sh 192.168.0.8
#  ou sans argument (il demandera l'IP) :
#      sudo bash jetson_setup.sh
#
#  IPs deja prises (ne PAS reutiliser) :
#      .50 = PC      .2/.3/.4/.6 = flotte phase 1      .7 = zed5
#  Prochaines libres : .8, .9, .10, ...
# ============================================================

IFACE="eth0"

# --- IP en argument, sinon on la demande ---
IP="$1"
if [ -z "$IP" ]; then
  echo -n "IP statique a donner a ce Jetson (ex: 192.168.0.8) : "
  read IP
fi
# format minimal 192.168.0.X
case "$IP" in
  192.168.0.*) ;;
  *) echo "ERREUR: '$IP' n'est pas une IP 192.168.0.x — abandon."; exit 1 ;;
esac
STATIC_IP="$IP/24"

echo "=================================================="
echo " 1. Detection de la connexion filaire sur $IFACE"
echo "=================================================="
CON="$(nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$IFACE" '$2==d{print $1; exit}')"
if [ -z "$CON" ]; then
  CON="$(nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
fi
if [ -z "$CON" ]; then
  echo "Aucun profil ethernet trouve -> creation de 'w0'"
  nmcli con add type ethernet ifname "$IFACE" con-name w0
  CON="w0"
fi
echo "Connexion ciblee : '$CON'"

echo "=================================================="
echo " 2. IP statique $STATIC_IP (sans gateway ni DNS)"
echo "=================================================="
nmcli con mod "$CON" ipv4.method manual ipv4.addresses "$STATIC_IP" ipv4.gateway "" ipv4.dns ""
nmcli con down "$CON" 2>/dev/null
nmcli con up "$CON"

echo "=================================================="
echo " 3. Activation du serveur SSH"
echo "=================================================="
systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null \
  || echo "ATTENTION: openssh-server semble absent (pas d'internet pour l'installer)"

echo "=================================================="
echo " 4. Perf : verrouillage des frequences (jetson_clocks)"
echo "=================================================="
nvpmodel -q 2>/dev/null | head -4
jetson_clocks 2>/dev/null && echo "jetson_clocks OK (frequences au max)" \
  || echo "jetson_clocks non applique (non bloquant)"

echo "=================================================="
echo " 5. VERIFICATION"
echo "=================================================="
echo "--- IP sur $IFACE (doit montrer $IP) ---"
ip -4 addr show "$IFACE" | grep inet || echo "PAS D'IPv4 !"
echo "--- Etat SSH ---"
systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo "ssh inactif"
echo ""
echo "=================================================="
echo " Termine. Depuis le PC :  ping $IP   puis   ssh zed@$IP"
echo "=================================================="
