"""Read-only comparison of the two linear axes. Issues no motion, writes nothing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
AXES = [0, 1]

# Everything that feeds hard-stop homing: the detection threshold, the
# current limits that cap push force, and the profile that governs how much
# momentum arrives at the stop.
REAL_VARS = [
    "CERRV", "CERRI", "CERRA",          # critical position error thresholds
    "ERRV", "ERRI", "ERRA",             # non-critical position error
    "XCURV", "XRMSM", "XRMSD",          # current limits -> HomingCurrLimit
    "VEL", "ACC", "DEC", "KDEC", "JERK",
    "EFAC", "SLPMIN", "SLPMAX",
    "PE", "FPOS", "RPOS",
]
INT_VARS = ["MFLAGS", "MST", "FAULT", "FMASK", "MERR", "IST"]

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")
print(f"connected to {cfg.CONTROLLER_HOST}\n")

print(f"{'variable':<12} {'axis 0 (works)':>18} {'axis 1 (faults)':>18}   ratio")
print("-" * 72)

for name in REAL_VARS:
    vals = []
    for i in AXES:
        try:
            vals.append(float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE,
                                          sp.SYNCHRONOUS, True)))
        except Exception:
            vals.append(None)
    if None in vals:
        print(f"{name:<12} {'read failed':>18}")
        continue
    a, b = vals
    ratio = ""
    if a not in (0.0,) and b not in (0.0,):
        r = b / a
        if abs(r - 1.0) > 0.01:
            ratio = f"  {r:.3g}x"
    flag = "  <<<" if (a != b) else ""
    print(f"{name:<12} {a:>18.4g} {b:>18.4g}{ratio}{flag}")

print()
for name in INT_VARS:
    vals = []
    for i in AXES:
        try:
            vals.append(int(sp.ReadInteger(hc, NONE, name, i, i, NONE, NONE,
                                           sp.SYNCHRONOUS, True)))
        except Exception:
            vals.append(None)
    a, b = vals
    flag = "  <<<" if a != b else ""
    print(f"{name:<12} {a:>18} {b:>18}{flag}   (0x{a:x} / 0x{b:x})" + flag)

sp.CloseComm(hc, True)
print("\ndisconnected")
