"""Show each axis's current ratings and the HomingCurrLimit HOME would default to.

HOME limits motor current during its search:
    HomingCurrLimit = min(XCURV, 0.5*XRMSM, 0.5*XRMSD)
If that is too low for the axis, the motor cannot move and the search stalls
immediately -- which looks identical to "the reference was never found".
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


VARS = ("XCURV", "XRMSM", "XRMSD", "XRMST")

header = f"{'var':<8}" + "".join(f"{'axis ' + str(a.index):>12}" for a in cfg.AXES)
print(header)
print("-" * len(header))
for v in VARS:
    row = f"{v:<8}"
    for a in cfg.AXES:
        try:
            row += f"{rr(v, a.index):>12.6g}"
        except Exception:
            row += f"{'n/a':>12}"
    print(row)

print("\ndefault HomingCurrLimit = min(XCURV, 0.5*XRMSM, 0.5*XRMSD):")
for a in cfg.AXES:
    try:
        xcurv, xrmsm, xrmsd = rr("XCURV", a.index), rr("XRMSM", a.index), rr("XRMSD", a.index)
        lim = min(xcurv, 0.5 * xrmsm, 0.5 * xrmsd)
        driver = ("XCURV" if lim == xcurv else
                  "0.5*XRMSM" if lim == 0.5 * xrmsm else "0.5*XRMSD")
        print(f"  axis {a.index} {a.name:<12} {lim:>10.4g}   (set by {driver})")
    except Exception as exc:
        print(f"  axis {a.index} {a.name:<12} read failed: {exc}")

sp.CloseComm(hc, True)
