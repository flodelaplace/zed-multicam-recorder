#!/usr/bin/env python3
"""
Synchronised mosaic playback of N cameras, aligned by wall-clock.

Reads a local directory produced by `orchestrator.py pull` *after*
`orchestrator.py convert-mp4`. Each subdir contains :

    <prefix>.mp4               left-camera video, one frame per successful grab
    <prefix>.timestamps.csv    frame_idx, hw_ts_ns, mono_ns, dropped_since_prev
    <prefix>.stats.json        first_frame_unix_ns, first_frame_hw_ts_ns, ...

For each cam, every frame's wall-clock time is computed as :

    wall_clock_ns = first_frame_unix_ns + (hw_ts_ns - first_frame_hw_ts_ns)

The script then steps a global wall-clock cursor at 30 fps and, for each
camera, displays the frame whose wall-clock is closest to the cursor — so
visual events that occurred at the same instant of real time line up across
the panes, regardless of which camera was triggered first.

Usage :
    python3 playback.py ./svo
    python3 playback.py ./svo --fps 15 --scale 0.4

Controls :
    SPACE  pause / resume
    q      quit
    .  /  ,    step one frame forward / backward (when paused)
    s      print per-cam wall-clock spread for the current cursor

Requirements : Python 3.7+, opencv-python  (no ZED SDK needed on the PC).
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import cv2
import numpy as np


def _load_cam(subdir):
    """Read MP4 + CSV + stats. Returns dict or None if anything is missing."""
    # rglob so we tolerate the nested ./svo/<cam>/recordings/* layout that
    # scp -r produces, in addition to a flat ./svo/<cam>/* layout.
    mp4 = next(iter(sorted(subdir.rglob("*.mp4"))), None)
    csv_path = next(iter(sorted(subdir.rglob("*.timestamps.csv"))), None)
    stats_path = next(iter(sorted(subdir.rglob("*.stats.json"))), None)
    if not (mp4 and csv_path and stats_path):
        miss = [n for p, n in [(mp4, "mp4"), (csv_path, "csv"), (stats_path, "stats.json")] if not p]
        print(f"  skip {subdir.name}: missing {miss}", file=sys.stderr)
        return None

    stats = json.loads(stats_path.read_text())
    first_unix = stats.get("first_frame_unix_ns")
    first_hw = stats.get("first_frame_hw_ts_ns")
    if first_unix is None or first_hw is None:
        print(f"  skip {subdir.name}: stats.json missing first_frame_*",
              file=sys.stderr)
        return None

    # idx -> wall_ns. Skip error rows (idx == -1).
    walls = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                idx = int(row["frame_idx"])
                hw_ts = int(row["hw_ts_ns"])
            except (ValueError, KeyError):
                continue
            if idx < 0 or hw_ts <= 0:
                continue
            walls[idx] = first_unix + (hw_ts - first_hw)
    if not walls:
        print(f"  skip {subdir.name}: no usable frames in CSV", file=sys.stderr)
        return None

    return {
        "label": subdir.name,
        "mp4_path": mp4,
        "stats": stats,
        "walls": walls,                    # frame_idx -> wall_clock_ns
        "indices_sorted": sorted(walls.keys()),
        "cap": cv2.VideoCapture(str(mp4)),
        "cur_idx": -1,
        "cur_frame": None,
    }


def _seek_forward_to(cam, target_idx):
    """Read frames forward until cam.cur_idx == target_idx, return frame.

    If target_idx < cur_idx, fall back to set(CAP_PROP_POS_FRAMES) which is
    slow on H.264 but unavoidable for backward seeks."""
    cap = cam["cap"]
    if target_idx < cam["cur_idx"]:
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
        cam["cur_idx"] = target_idx - 1
    while cam["cur_idx"] < target_idx:
        ret, frame = cap.read()
        if not ret:
            break
        cam["cur_idx"] += 1
        cam["cur_frame"] = frame
    return cam["cur_frame"]


def _closest_idx(cam, t_ns):
    """Find the frame index whose wall-clock is closest to t_ns. O(log n)."""
    indices = cam["indices_sorted"]
    walls = cam["walls"]
    # bisect on wall-clock value (indices are sorted, walls sorted too if no jumps)
    lo, hi = 0, len(indices) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if walls[indices[mid]] < t_ns:
            lo = mid + 1
        else:
            hi = mid
    # lo is the first index with wall >= t_ns. Compare with previous.
    if lo > 0 and abs(walls[indices[lo - 1]] - t_ns) < abs(walls[indices[lo]] - t_ns):
        return indices[lo - 1]
    return indices[lo]


def _compose(frames, scale, layout):
    """Stack a list of frames into a mosaic. Layout is (rows, cols)."""
    rows, cols = layout
    if not frames:
        return np.zeros((100, 200, 3), dtype=np.uint8)
    h, w = frames[0].shape[:2]
    sw, sh = int(w * scale), int(h * scale)
    blank = np.zeros((sh, sw, 3), dtype=np.uint8)
    cells = [cv2.resize(f, (sw, sh)) if f is not None else blank.copy()
             for f in frames]
    while len(cells) < rows * cols:
        cells.append(blank.copy())
    grid_rows = []
    for r in range(rows):
        grid_rows.append(np.hstack(cells[r * cols:(r + 1) * cols]))
    return np.vstack(grid_rows)


def _layout_for(n):
    if n <= 1: return (1, 1)
    if n == 2: return (1, 2)
    if n <= 4: return (2, 2)
    if n <= 6: return (2, 3)
    return (3, 3)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("local_dir", help="Local-dir produced by `pull`")
    p.add_argument("--fps", type=int, default=30,
                   help="Playback target fps (default: 30)")
    p.add_argument("--scale", type=float, default=0.5,
                   help="Per-cam frame scale factor (default: 0.5)")
    p.add_argument("--save", default=None,
                   help="Also write the mosaic to this MP4 path while playing")
    p.add_argument("--no-display", action="store_true",
                   help="Skip the cv2 window — useful with --save in batch mode")
    args = p.parse_args()
    if args.no_display and not args.save:
        sys.exit("--no-display only makes sense with --save")

    local = Path(args.local_dir)
    if not local.exists():
        sys.exit(f"{local} does not exist")

    cams = []
    for sub in sorted(local.iterdir()):
        if sub.is_dir():
            cam = _load_cam(sub)
            if cam:
                cams.append(cam)
    if not cams:
        sys.exit("No cams loaded — make sure the dir has subdirs with "
                 "<prefix>.mp4 + <prefix>.timestamps.csv + <prefix>.stats.json")

    print(f"Loaded {len(cams)} cams: {[c['label'] for c in cams]}")
    layout = _layout_for(len(cams))

    # Common wall-clock range : start = max of per-cam first frames so each
    # cam has at least one frame at t_start; end = min of per-cam last frames.
    starts = [min(c["walls"].values()) for c in cams]
    ends = [max(c["walls"].values()) for c in cams]
    t_start, t_end = max(starts), min(ends)
    spread_start_ms = (max(starts) - min(starts)) / 1e6
    print(f"First-frame spread across cams : {spread_start_ms:.1f} ms")
    print(f"Common wall-clock range        : {(t_end - t_start) / 1e9:.2f} s")
    print()
    print("Controls : SPACE pause/resume, q quit, '.' / ',' step, 's' show spread")

    step_ns = int(1e9 / args.fps)
    t = t_start
    paused = False
    writer = None

    while t <= t_end:
        frames = []
        for c in cams:
            target = _closest_idx(c, t)
            frame = _seek_forward_to(c, target)
            if frame is None:
                frames.append(None)
                continue
            label = (f"{c['label']}  f{target}  "
                     f"+{(c['walls'][target] - t_start) / 1e6:.1f}ms")
            cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (0, 255, 0), 2)
            frames.append(frame)

        mosaic = _compose(frames, args.scale, layout)
        cur_s = (t - t_start) / 1e9
        cv2.putText(mosaic, f"t = {cur_s:6.3f} s   {len(cams)} cams",
                    (10, mosaic.shape[0] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        if args.save and writer is None:
            mh, mw = mosaic.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(args.save, fourcc, float(args.fps), (mw, mh))
            if not writer.isOpened():
                sys.exit(f"cannot open writer {args.save}")
            print(f"[playback] writing mosaic to {args.save}  ({mw}x{mh}@{args.fps})")
        if writer is not None:
            writer.write(mosaic)

        if not args.no_display:
            cv2.imshow("ZED multicam sync playback", mosaic)
            wait_ms = 0 if paused else max(1, int(1000 / args.fps))
            key = cv2.waitKey(wait_ms) & 0xFF
        else:
            key = 0  # no interaction in batch mode

        if key == ord('q'):
            break
        elif key == ord(' '):
            paused = not paused
        elif key == ord('.'):
            t += step_ns
        elif key == ord(','):
            t -= step_ns
        elif key == ord('s'):
            chosen = [c["walls"][_closest_idx(c, t)] for c in cams]
            sp = (max(chosen) - min(chosen)) / 1e6
            print(f"  t={cur_s:.3f}s  per-cam wall-clock spread = {sp:.2f} ms")
        elif not paused:
            t += step_ns

    for c in cams:
        c["cap"].release()
    if writer is not None:
        writer.release()
        print(f"[playback] saved mosaic to {args.save}")
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
