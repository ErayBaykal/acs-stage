"""Travel-range calibration: find both ends of an axis and record them.

Homing establishes one end and sets zero. This routine keeps that reference
and then feels for the *opposite* end, so the pair defines the usable range.

The far end is deliberately NOT found with another HOME command: every homing
method re-zeroes the axis, which would destroy the reference the first home
just established. Instead the axis is jogged slowly toward the far end while
the appropriate stop condition is watched:

  rotation axes -- the opposite limit switch (FAULT #RL / #LL). No mechanical
                   contact at all.
  linear axes   -- position error rising as the stage meets its hard stop,
                   caught well below the #CPE fault threshold so the axis is
                   halted deliberately rather than faulted.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import SPiiPlusPython as sp

from . import config as cfg
from .controller import NONE, ControllerError, StageController
from .travel import DEFAULT_MARGIN_FRACTION, TravelRange

log = logging.getLogger(__name__)

FAULT_RL_BIT = 0
FAULT_LL_BIT = 1

# Homing methods used to probe the far end of travel.
#
# Which one depends on whether the axis has an index pulse, AND on what this
# firmware actually implements. Probed on hardware (FW 2.60):
#
#   50  negative hard stop + index   SUPPORTED   (axis 0 homes with it)
#   51  positive hard stop + index   SUPPORTED   (axis 1 homed with it)
#   52  negative hard stop           SUPPORTED   (probe returned 3254)
#   53  positive hard stop           NOT SUPPORTED -- returns 3314
#
# Support is asymmetric: 52 exists but 53 does not. So an axis with no index
# cannot have its POSITIVE end probed by homing at all on this firmware.
HARD_STOP_METHOD_WITH_INDEX = {
    cfg.Direction.NEGATIVE: 50,
    cfg.Direction.POSITIVE: 51,
}
HARD_STOP_METHOD_NO_INDEX = {
    cfg.Direction.NEGATIVE: 52,
    cfg.Direction.POSITIVE: None,   # method 53 is not in FW 2.60
}

AST_INHOMING_BIT = 25

# MERR codes for reaching a hardware limit switch. During a far-end search
# these are the SUCCESS condition, not a failure: the switch is exactly what
# we were looking for. The controller disables the axis when a limit trips,
# which is correct, but it also sets MERR -- so a blanket "any MERR aborts"
# check throws away the result we wanted.
LIMIT_MERR = {5010: ("#RL", cfg.Direction.POSITIVE),
              5011: ("#LL", cfg.Direction.NEGATIVE)}

# Stop the far-end search when |PE| exceeds this fraction of CERRV. The #CPE
# fault fires at CERRV and hard-stop homing detects at 0.75*CERRV, so staying
# at 0.5 means we halt under our own control, before either of those.
PE_STOP_FRACTION = 0.5

# Give up rather than run forever if the far end is never detected.
SEARCH_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 0.005

# How many polls we want to land between PE crossing our threshold and PE
# reaching CERRV (where #CPE fires). This is what makes detection possible at
# all, and it caps the search speed -- see search_velocity().
DETECTION_SAMPLES = 5

# Stand-in for "no soft limit" while measuring. Matches how axes 4 and 5 are
# already configured on this machine.
WIDE_LIMIT = 2e14

# CERRV is multiplied by this during a search so #CPE cannot fire before our
# own detection reacts. Detection still uses the original CERRV.
CERRV_SEARCH_MULTIPLIER = 10.0

# How long to ease off a hard stop before the next leg of the search. Long
# enough to be unambiguously clear of it -- a token nudge leaves the axis
# close enough that residual contact still matters.
BACK_OFF_SECONDS = 4.0

# Position error is considered settled below this fraction of CERRV. Homing
# needs most of its error budget intact to survive its own acceleration.
PE_SETTLED_FRACTION = 0.1

# Pause after a motion completes before issuing the next command.
SETTLE_SECONDS = 0.5

# Poll period while watching a homing move.
HOMING_POLL_S = 0.02

# How long to wait for an axis to actually stop before giving up on it.
STOP_TIMEOUT_S = 10.0


def search_velocity(cerrv: float, jog_velocity: float | None) -> float | None:
    """Fastest search speed at which the hard stop is still detectable.

    Once a stage is against its stop the commanded position keeps advancing
    while the feedback does not, so position error grows at roughly the
    commanded velocity. To catch PE between our threshold
    (PE_STOP_FRACTION * CERRV) and the #CPE fault (CERRV), the axis must
    cover that margin in no fewer than DETECTION_SAMPLES polls:

        margin / velocity >= DETECTION_SAMPLES * POLL_INTERVAL_S

    Axis 0 originally searched at 5% of VEL = 1.2e6 counts/s, which crosses a
    1250-count margin in ~1 ms against a 20 ms poll. Detection could never
    fire; the axis just faulted on #CPE instead.
    """
    margin = abs(cerrv) * (1.0 - PE_STOP_FRACTION)
    cap = margin / (DETECTION_SAMPLES * POLL_INTERVAL_S)
    if jog_velocity is None:
        return cap
    return min(abs(jog_velocity), cap)


class CalibrationCancelled(RuntimeError):
    pass


class MethodUnsupported(RuntimeError):
    """This firmware/axis combination has no usable hard-stop homing method."""


@dataclass
class Progress:
    axis: int
    message: str
    position: float | None = None
    done: bool = False


class TravelCalibrator:
    """Measures one axis's travel. Run off the UI thread."""

    def __init__(self, controller: StageController, on_progress=None):
        self.controller = controller
        self._on_progress = on_progress
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _report(self, axis: int, message: str, position: float | None = None,
                done: bool = False) -> None:
        log.info("calibrate axis %d: %s", axis, message)
        if self._on_progress:
            self._on_progress(Progress(axis, message, position, done))

    def _check_cancel(self, axis: int) -> None:
        if not self._cancel.is_set():
            return
        try:
            self.controller.halt(axis)
        except Exception:
            log.warning("halt on cancel failed", exc_info=True)
        self.controller.abort_homing(axis)
        raise CalibrationCancelled("calibration cancelled")

    # -- controller reads --------------------------------------------------

    def _read_real(self, name: str, axis: int) -> float:
        hc = self.controller._require()
        with self.controller._lock:
            return float(sp.ReadReal(hc, NONE, name, axis, axis, NONE, NONE,
                                     sp.SYNCHRONOUS, True))

    def _read_int(self, name: str, axis: int) -> int:
        hc = self.controller._require()
        with self.controller._lock:
            return int(sp.ReadInteger(hc, NONE, name, axis, axis, NONE, NONE,
                                      sp.SYNCHRONOUS, True))

    def _write_real(self, name: str, axis: int, value: float) -> None:
        hc = self.controller._require()
        with self.controller._lock:
            sp.WriteReal(hc, NONE, name, axis, axis, NONE, NONE, value,
                         sp.SYNCHRONOUS, True)

    @contextmanager
    def _fault_ceiling_raised(self, axis: int):
        """Temporarily raise CERRV so #CPE does not fire during the search.

        Finding a hard stop *requires* position error to build -- that is the
        only signal that the stage has stopped. We detect at
        PE_STOP_FRACTION * CERRV and halt, but #CPE fires at CERRV, and the
        gap between them is crossed in tens of milliseconds. Host-side
        detection cannot win that race: every poll is a TCP round trip, so the
        loop period is more like 10-20 ms, not the 5 ms nominal.

        Raising the ceiling for the duration removes the race. Detection still
        uses the ORIGINAL CERRV, so the trip point is unchanged -- only the
        fault that was firing first is moved out of the way.

        The search remains bounded: slow speed, PE monitoring, a timeout, and
        Esc cancellation.
        """
        original = self._read_real("CERRV", axis)
        raised = abs(original) * CERRV_SEARCH_MULTIPLIER
        self._report(axis, f"CERRV raised {original:.0f} -> {raised:.0f} "
                           f"for the search")
        self._write_real("CERRV", axis, raised)
        try:
            yield original
        finally:
            try:
                self._write_real("CERRV", axis, original)
                self._report(axis, f"CERRV restored to {original:.0f}")
            except Exception:
                log.exception(
                    "FAILED to restore CERRV on axis %d — it is currently "
                    "%.0f and should be %.0f. Set it manually.",
                    axis, raised, original)

    @contextmanager
    def _limits_widened(self, axis: int):
        """Temporarily remove the soft limits so travel can be measured.

        The existing limits bound the very range we are trying to measure --
        axis 0's search stopped dead on SRLIMIT at 2,304,000 (MERR 5015), and
        axis 1's tripped SLLIMIT on its first step (MERR 5016). Neither told
        us anything about where the mechanical stops actually are.

        The originals are always restored, including on cancellation or error,
        so a failed calibration leaves the axis exactly as it was found.
        """
        original_lo = self._read_real("SLLIMIT", axis)
        original_hi = self._read_real("SRLIMIT", axis)
        self._report(axis, f"soft limits widened for the sequence "
                           f"(were {original_lo:.0f} .. {original_hi:.0f})")
        self._write_real("SLLIMIT", axis, -WIDE_LIMIT)
        self._write_real("SRLIMIT", axis, WIDE_LIMIT)

        # The caller sets state["replaced"] once it has written measured
        # limits of its own. Restoring the originals in that case would
        # briefly reimpose stale limits on a freshly re-homed axis -- enough
        # to fault it, since the old frame no longer applies.
        state = {"replaced": False}
        try:
            yield state
        finally:
            if state["replaced"]:
                return
            try:
                self._write_real("SLLIMIT", axis, original_lo)
                self._write_real("SRLIMIT", axis, original_hi)
                self._report(axis, "soft limits restored")
            except Exception:
                log.exception(
                    "FAILED to restore soft limits on axis %d — they are "
                    "currently wide open (+/-%.0e). Set SLLIMIT=%.0f and "
                    "SRLIMIT=%.0f manually.",
                    axis, WIDE_LIMIT, original_lo, original_hi)

    # -- the search ---------------------------------------------------------

    def _probe_far_end_with_firmware(self, axis: int,
                                     direction: cfg.Direction) -> float:
        """Find the far end using the controller's own hard-stop homing.

        Why not jog and watch position error from here: HOME does the same
        detection *in firmware, every controller cycle*, and it limits motor
        current during the search (HomingCurrLimit) so the stage eases into
        the stop. A host-side loop has 10-20 ms of TCP latency per iteration
        and jogs at full current -- it slams into the stop and #CPE fires
        before the halt lands. That is why "Home" always worked and
        "Find Limits" always faulted.

        Methods 52/53 are hard-stop-only, needing no index or switch at the
        far end.

        HOME re-zeroes the axis on completion, so the far-end position is
        captured by tracking the extreme FPOS reached *before* that reset.
        The caller re-homes afterwards to restore the intended frame.
        """
        axis_cfg = cfg.AXES_BY_INDEX[axis]
        table = (HARD_STOP_METHOD_WITH_INDEX if axis_cfg.has_index
                 else HARD_STOP_METHOD_NO_INDEX)
        method = table[direction]
        if method is None:
            raise MethodUnsupported(
                f"no firmware homing method exists for the "
                f"{direction.name.lower()} end of axis {axis}")
        velocity = self.controller.homing_velocity(axis)
        args = [str(axis), str(method)]
        if velocity is not None:
            args.append(f"{velocity:.6g}")
        command = "HOME " + ",".join(args)

        self._report(axis, f"probing {direction.name.lower()} end with "
                           f"firmware hard-stop homing: {command}")
        try:
            self.controller.execute(command)
        except ControllerError as exc:
            if "3314" in str(exc):
                raise MethodUnsupported(
                    f"method {method} is not supported on axis {axis}") from exc
            raise

        extreme = self._read_real("FPOS", axis)
        sign = 1 if direction is cfg.Direction.POSITIVE else -1
        started = time.monotonic()
        saw_motion = False

        while True:
            self._check_cancel(axis)
            if time.monotonic() - started > SEARCH_TIMEOUT_S:
                raise ControllerError(
                    f"axis {axis}: firmware homing probe did not finish "
                    f"within {SEARCH_TIMEOUT_S:.0f} s")

            pos = self._read_real("FPOS", axis)
            if sign * pos > sign * extreme:
                extreme = pos
            ast = self._read_int("AST", axis)
            in_homing = bool(ast >> AST_INHOMING_BIT & 1)
            saw_motion = saw_motion or in_homing

            merr = self._read_int("MERR", axis)
            if merr:
                raise ControllerError(
                    f"axis {axis}: firmware homing probe faulted — MERR "
                    f"{merr} ({self.controller.error_text('?%d' % merr)})")

            if saw_motion and not in_homing:
                break
            time.sleep(0.02)

        self._report(axis, f"{direction.name.lower()} end reached at "
                           f"{extreme:.0f}")
        return extreme

    def _watch_homing(self, axis: int, direction: cfg.Direction
                      ) -> tuple[float, float]:
        """Wait for a HOME to finish, watching where it goes.

        Returns (extreme reached, position immediately before the re-zero),
        both in the frame that was in force *during* the move.

        This is what lets the reference end be measured without a separate
        trip: a hard-stop homing method already drives to the stop and then
        backs off to its reference, so both points are observable in one pass.
        The difference between them is frame-independent, which is what makes
        it usable after the re-zero lands.

        The re-zero appears as a discontinuity in FPOS -- the axis cannot
        physically jump -- so it is detected by comparing each step against the
        furthest the axis could have travelled between polls.
        """
        velocity = self.controller.homing_velocity(axis) or 0.0
        # Generous: a real move covers at most velocity*dt per poll.
        jump_threshold = max(abs(velocity) * HOMING_POLL_S * 10.0, 1000.0)

        sign = 1 if direction is cfg.Direction.POSITIVE else -1
        previous = self._read_real("FPOS", axis)
        extreme = previous
        before_rezero = previous
        started = time.monotonic()
        saw_motion = False

        while True:
            self._check_cancel(axis)
            if time.monotonic() - started > SEARCH_TIMEOUT_S:
                raise ControllerError(
                    f"axis {axis}: homing did not finish within "
                    f"{SEARCH_TIMEOUT_S:.0f} s")

            pos = self._read_real("FPOS", axis)
            if abs(pos - previous) > jump_threshold:
                # Discontinuity: the controller has just re-zeroed.
                before_rezero = previous
            elif sign * pos > sign * extreme:
                extreme = pos
            previous = pos

            ast = self._read_int("AST", axis)
            in_homing = bool(ast >> AST_INHOMING_BIT & 1)
            moving = bool(self._read_int("MST", axis) >> cfg.MST_MOVE_BIT & 1)
            saw_motion = saw_motion or in_homing

            merr = self._read_int("MERR", axis)
            if merr:
                raise ControllerError(
                    f"axis {axis}: homing faulted — MERR {merr} "
                    f"({self.controller.error_text('?%d' % merr)})")

            if saw_motion and not in_homing and not moving:
                time.sleep(SETTLE_SECONDS)
                return extreme, before_rezero
            time.sleep(HOMING_POLL_S)

    def _probe_end(self, axis: int, direction: cfg.Direction) -> tuple[float, bool]:
        """Find one end of travel, preferring firmware homing.

        Firmware homing is far better at this -- it detects contact every
        controller cycle and limits motor current -- but the available methods
        are **axis-dependent**, not just firmware-dependent. Method 52 is
        accepted on axis 0 and rejected with 3314 on axis 6, which is rotary
        and has no index. Axis 6 has no firmware method for its negative end
        at all: 52 is rejected, 50 needs an index it lacks, 17 needs a switch
        it lacks.

        So fall back to the host-side jog-and-watch probe when no firmware
        method works. That fallback is only viable on slow axes -- the
        detection margin analysis in search_velocity() shows axis 6 gets ~145
        samples between the trip point and #CPE where the linear axes got
        0.21 -- but axis 6 is exactly such an axis, and it was measured
        successfully this way before.

        Returns (position, used_firmware). The flag matters: firmware homing
        leaves the axis homed on its reference, while the host-side fallback
        leaves it sitting at the stop, so the caller must re-home after a
        fallback even when it could skip that after a firmware probe.
        """
        try:
            return self._probe_far_end_with_firmware(axis, direction), True
        except MethodUnsupported as exc:
            self._report(axis, f"{exc}; falling back to host-side detection")

        # CERRV is raised for the fallback: host-side detection cannot react
        # before #CPE fires otherwise.
        with self._fault_ceiling_raised(axis) as cerrv:
            position, reason = self._find_far_end(axis, direction, cerrv)
        self._report(axis, f"{direction.name.lower()} end at {position:.0f} "
                           f"({reason}, host-side)")
        return position, False

    def _prepare_for_motion(self, axis: int) -> None:
        """Bring an axis to a clean, stationary, enabled state.

        Required before every homing attempt, for three separate reasons all
        of which have bitten:

        - HOME refuses while the axis is moving, and a preceding halt does not
          always settle before the next command lands.
        - _wait_for_homing treats any non-zero MERR as a fresh fault, so a
          leftover code makes the next home appear to fail instantly. Axis 6
          "faulted" 0.79 s after a HOME, before moving 800 counts, purely
          because MERR still read 5023 from the previous attempt.
        - a fault disables the motor, and HOME needs it enabled.

        Motion is confirmed by watching FPOS rather than MST.#MOVE, because
        that bit has been observed stuck at 1 on this controller while the
        axis was demonstrably stationary.
        """
        try:
            self.controller.halt(axis)
        except Exception:
            log.debug("halt before motion failed on axis %d", axis, exc_info=True)

        deadline = time.monotonic() + STOP_TIMEOUT_S
        previous = self._read_real("FPOS", axis)
        still = 0
        while time.monotonic() < deadline:
            time.sleep(0.1)
            current = self._read_real("FPOS", axis)
            still = still + 1 if abs(current - previous) < 1.0 else 0
            previous = current
            if still >= 3:
                break
        else:
            log.warning("axis %d still moving after %.0f s", axis, STOP_TIMEOUT_S)

        try:
            self.controller.fault_clear(axis)
        except Exception:
            log.warning("fault clear failed on axis %d", axis, exc_info=True)

        status = self.controller.poll().get(axis)
        if status is not None and not status.enabled:
            self.controller.enable(axis)
            time.sleep(SETTLE_SECONDS)

        # Wait for position error to settle before handing over to HOME.
        #
        # A halt leaves RPOS parked beyond FPOS, so PE persists -- we measured
        # a standing -481 after one hard-stop contact. Homing then begins with
        # much of its CERRV budget already spent and trips almost immediately:
        # axis 6 faulted 0.86 s into a re-home straight after a stop contact,
        # while the same command from mid-travel succeeded.
        cerrv = self._read_real("CERRV", axis)
        target = abs(cerrv) * PE_SETTLED_FRACTION
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < deadline:
            pe = abs(self._read_real("PE", axis))
            if pe <= target:
                return
            time.sleep(0.1)
        log.warning("axis %d: PE still %.0f (want <= %.0f) after %.0f s",
                    axis, abs(self._read_real("PE", axis)), target,
                    STOP_TIMEOUT_S)

    def _home_with_recovery(self, axis: int) -> None:
        """Home an axis, backing off and retrying once if it overshoots.

        A switch-homed axis can only find its switch from the approach side.
        After a power cycle the position reference is arbitrary and the stage
        may be sitting *past* the switch -- homing then drives away from it,
        into the far hard stop, and faults on #CPE without the switch ever
        triggering. That is not a wiring problem and not a configuration
        problem, just where the stage happened to be parked.

        So on that specific failure, back off toward the other end until the
        switch appears (or the known travel is exhausted) and try once more.
        """
        self._prepare_for_motion(axis)
        try:
            self.controller.home(axis)
            self._wait_for_homing(axis)
            return
        except ControllerError as exc:
            if "5023" not in str(exc):
                raise
            self._report(axis, "homing faulted without finding its reference "
                               "— may be parked past the switch; backing off")

        axis_cfg = cfg.AXES_BY_INDEX[axis]
        away = (cfg.Direction.NEGATIVE
                if axis_cfg.homing_direction is cfg.Direction.POSITIVE
                else cfg.Direction.POSITIVE)
        switch_bit = (FAULT_RL_BIT
                      if axis_cfg.homing_direction is cfg.Direction.POSITIVE
                      else FAULT_LL_BIT)

        velocity = self.controller.homing_velocity(axis)
        travel = self._known_span(axis)
        # Bound the back-off by the axis's own travel; without a measurement,
        # fall back to the search timeout.
        limit_s = (abs(travel / velocity) * 1.2 if travel and velocity
                   else SEARCH_TIMEOUT_S)

        self._prepare_for_motion(axis)
        nominal_cerrv = self._read_real("CERRV", axis)
        pe_threshold = abs(nominal_cerrv) * PE_STOP_FRACTION

        # Raise the fault ceiling for the back-off too: it may well meet the
        # far hard stop, and running into that with #CPE armed disables the
        # motor and leaves nothing for the retry to work with.
        with self._fault_ceiling_raised(axis):
            self.controller.jog(axis, away, velocity=velocity)
            started = time.monotonic()
            try:
                while time.monotonic() - started < limit_s:
                    self._check_cancel(axis)
                    if self._read_int("FAULT", axis) >> switch_bit & 1:
                        self._report(axis, "switch found while backing off")
                        break
                    if abs(self._read_real("PE", axis)) > pe_threshold:
                        self._report(axis, "reached the far stop while "
                                           "backing off — stopping there")
                        break
                    time.sleep(POLL_INTERVAL_S)
                else:
                    self._report(axis, "backed off the full travel without "
                                       "finding the switch")
            finally:
                self.controller.halt(axis)
                time.sleep(SETTLE_SECONDS)

        self._report(axis, "retrying home from the approach side")
        self._prepare_for_motion(axis)
        self.controller.home(axis)
        self._wait_for_homing(axis)

    def _known_span(self, axis: int) -> float | None:
        """Measured travel for an axis, if it has been calibrated before."""
        from .travel import TravelStore
        stored = TravelStore().get(axis)
        return abs(stored.span) if stored else None

    def _wait_for_homing(self, axis: int) -> None:
        """Block until a HOME finishes AND the axis has stopped moving.

        Waiting only for AST.#INHOMING is not enough: the axis can still be
        settling when the bit clears, and the next HOME is then rejected with
        "command cannot be executed while the current motion is in progress".
        """
        started = time.monotonic()
        saw_motion = False
        while True:
            self._check_cancel(axis)
            if time.monotonic() - started > SEARCH_TIMEOUT_S:
                raise ControllerError(
                    f"axis {axis}: homing did not finish within "
                    f"{SEARCH_TIMEOUT_S:.0f} s")
            ast = self._read_int("AST", axis)
            in_homing = bool(ast >> AST_INHOMING_BIT & 1)
            moving = bool(self._read_int("MST", axis) >> cfg.MST_MOVE_BIT & 1)
            saw_motion = saw_motion or in_homing
            merr = self._read_int("MERR", axis)
            if merr:
                raise ControllerError(
                    f"axis {axis}: homing faulted — MERR {merr} "
                    f"({self.controller.error_text('?%d' % merr)})")
            if saw_motion and not in_homing and not moving:
                time.sleep(SETTLE_SECONDS)
                return
            time.sleep(0.02)

    def _find_far_end(self, axis: int, direction: cfg.Direction,
                      nominal_cerrv: float | None = None) -> tuple[float, str]:
        """Jog toward the far end until it stops, then halt.

        Watches BOTH a limit switch and rising position error, on every axis
        type, and takes whichever arrives first. An earlier version watched
        only the switch on rotation axes -- when axis 6 met a bare mechanical
        stop with no switch on that end, the search did not notice and left
        the motor pushing against the stop until the timeout. Never assume an
        axis has a reference at both ends.

        Returns (resting position, what stopped it).
        """
        # Use the axis's normal CERRV for the trip point even though the fault
        # ceiling is raised -- the raise only removes the race, it must not
        # change how hard we push into the stop.
        cerrv = (nominal_cerrv if nominal_cerrv is not None
                 else self._read_real("CERRV", axis))
        pe_threshold = abs(cerrv) * PE_STOP_FRACTION

        # Homing went one way, so the far end carries the opposite switch --
        # if it has one at all.
        far_bit = FAULT_RL_BIT if direction is cfg.Direction.POSITIVE else FAULT_LL_BIT
        switch_name = "#RL" if direction is cfg.Direction.POSITIVE else "#LL"

        # Base the search on the HOMING velocity, not the fine jog velocity.
        # Jog speed is sized for hand control and, on an axis with no measured
        # travel, falls back to 1% of VEL -- 103 cts/s on axis 4, which would
        # take four minutes to cross its range. search_velocity() still caps it
        # to whatever keeps hard-stop detection possible.
        velocity = search_velocity(
            cerrv,
            self.controller.homing_velocity(axis)
            or self.controller.jog_velocity(axis, fine=True),
        )
        self._report(axis, f"searching {direction.name.lower()} at "
                           f"{velocity:.0f} cts/s: {switch_name} switch or "
                           f"hard stop (PE > {pe_threshold:.0f})")

        self.controller.jog(axis, direction, velocity=velocity)
        started = time.monotonic()
        reason = ""
        try:
            while True:
                self._check_cancel(axis)
                if time.monotonic() - started > SEARCH_TIMEOUT_S:
                    raise ControllerError(
                        f"axis {axis}: far end not found within "
                        f"{SEARCH_TIMEOUT_S:.0f} s — aborting rather than "
                        "continuing to drive"
                    )

                fault = self._read_int("FAULT", axis)
                if fault >> far_bit & 1:
                    reason = f"limit switch ({switch_name})"
                    self._report(axis, f"{switch_name} limit switch reached")
                    break

                pe = abs(self._read_real("PE", axis))
                if pe > pe_threshold:
                    reason = "hard stop"
                    self._report(axis, f"hard stop detected (PE {pe:.0f}, "
                                       f"no {switch_name} switch on this end)")
                    break

                merr = self._read_int("MERR", axis)
                if merr in LIMIT_MERR:
                    name, limit_direction = LIMIT_MERR[merr]
                    if limit_direction is direction:
                        # This is the SUCCESS case. The controller disables the
                        # axis when a limit trips -- correct behaviour -- and
                        # sets MERR, so treating every MERR as a failure threw
                        # away exactly the result we were searching for.
                        # _prepare_for_motion re-enables before the next home.
                        reason = f"limit switch ({name})"
                        self._report(axis, f"{name} limit switch reached "
                                           f"(MERR {merr})")
                        break
                    raise ControllerError(
                        f"axis {axis}: hit the {name} limit while searching "
                        f"{direction.name.lower()} — that is the wrong end. "
                        f"Check the switch wiring or the direction convention."
                    )

                # Any other fault means something went wrong; stop rather
                # than push through it.
                if merr:
                    raise ControllerError(
                        f"axis {axis} faulted during search: MERR {merr} "
                        f"({self.controller.error_text('?%d' % merr)})"
                    )

                time.sleep(POLL_INTERVAL_S)
        finally:
            try:
                self.controller.halt(axis)
            except Exception:
                log.warning("halt after search failed", exc_info=True)

        # Let the profiled stop finish before reading the resting position.
        time.sleep(0.3)
        position = self._read_real("FPOS", axis)

        # Ease off the stop. Leaving the stage pressed against it holds a
        # standing position error, which faults the axis and makes the next
        # leg of the search start from a faulted state.
        if reason == "hard stop":
            self._back_off(axis, direction, velocity)

        return position, reason

    def _back_off(self, axis: int, direction: cfg.Direction,
                  velocity: float) -> None:
        """Move a short way off a hard stop and clear the resulting fault."""
        away = (cfg.Direction.POSITIVE if direction is cfg.Direction.NEGATIVE
                else cfg.Direction.NEGATIVE)
        try:
            self.controller.fault_clear(axis)
            self.controller.jog(axis, away, velocity=velocity)
            time.sleep(BACK_OFF_SECONDS)
        except Exception:
            log.warning("back-off jog failed on axis %d", axis, exc_info=True)
        finally:
            try:
                self.controller.halt(axis)
                time.sleep(0.3)
                self.controller.fault_clear(axis)
            except Exception:
                log.warning("back-off halt failed on axis %d", axis, exc_info=True)
        self._report(axis, f"backed off the stop ({away.name.lower()})")

    # -- public -------------------------------------------------------------

    def home_and_measure(self, axis: int) -> TravelRange:
        """Home an axis and measure its travel, as one operation.

        Homing and finding the ends are the same job: homing establishes the
        reference end and fixes the coordinate frame, and the opposite end is
        only meaningful within that frame. Keeping them as separate buttons
        made it possible to measure against a stale frame -- which is how a
        parked position once got recorded as an endpoint, producing limits
        that confined the stage to a band it could not escape.

        Sequence:
          1. home against the configured reference (limit switch, or hard
             stop + index)
          2. probe the opposite end with firmware hard-stop homing
          3. re-home, to restore the frame the recorded numbers belong to
        """
        axis_cfg = cfg.AXES_BY_INDEX[axis]
        self._cancel.clear()

        status = self.controller.poll().get(axis)
        if status is None:
            raise ControllerError(f"axis {axis}: no status")
        if not status.enabled:
            raise ControllerError(f"axis {axis} ({axis_cfg.name}): motor is disabled")

        home_direction = axis_cfg.homing_direction
        far_direction = (cfg.Direction.POSITIVE
                         if home_direction is cfg.Direction.NEGATIVE
                         else cfg.Direction.NEGATIVE)

        # Widen across the ENTIRE sequence, starting before the first home.
        # The existing limits are in whatever frame preceded this run and can
        # block the homing search itself -- axis 1 could not home negative
        # because its SLLIMIT was 0, and axis 0's search stopped dead on
        # SRLIMIT. Nothing measured here is valid until the axis is homed, so
        # the old limits have no authority during the sequence.
        with self._limits_widened(axis) as limits_state:
            # Each end is measured by an explicit probe, and the frame is
            # restored by re-homing after each one. That is five traverses on
            # an index-homed axis, and the count is deliberate.
            #
            # A three-traverse version was tried: infer the reference-end stop
            # from the initial home, which already drives to it before backing
            # off to the index. It does not work reliably. Homing reports
            # complete at essentially the same moment it re-zeroes, so the
            # observation of the pre-re-zero position is a race. Measured
            # against the explicit probe on axis 0, the positive end agreed to
            # 583 counts but the inferred negative end was wrong by 172,908.
            #
            # Wrong limits confine the stage to a band it cannot escape, so
            # correctness wins over saving ~40 s on a per-power-cycle
            # operation.
            self._report(axis, "homing against the reference end")
            self._home_with_recovery(axis)
            reference_end = self._read_real("FPOS", axis)
            self._report(axis, f"homed at {reference_end:.0f}", reference_end)
            start = reference_end

            far_end, _ = self._probe_end(axis, far_direction)
            self._report(axis, "re-homing to restore the reference frame")
            self._prepare_for_motion(axis)
            self.controller.home(axis)
            self._wait_for_homing(axis)

            if axis_cfg.has_index:
                # The reference is an INDEX, which sits *inside* travel --
                # method 50/51 finds the hard stop then backs off to the first
                # index, and on this machine that index is near the middle of
                # the stage. Treating it as the end of travel discarded half
                # of axis 0's range.
                self._report(axis, "probing the reference end for its hard "
                                   "stop (the index sits inside travel)")
                near_end, used_firmware = self._probe_end(axis, home_direction)
                near_ref = "hard stop"

                # The probe can be skipped as a re-home only when firmware
                # homing did it AND used this axis's own homing method: it
                # then ended by zeroing on the same index, which is the frame
                # we want. A host-side fallback leaves the axis at the stop,
                # so it always needs a re-home.
                probe_method = (HARD_STOP_METHOD_WITH_INDEX
                                if axis_cfg.has_index
                                else HARD_STOP_METHOD_NO_INDEX)[home_direction]
                if used_firmware and probe_method == axis_cfg.homing_method:
                    self._report(axis, "already homed by the probe — no "
                                       "re-home needed")
                else:
                    self._report(axis, "re-homing to restore the reference frame")
                    self._prepare_for_motion(axis)
                    self.controller.home(axis)
                    self._wait_for_homing(axis)
            else:
                # Homed on a limit switch: the switch *is* the intended end of
                # travel, so there is nothing beyond it worth using.
                near_end, near_ref = reference_end, "limit switch"

            lo, hi = sorted((near_end, far_end))
            if far_direction is cfg.Direction.POSITIVE:
                min_ref, max_ref = near_ref, "hard stop"
            else:
                min_ref, max_ref = "hard stop", near_ref

            # A span that does not enclose the reference end means one of the
            # "ends" was not an end at all -- a soft limit, a fault, or a
            # premature stop. Recording it would confine the axis to a band it
            # cannot escape, which is what happened to axis 0 when a parked
            # position was mistaken for an endpoint. Raising here leaves the
            # context manager to restore the original limits.
            if not (lo - 1 <= start <= hi + 1):
                raise ControllerError(
                    f"axis {axis}: measured range {lo:.0f} .. {hi:.0f} does "
                    f"not contain the reference end {start:.0f}. One end was "
                    f"not a real limit — refusing to record it."
                )

            # Apply the measured limits now, while still inside the widened
            # block, so the stale ones never reapply to the new frame.
            #
            # TravelRange.safe_limits applies the margin only at the probed
            # end -- the homed end gets none, or the axis would land outside
            # its own limit every time it homes.
            measured = TravelRange(
                axis=axis, min_counts=lo, max_counts=hi,
                min_reference=min_ref, max_reference=max_ref,
                measured_at=datetime.now().isoformat(timespec="seconds"),
                homed_zero=reference_end,
            )
            safe_lo, safe_hi = measured.safe_limits()
            self._write_real("SLLIMIT", axis, safe_lo)
            self._write_real("SRLIMIT", axis, safe_hi)
            limits_state["replaced"] = True
            self._report(axis, f"soft limits set from measurement: "
                               f"{safe_lo:.0f} .. {safe_hi:.0f} "
                               f"(no margin at the homed end, {reference_end:.0f})")

        homed_end = reference_end

        result = TravelRange(
            axis=axis,
            min_counts=lo,
            max_counts=hi,
            min_reference=min_ref,
            max_reference=max_ref,
            measured_at=datetime.now().isoformat(timespec="seconds"),
            homed_zero=homed_end,
        )
        self._report(axis, f"travel = {result.span:.0f} counts "
                           f"({lo:.0f} .. {hi:.0f})", done=True)
        return result


def write_soft_limits(controller: StageController, travel: TravelRange,
                      margin_fraction: float | None = None) -> tuple[float, float]:
    """Push a measured range to the controller as SLLIMIT / SRLIMIT.

    This is what makes the calibration protective rather than advisory: the
    controller enforces these every cycle and predictively, where a UI check
    runs at 10 Hz and dies with the process.

    Volatile until saved to flash from MMI.
    """
    from .travel import DEFAULT_MARGIN_FRACTION
    lo, hi = travel.safe_limits(
        DEFAULT_MARGIN_FRACTION if margin_fraction is None else margin_fraction)
    hc = controller._require()
    with controller._lock:
        sp.WriteReal(hc, NONE, "SLLIMIT", travel.axis, travel.axis, NONE, NONE,
                     lo, sp.SYNCHRONOUS, True)
        sp.WriteReal(hc, NONE, "SRLIMIT", travel.axis, travel.axis, NONE, NONE,
                     hi, sp.SYNCHRONOUS, True)
    log.info("axis %d soft limits set to %.0f .. %.0f", travel.axis, lo, hi)
    return lo, hi
