"""Restore CERRV to its nominal value on an axis.

Calibration raises CERRV 10x during a hard-stop search and restores it in a
`finally`. If the process is killed mid-search -- closing the window during a
sequence -- that restore never runs and the axis is left with a fault
threshold 10x too lenient.

Usage:  python tools/restore_cerrv.py <axis> <value>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def rr(name, i):
    return float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


if len(sys.argv) >= 3:
    axis, value = int(sys.argv[1]), float(sys.argv[2])
    before = rr("CERRV", axis)
    sp.WriteReal(hc, NONE, "CERRV", axis, axis, NONE, NONE, value,
                 sp.SYNCHRONOUS, True)
    print(f"axis {axis}: CERRV {before:.0f} -> {rr('CERRV', axis):.0f}")

print(f"\n{'ax':<3} {'name':<12} {'CERRI':>10} {'CERRV':>10} {'CERRA':>10}")
print("-" * 50)
for a in cfg.AXES:
    print(f"{a.index:<3} {a.name:<12} {rr('CERRI', a.index):>10.6g} "
          f"{rr('CERRV', a.index):>10.6g} {rr('CERRA', a.index):>10.6g}")

sp.CloseComm(hc, True)
