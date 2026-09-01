# PyInstaller spec for the stage control panel.
#
# Build with:  .venv\Scripts\pyinstaller packaging\acs-stage.spec --noconfirm
# or via:      .venv\Scripts\python tools\build_exe.py
#
# One file, no console. The console is worth a word: the panel logs jog
# commands and controller errors to stdout, and hiding that means a failure to
# connect looks like nothing happening. The log is therefore also written to a
# file next to the exe -- see acs_stage/ui/__init__.py.
#
# travel.json is deliberately NOT bundled. It is written at runtime by the
# calibration, so it lives next to the exe (acs_stage/paths.py: data()), not
# inside the archive, which is unpacked to a temp directory and deleted on
# exit.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# Qt modules this app never touches. PySide6 ships a very large set and the
# default hook takes most of it; the panel imports only QtCore, QtGui and
# QtWidgets.
QT_UNUSED = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
]

EXCLUDES = QT_UNUSED + [
    # numpy is NOT excluded: SPiiPlusPython.SPiiPlusDefs.SPiiPlusEnums
    # imports it at top level, so dropping it breaks the binding at import.
    "matplotlib", "scipy", "pandas", "PIL", "tkinter",
    "pytest", "setuptools", "pip",
]

a = Analysis(
    ["../run.py"],
    pathex=[".."],
    binaries=[],
    # The watchdog program is read at connect time and must ship with the exe.
    datas=[("../acspl/watchdog.prg", "acspl")],
    hiddenimports=collect_submodules("SPiiPlusPython"),
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ACS Stage Control",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
