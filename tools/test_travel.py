"""Check the TravelRange maths that gates jog commands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage.travel import TravelRange  # noqa: E402

# Index-homed axis: BOTH ends are hard stops, and the homing index sits
# inside travel (axes 0 and 1 are like this).
r = TravelRange(axis=0, min_counts=-1000.0, max_counts=9000.0,
                min_reference="hard stop", max_reference="hard stop",
                measured_at="now", homed_zero=0.0)

span = r.span
lo, hi = r.safe_limits()          # margin at both ends: both are hard stops
print(f"span            = {span}")
print(f"safe_limits     = {lo} .. {hi}")

checks = [
    ("span", span, 10000.0),
    ("lo has margin", lo, -900.0),
    ("hi has margin", hi, 8900.0),
    # The homing index is inside travel, so it must be comfortably legal.
    ("homed index is legal", r.contains(0.0), True),
    ("travel below the index is usable", r.contains(-800.0), True),
    ("contains(-950)", r.contains(-950.0), False),
    ("contains(8950)", r.contains(8950.0), False),
    ("headroom(0,+1)", r.headroom(0.0, 1), 8900.0),
    ("headroom(0,-1)", r.headroom(0.0, -1), 900.0),
    ("headroom(8900,+1)", r.headroom(8900.0, 1), 0.0),
    ("headroom(8950,+1)", r.headroom(8950.0, 1), -50.0),
]

failed = 0
for name, got, want in checks:
    ok = abs(got - want) < 1e-9 if isinstance(want, float) else got == want
    print(f"  {'ok ' if ok else 'FAIL'} {name:<22} got={got!r:<12} want={want!r}")
    failed += not ok

# A jog is refused when headroom <= 0; confirm both ends refuse and the
# middle allows.
print("\njog gate (refused when headroom <= 0):")
for pos, direction, expect_refused in [
    (0.0, 1, False), (0.0, -1, False),
    (8900.0, 1, True), (8950.0, 1, True),
    (-900.0, -1, True), (-950.0, -1, True),
    (8900.0, -1, False),     # at the top, moving back down is fine
    (-900.0, 1, False),      # at the bottom, moving up is fine
]:
    refused = r.headroom(pos, direction) <= 0
    ok = refused == expect_refused
    print(f"  {'ok ' if ok else 'FAIL'} pos={pos:>8} dir={direction:+d} "
          f"refused={refused} expected={expect_refused}")
    failed += not ok

# Homed at the HIGH end, as axis 6 is: margin belongs at the low end.
print("\nhomed at the high end (axis 6 style):")
r6 = TravelRange(axis=6, min_counts=-24065.0, max_counts=-3.0,
                 min_reference="hard stop", max_reference="limit switch",
                 measured_at="now", homed_zero=-3.0)
lo6, hi6 = r6.safe_limits()
print(f"  safe_limits = {lo6:.1f} .. {hi6:.1f}")
for name, got, want in [
    ("homed_at_min", r6.homed_at_min, False),
    ("hi == homed end", hi6, -3.0),
    ("lo has margin", round(lo6, 2), round(-24065.0 + 240.62, 2)),
    ("home position is legal", r6.contains(-3.0), True),
]:
    ok = abs(got - want) < 1e-6 if isinstance(want, float) else got == want
    print(f"  {'ok ' if ok else 'FAIL'} {name:<24} got={got!r} want={want!r}")
    failed += not ok

print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
