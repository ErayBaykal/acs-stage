"""Host side of the controller watchdog.

The UI writes a heartbeat counter; an ACSPL+ program on the controller kills
motion if that counter goes stale. See acspl/watchdog.prg for the controller
half and why it exists.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import SPiiPlusPython as sp

from . import config as cfg
from .controller import NONE, ControllerError, StageController

log = logging.getLogger(__name__)

PROGRAM_PATH = Path(__file__).resolve().parent.parent / "acspl" / "watchdog.prg"

# Any buffer we are willing to overwrite must contain this marker, so we can
# never silently destroy an unrelated program the user wrote in MMI.
MARKER = "Host watchdog for the 5-axis stage control panel"


class WatchdogError(RuntimeError):
    pass


class Watchdog:
    def __init__(self, controller: StageController, buffer: int = cfg.WATCHDOG_BUFFER):
        self.controller = controller
        self.buffer = buffer
        self._counter = 0
        self._armed = False
        self._installed = False

    # -- installation -------------------------------------------------------

    def buffer_is_safe_to_use(self) -> tuple[bool, str]:
        """Check the target buffer is empty or already ours.

        LoadBuffer clears the buffer first, so writing to an occupied buffer
        would destroy whatever program is in it. The user configured this
        machine by hand in MMI; silently overwriting their work is not an
        acceptable failure mode.
        """
        hc = self.controller._require()
        try:
            existing = sp.UploadBuffer(hc, self.buffer, 0, 64000, sp.SYNCHRONOUS, True)
        except Exception as exc:
            return False, f"could not read buffer {self.buffer}: {exc}"

        text = (existing or "").strip()
        if not text:
            return True, f"buffer {self.buffer} is empty"
        if MARKER in text:
            return True, f"buffer {self.buffer} already holds the watchdog"
        preview = text.splitlines()[0][:60] if text.splitlines() else ""
        return False, (
            f"buffer {self.buffer} already contains another program "
            f"(starts {preview!r}). Pick a different WATCHDOG_BUFFER in "
            f"config.py rather than overwriting it."
        )

    def install(self, force: bool = False) -> None:
        """Load, compile and start the watchdog program on the controller."""
        hc = self.controller._require()

        if not force:
            ok, reason = self.buffer_is_safe_to_use()
            if not ok:
                raise WatchdogError(reason)
            log.info("watchdog buffer check: %s", reason)

        if not PROGRAM_PATH.exists():
            raise WatchdogError(f"watchdog program not found at {PROGRAM_PATH}")
        source = PROGRAM_PATH.read_text(encoding="ascii")

        try:
            sp.StopBuffer(hc, self.buffer, sp.SYNCHRONOUS, True)
        except Exception:
            pass  # not running is fine

        # Load and compile before touching any of the protocol variables:
        # they are globals declared *by this program*, so they do not exist
        # until it is compiled. Writing them first fails with error 1064,
        # "Undefined global variable".
        sp.LoadBuffer(hc, self.buffer, source, len(source), sp.SYNCHRONOUS, True)
        sp.CompileBuffer(hc, self.buffer, sp.SYNCHRONOUS, True)
        sp.RunBuffer(hc, self.buffer, None, sp.SYNCHRONOUS, True)

        # The program zeroes HOSTWDEN/HOSTWDFIRED itself on startup, so the
        # timeout is the only value the host needs to supply.
        self._write("HOSTWDTMO", cfg.WATCHDOG_TIMEOUT_MS)

        self._installed = True
        log.info("watchdog installed in buffer %d", self.buffer)

    # -- runtime -------------------------------------------------------------

    def arm(self, attempts: int = 10, delay_s: float = 0.05) -> None:
        """Arm the watchdog and confirm it actually took.

        The controller program zeroes HOSTWDEN during its own startup, which
        can race a write issued immediately after RunBuffer. Rather than
        assume, write and read back -- an unarmed watchdog that reports itself
        as armed is worse than no watchdog at all.
        """
        if not self._installed:
            raise WatchdogError("watchdog not installed")

        self._write("HOSTWDTMO", cfg.WATCHDOG_TIMEOUT_MS)
        for attempt in range(attempts):
            self.beat()  # fresh deadline before arming
            self._write("HOSTWDEN", 1)
            if self._read("HOSTWDEN") == 1:
                self._armed = True
                log.info(
                    "watchdog armed (%d ms timeout, %d ms heartbeat)",
                    cfg.WATCHDOG_TIMEOUT_MS, cfg.WATCHDOG_PERIOD_MS,
                )
                return
            time.sleep(delay_s)

        raise WatchdogError(
            "watchdog would not arm: HOSTWDEN reads back 0 after "
            f"{attempts} attempts. The program in buffer {self.buffer} may "
            "not be running."
        )

    def disarm(self) -> None:
        """Disarm for a clean shutdown, so a deliberate exit is not a kill."""
        if not self._installed:
            return
        try:
            self._write("HOSTWDEN", 0)
        except Exception:
            log.warning("could not disarm watchdog", exc_info=True)
        self._armed = False

    def beat(self) -> None:
        self._counter = (self._counter + 1) % 1_000_000
        self._write("HOSTWDOG", self._counter)

    @property
    def armed(self) -> bool:
        return self._armed

    def fired(self) -> bool:
        try:
            return bool(self._read("HOSTWDFIRED"))
        except Exception:
            return False

    def clear_fired(self) -> None:
        self._write("HOSTWDFIRED", 0)

    # -- variable access ------------------------------------------------------

    def _write(self, name: str, value: int) -> None:
        hc = self.controller._require()
        with self.controller._lock:
            sp.WriteInteger(hc, NONE, name, NONE, NONE, NONE, NONE,
                            int(value), sp.SYNCHRONOUS, True)

    def _read(self, name: str) -> int:
        hc = self.controller._require()
        with self.controller._lock:
            return int(sp.ReadInteger(hc, NONE, name, NONE, NONE, NONE, NONE,
                                      sp.SYNCHRONOUS, True))
