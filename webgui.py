#!/usr/bin/env python3
"""
webgui.py — dashboard web local de controle de la flotte ZED.

Zero dependance runtime cote serveur hormis `cryptography` (registre patient
chiffre) ; reutilise orchestrator.py pour la plomberie fleet.

    python3 webgui.py --config config.json [--port 8080] [--data data]
    -> http://localhost:8080

Fonctions : pre-check + voyant PRET, enregistrement DEBUT/FIN manuel par tache,
patients anonymises (code) + table de correspondance chiffree par mot de passe,
place disque par Jetson, extraction des videos vers le bon dossier, mini-stats.
"""
import argparse
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import orchestrator as orch
from patient_store import PatientStore

CFG = None
HOSTS = []
STORE = None
DATA_DIR = Path("data")
CURRENT = {"code": None, "task": None, "label": None, "started": None}
_LOCK = threading.Lock()

TASKS = {
    "calib":  "Calibration (marche dans tout l'espace)",
    "marche": "Marche (5 A/R 10m)",
    "sts5":   "Lever de chaise 5x5STS",
    "sts1":   "Lever de chaise 1min",
    "tug":    "TUG",
}
LOW_DISK_MB = 1024   # avertissement si moins d'1 Go libre


# ---------- fleet backend ---------- #

def _ping_status():
    # un seul round TCP STATUS : s'il repond, le daemon est up (le PING etait redondant)
    stats = orch.parallel(HOSTS, CFG["port"], {"cmd": "STATUS"}, timeout=3)
    out = {}
    for h in HOSTS:
        ip = h["ip"]; s = stats.get(ip, {})
        ok = isinstance(s, dict) and bool(s.get("ok"))
        cur = (s.get("current") or {}) if isinstance(s, dict) else {}
        out[ip] = {"daemon": ok,
                   "state": (s.get("state") if isinstance(s, dict) else None) or "?",
                   "frames": cur.get("frames_grabbed")}
    return out


def light_status():
    """Etat rapide SANS SSH (TCP STATUS seul) : pour le poll pendant l'enregistrement,
    afin de ne pas marteler les Jetson avec 12 connexions SSH toutes les 2 s en pleine capture."""
    ps = _ping_status()
    cams = [{"ip": h["ip"], "label": h["label"], "daemon": ps[h["ip"]]["daemon"],
             "state": ps[h["ip"]]["state"], "frames": ps[h["ip"]]["frames"]} for h in HOSTS]
    rec = sum(1 for c in cams if c["state"] == "recording")
    return {"cams": cams, "n": len(cams), "n_recording": rec, "ts": time.strftime("%H:%M:%S")}


def _ssh_checks():
    cmd = ("printf 'OFF=%s\\nFREE=%s\\nCAM=%s\\n' "
           "\"$(timeout 2 chronyc -n tracking 2>/dev/null | awk '/System time/{print $4}')\" "
           "\"$(df -m / | awk 'NR==2{print $4}')\" "
           "\"$(lsusb -d 2b03:f780 2>/dev/null | wc -l)\"")
    res = orch.ssh_run_parallel(HOSTS, cmd)
    out = {}
    for h in HOSTS:
        rc, so, se = res.get(h["ip"], (1, "", ""))
        d = {"offset_ms": None, "free_mb": None, "cam": None}
        for line in (so or "").splitlines():
            m = re.match(r"(OFF|FREE|CAM)=(.*)", line.strip())
            if not m:
                continue
            k, v = m.group(1), m.group(2).strip()
            try:
                if k == "OFF" and v:
                    d["offset_ms"] = abs(float(v)) * 1000.0
                elif k == "FREE" and v:
                    d["free_mb"] = int(v)
                elif k == "CAM" and v != "":
                    d["cam"] = int(v)
            except ValueError:
                pass
        out[h["ip"]] = d
    return out


def preflight():
    ps = _ping_status(); sc = _ssh_checks()
    cams = []
    for h in HOSTS:
        ip = h["ip"]; a = ps.get(ip, {}); b = sc.get(ip, {})
        reasons = []; verdict = "ok"
        if not a.get("daemon"):
            verdict = "fail"; reasons.append("daemon injoignable")
        if b.get("free_mb") is not None and b["free_mb"] < LOW_DISK_MB:
            verdict = "fail"; reasons.append("disque presque plein")
        if b.get("cam") is not None and b["cam"] == 0 and verdict != "fail":
            verdict = "warn"; reasons.append("ZED absente de l'USB (verifie le cable)")
        if b.get("offset_ms") is not None and b["offset_ms"] > 50 and verdict == "ok":
            verdict = "warn"; reasons.append("horloge %.0f ms" % b["offset_ms"])
        cams.append({"ip": ip, "label": h["label"], "daemon": a.get("daemon"),
                     "state": a.get("state"), "frames": a.get("frames"),
                     "offset_ms": b.get("offset_ms"), "free_mb": b.get("free_mb"),
                     "cam_seen": b.get("cam"), "verdict": verdict, "reason": ", ".join(reasons)})
    n_fail = sum(1 for c in cams if c["verdict"] == "fail")
    n_ok = sum(1 for c in cams if c["verdict"] == "ok")
    return {"cams": cams, "n": len(cams), "n_ok": n_ok, "n_fail": n_fail,
            "can_record": n_fail == 0, "ts": time.strftime("%H:%M:%S"),
            "current": dict(CURRENT)}


# ---------- record (manual start/stop) ---------- #

def do_start(task, resolution=None, fps=None):
    if STORE.locked():
        return {"error": "registre verrouille - deverrouille-le (mot de passe) puis re-selectionne le patient", "locked": True}
    if not CURRENT["code"]:
        return {"error": "selectionne d'abord un patient", "locked": False}
    if task not in TASKS:
        return {"error": "tache inconnue"}
    label = "%s_%s" % (CURRENT["code"], task)
    take_id = time.strftime("%Y%m%dT%H%M%S")   # partage par les 12 cams de cette prise
    resolution = resolution or CFG.get("default_resolution", "HD720")
    fps = int(fps or CFG.get("default_fps", 30))
    msg = {"cmd": "START", "duration_s": 0, "label": label, "take_id": take_id,
           "resolution": resolution, "fps": fps}  # 0 = jusqu'au STOP
    starts = orch.parallel(HOSTS, CFG["port"], msg, timeout=20)
    rows, start_ns = [], []
    for h in HOSTS:
        r = starts.get(h["ip"], {})
        rows.append({"ip": h["ip"], "label": h["label"], "ok": bool(r.get("ok")),
                     "error": r.get("error")})
        if r.get("ok") and r.get("start_unix_ns"):
            start_ns.append(r["start_unix_ns"])
    spread = (max(start_ns) - min(start_ns)) / 1e6 if len(start_ns) >= 2 else 0.0
    n_rec = sum(1 for r in rows if r["ok"])
    with _LOCK:
        CURRENT.update({"task": task, "label": label, "take_id": take_id,
                        "started": time.strftime("%H:%M:%S")})
    return {"rows": rows, "n": len(rows), "n_recording": n_rec,
            "all_ok": n_rec == len(rows), "spread_ms": round(spread, 1),
            "task": task, "task_label": TASKS[task],
            "resolution": resolution, "fps": fps}


def do_stop():
    orch.parallel(HOSTS, CFG["port"], {"cmd": "STOP"}, timeout=20)
    label = CURRENT.get("label"); take_id = CURRENT.get("take_id")
    trial = _trial_of(label, take_id) if (label and take_id) else 1
    stats = _compute_take_stats(CURRENT["code"], CURRENT["task"], label, take_id, trial) \
        if (label and take_id) else {"cams": [], "n": 0}
    stats.update({"label": label, "take_id": take_id, "trial": trial,
                  "code": CURRENT["code"], "task": CURRENT["task"]})
    return stats


def take_stats(label, take_id, trial=None):
    """Stats riches d'une prise deja enregistree (re-consultables depuis la liste)."""
    if not label or not _TAKE_ID_RE.match(str(take_id or "")):
        return {"error": "prise invalide"}
    try:
        trial = int(trial)
    except (TypeError, ValueError):
        trial = _trial_of(label, take_id)
    code, _, task = label.partition("_")
    st = _compute_take_stats(code, task, label, take_id, trial)
    st.update({"label": label, "take_id": take_id, "trial": trial, "code": code, "task": task})
    return st


def _drops_from_csv(path):
    """Retourne (duree_s, [ {t: offset_s, n: frames_manquantes}, ... ], nb_frames).
    Drops mesures comme dans `analyze` : intervalle hw_ts > 1.5x la mediane."""
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return 0.0, [], 0
    ts = []
    for ln in lines[1:]:                       # saute l'en-tete
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        try:
            ts.append(int(parts[1]))           # colonne hw_ts_ns
        except ValueError:
            continue
    if len(ts) < 3:
        return 0.0, [], len(ts)
    iv = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    med = sorted(iv)[len(iv) // 2]
    drops = []
    if med > 0:
        thr = 1.5 * med
        for i, v in enumerate(iv):
            if v > thr:
                n = int(round(float(v) / med)) - 1
                if n > 0:
                    drops.append({"t": round((ts[i] - ts[0]) / 1e9, 2), "n": n})
    return (ts[-1] - ts[0]) / 1e9, drops, len(ts)


def _compute_take_stats(code, task, label, take_id, trial=1):
    """Pull les CSV + stats.json (legers) de CETTE prise, calcule par cam :
    frames, duree, drops reels (+positions pour la timeline), et le dephasage de depart."""
    dest = _essai_dir(code, task, trial) / "raw_svo"
    dest.mkdir(parents=True, exist_ok=True)
    for ext in ("timestamps.csv", "stats.json"):
        _scp_from_all("%s/%s_*_%s.%s" % (CFG["remote_dir"], label, take_id, ext), dest)
    ser2host = {str(h.get("serial")): h for h in HOSTS}
    cams = []
    for statf in sorted(dest.glob("%s_*_%s.stats.json" % (label, take_id))):
        try:
            sj = json.loads(statf.read_text())
        except Exception:
            continue
        serial = str(sj.get("serial"))
        h = ser2host.get(serial, {})
        csvf = statf.parent / (statf.name[:-len(".stats.json")] + ".timestamps.csv")
        dur, drops, nframes = _drops_from_csv(csvf)
        frames = nframes or sj.get("frames_grabbed")
        cams.append({
            "serial": serial, "ip": h.get("ip"), "label": h.get("label", serial),
            "frames": frames, "duration_s": round(dur, 2),
            "drops": drops, "drop_count": sum(d["n"] for d in drops),
            "first_frame_unix_ns": sj.get("first_frame_unix_ns"),
            "open_ms": sj.get("start_to_first_frame_ms"),
        })
    # dephasage de depart (sync) : ecart des 1eres frames a l'horloge murale (NTP)
    ffns = [c["first_frame_unix_ns"] for c in cams if c.get("first_frame_unix_ns")]
    t0 = min(ffns) if ffns else 0
    for c in cams:
        ff = c.get("first_frame_unix_ns")
        c["offset_ms"] = round((ff - t0) / 1e6, 1) if ff else None
    spread_ms = round((max(ffns) - min(ffns)) / 1e6, 1) if len(ffns) >= 2 else 0.0
    tot_frames = sum(c["frames"] for c in cams if isinstance(c["frames"], int))
    tot_missed = sum(c["drop_count"] for c in cams)
    expected = tot_frames + tot_missed
    pct = round(100.0 * tot_missed / expected, 3) if expected else 0.0
    fr = [c["frames"] for c in cams if isinstance(c["frames"], int)]
    med = sorted(fr)[len(fr) // 2] if fr else 0
    stalled = [c["label"] for c in cams if isinstance(c["frames"], int) and med and c["frames"] < med - 30]
    max_dur = max((c["duration_s"] for c in cams), default=0.0)
    cams.sort(key=lambda c: str(c["label"]))
    return {"cams": cams, "n": len(cams), "max_duration_s": round(max_dur, 2),
            "totals": {"frames": tot_frames, "missed": tot_missed, "expected": expected, "pct": pct},
            "sync": {"spread_ms": spread_ms},
            "median_frames": med, "stalled": stalled}


def _scp_from_all(remote_glob, local_dir):
    def one(h):
        tgt = "%s@%s:%s" % (h["user"], h["ip"], remote_glob)
        return subprocess.run(["scp", *orch.SSH_OPTS, "-q", tgt, str(local_dir)],
                              capture_output=True, text=True)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as ex:
        list(ex.map(one, HOSTS))


_TAKE_ID_RE = re.compile(r"^\d{8}T\d{6}$")


def _essai_dir(code, task, trial):
    """data/<code>/<task>/essai_<N>/  — un dossier par prise (trial)."""
    base = (DATA_DIR / code / task) if task else (DATA_DIR / code)
    return base / ("essai_%d" % int(trial or 1))


def _trial_of(label, take_id):
    """Rang chronologique (1..N) de take_id parmi les prises de <label> sur la flotte."""
    res = orch.ssh_run_parallel(HOSTS, "ls %s/%s_*.svo 2>/dev/null || true" % (CFG["remote_dir"], label))
    tids = set()
    for h in HOSTS:
        _, so, _ = res.get(h["ip"], (1, "", ""))
        for ln in (so or "").splitlines():
            m = _REC_RE.match(Path(ln.strip()).name)
            if m and m.group(1) == label:
                tids.add(m.group(3))
    order = sorted(tids)
    return (order.index(take_id) + 1) if take_id in order else 1


def _take_extracted(label, take_id, trial):
    code, _, task = label.partition("_")
    dest = _essai_dir(code, task, trial) / "raw_svo"
    return dest.is_dir() and bool(list(dest.glob("%s_*_%s.svo" % (label, take_id))))


def _take_converted(label, take_id, trial):
    code, _, task = label.partition("_")
    return (_essai_dir(code, task, trial) / "raw_mp4" / "global.json").exists()


def _recording_now():
    """Labels des cams actuellement en enregistrement (pour bloquer une extraction prematuree)."""
    ps = _ping_status()
    return [h["label"] for h in HOSTS if ps.get(h["ip"], {}).get("state") == "recording"]


def pull_progress(label, take_id, trial):
    """Taille locale actuelle des fichiers de la prise (pour la barre de progression)."""
    if not label or not _TAKE_ID_RE.match(str(take_id or "")):
        return {"done_mb": 0}
    code, _, task = label.partition("_")
    dest = _essai_dir(code, task, int(trial or 1)) / "raw_svo"
    if not dest.is_dir():
        return {"done_mb": 0}
    tot = sum(f.stat().st_size for f in dest.glob("%s_*_%s.*" % (label, take_id)))
    return {"done_mb": round(tot / 1048576, 1)}


def do_pull(label=None, take_id=None, trial=None, check_recording=True):
    """Extrait UNE prise -> data/<code>/<task>/essai_<N>/raw_svo/."""
    label = label or CURRENT.get("label")
    take_id = take_id or CURRENT.get("take_id")
    if not label or not take_id or not _TAKE_ID_RE.match(str(take_id)):
        return {"error": "prise invalide (choisis-en une dans la liste)"}
    if check_recording:
        rec = _recording_now()
        if rec:
            return {"error": "enregistrement en cours (%s) — clique FIN avant d'extraire "
                    "(sinon tu copies un fichier incomplet)" % ", ".join(rec)}
    if trial is None:
        trial = _trial_of(label, take_id)
    code, _, task = label.partition("_")
    dest = _essai_dir(code, task, trial) / "raw_svo"
    dest.mkdir(parents=True, exist_ok=True)
    for ext in ("svo", "timestamps.csv", "stats.json"):
        _scp_from_all("%s/%s_*_%s.%s" % (CFG["remote_dir"], label, take_id, ext), dest)
    files = sorted(dest.glob("%s_*_%s.*" % (label, take_id)))
    total_mb = sum(f.stat().st_size for f in files) / 1048576
    svos = [f.name for f in files if f.suffix == ".svo"]
    return {"dest": str(dest), "n_svo": len(svos), "total_mb": round(total_mb, 1),
            "files": len(files), "trial": trial}


def pull_all():
    """Extrait toutes les PRISES presentes sur les Jetson pas encore extraites (skip le reste)."""
    rec = _recording_now()
    if rec:
        return {"error": "enregistrement en cours (%s) — clique FIN avant d'extraire" % ", ".join(rec)}
    recs = list_recordings().get("recordings", [])
    done, skipped, errors = [], [], []
    total_mb = 0.0
    total_svo = 0
    for r in recs:
        tag = "%s #%s" % (r["label"], r.get("trial", 1))
        if r.get("extracted"):
            skipped.append(tag); continue
        res = do_pull(r["label"], r["take_id"], r.get("trial"), check_recording=False)
        if res.get("error"):
            errors.append(tag)
        else:
            done.append(tag)
            total_mb += res.get("total_mb", 0)
            total_svo += res.get("n_svo", 0)
    return {"n_recordings": len(recs), "n_extracted": len(done), "n_skipped": len(skipped),
            "n_errors": len(errors), "extracted": done, "skipped": skipped, "errors": errors,
            "n_svo": total_svo, "total_mb": round(total_mb, 1)}


# ---------- conversion SVO -> MP4 (gauche) + resynchro (raw_svo / raw_mp4) ---------- #

# Converti sur la Jetson (pyzed y est). VIEW.LEFT = image RGB gauche SEULE (pas le stereo).
_CONVERT_PY = r'''
import os, sys, glob, subprocess
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import pyzed.sl as sl
import numpy as np
# pyzed decode -> frames brutes -> ffmpeg (H.264 libx264). Pas de dependance cv2
# (une Jetson ne l'a pas), et meilleure qualite/compression que le mp4v.
for svo in sorted(glob.glob(sys.argv[1])):
    mp4 = os.path.splitext(svo)[0] + ".mp4"
    if os.path.exists(mp4) and os.path.getmtime(mp4) >= os.path.getmtime(svo):
        print("skip", os.path.basename(mp4)); continue
    zed = sl.Camera(); init = sl.InitParameters()
    init.set_from_svo_file(svo); init.svo_real_time_mode = False
    init.depth_mode = sl.DEPTH_MODE.NONE
    if zed.open(init) != sl.ERROR_CODE.SUCCESS:
        print("openfail", os.path.basename(svo)); continue
    info = zed.get_camera_information(); res = info.camera_resolution
    fps = info.camera_fps or 30
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", "%dx%d" % (res.width, res.height), "-r", str(fps), "-i", "-",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", mp4], stdin=subprocess.PIPE)
    img = sl.Mat(); n = 0
    while True:
        e = zed.grab()
        if e == sl.ERROR_CODE.END_OF_SVOFILE_REACHED: break
        if e != sl.ERROR_CODE.SUCCESS: continue
        zed.retrieve_image(img, sl.VIEW.LEFT)          # gauche uniquement
        bgr = np.ascontiguousarray(img.get_data()[:, :, :3])   # BGRA -> BGR
        try:
            ff.stdin.write(bgr.tobytes())
        except (BrokenPipeError, IOError):
            break
        n += 1
    zed.close()
    try:
        ff.stdin.close(); ff.wait()
    except Exception:
        pass
    print("wrote", os.path.basename(mp4), n)
print("done")
'''

# etat de progression de la conversion+synchro en cours (pour la barre)
CONV = {"active": False, "label": None, "take_id": None, "stage": "", "n_cams": 0, "done": 0}


def convert_progress():
    return dict(CONV)


def convert_and_sync(label=None, take_id=None, trial=None):
    """SVO -> MP4 gauche (sur Jetson) + resynchro wall-clock + frames noires (sync_align).
    Produit data/<code>/<task>/essai_<N>/raw_svo/ et raw_mp4/."""
    import shutil
    label = label or CURRENT.get("label")
    take_id = take_id or CURRENT.get("take_id")
    if not label or not _TAKE_ID_RE.match(str(take_id or "")):
        return {"error": "prise invalide"}
    rec = _recording_now()
    if rec:
        return {"error": "enregistrement en cours (%s) — clique FIN d'abord" % ", ".join(rec)}
    if trial is None:
        trial = _trial_of(label, take_id)
    code, _, task = label.partition("_")
    essai = _essai_dir(code, task, trial)
    raw_svo = essai / "raw_svo"; raw_mp4 = essai / "raw_mp4"; work = essai / "_work"
    raw_svo.mkdir(parents=True, exist_ok=True)
    n_expected = len(HOSTS)
    CONV.update({"active": True, "label": label, "take_id": take_id,
                 "stage": "extraction des SVO", "n_cams": n_expected, "done": 0})
    try:
        # 1. SVO + CSV + stats -> raw_svo
        for ext in ("svo", "timestamps.csv", "stats.json"):
            _scp_from_all("%s/%s_*_%s.%s" % (CFG["remote_dir"], label, take_id, ext), raw_svo)
        # 2. conversion SVO->MP4 (gauche) sur les Jetson, puis pull des MP4
        CONV["stage"] = "conversion SVO->MP4 (gauche) sur les Jetson"
        pattern = "%s/%s_*_%s.svo" % (CFG["remote_dir"], label, take_id)
        py = "python3 -c '" + _CONVERT_PY.replace("'", "'\\''") + "' \"" + pattern + "\""
        orch.ssh_run_parallel(HOSTS, py)
        _scp_from_all("%s/%s_*_%s.mp4" % (CFG["remote_dir"], label, take_id), raw_svo)
        # 3. layout par cam pour sync_align (un sous-dossier par cam = son label)
        CONV["stage"] = "synchronisation + frames noires"
        if work.exists():
            shutil.rmtree(work)
        ser2label = {str(h.get("serial")): h.get("label", str(h.get("serial"))) for h in HOSTS}
        for mp4 in sorted(raw_svo.glob("%s_*_%s.mp4" % (label, take_id))):
            stem = mp4.name[:-4]
            m = re.match(r"^.+_(\d+)_%s$" % re.escape(take_id), stem)
            cl = ser2label.get(m.group(1) if m else "", stem)
            d = work / cl; d.mkdir(parents=True, exist_ok=True)
            for ext in (".mp4", ".timestamps.csv", ".stats.json"):
                src = raw_svo / (stem + ext)
                if src.exists():
                    (d / (stem + ext)).symlink_to(src.resolve())
        # 4. sync_align -> raw_mp4
        if raw_mp4.exists():
            shutil.rmtree(raw_mp4)
        r = subprocess.run(["python3", "sync_align.py", str(work), "--out-dir", str(raw_mp4),
                            "--fps", str(int(CFG.get("default_fps", 30))), "--config", "config.json"],
                           capture_output=True, text=True, timeout=1800)
        # 5. aplatir raw_mp4/<cl>/<cl>.aligned.mp4 -> raw_mp4/<cl>.mp4
        summary = {}
        gj = raw_mp4 / "global.json"
        if gj.exists():
            try:
                summary = json.loads(gj.read_text())
            except Exception:
                pass
        n_out = 0
        if raw_mp4.exists():
            for sub in sorted(raw_mp4.iterdir()):
                if not sub.is_dir():
                    continue
                for f in sub.glob("*.aligned.mp4"):
                    f.rename(raw_mp4 / (sub.name + ".mp4")); n_out += 1
                for f in sub.glob("*.aligned.json"):
                    f.rename(raw_mp4 / (sub.name + ".dropped.json"))
                shutil.rmtree(sub)
        shutil.rmtree(work, ignore_errors=True)
        if n_out == 0:
            return {"error": "synchro echouee: " + ((r.stderr or r.stdout or "")[-300:])}
        have_mp4 = set()
        for mp4 in raw_svo.glob("%s_*_%s.mp4" % (label, take_id)):
            mm = re.match(r"^.+_(\d+)_%s$" % re.escape(take_id), mp4.name[:-4])
            if mm:
                have_mp4.add(mm.group(1))
        missing = [h["label"] for h in HOSTS if str(h.get("serial")) not in have_mp4]
        total_black = sum(c.get("black_count", 0) for c in summary.get("cams", []))
        n_frames = summary.get("n_frames", 0) or 0
        cells = n_frames * n_out
        return {"ok": True, "n_cams": n_out, "raw_svo": str(raw_svo), "raw_mp4": str(raw_mp4),
                "duration_s": round(summary.get("duration_s", 0), 1), "trial": trial,
                "spread_ms": summary.get("first_frame_spread_ms"),
                "n_frames": n_frames, "total_black": total_black, "missing": missing,
                "black_pct": round(100.0 * total_black / cells, 3) if cells else 0.0}
    finally:
        CONV.update({"active": False, "stage": ""})


# ---------- gestion des enregistrements sur la flotte ---------- #

# nom de fichier : <code>_<task>_<serial>_<take_id>.svo  (take_id = YYYYMMDDThhmmss)
_REC_RE = re.compile(r"^(.+)_(\d+)_(\d{8}T\d{6})\.svo$")


def list_recordings():
    """Liste les enregistrements groupes PAR PRISE (une ligne par prise = trial),
    avec taille + duree, pour reperer un essai a jeter."""
    cmd = ("cd %s 2>/dev/null || exit 0; for f in *.svo; do [ -e \"$f\" ] || continue; "
           "sz=$(du -m \"$f\" 2>/dev/null | cut -f1); c=\"${f%%.svo}.timestamps.csv\"; "
           "n=0; [ -e \"$c\" ] && n=$(wc -l < \"$c\" 2>/dev/null); echo \"$f|$sz|$n\"; done"
           % CFG["remote_dir"])
    res = orch.ssh_run_parallel(HOSTS, cmd)
    fps = float(CFG.get("default_fps", 30)) or 30.0
    agg = {}   # (label, take_id) -> {...}
    for h in HOSTS:
        rc, so, se = res.get(h["ip"], (1, "", ""))
        for line in (so or "").splitlines():
            p = line.strip().split("|")
            if len(p) != 3:
                continue
            m = _REC_RE.match(p[0])
            if not m:
                continue
            try:
                mb = int(p[1]); nlines = int(p[2])
            except ValueError:
                continue
            label, take_id = m.group(1), m.group(3)
            frames = max(nlines - 1, 0)   # -1 pour l'en-tete du CSV
            a = agg.setdefault((label, take_id),
                               {"size_mb": 0, "cams": set(), "frames": []})
            a["size_mb"] += mb
            a["cams"].add(h["ip"])
            if frames:
                a["frames"].append(frames)
    # trial index par label (chronologique via take_id)
    takes_by_label = {}
    for (label, take_id) in agg:
        takes_by_label.setdefault(label, []).append(take_id)
    for label in takes_by_label:
        takes_by_label[label].sort()
    out = []
    for (label, take_id), a in agg.items():
        code, _, task = label.partition("_")
        med_frames = sorted(a["frames"])[len(a["frames"]) // 2] if a["frames"] else 0
        order = takes_by_label[label]
        trial = order.index(take_id) + 1
        hhmmss = take_id[9:11] + ":" + take_id[11:13] + ":" + take_id[13:15]
        out.append({"label": label, "code": code, "task": task, "take_id": take_id,
                    "trial": trial, "n_trials": len(order),
                    "size_mb": a["size_mb"], "n_cams": len(a["cams"]),
                    "duration_s": round(med_frames / fps, 1) if med_frames else 0,
                    "when": hhmmss, "extracted": _take_extracted(label, take_id, trial),
                    "converted": _take_converted(label, take_id, trial)})
    out.sort(key=lambda r: (r["label"], r["take_id"]))
    return {"recordings": out}


def delete_recording(label, take_id, force, trial=None):
    if not label or not re.match(r"^[A-Za-z0-9_]+$", label) or not _TAKE_ID_RE.match(str(take_id or "")):
        return {"error": "prise invalide"}
    if trial is None:
        trial = _trial_of(label, take_id)
    extracted = _take_extracted(label, take_id, trial)
    if not extracted and not force:
        return {"warning": "non_extrait", "extracted": False,
                "msg": "Cette prise n'a PAS encore été extraite vers ton PC. Supprimer quand même ?"}
    orch.ssh_run_parallel(HOSTS, "rm -f %s/%s_*_%s.* 2>/dev/null; echo ok"
                          % (CFG["remote_dir"], label, take_id))
    return {"ok": True, "deleted": "%s #%s" % (label, take_id), "extracted": extracted}


# ---------- actions par camera (depannage) ---------- #

def _host_by_ip(ip):
    for h in HOSTS:
        if h["ip"] == ip:
            return h
    return None


def cam_relaunch(ip):
    h = _host_by_ip(ip)
    if not h:
        return {"error": "ip inconnue"}
    orch.ssh_run(h, "sudo -n systemctl restart zed-recorder")
    time.sleep(3)
    try:
        pr = orch.send_cmd(ip, CFG["port"], {"cmd": "PING"}, timeout=4)
        return {"ok": bool(pr.get("ok")), "msg": "daemon relancé"}
    except Exception as e:
        return {"ok": False, "msg": "relancé mais pas de réponse (%s)" % e}


def cam_reboot_cam(ip):
    """Reboot firmware de la ZED2 par serial (sans replug physique)."""
    h = _host_by_ip(ip)
    if not h:
        return {"error": "ip inconnue"}
    sn = h.get("serial")
    if not sn:
        return {"error": "serial inconnu pour cette cam"}
    py = ("OPENBLAS_CORETYPE=ARMV8 python3 -c "
          "'import pyzed.sl as sl; sl.Camera.reboot(%d); print(\"reboot_sent\")'" % int(sn))
    r = orch.ssh_run(h, py, capture=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    ok = "reboot_sent" in out
    return {"ok": ok, "msg": ("reboot caméra envoyé — ré-énumération ~10-15 s"
                              if ok else "échec: " + out[-200:])}


def cam_reboot_jetson(ip):
    h = _host_by_ip(ip)
    if not h:
        return {"error": "ip inconnue"}
    orch.ssh_run(h, "sudo -n reboot")   # coupe la connexion
    return {"ok": True, "msg": "reboot Jetson lancé (~60 s). Le daemon redémarre tout seul (systemd)."}


def cam_logs(ip):
    h = _host_by_ip(ip)
    if not h:
        return {"error": "ip inconnue"}
    cmd = ("echo '=== recorder log (journalctl) ==='; "
           "sudo -n journalctl -u zed-recorder -n 25 --no-pager 2>/dev/null "
           "|| systemctl status zed-recorder --no-pager 2>/dev/null | tail -25; "
           "echo; echo '=== USB / dmesg recent ==='; "
           "dmesg 2>/dev/null | grep -iE 'usb|xhci|reset|uvc|2b03' | tail -15")
    r = orch.ssh_run(h, cmd, capture=True)
    return {"ip": ip, "label": h["label"], "log": (r.stdout or r.stderr or "(vide)")}


def cam_fix_usb(ip):
    """Reset du controleur tegra-xusb (repare un lien video SuperSpeed coince :
    IMU f781 presente mais video f780 absente). Pas de replug physique."""
    h = _host_by_ip(ip)
    if not h:
        return {"error": "ip inconnue"}
    cmd = ("echo -n 3610000.xhci | sudo -n tee /sys/bus/platform/drivers/tegra-xusb/unbind >/dev/null 2>&1; "
           "sleep 3; echo -n 3610000.xhci | sudo -n tee /sys/bus/platform/drivers/tegra-xusb/bind >/dev/null 2>&1; "
           "sleep 6; lsusb -d 2b03:f780 2>/dev/null | wc -l")
    r = orch.ssh_run(h, cmd, capture=True)
    n = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else "0"
    ok = n == "1"
    return {"ok": ok, "msg": ("USB reparé — vidéo (f780) de nouveau visible"
                              if ok else "toujours pas de vidéo — vérifie le câble ou reboote la caméra")}


def preview():
    """Grab une vignette gauche de chaque cam (commande GRAB du recorder)."""
    res = orch.parallel(HOSTS, CFG["port"], {"cmd": "GRAB"}, timeout=30)
    out = []
    for h in HOSTS:
        r = res.get(h["ip"], {})
        d = r if isinstance(r, dict) else {}
        out.append({"ip": h["ip"], "label": h["label"], "rotate": int(h.get("rotate", 0) or 0),
                    "ok": bool(d.get("ok")), "jpg": d.get("jpg"), "error": d.get("error")})
    return {"cams": out, "n": len(out), "n_ok": sum(1 for c in out if c["ok"]),
            "ts": time.strftime("%H:%M:%S")}


def fleet_restart():
    """Redeploie zed_recorder.py dans /opt/zedrec + relance le service systemd sur les 12.
    Le service (Restart=always, enable au boot) rend ceci rarement necessaire, mais
    ce bouton force un redeploy propre du script + un restart apres un rebranchement."""
    from concurrent.futures import ThreadPoolExecutor
    zr = str(Path(__file__).resolve().parent / "zed_recorder.py")

    def deploy(h):
        subprocess.run(["scp", *orch.SSH_OPTS, "-q", zr,
                        "%s@%s:/opt/zedrec/zed_recorder.py" % (h["user"], h["ip"])],
                       capture_output=True, timeout=30)
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as ex:
        list(ex.map(deploy, HOSTS))
    orch.ssh_run_parallel(HOSTS, "sudo -n systemctl restart zed-recorder")
    time.sleep(5)
    pings = orch.parallel(HOSTS, CFG["port"], {"cmd": "PING"}, timeout=4)
    n = sum(1 for r in pings.values() if isinstance(r, dict) and r.get("ok"))
    return {"ok": n == len(HOSTS), "n_up": n, "n": len(HOSTS)}


# ---------- bridge calibration extrinseque (repo externe, config-driven) ---------- #

CALIB = {"active": False, "label": None, "trial": None, "stage": "", "log": "",
         "done": False, "ok": False, "output": None, "mre": None}


def _zed_intrinsics(host, section="LEFT_CAM_HD"):
    """Lit fx,fy,cx,cy,k1,k2,p1,p2 depuis /usr/local/zed/settings/SN<serial>.conf du Jetson."""
    sn = host.get("serial")
    if not sn:
        return None
    cmd = "sed -n '/\\[%s\\]/,/^\\[/p' /usr/local/zed/settings/SN%s.conf 2>/dev/null" % (section, sn)
    r = orch.ssh_run(host, cmd, capture=True)
    vals = {}
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("["):
            k, _, v = line.partition("=")
            try:
                vals[k.strip()] = float(v.strip())
            except ValueError:
                pass
    return vals if all(k in vals for k in ("fx", "fy", "cx", "cy")) else None


def _build_calib_scene_toml(raw_mp4_dir, toml_path):
    """Genere un Calib_scene.toml (format Pose2Sim) pour les cams presentes dans raw_mp4_dir,
    depuis la calib usine ZED (SN.conf). Sections nommees par label (= nom du .mp4).
    Retourne (n_ok, missing). NB: suppose HD720 non tourne (rotate=0)."""
    from concurrent.futures import ThreadPoolExecutor
    label2host = {h.get("label"): h for h in HOSTS}
    cams = sorted(p.stem for p in Path(raw_mp4_dir).glob("*.mp4"))
    W, H = 1280.0, 720.0

    def one(cl):
        h = label2host.get(cl)
        return cl, (_zed_intrinsics(h) if h else None)
    with ThreadPoolExecutor(max_workers=max(1, len(cams))) as ex:
        intr = dict(ex.map(one, cams))
    lines, ok, missing = [], 0, []
    for cl in cams:
        v = intr.get(cl)
        if not v:
            missing.append(cl); continue
        rot = int((label2host.get(cl) or {}).get("rotate", 0) or 0)
        # sync_align tourne le MP4 -> on tourne les intrinseques a l'identique
        # (convention de utils/convert_calib_rotation.py : 90=cw, 270=ccw)
        fx, fy = v["fx"], v["fy"]; cx, cy = v["cx"], v["cy"]
        k1, k2 = v.get("k1", 0.0), v.get("k2", 0.0); p1, p2 = v.get("p1", 0.0), v.get("p2", 0.0)
        w, h = W, H
        for d in ({90: ["cw"], 270: ["ccw"], 180: ["cw", "cw"]}.get(rot, [])):
            fx, fy = fy, fx
            if d == "cw":
                cx, cy = h - cy, cx; p1, p2 = p2, -p1
            else:
                cx, cy = cy, w - cx; p1, p2 = -p2, p1
            w, h = h, w
        lines += [
            '[%s]' % cl,
            'name = "%s"' % cl,
            'size = [ %.1f, %.1f]' % (w, h),
            'matrix = [ [ %r, 0.0, %r], [ 0.0, %r, %r], [ 0.0, 0.0, 1.0]]' % (fx, cx, fy, cy),
            'distortions = [ %r, %r, %r, %r]' % (k1, k2, p1, p2),
            'rotation = [ 0.0, 0.0, 0.0]',
            'translation = [ 0.0, 0.0, 0.0]',
            'fisheye = false', '']
        ok += 1
    lines += ['[metadata]', 'adjusted = false', 'error = 0.0']
    Path(toml_path).write_text("\n".join(lines) + "\n")
    return ok, missing


def _tail(path, n):
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def _parse_mre(logf):
    """Recupere la meilleure MRE affichee par le pipeline (best line, souvent avec '*')."""
    best = None
    for ln in _tail(logf, 400).splitlines():
        m = re.search(r"([Mm]RE|reproj\w*)\D{0,20}?([0-9]+\.[0-9]+)\s*px", ln)
        if m:
            v = float(m.group(2))
            if best is None or "*" in ln or v < best:
                best = v
    return best


def calib_status():
    return dict(CALIB)


def run_calibration(label=None, take_id=None, trial=None, height=None, ref_frame=None):
    """Lance le repo de calibration externe (config-driven) en fond sur essai_<N>/raw_mp4/."""
    if CALIB.get("active"):
        return {"error": "une calibration tourne deja"}
    ccfg = CFG.get("calibration") or {}
    repo = ccfg.get("repo")
    if not repo or not Path(repo).is_dir():
        return {"error": "repo de calibration introuvable (config.calibration.repo)"}
    label = label or CURRENT.get("label")
    take_id = take_id or CURRENT.get("take_id")
    if not label or not _TAKE_ID_RE.match(str(take_id or "")):
        return {"error": "prise invalide"}
    if trial is None:
        trial = _trial_of(label, take_id)
    code, _, task = label.partition("_")
    essai = _essai_dir(code, task, trial)
    raw_mp4 = essai / "raw_mp4"
    if not (raw_mp4 / "global.json").exists():
        return {"error": "convertis d'abord la prise (🎬 MP4 synchro) — raw_mp4 manquant"}
    toml = essai / "Calib_scene.toml"
    n_ok, missing = _build_calib_scene_toml(raw_mp4, toml)
    if n_ok < 2:
        return {"error": "intrinseques insuffisantes (%d cam)" % n_ok}
    out = essai / "calib_output"
    out.mkdir(parents=True, exist_ok=True)
    conda = os.path.expanduser(ccfg.get("conda_bin", "~/miniconda3/bin/conda"))
    cmd = [conda, "run", "--no-capture-output", "-n", ccfg.get("conda_env", "human_calib"),
           "bash", ccfg.get("script", "scripts/calibrate.sh"),
           str(raw_mp4.resolve()), str(toml.resolve()), str(out.resolve()),
           ccfg.get("device", "cuda"), ccfg.get("mode", "balanced"),
           "--pose_engine", ccfg.get("pose_engine", "metrabs"),
           "--frame_skip", str(ccfg.get("frame_skip", 5))]
    if height:
        cmd += ["--height", str(height)]
    if ref_frame not in (None, ""):
        cmd += ["--ref_frame", str(ref_frame)]
    CALIB.update({"active": True, "label": label, "trial": trial, "done": False, "ok": False,
                  "stage": "pose (MeTRAbs) + bundle adjustment sur GPU…", "log": "",
                  "output": str(out), "mre": None})
    threading.Thread(target=_calib_worker, args=(cmd, repo, out), daemon=True).start()
    return {"ok": True, "started": True, "n_cams": n_ok, "missing": missing, "output": str(out)}


def _calib_worker(cmd, repo, out):
    logf = Path(out) / "calib.log"
    try:
        with open(logf, "w") as lf:
            p = subprocess.Popen(cmd, cwd=repo, stdout=lf, stderr=subprocess.STDOUT)
            while p.poll() is None:
                CALIB["log"] = _tail(logf, 40)
                time.sleep(2)
            rc = p.wait()
        CALIB["log"] = _tail(logf, 80)
        res = Path(out) / "results" / "Calib_scene_calibrated.toml"
        CALIB.update({"active": False, "done": True, "ok": (rc == 0 and res.exists()),
                      "mre": _parse_mre(logf),
                      "result_toml": str(res) if res.exists() else None,
                      "stage": "terminé" if res.exists() else "échec (voir log)"})
    except Exception as e:
        CALIB.update({"active": False, "done": True, "ok": False, "stage": "erreur: %s" % e})


# ---------- patients ---------- #

def patients_state():
    return {"initialized": STORE.initialized(), "locked": STORE.locked(),
            "codes": STORE.codes_on_disk(),
            "patients": (STORE.all() if not STORE.locked() else None),
            "current": CURRENT["code"]}


def patient_protocol():
    """Checklist des taches faites/restantes pour le patient courant."""
    code = CURRENT.get("code")
    if not code:
        return {"code": None, "tasks": []}
    recs = [r for r in list_recordings().get("recordings", []) if r["code"] == code]
    tasks = []
    for t, lbl in TASKS.items():
        takes = [r for r in recs if r["task"] == t]
        tasks.append({"task": t, "label": lbl, "done": bool(takes), "n_takes": len(takes),
                      "extracted": all(r["extracted"] for r in takes) if takes else False})
    return {"code": code, "tasks": tasks, "n_done": sum(1 for x in tasks if x["done"]), "n": len(tasks)}


def close_patient():
    """Clot le patient courant : extrait toutes ses prises pas encore extraites, puis
    les supprime des Jetson (libere l'eMMC). Ne supprime JAMAIS une prise non extraite."""
    code = CURRENT.get("code")
    if not code:
        return {"error": "aucun patient selectionne"}
    rec = _recording_now()
    if rec:
        return {"error": "enregistrement en cours (%s) — clique FIN d'abord" % ", ".join(rec)}
    recs = [r for r in list_recordings().get("recordings", []) if r["code"] == code]
    if not recs:
        return {"error": "aucune prise de ce patient sur les Jetson"}
    extracted, freed, errors = [], [], []
    for r in recs:
        tag = "%s#%s" % (r["task"], r["trial"])
        if not _take_extracted(r["label"], r["take_id"], r["trial"]):
            do_pull(r["label"], r["take_id"], r["trial"], check_recording=False)
        if not _take_extracted(r["label"], r["take_id"], r["trial"]):
            errors.append(tag); continue          # extraction ratee -> on NE supprime PAS
        extracted.append(tag)
        delete_recording(r["label"], r["take_id"], force=True, trial=r["trial"])
        freed.append(tag)
    df = _ssh_checks()
    free_min = min([b.get("free_mb") for b in df.values() if b.get("free_mb") is not None] or [0])
    return {"ok": True, "code": code, "n": len(recs), "extracted": len(extracted),
            "freed": len(freed), "errors": errors, "free_min_mb": free_min}


# ---------- HTTP ---------- #

class Handler(BaseHTTPRequestHandler):
    def _j(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif self.path.startswith("/api/pull_progress"):
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(self.path).query)
            self._j(200, pull_progress((q.get("label") or [""])[0], (q.get("take_id") or [""])[0],
                                       (q.get("trial") or ["1"])[0]))
        elif self.path.startswith("/api/convert_progress"):
            self._j(200, convert_progress())
        elif self.path.startswith("/api/calib_status"):
            self._j(200, calib_status())
        elif self.path.startswith("/api/preview"):
            self._j(200, preview())
        elif self.path.startswith("/api/protocol"):
            self._j(200, patient_protocol())
        elif self.path.startswith("/api/take_stats"):
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(self.path).query)
            self._j(200, take_stats((q.get("label") or [""])[0], (q.get("take_id") or [""])[0],
                                    (q.get("trial") or [None])[0]))
        elif self.path.startswith("/api/status"):
            self._j(200, light_status())
        elif self.path.startswith("/api/health"):
            self._j(200, preflight())
        elif self.path.startswith("/api/patients"):
            self._j(200, patients_state())
        elif self.path.startswith("/api/cam/logs"):
            m = re.search(r"[?&]ip=([\d.]+)", self.path)
            self._j(200, cam_logs(m.group(1) if m else ""))
        elif self.path.startswith("/api/recordings"):
            self._j(200, list_recordings())
        else:
            self._j(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            data = {}
        p = self.path
        try:
            if p.startswith("/api/patients/setup"):
                STORE.setup(data.get("password", "")); self._j(200, patients_state())
            elif p.startswith("/api/patients/unlock"):
                STORE.unlock(data.get("password", "")); self._j(200, patients_state())
            elif p.startswith("/api/patients/lock"):
                STORE.lock(); self._j(200, patients_state())
            elif p.startswith("/api/patients/create"):
                code = STORE.add({k: v for k, v in data.items() if k != "password"})
                CURRENT["code"] = code
                self._j(200, {"code": code, **patients_state()})
            elif p.startswith("/api/patients/select"):
                CURRENT["code"] = data.get("code") or None
                self._j(200, patients_state())
            elif p.startswith("/api/start"):
                self._j(200, do_start(data.get("task", ""), data.get("resolution"), data.get("fps")))
            elif p.startswith("/api/stop"):
                self._j(200, do_stop())
            elif p.startswith("/api/pull_all"):
                self._j(200, pull_all())
            elif p.startswith("/api/convert_sync"):
                self._j(200, convert_and_sync(data.get("label"), data.get("take_id"), data.get("trial")))
            elif p.startswith("/api/calibrate"):
                self._j(200, run_calibration(data.get("label"), data.get("take_id"), data.get("trial"),
                                             data.get("height"), data.get("ref_frame")))
            elif p.startswith("/api/pull"):
                self._j(200, do_pull(data.get("label"), data.get("take_id"), data.get("trial")))
            elif p.startswith("/api/recordings/delete"):
                self._j(200, delete_recording(data.get("label", ""), data.get("take_id"), bool(data.get("force")), data.get("trial")))
            elif p.startswith("/api/cam/relaunch"):
                self._j(200, cam_relaunch(data.get("ip", "")))
            elif p.startswith("/api/cam/reboot_cam"):
                self._j(200, cam_reboot_cam(data.get("ip", "")))
            elif p.startswith("/api/cam/reboot_jetson"):
                self._j(200, cam_reboot_jetson(data.get("ip", "")))
            elif p.startswith("/api/cam/fix_usb"):
                self._j(200, cam_fix_usb(data.get("ip", "")))
            elif p.startswith("/api/close_patient"):
                self._j(200, close_patient())
            elif p.startswith("/api/fleet/restart"):
                self._j(200, fleet_restart())
            else:
                self._j(404, {"error": "not found"})
        except Exception as e:
            self._j(200, {"error": "%s: %s" % (type(e).__name__, e)})


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ZED Fleet Control</title>
<style>
:root{--bg:#0f1216;--card:#1a1f27;--line:#2a313c;--fg:#e8edf2;--mut:#8b97a6;--ok:#22c55e;--warn:#f59e0b;--fail:#ef4444;--accent:#3b82f6}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:17px;margin:0}.sub{color:var(--mut);font-size:13px}
#banner{margin-left:auto;padding:10px 18px;border-radius:12px;font-weight:700;background:var(--card);border:1px solid var(--line)}
#banner.ready{background:rgba(34,197,94,.15);border-color:var(--ok);color:var(--ok)}
#banner.busy{background:rgba(59,130,246,.15);border-color:var(--accent);color:var(--accent)}
#banner.bad{background:rgba(239,68,68,.15);border-color:var(--fail);color:var(--fail)}
main{padding:18px;max-width:1180px;margin:0 auto}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.panel h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 10px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{font:inherit;font-weight:600;padding:9px 15px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}button:disabled{opacity:.4;cursor:not-allowed}
button.task{background:rgba(59,130,246,.12);border-color:var(--accent)}
button.task.calib{background:rgba(168,85,247,.15);border-color:#a855f7;color:#c084fc}
button.stop{background:rgba(239,68,68,.14);border-color:var(--fail);color:var(--fail)}
button.go{background:rgba(34,197,94,.14);border-color:var(--ok);color:var(--ok)}
input,select{font:inherit;padding:8px 10px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.tile{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--mut);border-radius:11px;padding:10px 12px}
.tile.ok{border-left-color:var(--ok)}.tile.warn{border-left-color:var(--warn)}.tile.fail{border-left-color:var(--fail)}
.tile .lab{font-weight:700}.tile .ip{color:var(--mut);font-size:12px}.tile .kv{color:var(--mut);font-size:12px;margin-top:5px}
.tile .rec{color:var(--ok);font-weight:600;font-size:12px}
.reason{color:var(--warn);font-size:12px;margin-top:3px}.tile.fail .reason{color:var(--fail)}
.acts{margin-top:8px;display:flex;gap:4px}.acts button{padding:3px 8px;font-size:13px;border-radius:7px}
#modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);align-items:center;justify-content:center;z-index:9}
.mbox{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;max-width:820px;width:92%;max-height:82vh;overflow:auto}
.mbox pre{white-space:pre-wrap;font-size:12px;color:var(--mut);margin-top:8px}
.recrow{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--line)}
.recrow button{padding:4px 10px;font-size:13px}.pill{padding:2px 8px;border-radius:20px;font-size:12px;background:var(--bg);border:1px solid var(--line)}
#stats{font-size:14px}.st{display:inline-block;margin:4px 14px 4px 0}
.statshead{display:flex;flex-wrap:wrap;gap:4px 16px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid var(--line)}
b.ok{color:var(--ok)}b.warn{color:var(--warn)}b.bad{color:var(--fail)}
.camlist{display:flex;flex-direction:column;gap:5px}
.camrow{display:grid;grid-template-columns:70px 70px 78px 1fr 60px;align-items:center;gap:10px;font-size:12px}
.camrow .cn{font-weight:700}
.camrow .co{color:var(--mut);font-variant-numeric:tabular-nums;text-align:right}
.camrow .cd{color:var(--mut)}.camrow .cd.warn{color:var(--warn);font-weight:700}
.camrow .cdur{color:var(--mut);text-align:right;font-variant-numeric:tabular-nums}
.tl{position:relative;height:15px;background:rgba(34,197,94,.16);border:1px solid rgba(34,197,94,.35);border-radius:4px;overflow:hidden}
.tl .mark{position:absolute;top:-1px;bottom:-1px;width:2px;background:var(--fail);box-shadow:0 0 4px 1px rgba(239,68,68,.8);cursor:help}
#pullprog{display:none;position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:26;
 background:var(--card);border:1px solid var(--accent);border-radius:12px;padding:11px 16px;min-width:300px;box-shadow:0 8px 28px rgba(0,0,0,.45)}
#pullprog .lab{font-size:13px;font-weight:600;margin-bottom:7px}
#pullprog .track{height:9px;background:var(--line);border-radius:6px;overflow:hidden}
#pullprog .fill{height:100%;width:0;background:var(--accent);border-radius:6px;transition:width .3s}
#pullprog .fill.indet{width:35%;animation:indet 1.1s ease-in-out infinite}
@keyframes indet{0%{margin-left:-35%}100%{margin-left:100%}}
#calibchip{display:none;position:fixed;left:20px;bottom:20px;z-index:27;cursor:pointer;
 padding:10px 15px;border-radius:12px;font-weight:700;font-size:13px;box-shadow:0 6px 22px rgba(0,0,0,.4)}
#calibchip.run{background:rgba(168,85,247,.18);border:1px solid #a855f7;color:#c084fc;animation:pulse 1.6s infinite}
#calibchip.ok{background:rgba(34,197,94,.15);border:1px solid var(--ok);color:var(--ok)}
#calibchip.bad{background:rgba(239,68,68,.15);border:1px solid var(--fail);color:var(--fail)}
.pill{padding:2px 8px;border-radius:20px;font-size:12px;background:var(--bg);border:1px solid var(--line)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
#recbar{display:none;position:fixed;top:0;left:0;right:0;z-index:20;background:var(--fail);color:#fff;
 font-weight:800;font-size:16px;padding:11px 20px;align-items:center;justify-content:center;gap:18px;
 letter-spacing:.02em;box-shadow:0 3px 16px rgba(239,68,68,.6);animation:pulse 1.4s infinite}
#recbar .dot{font-size:20px}#recbar .t{font-variant-numeric:tabular-nums;font-size:19px}
body.recording{box-shadow:inset 0 0 0 5px var(--fail)}
body.recording main{padding-top:56px}
#count{display:none;position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.82);
 align-items:center;justify-content:center;font-weight:900;color:#fff;font-size:clamp(90px,22vw,260px)}
#count.go{color:var(--ok);text-shadow:0 0 40px rgba(34,197,94,.7)}
#confirm,#calibmod{display:none;position:fixed;inset:0;z-index:40;background:rgba(0,0,0,.72);align-items:center;justify-content:center}
#confirm .box,#calibmod .box{background:var(--card);border:2px solid var(--fail);border-radius:16px;padding:24px 28px;max-width:540px;width:92%;text-align:center}
#calibmod .box{border-color:#a855f7}
#confirm .big,#calibmod .big{font-size:22px;font-weight:800;color:var(--fail);margin-bottom:8px}
#calibmod .big{color:#c084fc}
#calibmod .crow{display:flex;gap:12px;justify-content:center;margin-top:20px}
#calibmod button{padding:12px 22px;font-size:16px}
#calibmod input{margin:5px}
#previewmod{display:none;position:fixed;inset:0;z-index:35;background:rgba(0,0,0,.85);padding:18px;overflow:auto}
#previewmod .pbox{max-width:1220px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
#prevgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px;margin-top:10px}
#prevgrid .pcell{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px;min-height:230px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px}
#prevgrid .pcell img{max-width:100%;max-height:180px;border-radius:4px}
#prevgrid .pcell .pl{font-size:12px;font-weight:700}
#prevgrid .pcell .pko{color:var(--fail);font-size:12px;text-align:center}
#confirm .cko{color:var(--warn);font-weight:700;margin:12px 0}
#confirm .crow{display:flex;gap:12px;justify-content:center;margin-top:20px}
#confirm button{padding:12px 22px;font-size:16px}
@media(prefers-color-scheme:light){:root{--bg:#f4f6f9;--card:#fff;--line:#dde3ea;--fg:#1a2430;--mut:#667}}
</style></head><body>
<header><div><h1>ZED Fleet Control</h1><div class="sub" id="sub">chargement…</div></div><div id="banner">—</div></header>
<main>
 <div class="panel" id="ppanel"><h2>Patient</h2><div class="row" id="prow"></div></div>
 <div class="panel"><h2>Enregistrement — Début / Fin manuel</h2>
   <div class="row" style="margin-bottom:10px">
     <span class="sub">Capture :</span>
     <select id="res" onchange="syncFps()">
       <option value="VGA">VGA 672×376</option>
       <option value="HD720" selected>HD720 1280×720</option>
       <option value="HD1080">HD1080 1920×1080</option>
       <option value="HD2K">HD2K 2208×1242</option>
     </select>
     <select id="fps" onchange="warnRes()"></select>
     <span class="sub" id="reswarn"></span>
   </div>
   <div class="row" id="taskrow">
     <button class="task calib" data-t="calib" onclick="start('calib')" title="Enregistre un essai de calibration : une personne marche dans tout l'espace">🎯 Calibration</button>
     <button class="task" data-t="marche" onclick="start('marche')">▶ Marche</button>
     <button class="task" data-t="sts5" onclick="start('sts5')">▶ 5×5STS</button>
     <button class="task" data-t="sts1" onclick="start('sts1')">▶ STS 1min</button>
     <button class="task" data-t="tug" onclick="start('tug')">▶ TUG</button>
     <button class="stop" id="stopbtn" onclick="stop()" disabled>■ FIN</button>
     <button onclick="pull()" id="pullbtn">⬇ Extraire les vidéos</button>
     <button onclick="showPreview()" title="Aperçu live des 12 caméras (vérifier cadrage/orientation)">👁 Aperçu</button>
     <button onclick="check()">↻ Vérifier</button>
     <button class="go" onclick="fleetRestart()" id="fleetbtn">🔄 Relancer la flotte</button>
     <button class="stop" onclick="closePatient()" id="closebtn" title="Extrait toutes les prises du patient puis libère l'eMMC">🧹 Clôturer le patient</button>
   </div>
   <div id="proto" class="row" style="margin-top:6px"></div>
   <div id="stats" style="margin-top:10px"></div>
 </div>
 <div class="panel"><h2>Enregistrements sur les Jetson</h2>
   <div class="row"><button onclick="loadRecordings()">↻ Lister</button><span class="sub" id="recinfo">clique pour lister ce qui est stocké sur les Jetson</span></div>
   <div id="reclist" style="margin-top:8px"></div>
 </div>
 <div class="grid" id="grid"></div>
 <div id="modal" onclick="if(event.target.id==='modal')closeModal()"><div class="mbox"><div class="row"><b id="modaltitle"></b><button onclick="closeModal()" style="margin-left:auto">✕</button></div><pre id="modalbody"></pre></div></div>
</main>
<div id="recbar"></div>
<div id="calibchip" onclick="openCalibLog()"></div>
<div id="count"></div>
<div id="pullprog"><div class="lab" id="pullproglab"></div><div class="track"><div class="fill" id="pullprogfill"></div></div></div>
<div id="confirm"><div class="box"><div id="confirmbody"></div>
  <div class="crow"><button class="go" id="cyes">Oui, enregistrer</button><button class="stop" id="cno">Annuler</button></div></div></div>
<div id="previewmod"><div class="pbox"><div class="row"><b id="prevtitle">Aperçu des caméras</b><button onclick="document.querySelector('#previewmod').style.display='none'" style="margin-left:auto">✕ Fermer</button></div><div id="prevgrid"></div></div></div>
<div id="calibmod"><div class="box">
  <div class="big">🧭 Calibration extrinsèque</div>
  <div class="sub">Le sujet doit se déplacer dans tout le volume. Renseigne sa taille et une frame où il est <b>debout droit, pieds à plat</b> (pour l'échelle métrique + le sol).</div>
  <div class="row" style="justify-content:center;margin-top:14px">
    <input id="cbheight" type="number" step="0.01" placeholder="Taille (m) — ex 1.75">
    <input id="cbref" type="number" placeholder="Frame de référence (debout droit)">
  </div>
  <div class="crow"><button class="go" id="cbgo">Lancer la calibration</button><button class="stop" id="cbno">Annuler</button></div></div></div>
<script>
const $=s=>document.querySelector(s);let recording=false,timer=null,curTask=null;
function banner(t,c){const b=$('#banner');b.textContent=t;b.className=c||''}
function offTxt(m){return m==null?'?':(m<1?(m*1000).toFixed(0)+'µs':m.toFixed(1)+'ms')}
function freeTxt(mb){return mb==null?'?':(mb/1024).toFixed(1)+'G'}
async function jget(u){return (await fetch(u)).json()}
async function jpost(u,b){return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})})).json()}

// ---- signaux sonores + décompte + confirmation ----
let actx=null;
function beep(freq,dur,vol){try{actx=actx||new (window.AudioContext||window.webkitAudioContext)();
 if(actx.state==='suspended')actx.resume();
 const o=actx.createOscillator(),g=actx.createGain();o.type='sine';o.frequency.value=freq;
 o.connect(g);g.connect(actx.destination);g.gain.setValueAtTime(vol||0.3,actx.currentTime);
 g.gain.exponentialRampToValueAtTime(0.0001,actx.currentTime+(dur||0.15));
 o.start();o.stop(actx.currentTime+(dur||0.15));}catch(e){}}
function countdown(){return new Promise(res=>{const el=$('#count');el.style.display='flex';let n=3;
 const step=()=>{if(n>0){el.className='';el.textContent=n;beep(660,0.16,0.35);n--;setTimeout(step,700);}
  else{el.className='go';el.textContent='GO !';beep(990,0.4,0.45);
   setTimeout(()=>{el.style.display='none';el.className='';res();},750);}};step();});}
function confirmBig(html,ackOnly){return new Promise(res=>{const m=$('#confirm');
 $('#confirmbody').innerHTML=html;$('#cno').style.display=ackOnly?'none':'';
 $('#cyes').textContent=ackOnly?'Compris, continuer':'Oui, enregistrer quand même';
 m.style.display='flex';
 $('#cyes').onclick=()=>{m.style.display='none';res(true);};
 $('#cno').onclick=()=>{m.style.display='none';res(false);};});}
let recStart=0,recCount=0,recTotal=0,tick=null;
function elapsed(){const s=Math.max(0,Math.floor((Date.now()-recStart)/1000));
 return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');}
function recBar(show){const b=$('#recbar');
 if(!show){b.style.display='none';document.body.classList.remove('recording');return;}
 document.body.classList.add('recording');b.style.display='flex';
 b.innerHTML=`<span class="dot">●</span><span>ENREGISTREMENT</span><span>${curTask||''}</span>`
  +`<span>${recCount}/${recTotal} caméras</span><span class="t">${elapsed()}</span>`
  +`<span style="opacity:.85">— clique ■ FIN pour arrêter</span>`;}

function renderTiles(d){
 $('#sub').textContent=`${d.n} caméras · ${d.ts} · ${d.n_ok} OK / ${d.n_fail} KO`;
 const g=$('#grid');g.innerHTML='';
 for(const c of d.cams){const v=document.createElement('div');v.className='tile '+c.verdict;
  let h=`<span class="lab">${c.label}</span> <span class="ip">${c.ip}</span>`;
  h+=`<div class="kv">état: ${c.state}${c.frames!=null?' · '+c.frames+'f':''}</div>`;
  h+=`<div class="kv">horloge ${offTxt(c.offset_ms)} · dispo ${freeTxt(c.free_mb)}${c.cam_seen!=null?' · cam '+c.cam_seen:''}</div>`;
  if(c.reason)h+=`<div class="reason">${c.reason}</div>`;
  if(c.state==='recording')h+='<div class="rec">● enregistre</div>';
  h+=`<div class="acts"><button title="Relancer le daemon" onclick="camAct('${c.ip}','relaunch')">⟳</button><button title="Réparer USB (reset xhci — vidéo coincée)" onclick="camAct('${c.ip}','fix_usb')">🔧</button><button title="Redémarrer la caméra (firmware, sans replug)" onclick="camAct('${c.ip}','reboot_cam')">⚡</button><button title="Rebooter la Jetson" onclick="camReboot('${c.ip}')">⏻</button><button title="Voir logs / erreurs" onclick="showLog('${c.ip}')">📋</button></div>`;
  v.innerHTML=h;g.appendChild(v);}
}
async function check(silent){if(!silent)banner('vérification…','busy');
 const d=await jget('/api/health');renderTiles(d);
 const rec=d.cams.filter(c=>c.state==='recording').length;
 if(rec>0&&!recording)restoreRecording(rec,d.n);   // recuperation apres un rechargement de page
 if(!recording){if(d.n_fail>0)banner(`⚠ ${d.n_fail} cam(s) KO`,'bad');else banner(`Flotte prête — ${d.n_ok}/${d.n}`,'ready');}
 return d;}
// une ou plusieurs cams enregistrent alors que l'UI l'ignore (page rechargee) -> on rend le controle
function restoreRecording(rec,n){recording=true;recCount=rec;recTotal=n;recStart=recStart||Date.now();
 $('#stopbtn').disabled=false;setTaskBtns(true);recBar(true);
 banner('enregistrement en cours repris — clique ■ FIN pour arrêter','bad');
 if(timer)clearInterval(timer);timer=setInterval(poll,2000);
 if(tick)clearInterval(tick);tick=setInterval(()=>{if(recording)recBar(true);},1000);}
async function poll(){const d=await jget('/api/status');   // LEGER, sans SSH (ne charge pas les Jetson en capture)
 recCount=d.n_recording;recTotal=d.n;
 if(recording&&recCount===0){   // toutes arretees ailleurs -> on sort de l'etat record
  recording=false;recBar(false);$('#stopbtn').disabled=true;setTaskBtns(false);
  if(timer){clearInterval(timer);timer=null;}if(tick){clearInterval(tick);tick=null;}
  banner('enregistrement terminé','ready');return;}
 if(recording)recBar(true);
}

// ---- patients ----
async function loadPatients(){const s=await jget('/api/patients');renderPatients(s);}
function renderPatients(s){const r=$('#prow');r.innerHTML='';
 if(!s.initialized){r.innerHTML=`<span class="sub">1ʳᵉ utilisation — définis le mot de passe du registre :</span>
   <input type="password" id="pw" placeholder="mot de passe"><button onclick="setup()">Créer le registre</button>`;return;}
 if(s.locked){r.innerHTML=`<input type="password" id="pw" placeholder="mot de passe"><button onclick="unlock()">🔓 Déverrouiller</button>
   <span class="sub">${s.codes.length} patient(s) sur le disque</span>`;return;}
 // unlocked
 let opts='<option value="">— choisir —</option>';
 for(const c of s.codes){const p=s.patients[c]||{};const nm=p.nom?`${p.nom} ${p.prenom||''}`.trim():'';
   opts+=`<option value="${c}" ${c===s.current?'selected':''}>${c}${nm?' · '+nm:''}</option>`;}
 r.innerHTML=`<select id="psel" onchange="selectP()">${opts}</select>
  <button class="go" onclick="toggleNew()">+ Nouveau patient</button>
  <button onclick="lock()">🔒 Verrouiller</button>
  <span class="sub" id="curp"></span>
  <div class="row" id="newform" style="display:none;margin-top:10px;width:100%">
    <input id="nom" placeholder="Nom"><input id="prenom" placeholder="Prénom">
    <input id="age" placeholder="Âge" size="4"><input id="imc" placeholder="IMC" size="4">
    <input id="mmse" placeholder="MMSE" size="4"><input id="notes" placeholder="Notes" size="18">
    <button class="go" onclick="createP()">Créer le code patient</button>
  </div>`;
 updateCur(s);
}
function updateCur(s){const c=s.current;const el=$('#curp');if(!el)return;
 if(c&&s.patients&&s.patients[c]){const p=s.patients[c];el.textContent=`Patient courant : ${c} (${p.nom||''} ${p.prenom||''})`;}
 else el.textContent=c?('Patient courant : '+c):'aucun patient sélectionné';}
function toggleNew(){const f=$('#newform');f.style.display=f.style.display==='none'?'flex':'none';}
async function setup(){const pw=$('#pw').value;if(!pw)return;await jpost('/api/patients/setup',{password:pw});loadPatients();}
async function unlock(){const pw=$('#pw').value;const r=await jpost('/api/patients/unlock',{password:pw});
 if(r.error){banner('✖ '+r.error,'bad');}else loadPatients();}
async function lock(){await jpost('/api/patients/lock');loadPatients();}
async function selectP(){await jpost('/api/patients/select',{code:$('#psel').value});loadPatients();loadProtocol();}
async function createP(){const b={nom:$('#nom').value,prenom:$('#prenom').value,age:$('#age').value,
  imc:$('#imc').value,mmse:$('#mmse').value,notes:$('#notes').value};
 const r=await jpost('/api/patients/create',b);if(r.error){banner('✖ '+r.error,'bad');return;}
 banner('patient créé : '+r.code,'ready');loadPatients();}

// ---- record ----
const FPS_BY_RES={VGA:[15,30,60,100],HD720:[15,30,60],HD1080:[15,30],HD2K:[15]};
function syncFps(){const r=$('#res').value,sel=$('#fps'),cur=+sel.value||30;
 sel.innerHTML=FPS_BY_RES[r].map(f=>'<option>'+f+'</option>').join('');
 sel.value=FPS_BY_RES[r].includes(cur)?cur:(FPS_BY_RES[r].includes(30)?30:FPS_BY_RES[r][0]);
 warnRes();}
function warnRes(){const r=$('#res').value,f=+$('#fps').value,w=$('#reswarn');
 if(r==='HD720'&&f===30){w.textContent='✓ testé stable (0.07% de drops)';w.style.color='var(--ok)';}
 else{w.textContent='⚠ testé instable sur 12 cams (720@60 ≈13%, 1080@30 ≈34% de drops)';w.style.color='var(--warn)';}}
async function start(task){
 beep(440,0.04,0.12);   // clic + débloque l'audio (geste utilisateur)
 banner('pré-check…','busy');const pf=await jget('/api/health');renderTiles(pf);
 // 1) des cams KO au pré-check -> gros warning avec choix
 if(pf.n_fail>0){
  const ko=pf.cams.filter(c=>c.verdict==='fail').map(c=>`${c.label} (${c.ip})`).join(', ');
  const go=await confirmBig(`<div class="big">⚠ ${pf.n_fail} caméra(s) NE DÉMARRENT PAS</div>
    <div class="cko">${ko}</div>
    <div>Enregistrer quand même avec les <b>${pf.n_ok}/${pf.n}</b> caméras prêtes ?</div>`);
  if(!go){banner('enregistrement annulé','bad');return;}
 }
 banner('démarrage des caméras…','busy');
 const r=await jpost('/api/start',{task:task,resolution:$('#res').value,fps:+$('#fps').value});
 if(r.error){banner('✖ '+r.error,'bad');loadPatients();return;}  // resync panneau patient (ex: registre verrouille)
 curTask=r.task_label;recTotal=r.n;recCount=r.n_recording;
 // 2) certaines n'ont pas démarré à l'ouverture -> gros warning avec choix
 if(!r.all_ok){
  const miss=r.rows.filter(x=>!x.ok).map(x=>x.label).join(', ');
  const go=await confirmBig(`<div class="big">⚠ ${r.n-r.n_recording} caméra(s) n'ont pas démarré</div>
    <div class="cko">${miss}</div>
    <div><b>${r.n_recording}/${r.n}</b> caméras enregistrent. Continuer sans les manquantes ?</div>`);
  if(!go){await jpost('/api/stop');banner('enregistrement annulé (caméras arrêtées)','bad');check();return;}
 }
 recording=true;recStart=Date.now();$('#stopbtn').disabled=false;setTaskBtns(true);
 await countdown();     // 3 · 2 · 1 · GO ! + son -> le patient peut y aller
 recBar(true);
 if(timer)clearInterval(timer);timer=setInterval(poll,2000);
 if(tick)clearInterval(tick);tick=setInterval(()=>{if(recording)recBar(true);},1000);
 poll();
}
async function stop(){
 // couper l'etat + minuteurs AVANT le calcul des stats (long), sinon le tick/poll
 // re-affiche la barre rouge pendant l'attente
 recording=false;
 if(timer){clearInterval(timer);timer=null;}if(tick){clearInterval(tick);tick=null;}
 recBar(false);$('#stopbtn').disabled=true;setTaskBtns(false);
 banner('arrêt + stats…','busy');beep(330,0.25,0.3);
 const r=await jpost('/api/stop');
 showStats(r);banner('tâche terminée — vois les stats','ready');check();loadProtocol();
}
function setTaskBtns(dis){document.querySelectorAll('#taskrow button.task').forEach(b=>b.disabled=dis);}
function fmtDur(s){s=Math.round(s||0);const m=Math.floor(s/60);return m?`${m}m${String(s%60).padStart(2,'0')}`:`${s}s`;}
function showStats(r){
 const el=$('#stats');
 if(r.error){el.innerHTML=`<span class="reason">${r.error}</span>`;return;}
 if(!r.cams||!r.cams.length){el.innerHTML='<span class="reason">pas de stats (essai vide ou CSV introuvables)</span>';return;}
 const t=r.totals||{},sync=r.sync||{};
 const dur=r.max_duration_s||Math.max(1,...r.cams.map(c=>c.duration_s||0));
 const pc=t.pct>0.5?'bad':(t.pct>0?'warn':'ok');
 const sc=sync.spread_ms>1000?'warn':'ok';
 let head=`<div class="statshead">
   <span class="st"><b>${r.task}</b> · patient ${r.code}</span>
   <span class="st">durée <b>${fmtDur(dur)}</b></span>
   <span class="st">drops <b class="${pc}">${t.pct}%</b> <span class="sub">(${t.missed}/${t.expected})</span></span>
   <span class="st">déphasage départ <b class="${sc}">${sync.spread_ms} ms</b> <span class="sub">(rattrapable)</span></span>
   <span class="st">${r.n} cams</span></div>`;
 let rows='';
 for(const c of r.cams){
   const marks=(c.drops||[]).map(d=>`<span class="mark" style="left:${(d.t/dur*100).toFixed(2)}%" title="${d.n} frame(s) manquante(s) à ${fmtDur(d.t)}"></span>`).join('');
   const dc=c.drop_count||0;
   const off=c.offset_ms==null?'—':(c.offset_ms>0?'+'+c.offset_ms:'0')+' ms';
   rows+=`<div class="camrow">
     <span class="cn">${c.label}</span>
     <span class="co" title="décalage de la 1ʳᵉ frame vs la cam la plus tôt">${off}</span>
     <span class="cd ${dc>0?'warn':''}">${dc} drop${dc>1?'s':''}</span>
     <div class="tl" title="${c.frames} frames · ${fmtDur(c.duration_s)}">${marks}</div>
     <span class="cdur">${fmtDur(c.duration_s)}</span></div>`;
 }
 const stalled=r.stalled&&r.stalled.length?`<div class="reason">⚠ nettement moins de frames : ${r.stalled.join(', ')} (essai à vérifier / rejouer)</div>`:'';
 el.innerHTML=head+`<div class="camlist">${rows}</div>${stalled}
   <div class="sub" style="margin-top:6px">traits rouges = frames manquantes (position sur la durée) · décalage = déphasage de départ (recalable en post via les timestamps)</div>`;
}
function showProg(show,label,pct){const p=$('#pullprog'),f=$('#pullprogfill');
 if(!show){p.style.display='none';f.className='fill';return;}
 p.style.display='block';$('#pullproglab').textContent=label;
 if(pct==null){f.className='fill indet';f.style.width='35%';}
 else{f.className='fill';f.style.width=Math.max(2,Math.min(100,pct))+'%';}}
async function pull(){banner('extraction des essais manquants…','busy');$('#pullbtn').disabled=true;
 showProg(true,'extraction des essais manquants…',null);
 const r=await jpost('/api/pull_all');$('#pullbtn').disabled=false;showProg(false);
 if(r.error){banner('✖ '+r.error,'bad');return;}
 if(r.n_extracted===0&&r.n_skipped>0){banner(`✓ rien à extraire — les ${r.n_skipped} essais sont déjà sur ton PC`,'ready');}
 else{banner(`✓ ${r.n_extracted} prise(s) extraite(s) · ${r.n_svo} vidéos (${r.total_mb} Mo) · ${r.n_skipped} déjà présent(s)`,'ready');}
 const err=r.n_errors?` · <span class="reason">⚠ ${r.n_errors} en erreur : ${r.errors.join(', ')}</span>`:'';
 $('#stats').innerHTML+=`<div class="sub">extraction globale → ${r.n_extracted} nouvelle(s), ${r.n_skipped} déjà là${err}</div>`;
 loadRecordings();}

async function camAct(ip,kind){banner('action '+kind+' sur '+ip+'…','busy');
 const r=await jpost('/api/cam/'+kind,{ip:ip});
 banner((r.ok?'✓ ':'✖ ')+ip+' — '+(r.msg||r.error||''),(r.ok?'ready':'bad'));setTimeout(check,3500);}
async function camReboot(ip){if(!confirm('Rebooter la Jetson '+ip+' ? (~60s indisponible, il faudra relancer son daemon)'))return;
 const r=await jpost('/api/cam/reboot_jetson',{ip:ip});banner((r.ok?'⏻ ':'✖ ')+(r.msg||r.error||''),'busy');}
async function showLog(ip){$('#modaltitle').textContent='Logs '+ip+'…';$('#modalbody').textContent='chargement…';$('#modal').style.display='flex';
 const r=await jget('/api/cam/logs?ip='+ip);$('#modaltitle').textContent='Logs '+(r.label||'')+' ('+ip+')';$('#modalbody').textContent=r.log||r.error||'(vide)';}
// ---- calibration : suivi persistant en fond + pastille reouvrable ----
let calibLast={},calibModalOpen=false,calibDismissed=false,calibNotified=false;
function fmtCalib(s){return (s.stage?('['+s.stage+']\n\n'):'')+(s.log||'(démarrage…)');}
function openCalibLog(){calibModalOpen=true;
 $('#modaltitle').textContent='🧭 Calibration'+(calibLast.label?(' '+calibLast.label+(calibLast.trial?(' · essai '+calibLast.trial):'')):'');
 $('#modalbody').textContent=fmtCalib(calibLast);$('#modal').style.display='flex';
 if(calibLast.done)calibDismissed=true;}
function renderCalibChip(s){const c=$('#calibchip');
 if(s.active){c.className='run';c.style.display='block';c.textContent='🧭 Calibration en cours — clique pour le log';}
 else if(s.done&&!calibDismissed){c.className=s.ok?'ok':'bad';c.style.display='block';
   c.textContent='🧭 Calibration '+(s.ok?('terminée ✓'+(s.mre?(' · MRE '+s.mre+'px'):'')):'échouée ✗')+' — clique pour le log';}
 else c.style.display='none';}
async function watchCalib(){try{const s=await jget('/api/calib_status');calibLast=s;renderCalibChip(s);
 if(calibModalOpen)$('#modalbody').textContent=fmtCalib(s);
 if(s.done&&!calibNotified&&(s.label)){calibNotified=true;
   if(s.ok)banner('✓ calibration terminée'+(s.mre?(' · MRE '+s.mre+' px'):'')+' → '+(s.result_toml||s.output),'ready');
   else banner('✖ calibration échouée (clique la pastille pour le log)','bad');}
 if(s.active)calibNotified=false;
 }catch(e){}}
function closeModal(){$('#modal').style.display='none';calibModalOpen=false;}
function calibRec(label,take_id,trial){
 const m=$('#calibmod');$('#cbheight').value='';$('#cbref').value='';m.style.display='flex';
 $('#cbno').onclick=()=>{m.style.display='none';};
 $('#cbgo').onclick=async()=>{const h=$('#cbheight').value,rf=$('#cbref').value;m.style.display='none';
   banner('calibration lancée… (plusieurs minutes, GPU)','busy');
   const r=await jpost('/api/calibrate',{label:label,take_id:take_id,trial:trial,height:h||null,ref_frame:rf||null});
   if(r.error){banner('✖ '+r.error,'bad');return;}
   calibDismissed=false;calibNotified=false;await watchCalib();openCalibLog();
 };
}
async function fleetRestart(){
 banner('relance de la flotte (redéploie + relance les 12)…','busy');$('#fleetbtn').disabled=true;
 const r=await jpost('/api/fleet/restart');$('#fleetbtn').disabled=false;
 if(r.ok)banner('✓ flotte relancée — '+r.n_up+'/'+r.n+' daemons OK','ready');
 else banner('⚠ '+r.n_up+'/'+r.n+' daemons OK (relance les autres / vérifie leur alim)','bad');
 check();}
const SHORT={calib:'Calib',marche:'Marche',sts5:'5STS',sts1:'STS1min',tug:'TUG'};
async function showPreview(){
 $('#prevtitle').textContent='Aperçu — ouverture des 12 caméras (~10s)…';
 $('#prevgrid').innerHTML='<span class="sub">capture en cours…</span>';$('#previewmod').style.display='block';
 const d=await jget('/api/preview');
 $('#prevtitle').textContent=`Aperçu des caméras — ${d.n_ok}/${d.n} · ${d.ts}`;
 const rot={90:90,180:180,270:-90};let h='';
 for(const c of d.cams){
  const body=c.ok?`<img src="data:image/jpeg;base64,${c.jpg}" style="transform:rotate(${rot[c.rotate]||0}deg)">`:`<span class="pko">✖ ${c.error||'image manquante'}</span>`;
  h+=`<div class="pcell">${body}<span class="pl">${c.label}${c.rotate?(' ↻'+c.rotate+'°'):''}</span></div>`;}
 $('#prevgrid').innerHTML=h;}
async function loadProtocol(){
 const d=await jget('/api/protocol');const el=$('#proto');
 if(!d.code){el.innerHTML='';return;}
 let h=`<span class="sub">Protocole ${d.code} — ${d.n_done}/${d.n} tâches :</span> `;
 for(const t of d.tasks){const ic=t.done?'✅':'⬜';const ex=(t.done&&!t.extracted)?' ⚠non extrait':'';
  h+=`<span class="pill" title="${t.n_takes} prise(s)${ex}">${ic} ${SHORT[t.task]||t.task}${t.n_takes>1?' ×'+t.n_takes:''}</span> `;}
 el.innerHTML=h;}
async function closePatient(){
 if(!confirm("Clôturer le patient courant ?\\n\\nÇa EXTRAIT toutes ses prises vers ton PC, PUIS les SUPPRIME des Jetson (libère l'eMMC).\\nUne prise qui n'a pas pu être extraite est conservée (jamais supprimée sans copie).")) return;
 banner('clôture patient : extraction + nettoyage eMMC…','busy');$('#closebtn').disabled=true;
 const r=await jpost('/api/close_patient');$('#closebtn').disabled=false;
 if(r.error){banner('✖ '+r.error,'bad');return;}
 const warn=r.errors&&r.errors.length?` · ⚠ conservées (non extraites) : ${r.errors.join(', ')}`:'';
 banner(`✓ ${r.code} clôturé — ${r.extracted}/${r.n} extraites, ${r.freed} libérées · dispo min ${(r.free_min_mb/1024).toFixed(1)} G${warn}`,r.errors&&r.errors.length?'bad':'ready');
 loadRecordings();loadProtocol();}
async function loadRecordings(){const d=await jget('/api/recordings');const l=$('#reclist');
 $('#recinfo').textContent=(d.recordings.length||0)+' prise(s) sur les Jetson';
 if(!d.recordings.length){l.innerHTML='<span class="sub">aucun enregistrement sur les Jetson</span>';return;}
 let h='';for(const r of d.recordings){
  const b=r.extracted?'<span class="pill" style="color:var(--ok)">✓ extrait</span>':'<span class="pill" style="color:var(--warn)">⚠ non extrait</span>';
  const trial=r.n_trials>1?`<span class="pill" style="color:var(--accent)">essai ${r.trial}/${r.n_trials}</span> `:'';
  const conv=r.converted?'<span class="pill" style="color:var(--ok)">🎬 synchro ✓</span> ':'';
  h+=`<div class="recrow"><b>${r.code}</b> · ${r.task||'(essai)'} ${trial}· ⏱ ${fmtDur(r.duration_s)} · ${r.n_cams} cams · ${r.size_mb} Mo · ${r.when} ${conv}${b}<span style="flex:1"></span><button onclick="statsRec('${r.label}','${r.take_id}',${r.trial})" title="Revoir les stats (drops, timeline, sync) de cette prise">📊 Stats</button><button onclick="pullRec('${r.label}','${r.take_id}',${r.size_mb},${r.trial})">⬇ Extraire</button><button class="go" onclick="convertRec('${r.label}','${r.take_id}',${r.trial})" title="SVO->MP4 gauche + resynchro wall-clock + frames noires aux drops (essai_${r.trial}/raw_mp4/)">🎬 MP4 synchro</button>${r.converted?`<button onclick="calibRec('${r.label}','${r.take_id}',${r.trial})" title="Lancer la calibration extrinsèque sur les MP4 synchro (repo externe, GPU)">🧭 Calibrer</button>`:''}<button class="stop" onclick="delRec('${r.label}','${r.take_id}',false,${r.trial})">🗑 Supprimer</button></div>`;}
 l.innerHTML=h;}
async function statsRec(label,take_id,trial){
 banner('calcul des stats de la prise…','busy');
 const r=await jget('/api/take_stats?label='+encodeURIComponent(label)+'&take_id='+take_id+'&trial='+trial);
 if(r.error){banner('✖ '+r.error,'bad');return;}
 showStats(r);banner('📊 stats — '+label+' (essai '+trial+')','ready');
 $('#stats').scrollIntoView({behavior:'smooth',block:'center'});}
async function pullRec(label,take_id,expmb,trial){
 showProg(true,'extraction… 0 / '+expmb+' Mo',2);banner('extraction en cours…','busy');
 let done=false;
 const iv=setInterval(async()=>{if(done)return;
   try{const p=await jget('/api/pull_progress?label='+encodeURIComponent(label)+'&take_id='+take_id+'&trial='+trial);
     const pct=expmb?Math.round(p.done_mb/expmb*100):null;
     showProg(true,'extraction… '+p.done_mb+' / '+expmb+' Mo',pct==null?null:Math.min(99,pct));
   }catch(e){}},800);
 const r=await jpost('/api/pull',{label:label,take_id:take_id,trial:trial});
 done=true;clearInterval(iv);
 if(r.error){showProg(false);banner('✖ '+r.error,'bad');return;}
 showProg(true,'✓ '+r.n_svo+' vidéos ('+r.total_mb+' Mo)',100);
 setTimeout(()=>showProg(false),1600);
 banner('✓ '+r.n_svo+' vidéos extraites ('+r.total_mb+' Mo)','ready');loadRecordings();}
async function convertRec(label,take_id,trial){
 showProg(true,'conversion + synchro… (plusieurs minutes)',null);banner('conversion + synchro en cours…','busy');
 let done=false;
 const iv=setInterval(async()=>{if(done)return;try{const p=await jget('/api/convert_progress');
   if(p&&p.active&&p.stage)showProg(true,'⚙ '+p.stage+'…',null);}catch(e){}},1500);
 const r=await jpost('/api/convert_sync',{label:label,take_id:take_id,trial:trial});
 done=true;clearInterval(iv);
 if(r.error){showProg(false);banner('✖ '+r.error,'bad');return;}
 showProg(true,'✓ synchro terminée',100);setTimeout(()=>showProg(false),1800);
 const miss=(r.missing&&r.missing.length)?` · ⚠ manquantes: ${r.missing.join(', ')}`:'';
 banner(`✓ ${r.n_cams} MP4 synchro · ${fmtDur(r.duration_s)} · ${r.total_black} frames noires (${r.black_pct}%) · départ ±${r.spread_ms} ms → essai_${r.trial}/raw_mp4/${miss}`,r.missing&&r.missing.length?'bad':'ready');
 loadRecordings();}
async function delRec(label,take_id,force,trial){const r=await jpost('/api/recordings/delete',{label:label,take_id:take_id,force:force,trial:trial});
 if(r.warning==='non_extrait'){if(confirm('⚠ '+r.msg))return delRec(label,take_id,true,trial);return;}
 if(r.error){banner('✖ '+r.error,'bad');return;}
 banner('🗑 prise supprimée','ready');loadRecordings();}
syncFps();loadPatients();loadProtocol();check();setInterval(()=>{if(!recording)check(true);},12000);
watchCalib();setInterval(watchCalib,3000);
</script></body></html>"""


def main():
    global CFG, HOSTS, STORE, DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--data", default="data")
    args = ap.parse_args()
    CFG = orch.load_config(args.config)
    HOSTS = CFG["hosts"]
    DATA_DIR = Path(args.data)
    STORE = PatientStore(DATA_DIR)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print("ZED Fleet Control — http://localhost:%d  (%d cams, data=%s)" % (args.port, len(HOSTS), DATA_DIR))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\narret.")


if __name__ == "__main__":
    main()
