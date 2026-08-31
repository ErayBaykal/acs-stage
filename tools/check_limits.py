"""Read-only check of software travel limits and their fault enables."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1

# FAULT / FMASK bit positions (ACSPL+ reference guide, Table 5-19).
BITS = {"#RL": 0, "#LL": 1, "#SRL": 5, "#SLL": 6, "#PE": 12, "#CPE": 13}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")
print(f"connected to {cfg.CONTROLLER_HOST}\n")


def rr(name, i):
    return float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def ri(name, i):
    return int(sp.ReadInteger(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


print(f"{'ax':<3} {'name':<12} {'SLLIMIT':>12} {'SRLIMIT':>12} {'RPOS':>12} "
      f"{'in range':>9}   enabled faults / active now")
print("-" * 110)
for a in cfg.AXES:
    i = a.index
    try:
        fmask, fault = ri("FMASK", i), ri("FAULT", i)
        sll, srl, rpos = rr("SLLIMIT", i), rr("SRLIMIT", i), rr("RPOS", i)
        enabled = [n for n, b in BITS.items() if fmask >> b & 1]
        active = [n for n, b in BITS.items() if fault >> b & 1]
        in_range = "yes" if sll <= rpos <= srl else "** NO **"
        print(f"{i:<3} {a.name:<12} {sll:>12.6g} {srl:>12.6g} {rpos:>12.6g} "
              f"{in_range:>9}   {','.join(enabled) or '-':<28} "
              f"active: {','.join(active) or 'none'}")
    except Exception as exc:
        print(f"{i:<3} {a.name:<12}  read failed: {exc}")

sp.CloseComm(hc, True)
print("\ndisconnected")
