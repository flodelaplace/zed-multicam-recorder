#!/usr/bin/env python3
"""
ZED multicam orchestrator — fleet manager for distributed Jetson recorders.

Runs on the PC side (WSL2 / Linux / macOS). Talks to N Jetsons running
zed_recorder.py over TCP for command-and-control, and over SSH/SCP for
deployment, log collection, and SVO retrieval.

A typical session:

    # 0. One-off bootstrap to install ZED SDK + pyzed on each Jetson
    bash bootstrap.sh --config config.json

    # 1. Start recorder daemons on every host
    python3 orchestrator.py launch --config config.json

    # 2. Sanity check
    python3 orchestrator.py ping --config config.json
    python3 orchestrator.py list-cams --config config.json

    # 3. Run a 60 s synchronized recording
    python3 orchestrator.py record --config config.json \\
        --duration 60 --label patient_001 --resolution HD1080 --fps 30

    # 4. Pull SVOs back
    python3 orchestrator.py pull --config config.json --local-dir ./svo

    # 5. Analyze drops/fps from sidecar CSVs
    python3 orchestrator.py analyze --local-dir ./svo

    # 6. Cleanup remote recordings to free Jetson eMMC
    python3 orchestrator.py clean --config config.json

    # 7. Stop recorders
    python3 orchestrator.py kill --config config.json
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------- config ---------- #

def load_config(path):
    """Load and validate a fleet config file (JSON)."""
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("port", 9999)
    cfg.setdefault("remote_dir", "/tmp/recordings")
    cfg.setdefault("default_resolution", "HD1080")
    cfg.setdefault("default_fps", 30)
    cfg.setdefault("default_ssh_user", "zed")
    cfg.setdefault("openblas_armv8", True)
    if "hosts" not in cfg or not cfg["hosts"]:
        raise ValueError(f"config {path} must define a non-empty 'hosts' list")
    for h in cfg["hosts"]:
        if "ip" not in h:
            raise ValueError(f"each host needs 'ip' field, got {h!r}")
        h.setdefault("user", cfg["default_ssh_user"])
        h.setdefault("label", h["ip"])
    return cfg


def resolve_hosts(args, cfg):
    """If --hosts overrides config, build a synthetic hosts list."""
    if args.hosts:
        return [{"ip": ip, "user": cfg["default_ssh_user"], "label": ip}
                for ip in args.hosts]
    return cfg["hosts"]


# ---------- TCP client ---------- #

def send_cmd(ip, port, msg, timeout=5.0):
    with socket.create_connection((ip, port), timeout=timeout) as s:
        s.sendall((json.dumps(msg) + "\n").encode())
        f = s.makefile("rb")
        line = f.readline()
        if not line:
            raise RuntimeError("empty response")
        return json.loads(line.decode())


def parallel(hosts, port, msg, timeout=5.0):
    """Send the same message to all hosts in parallel. Returns dict[ip] = response."""
    results = {}
    if not hosts:
        return results
    with ThreadPoolExecutor(max_workers=len(hosts)) as ex:
        futs = {ex.submit(send_cmd, h["ip"], port, msg, timeout): h for h in hosts}
        for fut in as_completed(futs):
            h = futs[fut]
            try:
                results[h["ip"]] = fut.result()
            except Exception as exc:
                results[h["ip"]] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return results


# ---------- SSH helpers ---------- #

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new"]


def ssh_run(host, remote_cmd, capture=False):
    """Run a remote command over SSH. Returns CompletedProcess."""
    target = f"{host['user']}@{host['ip']}"
    cmd = ["ssh", *SSH_OPTS, target, remote_cmd]
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True)
    return subprocess.run(cmd)


def ssh_run_parallel(hosts, remote_cmd, capture=True):
    """Run the same remote command on all hosts in parallel. Returns dict[ip] = (rc, stdout, stderr)."""
    results = {}
    if not hosts:
        return results
    with ThreadPoolExecutor(max_workers=len(hosts)) as ex:
        futs = {ex.submit(ssh_run, h, remote_cmd, capture=True): h for h in hosts}
        for fut in as_completed(futs):
            h = futs[fut]
            r = fut.result()
            results[h["ip"]] = (r.returncode, r.stdout, r.stderr)
    return results


def scp_to(host, local_path, remote_path):
    target = f"{host['user']}@{host['ip']}:{remote_path}"
    return subprocess.run(["scp", *SSH_OPTS, str(local_path), target],
                          capture_output=True, text=True)


# ---------- pretty printing ---------- #

def _print_table(results, hosts):
    width = max((len(h["ip"]) for h in hosts), default=10)
    lwidth = max((len(h.get("label", "")) for h in hosts), default=0)
    for h in hosts:
        ip = h["ip"]
        label = h.get("label", "")
        r = results.get(ip, {"ok": False, "error": "no response"})
        marker = "OK " if r.get("ok") else "ERR"
        label_part = f"  {label:<{lwidth}}" if lwidth > 0 else ""
        print(f"  {marker}  {ip:<{width}}{label_part}  {json.dumps(r)}")


# ---------- subcommands ---------- #

def cmd_ping(args, cfg):
    hosts = resolve_hosts(args, cfg)
    res = parallel(hosts, cfg["port"], {"cmd": "PING"}, timeout=3)
    _print_table(res, hosts)
    return 0 if all(r.get("ok") for r in res.values()) else 1


def cmd_status(args, cfg):
    hosts = resolve_hosts(args, cfg)
    res = parallel(hosts, cfg["port"], {"cmd": "STATUS"}, timeout=3)
    _print_table(res, hosts)
    return 0


def cmd_list_cams(args, cfg):
    hosts = resolve_hosts(args, cfg)
    env = "OPENBLAS_CORETYPE=ARMV8 " if cfg["openblas_armv8"] else ""
    py = (env + "python3 -c '"
          "import pyzed.sl as sl; "
          "print([(c.serial_number, str(c.camera_state)) for c in sl.Camera.get_device_list()])"
          "' 2>&1")
    out = ssh_run_parallel(hosts, py)
    width = max(len(h["ip"]) for h in hosts)
    lwidth = max(len(h["label"]) for h in hosts)
    for h in hosts:
        rc, stdout, stderr = out.get(h["ip"], (1, "", "no response"))
        result = (stdout or stderr).strip().splitlines()[-1] if (stdout or stderr) else "(empty)"
        print(f"  {h['ip']:<{width}}  {h['label']:<{lwidth}}  {result}")
    return 0


def cmd_launch(args, cfg):
    hosts = resolve_hosts(args, cfg)
    # zed_recorder.py sets OPENBLAS_CORETYPE itself via os.environ.setdefault
    # before any numpy/pyzed import — no need to set it on the launch line
    # (and we cannot, since `setsid VAR=val cmd` interprets VAR=val as the
    # program name and fails).
    cmd = (
        f"mkdir -p {cfg['remote_dir']}; "
        f"fuser -k {cfg['port']}/tcp 2>/dev/null; sleep 0.5; "
        f"setsid python3 /tmp/zed_recorder.py "
        f"--output-dir {cfg['remote_dir']} "
        f"--resolution {args.resolution} --fps {args.fps} "
        f"--port {cfg['port']} "
        f"< /dev/null > /tmp/zed_recorder.log 2>&1 & "
        f"sleep 0.3"
    )
    print(f"[launch] starting recorders ({args.resolution} @ {args.fps} fps) on {len(hosts)} hosts")
    out = ssh_run_parallel(hosts, cmd)
    for h in hosts:
        rc, _, stderr = out.get(h["ip"], (1, "", "?"))
        marker = "OK " if rc == 0 else "ERR"
        print(f"  {marker}  {h['ip']:15}  {h['label']}")
    print(f"[launch] waiting {args.wait}s for recorders to bind")
    time.sleep(args.wait)
    res = parallel(hosts, cfg["port"], {"cmd": "PING"}, timeout=3)
    _print_table(res, hosts)
    return 0 if all(r.get("ok") for r in res.values()) else 1


def cmd_kill(args, cfg):
    hosts = resolve_hosts(args, cfg)
    out = ssh_run_parallel(hosts, f"fuser -k {cfg['port']}/tcp 2>/dev/null; echo killed")
    for h in hosts:
        rc, stdout, _ = out.get(h["ip"], (1, "", ""))
        print(f"  {h['ip']:15}  {h['label']:20}  {stdout.strip()}")
    return 0


def cmd_clean(args, cfg):
    hosts = resolve_hosts(args, cfg)
    if not args.yes:
        print(f"About to remove {cfg['remote_dir']}/* on {len(hosts)} hosts.")
        confirm = input("Type YES to proceed: ").strip()
        if confirm != "YES":
            print("aborted")
            return 1
    out = ssh_run_parallel(hosts, f"rm -rf {cfg['remote_dir']}/*; df -h / | tail -1 | awk '{{print $4 \" free\"}}'")
    for h in hosts:
        rc, stdout, _ = out.get(h["ip"], (1, "", ""))
        print(f"  {h['ip']:15}  {h['label']:20}  {stdout.strip()}")
    return 0


def cmd_record(args, cfg):
    hosts = resolve_hosts(args, cfg)
    # 1. Pre-flight
    print(f"[1/4] Pinging {len(hosts)} hosts ...")
    pings = parallel(hosts, cfg["port"], {"cmd": "PING"}, timeout=3)
    bad = [ip for ip, r in pings.items() if not r.get("ok")]
    if bad:
        print(f"  Hosts unreachable : {bad}", file=sys.stderr)
        if not args.force:
            print("  Aborting (use --force to record on the rest anyway).", file=sys.stderr)
            return 2
        hosts = [h for h in hosts if h["ip"] not in bad]
    print(f"  All {len(hosts)} hosts OK.")

    # 2. START in parallel
    msg = {"cmd": "START", "duration_s": args.duration, "label": args.label}
    print(f"[2/4] Sending START (duration_s={args.duration}, label={args.label!r}) ...")
    t0 = time.monotonic()
    starts = parallel(hosts, cfg["port"], msg, timeout=15)
    span_ms = (time.monotonic() - t0) * 1000
    bad = [ip for ip, r in starts.items() if not r.get("ok")]
    print(f"  All STARTs returned in {span_ms:.0f} ms.")
    for h in hosts:
        ip = h["ip"]
        r = starts.get(ip, {})
        marker = "OK " if r.get("ok") else "ERR"
        print(f"  {marker}  {ip:15}  {h['label']:20}  {json.dumps(r)}")
    if bad:
        print(f"  WARN: {len(bad)} hosts failed to start.", file=sys.stderr)

    # spread of unix_ns starts (only meaningful if NTP-synced)
    start_ns = [r.get("start_unix_ns") for r in starts.values()
                if r.get("ok") and r.get("start_unix_ns")]
    if len(start_ns) >= 2:
        spread_ms = (max(start_ns) - min(start_ns)) / 1e6
        if spread_ms < 5_000:
            print(f"  Start-time spread across hosts : {spread_ms:.1f} ms")
        else:
            print(f"  Start-time spread {spread_ms:.0f} ms (likely NTP-unsynced clocks; ignore)")

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
    print("[4/4] Sending STOP and collecting stats ...")
    stops = parallel(hosts, cfg["port"], {"cmd": "STOP"}, timeout=30)
    print()
    print("RESULTS  (note: 'sdk_drops' = SDK's get_frame_dropped_count(),")
    print("  unreliable. Run `analyze` after pull for real frame loss.)")
    print("-" * 96)
    for h in hosts:
        ip = h["ip"]
        r = stops.get(ip, {})
        s = r.get("stats", {})
        grabbed = s.get("frames_grabbed", "?")
        sdk_drops = s.get("frames_dropped", "?")
        delay = s.get("start_to_first_frame_ms")
        delay_s = f"{delay} ms" if delay is not None else "?"
        fname = s.get("filename", "?")
        print(f"  {ip:15}  {h['label']:20}  grabbed={grabbed}  "
              f"sdk_drops={sdk_drops}  start->1st_frame={delay_s}")
        print(f"                                            {fname}")
    print("-" * 96)

    # First-frame wall-clock spread across cams (NTP-dependent).
    # This is the headline sync metric: with NTP-synced Jetsons, it tells
    # you how much real wall-clock time passed between the cam that was
    # ready first and the cam that was ready last.
    first = [s.get("stats", {}).get("first_frame_unix_ns")
             for s in stops.values()
             if s.get("ok") and s.get("stats", {}).get("first_frame_unix_ns")]
    if len(first) >= 2:
        first_spread_ms = (max(first) - min(first)) / 1e6
        if first_spread_ms < 5_000:
            print(f"\nFirst-frame spread across cams : {first_spread_ms:.1f} ms"
                  f"  (the smaller, the better synchronised the cams are)")
        else:
            print(f"\nFirst-frame spread {first_spread_ms:.0f} ms — clocks not")
            print("  NTP-synchronised, this number is meaningless. Run setup_ntp.sh.")

    return 0


def cmd_stop(args, cfg):
    """Send STOP to every recorder and print returned stats. Useful to abort
    a record early without waiting for its --duration to elapse."""
    hosts = resolve_hosts(args, cfg)
    print(f"[stop] sending STOP to {len(hosts)} host(s)")
    stops = parallel(hosts, cfg["port"], {"cmd": "STOP"}, timeout=30)
    for h in hosts:
        ip = h["ip"]
        r = stops.get(ip, {})
        s = r.get("stats", {})
        warn = s.get("warning", "")
        grabbed = s.get("frames_grabbed", "?")
        fname = s.get("filename", "?")
        marker = "OK " if r.get("ok") else "ERR"
        suffix = f" ({warn})" if warn else ""
        print(f"  {marker}  {ip:15}  {h['label']:20}  grabbed={grabbed}{suffix}")
        if fname != "?":
            print(f"                                            {fname}")
    return 0


def cmd_pull(args, cfg):
    hosts = resolve_hosts(args, cfg)
    local = Path(args.local_dir)
    local.mkdir(parents=True, exist_ok=True)
    failures = 0
    for h in hosts:
        # The trailing /. tells scp to copy the *contents* of remote_dir
        # rather than remote_dir itself, so we get ./<label>/files.svo
        # rather than ./<label>/recordings/files.svo.
        target = f"{h['user']}@{h['ip']}:{cfg['remote_dir']}/."
        dst = local / h["label"].replace("/", "_")
        dst.mkdir(exist_ok=True)
        print(f"[pull] {target} -> {dst}")
        rc = subprocess.run(["scp", "-r", *SSH_OPTS, target, str(dst)]).returncode
        if rc != 0:
            print(f"  scp failed (rc={rc})", file=sys.stderr)
            failures += 1
    return 0 if failures == 0 else 1


def cmd_analyze(args, cfg):
    """Walk local-dir for *.timestamps.csv and report real fps / real misses
    based on hw_ts deltas (not the bogus SDK counter).

    Prints two sections:
      1. Per-cam summary table (events count, estimated missed frames, max gap)
      2. Per-cam freeze detail (when, how long, how many frames sautées)
    """
    local = Path(args.local_dir)
    if not local.exists():
        print(f"  {local} does not exist", file=sys.stderr)
        return 1
    rows = []
    freeze_details = []  # list of (cam_label, [freezes])
    for csv_path in sorted(local.rglob("*.timestamps.csv")):
        # idx,hw_ts,mono,drop  — keep idx alongside ts for the detail report
        rec = []
        with open(csv_path) as f:
            f.readline()  # header
            for line in f:
                parts = line.strip().split(",")
                if len(parts) != 4:
                    continue
                idx_s, ts_s, _, _ = parts
                if idx_s == "-1":
                    continue
                try:
                    idx = int(idx_s)
                    t = int(ts_s)
                except ValueError:
                    continue
                if t > 0:
                    rec.append((idx, t))
        if len(rec) < 2:
            continue

        first_ts = rec[0][1]
        intervals_ns = [rec[i + 1][1] - rec[i][1] for i in range(len(rec) - 1)]
        duration_s = (rec[-1][1] - first_ts) / 1e9
        avg_fps = len(rec) / duration_s if duration_s > 0 else 0
        # Median = robust estimate of expected frame period.
        sorted_iv = sorted(intervals_ns)
        median_ns = sorted_iv[len(sorted_iv) // 2]
        threshold_ns = int(median_ns * 1.5)

        # Per-freeze accounting.
        freezes = []
        frames_missed_total = 0
        for i, dt_ns in enumerate(intervals_ns):
            if dt_ns > threshold_ns:
                idx_before, ts_before = rec[i]
                gap_ms = dt_ns / 1e6
                # Estimated frames skipped : (gap / expected_period) - 1.
                n_skipped = max(1, int(round(dt_ns / median_ns)) - 1)
                t_since_start = (ts_before - first_ts) / 1e9
                freezes.append({
                    "t_s": round(t_since_start, 2),
                    "gap_ms": round(gap_ms, 1),
                    "after_frame": idx_before,
                    "frames_missed": n_skipped,
                })
                frames_missed_total += n_skipped

        expected_frames = int(round(duration_s * (1e9 / median_ns)))
        rows.append({
            "file": str(csv_path.relative_to(local)),
            "frames": len(rec),
            "duration_s": round(duration_s, 2),
            "avg_fps": round(avg_fps, 2),
            "events": len(freezes),
            "frames_missed": frames_missed_total,
            "loss_pct": round(100 * frames_missed_total / max(1, expected_frames), 3),
            "max_gap_ms": round(max(intervals_ns) / 1e6, 1),
        })
        # Derive a short cam label = the parent dir of the CSV.
        freeze_details.append((csv_path.parent.name, freezes))

    if not rows:
        print(f"No *.timestamps.csv found under {local}")
        return 1

    # 1. Summary table.
    cols = ["file", "frames", "duration_s", "avg_fps",
            "events", "frames_missed", "loss_pct", "max_gap_ms"]
    widths = {c: max(len(c), max(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
    print()

    total_missed = sum(r["frames_missed"] for r in rows)
    total_expected = sum(int(round(r["duration_s"] * r["avg_fps"])) for r in rows)
    print(f"Total frames missed across all cams : {total_missed} on "
          f"~{total_expected} expected ({100 * total_missed / max(1, total_expected):.3f}%)")
    print()

    # 2. Freeze detail per cam.
    any_freeze = any(freezes for _, freezes in freeze_details)
    if any_freeze:
        print("Freeze details (where the gaps actually happened):")
        for cam_label, freezes in freeze_details:
            if not freezes:
                print(f"  {cam_label}: 0 freeze")
                continue
            total = sum(fz["frames_missed"] for fz in freezes)
            print(f"  {cam_label}: {len(freezes)} freeze(s), "
                  f"~{total} frames missed total")
            for fz in freezes:
                print(f"      t={fz['t_s']:6.1f}s  "
                      f"gap={fz['gap_ms']:7.1f}ms  "
                      f"(~{fz['frames_missed']:3d} frames) "
                      f"after frame {fz['after_frame']}")
    else:
        print("No freezes detected on any cam.")
    return 0


def cmd_doctor(args, cfg):
    """Push and run jetson_doctor.sh on each host."""
    hosts = resolve_hosts(args, cfg)
    here = Path(__file__).resolve().parent / "jetson_doctor.sh"
    if not here.exists():
        print(f"jetson_doctor.sh not found next to orchestrator at {here}", file=sys.stderr)
        return 1
    for h in hosts:
        print(f"\n===== {h['ip']}  {h['label']} =====")
        rc = scp_to(h, here, "/tmp/jetson_doctor.sh").returncode
        if rc != 0:
            print(f"  scp failed (rc={rc})", file=sys.stderr)
            continue
        ssh_run(h, "bash /tmp/jetson_doctor.sh")
    return 0


def cmd_deploy_recorder(args, cfg):
    """SCP zed_recorder.py to /tmp on each host."""
    hosts = resolve_hosts(args, cfg)
    here = Path(__file__).resolve().parent / "zed_recorder.py"
    if not here.exists():
        print(f"zed_recorder.py not found next to orchestrator at {here}", file=sys.stderr)
        return 1
    failures = 0
    for h in hosts:
        rc = scp_to(h, here, "/tmp/zed_recorder.py").returncode
        marker = "OK " if rc == 0 else "ERR"
        print(f"  {marker}  {h['ip']:15}  {h['label']}")
        if rc != 0:
            failures += 1
    return 0 if failures == 0 else 1


# Inline pyzed converter that turns each remote .svo into a .mp4 (left cam
# only) using cv2.VideoWriter. Runs on the Jetson; that's where pyzed lives.
_REMOTE_CONVERT_PY = r"""
import os, sys, glob
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import pyzed.sl as sl
import cv2

REMOTE_DIR = sys.argv[1]

svos = sorted(glob.glob(os.path.join(REMOTE_DIR, "*.svo")))
if not svos:
    print("no svo found in", REMOTE_DIR); sys.exit(0)

for svo in svos:
    mp4 = os.path.splitext(svo)[0] + ".mp4"
    if os.path.exists(mp4) and os.path.getmtime(mp4) >= os.path.getmtime(svo):
        print("skip (mp4 up to date):", os.path.basename(mp4))
        continue
    print("convert ->", os.path.basename(mp4))
    zed = sl.Camera()
    init = sl.InitParameters()
    init.set_from_svo_file(svo)
    init.svo_real_time_mode = False
    init.depth_mode = sl.DEPTH_MODE.NONE
    err = zed.open(init)
    if err != sl.ERROR_CODE.SUCCESS:
        print("  open failed:", err); continue
    info = zed.get_camera_information()
    res = info.camera_resolution
    fps = info.camera_fps
    img = sl.Mat()
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(mp4, fourcc, float(fps), (res.width, res.height))
    n = 0
    while True:
        e = zed.grab()
        if e == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
            break
        if e != sl.ERROR_CODE.SUCCESS:
            continue
        zed.retrieve_image(img, sl.VIEW.LEFT)
        bgra = img.get_data()
        bgr = cv2.cvtColor(bgra, cv2.COLOR_BGRA2BGR)
        out.write(bgr)
        n += 1
    out.release()
    zed.close()
    print("  wrote", n, "frames")
print("done")
"""


def cmd_convert_mp4(args, cfg):
    """SVO -> MP4 conversion via pyzed on each Jetson, in-place in remote_dir.

    The .mp4 sits next to its .svo so that subsequent `pull` brings everything
    back together. Lossy re-encode on Jetson Nano takes ~real-time per camera
    (so 2 min recording = 2 min conversion)."""
    hosts = resolve_hosts(args, cfg)
    print(f"[convert-mp4] running pyzed converter on {len(hosts)} hosts in parallel")
    print("  (each conversion ~ real-time per camera duration)")

    def _one(host):
        target = f"{host['user']}@{host['ip']}"
        cmd = ("python3 -c '" + _REMOTE_CONVERT_PY.replace("'", "'\\''")
               + "' " + cfg["remote_dir"])
        result = subprocess.run(["ssh", *SSH_OPTS, target, cmd],
                                capture_output=True, text=True)
        return host, result

    results = {}
    with ThreadPoolExecutor(max_workers=len(hosts)) as ex:
        futs = {ex.submit(_one, h): h for h in hosts}
        for fut in as_completed(futs):
            host, result = fut.result()
            results[host["ip"]] = result

    for h in hosts:
        ip = h["ip"]
        r = results.get(ip)
        marker = "OK " if r and r.returncode == 0 else "ERR"
        print(f"  {marker}  {ip:15}  {h['label']}")
        if r and r.stdout:
            for line in r.stdout.strip().splitlines():
                print(f"        {line}")
        if r and r.returncode != 0 and r.stderr:
            for line in r.stderr.strip().splitlines()[-5:]:
                print(f"        ERR: {line}")
    return 0


def cmd_restart(args, cfg):
    """Redeploy zed_recorder.py and (re)start the daemon on each host.

    Useful after a Jetson reboot, /tmp wipe, or whenever pings start failing.
    Equivalent to: deploy-recorder + launch."""
    print("[restart 1/2] redeploying zed_recorder.py")
    cmd_deploy_recorder(args, cfg)
    print("\n[restart 2/2] launching daemons")
    if args.resolution is None:
        args.resolution = cfg["default_resolution"]
    if args.fps is None:
        args.fps = cfg["default_fps"]
    if not hasattr(args, "wait") or args.wait is None:
        args.wait = 4.0
    return cmd_launch(args, cfg)


# ---------- CLI ---------- #

def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.json",
                   help="Path to fleet config JSON (default: config.json)")
    p.add_argument("--hosts", nargs="+",
                   help="Override host IPs from config (uses default_ssh_user)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ping", help="Ping all recorders over TCP")
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("status", help="Get current state of each recorder")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("list-cams", help="List ZED cameras detected on each host")
    sp.set_defaults(func=cmd_list_cams)

    sp = sub.add_parser("launch", help="Start zed_recorder.py on each host (background)")
    sp.add_argument("--resolution", default=None,
                    choices=["HD2K", "HD1080", "HD720", "VGA"])
    sp.add_argument("--fps", type=int, default=None, choices=[15, 30, 60, 100])
    sp.add_argument("--wait", type=float, default=4.0,
                    help="Seconds to wait after launch for daemons to bind (default: 4)")
    sp.set_defaults(func=cmd_launch)

    sp = sub.add_parser("kill", help="Stop recorders on each host")
    sp.set_defaults(func=cmd_kill)

    sp = sub.add_parser("stop",
                        help="Send STOP to every recorder (abort an in-progress "
                             "recording without killing the daemons)")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("record", help="Synchronously record on all hosts")
    sp.add_argument("--duration", type=float, default=60,
                    help="Recording duration in seconds (0 = until ENTER)")
    sp.add_argument("--label", default="test",
                    help="Filename prefix label")
    sp.add_argument("--force", action="store_true",
                    help="Continue even if some hosts are unreachable")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("pull", help="SCP recordings back to local dir")
    sp.add_argument("--local-dir", default="./svo")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("clean", help="Remove remote recordings to free Jetson disk")
    sp.add_argument("--yes", action="store_true",
                    help="Skip confirmation prompt")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("analyze", help="Compute REAL fps/loss from sidecar CSVs (post pull)")
    sp.add_argument("--local-dir", default="./svo")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("doctor", help="Run jetson_doctor.sh on each host")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("deploy-recorder", help="Push zed_recorder.py to each host")
    sp.set_defaults(func=cmd_deploy_recorder)

    sp = sub.add_parser("convert-mp4",
                        help="Convert each SVO on each Jetson to MP4 (left cam) "
                             "via pyzed, ready for download by `pull`")
    sp.set_defaults(func=cmd_convert_mp4)

    sp = sub.add_parser("restart",
                        help="Redeploy zed_recorder.py + (re)start daemon on each host "
                             "(use after Jetson reboot or /tmp wipe)")
    sp.add_argument("--resolution", default=None,
                    choices=["HD2K", "HD1080", "HD720", "VGA"])
    sp.add_argument("--fps", type=int, default=None, choices=[15, 30, 60, 100])
    sp.add_argument("--wait", type=float, default=4.0)
    sp.set_defaults(func=cmd_restart)

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    # Apply config defaults to launch args
    if args.cmd == "launch":
        if args.resolution is None:
            args.resolution = cfg["default_resolution"]
        if args.fps is None:
            args.fps = cfg["default_fps"]
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
