# 5-Axis Stage Control Panel — Context & Architecture Findings

Research pass over the ACS documentation + the installed ADK, before any code is written.

## Hardware / software inventory (verified on this machine)

| Item | Value | Source |
|---|---|---|
| Controller | SPiiPlus EC, P/N `SP+EC-16000032NNNDNDNN`, S/N `ECM09393A2` | `ControllerConnectionFavorites.xml` |
| Controller IP | `10.0.0.101` : `701` (TCP), reachable, 1 ms ping | ping + favorites |
| Controller firmware | **2.60** | favorites XML |
| Installed ADK Suite / MMI | **4.20.01** | `Program Files (x86)\ACS Motion Control\` |
| Drives | UDMnt, EtherCAT SubDevice, 2- or 3-phase BLDC / stepper / DC brush / voice coil | UDMnt datasheet |
| UDMnt I/O | GP I/O (4/2), **Limit Sensor Inputs (4)**, MARK (4), PEG (2), 2x analog in | UDMnt datasheet |
| UDMnt feedback | 2x AqB / 2x SinCos / 2x Absolute | UDMnt datasheet |
| Host libraries available | C/C++, C#/.NET, Python, MATLAB | ADK Suite |
| Python wheels shipped | `spiipluspython-4.20.1.0` for cp311/cp312/cp313/cp314, win_amd64 | ADK Suite |

### Firmware 2.60 capability probe — RESOLVED on hardware

Controller confirmed at FW **2.60** (`?VR`) against host tooling **4.20.01**. Probed the boundary:

| Probe | Result | Meaning |
|---|---|---|
| `?HOMEDEF(0)` | `?1064` — undefined global variable | **`HOMEDEF` does not exist in 2.60** |
| `HOME` (bare) | `?2033` — mandatory argument omitted | **`HOME` command DOES exist** — parser knows the keyword and wants an axis |
| `HOME 0,52` (motor disabled) | `?3254` — operation requires motor enabled | **Method 52 (negative hard stop) IS supported** — passed method validation, failed only the enable check. Not 3314. |
| `HOME 0,17` (motor disabled) | `?3254` | **Method 17 (negative limit switch) IS supported** |
| `HOME 0,53` | `?3314` | **Method 53 (positive hard stop) is NOT supported** |

### Homing method support on FW 2.60 — asymmetric

| method | meaning | status |
|---|---|---|
| 17 | negative limit switch | supported |
| 18 | positive limit switch | supported — axis 6 homes with it |
| 50 | negative hard stop + index | supported — axis 0 homes with it |
| 51 | positive hard stop + index | supported — axis 1 homed with it |
| 52 | negative hard stop, no index | supported (probe returned 3254) |
| **53** | **positive hard stop, no index** | **NOT supported — returns 3314** |

**52 exists but 53 does not.** And support is **axis-dependent, not just firmware-dependent**:
`HOME 0,52` is accepted on axis 0 (linear, has an index) but `HOME 6,52` returns 3314 on axis 6
(rotary, no index).

Consequence: **axis 6 has no firmware homing method for its negative end at all** — 52 is rejected,
50 needs an index it does not have, 17 needs a switch it does not have. It therefore uses the
host-side jog-and-watch probe as a fallback, which works there precisely because its `VEL` is low:
the detection-margin analysis gives it ~145 samples where the linear axes got 0.21.

`calibrate._probe_end()` tries firmware first and falls back automatically on `3314`. The fallback
leaves the axis at the stop rather than homed, so the caller must always re-home after one.

`tools/probe_methods.py` re-runs this survey; it requires the axis to be disabled so no motion is
possible.

Both homing strategies the machine needs are present in firmware. Positive-direction variants
(18, 53) are the paired implementations of the same methods and are very likely present too, but
have not been probed — the buffer program should still handle a `3314` return gracefully.

Probe technique worth reusing: with the motor **disabled**, issuing a `HOME` variant cannot produce
motion, and the returned error tells you how far through validation the firmware got. `3314` means
the method is rejected; `3254` means the method was accepted and only the enable precondition
failed.

**Consequences for the design:**

- Use the built-in `HOME` command. No hand-rolled search motion needed.
- `HOMEDEF` / `HOMEVEL` / `HOMEVELL` are **not available** — these shipped after 2.60. The homing
  method cannot be stored as a per-axis controller default.
- Therefore **pass the method explicitly on every call**: `HOME <axis>,<method>,<vel>,...`.
  The linear-vs-rotation distinction lives in the ACSPL+ buffer program as literals or user-declared
  globals, not in a firmware array.
- Still verify per-method support before relying on 52/53 — error **3314** = "Requested Homing
  Method is not supported".

Anything else the 4.20.01 docs describe should be treated as unverified until probed the same way;
the doc set is four minor versions ahead of the controller.

### EtherCAT topology (recovered from `SPiiPlus_MMI_Log_2026-08-26.log`)

Six 2-axis UDMnt units, DIP 0–5, network axes 0–11, all at 60 Vdc.
Regenerate with `tools/topology.py`.

| DIP | Axes | Part number | Serial | Cont./peak A | Stage assignment |
|---|---|---|---|---|---|
| 0 | 0, 1 | `UDMnt2B220N0R` | UDM11917 | **5 / 10** | **both linear stages** |
| 1 | 2, 3 | `UDMnt2B220N0R` | UDM11897 | 5 / 10 | unused |
| 2 | 4, 5 | `UDMnt2A220N0R` | UDM11935 | **2.5 / 5** | **rotation ×2** |
| 3 | 6, 7 | `UDMnt2A220N0R` | UDM11925 | **2.5 / 5** | **rotation ×1** (7 spare) |
| 4 | 8, 9 | `UDMnt2C220N0R` | UDM08509 | 10 / 20 | unused |
| 5 | 10, 11 | `UDMnt2A220N0R` | UDM11096 | 2.5 / 5 | unused |

Confirmed axis map: **linear = 0, 1** — **rotation = 4, 5, 6**. Seven drive axes spare.

Both linear stages sit on one 5/10 A unit; the three rotation stages span two 2.5/5 A units.
The 10/20 A unit is entirely unused.

The current rating matters directly for hard-stop homing: the default `HomingCurrLimit` is
`min(XCURV, 0.5*XRMSM, 0.5*XRMSD)`, and the current limit is what sets how hard the stage pushes
into the mechanical stop. The linear stages being on the higher-rated 5/10 A unit means more
available force if the limit is left at default — set it explicitly rather than relying on the
default.

**Homing can run on all five axes concurrently** (confirmed with user) — no collision sequencing
needed in the buffer program.

### Encoders are incremental — position is lost on power-down

Decoding the part numbers (`UDMnt` + fields 1-8):

```
UDMnt 2 A 2 2 0 N 0 R
      │ │ │ │ │ │ │ └ I/O configuration
      │ │ │ │ │ │ └── absolute encoder interfaces: 0
      │ │ │ │ │ └──── absolute encoder type: N = None
      │ │ │ │ └────── 100 MHz SinCos: 0
      │ │ │ └──────── 500 kHz SinCos: 2
      │ │ └────────── encoder channels: 2
      │ └──────────── current rating
      └────────────── drive axes: 2
```

Corroborated by `ABS_ENCODERS_TYPE=None` on every unit.

**Consequence: homing is required after every controller power cycle.** There is no absolute
feedback, no battery backup, and no position-save-to-flash that survives power-down. `MFLAGS.#HOME`
reads 0 on every boot.

This makes homing the most frequently executed operation on the machine, not a commissioning step.
Design implications:

- Homing must be **one click in the UI** — never a round trip through MMI.
- UI reads `MFLAGS.#HOME` per axis on connect and visibly flags un-homed axes.
- Keep soft limits disabled until home completes (position reference is meaningless before that).
- Block or warn on absolute moves against un-homed axes.
- Because it runs daily, it is the operation *least* tolerant of a mid-sequence host crash — which
  favours the sequence living in a controller buffer, triggered from the UI.

### Index pulses CONFIRMED on both linear stages

Tested on hardware by arming the latch (`IST(n).#IND=1` then `=0`), moving the axis through a
revolution, and reading back:

| Axis | `?IST(n).#IND` | Result |
|---|---|---|
| 0 | `1` | index present and latching |
| 1 | `1` | index present and latching |

**So the linear stages use homing methods 50/51 (hard stop *then* index), not 52/53.** The hard stop
gets close, the index pulse supplies a precise repeatable zero. Materially better repeatability for
the same one-click operation.

Useful technique: the drive reads the encoder continuously **whether or not the motor is enabled**,
so on a back-driveable stage the index can be tested by hand with the motor disabled — no powered
motion, no risk of running into a hard stop. Preferred over jogging when the axis has no limit
switches.

### The two linear stages have OPPOSITE direction conventions

Found by debugging a repeated homing failure on axis 1. Every tuning parameter was identical
between axes 0 and 1 — `CERRV`, `ERRV`, currents, `VEL`, `ACC`, `JERK`, `EFAC`, soft limits. The
only difference was in `MFLAGS`:

```
axis 0  MFLAGS = 0x2a3308      #INVDOUT (bit 13) = 1
axis 1  MFLAGS = 0x2a1300      #INVDOUT (bit 13) = 0
                    ^^^^ XOR = 0x2008 -> bits 3 (#HOME) and 13 (#INVDOUT)
```

`#INVDOUT` "inverts the drive output command/s ... effectively inverts the direction of the motion
and the sign of the feedback." So a *negative* homing command drives axis 1 the opposite physical
way from axis 0.

**Symptom:** `HOME 1,50` drove the stage the wrong way and faulted — twice, at `FPOS` -436,500
then -804,603.

**Root cause:** axis 1's `SLLIMIT` is **0**, so driving negative immediately put `RPOS` below the
software left limit. `FAULT` bit 6 (`#SLL`) tripped, the controller killed the axis, and the hard
deceleration then produced `MERR 5023` (Critical Position Error). The 5023 was a *consequence*, not
the trigger — an earlier reading of this as a position-error/momentum problem was wrong.

**Fix:** axis 1 homes **positive** (method 51), axis 0 homes **negative** (method 50).

Homing was also slowed to **1% of the axis's tuned `VEL`** (243,200 counts/s), expressed as a
fraction so it scales with the setup. This was applied at the same time as the direction fix, so it
is not independently validated — the direction change is the one supported by evidence. Slow homing
is still worth keeping: the margin between hard-stop detection (`0.75*CERRV`) and the `#CPE` fault
(`CERRV`) is only 25%.

**Verified working:** `HOME 1,51,243200` and `HOME 0,50,243200` both completed with `MFLAGS.#HOME`
set and no faults.

### The `#INVDOUT` rule — predicts homing direction

Confirmed on every axis homed so far:

| axis | `#INVDOUT` | direction that worked | method |
|---|---|---|---|
| 0 Linear X | 1 | negative | 50 |
| 1 Linear Y | 0 | positive | 51 |
| 6 Rotation C | 0 | positive | 18 |
| 4 Rotation A | 0 | *predicted positive* | 18 |
| 5 Rotation B | 0 | *predicted positive* | 18 |

**`#INVDOUT = 0` → home POSITIVE. `#INVDOUT = 1` → home NEGATIVE.** Check this bit before the first
homing attempt on any new axis; getting it wrong drives the stage into the opposite mechanical stop
until something faults.

Axis 6 cost four failed attempts before this was applied — each one ran ~8,490 counts into a
mechanical stop and died on `#CPE`, with `#LL` never going active. The switch was wired correctly
the whole time; it is simply at the *positive* end. Method 18 homed it on the first try.

### Why soft limits "come back" — controller restarts, NOT `HOME`

**Runtime writes to `SLLIMIT`/`SRLIMIT` (and `FMASK`) live in RAM. A controller restart reloads
them from flash.** Axis 6's flash values are 200/200, so every restart restores that impossible
window and any manual change is lost.

Diagnosed with `tools/uptime.py`:

```
TIME = 419,480 ms          uptime 0:06:59  -> restarted
#HOME    = 0 on all axes   (were 1 on axes 0, 1, 6)
#BRUSHOK = 0 on all axes   commutation lost
HOSTWDEN -> "Undefined global variable"   watchdog program gone
buffer 9 -> empty
```

It looks correlated with homing only because homing is what happens next, and homing zeroes the
axis to ~0 — outside the 200/200 window — which makes the pre-existing violation visible.

**Anything set at runtime must be saved to flash to survive a restart.** That includes soft limits
set by the calibration routine.

Two earlier hypotheses recorded here were wrong and are retracted:

- *"`HOME` restores the fault masks"* — rested on two snapshots taken while masks were being
  toggled manually in MMI. `tools/dump_buffers.py` confirms no controller program writes
  `SLLIMIT`/`SRLIMIT`/`FMASK`/`SAFETYCONF`/`FDEF`.
- *"the 200/200 window was only ever violated on different sides"* — true of the fault bit, but it
  did not explain the values themselves reverting, which is the restart.

Related detail that still holds: the soft-limit *condition* bit in `FAULT` is only evaluated while
the axis is enabled — a disabled axis reads `FAULT = 0` even when sitting far outside its limits.

### What a controller restart costs

| lost | restore by |
|---|---|
| commutation (`#BRUSHOK`) | `COMMUT` per brushless axis, or a commutation startup program |
| homing (`#HOME`) | re-home each axis |
| `SLLIMIT`/`SRLIMIT` set at runtime | reverts to flash values — save to flash to persist |
| ACSPL+ buffers loaded at runtime | the panel reinstalls the watchdog automatically on connect |

### Axis 6 has a reference at ONE end only

Measured: homed at the **positive limit switch** (~0), and the negative end is a **bare mechanical
stop** at about **-8,000 counts**. There is no negative limit switch, despite both `#RL` and `#LL`
faults being enabled in `FMASK`.

Corroborating evidence: the four failed method-17 attempts all stopped ~8,490 counts negative on
`#CPE` with `#LL` never going active — that was the mechanical stop, not a missing signal.

**Do not assume an axis has a reference at both ends.** The travel search must watch for a hard
stop as well as a limit switch. An earlier version of `calibrate.py` watched only the switch on
rotation axes; when axis 6 met its bare stop the search did not notice and left the motor pushing
against it for ~100 s until the timeout.

### Travel calibration — three things that had to be right

`acs_stage/calibrate.py` measures both ends of an axis after homing. Three separate defects had to
be fixed before it worked on the linear axes, all of them silent failures:

**1. The search must watch for a hard stop, not just a limit switch.** Axis 6 has a switch at one
end and a bare mechanical stop at the other; watching only the switch left the motor pushing
against that stop for ~100 s until the timeout.

**2. The search speed must be derived from the detection margin, not chosen.** Once a stage is
against its stop, position error grows at roughly the commanded velocity. To catch PE between the
threshold (`0.5*CERRV`) and the `#CPE` fault (`CERRV`), the axis must cross that margin in no fewer
than a few polls:

```
velocity <= CERRV * (1 - PE_STOP_FRACTION) / (DETECTION_SAMPLES * POLL_INTERVAL_S)
```

At the original 5% of `VEL`, axis 0 searched at 1,216,000 counts/s and crossed a 1250-count margin
in ~1 ms against a 20 ms poll — **0.21 samples**. Detection was arithmetically impossible; the axis
simply faulted on `#CPE`. Axis 6 worked only because its `VEL` is 10,314, giving 145 samples by
accident. Now capped at 50,000 counts/s for the linear axes, exactly 5 samples.

**3. The existing soft limits must be widened during the measurement.** They bound the very range
being measured:

- axis 0 searching positive stopped dead on `SRLIMIT` at 2,304,000 (`MERR 5015`)
- axis 1 searching negative tripped `SLLIMIT = 0` on its first step (`MERR 5016`)

Neither revealed anything about the mechanical stops. Calibration now widens `SLLIMIT`/`SRLIMIT` to
±2e14 for the duration and restores the originals in a `finally`, so a cancelled or failed run
leaves the axis exactly as found.

### `HomingCurrLimit` — why the rotation stages could not home

`HOME` limits motor current during its search to
`min(XCURV, 0.5*XRMSM, 0.5*XRMSD)` — **half the motor's rated continuous
current**. A jog uses full current; `HOME` does not.

| axis | XCURV | XRMSM | default `HomingCurrLimit` |
|---|---|---|---|
| 0, 1 linear | 64 | 16 | 8 — ample |
| 4, 5, 6 rotation | 3.8 | 2.02 | **1.01 — too little to turn the stage** |

With 1.01 the rotation motor cannot move at all. Position error then grows at
the commanded velocity and `#CPE` fires after `CERRV / velocity` seconds:
**0.73 s predicted, 0.79–0.84 s observed across three attempts.** The symptom
looks exactly like "the limit switch was never found", which sent this
investigation down several wrong paths — unwired switches, parked past the
switch, stale `MERR`.

The decisive test was jogging the same axis at the same velocity: it moved
+3,348 counts with `PE` ≈ 0 and no faults. Healthy at speed, stalled under
`HOME` — isolating current as the only difference.

**Fix:** pass an explicit `homing_current_limit` of 2.0 on the rotation axes —
`XRMSM` is 2.02, so this is the rated continuous current, not an overload.
These axes home on a limit switch, so there is no hard-stop contact for a
lower current to protect against.

`HOME` takes its optional arguments positionally
(`Axis, Method, HomingVel, MaxDistance, HomingOffset, HomingCurrLimit`), so
supplying a current limit also means supplying `MaxDistance` — which usefully
bounds the search at the same time.

**General lesson:** when a firmware homing search behaves as though it never
sees its reference, check whether the axis is moving at all before concluding
anything about switches or wiring. Time-to-fault ≈ `CERRV / homing velocity`
is the signature of a stalled motor.

### Let the firmware find the stops — do not do it from the host

The single most important lesson from calibrating this machine. `HOME` found hard stops reliably
every time; a host-side jog-and-watch loop faulted on `#CPE` every time. Two reasons:

1. **`HOME` checks every controller cycle, inside the servo loop.** A host loop polls over TCP with
   several reads per iteration — 10–20 ms of latency. Position error crosses from any sensible
   detection threshold to `CERRV` faster than the host can react, so the fault always wins.
2. **`HOME` limits motor current during the search** (`HomingCurrLimit`, default
   `min(XCURV, 0.5*XRMSM, 0.5*XRMSD)`), so the stage eases into the stop. A jog uses full current
   and slams into it.

**Travel calibration therefore probes the far end with `HOME` methods 52/53** (hard stop, no index
or switch required), tracking the extreme `FPOS` reached before the homing re-zeroes the axis, then
re-homes to restore the intended frame.

The host-side `_find_far_end` is retained but unused by `calibrate()`. Everything that was built to
make it work — raising `CERRV`, capping search velocity, backing off the stop — was compensating
for being in the wrong place to begin with.

### Why hard-stop searches kept faulting on `#CPE`

Finding a hard stop **requires** position error to build — it is the only signal that the stage has
stopped. The search detects at `PE_STOP_FRACTION * CERRV` and halts, but `#CPE` fires at `CERRV`,
and that margin is crossed in tens of milliseconds.

Host-side detection cannot win that race. Every poll is a TCP round trip and the loop performs
three reads per iteration, so the real loop period is 10–20 ms, not the 5 ms nominal. The fault
fires before the halt lands.

**Fix: raise `CERRV` by 10x for the duration of the search and restore it afterwards.** Detection
still uses the *original* `CERRV`, so how hard the stage pushes into the stop is unchanged — only
the fault that was firing first is moved out of the way. The search stays bounded by its slow
speed, PE monitoring, a timeout, and Esc cancellation.

Also required: **back off the stop between search legs.** Leaving the stage pressed against a stop
holds a standing position error, which faults the axis, so the second leg aborted immediately on a
stale `MERR` from the first.

### Jog speed must come from travel, not from `VEL`

`VEL` is an axis's maximum *programmed* velocity and has no relation to how far its stage can move.
Sizing jog as a fraction of it produced 6,080,000 counts/s on axis 0 — a full traverse of its ~3e6
counts in half a second, which drove the stage through its soft limit on the first keypress. It
only looked reasonable on axis 6, whose `VEL` happens to be 10,314.

Jog speed is now sized so a **full traverse takes 15 s** (60 s with Shift), derived from the axis's
soft-limit span, capped at `MAX_JOG_FRACTION` of `VEL` for capability. Axes with placeholder limits
(±2e14) fall back to a conservative fraction of `VEL`.

| axis | old coarse | new coarse |
|---|---|---|
| 0 Linear X | 6,080,000 | 203,093 |
| 1 Linear Y | 6,080,000 | 153,600 |
| 6 Rotation C | 2,578 | 1,572 |
| 4, 5 | 2,578 | 516 (no known span) |

### Arrow keys cannot be used for jogging

Qt consumes them for focus navigation between widgets, so they never reach the key handler. Jog
keys are letters (`J`/`L`, `K`/`I`, and `A`/`D`, `S`/`W`, `Q`/`E`), and all buttons are set to
`Qt.NoFocus` — which also stops a focused button being fired by Space or Enter.

### The `#INVDOUT` guard

`StageController.home()` reads `MFLAGS.#INVDOUT` and refuses to home if it disagrees with the
configured direction (`config.expected_direction`). A controller restart can silently revert
`#INVDOUT` to a stale flash value — which is what broke axis 0 mid-session — and homing against it
drives the stage into the opposite stop until it faults. This class of failure cost several runs
across axes 1, 6 and 0 before it was made impossible.

### The homing index sits mid-travel — it is NOT the end of travel

Homing methods 50/51 find the hard stop and then back off to *the first index pulse past it*. On
both linear stages that index is near the **middle** of the range, not near the stop.

Measured on hardware:

| axis | reference | negative stop | positive stop | span |
|---|---|---|---|---|
| 0 Linear X | index at 0 | -2,565,354 | +2,559,027 | 5,124,381 |
| 1 Linear Y | index at ~1 | -1,671,212 | +1,274,933 | 2,946,145 |

Axis 0's index is almost exactly centred — symmetric to 0.25%.

**Consequence: treating the homed position as an end of travel discards roughly half the stage.**
An early version set `SLLIMIT` to the index (0) and the axis could not move below mid-travel.
Travel calibration therefore probes *both* hard stops explicitly, and the homing reference is
recorded only as a coordinate origin.

### The stored soft limits never matched the real travel

| axis | stored before | actually measured |
|---|---|---|
| 0 | -742,400 .. 2,304,000 | -2,392,446 .. 2,558,444 |
| 1 | 0 .. 2,304,000 | -1,671,212 .. 1,274,933 |
| 6 | 200 .. 200 | -24,065 .. -3 |

Axis 1's stored `SRLIMIT` was **over a million counts beyond its physical stop** — no protection at
all on that side — while its `SLLIMIT` of 0 forbade 1.67M counts of usable travel.

Corollary: **stored soft limits are not evidence for where an axis homes.** An earlier inference
that axis 1 "must" home negative because its limits read `0 .. 2,304,000` was wrong for exactly
this reason.

### The two linear stages have opposite sign conventions

| axis | `#INVDOUT` | homes | method |
|---|---|---|---|
| 0 Linear X | 1 | negative | 50 |
| 1 Linear Y | 0 | positive | 51 |
| 4, 5, 6 rotation | 0 | positive | 18 |

Both linear conventions work. They are mirror images, not a fault. Reconciling them would mean
flipping `#INVDOUT` on one stage and re-verifying its direction from scratch.

### Cancelling a homing requires DISABLING the axis

`HALT` and `KILL` stop the motion but leave `AST.#INHOMING` set. The axis then
rejects every subsequent motion command with **3065** ("command cannot be
executed while the current motion is in progress") until it is disabled.

The ACSPL+ reference gives the only remedy: *"Disable axis during homing
process will cancel the homing process."*

Axis 6 was wedged this way for ~20 minutes after an Esc during homing, and
several apparently unrelated failures afterwards ("axis is moving", "motion in
progress") were all downstream of it.

Both `TravelCalibrator._check_cancel` and the UI E-stop now call
`StageController.abort_homing()`, which disables the axis only if a homing is
genuinely still active — it does drop holding torque, so it is not done
speculatively.

### Interrupted calibration leaves `CERRV` raised

The calibration raises `CERRV` 10x during a hard-stop search and restores it in
a `finally`. Killing the process mid-search — closing the window during a
sequence — skips that, leaving the axis with a fault threshold 10x too
lenient. Axis 6 was found at 7500 instead of 750.

`tools/restore_cerrv.py` reports all axes and restores a value.

### Gamepad control

Xbox pad via XInput (`ctypes`, no dependency). Hold a button to select an axis
and arm motion; push the left stick to move it.

| hold | stick | axis | |
|---|---|---|---|
| X | ← → | 1 Linear X | inverted |
| Y | ↑ ↓ | 0 Linear Y | |
| A | ← → | 4 Rotation A | inverted |
| B | ← → | 5 Rotation B | inverted |
| RT | ↑ ↓ | 6 Rotation C | held past 50% pull |

All three rotation stages are inverted, consistent with their sharing
`#INVDOUT = 0`.

**The linear names come from the pad buttons.** Axis 1 is displayed as
"Linear X" because button **X** drives it, and axis 0 as "Linear Y" because
button **Y** drives it — earlier tables in this document use the reverse
labels, so read by **axis number**, which never changed. The keyboard cluster
is laid out by direction rather than by name (I/K vertical drives axis 1,
J/L horizontal drives axis 0), so the letters on the keyboard do not line up
with the letters in the axis names.

Speed is `deflection ** GAMEPAD_RESPONSE_EXPONENT` (2.0) times
`GAMEPAD_MAX_SPEED_FACTOR` (3.0) times the keyboard jog speed, capped at
`MAX_JOG_FRACTION` of the axis's `VEL`. Squaring matters: linear response
crams all the fine control into the first few degrees of stick travel. At 2.0
a quarter deflection is 6% of full speed, so most of the stick is devoted to
slow motion while full deflection still crosses the travel in ~5 s.

Safety rules, all verified in `tools/test_gamepad_jog.py`:

- the held button is a **dead-man switch** — release halts the axis
- **one axis at a time**; two bound buttons held is ambiguous and stops
- **losing the pad halts** — a wireless dropout cannot leave an axis running
- **measured travel limits apply**, same as keyboard jog
- jogs are only re-sent when velocity changes >4% or direction flips, so a
  20 Hz poll does not flood the controller

**Xbox pads connected over Bluetooth do not work.** Windows enumerates them as
HID without creating the `IG_00` XInput interface, so `XInputGetState` returns
1167 on every slot. The pad must go through the Xbox Wireless Adapter or a USB
cable — and the Bluetooth pairing must be *removed* first, or the controller
reconnects to it in preference every time. `tools/probe_xinput.py` diagnoses
this.

### Verified cold-start sequence

Confirmed end to end after a full controller power cycle (2026-08-31 16:21):

```
connect            -> #INVDOUT auto-corrected on axis 0, watchdog reinstalled
Enable All
Comm on axes 0, 1  -> rotation axes are not controller-commutated (#BRUSHL=0)
Home each axis     -> homes, measures both ends, writes soft limits
Save to Flash      -> everything above persists
```

Total ~6 minutes for all five axes. Only commutation and homing are
unavoidable per power cycle; they are inherent to incremental encoders.

Repeatability across the power cycle:

| axis | before | after | difference |
|---|---|---|---|
| 0 | 5,123,200 | 5,122,387 | 813 (0.016%) |
| 1 | 2,947,091 | 2,944,154 | 2,937 (0.1%) |
| 4 | 27,372 | 27,377 | **5 counts** |
| 5 | 27,822 | 27,822 | **0 counts** |

Axes 4 and 5 are switch-referenced at *both* ends, which is why they reproduce
essentially exactly. The linear axes use hard stops, which are mechanically
softer references.

### Final calibration results — all five axes

| axis | measured travel | span | references | soft limits |
|---|---|---|---|---|
| 0 Linear X | -2,565,037 .. 2,558,163 | 5,123,200 | hard stop both ends | -2,513,800 .. 2,506,930 |
| 1 Linear Y | -1,671,569 .. 1,275,522 | 2,947,091 | hard stop both ends | -1,642,100 .. 1,246,050 |
| 4 Rotation A | -27,376 .. -4 | 27,372 | **switch both ends** | -27,102 .. -4 |
| 5 Rotation B | -27,825 .. -3 | 27,822 | **switch both ends** | -27,547 .. -3 |
| 6 Rotation C | -24,931 .. -3 | 24,928 | switch (+) / hard stop (−) | -24,682 .. -3 |

Axes 4 and 5 measure 27,372 and 27,822 — a 1.6% spread on nominally identical
stages, which is a useful cross-check that the measurement is sound.

Repeatability across a full power cycle on axis 0: **317 counts out of 5.1
million**, from index-based homing.

### Switch wiring history

Axes 4 and 5 initially read `#RL` **and** `#LL` active simultaneously — the
floating-input signature. Two separate hardware fixes were needed:

1. **Detector polarity inverted** on both axes. Axis 4 then worked fully.
2. **Axis 5 rewired.** After the inversion its `#RL` still never fired and its
   `#LL` was stuck on — asserted at positions 6,900 counts apart, which is not
   point-like behaviour. Swapping the switches fixed it.

Both now have working switches at both ends. Axis 6 still has only one switch
(positive); its negative end is a bare mechanical stop.

### Switch-homing methods abort SILENTLY when the switch is already active

Documented behaviour for methods 17/18: *"If the limit switch is ON when the
function is called, the homing is aborted (no homing attained)."*

What the documentation does not say is how that abort presents. It is not an
error. The command is accepted, nothing moves, no fault is raised — and
`AST.#INHOMING` stays set, so the axis then rejects every later command with
3065 until it is disabled.

Axes 4 and 5 sat in this state for 29 s and 9 s without moving a single count,
looking exactly like a homing search in progress.

`StageController.home()` now checks the target switch before issuing a
switch-homing method, and refuses with an explanation instead.

### Reaching a limit switch sets MERR — that is success, not failure

When a hardware limit trips, the controller disables the axis and records the
reason in `MERR`:

| code | meaning |
|---|---|
| 5010 | Hardware Right Limit |
| 5011 | Hardware Left Limit |

During a far-end search that is precisely the result being looked for, but a
blanket "any non-zero `MERR` aborts" check discarded it — axis 4 found its
negative switch after 27 s and the search reported failure.

`_find_far_end` now treats a limit `MERR` matching the search direction as a
successful detection, and a limit from the *opposite* direction as a real
error (wrong end — wiring or direction convention).

### Switch polarity had to be inverted on axes 4 and 5

Both read `#RL` and `#LL` active simultaneously, which is impossible as a
position and was the floating-input signature. After inverting the detector
polarity both read cleanly inactive and **both axes home**. Axis 4 has working
switches at *both* ends, unlike axis 6 which has one.

### Limit switch status

| axis | `#RL` | `#LL` | reading |
|---|---|---|---|
| 6 Rotation C | triggers reliably at the positive end | never seen across 4+ full traverses | one switch working; negative input connected but never closing |
| 4, 5 | both `#RL` and `#LL` read active simultaneously | | floating inputs — not wired |

A stage cannot be at both ends at once, so simultaneous `#RL`+`#LL` is the
signature of unwired inputs. Axis 6 differs: its `#LL` reads cleanly inactive,
which is an input that is connected but never closed.

### Over-travel protection — homing does NOT establish it

Homing finds **one** reference point. It does not map the travel range, and having homed does not
prevent driving off the other end. Over-travel protection is separate:

- **Soft limits**: `SLLIMIT` / `SRLIMIT` per axis, enforced against `RPOS`, enabled via the `#SLL`
  (bit 6) and `#SRL` (bit 5) bits of `FMASK`.
- **Hardware limit switches**: `#LL` (bit 1) and `#RL` (bit 0).

`SLPMIN`/`SLPMAX` are **not** travel limits — they are modulo-axis bounds tied to
`MFLAGS.#MODULO` (bit 29). Do not read them as travel range.

Measured state (`tools/check_limits.py`):

| axis | `SLLIMIT` | `SRLIMIT` | soft limits enabled | notes |
|---|---|---|---|---|
| 0 Linear X | -742,400 | 2,304,000 | **yes** | protected |
| 1 Linear Y | 0 | 2,304,000 | **yes** | protected; `SLLIMIT=0` is why negative homing failed |
| 4 Rotation A | -2e14 | 2e14 | **no** | effectively unlimited |
| 5 Rotation B | -2e14 | 2e14 | **no** | effectively unlimited |
| 6 Rotation C | 200 | 200 | no | zero-width window — misconfigured |

⚠️ **The linear stages are protected by soft limits** — necessary, since they have no limit
switches at all.

⚠️ **The rotation stages have no soft limits.** They rely entirely on hardware limit switches, and
not all of those are wired. A rotation axis with neither a wired switch nor soft limits has **zero**
over-travel protection. This is the strongest reason to keep homing blocked on them until their
switches are confirmed, and a good reason to set real `SLLIMIT`/`SRLIMIT` values on them.

⚠️ **Axis 6's soft limits are nonsense** (`SLLIMIT = SRLIMIT = 200`, axis at 14,252). Harmless only
because `#SLL`/`#SRL` are masked off; it would fault immediately if they were enabled.

### Final homing plan

| Axis | Stage | Method | Detection | Status |
|---|---|---|---|---|
| 0 Linear X | linear | **50** negative | hard stop + index | **homed, verified** |
| 1 Linear Y | linear | **51** positive | hard stop + index | **homed, verified** |
| 6 Rotation C | rotation | **18** positive | limit switch | **homed, verified** |
| 4 Rotation A | rotation | 18 positive (predicted) | limit switch | blocked — switch not wired |
| 5 Rotation B | rotation | 18 positive (predicted) | limit switch | blocked — switch not wired |

All three homing strategies the machine needs are validated on hardware. All five can home
concurrently.

---

## 1. The parameters you configured do not need to be re-entered anywhere

This is the most important finding, and it changes the premise of the question.

The motor parameters, encoder parameters, soft/hard limits, EtherCAT network map and servo tuning
you defined **live in the SPiiPlus EC's own flash memory**, not in the MMI application on the PC.
MMI is just an editor pointed at the controller.

Consequence: any host program that opens a socket to `10.0.0.101:701` is talking to an
already-fully-configured controller. A custom UI inherits your entire setup for free. There is no
"import the config into the UI" step required to make motion work correctly.

What the MMI workspace file (`%APPDATA%\ACS Motion Control\SPiiPlus MMI Application Studio\
4.20.01.00\Data\default.acsw`) contains is only window layout — it is a ZIP of deflate-compressed
`.itm` panel descriptors. No motor parameters. Confirmed by inspection.

## 2. Export / import does exist, and is worth using — but as backup, not as UI input

**Format:** `.spi` application file. Contains axis parameters, system parameters, ACSPL+ program
buffers, user variables, adjuster data, and user files. Sections are individually selectable, and
the loader supports **axis remapping** (source axis N → destination axis M).

Three ways to drive it:

| Route | How | Use case |
|---|---|---|
| GUI | MMI → Toolbox → Application Development → Application Wizard → *Save Application to PC* / *Load Application to Controller* | Manual backup before you change something |
| API | `AnalyzeApplication()` → `SaveApplication()` / `LoadApplication()` → `FreeApplication()`, then `ControllerReboot()` | **UI gets its own Backup/Restore buttons** |
| CLI | `SPiiPlus Upgrader` with `/APPLFILE`, `/APPLPAR`, `/APPLBUF`, `/APPLADJ`, `/APPLSP`, `/APPLUSERFILE` | Scripted / version-controlled config snapshots |

Python signature sketch:

```python
info = sp.AnalyzeApplication(hc, None, sp.SYNCHRONOUS, True)      # None = current controller app
sp.SaveApplication(hc, r"config\stage-2026-08-28.spi", info, sp.SYNCHRONOUS, True)
sp.FreeApplication(info, True)
```

Note the save path writes to controller flash first, then flash → file. Flash is rated ~100k write
cycles; don't put this on a timer.

## 2a. Units: the controller works in encoder counts

Measured on hardware: `?EFAC(0)` → **1**. Since `FPOS = FP * EFAC + EOFFS`, an `EFAC` of 1 means
**user units are raw encoder counts** on axis 0. Confirm the other four axes before relying on this
globally.

`?CERRV(0)` → **2500** counts, so hard-stop detection trips at `0.75 * 2500 = 1875` counts of
following error unless `HardStopThreshold` overrides it.

**Do not "fix" this by changing `EFAC`.** Rescaling it also rescales `VEL`, `ACC`, `DEC`, `CERRV`,
soft limits and every other position-derived variable already tuned in the working setup. The cost
of a cosmetic units change is re-validating the whole configuration.

**Instead the UI carries a display-units layer**: a per-axis counts-per-mm / counts-per-degree
factor used for readout and for entering moves, converting at the boundary. Controller config stays
untouched.

Physical meaning of 1875 counts is unknown until encoder resolution × screw pitch is established —
that comes from the mechanics, not the controller. Settle `HardStopThreshold` empirically during
commissioning: jog slowly into the stop with current limited, capture `PE` on the Scope, pick a
threshold below the runaway.

## 3. For values the UI itself needs, read them live from the controller

The UI will want: which axes exist, counts→mm/deg scaling, soft limit travel, default velocities,
axis names. **Do not parse the `.spi` for these.** Read them from the controller at connect time via
`ReadReal` / `ReadInteger` / `GetConf` on the standard ACSPL+ variables.

This dissolves the export/import round trip entirely: change something in MMI → save to flash → the
UI picks it up on its next connect. Nothing to keep in sync, nothing to forget to re-export.

The `.spi` file then serves its proper purpose — disaster recovery and machine duplication.

### What `tools/backup_controller.py` captures

Run it to put the controller's flash contents into the repo. Read-only unless `--spi` is passed.

| output | what it is | restores? |
|---|---|---|
| `config/backup/parameters.txt` | 39 parameters per axis as a table, plus decoded `MFLAGS` bits | no — for diffing |
| `config/backup/parameters.json` | the same, machine-readable | no |
| `config/backup/buffers/*.prg` | every non-empty ACSPL+ buffer, as text | yes, per buffer |
| `config/backup/application-*.spi` | the full application image (`--spi`) | **yes, everything** |

Only the `.spi` is a true restore image. The text outputs exist because a binary blob cannot answer
"what changed since last week, and did anyone touch `#INVDOUT` again?" — the readable table diffs in
git and the `.spi` sitting beside it does the actual recovery.

`--spi` is opt-in because `SaveApplication` writes controller flash before copying flash → file.
Flash is rated ~100k cycles; run it deliberately, never on a timer.

### The machine builder's axis names, recovered from buffer 1

Buffer 1 holds an integrator's startup program that sets `KDEC`/`JERK` per axis, each line commented
with the axis's original name:

| axis | OEM name | this project |
|---|---|---|
| 0 | `Y1` | Linear Y |
| 1 | `X1` | Linear X |
| 2 | `Z1` | — no stage |
| 3 | `Z_BCT1` | — no stage |
| 4 | `Rz_PUT1` | Rotation A |
| 5 | `Ry_PUT1` | Rotation B |
| 6 | `Rx_PUT1` | Rotation C |
| 8 | `X2` | — no stage |
| 10, 11 | `X_BCT1`, `Y_BCT1` | — no stage |

**Axis 0 is Y and axis 1 is X**, independently of anything in this project — which is what the panel
now displays, having arrived there from the gamepad buttons. The rotation stages are `Rz`, `Ry`,
`Rx` in axis order, so A/B/C map to Rz/Ry/Rx.

Buffer 1 also issues `SETCONF(270, 4|5|6, 5)` for the three rotation axes.

Note that buffer 1 is **not in effect**: it sets `KDEC(0) = 3E7`, while the live value reads
`2.56E8`. So it is either not run at startup or has been superseded — do not assume the values in
it describe the machine. The live readout does.

## 4. Homing is already implemented in controller firmware

The `HOME` ACSPL+ command covers **both** of your homing situations natively. No hand-rolled
sequence needed.

```
HOME    Axis, [HomingMethod, HomingVel, MaxDistance, HomingOffset,
               HomingCurrLimit, HardStopThreshold, ...]
HOME/e  Axis, [... , Timeout]        ! blocking variant
```

Relevant methods:

| Method | Behaviour | Maps to |
|---|---|---|
| 17 | Home on **negative limit switch** | rotation stages |
| 18 | Home on **positive limit switch** | rotation stages |
| 52 | Home on **negative hard stop** (ACS-specific) | linear stages |
| 53 | Home on **positive hard stop** (ACS-specific) | linear stages |
| 50 / 51 | Hard stop **+ index pulse** refinement | linear stages, *if* they have an index |
| 37 | Home on current position | manual/recovery |
| 33 / 34 | Index pulse only | — |

### How "stall detection" actually works here

It is **position-error based, not current-threshold based**:

```
hard stop detected when   abs(PE) > min(HardStopThreshold, CERRV * 0.75)
```

`HomingCurrLimit` caps drive current during the search (default
`min(XCURV, 0.5*XRMSM, 0.5*XRMSD)`). With current limited, the motor cannot follow the commanded
profile once it reaches the stop, so position error grows and trips the threshold. Same physical
outcome as stall-current detection, but easier to tune and it degrades safely.

Method 52/53 behaviour after detection: backs off to the detection point, advances by `threshold`
(that becomes home), then a further `2 × threshold`, then halts.

### Status flags for the UI

- `MFLAGS.#HOME` → 1 once homing completed
- `AST.#INHOMING` → 1 while homing in progress
- `E_TYPE` and encoder re-init **clear** `#HOME` — the UI must re-check, not cache
- Error `3314` = requested homing method not supported

### Per-axis persistent defaults

`HOMEDEF[]` (method), `HOMEVEL[]` (homing velocity), `HOMEVELL[]` (limit-search velocity) are
standard arrays. Set them once, save to flash, and `HOME <axis>` with no arguments does the right
thing per axis. **This is where the "linear vs rotation" distinction should live** — in the
controller, not in UI code.

### Preconditions

Axis must be enabled, commutated, and not moving. Disabling mid-home cancels. No other motion may
be queued on that axis during homing.

## 5. Can this be done inside MMI instead of building a UI?

Partly. Split it.

**What MMI already gives you:**
- *Motion Manager → Jog Motion*: direction, velocity, acc, dec, kill-dec, jerk, per axis.
- *Program Manager*: full ACSPL+ editor, 64 buffers, breakpoints, save-to-flash.
- *Safety and Faults Monitor*, *Scope*, *Inputs/Outputs Manager*.

**What MMI cannot do — the actual gap:**
Keyboard jog. I inspected the shortcut profiles
(`Settings\Shortcuts\Default.json`, `KeyboardProfile1-3.json`) — every binding is a **code-editor**
action (`FormatSelection`, `CommentLinesEdit`, `MoveToNextWord`, …). There are no motion actions in
the shortcut system and no way to add them. Jog is form-and-button driven only.

That gap is real and is what justifies the custom UI. But it's a *small* gap.

### Recommended split

```
  ┌─────────────────────────────────────────┐
  │ Controller (SPiiPlus EC, flash)         │
  │  • all motor/encoder/limit parameters   │  ← already done, stays put
  │  • HOMEDEF/HOMEVEL/HOMEVELL per axis    │  ← set once in MMI
  │  • ACSPL+ homing sequence buffer        │  ← authored in MMI Program Manager
  │  • safety/fault responses               │
  └────────────────▲────────────────────────┘
                   │  Ethernet TCP :701
  ┌────────────────┴────────────────────────┐    ┌──────────────────┐
  │ Custom UI (thin)                        │    │ MMI (kept open)  │
  │  • connect / status / E-stop            │    │  Scope, Faults,  │
  │  • keyboard jog                         │    │  Program Manager │
  │  • "Home All" → triggers buffer         │    └──────────────────┘
  │  • Backup/Restore .spi                  │
  └─────────────────────────────────────────┘
```

Put the homing sequence in an ACSPL+ buffer, not in host code. It then runs deterministically on
the controller at servo rate, survives a PC crash / cable pull / UI restart mid-home, and stays
editable in the tool where you already know the parameters. The UI just calls `RunBuffer()` or
sends `HOME 0,52,...` and polls `AST.#INHOMING` / `MFLAGS.#HOME`.

## 6. MMI and the custom UI can run at the same time

The **SPiiPlus User Mode Driver** arbitrates multiple connected applications — it lists them by
name, comm channel and PID, and can disconnect them individually. So you can keep MMI's Scope and
Safety & Faults Monitor open while driving the stage from the custom UI. Significant for
development and for tuning the hard-stop thresholds.

The UMD also hosts a global **Emergency Stop** button that host apps opt into via
`RegisterEmergencyStop()`. The UI should register.

---

## Host API surface (Python binding, mirrors the C library)

| Need | Call |
|---|---|
| Connect | `OpenCommEthernetTCP(ip, 701)` → handle, `-1` on failure |
| Disconnect | `CloseComm(hc, True)` |
| Enable / disable | `Enable(hc, axis, ...)` / `Disable(...)` |
| **Jog** | `Jog(hc, flags, axis, velocity, wait)`; `JogM(...)` for multi-axis |
| Stop | `Halt(...)` (profiled, uses `DEC`) / `Kill(...)` (fast, uses `KDEC`) / `KillAll(...)` |
| Point to point | `ToPoint(hc, flags, axis, target, ...)` |
| Position | `GetFPosition(hc, axis, ...)` |
| Status | `GetMotorState()` (`#ENABLED` bit 0, `#MOVE` bit 5), `GetAxisState()` |
| Faults | `GetFault()`, `FaultClear()`, `EnableFault()`, `SetFaultMask()` |
| **Any ACSPL+ command** | `Transaction(hc, cmd + '\r', len(cmd)+1, 1024, wait, True)` → reply string |
| Buffers | `LoadBuffer()`, `CompileBuffer()`, `RunBuffer()`, `StopBuffer()` |
| Variables | `ReadReal()`, `WriteReal()`, `ReadInteger()`, `WriteInteger()` |
| Backup / restore | `AnalyzeApplication()`, `SaveApplication()`, `LoadApplication()` |
| E-stop | `RegisterEmergencyStop()` / `UnregisterEmergencyStop()` |
| Waits | `WaitMotionEnd()`, `WaitMotorEnabled()` |

`HOME` has **no dedicated wrapper** in the host libraries — issue it via `Transaction()`, or wrap it
in an ACSPL+ buffer and `RunBuffer()`. The buffer route is preferred (see §5).

---

## Commutation: also required after every power-up

Hit on hardware — `HOME 0,50` was rejected because axis 0 was not commutated.

The motors are DC brushless on **incremental** SinCos encoders, and the MMI guide is explicit:

> "the commutation process must be done only once for an absolute encoder but **after every
> power-up for incremental quadrature and SinCos encoders** — since the motor position is not known."

So the per-power-cycle sequence is **commutate → home**, in that order. `HOME` requires the axis
"enabled, commutated, and not in motion" (error 3257 = "operation requires the motor to be enabled",
3257's sibling 3257/3257 family covers commutation).

State is `MFLAGS.#BRUSHOK`, **bit 9**. Cleared at power-up, same as `#HOME` (bit 3).

### Commutation Startup Program (MMI → Adjuster → Commutation)

`Generate Startup Program` writes an ACSPL+ program into a chosen buffer that re-establishes
commutation at power-up. This is ACS's intended solution and should replace doing it by hand.

Two schemes:

- **Detent-point based** — energises the motor and lets it pull into alignment. Involves movement.
- **Auto-commutation (`COMMUT`)** — closed-loop, "involving almost no motor movement". The docs
  recommend this: detent-based setup is required **once** during Adjuster commutation, after which
  "automatic commutation is recommended at every controller power up".

Index-based schemes include **"First Index Next to Left Hard Stop"**, which matches axes 0 and 1
exactly (both have a confirmed index and a hard stop). It stores the commutation phase at the index
and reuses it at later power-ups — more repeatable than re-deriving each time.

⚠️ **The motor can jump up to one magnetic pitch (180 electrical degrees) in either direction**
during commutation. Do not run it with a stage parked hard against a stop.

⚠️ **Do not put the startup program in buffer 9** — that is the watchdog. The controller runs up to
9 buffers simultaneously and both of these run persistently.

## Watchdog design (implemented)

`acspl/watchdog.prg` runs on the controller; `acs_stage/watchdog.py` is the host half.

The UI increments a global `HOSTWDOG` every 200 ms. The controller program tracks the last change
and kills axes 0, 1, 4, 5, 6 if the counter goes stale for `HOSTWDTMO` (default 1000 ms).

Why a **polling loop** rather than an `ON` autoroutine: autoroutine conditions are edge-triggered.
`TIME > deadline` stays true once tripped, so it would fire once and never re-arm cleanly across a
reconnect. A `WHILE 1` loop with `WAIT 20` has no such subtlety.

Protocol globals:

| Variable | Written by | Meaning |
|---|---|---|
| `HOSTWDOG` | host | heartbeat counter |
| `HOSTWDEN` | host | 1 = armed; set 0 for a clean disconnect |
| `HOSTWDTMO` | host | timeout in ms |
| `HOSTWDFIRED` | controller | 1 = it killed motion |

Design points that matter:

- **Starts disarmed.** Loading the buffer cannot itself kill motion; the host arms only once its
  heartbeat is already running.
- **Disarms on clean exit.** Otherwise quitting the UI is indistinguishable from a crash.
- **Kills a named axis list, not `KILLALL`** — the other seven drive axes are not ours to stop.
- **Reason code 9001** lands in `MERR`, so the cause is visible after the fact.
- **Separate Qt timer from the status poll**, so a slow poll cannot delay the heartbeat and trip
  the watchdog on a healthy system.
- **Buffer 9 by default, and the installer refuses to overwrite an occupied buffer.** `LoadBuffer`
  clears the target first; silently destroying a program written in MMI is not an acceptable
  failure mode. The check passes only if the buffer is empty or already contains our marker.

## Design concerns to settle before building

1. **Key-release-to-stop has a failure mode.** If the window loses focus while a jog key is held
   down, the OS never delivers the key-up event and the axis keeps moving. Mitigations, all needed:
   focus-out → `Halt`; filter auto-repeat so held keys don't re-issue motion; and a controller-side
   watchdog — an ACSPL+ autoroutine that kills motion if a host heartbeat variable goes stale.
   The watchdog is the only one of the three that survives the UI process being killed outright.

2. **Hard-stop homing drives the stage into a physical stop.** Every run puts load on the mechanics.
   Worth setting `HomingCurrLimit` conservatively and validating `HardStopThreshold` on a scope
   trace before running it unattended.

3. **`#HOME` is cleared by encoder re-init.** The UI must treat "homed" as live controller state and
   read it every poll, never cache it across a reconnect.

4. **Soft limits should be enabled only after homing.** Before a successful home the position
   reference is meaningless, so soft limits protect nothing.

## Open questions

Resolved on hardware: axis mapping (0,1 linear / 4,5,6 rotation), concurrent homing (yes),
`HOME` support (yes), methods 52 and 17 (both supported), index pulses (present on 0 and 1),
language (Python + PySide6).

Still open:

- **Homing direction per axis** — 50 vs 51 for the linear stages, 17 vs 18 for the rotation stages.
  Mechanical question: which end of travel is the safe/natural home for each.
- **`HardStopThreshold` value** — tune empirically on a Scope trace during commissioning.
  `CERRV(0)` = 2500 counts gives a default trip at 1875 counts of following error.
- **`HomingCurrLimit` value** — set explicitly rather than inheriting
  `min(XCURV, 0.5*XRMSM, 0.5*XRMSD)`; the linear stages sit on the higher-rated 5/10 A unit.
- **Counts per mm / per degree** for the UI display-units layer.
- **`EFAC` on axes 1, 4, 5, 6** — confirmed 1 on axis 0, assume-and-verify for the rest.
