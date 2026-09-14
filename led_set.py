import os, sys, time
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import pyzed.sl as sl

hold = int(sys.argv[1]) if len(sys.argv) > 1 else 240
led = int(sys.argv[2]) if len(sys.argv) > 2 else 1
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
if ok:
    try:
        cam.set_camera_settings(sl.VIDEO_SETTINGS.LED_STATUS, led)
    except Exception as e:
        print("LED err:", e)
    time.sleep(hold)
    cam.close()
