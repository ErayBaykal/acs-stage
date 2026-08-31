"""Verify the home-and-measure sequence has the right structure."""
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from acs_stage import calibrate as c  # noqa: E402

seq = inspect.getsource(c.TravelCalibrator.home_and_measure)
rec = inspect.getsource(c.TravelCalibrator._home_with_recovery)
prep = inspect.getsource(c.TravelCalibrator._prepare_for_motion)
probe = inspect.getsource(c.TravelCalibrator._probe_end)

homes = len(re.findall(r"controller\.home\(axis\)", seq))
preps = len(re.findall(r"_prepare_for_motion\(axis\)", seq))

checks = [
    ("widening opens before the first home",
     seq.index("_limits_widened") < seq.index("_home_with_recovery")),
    ("every direct home is preceded by preparation", preps >= homes),
    ("recovery prepares before its homes", rec.count("_prepare_for_motion") >= 2),
    ("preparation halts first", "controller.halt" in prep),
    ("preparation waits on FPOS, not the MST move bit",
     "FPOS" in prep and "MST_MOVE" not in prep),
    ("preparation clears faults and enables",
     "fault_clear" in prep and "controller.enable" in prep),
    ("probe falls back when firmware method unsupported",
     "MethodUnsupported" in probe and "_find_far_end" in probe),
    ("measured limits written before widening unwinds",
     seq.index('limits_state["replaced"]') < seq.index("homed_end =")),
    ("sanity check precedes the write",
     seq.index("refusing to record") < seq.index('limits_state["replaced"]')),
]

failed = 0
for name, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    failed += not ok

print(f"\n  direct home() calls: {homes}, prepare_for_motion(): {preps}")
print("\nFAILURES:", failed)
sys.exit(1 if failed else 0)
