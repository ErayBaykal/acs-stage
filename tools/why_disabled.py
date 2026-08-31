"""Why did an axis disable? Reads MERR and the fault response config.

Read-only.  Usage: python tools/why_disabled.py 0
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
axis = int(sys.argv[1]) if len(sys.argv) > 1 else 0

BITS = {"#RL": 0, "#LL": 1, "#NT": 2, "#HOT": 4, "#SRL": 5, "#SLL": 6,
        "#ENCNC": 7, "#DRIVE": 9, "#ENC": 10, "#PE": 12, "#CPE": 13,
        "#VL": 14, "#AL": 15, "#CL": 16, "#SP": 17, "#STO": 18}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def ri(n, i=axis):
    return int(sp.ReadInteger(hc, NONE, n, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def rr(n, i=axis):
    return float(sp.ReadReal(hc, NONE, n, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


merr = ri("MERR")
print(f"axis {axis}")
print(f"  MERR = {merr}" + (f"  ({sp.GetErrorString(hc, merr)})" if merr else ""))
print(f"  MST.#ENABLED = {ri('MST') & 1}")

fault, fmask, fdef = ri("FAULT"), ri("FMASK"), ri("FDEF")
print(f"\n  {'bit':<8} {'active':>7} {'enabled':>8} {'default resp':>13}")
for name, b in BITS.items():
    a, e, d = fault >> b & 1, fmask >> b & 1, fdef >> b & 1
    if a or (e and d):
        print(f"  {name:<8} {a:>7} {e:>8} {d:>13}")

print(f"\n  position error thresholds:")
for v in ("ERRI", "ERRV", "ERRA", "CERRI", "CERRV", "CERRA"):
    try:
        print(f"    {v:<6} = {rr(v):>10.6g}")
    except Exception as exc:
        print(f"    {v:<6} : {exc}")

print(f"\n  PE = {rr('PE'):.6g}   FPOS = {rr('FPOS'):.6g}   RPOS = {rr('RPOS'):.6g}")

sp.CloseComm(hc, True)
