"""Control panel window: live status, keyboard jog, homing, E-stop."""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QStatusBar, QVBoxLayout, QWidget,
)

from .. import config as cfg
from ..calibrate import (CalibrationCancelled, TravelCalibrator,
                         write_soft_limits)
from ..controller import ControllerError, StageController
from ..gamepad import BUTTONS, Gamepad
from ..gamepad_jog import GamepadJog
from ..travel import TravelStore
from ..watchdog import Watchdog, WatchdogError

log = logging.getLogger(__name__)

DOT_OK = "#3ba55d"
DOT_OFF = "#5a5a5a"
DOT_WARN = "#d4a017"
DOT_BAD = "#cf2155"


def _dot(colour: str) -> str:
    return f"<span style='color:{colour};font-size:16px'>&#9679;</span>"


class AxisRow:
    """One row of the status grid, plus the widgets that show it."""

    def __init__(self, axis: cfg.AxisConfig, grid: QGridLayout, row: int,
                 on_home, on_commutate):
        self.axis = axis
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)

        self.name = QLabel(f"{axis.index}  {axis.name}")
        self.position = QLabel("--")
        self.position.setFont(mono)
        self.position.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.enabled = QLabel(_dot(DOT_OFF))
        self.moving = QLabel(_dot(DOT_OFF))
        self.commutated = QLabel(_dot(DOT_OFF))
        self.homed = QLabel(_dot(DOT_OFF))
        self.keys = QLabel(f"{axis.key_negative} / {axis.key_positive}")
        self.keys.setStyleSheet("color:#888")

        self.commutate_button = QPushButton("Comm")
        self.commutate_button.setToolTip(
            f"COMMUT {axis.index} — auto-commutation. Needed after every "
            "controller power-up. The motor may jump up to one magnetic pitch."
        )
        self.commutate_button.clicked.connect(lambda: on_commutate(axis.index))

        self.home_button = QPushButton("Home")
        self.home_button.clicked.connect(lambda: on_home(axis.index))
        if axis.can_home:
            self.home_button.setToolTip(
                f"Home and measure travel:\n"
                f"  1. HOME {axis.index},{axis.homing_method} "
                f"({axis.homing_direction.name.lower()})\n"
                f"  2. probe the opposite end\n"
                f"  3. re-home, then set soft limits from the result"
            )
        else:
            # Blocked rather than merely discouraged: an unwired limit switch
            # means the search never terminates.
            self.home_button.setEnabled(False)
            self.home_button.setText("Home ⛔")
            self.home_button.setToolTip(axis.blocked_reason)
            self.name.setStyleSheet("color:#888")

        self.travel_label = QLabel("not measured")
        self.travel_label.setStyleSheet("color:#888")

        for col, w in enumerate(
            (self.name, self.position, self.enabled, self.moving,
             self.commutated, self.homed, self.keys, self.travel_label,
             self.commutate_button, self.home_button)
        ):
            grid.addWidget(w, row, col)

    def set_travel(self, travel_range) -> None:
        if travel_range is None:
            self.travel_label.setText("not calibrated")
            self.travel_label.setStyleSheet("color:#888")
            self.travel_label.setToolTip(
                "Travel has not been measured. Nothing in the UI restricts "
                "how far this axis can be jogged."
            )
            return
        lo, hi = travel_range.safe_limits()
        self.travel_label.setText(f"{lo:+.0f} .. {hi:+.0f}")
        self.travel_label.setStyleSheet(f"color:{DOT_OK}")
        self.travel_label.setToolTip(
            f"measured {travel_range.min_counts:.0f} .. "
            f"{travel_range.max_counts:.0f} "
            f"(span {travel_range.span:.0f})\n"
            f"low end: {travel_range.min_reference}\n"
            f"high end: {travel_range.max_reference}\n"
            f"measured {travel_range.measured_at}"
        )

    def update(self, status, error_text: str = "") -> None:
        self.position.setText(f"{self.axis.to_display(status.position):+12.1f} {self.axis.unit}")
        if status.motor_error:
            self.name.setText(f"{self.axis.index}  {self.axis.name}  ⚠")
            self.name.setStyleSheet(f"color:{DOT_BAD}")
            self.name.setToolTip(f"MERR {status.motor_error}: {error_text}")
        else:
            self.name.setText(f"{self.axis.index}  {self.axis.name}")
            self.name.setStyleSheet("" if self.axis.can_home else "color:#888")
            self.name.setToolTip("")
        self.enabled.setText(_dot(DOT_OK if status.enabled else DOT_OFF))
        self.moving.setText(_dot(DOT_WARN if status.moving else DOT_OFF))
        if not status.needs_commutation:
            # Not a controller-commutated brushless motor: #BRUSHOK is
            # meaningless here and would otherwise sit red forever.
            self.commutated.setText("<span style='color:#666'>n/a</span>")
            self.commutate_button.setEnabled(False)
            self.commutate_button.setToolTip(
                "Not a controller-commutated brushless motor "
                "(MFLAGS.#BRUSHL = 0) — commutation does not apply"
            )
        else:
            self.commutated.setText(_dot(DOT_OK if status.commutated else DOT_BAD))
            self.commutate_button.setEnabled(status.enabled and not status.moving)
        self.homed.setText(_dot(DOT_OK if status.homed else DOT_BAD))
        # Homing needs commutation first, so surface the ordering rather than
        # letting the user press Home and get a rejection.
        if self.axis.can_home:
            self.home_button.setEnabled(status.ready_to_home)
            self.home_button.setToolTip(
                status.not_ready_reason()
                or f"HOME {self.axis.index},{self.axis.homing_method} "
                   f"({self.axis.homing_direction.name.lower()})"
            )

    def clear(self) -> None:
        self.position.setText("--")
        for w in (self.enabled, self.moving, self.commutated, self.homed):
            w.setText(_dot(DOT_OFF))

    def set_connected(self, connected: bool) -> None:
        self.home_button.setEnabled(connected and self.axis.can_home)
        self.commutate_button.setEnabled(connected)


class MainWindow(QMainWindow):
    # Calibration runs on a worker thread; Qt widgets may only be touched from
    # the GUI thread, so results come back as signals.
    calibration_progress = Signal(object)
    calibration_finished = Signal(int, object, str)

    def __init__(self, controller: StageController):
        super().__init__()
        self.controller = controller
        self.watchdog = Watchdog(controller)
        self.travel = TravelStore()
        self.gamepad = Gamepad()
        self.gamepad_jog = GamepadJog(controller, self.travel)
        self._gamepad_state = None
        self._gamepad_was_connected = False
        self._poll_failures = 0
        self._calibrator: TravelCalibrator | None = None
        self._calibration_thread: threading.Thread | None = None
        self._watchdog_fired_seen = False
        self._jogging: dict[int, cfg.Direction] = {}
        self._key_map: dict[str, tuple[int, cfg.Direction]] = {}
        for axis in cfg.AXES:
            self._key_map[axis.key_negative.lower()] = (axis.index, cfg.Direction.NEGATIVE)
            self._key_map[axis.key_positive.lower()] = (axis.index, cfg.Direction.POSITIVE)

        self.setWindowTitle("5-Axis Stage Control")
        self.setMinimumWidth(620)
        self._build()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(cfg.POLL_PERIOD_MS)

        # Separate, faster timer: the heartbeat must not be delayed by a slow
        # status poll, or the watchdog would trip on a healthy system.
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self._heartbeat)
        self.heartbeat_timer.start(cfg.WATCHDOG_PERIOD_MS)

        # Gamepad polling is independent of the controller connection so the
        # pad can be identified and tested before anything is powered.
        self.gamepad_timer = QTimer(self)
        self.gamepad_timer.timeout.connect(self._poll_gamepad)
        self.gamepad_timer.start(cfg.GAMEPAD_POLL_MS)

        self.calibration_progress.connect(self._on_calibration_progress)
        self.calibration_finished.connect(self._on_calibration_finished)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)

        conn = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._toggle_connection)
        self.connection_label = QLabel(
            f"{_dot(DOT_BAD)} {self.controller.host}:{self.controller.port}"
        )
        self.watchdog_label = QLabel(f"{_dot(DOT_OFF)} watchdog idle")
        conn.addWidget(self.connect_button)
        conn.addWidget(self.connection_label)
        conn.addStretch()
        conn.addWidget(self.watchdog_label)
        outer.addLayout(conn)

        box = QGroupBox("Axes")
        grid = QGridLayout(box)
        for col, title in enumerate(
            ("Axis", "Position", "En", "Mov", "Comm", "Homed", "Keys",
             "Travel", "", "", "")
        ):
            header = QLabel(f"<b>{title}</b>")
            grid.addWidget(header, 0, col)
        self.rows = [
            AxisRow(a, grid, i + 1, self._home_axis, self._commutate_axis)
            for i, a in enumerate(cfg.AXES)
        ]
        for row in self.rows:
            row.set_travel(self.travel.get(row.axis.index))
        outer.addWidget(box)

        blocked = [a for a in cfg.AXES if not a.can_home]
        if blocked:
            warning = QLabel(
                "⚠ Homing disabled on "
                + ", ".join(a.name for a in blocked)
                + " — limit switches not marked as connected. "
                "Enable per axis in <code>config.py</code> once wired."
            )
            warning.setWordWrap(True)
            warning.setStyleSheet(
                "color:#d4a017;border:1px solid #d4a017;border-radius:4px;padding:6px"
            )
            outer.addWidget(warning)

        actions = QHBoxLayout()
        self.enable_button = QPushButton("Enable All")
        self.enable_button.clicked.connect(self._enable_all)
        self.disable_button = QPushButton("Disable All")
        self.disable_button.clicked.connect(self._disable_all)
        self.flash_button = QPushButton("Save to Flash")
        self.flash_button.setToolTip(
            "Persist axis parameters (soft limits, MFLAGS) to controller "
            "flash so they survive a power cycle. Flash is rated for ~100,000 "
            "writes, so do this when a configuration is settled, not routinely."
        )
        self.flash_button.clicked.connect(self._save_to_flash)
        self.clear_button = QPushButton("Clear Faults")
        self.clear_button.setToolTip(
            "FCLEAR on every axis. Needed after a fault (e.g. critical "
            "position error) before the motor will enable again."
        )
        self.clear_button.clicked.connect(self._clear_faults)
        self.home_button = QPushButton("Home All")
        self.home_button.clicked.connect(self._home_all)
        actions.addWidget(self.enable_button)
        actions.addWidget(self.disable_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.home_button)
        actions.addWidget(self.flash_button)
        actions.addStretch()

        self.estop_button = QPushButton("STOP")
        self.estop_button.setStyleSheet(
            "background:#cf2155;color:white;font-weight:bold;padding:6px 22px"
        )
        self.estop_button.setShortcut(QKeySequence(Qt.Key_Escape))
        self.estop_button.setToolTip("Kill all motion (Esc)")
        self.estop_button.clicked.connect(self._estop)
        actions.addWidget(self.estop_button)
        outer.addLayout(actions)

        hint = QLabel(
            "Hold a key to jog, release to stop. Hold <b>Shift</b> for fine speed. "
            "<b>Esc</b> kills all motion."
        )
        hint.setStyleSheet("color:#888")
        outer.addWidget(hint)

        outer.addWidget(self._build_gamepad_box())

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

        # Keep keyboard focus on the window itself. Otherwise Qt moves focus
        # between buttons, which both swallows keys meant for jogging and
        # leaves a button armed for Space/Enter -- an accidental Home or
        # Find Limits is not a good way to discover that.
        for button in root.findChildren(QPushButton):
            button.setFocusPolicy(Qt.NoFocus)
        self.setFocusPolicy(Qt.StrongFocus)

        self._set_controls_enabled(False)

    def _build_gamepad_box(self) -> QGroupBox:
        """Controller status and a live input readout.

        The readout is deliberately raw -- it names exactly the buttons and
        axes the code sees, so a mapping can be described unambiguously
        instead of by guessing which button is "the top one".
        """
        box = QGroupBox("Gamepad")
        layout = QVBoxLayout(box)

        self.gamepad_status = QLabel()
        layout.addWidget(self.gamepad_status)

        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.gamepad_input = QLabel("—")
        self.gamepad_input.setFont(mono)
        self.gamepad_input.setStyleSheet("color:#3ba55d")
        self.gamepad_input.setMinimumHeight(20)
        layout.addWidget(self.gamepad_input)

        self.gamepad_jog_label = QLabel("—")
        self.gamepad_jog_label.setFont(mono)
        self.gamepad_jog_label.setStyleSheet("color:#d4a017")
        self.gamepad_jog_label.setMinimumHeight(20)
        layout.addWidget(self.gamepad_jog_label)

        rows = []
        for name, binding in cfg.GAMEPAD_BINDINGS.items():
            axis_cfg = cfg.AXES_BY_INDEX[binding.axis]
            arrows = "← →" if binding.stick.value.endswith("_x") else "↑ ↓"
            rows.append(f"<b>{name}</b> + stick {arrows} → {axis_cfg.name}")
        mapping = QLabel("hold: " + "   ·   ".join(rows)
                         + "<br><span style='color:#888'>release the button to "
                           "stop · stick deflection sets speed · one button at "
                           "a time</span>")
        mapping.setWordWrap(True)
        mapping.setStyleSheet("font-size:11px")
        layout.addWidget(mapping)

        if not self.gamepad.available:
            self.gamepad_status.setText(
                f"{_dot(DOT_BAD)} XInput not available on this machine")
        return box

    def _poll_gamepad(self) -> None:
        state = self.gamepad.poll()
        if state.connected:
            if not self._gamepad_was_connected:
                log.info("gamepad connected in slot %s", state.slot)
            self.gamepad_status.setText(
                f"{_dot(DOT_OK)} Xbox controller connected (slot {state.slot})")
            self.gamepad_input.setText(state.describe())
        else:
            if self._gamepad_was_connected:
                # Losing the pad mid-jog must stop the axis, not leave it
                # running with nothing holding the dead-man button.
                log.info("gamepad disconnected")
                self.gamepad_jog.stop_all()
            self.gamepad_status.setText(
                f"{_dot(DOT_OFF)} no controller detected — plug in the USB "
                f"receiver" if self.gamepad.available
                else f"{_dot(DOT_BAD)} XInput not available on this machine")
            self.gamepad_input.setText("—")
        self._gamepad_was_connected = state.connected
        self._gamepad_state = state

        activity = self.gamepad_jog.update(state)
        self.gamepad_jog_label.setText(activity or "—")

    def _set_controls_enabled(self, on: bool) -> None:
        for w in (self.enable_button, self.disable_button, self.clear_button,
                  self.home_button, self.flash_button, self.estop_button):
            w.setEnabled(on)
        for row in self.rows:
            row.set_connected(on)

    # -- connection --------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.controller.connected:
            self.gamepad_jog.stop_all()
            self.watchdog.disarm()
            self.watchdog_label.setText(f"{_dot(DOT_OFF)} watchdog idle")
            self.watchdog_label.setToolTip("")
            self.controller.disconnect()
            self.connect_button.setText("Connect")
            self.connection_label.setText(
                f"{_dot(DOT_BAD)} {self.controller.host}:{self.controller.port}"
            )
            self._set_controls_enabled(False)
            for row in self.rows:
                row.clear()
            return
        try:
            self.controller.connect()
        except ControllerError as exc:
            QMessageBox.critical(self, "Connection failed", str(exc))
            return
        self.connect_button.setText("Disconnect")
        self.connection_label.setText(
            f"{_dot(DOT_OK)} {self.controller.host}:{self.controller.port}"
        )
        self._set_controls_enabled(True)
        self._watchdog_fired_seen = False

        # connect() aligns #INVDOUT with config; surface anything it could not
        # do, since an unaligned axis will refuse to home.
        try:
            notes = [n for n in self.controller.align_invdout()]
        except Exception:
            notes = []
        if notes:
            QMessageBox.warning(self, "Axis direction", "\n\n".join(notes))

        self._start_watchdog()

    def _start_watchdog(self) -> None:
        """Install and arm the controller-side watchdog.

        Failure here is not fatal to the session, but it does remove the only
        protection that survives losing the host -- so it is surfaced loudly
        rather than logged and forgotten.
        """
        try:
            self.watchdog.install()
            self.watchdog.arm()
        except (WatchdogError, ControllerError) as exc:
            self.watchdog_label.setText(f"{_dot(DOT_BAD)} watchdog OFF")
            self.watchdog_label.setToolTip(str(exc))
            QMessageBox.warning(
                self,
                "Watchdog not running",
                f"{exc}\n\n"
                "Jogging still works, but if this UI freezes or is killed "
                "mid-jog the controller will not stop the axis on its own.\n\n"
                "The linear stages have no limit switches.",
            )
            return
        except Exception as exc:
            log.exception("watchdog install failed")
            self.watchdog_label.setText(f"{_dot(DOT_BAD)} watchdog OFF")
            self.watchdog_label.setToolTip(str(exc))
            return
        self.watchdog_label.setText(f"{_dot(DOT_OK)} watchdog armed")
        self.watchdog_label.setToolTip(
            f"buffer {self.watchdog.buffer}, {cfg.WATCHDOG_TIMEOUT_MS} ms timeout"
        )

    def _heartbeat(self) -> None:
        if not (self.controller.connected and self.watchdog.armed):
            return
        try:
            self.watchdog.beat()
        except Exception as exc:
            log.warning("heartbeat failed: %s", exc)

    def _handle_connection_lost(self, reason: str) -> None:
        """Stop pretending to be connected once the link is clearly gone.

        Without this the panel keeps polling a dead handle, keeps logging the
        same warning, and keeps showing stale positions with a green
        indicator -- which is the worst possible display for a machine that
        might be moving. Seen for real when the controller closed the
        connection (error 182) and the UI carried on for 90 s.

        The controller-side watchdog covers the safety side: the heartbeat
        stops with the connection, and motion is killed about a second later.
        """
        log.error("connection lost: %s", reason)
        self._poll_failures = 0
        self._jogging.clear()
        self.gamepad_jog.stop_all()
        try:
            self.controller.disconnect()
        except Exception:
            log.warning("disconnect after connection loss failed", exc_info=True)

        self.connect_button.setText("Connect")
        self.connection_label.setText(
            f"{_dot(DOT_BAD)} CONNECTION LOST — {self.controller.host}")
        self.watchdog_label.setText(f"{_dot(DOT_OFF)} watchdog idle")
        self._set_controls_enabled(False)
        for row in self.rows:
            row.clear()

        QMessageBox.critical(
            self, "Connection lost",
            f"The controller closed the connection:\n\n{reason}\n\n"
            "The host heartbeat stopped with it, so the controller-side "
            "watchdog will have killed any motion.\n\n"
            "A common cause is other instances of this panel still running "
            "and holding connections — close any stale windows before "
            "reconnecting.")

    # -- polling -----------------------------------------------------------

    def _poll(self) -> None:
        if not self.controller.connected:
            return
        try:
            statuses = self.controller.poll()
        except Exception as exc:
            self._poll_failures += 1
            log.warning("poll failed (%d/%d): %s",
                        self._poll_failures, cfg.POLL_FAILURES_BEFORE_LOST, exc)
            if self._poll_failures >= cfg.POLL_FAILURES_BEFORE_LOST:
                self._handle_connection_lost(str(exc))
            return
        self._poll_failures = 0
        faulted = []
        for row in self.rows:
            status = statuses.get(row.axis.index)
            if status is None:
                continue
            text = ""
            if status.motor_error:
                text = self.controller.error_text(f"?{status.motor_error}")
                faulted.append(f"axis {row.axis.index} ({row.axis.name}): {text}")
            row.update(status, text)

        if faulted:
            self.statusBar().showMessage(" | ".join(faulted))
        elif self.statusBar().currentMessage().startswith("axis "):
            self.statusBar().clearMessage()

        # If the watchdog tripped, say so plainly -- otherwise the operator
        # just sees axes that stopped for no visible reason.
        if self.watchdog.armed or self._watchdog_fired_seen:
            if self.watchdog.fired() and not self._watchdog_fired_seen:
                self._watchdog_fired_seen = True
                self.watchdog_label.setText(f"{_dot(DOT_BAD)} WATCHDOG FIRED")
                QMessageBox.critical(
                    self,
                    "Watchdog fired",
                    "The controller stopped all motion because the host "
                    "heartbeat went stale.\n\n"
                    "Motion was killed with reason code 9001 (visible in "
                    "MERR). Re-connect to re-arm.",
                )

    # -- actions -----------------------------------------------------------

    def _enable_all(self) -> None:
        self._for_each_axis(self.controller.enable, "enable")

    def _disable_all(self) -> None:
        self._for_each_axis(self.controller.disable, "disable")

    def _clear_faults(self) -> None:
        self._for_each_axis(self.controller.fault_clear, "clear faults on")
        self.statusBar().showMessage("faults cleared", 3000)

    def _save_to_flash(self) -> None:
        summary = "\n".join(
            f"    axis {a.index}  {a.name:<12} "
            f"#INVDOUT={1 if a.homing_direction is cfg.Direction.NEGATIVE else 0}"
            + (f"   limits {t.safe_limits()[0]:.0f}..{t.safe_limits()[1]:.0f}"
               if (t := self.travel.get(a.index)) else "   (not measured)")
            for a in cfg.AXES
        )
        if QMessageBox.question(
            self, "Save to flash",
            "Write the current axis parameters to controller flash so they "
            "survive a power cycle?\n\n"
            f"{summary}\n\n"
            "Flash is rated for about 100,000 writes — this is worth doing "
            "when a configuration is settled, not routinely.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            self.controller.save_to_flash()
        except Exception as exc:
            log.exception("save to flash failed")
            QMessageBox.critical(self, "Save to flash failed", str(exc))
            return
        self.statusBar().showMessage(
            "axis parameters saved to flash — these now survive a restart", 8000)

    def _for_each_axis(self, fn, label: str) -> None:
        errors = []
        for axis in cfg.AXES:
            try:
                fn(axis.index)
            except Exception as exc:
                errors.append(f"axis {axis.index}: {exc}")
        if errors:
            QMessageBox.warning(self, f"Could not {label} every axis", "\n".join(errors))

    def _commutate_axis(self, axis_index: int) -> None:
        axis = cfg.AXES_BY_INDEX[axis_index]
        confirm = QMessageBox.question(
            self,
            "Commutate axis",
            f"Run auto-commutation on axis {axis_index} ({axis.name})?\n\n"
            "The motor can jump up to one magnetic pitch in either "
            "direction as the current vector aligns.\n\n"
            "Make sure the stage is not parked against a hard stop "
            "or other obstacle.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.controller.commutate(axis_index)
        except Exception as exc:
            QMessageBox.critical(self, "Commutation failed", str(exc))
            return
        self.statusBar().showMessage(f"commutating axis {axis_index}", 4000)

    # -- travel calibration --------------------------------------------------

    def _on_calibration_progress(self, progress) -> None:
        self.statusBar().showMessage(
            f"calibrating axis {progress.axis}: {progress.message}")

    def _on_calibration_finished(self, axis_index: int, result, error: str) -> None:
        axis = cfg.AXES_BY_INDEX[axis_index]
        if result is None:
            if error == "cancelled":
                self.statusBar().showMessage("calibration aborted", 5000)
            else:
                QMessageBox.critical(self, "Calibration failed", error)
            return

        self.travel.set(result)
        for row in self.rows:
            if row.axis.index == axis_index:
                row.set_travel(result)

        # Soft limits were already written by the calibration, inside the
        # widened block, so the stale ones never reapply to the new frame.
        lo, hi = result.safe_limits()
        self.controller.set_travel_span(axis_index, result.span)
        self.statusBar().showMessage(
            f"axis {axis_index} ({axis.name}): travel {result.min_counts:.0f} "
            f"..{result.max_counts:.0f}, soft limits {lo:.0f}..{hi:.0f} "
            f"— save to flash in MMI to persist", 10000)

    def _home_axis(self, axis_index: int) -> None:
        """Home the axis and measure its travel — one operation.

        Homing fixes the coordinate frame; the travel ends are only meaningful
        inside it. Doing them separately allowed a stale frame to be measured
        against, so they are no longer separable.
        """
        if self._calibration_thread and self._calibration_thread.is_alive():
            QMessageBox.information(
                self, "Already running",
                "A homing sequence is already in progress. Press Esc to abort it.")
            return

        axis = cfg.AXES_BY_INDEX[axis_index]
        if not self._confirm_homing([axis]):
            return

        self._calibrator = TravelCalibrator(
            self.controller,
            on_progress=lambda p: self.calibration_progress.emit(p),
        )

        def worker():
            try:
                result = self._calibrator.home_and_measure(axis_index)
                self.calibration_finished.emit(axis_index, result, "")
            except CalibrationCancelled:
                log.info("axis %d homing cancelled", axis_index)
                self.calibration_finished.emit(axis_index, None, "cancelled")
            except Exception as exc:
                # Log as well as showing the dialog: a failure that only
                # appears in a message box leaves nothing to diagnose from.
                log.exception("axis %d home-and-measure failed", axis_index)
                self.calibration_finished.emit(axis_index, None, str(exc))

        self._calibration_thread = threading.Thread(
            target=worker, name=f"home-{axis_index}", daemon=True)
        self._calibration_thread.start()
        self.statusBar().showMessage(
            f"homing axis {axis_index} ({axis.name})...")

    def _home_all(self) -> None:
        """Home and measure every ready axis, one after another.

        Sequential rather than concurrent: each axis's measurement re-homes
        and temporarily widens that axis's soft limits, and running several at
        once would make an abort much harder to reason about.
        """
        if self._calibration_thread and self._calibration_thread.is_alive():
            QMessageBox.information(
                self, "Already running",
                "A homing sequence is already in progress. Press Esc to abort it.")
            return

        targets = self.controller.homeable_axes()
        if not targets:
            QMessageBox.warning(
                self, "Nothing to home",
                "No axis is currently marked as safe to home.",
            )
            return
        if not self._confirm_homing(targets):
            return

        self._calibrator = TravelCalibrator(
            self.controller,
            on_progress=lambda p: self.calibration_progress.emit(p),
        )

        def worker():
            for axis in targets:
                try:
                    result = self._calibrator.home_and_measure(axis.index)
                    self.calibration_finished.emit(axis.index, result, "")
                except CalibrationCancelled:
                    log.info("axis %d homing cancelled", axis.index)
                    self.calibration_finished.emit(axis.index, None, "cancelled")
                    return
                except Exception as exc:
                    log.exception("axis %d home-and-measure failed", axis.index)
                    self.calibration_finished.emit(axis.index, None, str(exc))
                    return

        self._calibration_thread = threading.Thread(
            target=worker, name="home-all", daemon=True)
        self._calibration_thread.start()
        self.statusBar().showMessage(f"homing {len(targets)} axes...")

    def _confirm_homing(self, axes: list[cfg.AxisConfig]) -> bool:
        listing = "\n".join(
            f"    axis {a.index}  {a.name:<12} method {a.homing_method} "
            f"({a.homing_direction.name.lower()})"
            for a in axes
        )
        confirm = QMessageBox.question(
            self,
            "Home and measure travel",
            f"This homes and measures:\n\n{listing}\n\n"
            "For each axis:\n"
            "    1. home against its reference end\n"
            "    2. find the opposite end of travel\n"
            "    3. re-home, then set soft limits from the measurement\n\n"
            "Both ends are found by the controller's own homing, which limits "
            "motor current and detects contact in firmware.\n\n"
            "Soft limits are widened during the measurement and restored "
            "afterwards, including if you abort.\n\n"
            "Press Esc at any time to abort.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return confirm == QMessageBox.Yes

    def _estop(self) -> None:
        # Abort any running calibration first: it has its own jog loop and
        # would otherwise re-issue motion straight after the kill.
        if self._calibrator is not None:
            self._calibrator.cancel()
        self.gamepad_jog.stop_all()
        try:
            self.controller.kill_all()
            self.statusBar().showMessage("MOTION KILLED", 5000)
        except Exception as exc:
            log.error("kill failed: %s", exc)

        # KILL stops the motion but does not clear AST.#INHOMING, and the axis
        # then rejects every later command with 3065. Only disabling cancels a
        # homing, so do that for any axis still in one.
        aborted = []
        for axis in cfg.AXES:
            try:
                if self.controller.abort_homing(axis.index):
                    aborted.append(str(axis.index))
            except Exception:
                log.warning("abort_homing failed on axis %d", axis.index,
                            exc_info=True)
        if aborted:
            self.statusBar().showMessage(
                f"MOTION KILLED — axes {', '.join(aborted)} disabled to "
                f"cancel homing", 8000)

        self._jogging.clear()

    # -- keyboard jog --------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        # Auto-repeat fires continuously while a key is held. Re-issuing the
        # jog on every repeat would flood the controller, so ignore repeats:
        # the first press starts the motion and it runs until release.
        if event.isAutoRepeat():
            return
        binding = self._binding_for(event)
        if binding is None:
            super().keyPressEvent(event)
            return
        axis_index, direction = binding
        if not self.controller.connected or axis_index in self._jogging:
            return
        fine = bool(event.modifiers() & Qt.ShiftModifier)
        try:
            self.controller.jog(axis_index, direction, fine=fine,
                                travel=self.travel.get(axis_index))
            self._jogging[axis_index] = direction
        except Exception as exc:
            self.statusBar().showMessage(f"jog axis {axis_index} failed: {exc}", 4000)

    def keyReleaseEvent(self, event) -> None:
        if event.isAutoRepeat():
            return
        binding = self._binding_for(event)
        if binding is None:
            super().keyReleaseEvent(event)
            return
        self._stop_axis(binding[0])

    def _binding_for(self, event) -> tuple[int, cfg.Direction] | None:
        name = QKeySequence(event.key()).toString().lower()
        return self._key_map.get(name)

    def _stop_axis(self, axis_index: int) -> None:
        if self._jogging.pop(axis_index, None) is None:
            return
        try:
            self.controller.halt(axis_index)
        except Exception as exc:
            log.error("halt axis %d failed: %s", axis_index, exc)

    def _stop_all_jogs(self) -> None:
        for axis_index in list(self._jogging):
            self._stop_axis(axis_index)

    # -- safety --------------------------------------------------------------

    def focusOutEvent(self, event) -> None:
        # If the window loses focus while a jog key is held down, the key-up
        # event is delivered to whatever took focus instead of to us -- so the
        # axis would keep moving with nothing left to stop it. Halt on the way
        # out. This is one of three layers; the controller-side watchdog is the
        # only one that survives this process being killed outright.
        self._stop_all_jogs()
        super().focusOutEvent(event)

    def changeEvent(self, event) -> None:
        # Minimising or being deactivated by another window is the same hazard.
        if event.type() in (event.Type.WindowStateChange, event.Type.ActivationChange):
            if not self.isActiveWindow():
                self._stop_all_jogs()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if self._calibrator is not None:
            self._calibrator.cancel()
        if self._calibration_thread and self._calibration_thread.is_alive():
            self._calibration_thread.join(timeout=2.0)
        self._stop_all_jogs()
        self.gamepad_jog.stop_all()
        self.poll_timer.stop()
        self.heartbeat_timer.stop()
        self.gamepad_timer.stop()
        if self.controller.connected:
            # Disarm before the heartbeat stops, or a deliberate exit would
            # look identical to a crash and trip the kill.
            self.watchdog.disarm()
            self.controller.disconnect()
        super().closeEvent(event)


def run() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    app = QApplication([])
    window = MainWindow(StageController())
    window.show()
    return app.exec()
