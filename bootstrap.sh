#!/bin/bash
# bootstrap.sh — one-shot setup runner from the PC side.
#
# What it does:
#   1. Downloads ZED SDK installer + python wheels + .deb files into
#      ./artifacts/ on the PC (idempotent: skipped if already present).
#   2. SSH-deploys these artifacts + zed_recorder.py + jetson_doctor.sh +
#      install_jetson.sh to /tmp/zed-bootstrap/ on each Jetson listed in
#      the fleet config.
#   3. Detects the serial of each connected ZED2, downloads the matching
#      calibration file, and pushes it.
#   4. Reminds you to run install_jetson.sh on each Jetson interactively
#      (the Stereolabs SDK installer requires a few Y/n keystrokes).
#
# Requirements on the PC:
#   - python3, curl, ssh, scp
#   - Internet access (Jetsons stay on isolated LAN)
#   - SSH keys already deployed to each Jetson (run ssh-copy-id once first)
#   - A valid config.json (see config.example.json)
#
# Usage:
#     bash bootstrap.sh                     # uses config.json
#     bash bootstrap.sh --config foo.json   # custom config
#
# Re-run safe: artifacts are reused if already downloaded.

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG=${CONFIG:-config.json}
while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG=$2; shift 2 ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# //; s/^#//'; exit 0 ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found. Copy config.example.json -> config.json and edit it."
    exit 1
fi

ART=$HERE/artifacts
mkdir -p "$ART"

# ZED SDK 3.8.2 for L4T 32.6 (JetPack 4.6.x). For Xavier NX or other JetPack
# versions, change this URL accordingly. The .run filename below is what the
# server returns after redirect; keep it stable so install_jetson.sh can find it.
SDK_URL=https://download.stereolabs.com/zedsdk/3.8/l4t32.6/jetsons
SDK_RUN=ZED_SDK_Tegra_L4T32.6_v3.8.2.zstd.run

# Static .deb packages (Ubuntu 18.04 arm64 / bionic).
ZSTD_URL=http://ports.ubuntu.com/ubuntu-ports/pool/main/libz/libzstd/zstd_1.3.3+dfsg-2ubuntu1_arm64.deb
TURBOJPEG_URL=http://ports.ubuntu.com/ubuntu-ports/pool/universe/libj/libjpeg-turbo/libturbojpeg_1.5.2-0ubuntu5.18.04.6_arm64.deb

# Python wheels (manylinux2014_aarch64, cp36).
NUMPY_WHL=numpy-1.19.5-cp36-cp36m-manylinux2014_aarch64.whl
PYZED_URL=https://download.stereolabs.com/zedsdk/3.8/whl/linux_aarch64/pyzed-3.8-cp36-cp36m-linux_aarch64.whl
PIP_WHL=pip-21.3.1-py3-none-any.whl

echo "==> [1/4] Downloading artifacts to $ART (skipping any already present)"

dl() {
    local out=$1 url=$2
    if [ -s "$ART/$out" ]; then
        echo "    skip  $out"
    else
        echo "    fetch $out"
        curl -sL -o "$ART/$out" "$url"
    fi
}

dl "$SDK_RUN" "$SDK_URL"
dl zstd.deb "$ZSTD_URL"
dl libturbojpeg.deb "$TURBOJPEG_URL"
dl pyzed-3.8-cp36-cp36m-linux_aarch64.whl "$PYZED_URL"

# numpy + pip via pip3 download (handles PyPI)
if [ ! -s "$ART/$NUMPY_WHL" ]; then
    echo "    fetch numpy via pip3 download"
    pip3 download --no-deps --platform manylinux2014_aarch64 \
        --python-version 36 --abi cp36m --only-binary=:all: \
        numpy==1.19.5 -d "$ART/" >/dev/null
fi
if [ ! -s "$ART/$PIP_WHL" ]; then
    echo "    fetch pip via pip3 download"
    pip3 download pip==21.3.1 --no-deps -d "$ART/" >/dev/null
fi

echo
echo "==> [2/4] Reading host list from $CONFIG"
HOSTS_JSON=$(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
default_user = cfg.get('default_ssh_user', 'zed')
for h in cfg['hosts']:
    print(h['ip'], h.get('user', default_user))
")
echo "$HOSTS_JSON" | awk '{printf \"    %s  user=%s\\n\", \$1, \$2}'

echo
echo "==> [3/4] Pushing artifacts + scripts to each Jetson"
echo "$HOSTS_JSON" | while read ip user; do
    target=${user}@${ip}
    echo "    >> $target"
    ssh -o BatchMode=yes -o ConnectTimeout=5 \
        -o StrictHostKeyChecking=accept-new \
        "$target" "mkdir -p /tmp/zed-bootstrap"
    scp -q -o BatchMode=yes -o ConnectTimeout=10 \
        "$ART"/* \
        "$HERE/install_jetson.sh" \
        "$HERE/zed_recorder.py" \
        "$HERE/jetson_doctor.sh" \
        "$target:/tmp/zed-bootstrap/"
    # Also drop zed_recorder.py at the canonical /tmp location
    ssh -o BatchMode=yes "$target" \
        "cp /tmp/zed-bootstrap/zed_recorder.py /tmp/zed_recorder.py"
done

echo
echo "==> [4/4] Detecting connected ZED cameras and fetching their calibration"
echo "$HOSTS_JSON" | while read ip user; do
    target=${user}@${ip}
    sn=$(ssh -o BatchMode=yes "$target" \
        "OPENBLAS_CORETYPE=ARMV8 python3 -c '
import pyzed.sl as sl
cs = sl.Camera.get_device_list()
print(cs[0].serial_number if cs else \"\")
' 2>/dev/null" || echo "")
    if [ -z "$sn" ] || [ "$sn" = "" ]; then
        echo "    $ip  no camera detected (yet — install ZED SDK first)"
        continue
    fi
    conf="SN${sn}.conf"
    if [ ! -s "$ART/$conf" ]; then
        echo "    $ip  fetching calibration for SN $sn"
        curl -sL -o "$ART/$conf" "http://calib.stereolabs.com/?SN=${sn}"
    fi
    scp -q -o BatchMode=yes "$ART/$conf" "$target:/tmp/zed-bootstrap/"
done

echo
echo "==================================================================="
echo "Bootstrap done. Next steps (manual, INTERACTIVE for the SDK installer):"
echo
echo "  For each Jetson, SSH in and run:"
echo
echo '    ssh -t '"<user>@<ip>"' "bash /tmp/zed-bootstrap/install_jetson.sh"'
echo
echo "  Answer the SDK installer prompts as instructed (Y EULA, n static, n AI,"
echo "  Y MAXN, n samples, n auto-deps, Y Python API, exec=python3)."
echo
echo "  Once all Jetsons are installed, from this PC:"
echo
echo "    python3 orchestrator.py launch  --config $CONFIG"
echo "    python3 orchestrator.py ping    --config $CONFIG"
echo "    python3 orchestrator.py record  --config $CONFIG --duration 30 --label first_test"
echo "    python3 orchestrator.py pull    --config $CONFIG --local-dir ./svo"
echo "    python3 orchestrator.py analyze --local-dir ./svo"
echo "==================================================================="
