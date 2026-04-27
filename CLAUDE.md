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
trigger + orchestration. C'est l'architecture qui aurait dû être en place
dès le début.

## Architecture du pipeline

```
   PC central (Windows + WSL2 Ubuntu)
      └─ orchestrator.py    [TCP client]
                │
                ▼
        Hub / Switch Ethernet
        /        |       \         \
    Jetson 1  Jetson 2  Jetson 3  Jetson 4
    (J2021    (Xavier NX, NVMe SSD pour /data)
     Seeed)
        │ USB 3.0
        ▼
       ZED2  (un par Jetson)
```

Chaque Jetson exécute `zed_recorder.py` qui :
1. ouvre la ZED2 en mode RGB-only (`DEPTH_MODE.NONE`),
2. écoute en TCP port 9999,
3. sur START, lance un thread qui boucle `Camera.grab()` et écrit en SVO
   (compression H.265 hardware via NVENC),
4. enregistre un sidecar `*.timestamps.csv` avec une ligne par frame
   (`frame_idx, hw_ts_ns, mono_ns, dropped_since_prev`),
5. sur STOP, ferme proprement et renvoie les stats.

Le PC central (`orchestrator.py`) parle aux 4 Jetsons en parallèle via
`ThreadPoolExecutor`, mesure la dispersion temporelle des STARTs, et pull
les SVO en SCP en fin de session.

## État actuel — phase 1 (test 4 cam dans salle AQM)

Statut : code écrit, jamais lancé sur le matériel réel.

Étapes prévues, dans l'ordre :

1. **Découverte réseau** : retrouver les IPs des 4 Jetsons via `nmap -sn`,
   première connexion SSH, copie de clé publique.
2. **Doctor** : `jetson_doctor.sh` exécuté sur chaque Jetson pour valider
   ZED SDK, pyzed, USB 3.x, NVMe monté, espace dispo, time sync.
3. **Déploiement** : `zed_recorder.py` poussé en SCP sur les 4 Jetsons,
   lancé manuellement dans 4 sessions SSH (pas encore de systemd).
4. **Smoke test** : 60 s d'enregistrement orchestré, vérifier 0 drops.
5. **Test 1h** : la vraie validation. Si zéro drop → architecture validée.
6. **Industrialisation** (phase 2) : systemd unit, PTP, sync flash LED,
   déploiement Ansible.

## Décisions techniques à conserver

- **`DEPTH_MODE.NONE`** systématique au recording : la depth ZED coûte cher
  en GPU/CPU et n'est pas utilisée en aval. Si quelqu'un te demande de l'activer,
  questionne d'abord pourquoi — c'est probablement une mauvaise idée pour la
  reliability du grab loop.
- **SVO compression H.265** (pas H.264, pas LOSSLESS) : meilleur ratio à
  qualité visuelle équivalente, encodeur HW NVENC du Xavier NX le supporte
  nativement.
- **TCP JSON line-delimited** plutôt que ZMQ/gRPC : zéro dépendance, débuggable
  au `nc`, suffisant pour la poignée de commandes (PING / STATUS / START / STOP).
- **Sidecar CSV par SVO** : c'est notre source de vérité pour analyser les drops
  en post. Le `dropped_since_prev` vient de `zed.get_frame_dropped_count()` du SDK,
  qui est plus fiable que de compter les erreurs de `grab()`.
- **Pas de PTP au début** : on commence par mesurer la dispersion réelle des
  starts (`spread_ms` dans `cmd_record`). Si <100 ms, NTP suffit. Si pas, on
  passe à PTP.
- **Pas de systemd au début** : le recorder se lance à la main pour ce test 1.
  Une fois validé, on en fait une unité systemd avec `Restart=always`.

## Cible long terme — 200 patients

Quand on passera à la prod, il faudra ajouter :

- **Sync visuelle hardware** : flash LED piloté par Arduino (USB depuis PC central)
  visible par les N caméras. Deux flashs (début + fin d'essai) → mesure de la
  dérive temporelle sur la durée. Ces flashs seront détectés automatiquement
  dans les SVO en post (saut de luminance), supprimant le besoin de la GUI
  manuelle de `zed-multicam-sync`.
- **PTP réel** entre Jetsons (chrony en mode PTP slave, master sur le PC central).
- **systemd units** pour le recorder, avec restart auto.
- **Validation auto post-essai** : script qui à la fin de chaque session sort
  un rapport go/no-go (drops < seuil, dérive < seuil, sujet visible sur toutes
  les caméras pendant la phase clé).
- **Métadonnées par session** : ID patient anonymisé, timestamps, version du
  protocole, calib extrinsèque utilisée. Persistées dans une SQLite ou un CSV
  versionné, indispensable pour la publication du dataset.

## Comment travailler sur ce projet

### Tester côté PC sans Jetson

L'orchestrator est tout en lib standard Python. Tu peux le tester en lançant
un faux serveur recorder en local :

```bash
# Terminal 1 : faux recorder
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
"

# Terminal 2 :
python3 orchestrator.py ping --hosts 127.0.0.1
```

### Tester côté Jetson

Pas de simulateur ZED2 fonctionnel. Il faut une vraie caméra branchée.
`zed_recorder.py --resolution VGA --fps 15` permet de tourner sur des
configurations USB plus modestes pour debug.

### Itérer sur le code

- `orchestrator.py` se modifie côté WSL2, on relance et c'est bon.
- `zed_recorder.py` se modifie côté WSL2 puis se redéploie en SCP. Cf.
  l'alias `zed-deploy` recommandé dans la conversation pré-projet.
- Sur Jetson, kill de l'ancien recorder via Ctrl-C dans la session SSH puis
  relance.

## Gotchas / pièges connus

- **eMMC vs NVMe** : le Xavier NX a 16 Go d'eMMC interne. Une heure de SVO
  H.265 = ~20 Go. SI le NVMe n'est pas monté sur `/data`, le recording remplit
  l'eMMC et plante le Jetson. Le `jetson_doctor.sh` vérifie ce point —
  c'est CRITIQUE.
- **USB 2.0 silencieux** : si la ZED2 est branchée sur un port marqué USB 3
  mais qu'un câble pourri ou un hub USB 2 est entre les deux, elle marche
  toujours (en mode dégradé) sans erreur explicite. Le doctor check via
  `lsusb -t` la vitesse négociée (`5000M` = OK, `480M` = FAIL).
- **`pyzed` pas installé** : le ZED SDK pose les libs C++ mais pas le binding
  Python par défaut. Il faut lancer `python3 /usr/local/zed/get_python_api.py`
  une fois après install du SDK.
- **`get_frame_dropped_count()`** retourne le nombre depuis le DERNIER `grab()`,
  pas un cumul. Le recorder fait la somme correctement, ne pas casser ça.
- **Multi-thread + ZED SDK** : le SDK ZED a parfois des soucis avec spawn de
  processus. On utilise des threads (pas du `ProcessPoolExecutor`) côté
  recorder. Garder ça en tête si on optimise.
- **WSL2 networking** : depuis WSL2, les Jetsons sur le LAN sont accessibles
  directement (WSL2 a son propre stack réseau qui sort via NAT du Windows host).
  Pas besoin de port-forwarding. En revanche pour qu'un Jetson appelle WSL2
  c'est plus chiant (port-forwarding Windows nécessaire) — on évite ce sens.

## Fichiers du projet

| Fichier | Lieu d'exécution | Rôle |
|---|---|---|
| `README.md` | (humain) | Quick start utilisateur |
| `CLAUDE.md` | (toi) | Ce fichier |
| `jetson_doctor.sh` | Jetson | Diagnostic env, à lancer une fois par Jetson |
| `zed_recorder.py` | Jetson | Service de recording (TCP server + grab loop) |
| `orchestrator.py` | PC / WSL2 | Contrôleur (ping/status/record/pull/report) |

## Conventions de code

- Python 3.6+ compatible (les Jetsons tournent souvent encore sous 3.6/3.8).
- Type hints quand ça aide la lisibilité, pas de fanatisme.
- F-strings OK, walrus opérateur déconseillé (compat 3.6).
- Pas de dépendance externe ajoutée sans bonne raison — on tient en stdlib +
  `pyzed` côté recorder, stdlib pure côté orchestrator.
- Logs : `print(..., flush=True)` suffit pour le recorder (capture par systemd
  plus tard). Pas de `logging` configuré, on peut l'ajouter quand on industrialisera.

## Ressources externes

- ZED SDK Python API: https://www.stereolabs.com/docs/api/python/
- `Camera.enable_recording`: https://www.stereolabs.com/docs/api/python/classpyzed_1_1sl_1_1Camera.html
- ZED SDK on Jetson best practices: https://www.stereolabs.com/docs/installation/jetson
- Auteur du projet : Florian Delaplace, florian.delaplace@sportfx.ai
