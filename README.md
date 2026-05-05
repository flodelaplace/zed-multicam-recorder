# zed-multicam-recorder

Distributed multi-camera recording pipeline for ZED2 stereo cameras connected
to a fleet of NVIDIA Jetsons. Each Jetson records its own SVO file locally
(no live streaming over the network) and a central PC orchestrates synchronized
START/STOP across all of them.

Designed to replace the lossy *ZED 360* live-fusion workflow with a clean,
scientifically-reproducible capture path for markerless biomechanics.

## What's in this repo

| File | Where it runs | Role |
|---|---|---|
| `bootstrap.sh` | PC | One-shot: download artifacts and push to all Jetsons |
| `install_jetson.sh` | Jetson (via SSH after bootstrap) | Install ZED SDK + Python deps offline |
| `setup_ntp.sh` | PC | Configure PC as NTP server + Jetsons as clients |
| `jetson_doctor.sh` | Jetson | Validate ZED SDK + USB + storage + clock |
| `zed_recorder.py` | Jetson | TCP daemon + grab/record loop |
| `orchestrator.py` | PC | Fleet CLI: ping, record, pull, analyze, etc. |
| `gui.py` | PC | Tkinter GUI wrapper around the orchestrator |
| `playback.py` | PC | Mosaic playback synchronised by wall-clock |
| `config.example.json` | — | Sample fleet config |

## Hardware assumptions

- N Jetsons (validated on **Seeed reComputer J10 / Jetson Nano + JetPack 4.6.1**;
  should work on Xavier NX with minor changes — see *Adapting to other hardware*
  below).
- One ZED2 camera per Jetson, plugged on a USB 3.0 port.
- All Jetsons + PC on the same Ethernet LAN.
- The Jetsons can be on an **isolated subnet without internet** — the bootstrap
  fetches everything on the PC and pushes it.
- Static IPs (no DHCP) recommended.

## First-time setup, end to end

### 1 · Network plumbing

Pick a subnet (e.g. `192.168.0.0/24`) and assign each Jetson a static IP. The
PC must be on the same subnet. Verify with `ping`.

### 2 · SSH access

Generate a key on the PC if you don't have one:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
```

Push it to each Jetson (you'll need the password once per Jetson — the username
may differ across machines, e.g. `zed1` vs `zed`):

```bash
ssh-copy-id -o StrictHostKeyChecking=accept-new zed@192.168.0.3
```

After this, plain `ssh zed@192.168.0.3 hostname` should work without prompting.

### 3 · Configure the fleet

```bash
cp config.example.json config.json
$EDITOR config.json
```

Set `port`, `remote_dir`, the list of `hosts` with their `ip`, `user`, and
human-readable `label`. The `openblas_armv8` flag should stay `true` for Jetson
Nano (Cortex-A57) — the modern numpy wheels otherwise crash with SIGILL.

### 4 · Bootstrap

From the PC (needs internet):

```bash
bash bootstrap.sh
```

This:
- Downloads the ZED SDK installer (~46 MB compressed), `pyzed`, `numpy`,
  `pip`, `zstd`, `libturbojpeg` into `./artifacts/`.
- SSH-pushes everything to `/tmp/zed-bootstrap/` on each Jetson.
- Detects each connected ZED2's serial number and fetches its calibration
  file from `calib.stereolabs.com`.

Re-running is safe; existing artifacts are reused.

### 5 · Per-Jetson install (interactive once)

For each Jetson in the fleet, SSH in and run the installer:

```bash
ssh -t zed@192.168.0.3 "bash /tmp/zed-bootstrap/install_jetson.sh"
```

The Stereolabs installer asks ~7 yes/no questions. Answer:

| Prompt | Answer |
|---|---|
| EULA | `Y` |
| Static version | `n` |
| AI module | `n` |
| Maximum performance | `Y` |
| Install samples | `n` |
| Auto-install dependencies | `n` |
| Install the Python API | `Y` |
| Python executable | `python3` |

Two warnings are expected and harmless:
- *"CUDA detection failed"* near the start — CUDA is present at
  `/usr/local/cuda-10.2`, just not on PATH at install time. The runtime works.
- *"Python API failed to install"* at the end — the installer tries to
  download the `pyzed` wheel from the internet; we already pre-installed it
  manually, so this failure is expected.

### 6 · NTP synchronisation (recommended)

Without NTP, the wall-clock time on each Jetson drifts independently (we've
seen Jetsons whose dates differed by years). That makes it impossible to
measure the real inter-camera start delay. With NTP, the Jetsons' clocks are
kept within ~10 ms of the PC's, and the orchestrator's `First-frame spread
across cams` becomes a meaningful sync metric.

#### 6a · Windows / WSL2 prereqs (skip on native Linux)

WSL2 mirrored networking shares ports with the Windows host. Two things
prevent chrony from serving NTP until handled :

1. **`w32time` Windows service holds UDP 123.** Stop it.
2. **Windows Defender Firewall blocks inbound UDP 123.** Add a rule.

From PowerShell **as administrator** :

```powershell
Stop-Service w32time
New-NetFirewallRule -DisplayName "WSL2 chrony NTP" `
    -Direction Inbound -Protocol UDP -LocalPort 123 `
    -Action Allow -Profile Any
```

Optional, to make the `w32time` stop persistent across Windows reboots :

```powershell
Set-Service -Name w32time -StartupType Disabled
```

The firewall rule survives reboots automatically.

#### 6b · Run the setup

```bash
bash setup_ntp.sh
```

The script :
- detects the LAN-side PC IP from the first host in `config.json`,
- installs `chrony` on the PC and writes a server config that uses
  `pool.ntp.org` (when reachable) and the Hyper-V hardware clock `PHC0`
  (when on WSL2) as upstream sources,
- configures each Jetson's `systemd-timesyncd` to sync to the PC,
- verifies that every Jetson reports `System clock synchronized: yes`.

After ~30 seconds the Jetsons' wall-clocks line up.

#### 6c · Verification

```bash
python3 orchestrator.py --config config.json record \
    --duration 10 --label sync_check
```

In the output, look for the line :

```
First-frame spread across cams : XYZ ms
```

A few hundred ms is normal — most of it is variance in `Camera.open()`
warm-up time, not a clock issue. What matters is that the number is now
finite-and-meaningful instead of "50 sec, ignore". Each frame in the
sidecar CSV has a wall-clock timestamp comparable across cameras, which
is what downstream tools need to align frames in post.

### 7 · Validate

```bash
python3 orchestrator.py doctor    --config config.json   # full env audit
python3 orchestrator.py list-cams --config config.json   # confirm SDK sees each ZED2
python3 orchestrator.py launch    --config config.json   # start daemons
python3 orchestrator.py ping      --config config.json
```

You should see `ok` from all hosts and one camera per host listed.

## Daily ops

```bash
# Record 60 s synchronized on every host:
python3 orchestrator.py record --config config.json \
    --duration 60 --label patient_001

# (optional, only if you want to play the videos on the PC) convert each
# remote SVO to MP4 via pyzed on its Jetson — takes about real-time per cam:
python3 orchestrator.py convert-mp4 --config config.json

# Bring SVOs (+ MP4s if converted) + sidecar CSVs + stats.json back :
python3 orchestrator.py pull --config config.json --local-dir ./svo

# Compute REAL frame loss / fps per cam from sidecar CSVs (hw_ts deltas) :
python3 orchestrator.py analyze --local-dir ./svo

# Wall-clock-synchronised mosaic playback (needs the .mp4s — i.e. you ran
# convert-mp4 before pull). Press SPACE / q / . / , / s to control :
python3 playback.py ./svo

# Free up Jetson disk after pulling:
python3 orchestrator.py clean --config config.json --yes

# Stop the recorder daemons:
python3 orchestrator.py kill --config config.json
```

## GUI

If you'd rather click than type :

```bash
python3 gui.py                   # uses ./config.json
python3 gui.py --config foo.json
```

Requires `tkinter` (stdlib; on Linux: `sudo apt install python3-tk`). On WSL2,
Windows 11 already has WSLg so the window opens natively; on Windows 10 you
need an X server (e.g. VcXsrv) running.

The GUI is a thin front-end that calls `orchestrator.py` as a subprocess for
each action — the CLI stays the source of truth, all output streams into the
log area at the bottom.

Layout :
- *Config* row to load any `config.json`
- *Fleet* table showing the configured hosts
- *Daemons* row : resolution + fps pickers, Launch / **Restart** / Kill / Ping / List cams / Status
- *Record* row : duration + label + Record button
- *After recording* row : local dir, **Convert MP4** / Pull / Analyze / **Play sync** / Clean buttons
- *Output* log

## Subcommand reference

```
python3 orchestrator.py --config config.json <SUBCOMMAND>

  ping             Ping all recorders over TCP
  status           Get current state of each recorder
  list-cams        List ZED cameras detected on each host
  launch           Start zed_recorder.py on each host (background daemon)
                   --resolution {HD2K, HD1080, HD720, VGA}
                   --fps        {15, 30, 60, 100}    (resolution-dependent)
                   --wait       seconds to wait for bind (default 4)
  restart          Redeploy zed_recorder.py + (re)start daemon on each host
                   (use after a Jetson reboot or /tmp wipe — equivalent to
                    `deploy-recorder` then `launch`)
  kill             Stop recorders on each host
  record           Trigger synchronized recording on all hosts
                   --duration   seconds (0 = until ENTER)
                   --label      filename prefix
                   --force      proceed even if some hosts unreachable
  convert-mp4      Convert each remote SVO to MP4 (left cam) on its Jetson
                   via pyzed. Output sits next to the SVO so the next `pull`
                   brings everything together. ~1× realtime per camera.
  pull             SCP recordings back to local dir
                   --local-dir  default ./svo
  clean            Delete remote recordings to free Jetson disk
                   --yes        skip confirmation
  analyze          Compute REAL fps + loss from sidecar CSVs (post-pull)
                   --local-dir  default ./svo
  doctor           Run jetson_doctor.sh on each host
  deploy-recorder  Push zed_recorder.py to /tmp on each host
```

`--hosts a.b.c.d e.f.g.h` overrides the host list from config (uses
`default_ssh_user` for all of them).

## Quality / fps choice

ZED2 modes:

| Resolution | Pixels | Max fps |
|---|---|---|
| HD2K | 2208×1242 | 15 |
| HD1080 | 1920×1080 | 30 |
| HD720 | 1280×720 | 60 |
| VGA | 672×376 | 100 |

For markerless biomechanics post-processing (RTMPose, MeTRAbs, etc.) the
sweet spot is **HD1080 @ 30 fps** — full-HD body resolution + dense temporal
sampling. HD2K loses temporal info; HD720 loses spatial detail per joint.

Storage cost at HD1080 + H.264 hardware: ~24 Mbps ≈ **10 GB/h per camera**.
Plan SSDs accordingly (the eMMC of a Jetson Nano holds maybe 30 minutes of
4-cam recording).

## Visualising the sync (`playback.py`)

`playback.py` is a small standalone tool (cv2 only, no ZED SDK on the PC)
that plays N synchronised videos as a mosaic, aligned by wall-clock.

Workflow :

```bash
# 1. Record + convert + pull (on the PC, with daemons running) :
python3 orchestrator.py record       --config config.json --duration 30 --label sync_test
python3 orchestrator.py convert-mp4  --config config.json
python3 orchestrator.py pull         --config config.json --local-dir ./svo

# 2. Play :
python3 playback.py ./svo
```

Each cam pane is annotated with `f<index>  +<ms>` showing the wall-clock
offset of that frame from the global start. With NTP-synced clocks all
panes should advance together (within ±16.5 ms = half a frame at 30 fps).

Controls : `SPACE` pause/resume, `q` quit, `.` step forward, `,` step
backward, `s` print per-cam wall-clock spread for the current cursor (the
key debug metric : if `s` prints e.g. 4.2 ms, all four cams are
*frame-aligned* despite having started 478 ms apart).

## Synchronisation metrics

After every `record`, the orchestrator prints two numbers per camera :

- `start->1st_frame=X ms` — how long this cam took between receiving the
  START command and capturing its first frame. Computed inside the Jetson
  (no clock comparison) so meaningful even without NTP. Typical values on
  Jetson Nano + ZED2 in HD1080@30fps are 15–30 ms (camera open + sensor
  warm-up).

- `First-frame spread across cams` — wall-clock difference between the cam
  that was first ready and the one that was last ready. **Only meaningful
  with NTP** (see step 6 above). Once NTP-synced, this is the real
  inter-camera start offset (a few tens of ms typically).

Without genlock hardware, these tens of ms cannot be eliminated. They can
however be measured, reported, and applied as a per-cam time offset in
post-processing.

## Why "real_misses" instead of "drops"

The Stereolabs `getFrameDroppedCount()` returns a cumulative-since-open count
that increments on internal SDK events (e.g. IMU/video desync) without
correlating to actual gaps in the SVO. We've seen 7000+ "drops" reported
on cameras that recorded an essentially-perfect 30 fps stream.

Our `analyze` subcommand instead reads the sidecar CSV's `hw_ts_ns` column,
computes the median inter-frame interval, and counts intervals greater than
1.5× that as **real misses**. This matches what is actually missing from the
SVO and is the metric you care about for biomechanics post-processing.

## Adapting to other hardware

- **Different JetPack** : edit `SDK_URL` and `SDK_RUN` in `bootstrap.sh`,
  e.g. `https://download.stereolabs.com/zedsdk/4.x/jp5/jetsons` for JP5+.
- **Xavier NX (J20)** : set `openblas_armv8` to `false` in config (no SIGILL
  there) and consider switching `SVO_COMPRESSION_MODE.H264` →
  `SVO_COMPRESSION_MODE.H265` in `zed_recorder.py` (Xavier has H.265 NVENC,
  Nano does not — saves ~30% file size).
- **Other Linux/macOS PC** : the orchestrator is stdlib-only Python 3.7+
  so it runs anywhere. WSL2 with mirrored networking works.

## Troubleshooting

- **`Camera.open() failed: CALIBRATION FILE NOT AVAILABLE`** → bootstrap step 4
  didn't fetch the calibration. Re-run `bootstrap.sh` or manually push
  `SN<serial>.conf` from `http://calib.stereolabs.com/?SN=<serial>` into
  `/usr/local/zed/settings/`.
- **`Camera.open() failed: CAMERA STREAM FAILED TO START`** → physical USB
  unplug + replug usually fixes it. ZED2 firmware can get stuck.
- **`Illegal instruction (core dumped)` when importing pyzed** →
  `OPENBLAS_CORETYPE=ARMV8` is missing. The recorder sets it automatically
  at the top of `zed_recorder.py`; if you import pyzed manually, set it in
  your shell.
- **`module 'time' has no attribute 'time_ns'`** → you're on Python 3.6 and
  trying to run code that wasn't polyfilled. Our `zed_recorder.py` handles it.
- **NTP `Could not open NTP socket on 0.0.0.0:123` on WSL2** → Windows
  `w32time` is holding port 123. Run `Stop-Service w32time` from
  PowerShell admin (see step 6a).
- **Jetson `Timed out waiting for reply from 192.168.0.X:123`** → Windows
  Defender Firewall blocks inbound UDP 123. Add the firewall rule in
  step 6a.
- **Jetsons keep `System clock synchronized: no` after `setup_ntp.sh`** →
  inspect `journalctl -u systemd-timesyncd -n 20` on the Jetson. If it
  shows "Timed out", the PC chrony isn't reachable on UDP 123 (firewall,
  port not bound). Re-check `sudo ss -ulnp | grep chrony` on the PC.
- **Daemons stopped pinging after a Jetson reboot** → `/tmp` was wiped, so
  `zed_recorder.py` is gone too. Hit the **Restart (redeploy)** button in
  the GUI, or run `python3 orchestrator.py restart`. For phase 2 we'll
  move the recorder to a non-volatile path + systemd unit.

## Project context

See `CLAUDE.md` for the agent-facing project briefing (architecture choices,
phase 1 vs phase 2, motivation, dataset publication target).
