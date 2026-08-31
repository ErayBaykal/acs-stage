"""Live limit-switch monitor. Read-only — issues no motion.

Usage:  python tools/watch_limits.py 6
Trigger each switch by hand and confirm the indicator flips. Ctrl-C to stop.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
axis = int(sys.argv[1]) if len(sys.argv) > 1 else 6
RL_BIT, LL_BIT = 0, 1

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")

name = cfg.AXES_BY_INDEX[axis].name if axis in cfg.AXES_BY_INDEX else f"axis {axis}"
print(f"watching axis {axis} ({name}) — trigger each switch by hand, Ctrl-C to stop\n")

seen_rl = seen_ll = False
try:
    while True:
        fault = int(sp.ReadInteger(hc, NONE, "FAULT", axis, axis, NONE, NONE,
                                   sp.SYNCHRONOUS, True))
        pos = float(sp.ReadReal(hc, NONE, "FPOS", axis, axis, NONE, NONE,
                                sp.SYNCHRONOUS, True))
        rl, ll = fault >> RL_BIT & 1, fault >> LL_BIT & 1
        seen_rl, seen_ll = seen_rl or bool(rl), seen_ll or bool(ll)
        print(f"\r  FPOS {pos:>12.6g}   "
              f"#LL(neg) {'[TRIGGERED]' if ll else '     ...   '}   "
              f"#RL(pos) {'[TRIGGERED]' if rl else '     ...   '}   "
              f"| seen so far: LL={'Y' if seen_ll else 'n'} "
              f"RL={'Y' if seen_rl else 'n'}",
              end="", flush=True)
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\n")
    print(f"negative limit (#LL) responded: {'YES' if seen_ll else 'NOT SEEN'}")
    print(f"positive limit (#RL) responded: {'YES' if seen_rl else 'NOT SEEN'}")
    if not seen_ll:
        print("\n  !! #LL never triggered. Homing method 17 searches for this "
              "switch;\n     if it is not wired the stage will rotate "
              "indefinitely.")
finally:
    sp.CloseComm(hc, True)
