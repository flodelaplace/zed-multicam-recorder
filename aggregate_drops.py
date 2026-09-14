#!/usr/bin/env python3
# Agrege les drops par cam et par run a partir des CSV sauvegardes (audit/<run>/).
import subprocess, re, os

RUNS = ["gait90_1", "gait90_2", "tug180_1", "tug180_2", "sts360_1", "sts360_2"]
SER2LAB = {
    "22135621": "J5(.7)", "28362776": "J7(.9)", "28207060": "zed9(.10)",
    "20542385": "zed10(.11)", "25948117": "zed12(.12)", "23767062": "J8(.20)",
    "29813646": "zed21(.21)", "24710321": "zed22(.22)", "23859316": "zed23(.23)",
    "26461602": "zed25(.25)", "22040163": "zed26(.26)", "22516499": "zed29(.29)",
}
FR = 1000.0 / 30.0  # ms per frame

# data[serial][run] = (missed, events, max_gap_ms)
data = {}
run_present = {r: set() for r in RUNS}
for run in RUNS:
    d = os.path.join("audit", run)
    if not os.path.isdir(d):
        continue
    out = subprocess.run(["python3", "orchestrator.py", "analyze", "--local-dir", d],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.search(r"_(\d{8})_\d{8}T\S*\s+\d+\s+[\d.]+\s+[\d.]+\s+(\d+)\s+(\d+)\s+[\d.]+\s+([\d.]+)", line)
        if m:
            ser, ev, missed, gap = m.group(1), int(m.group(2)), int(m.group(3)), float(m.group(4))
            data.setdefault(ser, {})[run] = (missed, ev, gap)
            run_present[run].add(ser)

# Tableau: frames droppees (max consec d'affilee)
hdr = "cam".ljust(11) + "".join(r.replace("_", "").rjust(13) for r in RUNS) + "   TOTAL"
print(hdr)
print("-" * len(hdr))
tot_by_run = {r: 0 for r in RUNS}
for ser in sorted(data, key=lambda s: SER2LAB.get(s, s)):
    lab = SER2LAB.get(ser, ser)
    row = lab.ljust(11)
    tcam = 0
    for r in RUNS:
        if r in data[ser]:
            missed, ev, gap = data[ser][r]
            maxc = max(0, round(gap / FR) - 1) if missed else 0
            cell = "%d(%d)" % (missed, maxc)
            tcam += missed
            tot_by_run[r] += missed
        else:
            cell = "ABSENT" if ser not in run_present[r] else "0(0)"
        row += cell.rjust(13)
    row += ("%d" % tcam).rjust(8)
    print(row)
print("-" * len(hdr))
trow = "TOTAL flotte".ljust(11) + "".join(("%d" % tot_by_run[r]).rjust(13) for r in RUNS)
print(trow)
print("\nFormat cellule = frames_droppees(max_consecutives_d_affilee).  1 frame = 33.3 ms.")

# Stats moyennes de coupure (par event) globales
all_events, all_missed, worst = 0, 0, (0, "", "")
for ser in data:
    for r, (missed, ev, gap) in data[ser].items():
        all_events += ev
        all_missed += missed
        if gap > worst[0]:
            worst = (gap, SER2LAB.get(ser, ser), r)
print("\n=== moyennes ===")
if all_events:
    print("Coupures (freezes) totales sur les 6 runs : %d" % all_events)
    print("Frames perdues au total : %d" % all_missed)
    print("Moyenne de frames perdues PAR coupure : %.1f frames (%.0f ms)"
          % (all_missed / all_events, all_missed / all_events * FR))
print("Plus longue coupure observee : %.0f ms (~%d frames d'affilee) sur %s au run %s"
      % (worst[0], max(0, round(worst[0] / FR) - 1), worst[1], worst[2]))
