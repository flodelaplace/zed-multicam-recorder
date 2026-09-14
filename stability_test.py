import os
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import time
import pyzed.sl as sl


def btime():
    for line in open("/proc/stat"):
        if line.startswith("btime"):
            return int(line.split()[1])
    return 0


b0 = btime()
cam = sl.Camera()
init = sl.InitParameters()
init.camera_resolution = sl.RESOLUTION.HD720
init.camera_fps = 30
init.depth_mode = sl.DEPTH_MODE.NONE
ok = False
for k in range(5):
    if cam.open(init) == sl.ERROR_CODE.SUCCESS:
        ok = True
        break
    time.sleep(2)
if not ok:
    print("OPEN_FAIL (cam pas detectee -> replug USB)")
    raise SystemExit(1)

cam.enable_recording(sl.RecordingParameters("/tmp/stab.svo", sl.SVO_COMPRESSION_MODE.H264))
rt = sl.RuntimeParameters()
N = 7200  # ~240 s @ 30 fps
g = 0
t0 = time.time()
for i in range(N):
    if cam.grab(rt) == sl.ERROR_CODE.SUCCESS:
        g += 1
dt = time.time() - t0
cam.disable_recording()
cam.close()
b1 = btime()
verdict = "STABLE (pas de reboot)" if b0 == b1 else "*** REBOOT DETECTE ***"
print("grabbed %d/%d in %.0fs | btime_before=%d btime_after=%d => %s"
      % (g, N, dt, b0, b1, verdict))
try:
    os.remove("/tmp/stab.svo")
except OSError:
    pass
