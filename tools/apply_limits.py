"""Rewrite each calibrated axis's soft limits using the corrected margin rule.

The margin now applies only at the probed end; the homed end gets none, so
homing never lands the axis outside its own limit.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402
from acs_stage.travel import TravelStore  # noqa: E402

NONE = -1

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def rr(name, i):
    return float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def wr(name, i, v):
    sp.WriteReal(hc, NONE, name, i, i, NONE, NONE, v, sp.SYNCHRONOUS, True)


for r in TravelStore():
    lo, hi = r.safe_limits()
    before = (rr("SLLIMIT", r.axis), rr("SRLIMIT", r.axis))
    wr("SLLIMIT", r.axis, lo)
    wr("SRLIMIT", r.axis, hi)
    pos = rr("RPOS", r.axis)
    end = "min" if r.homed_at_min else "max"
    print(f"axis {r.axis}: {before[0]:.0f}..{before[1]:.0f}  ->  {lo:.0f}..{hi:.0f}"
          f"   (homed at {end} = {r.homed_zero:.0f}, no margin that end)")
    print(f"         RPOS {pos:.0f} in range: "
          f"{'yes' if lo <= pos <= hi else '** NO **'}")

print(f"\n{'ax':<3} {'name':<12} {'SLLIMIT':>12} {'SRLIMIT':>12} {'RPOS':>12} {'ok?':>9}")
print("-" * 64)
for a in cfg.AXES:
    lo, hi, pos = rr("SLLIMIT", a.index), rr("SRLIMIT", a.index), rr("RPOS", a.index)
    print(f"{a.index:<3} {a.name:<12} {lo:>12.6g} {hi:>12.6g} {pos:>12.6g} "
          f"{'yes' if lo <= pos <= hi else '** NO **':>9}")

sp.CloseComm(hc, True)
