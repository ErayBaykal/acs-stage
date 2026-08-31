"""Restore axis 0's soft limits, and report every axis's limits vs position.

Axis 0's originals were captured in the calibration log before it widened
them:  "soft limits widened for measurement (were -742400 .. 2304000)"

The calibration then wrote 2305204 .. 2420796 -- a ~115k band at the far
positive end, because it mistook the parked position for an endpoint.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
RESTORE = {0: (-742400.0, 2304000.0)}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def rr(name, i):
    return float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def wr(name, i, v):
    sp.WriteReal(hc, NONE, name, i, i, NONE, NONE, v, sp.SYNCHRONOUS, True)


for axis, (lo, hi) in RESTORE.items():
    before = (rr("SLLIMIT", axis), rr("SRLIMIT", axis))
    wr("SLLIMIT", axis, lo)
    wr("SRLIMIT", axis, hi)
    after = (rr("SLLIMIT", axis), rr("SRLIMIT", axis))
    print(f"axis {axis}: {before[0]:.0f} .. {before[1]:.0f}"
          f"  ->  {after[0]:.0f} .. {after[1]:.0f}")

print(f"\n{'ax':<3} {'name':<12} {'SLLIMIT':>12} {'SRLIMIT':>12} {'RPOS':>12} {'ok?':>6}")
print("-" * 62)
for a in cfg.AXES:
    i = a.index
    lo, hi, pos = rr("SLLIMIT", i), rr("SRLIMIT", i), rr("RPOS", i)
    print(f"{i:<3} {a.name:<12} {lo:>12.6g} {hi:>12.6g} {pos:>12.6g} "
          f"{'yes' if lo <= pos <= hi else '** NO **':>6}")

sp.CloseComm(hc, True)
