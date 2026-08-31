"""Jog an axis briefly and record exactly what the controller does.

Usage:  python tools/jog_diag.py <axis> <direction:+1|-1> [seconds] [speed]
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
seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
speed = float(sys.argv[4]) if len(sys.argv) > 4 else 400.0

FAULT_BITS = {"#RL": 0, "#LL": 1, "#SRL": 5, "#SLL": 6, "#DRIVE": 9,
              "#ENC": 10, "#PE": 12, "#CPE": 13, "#CL": 16, "#STO": 18}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def rr(n):
    return float(sp.ReadReal(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def ri(n):
    return int(sp.ReadInteger(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def faults():
    f = ri("FAULT")
    return ",".join(n for n, b in FAULT_BITS.items() if f >> b & 1) or "-"


sp.FaultClear(hc, axis, sp.SYNCHRONOUS, True)
time.sleep(0.3)
if not (ri("MST") & 1):
    print("motor disabled -> enabling")
    sp.Enable(hc, axis, sp.SYNCHRONOUS, True)
    time.sleep(0.5)

print(f"axis {axis}: enabled={ri('MST') & 1} FPOS={rr('FPOS'):.1f} "
      f"faults={faults()} MERR={ri('MERR')}")
print(f"jogging {direction:+d} at {speed} cts/s for {seconds}s\n")
print(f"{'t':>5} {'FPOS':>10} {'RPOS':>10} {'PE':>8} {'en':>3} {'mv':>3} "
      f"{'MERR':>5}  faults")

start = rr("FPOS")
try:
    sp.Jog(hc, sp.MotionFlags.ACSC_AMF_VELOCITY, axis,
           direction * speed, sp.SYNCHRONOUS, True)
except Exception as exc:
    print("jog rejected:", str(exc).splitlines()[-1])
    sp.CloseComm(hc, True)
    sys.exit(1)

t0 = time.monotonic()
while time.monotonic() - t0 < seconds:
    print(f"{time.monotonic()-t0:5.1f} {rr('FPOS'):>10.1f} {rr('RPOS'):>10.1f} "
          f"{rr('PE'):>8.1f} {ri('MST') & 1:>3} {ri('MST') >> 5 & 1:>3} "
          f"{ri('MERR'):>5}  {faults()}")
    time.sleep(0.25)

sp.Halt(hc, axis, sp.SYNCHRONOUS, True)
time.sleep(0.8)
moved = rr("FPOS") - start
print(f"\nmoved {moved:+.1f} counts (expected ~{direction * speed * seconds:+.0f})")
print(f"final: enabled={ri('MST') & 1} MERR={ri('MERR')} faults={faults()}")
sp.CloseComm(hc, True)
