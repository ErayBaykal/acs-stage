"""Drive stage axes from the gamepad.

Hold a button to select an axis and arm motion, push the stick to move it.
The button is a dead-man switch: release it and the axis halts.

Kept apart from the raw input layer (gamepad.py) and from the UI so the
mapping and its safety rules can be read in one place.
"""
from __future__ import annotations

import logging

from . import config as cfg
from .controller import ControllerError, StageController
from .gamepad import GamepadState

log = logging.getLogger(__name__)


class GamepadJog:
    """Translates gamepad state into jog commands.

    Only ever commands ONE axis at a time. Holding two binding buttons at
    once is ambiguous rather than a request to move two things, so it stops
    instead of guessing.
    """

    def __init__(self, controller: StageController, travel_store):
        self.controller = controller
        self.travel = travel_store
        self._active_axis: int | None = None
        self._commanded: float = 0.0
        self._last_reason: str = ""

    # -- mapping ---------------------------------------------------------

    @staticmethod
    def _held_bindings(state: GamepadState) -> list[tuple[str, cfg.Binding]]:
        """Which bound buttons are currently held."""
        held = []
        for name, binding in cfg.GAMEPAD_BINDINGS.items():
            if name == "RT":
                if state.right_trigger >= cfg.TRIGGER_HELD_THRESHOLD:
                    held.append((name, binding))
            elif name == "LT":
                if state.left_trigger >= cfg.TRIGGER_HELD_THRESHOLD:
                    held.append((name, binding))
            elif name in state.pressed:
                held.append((name, binding))
        return held

    @staticmethod
    def _response(deflection: float) -> float:
        """Shape stick deflection into a speed fraction.

        Raw deflection maps linearly, which makes slow, precise motion hard:
        the usable fine-control band is squeezed into the first few degrees of
        stick travel. Raising it to a power expands that band -- with the
        default exponent of 2, half deflection gives a quarter speed and a
        quarter gives a sixteenth, so most of the stick's range is devoted to
        slow motion while full deflection still reaches full speed.

        Sign is preserved; only the magnitude is curved.
        """
        magnitude = abs(deflection) ** cfg.GAMEPAD_RESPONSE_EXPONENT
        return magnitude if deflection >= 0 else -magnitude

    @staticmethod
    def _stick_value(state: GamepadState, stick: cfg.Stick) -> float:
        return {
            cfg.Stick.LEFT_X: state.left_x,
            cfg.Stick.LEFT_Y: state.left_y,
            cfg.Stick.RIGHT_X: state.right_x,
            cfg.Stick.RIGHT_Y: state.right_y,
        }[stick]

    # -- main entry ------------------------------------------------------

    def update(self, state: GamepadState) -> str:
        """Apply one gamepad sample. Returns a short status for display."""
        if not self.controller.connected or not state.connected:
            self._stop("not connected")
            return ""

        held = self._held_bindings(state)

        if len(held) > 1:
            names = "+".join(n for n, _ in held)
            self._stop(f"{names} held together — ambiguous")
            return f"{names}: hold only one"
        if not held:
            self._stop("no button held")
            return ""

        name, binding = held[0]
        axis = binding.axis
        deflection = self._stick_value(state, binding.stick)
        if binding.invert:
            deflection = -deflection
        deflection = self._response(deflection)

        # A held button with a centred stick means "armed, not moving".
        if deflection == 0.0:
            self._stop("stick centred")
            return f"{name} → axis {axis}: ready"

        maximum = self.controller.gamepad_velocity(axis)
        if maximum is None:
            self._stop("no jog velocity")
            return f"{name} → axis {axis}: no velocity configured"
        velocity = maximum * deflection

        # Switching axis mid-motion must stop the old one first, or it would
        # be left running with nothing watching it.
        if self._active_axis is not None and self._active_axis != axis:
            self._stop("axis changed")

        if not self._should_resend(velocity, maximum):
            return f"{name} → axis {axis}: {self._commanded:+,.0f} cts/s"

        direction = (cfg.Direction.POSITIVE if velocity > 0
                     else cfg.Direction.NEGATIVE)
        try:
            self.controller.jog(axis, direction,
                                velocity=abs(velocity),
                                travel=self.travel.get(axis))
        except ControllerError as exc:
            # At a travel limit, or the axis is not ready. Report it without
            # spamming: the message only changes when the reason changes.
            reason = str(exc)
            if reason != self._last_reason:
                log.info("gamepad jog refused: %s", reason)
                self._last_reason = reason
            self._active_axis = None
            self._commanded = 0.0
            return f"{name} → axis {axis}: blocked"
        except Exception as exc:
            log.warning("gamepad jog failed on axis %d: %s", axis, exc)
            return f"{name} → axis {axis}: error"

        # Log when a jog starts on a new axis, not on every velocity update --
        # enough to see gamepad activity in the log without drowning it at the
        # poll rate.
        if self._active_axis != axis:
            # Pre-format the number: %-style logging has no thousands
            # separator, so "%+,.0f" raises at emit time and buries the log
            # in tracebacks.
            log.info("gamepad jog: %s -> axis %d (%s) at %s cts/s",
                     name, axis, cfg.AXES_BY_INDEX[axis].name,
                     f"{velocity:+,.0f}")
        self._active_axis = axis
        self._commanded = velocity
        self._last_reason = ""
        return f"{name} → axis {axis}: {velocity:+,.0f} cts/s"

    def _should_resend(self, velocity: float, maximum: float) -> bool:
        """Avoid re-issuing near-identical jogs at the poll rate."""
        if self._active_axis is None:
            return True
        if (velocity > 0) != (self._commanded > 0):
            return True          # direction flipped
        change = abs(velocity - self._commanded) / abs(maximum or 1.0)
        return change > cfg.GAMEPAD_VELOCITY_HYSTERESIS

    def _stop(self, why: str) -> None:
        if self._active_axis is None:
            return
        axis = self._active_axis
        self._active_axis = None
        self._commanded = 0.0
        try:
            self.controller.halt(axis)
            log.info("gamepad jog: stopped axis %d (%s)", axis, why)
        except Exception as exc:
            log.warning("gamepad halt failed on axis %d: %s", axis, exc)

    def stop_all(self) -> None:
        """Unconditional stop — for E-stop, disconnect, or shutdown."""
        self._stop("stop_all")
        self._last_reason = ""

    @property
    def active_axis(self) -> int | None:
        return self._active_axis
