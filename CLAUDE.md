# Project context for Claude Code

> Ce fichier oriente Claude Code (et tout autre LLM agent) sur ce projet.
> Il est destiné à un agent de codage, pas à un utilisateur final.
> Pour les instructions d'usage humain, voir `README.md`.

## Mission

Pipeline d'acquisition multi-caméra ZED2 distribué pour analyse markerless
biomécanique. Cible immédiate : test fonctionnel à 4 caméras dans la salle AQM
du labo. Cible finale : campagne de **200 patients** avec publication d'un
dataset, donc qualité reproductible et drops ≈ 0.

L'enjeu scientifique est l'extraction de cinématique 3D markerless à partir
du flux RGB monoculaire (gauche) de chaque ZED2 — la depth ZED n'est PAS
utilisée, seule la stéréo RGB sert via pose estimation type RTMPose / MeTRAbs
en aval.

## Pourquoi ce projet existe

L'auteur (Florian, M2, biomeca) a hérité d'acquisitions faites avec ZED 360
(app Stereolabs de fusion Body Tracking via streaming réseau). Problèmes
constatés sur ces données :

- énormément de frames droppées par caméra (jusqu'à plusieurs %),
- démarrages désynchronisés entre cameras (offsets variables jusqu'à plusieurs frames),
- impossible de calibrer proprement dans certains cas.

Diagnostic : ZED 360 fait du streaming temps-réel pour fusion live, pas de la
capture scientifique propre. La bande passante réseau + le PC central qui
absorbe 8 flux + l'absence de hardware-sync sur ZED2 produisent ces artefacts.

L'auteur a déjà deux repos qui font de la **réparation post-hoc** :

- https://github.com/flodelaplace/zed-multicam-sync — détection drops via
  timestamps SVO, insertion de frames noires, alignement sur événement visuel,
  sidecars JSON. Pipeline mature mais traite des données déjà cassées.
- https://github.com/flodelaplace/lab-camera-dynamic-calibrator — calibration
  extrinsèque markerless à partir d'une personne en mouvement (MeTRAbs +
  Procrustes + Bundle Adjustment).

**Ce projet-ci est différent** : on attaque la **racine** du problème en
remplaçant ZED 360 par un pipeline d'acquisition propre, où chaque Jetson
enregistre son SVO en LOCAL (pas de streaming) et le PC central ne fait que
trigger + orchestration.

## Architecture du pipeline

```
   PC central (Windows + WSL2 Ubuntu)
      └─ orchestrator.py    [TCP client]
                │
                ▼
        Hub / Switch Ethernet (FS PoE)
        /        |       \         \
    Jetson 1  Jetson 2  Jetson 3  Jetson 4
    (Seeed reComputer J10 = Jetson Nano,
     JetPack 4.6.1, Python 3.6, eMMC 14 Go)
        │ USB 3.0
        ▼
       ZED2  (un par Jetson)
```

Chaque Jetson exécute `zed_recorder.py` qui :
1. ouvre la ZED2 en mode RGB-only (`DEPTH_MODE.NONE`),
2. écoute en TCP port 9999,
3. sur START, lance un thread qui boucle `Camera.grab()` et écrit en SVO
   (compression **H.264** hardware via NVENC — voir gotcha plus bas),
4. enregistre un sidecar `*.timestamps.csv` avec une ligne par frame
   (`frame_idx, hw_ts_ns, mono_ns, dropped_since_prev`),
5. sur STOP, ferme proprement et renvoie les stats.

Le PC central (`orchestrator.py`) parle aux N Jetsons en parallèle via
`ThreadPoolExecutor`, fait l'analyse réelle des drops via les hw_ts (pas
via le compteur SDK qui est trompeur), et pull les SVO en SCP.

## État actuel — phase 1 (validée mai 2026)

Pipeline validé end-to-end sur 4 cams en HD1080@30fps, 2 minutes. Vraies pertes
mesurées 0.03–0.17% par cam (cumulé < 1% sur l'ensemble). Bilan :

- ✅ Bootstrap offline fonctionnel (Jetsons sans internet)
- ✅ ZED SDK 3.8.2 + pyzed installés sur les 4
- ✅ Calibrations factory récupérées et déployées par cam
- ✅ Orchestrator config-driven (`config.json`) + GUI Tkinter
- ✅ Recorder rapporte `start_to_first_frame_ms` par cam (latence open+warmup,
   mesurée intra-Jetson donc valide sans NTP)
- ✅ NTP via `setup_ntp.sh` — chrony côté PC + systemd-timesyncd côté Jetsons.
   Une fois actif, le `record` sort un `First-frame spread across cams` qui
   est le vrai métric de sync (offset wall-clock entre les premières frames
   capturées par chaque cam).
- ⏳ SSD USB par Jetson (en commande) avant validation 1h
- ⏳ Rejouage en C++ + systemd à la phase 2

## Décisions techniques à conserver

- **`DEPTH_MODE.NONE`** systématique : la depth ZED coûte cher en GPU/CPU et
  n'est pas utilisée en aval. Si quelqu'un te demande de l'activer, questionne
  d'abord.
- **SVO compression `H264`** : le **Jetson Nano (T210, NVENC v6) ne fait PAS
  H.265 hardware**, seul Xavier+ le supporte. Activer H.265 sur Nano fait
  silencieusement basculer en encode software → CPU saturé, drops massifs.
  → si on industrialise sur Xavier NX en phase 2, on pourra repasser en H.265.
- **Extension `.svo`** (pas `.svo2`) : SDK 3.x parle SVO v1, le `.svo2` est
  pour SDK 4.x+.
- **TCP JSON line-delimited** plutôt que ZMQ/gRPC : zéro dépendance, débuggable
  au `nc`.
- **Sidecar CSV par SVO** : c'est notre source de vérité pour l'analyse
  post-hoc. Le `dropped_since_prev` est *recomputé* (delta cumulative) — voir
  le gotcha sur `get_frame_dropped_count()` plus bas.
- **`OPENBLAS_CORETYPE=ARMV8`** au top du recorder, AVANT tout import qui
  charge numpy : sans ça, le wheel manylinux2014_aarch64 de numpy crash en
  SIGILL sur Cortex-A57. Le recorder le set automatiquement via
  `os.environ.setdefault`.
- **Polyfills Python 3.6** : `from __future__ import annotations` et `T | None`
  ne marchent pas sur Python 3.6 (JetPack 4.6). Utiliser `Optional[T]` et
  remplacer `time.time_ns()` / `time.monotonic_ns()` par leurs polyfills (cf
  `zed_recorder.py`).
- **Drop measurement** : on n'utilise PAS l'orchestrator's `sdk_drops` (le
  compteur SDK est non corrélé aux pertes réelles). On utilise `analyze`
  qui compte les intervalles hw_ts > 1.5× la médiane.

## Cible long terme — 200 patients

Quand on passera à la prod, il faudra ajouter :

- **Stockage SSD USB** monté sur `/data/recordings` (l'eMMC 14 Go ne tient pas).
- **Sync visuelle hardware** : flash LED piloté par Arduino visible par les N
  caméras. Deux flashs (début + fin) → mesure de la dérive temporelle. Détection
  auto dans les SVO en post (saut de luminance), supprimant le besoin de la
  GUI manuelle de `zed-multicam-sync`. Alternative validée par Florian : objet
  qui tombe au sol.
- **NTP/PTP** entre Jetsons (chrony, master sur le PC central).
- **systemd unit** pour le recorder, avec `Restart=always`.
- **Validation auto post-essai** : script qui à la fin de chaque session sort
  un rapport go/no-go.
- **Métadonnées par session** : ID patient anonymisé, timestamps, version
  protocole, calib extrinsèque utilisée. Persistées en SQLite ou CSV versionné.

## Layout du projet

```
zed-multicam-recorder/
├── README.md                 humain
├── CLAUDE.md                 toi
├── config.example.json       template config
├── config.json               (gitignored typiquement) config réelle
├── bootstrap.sh              PC : download artifacts + push fleet
├── install_jetson.sh         Jetson : install offline ZED SDK + deps
├── setup_ntp.sh              PC : chrony serveur + Jetsons systemd-timesyncd
├── jetson_doctor.sh          Jetson : env audit
├── zed_recorder.py           Jetson : daemon TCP + grab loop
├── orchestrator.py           PC : fleet CLI (ping, record, restart, analyze, ...)
├── gui.py                    PC : Tkinter front-end (subprocess orchestrator.py)
└── artifacts/                (gitignored) téléchargements bootstrap
```

## Comment travailler sur ce projet

### Itérer sur le recorder

```bash
# Modif locale puis push :
python3 orchestrator.py deploy-recorder --config config.json
python3 orchestrator.py kill   --config config.json
python3 orchestrator.py launch --config config.json
```

### Tester côté PC sans Jetson

`orchestrator.py` est stdlib pure, peut se tester avec un faux serveur :

```bash
python3 -c "
import socket, json, threading
def handle(c):
    f = c.makefile('rwb', buffering=0)
    for line in f:
        msg = json.loads(line)
        f.write((json.dumps({'ok': True, 'echo': msg}) + '\n').encode())
s = socket.socket(); s.bind(('127.0.0.1', 9999)); s.listen()
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
" &
python3 orchestrator.py ping --hosts 127.0.0.1
```

### Tester côté Jetson sans cam

Pas de simulateur ZED. Il faut une vraie caméra. `--resolution VGA --fps 15`
allège pour debug.

## Gotchas / pièges connus (TOUS rencontrés sur le terrain)

- **`get_frame_dropped_count()` est CUMULATIF**, pas un delta. La doc
  Stereolabs officielle : *"Returns the number of frames dropped since
  Grab() was called for the first time."*. Le `zed_recorder.py` calcule
  donc le delta soi-même (`dropped_cum - last_dropped_cum`) pour le CSV,
  et stocke la valeur cumulative finale dans `frames_dropped`. Et de
  toute façon c'est trompeur — le compteur incremente sur des events
  internes du SDK pas corrélés aux vraies pertes vidéo. **Toujours
  utiliser `orchestrator.py analyze` pour le vrai métric.**

- **Numpy SIGILL sur Nano** : le wheel `manylinux2014_aarch64` utilise
  un OpenBLAS qui détecte le CPU au runtime et émet des instructions ARM
  non supportées par Cortex-A57. Fix : `OPENBLAS_CORETYPE=ARMV8` avant
  `import numpy`. Le recorder le fait. Si tu ajoutes un script Python
  qui import pyzed/numpy sur le Jetson, n'oublie pas de set ce env var.

- **CUDA pas dans PATH** : le doctor et l'installer ZED disent "CUDA
  detection failed" parce que `nvcc` n'est pas dans `$PATH` par défaut
  sur JetPack. Mais CUDA est bien là sous `/usr/local/cuda-10.2`. Le
  runtime ZED SDK le trouve via les libs `.so`, donc pas bloquant.

- **Python 3.6** sur les Nano (JetPack 4.6) : pas de `time.time_ns()`,
  pas de `from __future__ import annotations`, pas de `T | None`.
  Voir polyfills dans `zed_recorder.py`.

- **pyzed install échoue offline** : le `get_python_api.py` du SDK fait un
  `urllib.urlretrieve` qui timeout sans internet. Solution : on
  pré-télécharge le wheel `pyzed-3.8-cp36-cp36m-linux_aarch64.whl` via
  bootstrap et on `pip install --user --no-deps`. La dep `Cython` du
  metadata wheel est fausse (le binaire est précompilé), d'où `--no-deps`.

- **Calibration files** : ZED SDK télécharge `SN<serial>.conf` au
  premier `Camera.open()` depuis `calib.stereolabs.com`. Sans internet
  → "CALIBRATION FILE NOT AVAILABLE". Le bootstrap les fetche pour
  chaque cam connectée et les pose dans `/usr/local/zed/settings/`.

- **USB 2.0 silencieux** : si la ZED2 est branchée sur un port USB 3
  mais qu'un câble pourri ou hub USB 2 est entre les deux, elle marche
  toujours en mode dégradé sans erreur. Le doctor check `lsusb -t`
  (`5000M` = OK, `480M` = FAIL).

- **ZED2 firmware bloqué** : symptôme = `lsusb` voit la cam mais
  `sl.Camera.get_device_list()` retourne `[]`, ou `Camera.open()` →
  "CAMERA STREAM FAILED TO START" / "CAMERA MOTION SENSORS NOT DETECTED".
  Fix physique : débrancher / rebrancher le câble USB côté cam ou Jetson.
  Si récurrent → reboot le Jetson.

- **eMMC saturée à >80%** : performances chutent (write amplification du
  controller eMMC). Symptôme = stalls de 1-2 s pendant le record →
  avg fps qui chute. Solution : SSD USB externe.

- **Multi-thread + ZED SDK** : le SDK a parfois des soucis avec spawn de
  processus. On utilise des threads (pas `ProcessPoolExecutor`) côté
  recorder. Garder en tête si on optimise.

- **WSL2 networking** : depuis WSL2, les Jetsons sur le LAN sont accessibles
  directement (mirrored networking config `.wslconfig`). En revanche pour
  qu'un Jetson appelle WSL2 c'est plus chiant (port-forwarding Windows
  nécessaire) — on évite ce sens.

- **`pkill -f motif`** matche aussi le bash distant qui contient le motif
  en argument → on se suicide soi-même. Préférer `ps -C python3 -o pid=,cmd=`
  + filter, ou `fuser -k <port>/tcp` (cible précise).

- **NTP sur WSL2 mirrored = port 123 partagé avec Windows**. Deux pièges
  rencontrés en mai 2026 :
  1. **w32time tient UDP 123** : chrony échoue avec "Could not open NTP
     socket on 0.0.0.0:123". Fix : `Stop-Service w32time` côté PowerShell
     admin (et `Set-Service w32time -StartupType Disabled` pour persister).
  2. **Windows Defender Firewall bloque l'UDP 123 entrant** : journal
     timesyncd des Jetsons dit "Timed out waiting for reply from
     192.168.0.50:123". Fix : `New-NetFirewallRule ... -LocalPort 123 ...`
     côté PowerShell admin (rule survit aux reboots).
  3. **`bindaddress <PC_LAN_IP>`** dans chrony.conf est utile : évite les
     conflits sur 0.0.0.0:123 et ne change rien côté clients (qui se
     connectent à l'IP LAN du PC de toute façon).
  4. **`refclock PHC /dev/ptp0`** dans chrony.conf donne à chrony une
     référence stratum-1 (le clock Hyper-V) sans avoir besoin d'internet.
     Sans, chrony tombe en `local stratum 10` qui marche mais c'est moins
     précis. Sur native Linux sans Hyper-V, /dev/ptp0 n'existe pas et
     cette ligne est ignorée silencieusement par chrony.
  Le `setup_ntp.sh` du repo automatise tout côté Linux et imprime les
  prereqs Windows en début d'exécution si WSL2 détecté.

- **`/tmp` se vide au reboot du Jetson** : zed_recorder.py et
  /tmp/recordings disparaissent. Solution actuelle : sous-cmd `restart`
  dans orchestrator (et bouton "Restart (redeploy)" dans la GUI). En
  phase 2 il faudra poser le recorder à un path persistant
  (`/usr/local/bin/zed_recorder.py`) avec une unité systemd.

## Conventions de code

- Python 3.6+ compatible côté recorder. Stdlib seule côté orchestrator
  (pas de PyYAML / etc., utiliser JSON pour la config).
- Type hints quand ça aide la lisibilité, pas de fanatisme. Préférer
  `Optional[T]` à `T | None` (Python 3.6 compat).
- F-strings OK, walrus déconseillé (compat 3.6).
- Logs : `print(..., flush=True)` suffit (capture par systemd plus tard).
- Pas d'emoji dans le code.

## Ressources externes

- ZED SDK Python API : https://www.stereolabs.com/docs/api/python/
- `Camera.enable_recording` : https://www.stereolabs.com/docs/api/python/classpyzed_1_1sl_1_1Camera.html
- ZED SDK on Jetson : https://www.stereolabs.com/docs/installation/jetson
- Calibration files par serial : http://calib.stereolabs.com/?SN=<serial>
- Auteur : Florian Delaplace, florian.delaplace@sportfx.ai
