# acs-stage

Control panel for a 5-axis stage (2 linear + 3 rotation) driven by UDMnt
drives over EtherCAT from an ACS SPiiPlus EC master.

Homing, jogging from the keyboard or an Xbox pad, travel calibration, and a
controller-side watchdog — in one window, so the machine can be brought up
from cold without the ACS MMI.

## The machine

| axis | stage | homes to | keyboard | gamepad |
|---|---|---|---|---|
| 0 | Linear Y | hard stop + index, negative | J / L | **Y** + stick ↑↓ |
| 1 | Linear X | hard stop + index, positive | K / I | **X** + stick ←→ |
| 4 | Rotation A | limit switch, positive | A / D | **A** + stick ←→ |
| 5 | Rotation B | limit switch, positive | S / W | **B** + stick ←→ |
| 6 | Rotation C | limit switch, positive | Q / E | **RT** + stick ↑↓ |

Controller at `10.0.0.101:701`, firmware 2.60. Axes 2, 3 and 7–11 exist on the
drives but carry no stage.

The linear axes are named after the **pad buttons** that drive them, not after
the keyboard cluster — the keyboard is laid out by direction instead, so its
letters do not line up with the axis names.

## Why the panel exists

The encoders are incremental. Every power cycle loses commutation and the
homing reference on all five axes, so the machine has to be re-referenced
before it can be trusted to move — and the stored soft limits turned out never
to have matched the real travel. The panel homes each axis, measures its
travel to both ends, and writes the result to `config/travel.json`, which then
bounds every jog it will issue.

Positions are displayed in raw encoder counts: `EFAC` is 1 on this controller,
counts-per-mm has not been measured, and inventing a number would put a wrong
figure in front of the operator.

## Running it

```
python run.py
```

Needs Python 3.11 (the SPiiPlus wheel is `cp311`), PySide6, and
`spiipluspython` from the ACS ADK Suite install:

```
py -3.11 -m venv .venv
.venv\Scripts\pip install PySide6
.venv\Scripts\pip install "C:\Program Files (x86)\ACS Motion Control\SPiiPlus ADK Suite v4.20.01\SPiiPlus Python Library\spiipluspython-4.20.1.0-cp311-cp311-win_amd64.whl"
```

Gamepad support is XInput through `ctypes` — no dependency, but the pad must
present an XInput interface. A pad paired over **Bluetooth LE does not**;
use the Xbox Wireless Adapter or a cable.

### Cold start

1. **Connect**
2. **Commutate** each axis (lost on every power cycle)
3. **Home** each axis — finds both ends of travel and writes the limits

About six minutes for all five. Repeatability measured across a power cycle:
axis 5 exact, axis 4 within 5 counts, axis 0 within 813 counts of 5.1 M.

## Backing up the controller

The axis tuning, fault configuration and ACSPL+ buffers exist **only** in the
controller's flash. A failed firmware update or a board swap takes all of it.

```
python tools\backup_controller.py          # read-only: parameters + buffers
python tools\backup_controller.py --spi    # also the .spi restore image
```

Output lands in `config/backup/` so it is version-controlled:

| file | restores? |
|---|---|
| `parameters.txt` / `.json` | no — a readable snapshot, so git can diff it |
| `buffers/*.prg` | yes, per buffer |
| `application-*.spi` | **yes — everything** |

`--spi` is opt-in: saving the image writes controller flash before copying
flash to the file, and flash is rated around 100k cycles. Run it deliberately
after you change something, not on a schedule.

The same image can be produced from the MMI — Toolbox → Application
Development → Application Wizard → *Save Application to PC* — and loaded back
with *Load Application to Controller*, which supports remapping source axis N
onto destination axis M.

### Restoring

```
python tools\restore_controller.py config\backup\application-2026-08-31.spi
python tools\restore_controller.py <file> --apply
```

No MMI needed — the SPiiPlus Python binding exposes the whole path
(`AnalyzeApplication` → `LoadApplication` → `ControllerReboot`).

**It does nothing without `--apply`.** The default is a dry run that connects,
validates the image against the controller in front of it, and prints what
would be written. Restoring overwrites parameters, buffers and commutation
data, so the guards are deliberate: it refuses on a serial-number mismatch
unless you pass `--any-serial`, warns on a firmware mismatch, and refuses
while any axis is moving. `--only parameters` (or `buffers`, `commutation`,
`variables`) restores one group.

What a `.spi` cannot give you, so a bare-metal rebuild still needs:

1. **Firmware** — install it first; the image records `2.60` but does not
   contain it.
2. **Network reachability** — a factory-reset controller is not at
   `10.0.0.101`, and nothing here can reach it until it is.
3. **Commutation and homing** — the image carries the adjuster calibration,
   but the encoders are incremental, so every power-up still needs both.

## Safety

- **Watchdog.** The panel writes a heartbeat the controller watches from its
  own program buffer; if the panel dies or the link drops, the controller
  kills motion by itself. This has fired for real on a dropped connection.
- **Measured travel limits.** Jogs are refused past the calibrated ends
  rather than relying on the stored soft limits.
- **Dead-man switch on the pad.** Releasing the button halts the axis; one
  axis at a time; losing the pad halts.
- **Esc** aborts. Note that `HALT`/`KILL` leave `AST.#INHOMING` set — only
  disabling the axis cancels a homing.

## Layout

```
acs_stage/       config.py is the machine definition; everything reads from it
  ui/            the panel
acspl/           watchdog.prg -- ACSPL+ that runs on the controller
config/          measured travel calibration
docs/FINDINGS.md every hardware quirk found, with the evidence for it
tools/           standalone probes and offline tests
```

`docs/FINDINGS.md` is worth reading before changing anything — homing method
support is axis-dependent on this firmware, `#INVDOUT` decides the homing
direction and has silently reverted in flash, and the rotation stages cannot
turn at the firmware's default homing current.

Offline tests need no controller:

```
.venv\Scripts\python tools\test_gamepad_jog.py
.venv\Scripts\python tools\smoketest.py
```
