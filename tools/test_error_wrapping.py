"""Check that a rejected ACSPL+ command surfaces as a catchable ControllerError
carrying its error code, so callers can branch on it.

Regression: execute() used Transaction(failure_check=True), which raises the
library's own exception before the "?<code>" reply is ever inspected. The
3314 fallback in calibrate._probe_end only caught ControllerError, so an
unsupported homing method escaped it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage import config as cfg  # noqa: E402
from acs_stage.calibrate import MethodUnsupported, TravelCalibrator  # noqa: E402
from acs_stage.controller import ControllerError, StageController  # noqa: E402

failed = 0

c = StageController()
c.connect()
try:
    # Method 53 is not implemented on this firmware; axis 0 is a safe target
    # because an unsupported method is rejected before any motion.
    try:
        c.execute("HOME 0,53")
        print("FAIL: expected the command to be rejected")
        failed += 1
    except ControllerError as exc:
        ok = "3314" in str(exc)
        print(f"  {'ok  ' if ok else 'FAIL'} rejected as ControllerError "
              f"carrying the code: {str(exc).splitlines()[-1][:70]}")
        failed += not ok
    except Exception as exc:
        print(f"  FAIL raised {type(exc).__name__}, not ControllerError: {exc}")
        failed += 1

    # And the calibrator must translate that into MethodUnsupported so the
    # host-side fallback engages.
    cal = TravelCalibrator(c)
    try:
        cal._probe_far_end_with_firmware(6, cfg.Direction.NEGATIVE)
        print("  (axis 6 negative probe unexpectedly accepted)")
    except MethodUnsupported as exc:
        print(f"  ok   axis 6 negative probe -> MethodUnsupported ({exc})")
    except Exception as exc:
        print(f"  FAIL axis 6 probe raised {type(exc).__name__}: "
              f"{str(exc).splitlines()[-1][:70]}")
        failed += 1
finally:
    c.disconnect()

print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
