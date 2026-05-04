#!/bin/bash
# install_jetson.sh — to be run on each Jetson, AFTER bootstrap.sh has pushed
# all required artifacts into /tmp/zed-bootstrap/.
#
# This script installs the ZED SDK + Python bindings (pyzed) + all required
# system libs without needing internet on the Jetson. The Stereolabs SDK
# installer is interactive — answer the prompts as instructed below.
#
# Usage on the Jetson:
#     bash /tmp/zed-bootstrap/install_jetson.sh
#
# Idempotent: safe to re-run if something failed mid-way.

set -e
ART=/tmp/zed-bootstrap

if [ ! -d "$ART" ]; then
    echo "ERROR: $ART not found. Run bootstrap.sh from the PC first."
    exit 1
fi

echo "==> Step 1/6  apt-installable .deb packages (zstd, libturbojpeg)"
for deb in zstd.deb libturbojpeg.deb; do
    if [ -f "$ART/$deb" ]; then
        echo "    installing $deb"
        sudo dpkg -i "$ART/$deb" || true
    fi
done

echo
echo "==> Step 2/6  pip + numpy + pyzed user-local"
if ! python3 -c "import pip" 2>/dev/null; then
    echo "    pip missing, bootstrapping from wheel"
    PYTHONPATH="$ART/pip-21.3.1-py3-none-any.whl" \
        python3 -m pip install --user --no-deps "$ART/pip-21.3.1-py3-none-any.whl"
fi
python3 -m pip install --user --no-deps \
    "$ART/numpy-1.19.5-cp36-cp36m-manylinux2014_aarch64.whl" \
    "$ART/pyzed-3.8-cp36-cp36m-linux_aarch64.whl"

echo
echo "==> Step 3/6  ZED SDK installer (INTERACTIVE)"
echo
echo "    The Stereolabs installer is about to run. Answer the prompts:"
echo "      EULA Accept                                   -> Y"
echo "      Static version of the ZED SDK                 -> n"
echo "      Install AI module (object detection, etc.)    -> n"
echo "      Maximum performance mode                      -> Y"
echo "      Install samples                               -> n"
echo "      Auto-install dependencies (apt)               -> n"
echo "      Install the Python API                        -> Y"
echo "      Python executable                             -> python3"
echo
echo "    NOTE: the installer will say 'CUDA detection failed' near the start."
echo "    That is harmless on JetPack 4.6 — CUDA is installed but not in PATH."
echo "    NOTE: the Python API install at the end will fail with a network"
echo "    error (no internet). That is expected — we already installed pyzed"
echo "    manually in step 2."
echo
SDK_RUN=$(ls -1 "$ART"/ZED_SDK_*.run 2>/dev/null | head -1)
if [ -z "$SDK_RUN" ]; then
    echo "ERROR: no ZED_SDK_*.run found in $ART"
    exit 1
fi
chmod +x "$SDK_RUN"
( cd /tmp && "$SDK_RUN" )

echo
echo "==> Step 4/6  Calibration files (per-camera)"
mkdir -p /usr/local/zed/settings 2>/dev/null || \
    sudo mkdir -p /usr/local/zed/settings
for conf in "$ART"/SN*.conf; do
    [ -f "$conf" ] || continue
    echo "    installing $(basename $conf)"
    cp "$conf" /usr/local/zed/settings/ 2>/dev/null || \
        sudo cp "$conf" /usr/local/zed/settings/
done

echo
echo "==> Step 5/6  Final verification"
echo "    cams detected by SDK :"
OPENBLAS_CORETYPE=ARMV8 python3 -c \
    'import pyzed.sl as sl; print([(c.serial_number, str(c.camera_state)) for c in sl.Camera.get_device_list()])'
echo "    pyzed import + SDK version :"
OPENBLAS_CORETYPE=ARMV8 python3 -c \
    'import pyzed.sl as sl; print("pyzed OK, SDK", sl.Camera().get_sdk_version())'

echo
echo "==> Step 6/6  Done."
echo "    Recorder is ready to be launched."
echo "    From the PC, run:    python3 orchestrator.py launch --config config.json"
