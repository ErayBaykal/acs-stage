"""Xbox controller input via XInput.

XInput is the native Windows API for Xbox pads, so it is reached directly
through ctypes rather than pulling in a game library. That gives exact Xbox
button semantics, correct trigger/stick ranges, and hot-plug detection for
free, with no dependency to install on the lab PC.

Reading only -- this module has no knowledge of the stage. Mapping buttons to
motion is the caller's job.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167
MAX_CONTROLLERS = 4

# Button bit flags, from XInput.h.
BUTTONS = {
    "DPAD_UP": 0x0001,
    "DPAD_DOWN": 0x0002,
    "DPAD_LEFT": 0x0004,
    "DPAD_RIGHT": 0x0008,
    "START": 0x0010,
    "BACK": 0x0020,
    "LEFT_THUMB": 0x0040,       # left stick pressed in
    "RIGHT_THUMB": 0x0080,      # right stick pressed in
    "LB": 0x0100,
    "RB": 0x0200,
    "A": 0x1000,
    "B": 0x2000,
    "X": 0x4000,
    "Y": 0x8000,
}

# Microsoft's recommended dead zones, from XInput.h. Sticks rest slightly off
# centre, so raw values must be ignored below these.
LEFT_THUMB_DEADZONE = 7849
RIGHT_THUMB_DEADZONE = 8689
TRIGGER_THRESHOLD = 30

STICK_MAX = 32767.0
TRIGGER_MAX = 255.0


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wintypes.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", wintypes.DWORD),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


def _load_xinput():
    """XInput ships under several names depending on Windows version."""
    for name in ("XInput1_4.dll", "XInput1_3.dll", "XInput9_1_0.dll"):
        try:
            return ctypes.WinDLL(name), name
        except OSError:
            continue
    return None, None


_XINPUT, _XINPUT_NAME = _load_xinput()


def _apply_deadzone(x: int, y: int, deadzone: int) -> tuple[float, float]:
    """Scale a stick to -1..1, with the dead zone removed smoothly.

    Rescaling the remaining range (rather than just zeroing inside the dead
    zone) means the output starts from 0 at the dead-zone edge instead of
    jumping, which matters when a stick drives velocity.
    """
    magnitude = (x * x + y * y) ** 0.5
    if magnitude <= deadzone:
        return 0.0, 0.0
    # Direction, then magnitude rescaled from the dead-zone edge to full.
    magnitude = min(magnitude, STICK_MAX)
    scaled = (magnitude - deadzone) / (STICK_MAX - deadzone)
    return (x / magnitude) * scaled, (y / magnitude) * scaled


@dataclass
class GamepadState:
    connected: bool = False
    slot: int | None = None
    packet: int = 0
    pressed: set[str] = field(default_factory=set)
    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0
    left_trigger: float = 0.0
    right_trigger: float = 0.0

    def held(self, *names: str) -> bool:
        """True if every named button is currently held — for combinations."""
        return all(n in self.pressed for n in names)

    def describe(self) -> str:
        parts = []
        if self.pressed:
            parts.append("+".join(sorted(self.pressed)))
        if self.left_x or self.left_y:
            parts.append(f"L({self.left_x:+.2f},{self.left_y:+.2f})")
        if self.right_x or self.right_y:
            parts.append(f"R({self.right_x:+.2f},{self.right_y:+.2f})")
        if self.left_trigger:
            parts.append(f"LT{self.left_trigger:.2f}")
        if self.right_trigger:
            parts.append(f"RT{self.right_trigger:.2f}")
        return "  ".join(parts) if parts else "—"


class Gamepad:
    """Polls the first connected XInput controller.

    Call poll() on a timer. It re-scans for a controller when none is
    connected, so plugging the pad in mid-session just works.
    """

    def __init__(self):
        self.available = _XINPUT is not None
        self.driver = _XINPUT_NAME
        self._slot: int | None = None
        self._state = XINPUT_STATE()

    def _read(self, slot: int) -> XINPUT_STATE | None:
        if _XINPUT is None:
            return None
        state = XINPUT_STATE()
        result = _XINPUT.XInputGetState(slot, ctypes.byref(state))
        return state if result == ERROR_SUCCESS else None

    def poll(self) -> GamepadState:
        if _XINPUT is None:
            return GamepadState()

        # Stay on the known slot while it responds; otherwise scan.
        slots = ([self._slot] if self._slot is not None else []) + [
            s for s in range(MAX_CONTROLLERS) if s != self._slot
        ]
        for slot in slots:
            if slot is None:
                continue
            raw = self._read(slot)
            if raw is None:
                continue
            if slot != self._slot:
                log.info("gamepad found in slot %d", slot)
                self._slot = slot
            return self._decode(raw, slot)

        if self._slot is not None:
            log.info("gamepad disconnected")
            self._slot = None
        return GamepadState()

    def _decode(self, raw: XINPUT_STATE, slot: int) -> GamepadState:
        pad = raw.Gamepad
        lx, ly = _apply_deadzone(pad.sThumbLX, pad.sThumbLY, LEFT_THUMB_DEADZONE)
        rx, ry = _apply_deadzone(pad.sThumbRX, pad.sThumbRY, RIGHT_THUMB_DEADZONE)
        lt = (pad.bLeftTrigger / TRIGGER_MAX
              if pad.bLeftTrigger > TRIGGER_THRESHOLD else 0.0)
        rt = (pad.bRightTrigger / TRIGGER_MAX
              if pad.bRightTrigger > TRIGGER_THRESHOLD else 0.0)
        return GamepadState(
            connected=True,
            slot=slot,
            packet=raw.dwPacketNumber,
            pressed={n for n, bit in BUTTONS.items() if pad.wButtons & bit},
            left_x=lx, left_y=ly, right_x=rx, right_y=ry,
            left_trigger=lt, right_trigger=rt,
        )
