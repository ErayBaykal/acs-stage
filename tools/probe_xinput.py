"""Probe every XInput slot and report raw return codes.

Distinguishes "no controller" from "controller present but XInput cannot see
it", which is the usual symptom of an Xbox pad connected over Bluetooth whose
XInput interface has not started.
"""
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage.gamepad import (ERROR_DEVICE_NOT_CONNECTED, ERROR_SUCCESS,  # noqa: E402
                               XINPUT_STATE, _XINPUT, _XINPUT_NAME)

if _XINPUT is None:
    sys.exit("no XInput DLL could be loaded")

print(f"driver: {_XINPUT_NAME}\n")

CODES = {ERROR_SUCCESS: "connected", ERROR_DEVICE_NOT_CONNECTED: "not connected"}

for attempt in range(3):
    print(f"--- pass {attempt + 1} ---")
    for slot in range(4):
        state = XINPUT_STATE()
        rc = _XINPUT.XInputGetState(slot, ctypes.byref(state))
        label = CODES.get(rc, f"error {rc}")
        detail = ""
        if rc == ERROR_SUCCESS:
            g = state.Gamepad
            detail = (f"  buttons=0x{g.wButtons:04x} LT={g.bLeftTrigger} "
                      f"RT={g.bRightTrigger} LX={g.sThumbLX} LY={g.sThumbLY}")
        print(f"  slot {slot}: rc={rc:<5} {label}{detail}")
    time.sleep(0.5)

print("\nIf every slot reports 'not connected' while Windows lists an Xbox\n"
      "Wireless Controller, the pad is paired over Bluetooth but its XInput\n"
      "interface has not started. Connecting it by USB cable, or through the\n"
      "Xbox Wireless Adapter, presents it to XInput directly.")
