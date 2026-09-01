"""Where the package finds its files, running from source or frozen.

Two different questions, which are the same directory from source and are not
once PyInstaller is involved:

  bundled()  read-only assets shipped with the code, e.g. watchdog.prg.
             Frozen, these are unpacked into a temporary directory.

  data()     state the program writes and expects to find next time, i.e. the
             travel calibration. Frozen, this must NOT be the temporary
             directory -- it is deleted on exit, so a calibration written
             there would be silently lost and the next run would come up with
             no travel limits at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parent.parent


def frozen() -> bool:
    return getattr(sys, "frozen", False)


def bundled() -> Path:
    """Root for read-only files shipped alongside the code."""
    if frozen():
        # onefile unpacks here; onedir has no _MEIPASS and sits by the exe.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return _SOURCE_ROOT


def data() -> Path:
    """Root for files the program writes and re-reads on a later run."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT
