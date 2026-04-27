#!/usr/bin/env python3
"""
ZED Recorder service — runs on each Jetson.

Listens on TCP for line-delimited JSON commands and records SVO locally.

Protocol (each line is a JSON object terminated by \n) :
    > {"cmd": "PING"}
    < {"ok": true, "pong": true, "hostname": "jetson-cam1"}

    > {"cmd": "STATUS"}
    < {"ok": true, "state": "idle|recording", "current": {...}}

    > {"cmd": "START", "duration_s": 3600, "label": "patient_001"}
    < {"ok": true, "filename": "...", "serial": 22516499}

    > {"cmd": "STOP"}
    < {"ok": true, "stats": {"frames_grabbed": 107988, "frames_dropped": 0, ...}}

The recorder also writes a sidecar CSV next to each SVO with one row per frame
(idx, hardware_timestamp_ns, monotonic_ns, dropped_since_last_grab) so that drops
and inter-frame jitter can be analysed in post.

Usage on Jetson :
    python3 zed_recorder.py --output-dir /data/recordings --resolution HD1080 --fps 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pyzed.sl as sl

DEFAULT_PORT = 9999
DEFAULT_OUTPUT_DIR = "/data/recordings"


# ---------- Recorder ---------- #

class Recorder:
    def __init__(self, output_dir: Path, resolution: str, fps: int):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.resolution = resolution
        self.fps = fps

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.zed: sl.Camera | None = None
        self.state: str = "idle"   # idle | opening | recording | stopping
        self.current: dict = {}

    # -- camera open helper ---------------------------------------------------
    def _open_camera(self) -> sl.Camera:
        zed = sl.Camera()
        init = sl.InitParameters()
        init.camera_resolution = getattr(sl.RESOLUTION, self.resolution)
        init.camera_fps = self.fps
        init.depth_mode = sl.DEPTH_MODE.NONE          # RGB-only, no depth compute
        init.coordinate_units = sl.UNIT.MILLIMETER
        # Reduce memory and CPU pressure to maximise grab loop reliability
        init.sdk_verbose = 0
        err = zed.open(init)
        if err != sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f"Camera.open() failed: {err}")
        return zed

    # -- public API -----------------------------------------------------------
    def start(self, duration_s: float, label: str) -> dict:
        with self._lock:
            if self.state != "idle":
                raise RuntimeError(f"Cannot START: state={self.state}")
            self.state = "opening"

        try:
            zed = self._open_camera()
            info = zed.get_camera_information()
            serial = info.serial_number
            ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
            base = f"{label}_{serial}_{ts}"
            svo_path = self.output_dir / f"{base}.svo2"

            rec = sl.RecordingParameters(str(svo_path), sl.SVO_COMPRESSION_MODE.H265)
            err = zed.enable_recording(rec)
            if err != sl.ERROR_CODE.SUCCESS:
                zed.close()
                with self._lock:
                    self.state = "idle"
                raise RuntimeError(f"enable_recording failed: {err}")

            self.zed = zed
            self._stop_event.clear()
            self.current = {
                "filename": str(svo_path),
                "csv": str(svo_path.with_suffix(".timestamps.csv")),
                "serial": int(serial),
                "label": label,
                "resolution": self.resolution,
                "fps": self.fps,
                "start_unix_ns": time.time_ns(),
                "start_monotonic_ns": time.monotonic_ns(),
                "duration_s": duration_s,
                "frames_grabbed": 0,
                "frames_dropped": 0,
            }
            with self._lock:
                self.state = "recording"

            self._thread = threading.Thread(
                target=self._record_loop,
                args=(duration_s,),
                daemon=True,
                name="zed-record",
            )
            self._thread.start()

            return {
                "filename": str(svo_path),
                "serial": int(serial),
                "start_unix_ns": self.current["start_unix_ns"],
            }
        except Exception:
            with self._lock:
                self.state = "idle"
            raise

    def stop(self) -> dict:
        with self._lock:
            if self.state != "recording":
                return {"warning": f"not recording (state={self.state})", **self.current}
            self.state = "stopping"
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
        return dict(self.current)

    def status(self) -> dict:
        return {"state": self.state, "current": dict(self.current)}

    # -- internal record loop -------------------------------------------------
    def _record_loop(self, duration_s: float) -> None:
        assert self.zed is not None
        runtime = sl.RuntimeParameters()
        deadline = time.monotonic() + duration_s if duration_s > 0 else float("inf")

        csv_path = Path(self.current["csv"])
        try:
            with open(csv_path, "w", buffering=1) as f:
                f.write("frame_idx,hw_ts_ns,mono_ns,dropped_since_prev\n")
                idx = 0
                while not self._stop_event.is_set() and time.monotonic() < deadline:
                    err = self.zed.grab(runtime)
                    if err == sl.ERROR_CODE.SUCCESS:
                        try:
                            hw_ts = self.zed.get_timestamp(sl.TIME_REFERENCE.IMAGE).get_nanoseconds()
                        except Exception:
                            hw_ts = 0
                        try:
                            dropped = int(self.zed.get_frame_dropped_count())
                        except Exception:
                            dropped = 0
                        mono = time.monotonic_ns()
                        f.write(f"{idx},{hw_ts},{mono},{dropped}\n")
                        self.current["frames_grabbed"] = idx + 1
                        self.current["frames_dropped"] += dropped
                        idx += 1
                    else:
                        # grab error : log a row with idx=-1 so we keep a temporal trace
                        f.write(f"-1,0,{time.monotonic_ns()},{err}\n")
                        # Brief pause to avoid tight error loop
                        time.sleep(0.001)
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        if self.zed is not None:
            try:
                self.zed.disable_recording()
            except Exception:
                pass
            try:
                self.zed.close()
            except Exception:
                pass
            self.zed = None
        self.current["end_unix_ns"] = time.time_ns()
        with self._lock:
            self.state = "idle"


# ---------- TCP server ---------- #

def _send(f, obj: dict) -> None:
    f.write((json.dumps(obj) + "\n").encode())
    f.flush()


def _dispatch(msg: dict, recorder: Recorder) -> dict:
    cmd = msg.get("cmd", "").upper()
    if cmd == "PING":
        return {"ok": True, "pong": True, "hostname": socket.gethostname()}
    if cmd == "STATUS":
        return {"ok": True, **recorder.status()}
    if cmd == "START":
        info = recorder.start(
            duration_s=float(msg.get("duration_s", 3600)),
            label=str(msg.get("label", "test")),
        )
        return {"ok": True, **info}
    if cmd == "STOP":
        return {"ok": True, "stats": recorder.stop()}
    return {"ok": False, "error": f"unknown cmd: {cmd!r}"}


def _handle_client(conn: socket.socket, addr, recorder: Recorder) -> None:
    try:
        f = conn.makefile("rwb", buffering=0)
        for raw in f:
            try:
                msg = json.loads(raw.decode())
                resp = _dispatch(msg, recorder)
            except json.JSONDecodeError as exc:
                resp = {"ok": False, "error": f"bad json: {exc}"}
            except Exception as exc:
                resp = {"ok": False, "error": str(exc)}
            _send(f, resp)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(host: str, port: int, recorder: Recorder) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((host, port))
    s.listen(8)
    print(f"[zed_recorder] listening on {host}:{port}", flush=True)
    print(f"[zed_recorder] output dir: {recorder.output_dir}", flush=True)
    print(f"[zed_recorder] resolution={recorder.resolution} fps={recorder.fps}", flush=True)
    while True:
        conn, addr = s.accept()
        threading.Thread(
            target=_handle_client, args=(conn, addr, recorder), daemon=True
        ).start()


# ---------- CLI ---------- #

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="ZED Recorder service")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    p.add_argument("--resolution", default="HD1080",
                   choices=["HD2K", "HD1080", "HD720", "VGA"])
    p.add_argument("--fps", type=int, default=30, choices=[15, 30, 60, 100])
    args = p.parse_args(argv)

    if not args.output_dir.exists():
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(f"ERROR: cannot create {args.output_dir}. "
                  f"Run: sudo mkdir -p {args.output_dir} && sudo chown $USER {args.output_dir}",
                  file=sys.stderr)
            return 2

    rec = Recorder(args.output_dir, resolution=args.resolution, fps=args.fps)
    try:
        serve(args.host, args.port, rec)
    except KeyboardInterrupt:
        print("\n[zed_recorder] interrupted, exiting")
        if rec.state == "recording":
            rec.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
