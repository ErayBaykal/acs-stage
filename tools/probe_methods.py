"""Probe which HOME methods this firmware supports, without moving anything.

With the motor DISABLED a HOME command cannot produce motion. The error tells
us how far through validation the firmware got:

    3314 -> method rejected           (not supported)
    3254 -> requires motor enabled    (method ACCEPTED)

Usage: python tools/probe_methods.py 0
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
axis = int(sys.argv[1]) if len(sys.argv) > 1 else 0
METHODS = [1, 2, 17, 18, 33, 34, 37, 50, 51, 52, 53]

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")

enabled = int(sp.ReadInteger(hc, NONE, "MST", axis, axis, NONE, NONE,
                             sp.SYNCHRONOUS, True)) & 1
if enabled:
    sp.CloseComm(hc, True)
    sys.exit(f"axis {axis} is ENABLED — disable it first so this cannot move "
             f"the stage.")

print(f"axis {axis} is disabled; probing HOME methods (no motion possible)\n")
for method in METHODS:
    cmd = f"HOME {axis},{method}\r"
    try:
        reply = (sp.Transaction(hc, cmd, len(cmd), 1024, sp.SYNCHRONOUS, True)
                 or "").strip()
    except Exception as exc:
        reply = str(exc).splitlines()[-1]
    code = None
    if reply.startswith("?"):
        try:
            code = int(reply.lstrip("?").strip())
        except ValueError:
            pass
    if code == 3254:
        verdict = "SUPPORTED (needs motor enabled)"
    elif code == 3314:
        verdict = "NOT SUPPORTED"
    else:
        verdict = f"unexpected: {reply}"
    label = sp.GetErrorString(hc, code) if code else ""
    print(f"  method {method:>3}: {verdict:<34} {label}")

sp.CloseComm(hc, True)
