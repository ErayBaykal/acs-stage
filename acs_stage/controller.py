"""Host-side wrapper around the SPiiPlus controller.

Deliberately thin. The controller already holds every motor, encoder and limit
parameter in its own flash, so nothing here configures the machine -- it only
connects, observes, and issues motion.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import SPiiPlusPython as sp

from . import config as cfg

log = logging.getLogger(__name__)

NONE = -1  # ACSC_NONE, required as the buffer number for global variables


class ControllerError(RuntimeError):
    pass


@dataclass
class AxisStatus:
    index: int
    position: float          # raw controller units (encoder counts)
    enabled: bool
    moving: bool
    in_position: bool
    homed: bool
    commutated: bool
    brushless: bool          # MFLAGS.#BRUSHL: controller handles commutation
    motor_error: int = 0     # MERR: why the axis was last disabled, 0 = fine

    @property
    def needs_commutation(self) -> bool:
        """Only DC brushless motors the controller commutates need this.

        #BRUSHOK is meaningless when #BRUSHL is 0 -- it stays 0 forever, so
        treating it as a preconditon would permanently block such an axis.
        """
        return self.brushless

    @property
    def ready_to_home(self) -> bool:
        """HOME requires the axis enabled, commutated and stationary."""
        if not self.enabled or self.moving:
            return False
        return self.commutated or not self.needs_commutation

    def not_ready_reason(self) -> str | None:
        if not self.enabled:
            return "motor is disabled"
        if self.needs_commutation and not self.commutated:
            return "motor is not commutated (needed after every power-up)"
        if self.moving:
            return "axis is moving"
        return None


class StageController:
    """Connection to the SPiiPlus EC.

    All controller access goes through ``_lock``: the Qt poll timer and any
    user-initiated command can otherwise interleave mid-transaction.
    """

    def __init__(self, host: str = cfg.CONTROLLER_HOST, port: int = cfg.CONTROLLER_PORT):
        self.host = host
        self.port = port
        self._hc: int | None = None
        self._lock = threading.RLock()
        self._default_velocity: dict[int, float] = {}
        self._travel_span: dict[int, float] = {}

    # -- connection ------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._hc is not None and self._hc >= 0

    def connect(self) -> None:
        with self._lock:
            if self.connected:
                return
            hc = sp.OpenCommEthernetTCP(self.host, self.port)
            if hc == -1:
                raise ControllerError(f"could not connect to {self.host}:{self.port}")
            self._hc = hc
            log.info("connected to %s:%s", self.host, self.port)
            try:
                sp.RegisterEmergencyStop()
            except Exception:
                # The UMD E-stop button is a convenience, not a dependency.
                log.warning("could not register emergency stop", exc_info=True)
            # Align #INVDOUT before anything else: a restart may have reverted
            # it to a stale flash value, which would block homing.
            try:
                for note in self.align_invdout():
                    log.warning("%s", note)
            except Exception:
                log.warning("could not align #INVDOUT", exc_info=True)
            self._cache_default_velocities()

    def disconnect(self) -> None:
        with self._lock:
            if not self.connected:
                return
            try:
                self.halt_all()
            except Exception:
                log.warning("halt during disconnect failed", exc_info=True)
            try:
                sp.UnregisterEmergencyStop()
            except Exception:
                pass
            sp.CloseComm(self._hc, True)
            self._hc = None
            self._default_velocity.clear()
            self._travel_span.clear()
            log.info("disconnected")

    def _require(self) -> int:
        if not self.connected:
            raise ControllerError("not connected")
        return self._hc

    # -- configuration read ----------------------------------------------

    def _cache_default_velocities(self) -> None:
        """Cache each axis's VEL and its soft-limit span.

        Both feed jog speed: the span sets how long a traverse takes, and VEL
        is a fallback cap for axes whose limits are placeholders.
        """
        hc = self._require()
        self._travel_span.clear()
        for axis in cfg.AXES:
            try:
                vel = sp.ReadReal(hc, NONE, "VEL", axis.index, axis.index,
                                  NONE, NONE, sp.SYNCHRONOUS, True)
                self._default_velocity[axis.index] = float(vel)
            except Exception:
                log.warning("could not read VEL(%d)", axis.index, exc_info=True)
            try:
                lo = float(sp.ReadReal(hc, NONE, "SLLIMIT", axis.index, axis.index,
                                       NONE, NONE, sp.SYNCHRONOUS, True))
                hi = float(sp.ReadReal(hc, NONE, "SRLIMIT", axis.index, axis.index,
                                       NONE, NONE, sp.SYNCHRONOUS, True))
                span = abs(hi - lo)
                if 0 < span < cfg.IMPLAUSIBLE_SPAN:
                    self._travel_span[axis.index] = span
            except Exception:
                log.warning("could not read soft limits for axis %d",
                            axis.index, exc_info=True)
        for axis in cfg.AXES:
            v = self.jog_velocity(axis.index)
            f = self.jog_velocity(axis.index, fine=True)
            log.info("axis %d jog speed: coarse %s, fine %s cts/s",
                     axis.index,
                     f"{v:,.0f}" if v else "default",
                     f"{f:,.0f}" if f else "default")

    def set_travel_span(self, axis_index: int, span: float) -> None:
        """Override the span used for jog speed, e.g. after a calibration."""
        if 0 < span < cfg.IMPLAUSIBLE_SPAN:
            self._travel_span[axis_index] = span

    def read_invdout(self, axis_index: int) -> int:
        """MFLAGS.#INVDOUT for one axis — its physical direction convention."""
        hc = self._require()
        with self._lock:
            flags = int(sp.ReadInteger(hc, NONE, "MFLAGS", axis_index, axis_index,
                                       NONE, NONE, sp.SYNCHRONOUS, True))
        return flags >> cfg.MFLAGS_INVDOUT_BIT & 1

    def align_invdout(self) -> list[str]:
        """Set each axis's #INVDOUT to match its configured homing direction.

        A controller restart reloads #INVDOUT from flash, and if that value
        disagrees with config.py the axis cannot be homed. Rather than making
        the operator fix it by hand after every power cycle, apply it here.

        MFLAGS can only be written while the motor is DISABLED (error 1078).
        An enabled motor is left alone and reported instead of being disabled
        automatically -- dropping holding torque on a gravity-loaded stage
        would let it fall, and this code cannot know the orientation.

        Returns a list of human-readable notes about what it did or could not
        do. The change is volatile; use save_to_flash() to persist it.
        """
        hc = self._require()
        notes: list[str] = []
        for axis in cfg.AXES:
            wanted_bit = 1 if axis.homing_direction is cfg.Direction.NEGATIVE else 0
            with self._lock:
                flags = int(sp.ReadInteger(hc, NONE, "MFLAGS", axis.index,
                                           axis.index, NONE, NONE,
                                           sp.SYNCHRONOUS, True))
            if (flags >> cfg.MFLAGS_INVDOUT_BIT & 1) == wanted_bit:
                continue

            status = self.poll().get(axis.index)
            if status is not None and status.enabled:
                notes.append(
                    f"axis {axis.index} ({axis.name}): #INVDOUT needs to be "
                    f"{wanted_bit} for {axis.homing_direction.name} homing, "
                    f"but the motor is enabled and MFLAGS can only be written "
                    f"while it is disabled. Disable it and reconnect."
                )
                continue

            updated = ((flags | (1 << cfg.MFLAGS_INVDOUT_BIT)) if wanted_bit
                       else (flags & ~(1 << cfg.MFLAGS_INVDOUT_BIT)))
            try:
                with self._lock:
                    sp.WriteInteger(hc, NONE, "MFLAGS", axis.index, axis.index,
                                    NONE, NONE, updated, sp.SYNCHRONOUS, True)
            except Exception as exc:
                notes.append(f"axis {axis.index} ({axis.name}): could not set "
                             f"#INVDOUT to {wanted_bit}: {exc}")
                continue
            notes.append(f"axis {axis.index} ({axis.name}): #INVDOUT "
                         f"{flags >> cfg.MFLAGS_INVDOUT_BIT & 1} -> "
                         f"{wanted_bit} to match {axis.homing_direction.name} "
                         f"homing")
            log.info("axis %d #INVDOUT set to %d", axis.index, wanted_bit)
        return notes

    def save_to_flash(self) -> None:
        """Persist axis parameters to the controller's non-volatile memory.

        Everything written at runtime -- soft limits, MFLAGS bits -- reverts
        to flash on restart. This is what stops a power cycle undoing a
        session's work.

        Flash is rated for ~100,000 writes, so this is deliberate and manual
        rather than automatic.
        """
        hc = self._require()
        parameters = (sp.Axis.ACSC_PAR_ALL, -1)
        with self._lock:
            sp.ControllerSaveToFlash(hc, parameters, None, None, None, True)
        log.info("axis parameters saved to controller flash")

    def _search_distance(self, axis_index: int) -> float:
        """Bound for a homing search, in counts.

        Two ways to arrive at one, in order of preference:

        1. twice the axis's known travel -- generous, since an over-tight
           MaxDistance aborts a legitimate home;
        2. how far it would travel in MAX_SEARCH_SECONDS at homing speed.

        The time-based fallback matters for an axis that has never been
        calibrated. The previous fallback of 1e8 counts was no bound at all:
        on a rotation axis at 1031 cts/s that is over 26 hours of driving.
        """
        span = self._travel_span.get(axis_index)
        if span:
            return abs(span) * 2.0
        velocity = self.homing_velocity(axis_index) or self._default_velocity.get(
            axis_index, 0.0) * 0.1
        if velocity:
            return abs(velocity) * cfg.MAX_SEARCH_SECONDS
        return 1e6

    def homing_velocity(self, axis_index: int) -> float | None:
        """Homing search speed for an axis, or None to use the firmware default.

        Expressed in config as a fraction of the axis's tuned VEL so it scales
        with whatever the axis is set up for, rather than being an absolute
        number invented here.
        """
        axis = cfg.AXES_BY_INDEX[axis_index]
        if axis.homing_velocity_fraction is None:
            return None
        base = self._default_velocity.get(axis_index)
        if base is None:
            return None
        return abs(base) * axis.homing_velocity_fraction

    def gamepad_velocity(self, axis_index: int) -> float | None:
        """Speed at full stick deflection, in counts/s.

        A multiple of the keyboard jog speed so the pad can move quickly,
        still bounded by the axis's own capability -- the response curve
        handles the slow end, so a higher ceiling costs nothing in precision.
        """
        base = self.jog_velocity(axis_index)
        if base is None:
            return None
        scaled = base * cfg.GAMEPAD_MAX_SPEED_FACTOR
        vel = self._default_velocity.get(axis_index)
        if vel:
            scaled = min(scaled, abs(vel) * cfg.MAX_JOG_FRACTION)
        return scaled

    def jog_velocity(self, axis_index: int, fine: bool = False) -> float | None:
        """Jog speed for an axis, in counts/s.

        The slower of:
          - the speed that crosses this axis's travel in the target time, and
          - a fraction of VEL, for axes with no usable span.

        Sizing by traverse time is what stops a high-VEL axis flying across
        its whole range on a keypress. Axis 0 previously jogged at 25% of a
        VEL of 2.432e7 -- 6,080,000 counts/s against ~3e6 counts of travel.
        """
        seconds = (cfg.JOG_TRAVERSE_SECONDS_FINE if fine
                   else cfg.JOG_TRAVERSE_SECONDS_COARSE)
        span = self._travel_span.get(axis_index)
        base = self._default_velocity.get(axis_index)

        if span:
            # Travel is known: size by traverse time, capped only so we never
            # ask for more than the axis is tuned to deliver.
            velocity = span / seconds
            if base:
                velocity = min(velocity, abs(base) * cfg.MAX_JOG_FRACTION)
            return velocity

        if base:
            # No usable span -- fall back to a conservative slice of VEL.
            fraction = cfg.JOG_FRACTION_FINE if fine else cfg.JOG_FRACTION_COARSE
            return abs(base) * fraction

        return None

    # -- status -----------------------------------------------------------

    def poll(self) -> dict[int, AxisStatus]:
        """Read position, motor state and homed flag for every configured axis.

        Reads each variable as a contiguous array across the full axis span in
        one transaction rather than one call per axis per variable, which keeps
        a 10 Hz poll to three round trips instead of fifteen.
        """
        hc = self._require()
        lo = min(a.index for a in cfg.AXES)
        hi = max(a.index for a in cfg.AXES)

        with self._lock:
            fpos = sp.ReadReal(hc, NONE, "FPOS", lo, hi, NONE, NONE, sp.SYNCHRONOUS, True)
            mst = sp.ReadInteger(hc, NONE, "MST", lo, hi, NONE, NONE, sp.SYNCHRONOUS, True)
            mflags = sp.ReadInteger(hc, NONE, "MFLAGS", lo, hi, NONE, NONE, sp.SYNCHRONOUS, True)
            merr = sp.ReadInteger(hc, NONE, "MERR", lo, hi, NONE, NONE, sp.SYNCHRONOUS, True)

        out: dict[int, AxisStatus] = {}
        for axis in cfg.AXES:
            i = axis.index - lo
            state = int(mst[i])
            flags = int(mflags[i])
            out[axis.index] = AxisStatus(
                index=axis.index,
                position=float(fpos[i]),
                enabled=bool(state >> cfg.MST_ENABLED_BIT & 1),
                moving=bool(state >> cfg.MST_MOVE_BIT & 1),
                in_position=bool(state >> cfg.MST_INPOS_BIT & 1),
                homed=bool(flags >> cfg.MFLAGS_HOME_BIT & 1),
                commutated=bool(flags >> cfg.MFLAGS_BRUSHOK_BIT & 1),
                brushless=bool(flags >> cfg.MFLAGS_BRUSHL_BIT & 1),
                motor_error=int(merr[i]),
            )
        return out

    # -- motor management --------------------------------------------------

    def enable(self, axis_index: int) -> None:
        with self._lock:
            sp.Enable(self._require(), axis_index, sp.SYNCHRONOUS, True)

    def disable(self, axis_index: int) -> None:
        with self._lock:
            sp.Disable(self._require(), axis_index, sp.SYNCHRONOUS, True)

    def fault_clear(self, axis_index: int) -> None:
        with self._lock:
            sp.FaultClear(self._require(), axis_index, sp.SYNCHRONOUS, True)

    def read_fault(self, axis_index: int) -> int:
        hc = self._require()
        with self._lock:
            return int(sp.ReadInteger(hc, NONE, "FAULT", axis_index, axis_index,
                                      NONE, NONE, sp.SYNCHRONOUS, True))

    def is_homing(self, axis_index: int) -> bool:
        hc = self._require()
        with self._lock:
            ast = int(sp.ReadInteger(hc, NONE, "AST", axis_index, axis_index,
                                     NONE, NONE, sp.SYNCHRONOUS, True))
        return bool(ast >> cfg.AST_INHOMING_BIT & 1)

    def abort_homing(self, axis_index: int) -> bool:
        """Terminate an in-progress HOME, if there is one.

        HALT and KILL stop the *motion* but leave AST.#INHOMING set, and the
        controller then rejects every subsequent motion command with 3065,
        "command cannot be executed while the current motion is in progress".
        Axis 6 was stuck that way after a cancelled home until it was disabled.

        The ACSPL+ reference gives only one way out: "Disable axis during
        homing process will cancel the homing process." So this disables the
        axis -- which does drop holding torque, and is why it is only done
        when a homing is genuinely still active.

        Returns True if it had to intervene.
        """
        try:
            if not self.is_homing(axis_index):
                return False
        except Exception:
            log.warning("could not read AST on axis %d", axis_index, exc_info=True)
            return False

        log.warning("axis %d still in homing state — disabling to cancel it "
                    "(HALT/KILL do not clear #INHOMING)", axis_index)
        try:
            self.disable(axis_index)
        except Exception:
            log.error("could not disable axis %d to cancel homing",
                      axis_index, exc_info=True)
            return False
        return True

    def commutate(self, axis_index: int) -> None:
        """Run auto-commutation on one axis.

        Needed after every controller power-up: the encoders are incremental,
        so the motor's electrical angle is unknown at boot. Requires the motor
        enabled and idle.

        The motor can jump up to one magnetic pitch in either direction as the
        current vector aligns, so the axis should not be parked hard against
        an obstacle when this runs.
        """
        axis = cfg.AXES_BY_INDEX[axis_index]
        status = self.poll().get(axis_index)
        if status is not None:
            if not status.needs_commutation:
                raise ControllerError(
                    f"axis {axis_index} ({axis.name}) is not a "
                    "controller-commutated brushless motor (MFLAGS.#BRUSHL=0) "
                    "— commutation does not apply to it"
                )
            if not status.enabled:
                raise ControllerError(
                    f"cannot commutate axis {axis_index} ({axis.name}): "
                    "motor is disabled"
                )
            if status.moving:
                raise ControllerError(
                    f"cannot commutate axis {axis_index} ({axis.name}): "
                    "axis is moving"
                )
        log.info("commutating axis %d (%s)", axis_index, axis.name)
        self.execute(f"COMMUT {axis_index}")

    # -- motion -------------------------------------------------------------

    def jog(self, axis_index: int, direction: cfg.Direction, fine: bool = False,
            travel=None, velocity: float | None = None) -> None:
        """Start an open-ended jog. Stopped by halt(), not by a distance.

        If a measured TravelRange is supplied, refuse to start a jog that is
        already at or past the limit in that direction. This is a convenience
        guard, not a safety mechanism -- the controller's SLLIMIT/SRLIMIT are
        the real protection, checked every cycle and predictively.
        """
        hc = self._require()
        if travel is not None:
            status = self.poll().get(axis_index)
            if status is not None:
                headroom = travel.headroom(status.position, direction.value)
                if headroom <= 0:
                    lo, hi = travel.safe_limits()
                    raise ControllerError(
                        f"axis {axis_index} is at the "
                        f"{direction.name.lower()} end of its measured travel "
                        f"({status.position:.0f}, range {lo:.0f}..{hi:.0f})"
                    )
        if velocity is None:
            velocity = self.jog_velocity(axis_index, fine=fine)
        with self._lock:
            if velocity is None:
                # No cached VEL: fall back to the axis default profile.
                sp.Jog(hc, 0, axis_index, direction.value, sp.SYNCHRONOUS, True)
            else:
                sp.Jog(hc, sp.MotionFlags.ACSC_AMF_VELOCITY, axis_index,
                       direction.value * velocity, sp.SYNCHRONOUS, True)

    def halt(self, axis_index: int) -> None:
        """Profiled stop using the axis DEC value."""
        with self._lock:
            sp.Halt(self._require(), axis_index, sp.SYNCHRONOUS, True)

    def halt_all(self) -> None:
        for axis in cfg.AXES:
            try:
                self.halt(axis.index)
            except Exception:
                log.warning("halt(%d) failed", axis.index, exc_info=True)

    def kill_all(self) -> None:
        """Fast stop using KDEC. For the E-stop path, not for normal use."""
        with self._lock:
            sp.KillAll(self._require(), sp.SYNCHRONOUS, True)

    # -- homing --------------------------------------------------------------

    def home(self, axis_index: int) -> None:
        """Start homing one axis using its configured method.

        FW 2.60 has the HOME command but not the HOMEDEF per-axis default
        array, so the method number is passed explicitly on every call.
        Non-blocking: poll AxisStatus.homed to follow progress.

        Refuses axes whose homing reference is not present -- a rotation stage
        with unwired limit switches would otherwise search endlessly.
        """
        axis = cfg.AXES_BY_INDEX[axis_index]
        if not axis.can_home:
            raise ControllerError(axis.blocked_reason)

        # Verify the configured direction still matches the axis's actual
        # direction convention. A controller restart can silently revert
        # #INVDOUT to a stale flash value, and homing the wrong way drives the
        # stage into the opposite mechanical stop until it faults.
        invdout = self.read_invdout(axis_index)
        wanted = cfg.expected_direction(invdout)
        if wanted is not axis.homing_direction:
            raise ControllerError(
                f"axis {axis_index} ({axis.name}): #INVDOUT is {invdout}, "
                f"which means this axis homes {wanted.name}, but it is "
                f"configured to home {axis.homing_direction.name} "
                f"(method {axis.homing_method}).\n\n"
                f"Homing it now would drive it away from its reference and "
                f"into the opposite stop.\n\n"
                f"Either set #INVDOUT to "
                f"{1 if axis.homing_direction is cfg.Direction.NEGATIVE else 0}"
                f" on the controller (motor must be disabled), or change "
                f"homing_direction to {wanted.name} for this axis in config.py."
            )

        # A switch-homing method aborts if its target switch is already
        # active -- and it does so SILENTLY: no error, no motion, but
        # AST.#INHOMING stays set and the axis then rejects everything with
        # 3065 until disabled. Axes 4 and 5 sat "homing" for 29 s and 9 s
        # without moving a single count this way.
        #
        # Both #RL and #LL reading active at once is not a position at all --
        # a stage cannot be at both ends -- it is the floating-input signature
        # of switches that are not wired.
        if axis.homing_method in (1, 2, 17, 18):
            target_bit = (cfg.FAULT_RL_BIT
                          if axis.homing_direction is cfg.Direction.POSITIVE
                          else cfg.FAULT_LL_BIT)
            fault = self.read_fault(axis_index)
            rl = bool(fault >> cfg.FAULT_RL_BIT & 1)
            ll = bool(fault >> cfg.FAULT_LL_BIT & 1)
            if rl and ll:
                raise ControllerError(
                    f"axis {axis_index} ({axis.name}): both limit switches "
                    f"read active at once. A stage cannot be at both ends, so "
                    f"these inputs are floating — the switches are not wired "
                    f"to the controller.\n\n"
                    f"Homing method {axis.homing_method} would abort silently "
                    f"and leave the axis stuck in homing state."
                )
            if fault >> target_bit & 1:
                name = "#RL" if target_bit == cfg.FAULT_RL_BIT else "#LL"
                raise ControllerError(
                    f"axis {axis_index} ({axis.name}): {name} is already "
                    f"active, and homing method {axis.homing_method} aborts "
                    f"when its target switch is on. Jog off the switch first."
                )

        # Check the preconditions here rather than letting the controller
        # reject it with a bare code -- "not commutated" is the expected state
        # after a power cycle and deserves a useful message.
        status = self.poll().get(axis_index)
        if status is not None and not status.ready_to_home:
            raise ControllerError(
                f"cannot home axis {axis_index} ({axis.name}): "
                f"{status.not_ready_reason()}"
            )

        # HOME takes its optional arguments positionally:
        #   Axis, Method, HomingVel, MaxDistance, HomingOffset, HomingCurrLimit
        # so reaching the current limit means supplying everything before it.
        args = [str(axis.index), str(axis.homing_method)]
        velocity = self.homing_velocity(axis_index)
        if velocity is not None:
            args.append(f"{velocity:.6g}")

            if axis.homing_current_limit is not None:
                # MaxDistance is required to reach HomingCurrLimit. Bound it
                # by the axis's known travel with generous headroom -- too
                # small would abort a legitimate home.
                distance = axis.max_distance or self._search_distance(axis_index)
                args.append(f"{distance:.6g}")
                args.append("0")                       # HomingOffset
                args.append(f"{axis.homing_current_limit:.6g}")
            elif axis.max_distance is not None:
                args.append(f"{axis.max_distance:.6g}")
        elif axis.max_distance is not None:
            log.warning(
                "axis %d has max_distance but no homing velocity; ACSPL+ needs "
                "the velocity argument first, so the bound is being skipped",
                axis.index,
            )

        command = "HOME " + ",".join(args)
        log.info("homing axis %d (%s): %s", axis.index, axis.name, command)
        self.execute(command)

    def homeable_axes(self) -> list[cfg.AxisConfig]:
        return [a for a in cfg.AXES if a.can_home]

    def home_all(self) -> None:
        """Home every axis that has a usable reference.

        Concurrent -- confirmed no collision risk between the five stages.
        """
        targets = self.homeable_axes()
        if not targets:
            raise ControllerError("no axes are configured as safe to home")
        for axis in targets:
            self.home(axis.index)

    # -- raw command ----------------------------------------------------------

    def execute(self, command: str) -> str:
        """Send a raw ACSPL+ command and return the controller's reply.

        Errors come back as ``?<code>`` rather than as an exception, so callers
        that care must check. Used for HOME, which has no library wrapper.
        """
        hc = self._require()
        payload = command + "\r"
        try:
            with self._lock:
                reply = sp.Transaction(hc, payload, len(payload), 1024,
                                       sp.SYNCHRONOUS, True)
        except Exception as exc:
            # failure_check=True makes the library raise its own exception
            # before we ever see the "?<code>" reply, so callers that want to
            # branch on the error code never got a ControllerError to catch.
            # Normalise it here, preserving the code in the message.
            raise ControllerError(f"{command!r} -> {exc}") from exc
        reply = (reply or "").strip()
        if reply.startswith("?"):
            raise ControllerError(f"{command!r} -> {self.error_text(reply)}")
        return reply

    def error_text(self, reply: str) -> str:
        """Turn a ``?<code>`` reply into something readable."""
        try:
            code = int(reply.lstrip("?").strip())
        except ValueError:
            return f"unrecognised reply {reply!r}"
        try:
            return f"{code}: {sp.GetErrorString(self._require(), code)}"
        except Exception:
            return f"error {code}"
