"""Read-only controller state dump. Issues no motion and writes nothing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit(f"could not connect to {cfg.CONTROLLER_HOST}")
print(f"connected to {cfg.CONTROLLER_HOST}\n")


def rd_int(name, i):
    return int(sp.ReadInteger(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


def rd_real(name, i):
    return float(sp.ReadReal(hc, NONE, name, i, i, NONE, NONE, sp.SYNCHRONOUS, True))


print(f"{'ax':<3} {'name':<12} {'FPOS':>12} {'en':>3} {'mov':>4} {'comm':>5} "
      f"{'home':>5} {'IND':>4} {'MERR':>6} {'FAULT':>10}")
print("-" * 78)
for a in cfg.AXES:
    i = a.index
    try:
        mst, mfl = rd_int("MST", i), rd_int("MFLAGS", i)
        ist, merr, flt = rd_int("IST", i), rd_int("MERR", i), rd_int("FAULT", i)
        print(f"{i:<3} {a.name:<12} {rd_real('FPOS', i):>12.1f} "
              f"{mst >> cfg.MST_ENABLED_BIT & 1:>3} "
              f"{mst >> cfg.MST_MOVE_BIT & 1:>4} "
              f"{mfl >> cfg.MFLAGS_BRUSHOK_BIT & 1:>5} "
              f"{mfl >> cfg.MFLAGS_HOME_BIT & 1:>5} "
              f"{ist & 1:>4} {merr:>6} {flt:>#10x}")
    except Exception as exc:
        print(f"{i:<3} {a.name:<12}  read failed: {exc}")

print("\nwatchdog globals:")
for name in ("HOSTWDEN", "HOSTWDOG", "HOSTWDTMO", "HOSTWDFIRED"):
    try:
        v = int(sp.ReadInteger(hc, NONE, name, NONE, NONE, NONE, NONE,
                               sp.SYNCHRONOUS, True))
        print(f"  {name:<12} = {v}")
    except Exception as exc:
        print(f"  {name:<12} : {exc}")

print("\nMERR decode (non-zero only):")
for a in cfg.AXES:
    try:
        merr = rd_int("MERR", a.index)
        if merr:
            print(f"  axis {a.index}: {merr} - {sp.GetErrorString(hc, merr)}")
    except Exception:
        pass

sp.CloseComm(hc, True)
print("\ndisconnected")
