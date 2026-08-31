"""Measured travel ranges per axis, persisted between sessions.

Populated by the calibration routine (see calibrate.py) rather than typed in
by hand. Positions are raw controller units (encoder counts) in the coordinate
frame established by that axis's homing -- so a stored range is only valid
while the axis stays homed against the same reference.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parent.parent / "config" / "travel.json"

# Fraction of the measured span held back from each end when deriving usable
# limits. The ends were found by touching a hard stop or a limit switch; you
# do not want normal operation reaching them.
DEFAULT_MARGIN_FRACTION = 0.01


@dataclass
class TravelRange:
    axis: int
    min_counts: float
    max_counts: float
    min_reference: str          # how the low end was found
    max_reference: str          # how the high end was found
    measured_at: str
    homed_zero: float           # FPOS of the homing reference when measured

    @property
    def span(self) -> float:
        return self.max_counts - self.min_counts

    @property
    def homed_at_min(self) -> bool:
        """True if the homing reference is the low end of travel."""
        return abs(self.homed_zero - self.min_counts) <= abs(
            self.homed_zero - self.max_counts)

    def safe_limits(self, margin_fraction: float = DEFAULT_MARGIN_FRACTION
                    ) -> tuple[float, float]:
        """Usable limits, held back from the measured extremes.

        The margin depends on what each end actually is:

        - **hard stop** -> apply the margin. It is a mechanical collision and
          nothing should reach it in normal operation.
        - **limit switch** -> no margin. The switch is the intended end of
          travel *and* the position the axis homes to, so holding the limit
          back from it would put the home position outside its own soft limit
          and trip #SRL/#SLL the instant homing completes.

        On an index-homed axis both ends are hard stops and both get margins;
        the homing index sits comfortably inside, which is the point -- the
        index is not the end of travel, and treating it as one discards the
        travel between it and the stop.

        The controller's comparison is inclusive (#SLL fires on
        RPOS < SLLIMIT), so sitting exactly on a limit is legal.
        """
        margin = abs(self.span) * margin_fraction
        lo = self.min_counts + (margin if "switch" not in self.min_reference else 0.0)
        hi = self.max_counts - (margin if "switch" not in self.max_reference else 0.0)
        return lo, hi

    def contains(self, position: float, margin_fraction: float = DEFAULT_MARGIN_FRACTION) -> bool:
        lo, hi = self.safe_limits(margin_fraction)
        return lo <= position <= hi

    def headroom(self, position: float, direction: int,
                 margin_fraction: float = DEFAULT_MARGIN_FRACTION) -> float:
        """Distance remaining before the limit in `direction` (+1 / -1).

        Negative means already past it.
        """
        lo, hi = self.safe_limits(margin_fraction)
        return (hi - position) if direction > 0 else (position - lo)


class TravelStore:
    def __init__(self, path: Path = STORE_PATH):
        self.path = path
        self._ranges: dict[int, TravelRange] = {}
        self.load()

    def load(self) -> None:
        self._ranges.clear()
        if not self.path.exists():
            log.info("no travel calibration at %s", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("could not read travel calibration")
            return
        for entry in raw.get("axes", []):
            try:
                r = TravelRange(**entry)
            except TypeError:
                log.warning("skipping malformed travel entry: %r", entry)
                continue
            self._ranges[r.axis] = r
        log.info("loaded travel calibration for axes %s", sorted(self._ranges))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "written_at": datetime.now().isoformat(timespec="seconds"),
            "note": "Measured by the calibration routine. Counts are in the "
                    "homed coordinate frame; invalid if the axis is re-homed "
                    "against a different reference.",
            "axes": [asdict(r) for r in sorted(self._ranges.values(),
                                               key=lambda r: r.axis)],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("saved travel calibration to %s", self.path)

    def get(self, axis: int) -> TravelRange | None:
        return self._ranges.get(axis)

    def set(self, travel_range: TravelRange) -> None:
        self._ranges[travel_range.axis] = travel_range
        self.save()

    def clear(self, axis: int) -> None:
        if self._ranges.pop(axis, None) is not None:
            self.save()

    def __contains__(self, axis: int) -> bool:
        return axis in self._ranges

    def __iter__(self):
        return iter(sorted(self._ranges.values(), key=lambda r: r.axis))
