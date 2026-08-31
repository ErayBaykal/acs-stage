"""Move an axis off a hard stop it is pressed against.

After a KILL the servo can be left holding a target inside the stop -- RPOS
frozen beyond FPOS -- which applies a steady push. Jogging away from the stop
relieves it.

Usage:  python tools/relieve_stop.py <axis> <direction:+1|-1> [seconds]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
axis = int(sys.argv[1]) if len(sys.argv) > 1 else 6
direction = int(sys.argv[2]) if len(sys.argv) > 2 else 1
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
SPEED = 400.0

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def rr(n):
    return float(sp.ReadReal(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def ri(n):
    return int(sp.ReadInteger(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


print(f"axis {axis} before: FPOS={rr('FPOS'):.1f} RPOS={rr('RPOS'):.1f} "
      f"PE={rr('PE'):.1f} enabled={ri('MST') & 1}")

sp.FaultClear(hc, axis, sp.SYNCHRONOUS, True)
time.sleep(0.3)

try:
    sp.Jog(hc, sp.MotionFlags.ACSC_AMF_VELOCITY, axis,
           direction * SPEED, sp.SYNCHRONOUS, True)
    steps = int(seconds / 0.5)
    for k in range(steps):
        time.sleep(0.5)
        print(f"  t={0.5 * (k + 1):.1f}s  FPOS={rr('FPOS'):>10.1f}  PE={rr('PE'):>8.1f}")
finally:
    sp.Halt(hc, axis, sp.SYNCHRONOUS, True)
    time.sleep(1.0)
    sp.FaultClear(hc, axis, sp.SYNCHRONOUS, True)
    time.sleep(0.5)

print(f"axis {axis} after:  FPOS={rr('FPOS'):.1f} RPOS={rr('RPOS'):.1f} "
      f"PE={rr('PE'):.1f} MERR={ri('MERR')} enabled={ri('MST') & 1}")
sp.CloseComm(hc, True)
