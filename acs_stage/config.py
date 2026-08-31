"""Machine definition for the 5-axis stage.

Everything here was established against the real controller (FW 2.60) rather than
assumed -- see docs/FINDINGS.md for the probes behind each value.
"""
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

CONTROLLER_HOST = "10.0.0.101"
CONTROLLER_PORT = 701


class StageKind(Enum):
    LINEAR = "linear"
    ROTATION = "rotation"


class Direction(Enum):
    NEGATIVE = -1
    POSITIVE = 1


# Homing methods, confirmed supported on FW 2.60 by probing with the motor
# disabled (error 3254 "requires motor enabled" == method accepted;
# error 3314 would mean rejected).
#
#   50/51 - hard stop, then index pulse. Both linear stages have working
#           index pulses, so they get the index refinement rather than
#           settling for hard-stop-only (52/53).
#   17/18 - limit switch. The rotation stages have switches at both ends.
HOMING_METHODS = {
    (StageKind.LINEAR, Direction.NEGATIVE): 50,
    (StageKind.LINEAR, Direction.POSITIVE): 51,
    (StageKind.ROTATION, Direction.NEGATIVE): 17,
    (StageKind.ROTATION, Direction.POSITIVE): 18,
}

# MFLAGS / MST bit positions (ACSPL+ reference).
MFLAGS_HOME_BIT = 3
MFLAGS_BRUSHL_BIT = 8       # controller commutates this motor (DC brushless)
MFLAGS_BRUSHOK_BIT = 9      # commutation established (only meaningful if BRUSHL)
MFLAGS_INVDOUT_BIT = 13     # drive output inverted -> flips physical direction
AST_INHOMING_BIT = 25       # a HOME is in progress on this axis

# FAULT bit positions (ACSPL+ reference, Table 5-19).
FAULT_RL_BIT = 0            # hardware right (positive) limit switch
FAULT_LL_BIT = 1            # hardware left (negative) limit switch


def expected_direction(invdout: int) -> "Direction":
    """Homing direction implied by an axis's #INVDOUT bit.

    Established empirically on every axis homed on this machine:

        #INVDOUT = 1  ->  home NEGATIVE   (axis 0)
        #INVDOUT = 0  ->  home POSITIVE   (axes 1, 6)

    Homing against this rule drives the stage away from its reference and into
    the opposite mechanical stop until something faults. That mistake cost
    several runs on axes 1 and 6, and again on axis 0 after a restart silently
    reverted its #INVDOUT, so it is checked before every homing operation.
    """
    return Direction.NEGATIVE if invdout else Direction.POSITIVE
MST_ENABLED_BIT = 0
MST_INPOS_BIT = 4
MST_MOVE_BIT = 5


@dataclass
class AxisConfig:
    """One physical stage.

    counts_per_unit is deliberately 1.0 for now: EFAC on the controller is 1,
    so the controller's user units *are* raw encoder counts. The real
    counts-per-mm / counts-per-degree figures have not been measured yet, and
    inventing them would put wrong numbers in front of the operator. Until
    they're measured the UI honestly displays counts.
    """
    index: int
    name: str
    kind: StageKind
    key_negative: str
    key_positive: str
    homing_direction: Direction = Direction.NEGATIVE
    counts_per_unit: float = 1.0
    unit: str = "cts"

    # Whether the axis has a working encoder index. Verified by arming
    # IST.#IND and moving the axis: both linear stages latch one, axis 6 does
    # not. This selects which homing method can probe the far end -- and on
    # FW 2.60 an axis with no index cannot have its POSITIVE end probed at
    # all, since method 53 is not implemented.
    has_index: bool = False

    # Rotation stages home to a limit switch. If the switch is not wired,
    # HOME searches for a signal that never arrives -- and with no MaxDistance
    # the firmware uses *endless* motion, so the stage would turn forever.
    # Default False so an unwired axis cannot be homed by accident; flip to
    # True per axis once its switches are connected and verified.
    limit_switches_connected: bool = False

    # Homing search speed, as a fraction of the axis's own tuned VEL (read
    # from the controller at connect). Slow matters here: hard-stop detection
    # trips at 0.75*CERRV, but the *fault* trips at CERRV. Arrive at the stop
    # with too much momentum and position error blows through both before the
    # detection can latch -- which is exactly how axis 1 faulted with
    # MERR 5023 instead of homing.
    homing_velocity_fraction: float | None = None

    # Motor current cap during homing, in the controller's current units.
    # None uses the firmware default of min(XCURV, 0.5*XRMSM, 0.5*XRMSD).
    #
    # That default is half the motor's rated continuous current, which is fine
    # on the linear stages (8, from XRMSM=16) but not on the rotation stages,
    # where it works out at 1.01 from XRMSM=2.02 -- too little to turn them.
    # The motor then cannot move at all, position error grows at the commanded
    # velocity, and #CPE fires in CERRV/velocity seconds. Axis 6 failed this
    # way repeatedly: 0.73 s predicted, 0.79-0.84 s observed.
    homing_current_limit: float | None = None

    # Optional bound on the homing search, in controller units. Turns "search
    # until something faults" into a clean failure. ACSPL+ takes these
    # positionally, so MaxDistance requires a velocity ahead of it.
    max_distance: float | None = None

    @property
    def homing_method(self) -> int:
        return HOMING_METHODS[(self.kind, self.homing_direction)]

    @property
    def can_home(self) -> bool:
        """Linear stages home on a hard stop and need no switches.

        Rotation stages need their limit switch wired, or the search never
        terminates.
        """
        if self.kind is StageKind.LINEAR:
            return True
        return self.limit_switches_connected

    @property
    def blocked_reason(self) -> str | None:
        if self.can_home:
            return None
        return (
            f"{self.name}: limit switches not marked as connected. "
            f"HOME method {self.homing_method} would search endlessly. "
            f"Set limit_switches_connected=True in config.py once wired."
        )

    def to_display(self, counts: float) -> float:
        return counts / self.counts_per_unit

    def to_counts(self, value: float) -> float:
        return value * self.counts_per_unit


# Homing search speed as a fraction of the axis's tuned VEL. Deliberately
# slow: this is a search that ends by driving into a mechanical stop, and the
# margin between hard-stop detection (0.75*CERRV) and the critical position
# error fault (CERRV) is only usable if the axis arrives slowly.
#
# 1% is a starting point, not a validated value -- watch the first run and
# adjust. Too slow only costs time; too fast faults the axis.
HOMING_VELOCITY_FRACTION = 0.01

# Longest a homing search should run on an axis whose travel is unknown.
# Converted to a MaxDistance at the axis's homing speed, so an uncalibrated
# axis cannot search indefinitely.
MAX_SEARCH_SECONDS = 90.0


# Axis map read out of the EtherCAT topology and confirmed by the user.
# Axes 2, 3, 7, 8, 9, 10, 11 exist on the drives but carry no stage.
#
# Homing direction is per axis: the two linear stages have opposite
# MFLAGS.#INVDOUT, so "negative" means opposite physical directions on them.
AXES = [
    # Keys are letters, not arrows: Qt consumes arrow keys for focus
    # navigation between widgets, so they never reach the jog handler.
    #
    # Two clusters, both laid out like WASD:
    #   linear    I/K = axis 1 up/down,  J/L = axis 0 left/right
    #   rotation  W/S = axis 5,  A/D = axis 4,  Q/E = axis 6
    #
    # The linear names follow the GAMEPAD buttons, not the keyboard cluster:
    # button X drives axis 1 and button Y drives axis 0, so axis 1 is
    # "Linear X" and axis 0 is "Linear Y". The names exist to tell the
    # operator which pad button moves which stage; anything else would make
    # the two disagree. The keyboard cluster is still laid out by direction
    # (I/K vertical, J/L horizontal), so it does NOT line up with these
    # letters -- see docs/FINDINGS.md.
    AxisConfig(0, "Linear Y", StageKind.LINEAR, "J", "L",
               homing_direction=Direction.NEGATIVE,
               has_index=True,
               homing_velocity_fraction=HOMING_VELOCITY_FRACTION),

    # Axis 1 homes POSITIVE (method 51), matching its #INVDOUT of 0. Verified
    # on hardware -- it homed successfully this way.
    #
    # It was briefly set to NEGATIVE on the theory that its soft limits
    # (0 .. 2,304,000) implied a home at the negative end. Axis 0's
    # calibration disproved that: its stored limits were
    # -742,400 .. 2,304,000 while its real travel is
    # -2,392,446 .. 2,558,444, so the stored limits never described the
    # travel and are no evidence for where home is.
    #
    # The two linear stages therefore run OPPOSITE sign conventions:
    # axis 0 has #INVDOUT=1 and homes negative, axis 1 has #INVDOUT=0 and
    # homes positive. Both work. Making them consistent would mean changing
    # #INVDOUT on one and re-verifying its direction from scratch.
    AxisConfig(1, "Linear X", StageKind.LINEAR, "K", "I",
               homing_direction=Direction.POSITIVE,
               has_index=True,
               homing_velocity_fraction=HOMING_VELOCITY_FRACTION),
    # Axes 4 and 5 unblocked for testing at the user's request. Two caveats:
    #
    #  - both #RL and #LL read active on these axes, which is the floating-
    #    input signature of switches that are not wired. Method 18 aborts if
    #    the positive switch is already ON, so the likely outcome is an
    #    immediate abort with no motion -- the safe failure.
    #  - they have no usable soft limits (SLLIMIT/SRLIMIT are +/-2e14), so
    #    until they are calibrated their only over-travel protection is the
    #    time-bounded MaxDistance and the hard stops themselves.
    #
    # Direction is POSITIVE from the #INVDOUT rule: both read 0, and every
    # axis with 0 so far homed positive.
    #
    # Current limit 2.0 rather than the default 1.01: XRMSM is 2.02, so this
    # is the motor's rated continuous current, not an overload. Axis 6 could
    # not turn at all on the default.
    AxisConfig(4, "Rotation A", StageKind.ROTATION, "A", "D",
               limit_switches_connected=True,
               homing_direction=Direction.POSITIVE,
               homing_velocity_fraction=0.10,
               homing_current_limit=2.0),
    AxisConfig(5, "Rotation B", StageKind.ROTATION, "S", "W",
               limit_switches_connected=True,
               homing_direction=Direction.POSITIVE,
               homing_velocity_fraction=0.10,
               homing_current_limit=2.0),

    # Axis 6 switches are wired (confirmed with user). Both #RL and #LL faults
    # are enabled in FMASK and read inactive mid-travel.
    #
    # Homing velocity is 10% of VEL rather than the 1% used on the linear
    # stages: a limit switch triggers electrically, so there is no drive-into-
    # a-hard-stop impact and no CERRV margin to protect. Still slow enough to
    # watch and abort.
    # Homing POSITIVE (method 18) after four failed attempts at method 17:
    # the stage repeatedly ran ~8,490 counts negative into a mechanical stop
    # and died on #CPE, with #LL never once going active. Either the switch
    # is at the positive end or it is not wired. Axis 6 shares #INVDOUT=0
    # with axis 1, which also turned out to need the opposite direction.
    AxisConfig(6, "Rotation C", StageKind.ROTATION, "Q", "E",
               limit_switches_connected=True,
               homing_direction=Direction.POSITIVE,
               homing_velocity_fraction=0.10,
               has_index=False,
               # 2.0 rather than the default 1.01: XRMSM is 2.02, so this is
               # the motor's rated continuous current, not an overload. These
               # stages home on a limit switch, so there is no hard-stop
               # contact for a lower current to protect against.
               homing_current_limit=2.0),
]

AXES_BY_INDEX = {a.index: a for a in AXES}

# Jog speed is derived from how long a full traverse should take, NOT from a
# fraction of VEL.
#
# VEL is the axis's maximum programmed velocity and bears no relation to its
# travel. Axis 0's VEL is 2.432e7 against roughly 3e6 counts of travel, so
# "25% of VEL" = 6,080,000 counts/s crossed the entire stage in half a second
# and drove it through its soft limit. Axis 6 only felt reasonable because its
# VEL happens to be 10,314.
#
# Sizing by traverse time gives a speed that feels the same on every axis
# regardless of how VEL was tuned.
JOG_TRAVERSE_SECONDS_COARSE = 15.0
JOG_TRAVERSE_SECONDS_FINE = 60.0

# Used only when an axis has no usable travel figure -- e.g. axes 4 and 5,
# whose soft limits are +/-2e14 and therefore meaningless as a span.
JOG_FRACTION_COARSE = 0.05
JOG_FRACTION_FINE = 0.01

# Ceiling on any jog, as a fraction of the axis's own VEL. Purely a
# capability guard so a small travel range cannot ask for more speed than the
# axis is tuned for; the traverse time normally lands well below it.
MAX_JOG_FRACTION = 0.5

# Soft-limit spans wider than this are placeholders, not real travel.
IMPLAUSIBLE_SPAN = 1e12

# Host heartbeat. The UI writes a counter the controller watches; if it goes
# stale the controller kills motion on its own. This is the only jog safeguard
# that survives the UI process being killed outright.
#
# WATCHDOG_BUFFER must be a program buffer you are not using for anything
# else. Loading a buffer clears it first, so the installer refuses to write
# over a buffer that already holds an unrelated program -- change this number
# rather than forcing it.
WATCHDOG_BUFFER = 9
WATCHDOG_PERIOD_MS = 200
WATCHDOG_TIMEOUT_MS = 1000

POLL_PERIOD_MS = 100


class Stick(Enum):
    """Which stick component drives an axis."""
    LEFT_X = "left_x"       # left stick, horizontal
    LEFT_Y = "left_y"       # left stick, vertical
    RIGHT_X = "right_x"
    RIGHT_Y = "right_y"


class Binding(NamedTuple):
    """One gamepad button mapped to one stage axis."""
    axis: int
    stick: Stick
    invert: bool = False    # flip if the stage moves opposite to the stick


# Gamepad jog: hold a button, move the left stick, that axis moves.
#
# The held button is a dead-man switch -- releasing it stops the axis
# immediately -- and it also selects which axis the stick drives, so one
# stick can address all five without any mode state to get lost in.
#
# Stick deflection sets velocity proportionally, so a small push creeps and a
# full push runs at the axis's normal jog speed. That is far better for
# positioning a stage than the keyboard's fixed-speed on/off jogging.
#
GAMEPAD_BINDINGS = {
    # Several stages move opposite to the stick, so their sense is inverted
    # here rather than anywhere in the motion path -- the axis's own direction
    # convention (#INVDOUT) stays untouched.
    "X":  Binding(1, Stick.LEFT_X, invert=True),    # Linear X   -- left/right
    "Y":  Binding(0, Stick.LEFT_Y),                 # Linear Y   -- up/down
    "A":  Binding(4, Stick.LEFT_X, invert=True),    # Rotation A -- left/right
    "B":  Binding(5, Stick.LEFT_X, invert=True),    # Rotation B -- left/right
    "RT": Binding(6, Stick.LEFT_Y),                 # Rotation C -- up/down
}

# RT is analog; treat it as held past this fraction of full pull.
TRIGGER_HELD_THRESHOLD = 0.5

# How stick deflection maps to speed. 1.0 is linear; higher values give a
# larger slow-speed band near centre, which is what makes fine positioning
# practical. At 2.0, half deflection is a quarter speed.
#   1.0  linear          -- twitchy at the low end
#   2.0  squared         -- good general default
#   3.0  cubed           -- very fine near centre, more stick needed for speed
GAMEPAD_RESPONSE_EXPONENT = 2.0

# Full stick deflection runs this many times the keyboard jog speed, so the
# pad can cover ground quickly while the response curve keeps the slow end
# fine. Still capped at MAX_JOG_FRACTION of the axis's VEL, so an axis is
# never asked for more than it is tuned to deliver.
#   1.0  same as the keyboard  -- full traverse in ~15 s
#   3.0  current               -- full traverse in ~5 s
GAMEPAD_MAX_SPEED_FACTOR = 3.0

# Re-send a jog only when the commanded velocity moves by more than this
# fraction of the axis maximum. Without it, a 20 Hz poll would spam the
# controller with near-identical commands as the stick jitters.
GAMEPAD_VELOCITY_HYSTERESIS = 0.04

# Gamepad polling. 20 Hz is responsive for reading buttons and shows stick
# movement smoothly; XInputGetState is cheap so this costs nothing.
GAMEPAD_POLL_MS = 50

# Consecutive failed status polls before the connection is declared lost.
# A couple of failures can be transient; a run of them is not.
POLL_FAILURES_BEFORE_LOST = 3
