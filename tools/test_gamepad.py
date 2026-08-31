"""Live Xbox controller readout — confirms detection and identifies buttons.

Run it, press things, and watch the names. Ctrl-C to stop.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage.gamepad import BUTTONS, Gamepad  # noqa: E402

pad = Gamepad()
print(f"XInput available: {pad.available}"
      + (f" ({pad.driver})" if pad.driver else " — no XInput DLL found"))
if not pad.available:
    sys.exit("XInput is not available on this machine")

print("\nbutton names this module reports:")
print("  " + ", ".join(sorted(BUTTONS)))
print("\npress buttons / move sticks. Ctrl-C to stop.\n")

last = None
try:
    while True:
        state = pad.poll()
        if not state.connected:
            line = "no controller detected — plug in the receiver/USB"
        else:
            line = f"slot {state.slot}:  {state.describe()}"
        if line != last:
            print(f"  {line}")
            last = line
        time.sleep(0.05)
except KeyboardInterrupt:
    print("\nstopped")
