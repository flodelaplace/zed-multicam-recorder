#!/usr/bin/env python3
"""
Resample each cam's MP4 to a common wall-clock grid at fixed fps.

Inspired by the layout of github.com/flodelaplace/zed-multicam-sync, but
without the manual-visual-sync stage : NTP-synced clocks + the recorder's
first_frame_unix_ns sidecar give us absolute alignment for free.

Reads a local-dir produced by `orchestrator.py pull` *after* `convert-mp4`.
Each cam subdir must contain :
    <prefix>.mp4               left-cam video, one frame per successful grab
    <prefix>.timestamps.csv    frame_idx, hw_ts_ns, mono_ns, dropped_since_prev
    <prefix>.stats.json        first_frame_unix_ns, first_frame_hw_ts_ns, ...

Output (default to ./svo_aligned/) :
    <cam_label>/<cam_label>.aligned.mp4    exactly N frames @ args.fps,
                                           wall-clock-aligned across cams
    <cam_label>/<cam_label>.aligned.json   sidecar : black-frame indices,
                                           t_start, t_end, source_svo, ...
    global.json                            cross-cam summary

Frame n of any *.aligned.mp4 corresponds to wall-clock
``t_start_unix_ns + n / fps``. Frames listed in `black_frames` are synthetic
black images inserted because the source cam had a real gap there ; downstream
pose pipelines should skip / interpolate them.

Usage :
    python3 sync_align.py ./svo
    python3 sync_align.py ./svo --out-dir ./aligned --fps 30 --gap-threshold-ms 25

Requirements : Python 3.7+, opencv-python  (no ZED SDK needed on the PC).
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np


# ---------- per-cam loading ---------- #

def load_cam(subdir):
    """Returns (cam_dict, error_message). cam_dict is None on error."""
    subdir = Path(subdir)
    # rglob so we tolerate the nested ./svo/<cam>/recordings/* layout that
    # scp -r produces, in addition to a flat ./svo/<cam>/* layout.
    mp4 = next(iter(sorted(subdir.rglob("*.mp4"))), None)
    csv_path = next(iter(sorted(subdir.rglob("*.timestamps.csv"))), None)
    stats_path = next(iter(sorted(subdir.rglob("*.stats.json"))), None)
    if not (mp4 and csv_path and stats_path):
        miss = [n for p, n in
                [(mp4, "mp4"), (csv_path, "csv"), (stats_path, "stats.json")]
                if not p]
        return None, f"missing {miss}"

    stats = json.loads(stats_path.read_text())
    first_unix = stats.get("first_frame_unix_ns")
    first_hw = stats.get("first_frame_hw_ts_ns")
    if first_unix is None or first_hw is None:
        return None, "stats.json missing first_frame_*"

    walls = {}
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            try:
                idx = int(row["frame_idx"])
                hw_ts = int(row["hw_ts_ns"])
            except (ValueError, KeyError):
                continue
            if idx >= 0 and hw_ts > 0:
                walls[idx] = first_unix + (hw_ts - first_hw)
    if not walls:
        return None, "no usable frames in CSV"

    return {
        "label": subdir.name,
        "mp4_path": mp4,
        "csv_path": csv_path,
        "stats": stats,
        "walls": walls,
        "indices": sorted(walls.keys()),
    }, None


# ---------- per-cam resampling ---------- #

def _closest(walls_arr, t_ns):
    """Binary search : index in walls_arr (sorted) closest to t_ns."""
    lo, hi = 0, len(walls_arr) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if walls_arr[mid] < t_ns:
            lo = mid + 1
        else:
            hi = mid
    if lo > 0 and abs(walls_arr[lo - 1] - t_ns) < abs(walls_arr[lo] - t_ns):
        return lo - 1
    return lo


_ROTATE_CODES = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def align_cam(cam, grid, gap_thresh_ns, out_dir, fps, rotate=0):
    """Resample one cam to the common grid, writing <label>.aligned.mp4
    and returning the resampling stats dict.

    rotate: 0 / 90 / 180 / 270 degrees clockwise applied to every frame
    (and to the synthetic black frame) before writing."""
    if rotate not in (0, 90, 180, 270):
        return None, f"invalid rotate={rotate} (must be 0/90/180/270)"
    cap = cv2.VideoCapture(str(cam["mp4_path"]))
    if not cap.isOpened():
        return None, f"cannot open {cam['mp4_path']}"
    ret, first = cap.read()
    if not ret:
        cap.release()
        return None, "cannot read first frame"
    src_H, src_W = first.shape[:2]
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # rewind for sequential reads
    cur_mp4_idx = -1                     # index of last frame read from MP4
    cur_frame = None

    # Rotation swaps width/height for 90/270.
    if rotate in (90, 270):
        out_W, out_H = src_H, src_W
    else:
        out_W, out_H = src_W, src_H

    indices = cam["indices"]
    walls_arr = [cam["walls"][i] for i in indices]

    out_path = out_dir / cam["label"] / f"{cam['label']}.aligned.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(out_path), fourcc, float(fps), (out_W, out_H))
    if not out.isOpened():
        cap.release()
        return None, f"cannot open writer {out_path}"

    black = np.zeros((out_H, out_W, 3), dtype=np.uint8)
    black_frames = []

    for grid_n, T in enumerate(grid):
        pos = _closest(walls_arr, T)
        target_idx = indices[pos]
        gap = abs(walls_arr[pos] - T)

        if gap > gap_thresh_ns:
            out.write(black)
            black_frames.append(grid_n)
            continue

        # Forward-only sequential read (resampling is monotonic in time).
        # Fall back to a slow seek only if grid moves backward (rare).
        if target_idx < cur_mp4_idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
            cur_mp4_idx = target_idx - 1
            cur_frame = None
        while cur_mp4_idx < target_idx:
            ret, fr = cap.read()
            if not ret:
                break
            cur_mp4_idx += 1
            cur_frame = fr

        if cur_frame is None:
            out.write(black)
            black_frames.append(grid_n)
        else:
            if rotate in _ROTATE_CODES:
                frame_to_write = cv2.rotate(cur_frame, _ROTATE_CODES[rotate])
            else:
                frame_to_write = cur_frame
            out.write(frame_to_write)

    cap.release()
    out.release()
    return {
        "out_mp4": str(out_path),
        "n_frames": len(grid),
        "black_frames": black_frames,
        "black_count": len(black_frames),
        "width": out_W,
        "height": out_H,
        "rotate": rotate,
    }, None


# ---------- main ---------- #

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("local_dir", help="Pulled dir with one subdir per cam")
    p.add_argument("--out-dir", default="./svo_aligned",
                   help="Where to write aligned output (default: ./svo_aligned)")
    p.add_argument("--fps", type=int, default=30,
                   help="Target fps for the aligned grid (default: 30)")
    p.add_argument("--gap-threshold-ms", type=float, default=None,
                   help="Insert a black frame when a cam's nearest frame is more"
                        " than this far from the grid time. "
                        "Default: 0.75 / fps (= 25 ms at 30 fps).")
    p.add_argument("--config", default=None,
                   help="Optional path to fleet config JSON. If given, applies "
                        "per-cam `rotate` (0/90/180/270 deg clockwise) "
                        "matched by host label.")
    args = p.parse_args()

    rotate_by_label = {}
    if args.config:
        cfg = json.loads(Path(args.config).read_text())
        for h in cfg.get("hosts", []):
            rotate_by_label[h["label"]] = int(h.get("rotate", 0))

    local = Path(args.local_dir)
    if not local.exists():
        sys.exit(f"{local} does not exist")

    out_dir = Path(args.out_dir)

    # ---- Stage 1 : load cams ----
    print(f"Stage 1/4 : loading cams from {local}")
    cams = []
    for sub in sorted(local.iterdir()):
        if not sub.is_dir():
            continue
        cam, err = load_cam(sub)
        if cam is None:
            print(f"    skip {sub.name}: {err}", file=sys.stderr)
            continue
        cams.append(cam)
        dur = (max(cam["walls"].values()) - min(cam["walls"].values())) / 1e9
        print(f"    {cam['label']}: {len(cam['indices'])} frames, {dur:.2f}s")
    if not cams:
        sys.exit("No cams loaded.")

    # ---- Stage 2 : common wall-clock window + grid ----
    print()
    print("Stage 2/4 : common wall-clock window")
    starts = [min(c["walls"].values()) for c in cams]
    ends = [max(c["walls"].values()) for c in cams]
    t_start, t_end = max(starts), min(ends)
    if t_end <= t_start:
        sys.exit("Cameras do not overlap in wall-clock time.")
    duration_s = (t_end - t_start) / 1e9
    spread_start_ms = (max(starts) - min(starts)) / 1e6
    print(f"    first-frame spread across cams : {spread_start_ms:.1f} ms")
    print(f"    common window                  : {duration_s:.2f} s")

    step_ns = int(round(1e9 / args.fps))
    grid = list(range(t_start, t_end + 1, step_ns))
    n_grid = len(grid)

    gap_thresh_ns = (int(args.gap_threshold_ms * 1e6)
                     if args.gap_threshold_ms is not None
                     else int(step_ns * 0.75))
    print(f"    grid                           : {n_grid} frames @ {args.fps} fps")
    print(f"    gap threshold                  : {gap_thresh_ns / 1e6:.1f} ms")

    # ---- Stage 3 : resample each cam ----
    print()
    print("Stage 3/4 : resampling per cam")
    out_dir.mkdir(parents=True, exist_ok=True)
    cam_results = {}
    for cam in cams:
        rotate = rotate_by_label.get(cam["label"], 0)
        t0 = time.monotonic()
        res, err = align_cam(cam, grid, gap_thresh_ns, out_dir, args.fps,
                             rotate=rotate)
        dt = time.monotonic() - t0
        if err:
            print(f"    {cam['label']}: ERR {err}", file=sys.stderr)
            continue
        loss_pct = 100 * res["black_count"] / res["n_frames"]
        rot_s = f", rotated {rotate}°" if rotate else ""
        print(f"    {cam['label']}: {res['n_frames']} frames, "
              f"{res['black_count']} black ({loss_pct:.3f}%){rot_s}, "
              f"took {dt:.1f}s")
        cam_results[cam["label"]] = res

        # Per-cam sidecar
        sidecar = {
            "label": cam["label"],
            "fps": args.fps,
            "n_frames": res["n_frames"],
            "width": res["width"],
            "height": res["height"],
            "rotate": rotate,
            "t_start_unix_ns": t_start,
            "t_end_unix_ns": t_end,
            "duration_s": duration_s,
            "gap_threshold_ms": gap_thresh_ns / 1e6,
            "black_frames": res["black_frames"],
            "black_count": res["black_count"],
            "source_mp4": str(cam["mp4_path"].name),
            "first_frame_unix_ns": cam["stats"].get("first_frame_unix_ns"),
            "first_frame_hw_ts_ns": cam["stats"].get("first_frame_hw_ts_ns"),
            "serial": cam["stats"].get("serial"),
            "label_in_recorder": cam["stats"].get("label"),
        }
        (out_dir / cam["label"] / f"{cam['label']}.aligned.json").write_text(
            json.dumps(sidecar, indent=2))

    # ---- Stage 4 : global summary ----
    print()
    print("Stage 4/4 : global summary")
    summary = {
        "t_start_unix_ns": t_start,
        "t_end_unix_ns": t_end,
        "duration_s": duration_s,
        "fps": args.fps,
        "n_frames": n_grid,
        "first_frame_spread_ms": round(spread_start_ms, 3),
        "gap_threshold_ms": gap_thresh_ns / 1e6,
        "cams": [
            {
                "label": label,
                "black_count": r["black_count"],
                "loss_pct": round(100 * r["black_count"] / r["n_frames"], 4),
            }
            for label, r in cam_results.items()
        ],
    }
    (out_dir / "global.json").write_text(json.dumps(summary, indent=2))

    total_black = sum(r["black_count"] for r in cam_results.values())
    total_cells = n_grid * len(cam_results)
    print(f"\nDone. Output : {out_dir}/")
    print(f"  {len(cam_results)} aligned MP4(s) of {n_grid} frames each")
    print(f"  total black frames : {total_black} / {total_cells} "
          f"({100 * total_black / total_cells:.3f}%)")
    print(f"  see global.json + per-cam *.aligned.json for details")


if __name__ == "__main__":
    main()
