"""Show why an axis thinks it is busy, and optionally clear it.

Usage:  python tools/motion_state.py <axis> [--stop]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
axis = int(sys.argv[1]) if len(sys.argv) > 1 else 6
stop = "--stop" in sys.argv

MST_BITS = {"#ENABLED": 0, "#OPEN": 1, "#INPOS": 4, "#MOVE": 5, "#ACC": 6}
AST_BITS = {"#MOVE": 5, "#ACC": 6, "#SEGMENT": 7, "#VELLOCK": 8,
            "#POSLOCK": 9, "#DC": 22, "#INHOMING": 25}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def ri(n):
    return int(sp.ReadInteger(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def rr(n):
    return float(sp.ReadReal(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def report(label):
    mst, ast = ri("MST"), ri("AST")
    print(f"\n{label}")
    print(f"  FPOS={rr('FPOS'):.1f}  RPOS={rr('RPOS'):.1f}  PE={rr('PE'):.1f}  "
          f"MERR={ri('MERR')}")
    print(f"  MST=0x{mst:x}  " +
          " ".join(f"{n}={mst >> b & 1}" for n, b in MST_BITS.items()))
    print(f"  AST=0x{ast:x}  " +
          " ".join(f"{n}={ast >> b & 1}" for n, b in AST_BITS.items()))


report("before")

if stop:
    print("\nstopping: HALT -> KILL -> wait")
    try:
        sp.Halt(hc, axis, sp.SYNCHRONOUS, True)
    except Exception as exc:
        print("  halt:", str(exc).splitlines()[-1])
    time.sleep(1.0)
    if ri("AST") >> 25 & 1 or ri("MST") >> 5 & 1:
        try:
            sp.Kill(hc, axis, sp.SYNCHRONOUS, True)
        except Exception as exc:
            print("  kill:", str(exc).splitlines()[-1])
        time.sleep(1.5)
    try:
        sp.FaultClear(hc, axis, sp.SYNCHRONOUS, True)
    except Exception:
        pass
    time.sleep(0.5)
    report("after")

sp.CloseComm(hc, True)
