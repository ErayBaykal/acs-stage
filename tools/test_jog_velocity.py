"""Check jog speeds are sane against the real machine's VEL and travel."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage import config as cfg  # noqa: E402
from acs_stage.controller import StageController  # noqa: E402

# (axis, VEL, SLLIMIT, SRLIMIT) from the machine.
MACHINE = [
    (0, 2.432e7, -742400.0, 2304000.0),
    (1, 2.432e7, 0.0, 2304000.0),
    (4, 10314.0, -2e14, 2e14),
    (5, 10314.0, -2e14, 2e14),
    (6, 10314.0, -23824.4, -243.62),
]

c = StageController()
for axis, vel, lo, hi in MACHINE:
    c._default_velocity[axis] = vel
    span = abs(hi - lo)
    if 0 < span < cfg.IMPLAUSIBLE_SPAN:
        c._travel_span[axis] = span

print(f"{'ax':<3} {'span':>12} {'old coarse':>14} {'new coarse':>12} "
      f"{'new fine':>10} {'traverse':>9}")
print("-" * 68)

failed = 0
for axis, vel, lo, hi in MACHINE:
    span = abs(hi - lo)
    old = vel * 0.25                      # what jogged the stage across in 0.5 s
    new = c.jog_velocity(axis)
    fine = c.jog_velocity(axis, fine=True)
    known = c._travel_span.get(axis)
    traverse = (known / new) if known and new else float("inf")
    print(f"{axis:<3} {span:>12.6g} {old:>14,.0f} {new:>12,.0f} "
          f"{fine:>10,.0f} {traverse:>8.1f}s")

    if new >= old:
        print(f"    FAIL: not slower than before")
        failed += 1
    if fine >= new:
        print(f"    FAIL: fine is not slower than coarse")
        failed += 1
    # An axis with a known span should take about the configured traverse time.
    if known and abs(traverse - cfg.JOG_TRAVERSE_SECONDS_COARSE) > 0.5:
        print(f"    FAIL: traverse {traverse:.1f}s != "
              f"{cfg.JOG_TRAVERSE_SECONDS_COARSE}s")
        failed += 1

print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
