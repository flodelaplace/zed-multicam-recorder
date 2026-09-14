# Procédure d'acquisition — salle AQM (12 ZED2)

Démarche complète pour une session d'enregistrement, **sans aide extérieure**.
Tout se fait depuis le PC (Windows + WSL2 Ubuntu) et le navigateur.

---

## 0. Une seule fois (déjà fait, à revérifier seulement si ça casse)

- `C:\Users\fdela\.wslconfig` contient `networkingMode=mirrored` (WSL voit le réseau labo).
- Service Windows **w32time arrêté/désactivé** et règle pare-feu **UDP 123 entrant** (sinon le NTP
  des Jetsons ne passe pas). Voir README §6a.
- `chrony` (serveur NTP du PC) est `enabled` dans WSL : il démarre tout seul avec WSL.
- Sur chaque Jetson, le recorder est un **service systemd** (`zed-recorder`) : il démarre au boot
  et redémarre tout seul s'il plante. **Rien à lancer côté Jetson.**

---

## 1. Brancher le matériel

1. Allumer le **switch FS PoE**, brancher/allumer les **12 Jetsons**, chaque ZED2 sur son Jetson
   (port USB 3, câble d'origine).
2. Brancher le **câble Ethernet du PC** sur le switch.
3. Attendre **1-2 min** que les Jetsons bootent.

## 2. Préparer le réseau du PC

### 2a. Couper le VPN Cisco AnyConnect ⚠️

Le VPN capture la plage `192.168.0.x` (même plage que le labo) : **avec le VPN actif, aucun
Jetson ne répond.** Le déconnecter pendant toute la session. Internet reste dispo par le Wi-Fi.

### 2b. IP fixe sur la carte Ethernet

Le switch n'a **pas de DHCP**. La carte Ethernet du PC (Intel I219-LM, nommée « Ethernet ») doit
être en **IP fixe `192.168.0.50`, masque `255.255.255.0`, sans passerelle**.

Si elle a été remise en automatique (DHCP) pour avoir le réseau ailleurs, la repasser en fixe :

- **Graphique** : Paramètres → Réseau → Ethernet → Attribution IP → Modifier → Manuel → IPv4 :
  IP `192.168.0.50`, masque `255.255.255.0` (longueur de préfixe `24`), passerelle **vide**.
- **Ou PowerShell admin** :
  ```powershell
  Set-NetIPInterface -InterfaceAlias Ethernet -Dhcp Disabled
  Remove-NetIPAddress -InterfaceAlias Ethernet -AddressFamily IPv4 -Confirm:$false
  New-NetIPAddress -InterfaceAlias Ethernet -IPAddress 192.168.0.50 -PrefixLength 24
  ```

Pour **revenir en automatique** plus tard (réseau d'entreprise sur ce port) :
```powershell
Set-NetIPInterface -InterfaceAlias Ethernet -Dhcp Enabled
```

### 2c. Vérifier (terminal Ubuntu/WSL)

```bash
ip -4 -br addr show eth0          # doit afficher 192.168.0.50/24
ping -c 2 192.168.0.9             # un Jetson doit répondre
```

## 3. Lancer le dashboard

Ouvrir un terminal **Ubuntu (WSL)** puis :

```bash
cd ~/zed-multicam-recorder
bash start_dashboard.sh
```

Ouvrir **http://localhost:8080** dans le navigateur.
Laisser ce terminal ouvert pendant toute la session (**Ctrl-C** dedans pour arrêter le dashboard).
Relancer la commande est sans risque : elle tue l'ancienne instance avant.

## 4. Vérifier la flotte (dans le dashboard)

1. **↻ Vérifier** : chaque tuile caméra doit être verte (recorder OK, NTP synchronisé, USB 3,
   disque). Si une cam est rouge, voir §7.
2. **👁 Aperçu** (~15 s) : mosaïque des 12 vues pour contrôler cadrage et orientation.

## 5. Session patient

1. **🔓 Déverrouiller** le registre patient (mot de passe du registre).
2. **+ Nouveau patient** → **Créer le code patient** (code anonymisé type `416XMM`), ou
   sélectionner un patient existant.
3. La bande **Protocole** montre les tâches faites ✅ / restantes ⬜.

### Enregistrer une tâche

1. Cliquer la tâche : **🎯 Calibration**, **▶ Marche**, **▶ 5×5STS**, **▶ STS 1min** ou **▶ TUG**.
2. Pré-check automatique. Si des cams manquent → gros avertissement →
   **Oui, enregistrer** (avec les cams prêtes) ou **Annuler**.
3. Ouverture des cams (~10-15 s) puis **décompte 3 - 2 - 1 - GO ! sonore** → lancer le patient
   au GO. Barre **REC rouge** + chrono en haut pendant la capture.
4. **■ FIN** pour arrêter. Le tableau de stats s'affiche : frames, **drops réels** par cam
   (traits rouges sur la timeline) et **décalage de départ** entre cams.
   Repère : pertes < 0.3 % par cam = OK.
5. Refaire une tâche = nouvel essai (`essai_2`, …), rien n'est écrasé.

**Toujours enregistrer une 🎯 Calibration** (quelqu'un qui marche dans tout le volume) par
session : elle sert au calcul des positions des caméras.

### Paramètres

Garder **HD720 @ 30 fps** (validé à 12 cams : ~0.07 % de pertes).
HD720@60 ≈ 13 % de pertes, HD1080@30 ≈ 34 % → à éviter.

## 6. Après les enregistrements

Section **Enregistrements sur les Jetson** (**↻ Lister**) :

1. **⬇ Extraire les vidéos** (toutes les prises pas encore rapatriées) ou **⬇ Extraire** sur une
   ligne → copie dans `data/<code>/<tâche>/essai_<N>/raw_svo/`.
2. **🎬 MP4 synchro** sur une prise → `raw_mp4/` : un MP4 par cam, même longueur, calés sur
   l'horloge, frames noires aux pertes (listées dans `*.dropped.json`).
3. **🧭 Calibrer** sur la prise de calibration convertie → taille du sujet + frame de référence →
   **Lancer la calibration** (plusieurs minutes, GPU) →
   `calib_output/results/Calib_scene_calibrated.toml`.
4. **📊 Stats** pour revoir les stats d'une prise.
5. En fin de patient : **🧹 Clôturer le patient** = extrait tout ce qui reste PUIS supprime les
   prises des Jetsons (libère l'eMMC). Ne supprime jamais une prise non extraite.
6. **🔒 Verrouiller** le registre.

Les données restent sur le PC dans `~/zed-multicam-recorder/data/` (jamais versionnées dans git).

## 7. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| Aucun Jetson ne répond | VPN actif, ou carte Ethernet en DHCP (IP `169.254.x.x`) | §2a / §2b |
| Un seul Jetson absent | pas alimenté / câble réseau / encore en boot | vérifier LED du port switch, attendre 2 min |
| Cam rouge « recorder KO » | service planté | bouton **⟳** de la tuile, sinon **🔄 Relancer la flotte** |
| Cam présente mais pas de vidéo (IMU seule) après reboot | lien USB 3 coincé | bouton **🔧 Réparer USB** de la tuile ; sinon débrancher/rebrancher l'USB de la cam |
| USB « 480M » au lieu de « 5000M » | câble ou port USB 2 | changer de câble/port |
| NTP non synchronisé | w32time relancé par Windows ou pare-feu | PowerShell admin : `Stop-Service w32time`, vérifier règle UDP 123 ; puis attendre 1 min |
| Grosse perte (stall de plusieurs secondes) | eMMC du Jetson trop pleine (> 80 %) | **🧹 Clôturer le patient** / supprimer des prises extraites |
| Détail d'une erreur | — | bouton **📋** de la tuile (journal du recorder) |

### Commandes utiles (terminal WSL, dans `~/zed-multicam-recorder`)

```bash
python3 orchestrator.py --config config.json ping       # répond-il ? (12 lignes OK/ERR)
python3 orchestrator.py --config config.json status     # état des recorders
ssh zed@192.168.0.X                                     # se connecter à un Jetson (J7 : zed7@192.168.0.9)
sudo systemctl restart zed-recorder                     # (sur le Jetson) relancer le recorder
journalctl -u zed-recorder -n 50                        # (sur le Jetson) derniers logs
```

### Plan d'adressage

| Label | IP | | Label | IP |
|---|---|---|---|---|
| J5 | 192.168.0.7 | | zed21 | 192.168.0.21 |
| J7 | 192.168.0.9 | | zed22 | 192.168.0.22 |
| zed9 | 192.168.0.10 | | zed23 | 192.168.0.23 |
| zed10 | 192.168.0.11 | | zed25 | 192.168.0.25 |
| zed12 | 192.168.0.12 | | zed26 | 192.168.0.26 |
| J8 | 192.168.0.20 | | zed29 | 192.168.0.29 |
| **PC** | **192.168.0.50** | | | |
