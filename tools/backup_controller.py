"""Capture everything the controller holds in flash, into the repo.

The flash is the only copy of the axis tuning, the fault configuration and the
ACSPL+ buffers. Lose it -- a failed firmware update, a board swap, a mis-click
in the MMI -- and it is all gone, including work that took days to establish.
This writes it somewhere version control can see.

Three kinds of output, and they are NOT interchangeable:

  parameters.txt/.json  Readable snapshot. Diffs in git, so you can see what
                        changed between runs and when. NOT a restore image --
                        it is a curated list of parameters, not everything.

  buffers/*.prg         The ACSPL+ programs, as text. Directly reloadable.

  application.spi       The authoritative restore image: axis parameters,
                        system parameters, buffers, user variables, adjuster
                        (commutation) data, user files. Binary, so git stores
                        it but cannot diff it. This is what you restore from.

The .spi is opt-in (--spi) because saving one writes controller flash before
copying flash to the file, and flash is rated around 100k cycles. Fine to run
deliberately; not something to put on a timer.

Read-only otherwise: issues no motion and changes no parameter.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1

# Per-axis parameters worth keeping. Names this firmware does not implement
# are skipped and listed at the end, so the list can be generous without the
# tool failing on an unsupported one.
AXIS_REAL = [
    # motion profile
    "VEL", "ACC", "DEC", "JERK", "KDEC", "XVEL", "XACC", "XDEC", "XJERK",
    # travel limits
    "SLLIMIT", "SRLIMIT",
    # position error thresholds -- CERRV is what hard-stop homing detects on
    "CERRV", "CERRI", "CERRA", "ERRV", "ERRI", "ERRA",
    # encoder scaling
    "EFAC", "EOFFS",
    # current ratings -- these set the default homing current limit
    "XCURV", "XCURI", "XRMSM", "XRMSD", "XRMST", "XRMSTL",
    # servo loop
    "SLPKP", "SLPKI", "SLVKP", "SLVKI", "SLVLI", "SLAFF", "SLVFF",
    "SLVSOF", "SLVNFRQ", "SLVNWID", "SLVNATT", "SLIKP",
    # commutation
    "CFACTOR", "SLCOFFS",
]

AXIS_INT = [
    "MFLAGS",    # incl. #INVDOUT, which decides homing direction
    "FMASK",     # which faults are enabled
    "FDEF",      # what each fault does
    "SAFINI", "IFLAGS", "NFLAGS", "AFLAGS",
    "ENCTYPE", "E1TYPE", "E2TYPE",
]

# Controller-wide values.
SYSTEM_INT = ["HOSTWDEN", "HOSTWDTMO", "HOSTWDOG", "HOSTWDFIRED"]

# Bit fields. Printed as hex, because these are the parameters where a single
# changed bit matters and a decimal (or worse, a 6-digit float) hides it.
BITMASKS = {"MFLAGS", "FMASK", "FDEF", "IFLAGS", "NFLAGS", "AFLAGS"}

# The MFLAGS bits this machine actually depends on, decoded into words so a
# diff reads as "#INVDOUT went 1 -> 0" rather than as two large numbers.
# #INVDOUT in particular has silently reverted in flash and sent an axis
# homing into the wrong end of its travel.
MFLAGS_BITS = {
    cfg.MFLAGS_HOME_BIT: "#HOME",
    cfg.MFLAGS_BRUSHL_BIT: "#BRUSHL",
    cfg.MFLAGS_BRUSHOK_BIT: "#BRUSHOK",
    cfg.MFLAGS_INVDOUT_BIT: "#INVDOUT",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="config/backup",
                    help="output directory (default: config/backup)")
    ap.add_argument("--spi", action="store_true",
                    help="also save the .spi application image "
                         "(writes controller flash -- see above)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    outdir = root / args.out
    (outdir / "buffers").mkdir(parents=True, exist_ok=True)

    hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
    if hc == -1:
        return f"could not connect to {cfg.CONTROLLER_HOST}"
    print(f"connected to {cfg.CONTROLLER_HOST}\n")

    try:
        snapshot, missing = read_parameters(hc)
        write_parameters(outdir, snapshot, missing)
        dump_buffers(hc, outdir / "buffers")
        if args.spi:
            save_spi(hc, outdir)
        else:
            print("\n.spi image NOT saved -- pass --spi to include it. "
                  "It is the only true restore image.")
    finally:
        sp.CloseComm(hc, True)

    print(f"\nwritten to {outdir}")
    return 0


# -- readable snapshot ---------------------------------------------------

def read_parameters(hc):
    """Read every known parameter. Unsupported names are collected, not fatal."""
    missing = set()

    def read(fn, name, index):
        try:
            return fn(hc, NONE, name, index, index, NONE, NONE,
                      sp.SYNCHRONOUS, True)
        except Exception:
            missing.add(name)
            return None

    axes = {}
    for a in cfg.AXES:
        values = {}
        for name in AXIS_REAL:
            v = read(sp.ReadReal, name, a.index)
            if v is not None:
                values[name] = float(v)
        for name in AXIS_INT:
            v = read(sp.ReadInteger, name, a.index)
            if v is not None:
                values[name] = int(v)
        axes[a.index] = {"name": a.name, "values": values}
        print(f"axis {a.index} {a.name:<12} {len(values)} parameters")

    system = {}
    for name in SYSTEM_INT:
        try:
            system[name] = int(sp.ReadInteger(hc, NONE, name, NONE, NONE,
                                              NONE, NONE, sp.SYNCHRONOUS, True))
        except Exception:
            missing.add(name)

    return {"read_at": datetime.now().isoformat(timespec="seconds"),
            "controller": f"{cfg.CONTROLLER_HOST}:{cfg.CONTROLLER_PORT}",
            "axes": axes, "system": system}, missing


def format_value(name, value):
    """One cell of the table, formatted so a change is visible in a diff."""
    if value is None:
        return "-"
    if name in BITMASKS:
        # Read back signed; mask to 32 bits so the hex reads as the bit
        # pattern it is rather than as a negative number.
        return f"0x{value & 0xFFFFFFFF:08x}"
    if isinstance(value, int):
        return f"{value:,}"
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"      # whole-numbered reals: no 2.5e+03
    return f"{value:,.6g}"


def write_parameters(outdir, snapshot, missing):
    (outdir / "parameters.json").write_text(
        json.dumps(snapshot, indent=2), encoding="utf-8")

    # A text table as well: JSON diffs are readable, but a table shows one
    # axis drifting away from its neighbours at a glance.
    names = []
    for entry in snapshot["axes"].values():
        for n in entry["values"]:
            if n not in names:
                names.append(n)

    indices = list(snapshot["axes"])
    lines = [
        f"# controller {snapshot['controller']}",
        f"# read at    {snapshot['read_at']}",
        "#",
        "# Readable snapshot for diffing. NOT a restore image -- use the .spi.",
        "",
        f"{'parameter':<12}" + "".join(f"{'axis ' + str(i):>18}"
                                       for i in indices),
        f"{'':12}" + "".join(f"{snapshot['axes'][i]['name']:>18}"
                             for i in indices),
        "-" * (12 + 18 * len(indices)),
    ]
    for name in names:
        row = f"{name:<12}"
        for i in indices:
            row += f"{format_value(name, snapshot['axes'][i]['values'].get(name)):>18}"
        lines.append(row)

    lines += ["", "MFLAGS decoded:"]
    for i in indices:
        mflags = snapshot["axes"][i]["values"].get("MFLAGS")
        if mflags is None:
            continue
        bits = " ".join(f"{label}={mflags >> bit & 1}"
                        for bit, label in sorted(MFLAGS_BITS.items()))
        lines.append(f"  axis {i} {snapshot['axes'][i]['name']:<12} {bits}")

    if snapshot["system"]:
        lines += ["", "system:"]
        lines += [f"  {k:<12} = {v}" for k, v in snapshot["system"].items()]
    if missing:
        lines += ["", "not implemented on this firmware:",
                  "  " + ", ".join(sorted(missing))]

    (outdir / "parameters.txt").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")
    print(f"\n{len(names)} parameters captured per axis"
          + (f", {len(missing)} names unsupported" if missing else ""))


# -- ACSPL+ buffers ------------------------------------------------------

def dump_buffers(hc, outdir):
    print()
    kept = 0
    for buf in range(16):
        try:
            text = sp.UploadBuffer(hc, buf, 0, 64000, sp.SYNCHRONOUS, True) or ""
        except Exception as exc:
            print(f"buffer {buf:>2}: could not read: {exc}")
            continue
        path = outdir / f"buffer_{buf:02d}.prg"
        if not text.strip():
            # Remove rather than skip, so a buffer that was emptied on the
            # controller shows up as a deletion in git instead of going stale.
            path.unlink(missing_ok=True)
            continue
        path.write_text(text, encoding="utf-8")
        kept += 1
        print(f"buffer {buf:>2}: {len(text.splitlines()):>4} lines")
    print(f"{kept} non-empty buffer(s) saved")


# -- .spi application image ---------------------------------------------

def save_spi(hc, outdir):
    stamp = datetime.now().strftime("%Y-%m-%d")
    path = outdir / f"application-{stamp}.spi"
    print(f"\nsaving application image -> {path.name}")
    print("  (this writes controller flash, then copies flash to the file)")
    info = sp.AnalyzeApplication(hc, None, sp.SYNCHRONOUS, True)
    try:
        sp.SaveApplication(hc, str(path), info, sp.SYNCHRONOUS, True)
    finally:
        sp.FreeApplication(info, True)
    size = path.stat().st_size if path.exists() else 0
    print(f"  {size:,} bytes")


if __name__ == "__main__":
    sys.exit(main())
