import os, sys, time
os.environ.setdefault("OPENBLAS_CORETYPE", "ARMV8")
import pyzed.sl as sl

hold = int(sys.argv[1]) if len(sys.argv) > 1 else 300
cam = sl.Camera()
init = sl.InitParameters()
init.camera_resolution = sl.RESOLUTION.HD720
init.camera_fps = 30
init.depth_mode = sl.DEPTH_MODE.NONE
ok = False
for k in range(6):
    if cam.open(init) == sl.ERROR_CODE.SUCCESS:
        ok = True
        break
    time.sleep(2)
with open("/tmp/ledoff_status", "w") as f:
    if ok:
        try:
            cam.set_camera_settings(sl.VIDEO_SETTINGS.LED_STATUS, 0)
            f.write("OFF_OK")
        except Exception as e:
            f.write("SET_ERR:%s" % e)
    else:
        f.write("OPEN_FAIL")
if ok:
    time.sleep(hold)
    cam.close()
