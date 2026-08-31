"""Controller uptime and volatile-state check. Read-only.

TIME is milliseconds since power-up, so a small value means the controller
restarted recently -- which would explain volatile settings reverting to
their flash values.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")

ms = float(sp.ReadReal(hc, NONE, "TIME", NONE, NONE, NONE, NONE,
                       sp.SYNCHRONOUS, True))
print(f"TIME = {ms:,.0f} ms  -> controller has been up {timedelta(milliseconds=ms)}")

print("\nvolatile state that a restart would clear:")
print(f"{'ax':<3} {'name':<12} {'#HOME':>6} {'#BRUSHOK':>9} {'SLLIMIT':>12} {'SRLIMIT':>12}")
for a in cfg.AXES:
    i = a.index
    m = int(sp.ReadInteger(hc, NONE, "MFLAGS", i, i, NONE, NONE, sp.SYNCHRONOUS, True))
    sll = float(sp.ReadReal(hc, NONE, "SLLIMIT", i, i, NONE, NONE, sp.SYNCHRONOUS, True))
    srl = float(sp.ReadReal(hc, NONE, "SRLIMIT", i, i, NONE, NONE, sp.SYNCHRONOUS, True))
    print(f"{i:<3} {a.name:<12} {m >> cfg.MFLAGS_HOME_BIT & 1:>6} "
          f"{m >> cfg.MFLAGS_BRUSHOK_BIT & 1:>9} {sll:>12.6g} {srl:>12.6g}")

print("\nwatchdog globals (gone if the controller restarted):")
for name in ("HOSTWDEN", "HOSTWDOG", "HOSTWDTMO", "HOSTWDFIRED"):
    try:
        v = int(sp.ReadInteger(hc, NONE, name, NONE, NONE, NONE, NONE,
                               sp.SYNCHRONOUS, True))
        print(f"  {name:<12} = {v}")
    except Exception as exc:
        print(f"  {name:<12} : {str(exc).splitlines()[-1]}")

sp.CloseComm(hc, True)
