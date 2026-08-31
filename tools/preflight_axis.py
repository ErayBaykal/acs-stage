"""Read-only pre-flight for one axis before its first homing attempt.

Usage:  python tools/preflight_axis.py 6
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
axis = int(sys.argv[1]) if len(sys.argv) > 1 else 6

FAULT_BITS = {"#RL": 0, "#LL": 1, "#NT": 2, "#HOT": 4, "#SRL": 5, "#SLL": 6,
              "#ENCNC": 7, "#DRIVE": 9, "#ENC": 10, "#PE": 12, "#CPE": 13}
MFLAG_BITS = {"#DUMMY": 0, "#OPEN": 1, "#HOME": 3, "#BRUSHL": 8, "#BRUSHOK": 9,
              "#INVENC": 12, "#INVDOUT": 13, "#LINEAR": 21, "#MODULO": 29}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")

cfgaxis = cfg.AXES_BY_INDEX.get(axis)
name = cfgaxis.name if cfgaxis else f"axis {axis}"
print(f"connected — pre-flight for axis {axis} ({name})\n")


def rr(n):
    return float(sp.ReadReal(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def ri(n):
    return int(sp.ReadInteger(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


mflags, fault, fmask = ri("MFLAGS"), ri("FAULT"), ri("FMASK")

print(f"MFLAGS = 0x{mflags:x}")
for n, b in MFLAG_BITS.items():
    print(f"    {n:<10} = {mflags >> b & 1}")

print(f"\nlimit switches (FAULT = 0x{fault:x}):")
for n in ("#RL", "#LL"):
    b = FAULT_BITS[n]
    print(f"    {n:<6} active={fault >> b & 1}   fault enabled={fmask >> b & 1}")

# FAULT shows the *condition*; FMASK decides whether it triggers a response.
# Reporting a masked condition as an "active fault" is misleading -- it looks
# like the limit is enforcing when it is doing nothing.
enforcing, masked = [], []
for n, b in FAULT_BITS.items():
    if n in ("#RL", "#LL") or not (fault >> b & 1):
        continue
    (enforcing if fmask >> b & 1 else masked).append(n)

print("\nfaults ENFORCING (condition true and unmasked):",
      ", ".join(enforcing) or "none")
print("conditions true but MASKED OFF (no effect):",
      ", ".join(masked) or "none")

print(f"\nFPOS      = {rr('FPOS'):.6g}")
print(f"RPOS      = {rr('RPOS'):.6g}")
print(f"VEL       = {rr('VEL'):.6g}")
print(f"CERRV     = {rr('CERRV'):.6g}")
print(f"SLLIMIT   = {rr('SLLIMIT'):.6g}")
print(f"SRLIMIT   = {rr('SRLIMIT'):.6g}")
print(f"MERR      = {ri('MERR')}")

if cfgaxis:
    print(f"\nconfigured homing: method {cfgaxis.homing_method} "
          f"({cfgaxis.homing_direction.name.lower()}), "
          f"can_home={cfgaxis.can_home}")
    if cfgaxis.homing_method == 17 and fault >> FAULT_BITS['#LL'] & 1:
        print("  !! negative limit is ALREADY active — method 17 aborts in "
              "this state. Jog off it first.")
    if cfgaxis.homing_method == 18 and fault >> FAULT_BITS['#RL'] & 1:
        print("  !! positive limit is ALREADY active — method 18 aborts in "
              "this state. Jog off it first.")

sp.CloseComm(hc, True)
print("\ndisconnected")
