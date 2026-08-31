"""Check the gamepad mapping logic without touching the machine.

Uses a fake controller so the safety rules can be verified directly: one axis
at a time, release stops, ambiguous input stops, and jogs are not re-sent at
the poll rate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage import config as cfg  # noqa: E402
from acs_stage.gamepad import GamepadState  # noqa: E402
from acs_stage.gamepad_jog import GamepadJog  # noqa: E402


class FakeController:
    connected = True

    def __init__(self):
        self.jogs = []
        self.halts = []

    def jog_velocity(self, axis, fine=False):
        return 1000.0

    def gamepad_velocity(self, axis):
        return 1000.0

    def jog(self, axis, direction, velocity=None, travel=None, fine=False):
        self.jogs.append((axis, direction, velocity))

    def halt(self, axis):
        self.halts.append(axis)


class FakeTravel:
    def get(self, axis):
        return None


def state(pressed=(), lx=0.0, ly=0.0, rt=0.0, connected=True):
    return GamepadState(connected=connected, slot=0, pressed=set(pressed),
                        left_x=lx, left_y=ly, right_trigger=rt)


failed = 0


def check(name, condition):
    global failed
    print(f"  {'ok  ' if condition else 'FAIL'} {name}")
    failed += not condition


c = FakeController()
jog = GamepadJog(c, FakeTravel())

print("mapping:")
for btn, b in cfg.GAMEPAD_BINDINGS.items():
    inv = "  (inverted)" if b.invert else ""
    print(f"  {btn:<3} + {b.stick.value:<8} -> axis {b.axis} "
          f"({cfg.AXES_BY_INDEX[b.axis].name}){inv}")

print(f"\nresponse curve: deflection ** {cfg.GAMEPAD_RESPONSE_EXPONENT}")
for d in (0.25, 0.5, 0.75, 1.0):
    print(f"  stick {d:>4.0%}  ->  {d ** cfg.GAMEPAD_RESPONSE_EXPONENT:>5.1%} speed")

print("\nbehaviour:")

# Holding X with the stick pushed right drives axis 1.
expected = 1000.0 * (0.5 ** cfg.GAMEPAD_RESPONSE_EXPONENT)
jog.update(state(pressed=["X"], lx=0.5))
check("X + stick right jogs axis 1", c.jogs and c.jogs[-1][0] == 1)
check(f"speed follows the response curve ({expected:.0f})",
      abs(c.jogs[-1][2] - expected) < 1e-6)

# Same input again must NOT re-send.
before = len(c.jogs)
jog.update(state(pressed=["X"], lx=0.5))
check("identical input is not re-sent", len(c.jogs) == before)

# A meaningful change does re-send.
jog.update(state(pressed=["X"], lx=1.0))
check("larger deflection re-sends", len(c.jogs) == before + 1)

# Direction flip re-sends. X is inverted, so stick LEFT commands POSITIVE.
before = len(c.jogs)
jog.update(state(pressed=["X"], lx=-1.0))
check("direction flip re-sends", len(c.jogs) == before + 1)
check("X inverted: stick left -> POSITIVE",
      c.jogs[-1][1] is cfg.Direction.POSITIVE)

# Releasing the button halts.
c.halts.clear()
jog.update(state())
check("releasing the button halts", c.halts == [1])

# Centred stick with button held halts but stays armed.
jog.update(state(pressed=["Y"], ly=0.8))
c.halts.clear()
jog.update(state(pressed=["Y"], ly=0.0))
check("centred stick halts", c.halts == [0])

# Y drives axis 0 on the vertical component.
c.jogs.clear()
jog.update(state(pressed=["Y"], ly=0.6))
check("Y + stick up jogs axis 0", c.jogs and c.jogs[-1][0] == 0)

# Horizontal must NOT move axis 0 (it is bound to vertical).
c.jogs.clear()
jog.update(state())
jog.update(state(pressed=["Y"], lx=1.0))
check("Y ignores horizontal deflection", not c.jogs)

# Two buttons at once is ambiguous -> stop, no motion.
c.jogs.clear()
c.halts.clear()
jog.update(state(pressed=["A"], lx=0.9))
armed = len(c.jogs)
jog.update(state(pressed=["A", "B"], lx=0.9))
check("two buttons held stops", c.halts and len(c.jogs) == armed)

# Every binding's direction is driven by its invert flag, so check each one
# against the config rather than hard-coding which are flipped.
for btn, binding in cfg.GAMEPAD_BINDINGS.items():
    c.jogs.clear()
    jog.update(state())
    kwargs = {"rt": 0.9} if btn == "RT" else {"pressed": [btn]}
    if binding.stick.value.endswith("_x"):
        kwargs["lx"] = 1.0
    else:
        kwargs["ly"] = 1.0
    jog.update(state(**kwargs))
    expected = (cfg.Direction.NEGATIVE if binding.invert
                else cfg.Direction.POSITIVE)
    label = "inverted" if binding.invert else "normal"
    check(f"{btn} {label}: stick + -> {expected.name}",
          c.jogs and c.jogs[-1][1] is expected)

# RT is analog, counts as held past the threshold, and uses the VERTICAL axis.
c.jogs.clear()
jog.update(state())
jog.update(state(rt=0.9, ly=0.7))
check("RT past threshold + stick up jogs axis 6",
      c.jogs and c.jogs[-1][0] == 6)

c.jogs.clear()
jog.update(state())
jog.update(state(rt=0.9, lx=0.7))
check("RT ignores horizontal deflection", not c.jogs)

c.jogs.clear()
jog.update(state())
jog.update(state(rt=0.2, ly=0.7))
check("RT below threshold does nothing", not c.jogs)

# Losing the pad stops motion.
jog.update(state(pressed=["X"], lx=1.0))
c.halts.clear()
jog.update(state(connected=False))
check("gamepad disconnect halts", c.halts == [1])

print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
