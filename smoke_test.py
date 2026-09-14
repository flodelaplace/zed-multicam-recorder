import os
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import time
import pyzed.sl as sl

cam = sl.Camera()
init = sl.InitParameters()
init.camera_resolution = sl.RESOLUTION.HD720
init.camera_fps = 30
init.depth_mode = sl.DEPTH_MODE.NONE

t_open = time.time()
status = cam.open(init)
if status != sl.ERROR_CODE.SUCCESS:
    print("OPEN FAILED:", status)
    raise SystemExit(1)
print("open OK in %.2fs" % (time.time() - t_open))

svo_path = "/tmp/smoke.svo"
rec = sl.RecordingParameters(svo_path, sl.SVO_COMPRESSION_MODE.H264)
err = cam.enable_recording(rec)
if err != sl.ERROR_CODE.SUCCESS:
    print("ENABLE_RECORDING FAILED:", err)
    cam.close()
    raise SystemExit(1)

rt = sl.RuntimeParameters()
N = 150
t0 = time.time()
grabbed = 0
for i in range(N):
    if cam.grab(rt) == sl.ERROR_CODE.SUCCESS:
        grabbed += 1
dt = time.time() - t0
dropped = cam.get_frame_dropped_count()
cam.disable_recording()
cam.close()

print("grabbed %d/%d in %.2fs => %.1f fps" % (grabbed, N, dt, grabbed / dt if dt else 0))
print("sdk dropped count (cumulative):", dropped)
print("svo size: %d bytes" % os.path.getsize(svo_path))
