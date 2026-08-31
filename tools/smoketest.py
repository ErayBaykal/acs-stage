"""Build the window offscreen and exercise the non-motion paths.

Catches layout/attribute errors without a display and without a controller.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from acs_stage import config as cfg  # noqa: E402
from acs_stage.controller import StageController  # noqa: E402
from acs_stage.ui.main_window import MainWindow  # noqa: E402

app = QApplication([])
win = MainWindow(StageController())
win.show()
print("window built OK")

# Poll while disconnected must be a no-op, not a crash.
win._poll()
print("poll while disconnected OK")

# Key bindings should resolve to the axes we configured.
resolved = {}
for axis in cfg.AXES:
    for key_name, direction in ((axis.key_negative, "neg"), (axis.key_positive, "pos")):
        seq = getattr(Qt, f"Key_{key_name}", None)
        if seq is None:
            print(f"  !! no Qt key constant for {key_name!r}")
            continue
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, seq, Qt.NoModifier)
        binding = win._binding_for(ev)
        resolved[f"{key_name}"] = binding
        status = "OK " if binding else "!! UNRESOLVED"
        print(f"  {status} {key_name:<6} -> {binding}")

unresolved = [k for k, v in resolved.items() if v is None]
print("unresolved keys:", unresolved or "none")

# Jog key while disconnected must not raise.
ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_Left, Qt.NoModifier)
win.keyPressEvent(ev)
print("jog while disconnected OK")

# Focus-out safety path with nothing jogging.
win._stop_all_jogs()
print("stop_all_jogs OK")

sys.exit(0 if not unresolved else 1)
