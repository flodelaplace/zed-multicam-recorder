#!/usr/bin/env python3
"""
Convert SVO -> MP4 *locally* on the PC, in parallel, using ffmpeg with
hardware encoding (NVENC by default).

When the PC has pyzed + ffmpeg + an NVIDIA GPU, this is roughly an order of
magnitude faster than `orchestrator.py convert-mp4` (which runs a software
mp4v encoder on each Jetson Nano). Recommended path for production sessions.

Workflow :
    python3 orchestrator.py record    --duration 300 --label patient_01
    python3 orchestrator.py pull      --local-dir ./svo
    python3 convert_local.py          ./svo                       # this script
    python3 sync_align.py             ./svo --config config.json

Each input ``./svo/<cam>/.../<prefix>.svo`` produces a sibling
``<prefix>.mp4``. Idempotent (skips files whose .mp4 already exists, unless
``--force``).

Inspired by tools/convert_svo.py from
github.com/flodelaplace/zed-multicam-sync.
"""
import argparse
import os
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")  # harmless on x86

import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path


def _ffmpeg_writer(width, height, fps, out_path, codec, preset):
    """Spawn ffmpeg reading rawvideo BGR24 from stdin, encoding via codec."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-",
        "-c:v", codec,
        "-preset", preset,
        str(out_path),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def convert_one(svo_path, codec="h264_nvenc", preset="fast"):
    """Convert one SVO to MP4 next to it. Returns (mp4, n_frames, elapsed_s).

    Imports pyzed + cv2 lazily so the multiprocessing worker boots cleanly.
    """
    import pyzed.sl as sl
    import cv2

    svo_path = Path(svo_path)
    mp4 = svo_path.with_suffix(".mp4")

    zed = sl.Camera()
    init = sl.InitParameters()
    init.set_from_svo_file(str(svo_path))
    init.svo_real_time_mode = False
    init.depth_mode = sl.DEPTH_MODE.NONE
    err = zed.open(init)
    if err != sl.ERROR_CODE.SUCCESS:
        raise RuntimeError(f"open failed for {svo_path}: {err}")
    info = zed.get_camera_information()
    # SDK 5.x exposes resolution/fps under camera_configuration; SDK 3.x had
    # them as direct attributes. Handle both.
    if hasattr(info, "camera_configuration"):
        cfg = info.camera_configuration
        res = cfg.resolution
        fps = cfg.fps or 30
    else:
        res = info.camera_resolution
        fps = info.camera_fps or 30
    img = sl.Mat()

    t0 = time.monotonic()
    proc = _ffmpeg_writer(res.width, res.height, fps, mp4, codec, preset)
    n = 0
    try:
        while True:
            e = zed.grab()
            if e == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
                break
            if e != sl.ERROR_CODE.SUCCESS:
                continue
            zed.retrieve_image(img, sl.VIEW.LEFT)
            bgra = img.get_data()
            bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
            proc.stdin.write(bgr.tobytes())
            n += 1
    finally:
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        proc.wait()
        zed.close()
    return str(mp4), n, time.monotonic() - t0


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("local_dir",
                   help="Pulled local-dir with cam subdirs containing .svo files")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel SVO conversions (default: 4)")
    p.add_argument("--codec", default="libopenh264",
                   help="ffmpeg H.264 encoder. Pick the fastest one your "
                        "ffmpeg build supports. Common choices : "
                        "libopenh264 (software, universal, default), "
                        "libx264 (software, often faster than libopenh264), "
                        "h264_nvenc (NVIDIA GPU), "
                        "h264_qsv (Intel iGPU), "
                        "h264_videotoolbox (macOS), "
                        "h264_vaapi (Linux VAAPI).")
    p.add_argument("--preset", default="fast",
                   help="ffmpeg preset (default: fast)")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing .mp4 files")
    args = p.parse_args()

    local = Path(args.local_dir)
    if not local.exists():
        sys.exit(f"{local} does not exist")

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found in PATH. Install ffmpeg first.")

    try:
        import pyzed.sl  # noqa: F401  — sanity-check before forking workers
    except ImportError:
        sys.exit("pyzed not importable in this Python env. "
                 "Install ZED SDK + python API on the PC, or run "
                 "`orchestrator.py convert-mp4` (slower, runs on Jetsons).")

    svos = sorted(local.rglob("*.svo"))
    if not svos:
        sys.exit(f"No .svo found under {local}")

    todo = []
    for svo in svos:
        mp4 = svo.with_suffix(".mp4")
        if mp4.exists() and not args.force:
            print(f"  skip {mp4.name} (already exists, use --force to redo)")
            continue
        todo.append(svo)

    if not todo:
        print("Nothing to do.")
        return 0

    workers = min(args.workers, len(todo))
    print(f"Converting {len(todo)} SVO(s) with {workers} worker(s), "
          f"codec={args.codec}, preset={args.preset}")

    t_total = time.monotonic()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(convert_one, str(s), args.codec, args.preset): s
                for s in todo}
        for fut in as_completed(futs):
            svo = futs[fut]
            try:
                mp4, n, dt = fut.result()
                print(f"  OK  {Path(mp4).name}: {n} frames in {dt:.1f}s "
                      f"({n / dt:.1f} fps)")
            except Exception as exc:
                print(f"  ERR {svo.name}: {exc}", file=sys.stderr)

    print(f"done in {time.monotonic() - t_total:.1f}s wall")


if __name__ == "__main__":
    sys.exit(main() or 0)
