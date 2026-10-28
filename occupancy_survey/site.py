"""Site metadata, with location deliberately coarse and opt-in.

A survey dataset is only useful to somebody else if it says where and with
what it was taken. It stops being safe to ask a stranger to run if "where"
is precise enough to find their house.

So location here is a Maidenhead grid square and nothing else, it is
optional, and anything finer than six characters is REFUSED rather than
truncated. Six characters is about 5 x 8 km, which is coarse enough that a
rural operator is not identified by it and fine enough for propagation
sanity-checking. Eight characters is about 0.5 x 0.5 km, which in open
country is one property, so the tool will not store it even if asked.

The refusal is deliberate: truncating silently would mean a volunteer who
typed their full grid square would believe they had shared it, and a
volunteer who reads the error learns the rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GRID_RE = re.compile(r"^[A-R]{2}[0-9]{2}(?:[A-X]{2})?$", re.IGNORECASE)

MAX_GRID_CHARS = 6


class LocationTooPrecise(ValueError):
    """Raised for a grid square finer than MAX_GRID_CHARS."""


@dataclass(frozen=True)
class SiteMetadata:
    """Who and what took the data. Written into every file.

    `grid_square` is opt-in: None means the operator chose not to share a
    location, and the dataset is still valid and still mergeable -- it just
    cannot be used for anything geographic.

    The receive-path fields are not decoration. Occupancy is
    threshold-relative so it does not need absolute calibration, but a
    channel that reads 0% because the antenna is a 2 m whip in a basement is
    indistinguishable from a channel that is genuinely quiet unless the
    antenna is recorded.
    """
    site_id: str
    receiver_model: str
    antenna: str
    grid_square: str | None = None
    antenna_height_m: float | None = None
    preamp: str = "none"
    feedline: str = "unspecified"
    # What is physically in the coax ahead of the receiver. Recorded, never
    # actuated -- the tool cannot see it. It matters more than almost
    # anything else here: an unfiltered wideband front end near broadcast
    # transmitters generates its own signals, and that error correlates
    # with real traffic rather than averaging out. See docs/front-end.md.
    front_end_filters: str = "none"
    operator_note: str = ""

    def __post_init__(self):
        if not self.site_id or not self.site_id.strip():
            raise ValueError("site_id is required: it is how files from "
                             "different receivers are kept apart when merged")
        if not self.receiver_model.strip():
            raise ValueError("receiver_model is required")
        if not self.antenna.strip():
            raise ValueError(
                "antenna is required. A quiet channel and a deaf receive "
                "path produce the same number, and only this field tells "
                "them apart.")
        if self.grid_square is not None:
            self._validate_grid(self.grid_square)

    @staticmethod
    def _validate_grid(grid: str) -> None:
        g = grid.strip()
        if len(g) > MAX_GRID_CHARS:
            raise LocationTooPrecise(
                f"grid square '{g}' is {len(g)} characters. This tool stores "
                f"at most {MAX_GRID_CHARS} -- 'CM87ux' style, about 5 x 8 km "
                f"-- so that a dataset cannot identify a property. Truncate "
                f"it yourself, e.g. '{g[:MAX_GRID_CHARS]}', so the choice is "
                f"yours and visible."
            )
        if len(g) % 2 or not GRID_RE.match(g):
            raise ValueError(
                f"'{g}' is not a Maidenhead grid square. Expected 4 or 6 "
                f"characters like 'CM87' or 'CM87ux'."
            )

    def as_metadata(self) -> dict:
        return {
            "site_id": self.site_id.strip(),
            "grid_square": (self.grid_square.strip().upper()[:4] +
                            self.grid_square.strip().lower()[4:]
                            if self.grid_square else None),
            "location_shared": self.grid_square is not None,
            "location_precision": (
                f"maidenhead {len(self.grid_square.strip())}-character"
                if self.grid_square else "withheld"),
            "receiver_model": self.receiver_model.strip(),
            "antenna": self.antenna.strip(),
            "antenna_height_m": self.antenna_height_m,
            "preamp": self.preamp,
            "feedline": self.feedline,
            "front_end_filters": self.front_end_filters,
            "operator_note": self.operator_note,
        }
