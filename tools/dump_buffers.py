"""Dump every ACSPL+ program buffer and flag anything that writes limits.

Read-only. Looking for whatever is restoring SLLIMIT/SRLIMIT to 200.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

SUSPECT = re.compile(r"\b(SLLIMIT|SRLIMIT|FMASK|SAFETYCONF|FDEF|SLPMIN|SLPMAX)\b",
                     re.I)

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")
print(f"connected to {cfg.CONTROLLER_HOST}\n")

outdir = Path(__file__).resolve().parent.parent / "acspl" / "controller_dump"
outdir.mkdir(parents=True, exist_ok=True)

hits = []
for buf in range(16):
    try:
        text = sp.UploadBuffer(hc, buf, 0, 64000, sp.SYNCHRONOUS, True) or ""
    except Exception as exc:
        print(f"buffer {buf:>2}: <could not read: {exc}>")
        continue

    stripped = text.strip()
    if not stripped:
        print(f"buffer {buf:>2}: empty")
        continue

    lines = stripped.splitlines()
    (outdir / f"buffer_{buf:02d}.prg").write_text(text, encoding="utf-8")
    first = lines[0][:60]
    print(f"buffer {buf:>2}: {len(lines):>4} lines   {first!r}")

    for n, line in enumerate(lines, 1):
        if SUSPECT.search(line) and not line.strip().startswith("!"):
            hits.append((buf, n, line.strip()))

print(f"\nsaved to {outdir}")

if hits:
    print("\n*** lines touching limits / fault config ***")
    for buf, n, line in hits:
        print(f"  buffer {buf} line {n}: {line}")
else:
    print("\nno buffer references SLLIMIT/SRLIMIT/FMASK/SAFETYCONF/FDEF")

sp.CloseComm(hc, True)
