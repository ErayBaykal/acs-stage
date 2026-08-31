"""Show gamepad speed at each stick deflection, per axis.

Combines the response curve with the gamepad speed factor and the axis
capability cap, so the actual feel can be checked without the hardware.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage import config as cfg  # noqa: E402
from acs_stage.controller import StageController  # noqa: E402
from acs_stage.travel import TravelStore  # noqa: E402

# Real machine values.
VEL = {0: 2.432e7, 1: 2.432e7, 4: 10314.0, 5: 10314.0, 6: 10314.0}

c = StageController()
store = TravelStore()
for axis, vel in VEL.items():
    c._default_velocity[axis] = vel
    r = store.get(axis)
    if r:
        c._travel_span[axis] = abs(r.span)

print(f"response exponent {cfg.GAMEPAD_RESPONSE_EXPONENT}, "
      f"speed factor {cfg.GAMEPAD_MAX_SPEED_FACTOR}x keyboard\n")
print(f"{'axis':<14} {'keyboard':>10} {'gamepad max':>12} "
      f"{'25%':>9} {'50%':>9} {'75%':>9} {'traverse':>9}")
print("-" * 78)

for a in cfg.AXES:
    kb = c.jog_velocity(a.index)
    gp = c.gamepad_velocity(a.index)
    if kb is None or gp is None:
        continue
    span = c._travel_span.get(a.index)
    row = f"{a.index} {a.name:<12}"[:14]
    speeds = ""
    for d in (0.25, 0.5, 0.75):
        speeds += f"{gp * d ** cfg.GAMEPAD_RESPONSE_EXPONENT:>9,.0f}"
    traverse = f"{span / gp:>8.1f}s" if span else "        —"
    print(f"{row:<14} {kb:>10,.0f} {gp:>12,.0f}{speeds} {traverse}")

print("\ncapped at MAX_JOG_FRACTION (%.0f%%) of each axis's VEL"
      % (cfg.MAX_JOG_FRACTION * 100))
for a in cfg.AXES:
    cap = VEL[a.index] * cfg.MAX_JOG_FRACTION
    gp = c.gamepad_velocity(a.index)
    if gp and abs(gp - cap) < 1.0:
        print(f"  axis {a.index} {a.name}: at the cap ({cap:,.0f} cts/s)")
