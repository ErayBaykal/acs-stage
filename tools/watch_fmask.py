"""Watch FMASK / SLLIMIT / SRLIMIT for changes and timestamp them.

Read-only. Run this in its own terminal, then use the panel (or MMI) and see
exactly which action flips a mask bit.

Usage:  python tools/watch_fmask.py
"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
BITS = {"#RL": 0, "#LL": 1, "#SRL": 5, "#SLL": 6, "#PE": 12, "#CPE": 13}

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def ri(name, i):
    return int(sp.ReadInteger(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def rr(name, i):
    return float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def snapshot():
    out = {}
    for a in cfg.AXES:
        out[a.index] = (ri("FMASK", a.index),
                        rr("SLLIMIT", a.index),
                        rr("SRLIMIT", a.index))
    return out


def describe(fmask):
    return ",".join(n for n, b in BITS.items() if fmask >> b & 1) or "none"


print("watching FMASK / SLLIMIT / SRLIMIT on all configured axes.")
print("use the panel or MMI now — any change is timestamped below. Ctrl-C to stop.\n")

prev = snapshot()
for i, (fm, sll, srl) in sorted(prev.items()):
    print(f"  baseline axis {i}: FMASK=0x{fm:x} [{describe(fm)}] "
          f"SLLIMIT={sll:.6g} SRLIMIT={srl:.6g}")
print()

try:
    while True:
        cur = snapshot()
        for i in sorted(cur):
            if cur[i] == prev[i]:
                continue
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            ofm, osll, osrl = prev[i]
            nfm, nsll, nsrl = cur[i]
            print(f"[{ts}] axis {i} CHANGED")
            if ofm != nfm:
                print(f"    FMASK 0x{ofm:x} [{describe(ofm)}]")
                print(f"       -> 0x{nfm:x} [{describe(nfm)}]")
            if osll != nsll:
                print(f"    SLLIMIT {osll:.6g} -> {nsll:.6g}")
            if osrl != nsrl:
                print(f"    SRLIMIT {osrl:.6g} -> {nsrl:.6g}")
        prev = cur
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\nstopped")
finally:
    sp.CloseComm(hc, True)
