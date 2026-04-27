#!/bin/bash
# Run on each Jetson (Seeed reComputer J2021 / Xavier NX) to verify recording prerequisites.
# Bash 4+, Ubuntu 18.04 / 20.04 compatible.
#
# Usage: bash jetson_doctor.sh

set -u
PASS=0
FAIL=0

ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
warn() { echo "  [WARN] $*"; }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

hr()   { printf '%.0s-' {1..70}; echo; }

hr
echo "ZED Recorder — Jetson Doctor"
echo "Host: $(hostname)   Date: $(date -u +%FT%TZ)"
hr

echo "[1] Jetson model"
if [ -f /proc/device-tree/model ]; then
    MODEL=$(tr -d '\0' < /proc/device-tree/model)
    ok "Model: $MODEL"
else
    fail "/proc/device-tree/model not found — not a Jetson?"
fi
echo

echo "[2] L4T / JetPack version"
if [ -f /etc/nv_tegra_release ]; then
    head -1 /etc/nv_tegra_release
    ok "L4T release info present"
else
    warn "nv_tegra_release not found"
fi
echo

echo "[3] ZED SDK installation"
if [ -d /usr/local/zed ]; then
    ok "ZED SDK directory present at /usr/local/zed"
    if [ -x /usr/local/zed/tools/ZED_Diagnostic ]; then
        echo "    --- ZED_Diagnostic (first 25 lines) ---"
        /usr/local/zed/tools/ZED_Diagnostic 2>&1 | head -25 | sed 's/^/    /'
    fi
else
    fail "ZED SDK not at /usr/local/zed — install from https://www.stereolabs.com/developers/release/"
fi
echo

echo "[4] pyzed Python binding"
PY_OUT=$(python3 -c "import pyzed.sl as sl; c = sl.Camera(); print('pyzed OK, ZED SDK', c.get_sdk_version())" 2>&1)
if echo "$PY_OUT" | grep -q "pyzed OK"; then
    ok "$PY_OUT"
else
    fail "pyzed import failed: $PY_OUT"
    echo "    Try: cd /usr/local/zed && python3 get_python_api.py"
fi
echo

echo "[5] ZED2 USB detection"
USB_OUT=$(lsusb 2>/dev/null | grep -i -E 'stereolabs|2b03')
if [ -n "$USB_OUT" ]; then
    ok "ZED detected on USB:"
    echo "    $USB_OUT"
    # Check USB speed
    USB_SPEED=$(lsusb -t 2>/dev/null | grep -B1 -A1 -i "stereo\|video" | grep -oE '5000M|480M|10000M' | head -1)
    if [ "$USB_SPEED" = "5000M" ] || [ "$USB_SPEED" = "10000M" ]; then
        ok "USB speed = $USB_SPEED (USB 3.x, OK)"
    elif [ "$USB_SPEED" = "480M" ]; then
        fail "USB speed = $USB_SPEED (USB 2.0, TOO SLOW for ZED2 — change port)"
    fi
else
    fail "ZED2 not detected on USB. Check cable, replug."
fi
echo

echo "[6] Disk layout"
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT | sed 's/^/    /'
NVME_PRESENT=$(lsblk -d -n -o NAME | grep -c '^nvme' || true)
if [ "$NVME_PRESENT" -gt 0 ]; then
    ok "NVMe disk present"
    NVME_MNT=$(lsblk -n -o MOUNTPOINT $(lsblk -d -n -o PATH | grep nvme | head -1) 2>/dev/null | grep -v '^$' | head -1)
    if [ -n "$NVME_MNT" ]; then
        ok "NVMe mounted at $NVME_MNT"
    else
        warn "NVMe present but not mounted. To use it, format + mount + add to /etc/fstab"
    fi
else
    fail "No NVMe detected. Recording 1h SVO H.265 (~20 GB) on eMMC will fill the system disk."
fi
echo

echo "[7] Free space"
df -h / | sed 's/^/    /'
[ -d /data ] && df -h /data | sed 's/^/    /'
ROOT_FREE=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
if [ "$ROOT_FREE" -lt 50 ]; then
    warn "Less than 50 GB free on /. Recording target should NOT be /."
fi
DATA_FREE=$(df -BG --output=avail /data 2>/dev/null | tail -1 | tr -dc 0-9 || echo 0)
if [ "$DATA_FREE" -ge 100 ]; then
    ok "/data has ${DATA_FREE} GB free"
elif [ -d /data ]; then
    warn "/data has only ${DATA_FREE} GB free"
else
    warn "/data does not exist. Recommend: sudo mkdir /data && mount NVMe there."
fi
echo

echo "[8] Network"
ip -4 addr show | grep -E 'inet ' | grep -v '127.0.0.1' | sed 's/^/    /'
echo

echo "[9] Time / clock"
timedatectl status 2>/dev/null | head -8 | sed 's/^/    /'
NTP_ACTIVE=$(timedatectl status 2>/dev/null | grep -c "synchronized: yes" || true)
if [ "$NTP_ACTIVE" -gt 0 ]; then
    ok "System clock synchronized"
else
    warn "System clock not synchronized — for production setup, configure chrony with PTP master"
fi
echo

echo "[10] Recording prereqs in Python"
python3 - <<'PY' 2>&1 | sed 's/^/    /'
try:
    import pyzed.sl as sl
    z = sl.Camera()
    init = sl.InitParameters()
    init.camera_resolution = sl.RESOLUTION.HD1080
    init.camera_fps = 30
    init.depth_mode = sl.DEPTH_MODE.NONE
    err = z.open(init)
    if err == sl.ERROR_CODE.SUCCESS:
        info = z.get_camera_information()
        print("Camera open OK   serial:", info.serial_number, "  firmware:", info.camera_configuration.firmware_version if hasattr(info, 'camera_configuration') else "n/a")
        z.close()
    else:
        print("Camera open FAILED:", err)
except Exception as e:
    print("EXC:", e)
PY
echo

hr
echo "Summary: $PASS checks OK, $FAIL critical fails"
if [ "$FAIL" -eq 0 ]; then
    echo "READY. You can deploy zed_recorder.py."
    exit 0
else
    echo "FIX FAILS BEFORE RECORDING."
    exit 1
fi
