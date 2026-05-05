#!/bin/bash
# setup_ntp.sh — turn this PC into a local NTP server and configure each Jetson
# in the fleet config to sync to it via systemd-timesyncd.
#
# After this runs successfully, the wall-clock (CLOCK_REALTIME) of every
# Jetson is kept within ~10 ms of the PC's. That makes start_unix_ns and
# first_frame_unix_ns directly comparable across cameras for sync analysis.
#
# Usage:
#     bash setup_ntp.sh                         # config.json + autodetect PC IP
#     bash setup_ntp.sh --config foo.json
#     bash setup_ntp.sh --pc-ip 192.168.0.50    # override autodetect
#
# Idempotent. Safe to re-run.
#
# Requires sudo on the PC (apt install + chrony config) and sudo on each
# Jetson (edit /etc/systemd/timesyncd.conf). Run interactively.
#
# IMPORTANT — WSL2 PREREQS
# ------------------------
# If this PC is WSL2 with mirrored networking, port 123 is shared with
# Windows. Windows w32time service grabs port 123 and the Windows Defender
# Firewall blocks inbound UDP 123 by default. Before running this script,
# run BOTH commands below from PowerShell **as administrator** :
#
#   Stop-Service w32time
#   New-NetFirewallRule -DisplayName "WSL2 chrony NTP" \
#       -Direction Inbound -Protocol UDP -LocalPort 123 \
#       -Action Allow -Profile Any
#
# (To make the w32time stop persistent across Windows reboots:
#   Set-Service -Name w32time -StartupType Disabled )

set -e
HERE=$(cd "$(dirname "$0")" && pwd)
CONFIG=${CONFIG:-$HERE/config.json}
PC_IP=""

while [ $# -gt 0 ]; do
    case "$1" in
        --config)  CONFIG=$2; shift 2 ;;
        --pc-ip)   PC_IP=$2;  shift 2 ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found"
    exit 1
fi

# Detect WSL2 and warn if the Windows prereqs may not be in place.
IS_WSL=0
if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
    IS_WSL=1
fi

# Pull subnet from first host in config and find a matching local interface IP.
if [ -z "$PC_IP" ]; then
    FIRST_HOST_IP=$(python3 -c "
import json
print(json.load(open('$CONFIG'))['hosts'][0]['ip'])
")
    SUBNET=$(echo "$FIRST_HOST_IP" | awk -F. '{printf "%s.%s.%s.", $1, $2, $3}')
    PC_IP=$(ip -4 addr show 2>/dev/null \
        | awk -v s="$SUBNET" '$1=="inet" && index($2,s)==1 {split($2,a,"/"); print a[1]; exit}')
    if [ -z "$PC_IP" ]; then
        echo "ERROR: could not autodetect a PC IP on subnet ${SUBNET}0/24."
        echo "Pass it explicitly with --pc-ip 192.168.X.Y"
        exit 1
    fi
fi
echo "PC NTP server IP : $PC_IP"

ALLOW_SUBNET="${PC_IP%.*}.0/24"
echo "Allowed subnet   : $ALLOW_SUBNET"

if [ "$IS_WSL" = "1" ]; then
    echo
    echo "Detected WSL2 — make sure these PowerShell-admin commands have been run:"
    echo "    Stop-Service w32time"
    echo "    New-NetFirewallRule -DisplayName 'WSL2 chrony NTP' -Direction Inbound \\"
    echo "        -Protocol UDP -LocalPort 123 -Action Allow -Profile Any"
    echo
fi

# ---- 1. PC side : install + configure chrony as NTP server ---------------
echo "==> [PC] installing + configuring chrony"

if ! command -v chronyd >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y chrony
fi

CONF=/etc/chrony/chrony.conf
sudo cp -n "$CONF" "${CONF}.orig.$(date +%s)" 2>/dev/null || true

sudo tee "$CONF" > /dev/null <<EOF
# zed-multicam-recorder NTP server config (managed by setup_ntp.sh)

# Sync this server to public pools when internet is available.
pool pool.ntp.org iburst maxsources 4

# WSL2 only: PHC0 = the Hyper-V hardware clock. Gives chrony a stratum-1
# reference even when the public pool is unreachable. Harmless on non-WSL
# systems where /dev/ptp0 is absent (chrony just ignores it).
refclock PHC /dev/ptp0 poll 3 dpoll -2 offset 0 prefer

# Fallback if no upstream is reachable.
local stratum 10

# Bind to the LAN interface. On WSL2 this avoids fighting Windows for the
# 0.0.0.0:123 socket.
bindaddress ${PC_IP}

# Allow our LAN clients (Jetsons).
allow ${ALLOW_SUBNET}

# Standard.
driftfile /var/lib/chrony/chrony.drift
makestep 1.0 3
rtcsync
EOF

sudo systemctl enable --now chrony 2>/dev/null || true
sudo systemctl restart chrony
sleep 2

# Sanity check: is chrony actually serving on UDP 123 ?
if sudo ss -ulnp 2>/dev/null | grep -q "${PC_IP}:123.*chronyd"; then
    echo "==> [PC] chrony is bound on ${PC_IP}:123 OK"
else
    echo
    echo "ERROR: chrony failed to bind ${PC_IP}:123."
    if [ "$IS_WSL" = "1" ]; then
        echo "  Most likely cause on WSL2 : Windows w32time still owns the port,"
        echo "  or the Windows firewall blocks UDP 123 inbound. Re-run the two"
        echo "  PowerShell-admin commands listed at the top of this script and"
        echo "  re-run setup_ntp.sh."
    else
        echo "  Most likely cause : another NTP daemon (ntpd, openntpd) is bound"
        echo "  on this port. Stop it before re-running."
    fi
    exit 1
fi

echo
echo "==> [PC] chrony tracking:"
chronyc tracking 2>/dev/null | sed -n 's/^/    /;1,4p'
echo

# ---- 2. Jetson side : configure systemd-timesyncd ------------------------
echo "==> [Jetsons] configuring systemd-timesyncd to sync to ${PC_IP}"

# Read into an array so the inner ssh -t still has a real TTY for sudo.
mapfile -t HOST_LINES < <(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
default_user = cfg.get('default_ssh_user', 'zed')
for h in cfg['hosts']:
    print(h['ip'], h.get('user', default_user))
")

for entry in "${HOST_LINES[@]}"; do
    read -r ip user <<< "$entry"
    target="${user}@${ip}"
    echo
    echo ">> ${target}"
    ssh -t "$target" "sudo bash -c '
        if grep -q \"^NTP=\" /etc/systemd/timesyncd.conf; then
            sed -i \"s|^NTP=.*|NTP=${PC_IP}|\" /etc/systemd/timesyncd.conf
        elif grep -q \"^#NTP=\" /etc/systemd/timesyncd.conf; then
            sed -i \"s|^#NTP=.*|NTP=${PC_IP}|\" /etc/systemd/timesyncd.conf
        else
            echo \"NTP=${PC_IP}\" >> /etc/systemd/timesyncd.conf
        fi
        systemctl enable --now systemd-timesyncd 2>&1 | tail -1
        timedatectl set-ntp true
        systemctl restart systemd-timesyncd
        sleep 6
        timedatectl status | head -6
    '"
done

# ---- 3. Final verification : did the clocks actually sync ? --------------
echo
echo "==> [Verify] checking sync state on each Jetson"
sleep 5
ALL_OK=1
for entry in "${HOST_LINES[@]}"; do
    read -r ip user <<< "$entry"
    target="${user}@${ip}"
    line=$(ssh -o BatchMode=yes "$target" "timedatectl status | grep 'System clock synchronized'" 2>/dev/null | tr -d '\r')
    if echo "$line" | grep -q "yes"; then
        echo "    OK  $ip  $line"
    else
        echo "    KO  $ip  $line  -> check journalctl -u systemd-timesyncd"
        ALL_OK=0
    fi
done

echo
if [ "$ALL_OK" = "1" ]; then
    echo "==================================================================="
    echo "NTP setup complete. Every Jetson clock is now within ~10 ms of"
    echo "the PC's. Run a recording and the orchestrator will report a"
    echo "meaningful 'First-frame spread across cams' :"
    echo
    echo "    python3 orchestrator.py --config $CONFIG record \\"
    echo "        --duration 30 --label sync_check"
    echo "==================================================================="
else
    echo "Some Jetsons failed to sync. Wait 30 s and re-check via :"
    echo "    ssh <user>@<ip> 'timedatectl status | head -8'"
    echo "If still 'no', inspect journalctl -u systemd-timesyncd on that Jetson"
    echo "to see whether it can reach ${PC_IP}:123."
fi
