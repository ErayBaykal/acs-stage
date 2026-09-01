"""Write a .spi application image back into the controller. No MMI needed.

This is the other half of backup_controller.py, and the dangerous half: it
overwrites the controller's parameters, buffers and commutation data with
whatever is in the file, then reboots. Get the wrong file and you have
reconfigured the machine.

So it does nothing by default. Without --apply it connects, analyses the file,
checks it against the controller in front of it, prints what would be written,
and exits.

    python tools\\restore_controller.py config\\backup\\application-2026-08-31.spi
    python tools\\restore_controller.py <file> --apply

Guards, each overridable only on purpose:

  serial number   The image records the controller it came from. Restoring to
                  a different one is legitimate -- a board swap, machine
                  duplication -- but it is never what you want by accident,
                  so it needs --any-serial.
  firmware        A mismatch is reported. Parameter sets are firmware
                  specific; loading 2.60 parameters onto a different version
                  is not guaranteed to mean the same thing.
  motion          Refuses while any configured axis is moving.

What this does NOT restore, because a .spi does not contain it:

  * controller firmware -- install that first, on a blank controller
  * the controller's own reachability. If the unit has been factory reset it
    will not be at 10.0.0.101 and this script cannot connect to it at all.
    Reach it at its default address first (MMI or the Upgrader).
  * commutation and homing at runtime. The image carries the adjuster
    calibration, but the encoders are incremental: every power-up still needs
    commutation and homing before the machine can be trusted to move.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1

GROUPS = {
    "buffers": ("ACSPL",),
    "parameters": ("PAR",),
    "commutation": ("ADJ",),
    "variables": ("I", "V"),
}


def text(s):
    """ACSC_APPSL_STRING wraps its text in .string; plain str passes through."""
    return getattr(s, "string", s)


def group_of(filename):
    stem = text(filename).split(".")[0]
    for name, prefixes in GROUPS.items():
        for p in prefixes:
            if stem == p or (stem.startswith(p) and stem[len(p):].isdigit()) \
                    or stem == p + "_e":
                return name
    return "other"


# How long to watch FPOS, and how much drift still counts as stationary.
#
# Measured on this machine with every axis enabled and holding: the linear
# axes dither 1-2 counts and the rotation axes 0-2, so an exact-equality test
# reports a parked stage as moving. 10 counts is five times the observed
# dither and still an order of magnitude below any real jog -- the slowest
# configured one, axis 6 fine at 431 cts/s, covers ~172 counts in this window.
SETTLE_S = 0.4
STATIONARY_COUNTS = 10.0


def moving_axes(hc):
    """Which axes are actually moving.

    Sampled from FPOS rather than MST.#MOVE: that bit has been seen stuck at 1
    on this controller while the axis was demonstrably stationary, and a guard
    that cannot be satisfied is a guard people learn to bypass.
    """
    def fpos():
        return [float(sp.ReadReal(hc, NONE, "FPOS", a.index, a.index,
                                  NONE, NONE, sp.SYNCHRONOUS, True))
                for a in cfg.AXES]

    before = fpos()
    time.sleep(SETTLE_S)
    after = fpos()
    return [a.index for a, b, c in zip(cfg.AXES, before, after)
            if abs(c - b) > STATIONARY_COUNTS]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="the .spi file to load")
    ap.add_argument("--apply", action="store_true",
                    help="actually write to the controller (default: dry run)")
    ap.add_argument("--only", metavar="GROUP", action="append", default=[],
                    choices=sorted(GROUPS) + ["other"],
                    help="restore only these groups (repeatable): "
                         + ", ".join(sorted(GROUPS)))
    ap.add_argument("--any-serial", action="store_true",
                    help="allow restoring to a different controller")
    ap.add_argument("--no-reboot", action="store_true",
                    help="skip the reboot; parameters will not take effect "
                         "until the controller restarts")
    args = ap.parse_args()

    path = Path(args.image).resolve()
    if not path.exists():
        return f"no such file: {path}"

    hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
    if hc == -1:
        return (f"could not connect to {cfg.CONTROLLER_HOST}. A factory-reset "
                f"controller will not be at this address -- reach it at its "
                f"default one first.")

    try:
        return run(hc, path, args)
    finally:
        sp.CloseComm(hc, True)


def run(hc, path, args):
    firmware = sp.GetFirmwareVersion(hc, 256, sp.SYNCHRONOUS, True)
    serial = sp.GetSerialNumber(hc, 256, sp.SYNCHRONOUS, True)
    print(f"controller {cfg.CONTROLLER_HOST}  firmware {firmware}  "
          f"serial {serial}")

    info = sp.AnalyzeApplication(hc, str(path), sp.SYNCHRONOUS, True)
    try:
        return load(hc, path, info, firmware, serial, args)
    finally:
        sp.FreeApplication(info, True)


def load(hc, path, info, firmware, serial, args):
    attrs = {text(a.key): text(a.value) for a in info.attributes}
    print(f"image      {path.name}  firmware {attrs.get('Firmware', '?')}  "
          f"serial {attrs.get('Controller serial number', '?')}  "
          f"saved {attrs.get('Date', '?')}")

    if info.ErrCode:
        return f"the image reports error {info.ErrCode}; refusing to load it"

    bad = [text(s.filename) for s in info.sections if s.error]
    if bad:
        return f"sections failed to analyse: {', '.join(bad)}"

    # -- guards ----------------------------------------------------------
    img_serial = attrs.get("Controller serial number", "")
    if img_serial and img_serial != serial and not args.any_serial:
        return (f"image is from controller {img_serial}, this is {serial}. "
                f"Pass --any-serial if that is deliberate (board swap, "
                f"duplicating a machine).")

    img_fw = attrs.get("Firmware", "")
    if img_fw and img_fw != firmware:
        print(f"\n  WARNING: image firmware {img_fw} != controller {firmware}. "
              f"Parameter sets are firmware specific.")

    moving = moving_axes(hc)
    if moving:
        return f"axes {moving} are moving; stop them before restoring"

    # -- selection -------------------------------------------------------
    selected = 0
    by_group = {}
    for s in info.sections:
        group = group_of(s.filename)
        wanted = (not args.only) or (group in args.only)
        s.inuse = 1 if wanted else 0
        selected += wanted
        stats = by_group.setdefault(group, [0, 0, 0])
        stats[0] += 1
        if wanted:
            stats[1] += 1
            stats[2] += s.size

    print(f"\n{'group':<14}{'selected':>10}{'of':>5}{'bytes':>12}")
    print("-" * 41)
    for group, (total, chosen, size) in sorted(by_group.items()):
        print(f"{group:<14}{chosen:>10}{total:>5}{size:>12,}")

    if not selected:
        return "nothing selected"

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to load this "
              "into the controller.")
        return 0

    # -- write -----------------------------------------------------------
    print(f"\nloading {selected} section(s) into the controller...")
    sp.LoadApplication(hc, str(path), info, sp.SYNCHRONOUS, True)
    print("loaded")

    failed = [text(s.filename) for s in info.sections if s.inuse and s.error]
    if failed:
        return f"sections reported errors after load: {', '.join(failed)}"

    if args.no_reboot:
        print("\nNOT rebooting (--no-reboot). Parameters do not take effect "
              "until the controller restarts.")
        return 0

    print("rebooting the controller (this drops the connection)...")
    sp.ControllerReboot(hc, 60000, True)
    print("rebooted")

    print("\nThe encoders are incremental: commutate and home every axis "
          "before trusting any motion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
