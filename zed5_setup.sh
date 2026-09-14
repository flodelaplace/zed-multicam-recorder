# ============================================================
#  zed5_setup.sh  --  config reseau + SSH pour Jetson neuf (zed5)
#  A LANCER SUR LE JETSON, dans un terminal :
#      sudo bash zed5_setup.sh
#  (c'est du texte simple : tu peux aussi l'ouvrir et copier-coller)
# ============================================================

STATIC_IP="192.168.0.7/24"
IFACE="eth0"

echo "=================================================="
echo " 1. Detection de la connexion filaire sur $IFACE"
echo "=================================================="
# Profil NetworkManager actuellement lie a eth0
CON="$(nmcli -t -f NAME,DEVICE con show | awk -F: -v d="$IFACE" '$2==d{print $1; exit}')"
# Sinon, premier profil ethernet existant
if [ -z "$CON" ]; then
  CON="$(nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
fi
# Sinon, on cree un profil propre
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
# Le mode 20W 6-core a deja ete choisi a l'assistant ; on ne le force pas.
nvpmodel -q 2>/dev/null | head -4
jetson_clocks 2>/dev/null && echo "jetson_clocks OK (frequences au max)" \
  || echo "jetson_clocks non applique (non bloquant)"

echo "=================================================="
echo " 5. VERIFICATION"
echo "=================================================="
echo "--- IP sur $IFACE (doit montrer 192.168.0.7) ---"
ip -4 addr show "$IFACE" | grep inet || echo "PAS D'IPv4 !"
echo "--- Etat SSH ---"
systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo "ssh inactif"
echo ""
echo "=================================================="
echo " Termine. Depuis le PC, on testera :"
echo "    ping 192.168.0.7   puis   ssh zed@192.168.0.7"
echo "=================================================="