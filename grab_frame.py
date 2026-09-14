import os, sys, time
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import pyzed.sl as sl

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/view.jpg"
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
    print("OPEN_FAIL")
    sys.exit(1)
rt = sl.RuntimeParameters()
img = sl.Mat()
got = False
for _ in range(15):          # warmup + grab
    if cam.grab(rt) == sl.ERROR_CODE.SUCCESS:
        cam.retrieve_image(img, sl.VIEW.LEFT)
        got = True
if got:
    img.write(out)
    print("SAVED", out)
else:
    print("NO_FRAME")
cam.close()
