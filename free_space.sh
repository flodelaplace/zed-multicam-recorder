# ============================================================
#  free_space.sh  --  libere de l'espace eMMC sur un Jetson trop plein
#  AVANT d'installer le ZED SDK. Ne supprime QUE des choses inutiles
#  pour un recorder headless (apps bureau, docs, samples CUDA/VisionWorks).
#  Ne touche PAS aux libs systeme ni au kernel.
#
#  A lancer depuis le PC :   ssh -t zed@<ip> "sudo bash /tmp/free_space.sh"
# ============================================================
set +e

echo "=== ESPACE AVANT ==="; df -h / | tail -1

echo "=== Suppression apps bureau inutiles (libreoffice, chromium, thunderbird) ==="
apt-get purge -y 'libreoffice*' 'chromium-browser*' 'thunderbird*' >/dev/null 2>&1
apt-get autoremove --purge -y >/dev/null 2>&1
apt-get clean

echo "=== Suppression docs + samples CUDA/VisionWorks (non utilises) ==="
rm -rf /usr/local/cuda-10.2/doc /usr/local/cuda-10.2/samples
rm -rf /usr/share/visionworks* /usr/share/visionworks-sfm /usr/share/visionworks-tracking
rm -rf /usr/share/doc/*

echo "=== ESPACE APRES ==="; df -h / | tail -1
echo "(objectif : >1 Go libre pour installer le ZED SDK confortablement)"
