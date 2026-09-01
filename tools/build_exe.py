"""Build the standalone executable, and stage it for deployment.

    .venv\\Scripts\\python tools\\build_exe.py
    .venv\\Scripts\\python tools\\build_exe.py --deploy "C:\\path\\to\\folder"

The exe is self-contained apart from two things it expects to find beside it:

    config/travel.json    the measured travel calibration
    acs-stage.log         written at runtime

travel.json is deliberately not bundled inside the archive. It is written by
the calibration, and a onefile build unpacks to a temporary directory that is
deleted on exit -- a calibration saved there would vanish, and the next run
would come up with no travel limits and refuse to bound any jog. --deploy
copies the current calibration alongside the exe so a fresh install starts
with the machine's real limits.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "acs-stage.spec"
DIST = ROOT / "build" / "dist"
WORK = ROOT / "build" / "work"
EXE_NAME = "ACS Stage Control.exe"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deploy", metavar="DIR",
                    help="copy the built exe and the calibration here")
    ap.add_argument("--skip-build", action="store_true",
                    help="deploy the existing build without rebuilding")
    args = ap.parse_args()

    if not args.skip_build:
        pyinstaller = Path(sys.executable).parent / "pyinstaller.exe"
        if not pyinstaller.exists():
            return ("pyinstaller not found in this environment -- "
                    f"{sys.executable} -m pip install pyinstaller")
        print(f"building {EXE_NAME} ...")
        result = subprocess.run(
            [str(pyinstaller), str(SPEC), "--noconfirm",
             "--distpath", str(DIST), "--workpath", str(WORK)],
            cwd=ROOT)
        if result.returncode:
            return f"pyinstaller failed ({result.returncode})"

    exe = DIST / EXE_NAME
    if not exe.exists():
        return f"no build at {exe}"
    print(f"\n{exe}  {exe.stat().st_size / 1e6:.0f} MB")

    if args.deploy:
        target = Path(args.deploy).resolve()
        (target / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(exe, target / EXE_NAME)
        shutil.copy2(ROOT / "config" / "travel.json",
                     target / "config" / "travel.json")
        print(f"deployed to {target}")
        print("  " + EXE_NAME)
        print("  config/travel.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
