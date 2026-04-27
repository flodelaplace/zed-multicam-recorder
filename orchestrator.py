#!/usr/bin/env python3
"""
PC-side orchestrator for multi-Jetson ZED recording.

Run from WSL2 (or any Linux/macOS box). Talks to N Jetsons running
``zed_recorder.py`` over TCP and pulls SVOs back over SCP.

Usage examples
--------------
    HOSTS="192.168.1.101 192.168.1.102 192.168.1.103 192.168.1.104"

    # Quick health check :
    python3 orchestrator.py ping --hosts $HOSTS

    # Status of each Jetson :
    python3 orchestrator.py status --hosts $HOSTS

    # Record 60 s on all Jetsons simultaneously :
    python3 orchestrator.py record --hosts $HOSTS --duration 60 --label test_01

    # Record until you press ENTER :
    python3 orchestrator.py record --hosts $HOSTS --duration 0 --label long_test

    # Pull all recordings back :
    python3 orchestrator.py pull --hosts $HOSTS --user nvidia --local-dir ./svo

    # Generate a quick CSV summary of frames / drops :
    python3 orchestrator.py report --local-dir ./svo
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DEFAULT_PORT = 9999
DEFAULT_USER = "nvidia"
DEFAULT_REMOTE_DIR = "/data/recordings"


# ---------- TCP client helpers ---------- #

def send_cmd(host: str, port: int, msg: dict, timeout: float = 5.0) -> dict:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(msg) + "\n").encode())
        f = s.makefile("rb")
        line = f.readline()
        if not line:
            raise RuntimeError("empty response")
        return json.loads(line.decode())


def parallel(hosts, port, msg, timeout=5.0):
    """Send the same message to all hosts in parallel. Returns dict[host] = response."""
    results = {}
    if not hosts:
        return results
    with ThreadPoolExecutor(max_workers=len(hosts)) as ex:
        futs = {ex.submit(send_cmd, h, port, msg, timeout): h for h in hosts}
        for fut in as_completed(futs):
            h = futs[fut]
            try:
                results[h] = fut.result()
            except Exception as exc:
                results[h] = {"ok": False, "error": str(exc)}
    return results


def _print_table(results: dict, header: str = "host") -> None:
    width = max((len(h) for h in results), default=10)
    print(f"{'HOST':<{width}}  RESPONSE")
    for h, r in sorted(results.items()):
        print(f"{h:<{width}}  {json.dumps(r)}")


# ---------- subcommands ---------- #

def cmd_ping(args) -> int:
    res = parallel(args.hosts, args.port, {"cmd": "PING"})
    _print_table(res)
    fail = sum(1 for r in res.values() if not r.get("ok"))
    return 1 if fail else 0


def cmd_status(args) -> int:
    res = parallel(args.hosts, args.port, {"cmd": "STATUS"})
    _print_table(res)
    return 0


def cmd_record(args) -> int:
    # 1. Pre-flight : ping all hosts
    print(f"[1/4] Pinging {len(args.hosts)} hosts ...")
    pings = parallel(args.hosts, args.port, {"cmd": "PING"})
    bad = [h for h, r in pings.items() if not r.get("ok")]
    if bad:
        print(f"  Hosts unreachable : {bad}", file=sys.stderr)
        if not args.force:
            print("  Aborting (use --force to record on the rest anyway).", file=sys.stderr)
            return 2
        args.hosts = [h for h in args.hosts if h not in bad]
    print(f"  All {len(args.hosts)} hosts OK.")

    # 2. START in parallel
    msg = {"cmd": "START", "duration_s": args.duration, "label": args.label}
    print(f"[2/4] Sending START (duration_s={args.duration}, label={args.label!r}) ...")
    t0 = time.monotonic()
    starts = parallel(args.hosts, args.port, msg, timeout=15)
    span_ms = (time.monotonic() - t0) * 1000
    bad = [h for h, r in starts.items() if not r.get("ok")]
    print(f"  All STARTs returned in {span_ms:.0f} ms.")
    for h, r in sorted(starts.items()):
        marker = "OK " if r.get("ok") else "ERR"
        print(f"  {marker} {h}  {r}")
    if bad:
        print(f"  WARN: {len(bad)} hosts failed to start.", file=sys.stderr)

    # Spread of start_unix_ns across hosts (rough drift estimate)
    start_ns = [r.get("start_unix_ns") for r in starts.values() if r.get("ok") and r.get("start_unix_ns")]
    if len(start_ns) >= 2:
        spread_ms = (max(start_ns) - min(start_ns)) / 1e6
        print(f"  Start-time spread across hosts : {spread_ms:.1f} ms")

    # 3. Wait
    if args.duration > 0:
        print(f"[3/4] Waiting {args.duration:.0f} s + 2 s margin ...")
        try:
            time.sleep(args.duration + 2)
        except KeyboardInterrupt:
            print("  Ctrl-C : sending STOP early ...")
    else:
        print("[3/4] Recording until ENTER ...")
        try:
            input()
        except KeyboardInterrupt:
            pass

    # 4. STOP
    print(f"[4/4] Sending STOP and collecting stats ...")
    stops = parallel(args.hosts, args.port, {"cmd": "STOP"}, timeout=30)
    print()
    print("RESULTS")
    print("-" * 80)
    expected = int(args.duration * 30) if args.duration > 0 else None  # rough, adjust if --fps changes
    total_dropped = 0
    for h, r in sorted(stops.items()):
        s = r.get("stats", {})
        grabbed = s.get("frames_grabbed", "?")
        dropped = s.get("frames_dropped", "?")
        fname = s.get("filename", "?")
        if isinstance(dropped, int):
            total_dropped += dropped
        print(f"  {h}  grabbed={grabbed}  dropped={dropped}  -> {fname}")
    print("-" * 80)
    if expected:
        print(f"  Expected ~{expected} frames per cam (at 30 fps)")
    print(f"  Total drops across all cams : {total_dropped}")
    return 0


def cmd_pull(args) -> int:
    local = Path(args.local_dir)
    local.mkdir(parents=True, exist_ok=True)
    failures = 0
    for h in args.hosts:
        dst = local / h.replace(".", "_")
        dst.mkdir(exist_ok=True)
        cmd = ["scp", "-r", "-o", "StrictHostKeyChecking=accept-new",
               f"{args.user}@{h}:{args.remote_dir}/", str(dst)]
        print("[pull]", " ".join(cmd))
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            print(f"  scp failed for {h} (rc={rc})", file=sys.stderr)
            failures += 1
    return 0 if failures == 0 else 1


def cmd_report(args) -> int:
    """Walk local-dir, find all *.timestamps.csv and summarise drops / framerate."""
    import csv
    local = Path(args.local_dir)
    if not local.exists():
        print(f"  {local} does not exist", file=sys.stderr)
        return 1
    rows = []
    for csv_path in sorted(local.rglob("*.timestamps.csv")):
        with open(csv_path) as f:
            r = csv.DictReader(f)
            data = list(r)
        if not data:
            continue
        valid = [d for d in data if d.get("frame_idx") and int(d["frame_idx"]) >= 0]
        n = len(valid)
        drops = sum(int(d["dropped_since_prev"]) for d in valid
                    if d.get("dropped_since_prev", "").isdigit())
        # Approx duration from first / last hw timestamp
        try:
            ts = [int(d["hw_ts_ns"]) for d in valid if d.get("hw_ts_ns")]
            duration_s = (max(ts) - min(ts)) / 1e9 if len(ts) >= 2 else 0
            fps_avg = (n / duration_s) if duration_s > 0 else 0
        except Exception:
            duration_s = 0
            fps_avg = 0
        rows.append({
            "file": str(csv_path.relative_to(local)),
            "frames": n,
            "drops_reported": drops,
            "duration_s": round(duration_s, 2),
            "avg_fps": round(fps_avg, 2),
        })
    if not rows:
        print("No timestamps.csv found.")
        return 1
    # Print table
    cols = ["file", "frames", "drops_reported", "duration_s", "avg_fps"]
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    return 0


# ---------- CLI ---------- #

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="PC-side orchestrator for multi-Jetson ZED recording")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ping")
    sp.add_argument("--hosts", nargs="+", required=True)
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("status")
    sp.add_argument("--hosts", nargs="+", required=True)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("record")
    sp.add_argument("--hosts", nargs="+", required=True)
    sp.add_argument("--duration", type=float, default=60,
                    help="Recording duration in seconds (0 = until ENTER)")
    sp.add_argument("--label", default="test")
    sp.add_argument("--force", action="store_true",
                    help="Continue even if some hosts are unreachable")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("pull")
    sp.add_argument("--hosts", nargs="+", required=True)
    sp.add_argument("--user", default=DEFAULT_USER)
    sp.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    sp.add_argument("--local-dir", default="./svo")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("report")
    sp.add_argument("--local-dir", default="./svo")
    sp.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
