"""Compare far-end search speed before and after basing it on homing velocity."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage import config as cfg  # noqa: E402
from acs_stage.calibrate import search_velocity  # noqa: E402
from acs_stage.controller import StageController  # noqa: E402

# (axis, VEL, CERRV, measured span or None)
MACHINE = [
    (0, 2.432e7, 2500.0, 5123200.0),
    (1, 2.432e7, 2500.0, 2947091.0),
    (4, 10314.0, 750.0, None),
    (5, 10314.0, 750.0, None),
    (6, 10314.0, 750.0, 24928.0),
]

c = StageController()
for axis, vel, cerrv, span in MACHINE:
    c._default_velocity[axis] = vel
    if span:
        c._travel_span[axis] = span

print(f"{'ax':<3} {'homing v':>10} {'old (fine jog)':>15} {'new':>10} "
      f"{'speedup':>8} {'traverse':>10}")
print("-" * 62)

failed = 0
for axis, vel, cerrv, span in MACHINE:
    homing = c.homing_velocity(axis)
    old = search_velocity(cerrv, c.jog_velocity(axis, fine=True))
    new = search_velocity(cerrv, homing or c.jog_velocity(axis, fine=True))
    speedup = new / old if old else float("inf")
    known = span or 25000.0
    print(f"{axis:<3} {homing or 0:>10,.0f} {old:>15,.0f} {new:>10,.0f} "
          f"{speedup:>7.1f}x {known / new:>9.0f}s")
    if new < old:
        print("    FAIL: new search is slower than before")
        failed += 1
    # Detection must still be possible: enough samples before #CPE.
    from acs_stage.calibrate import (DETECTION_SAMPLES, PE_STOP_FRACTION,
                                     POLL_INTERVAL_S)
    margin = cerrv * (1 - PE_STOP_FRACTION)
    samples = margin / new / POLL_INTERVAL_S
    if samples < DETECTION_SAMPLES - 1e-9:
        print(f"    FAIL: only {samples:.2f} detection samples")
        failed += 1

print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
