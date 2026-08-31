"""Sweep an axis end to end and record whether each limit switch ever fires.

Answers "do the limit switches actually trigger, and where?" with data rather
than inference. Sweeps one way until a switch or a hard stop, then back the
other way, logging every transition of #RL and #LL against position.

Safety: CERRV is raised for the sweep and restored afterwards, hard stops are
detected by position error and halted on, each leg is time-bounded, and
Ctrl-C halts. Motion is at the axis's homing velocity.

Usage:  python tools/sweep_switches.py <axis> [seconds_per_leg]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import SPiiPlusPython as sp  # noqa: E402

from acs_stage import config as cfg  # noqa: E402

NONE = -1
RL_BIT, LL_BIT = 0, 1
PE_STOP_FRACTION = 0.5
CERRV_MULTIPLIER = 10.0
POLL = 0.02

axis = int(sys.argv[1]) if len(sys.argv) > 1 else 6
leg_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 90.0

hc = sp.OpenCommEthernetTCP(cfg.CONTROLLER_HOST, cfg.CONTROLLER_PORT)
if hc == -1:
    sys.exit("could not connect")


def rr(n):
    return float(sp.ReadReal(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


def wr(n, v):
    sp.WriteReal(hc, NONE, n, axis, axis, NONE, NONE, v, sp.SYNCHRONOUS, True)


def ri(n):
    return int(sp.ReadInteger(hc, NONE, n, axis, axis, NONE, NONE, sp.SYNCHRONOUS, True))


axis_cfg = cfg.AXES_BY_INDEX[axis]
nominal_cerrv = rr("CERRV")
pe_stop = abs(nominal_cerrv) * PE_STOP_FRACTION
velocity = abs(rr("VEL")) * (axis_cfg.homing_velocity_fraction or 0.10)

events = []          # (position, switch, new_state)
seen = {"#RL": False, "#LL": False}


def settle():
    """Wait until the axis is genuinely stationary and PE has closed.

    The MST move bit sticks on this controller, so watch FPOS. And a halt
    leaves RPOS parked beyond FPOS, so PE must be given time to close or the
    next command starts with its error budget already spent.
    """
    sp.Halt(hc, axis, sp.SYNCHRONOUS, True)
    deadline = time.monotonic() + 15.0
    previous, still = rr("FPOS"), 0
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = rr("FPOS")
        still = still + 1 if abs(current - previous) < 1.0 else 0
        previous = current
        if still >= 3 and abs(rr("PE")) <= abs(nominal_cerrv) * 0.1:
            return
    print("   (warning: axis did not fully settle)")


def sweep(direction, label):
    sign = 1 if direction > 0 else -1
    prev = {"#RL": ri("FAULT") >> RL_BIT & 1, "#LL": ri("FAULT") >> LL_BIT & 1}
    print(f"\n--- sweeping {label} at {velocity:.0f} cts/s "
          f"(stop if PE > {pe_stop:.0f}) ---")
    settle()
    sp.FaultClear(hc, axis, sp.SYNCHRONOUS, True)
    time.sleep(0.3)
    if not (ri("MST") & 1):
        sp.Enable(hc, axis, sp.SYNCHRONOUS, True)
        time.sleep(0.5)
    prev_start = dict(prev)
    active_now = [n for n, v in prev.items() if v]
    print(f"   starting from FPOS {rr('FPOS'):.0f}, "
          + (f"already on {', '.join(active_now)}" if active_now
             else "no switch active"))
    sp.Jog(hc, sp.MotionFlags.ACSC_AMF_VELOCITY, axis,
           sign * velocity, sp.SYNCHRONOUS, True)
    t0 = time.monotonic()
    reason = "time limit"
    try:
        while time.monotonic() - t0 < leg_seconds:
            fault = ri("FAULT")
            pos = rr("FPOS")
            for name, bit in (("#RL", RL_BIT), ("#LL", LL_BIT)):
                state = fault >> bit & 1
                if state != prev[name]:
                    events.append((pos, name, state))
                    seen[name] = seen[name] or bool(state)
                    print(f"   {name} -> {'ACTIVE' if state else 'clear'} "
                          f"at FPOS {pos:.0f}")
                    prev[name] = state
            # Only stop on a switch we were not already sitting on when the
            # leg started, or a sweep that begins on a switch ends instantly.
            newly = any(fault >> b & 1 and not prev_start[n]
                        for n, b in (("#RL", RL_BIT), ("#LL", LL_BIT)))
            if newly:
                reason = "limit switch"
                break
            if abs(rr("PE")) > pe_stop:
                reason = "hard stop"
                break
            time.sleep(POLL)
    finally:
        sp.Halt(hc, axis, sp.SYNCHRONOUS, True)
        time.sleep(1.0)
    print(f"   stopped on {reason} at FPOS {rr('FPOS'):.0f}")
    return reason


print(f"axis {axis} ({axis_cfg.name}) switch sweep")
print(f"  CERRV {nominal_cerrv:.0f} -> {nominal_cerrv * CERRV_MULTIPLIER:.0f} for the sweep")
wr("CERRV", nominal_cerrv * CERRV_MULTIPLIER)
try:
    sweep(-1, "negative")
    sweep(+1, "positive")
finally:
    sp.Halt(hc, axis, sp.SYNCHRONOUS, True)
    time.sleep(0.5)
    wr("CERRV", nominal_cerrv)
    sp.FaultClear(hc, axis, sp.SYNCHRONOUS, True)
    print(f"\nCERRV restored to {rr('CERRV'):.0f}")

print("\n=== RESULT ===")
for name in ("#LL", "#RL"):
    where = [f"{p:.0f}" for p, n, s in events if n == name and s]
    print(f"  {name} ({'negative' if name == '#LL' else 'positive'} end): "
          + (f"TRIGGERED at {', '.join(where)}" if seen[name] else "NEVER TRIGGERED"))
if not (seen["#LL"] and seen["#RL"]):
    missing = [n for n in ("#LL", "#RL") if not seen[n]]
    print(f"\n  {' and '.join(missing)} did not respond anywhere across the "
          f"sweep.\n  If a switch is installed at that end, it is not reaching "
          f"the controller.")

sp.CloseComm(hc, True)
