"""Check the search-speed cap actually makes hard-stop detection possible."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage.calibrate import (DETECTION_SAMPLES, PE_STOP_FRACTION,  # noqa: E402
                                 POLL_INTERVAL_S, search_velocity)

print(f"poll = {POLL_INTERVAL_S*1000:.0f} ms, want >= {DETECTION_SAMPLES} samples "
      f"between {PE_STOP_FRACTION:.0%} of CERRV and CERRV\n")

# (name, CERRV, VEL) taken from the real machine.
CASES = [
    ("axis 0 Linear X", 2500.0, 2.432e7),
    ("axis 1 Linear Y", 2500.0, 2.432e7),
    ("axis 6 Rotation C", 750.0, 10314.0),
]

failed = 0
for name, cerrv, vel in CASES:
    jog_fine = vel * 0.05          # what the old code used
    v = search_velocity(cerrv, jog_fine)
    margin = cerrv * (1 - PE_STOP_FRACTION)
    samples_old = margin / jog_fine / POLL_INTERVAL_S
    samples_new = margin / v / POLL_INTERVAL_S
    ok = samples_new >= DETECTION_SAMPLES - 1e-9
    failed += not ok
    print(f"{name}")
    print(f"   CERRV={cerrv:.0f}  margin={margin:.0f}  old jog={jog_fine:,.0f} cts/s")
    print(f"   old: {samples_old:8.2f} samples  {'(undetectable)' if samples_old < 1 else ''}")
    print(f"   new: {samples_new:8.2f} samples at {v:,.0f} cts/s   {'ok' if ok else 'FAIL'}")
    # The cap must never speed an axis UP beyond its jog velocity.
    if v > jog_fine + 1e-9:
        print("   FAIL: cap exceeded the requested jog velocity")
        failed += 1
    print()

# A slow axis should keep its own velocity rather than be capped upward.
v = search_velocity(750.0, 515.0)
print(f"slow axis keeps its speed: {v:.1f} (expected 515.0)")
failed += abs(v - 515.0) > 1e-9

print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
