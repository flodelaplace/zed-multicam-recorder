# ZED Multi-cam Recorder

Test rapide d'enregistrement local SVO sur 4 Jetsons (Seeed reComputer J2021 = Xavier NX 8GB)
+ orchestrateur PC central (Windows + WSL2 Ubuntu).

But : valider que chaque Jetson enregistre 1h de SVO sans drops, vs ZED 360 actuel.

## Topologie

```
PC central (WSL2)  ─┐
                    │   Ethernet
                    ▼
              Hub Ethernet
              /  /  \  \
            J1  J2  J3  J4
            |   |   |   |
            Z1  Z2  Z3  Z4   (ZED2 USB 3.0)
```

## Étape 1 — Découvrir les Jetsons

Depuis WSL2 sur le PC :

```bash
# Installe nmap si pas déjà :
sudo apt install nmap

# Scan le sous-réseau (adapte au tien) :
nmap -sn 192.168.1.0/24

# Les Jetsons ont un MAC prefix NVIDIA (00:04:4b:* sur Xavier NX moderne, parfois autre pour Seeed).
# Sinon teste chaque IP suspecte :
ssh nvidia@192.168.1.X    # mot de passe par défaut Seeed = nvidia ou seeed
```

Une fois trouvées, note les 4 IPs (ex. `192.168.1.101..104`) et configure SSH par clé pour
ne pas avoir à taper le mot de passe à chaque déploiement :

```bash
ssh-keygen -t ed25519                        # si pas déjà
for ip in 192.168.1.101 192.168.1.102 192.168.1.103 192.168.1.104; do
    ssh-copy-id nvidia@$ip
done
```

## Étape 2 — Doctor sur chaque Jetson

Copie et lance le diagnostic :

```bash
for ip in 192.168.1.101 ... ; do
    scp jetson_doctor.sh nvidia@$ip:/tmp/
    ssh nvidia@$ip 'bash /tmp/jetson_doctor.sh' | tee doctor_$ip.log
done
```

Vérifie dans chaque log :

- **ZED SDK détecté** (sinon : installer depuis https://www.stereolabs.com/developers/release/)
- **pyzed importable** (`import pyzed.sl`) — sinon lancer `/usr/local/zed/get_python_api.py`
- **Caméra ZED2 sur USB** (ligne `Stereolabs`)
- **NVMe monté** quelque part avec >100 Go libres. **CRITIQUE** : si tu n'as que l'eMMC (32 Go),
  une heure de SVO H.265 va remplir le disque et planter. Faut monter un NVMe ou au minimum
  rediriger les recordings vers un SSD externe USB.
- **Time sync** : note `timedatectl` — pour cette première manip on s'en fout, on alignera en post.
  Pour la vraie campagne il faudra activer chrony en mode PTP.

## Étape 3 — Déployer le recorder

```bash
for ip in 192.168.1.101 ... ; do
    ssh nvidia@$ip 'mkdir -p ~/zed_rec && sudo mkdir -p /data/recordings && sudo chown -R nvidia:nvidia /data'
    scp zed_recorder.py nvidia@$ip:~/zed_rec/
done
```

Lance manuellement dans une session SSH par Jetson (pour ce premier test) :

```bash
ssh nvidia@192.168.1.101
cd ~/zed_rec
python3 zed_recorder.py --output-dir /data/recordings --resolution HD1080 --fps 30
```

Tu dois voir : `[zed_recorder] listening on 0.0.0.0:9999`. Garde la session SSH ouverte.

(Pour la vraie manip on en fera un service systemd qui démarre au boot, mais pas la peine
maintenant.)

## Étape 4 — Orchestrer depuis WSL2

```bash
cd /mnt/c/.../zed-multicam-recorder    # adapte au chemin
HOSTS="192.168.1.101 192.168.1.102 192.168.1.103 192.168.1.104"

# Sanity check :
python3 orchestrator.py ping --hosts $HOSTS

# Test 60 secondes :
python3 orchestrator.py record --hosts $HOSTS --duration 60 --label test_01

# Status pendant l'enregistrement (depuis un autre terminal) :
python3 orchestrator.py status --hosts $HOSTS

# Récupérer les SVO localement :
python3 orchestrator.py pull --hosts $HOSTS --user nvidia --local-dir ./svo
```

## Étape 5 — Analyse

Pour chaque SVO récupéré, ouvre dans `ZED_SVO_Editor` (fourni avec le SDK) ou via ton
pipeline `zed-multicam-sync` existant pour compter les drops. Compare aux valeurs
historiques sous ZED 360.

Métriques à reporter :
- nb frames attendues (`fps × duration`)
- nb frames effectives dans le SVO
- nb drops (du timestamps.csv généré à côté de chaque SVO par le recorder)
- dérive horloge entre Jetsons (max delta des `start_time` reportés par chaque recorder)

## Étape 6 — Run de 1h

Si l'étape 5 sort 0 drops à 5 min : monte à 1h. Si 0 drops à 1h : objectif atteint, on peut
passer à l'industrialisation (systemd, PTP, sync flash LED).

## Troubleshooting

- **`pyzed not found`** : `cd /usr/local/zed && python3 get_python_api.py`
- **`ZED CAMERA NOT DETECTED`** : débrancher/rebrancher la ZED2, vérifier `lsusb`. Si toujours
  rien, le port USB est peut-être en USB2 (vérifier avec `lsusb -t` qu'on est bien en 5000M).
- **Drops malgré tout** : essaye `--resolution HD720` qui divise la bande passante par ~2.
  Si ça aide, le bottleneck est USB ou disque.
- **NVMe pas reconnu** : `lsblk` pour voir les périphériques. Si présent mais pas monté :
  `sudo mkfs.ext4 /dev/nvme0n1p1 && sudo mount /dev/nvme0n1p1 /data`. Ajouter à `/etc/fstab` pour
  qu'il monte au boot.
