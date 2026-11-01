"""Vendored copies of what this survey needs from repositories it may not edit.

WHY THIS EXISTS. Two sibling projects are upstream of this one and both are
READ-ONLY here (see CLAUDE.md): `hp8935_sinad_serial` owns the duty-cycle
table this survey exists to fill, and `sota-rf` turns FCC licence records
into a per-emitter prediction. Depending on sibling checkouts made the
survey host carry an analysis toolchain it never runs, and made a clone on
a new machine useless until two more clones and a 756 MB download had
happened.

WHAT IS VENDORED, AND WHAT DELIBERATELY IS NOT.

Vendored: the *reference table* and the *data contracts*. Both are small,
slow-moving, and are what the code here actually reads.

NOT vendored: sota-rf's pipeline or its FCC database. That is ~756 MB of
third-party data with an acquisition date on it, and copying it would
freeze a dataset that should be refreshed, duplicate a maintained tool so
that fixes stop flowing, and swap a dependency this repo can state for one
it would silently own. `tools/qth_predict.py` still shells out to sota-rf,
and that is now an OPTIONAL IMPORT STEP rather than a dependency: with the
contract and fixtures below, everything except generating a fresh
prediction runs with no sibling checkout at all.

THE RISK VENDORING CREATES, STATED PLAINLY. A copy of someone else's claim
can drift from it, and a stale copy fails silently in the worst direction:
measurements get compared against an assumption that has already been
revised. So the vendored table carries the source branch, commit, blob and
sha256 it was taken from, and `check_duty_cycles_drift()` re-verifies it
whenever the sibling repo happens to be present. When it is absent the
answer is "unknown", never "fine".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).parent
_DUTY_CYCLES = _HERE / "duty_cycles_section3.json"


def duty_cycles() -> dict:
    """The vendored section-3 table, with its provenance block."""
    return json.loads(_DUTY_CYCLES.read_text(encoding="utf-8"))


def duty_cycles_rows() -> list[dict]:
    """Just the table rows."""
    return duty_cycles()["rows"]


def duty_cycles_provenance() -> dict:
    """Where the vendored table came from, exactly."""
    return duty_cycles()["source"]


def provenance_line() -> str:
    """One line naming the vendored copy, for --check and dataset headers."""
    p = duty_cycles_provenance()
    return (f"duty_cycles.md section 3 vendored from {p['path']} @ "
            f"{p['branch_read']} blob {p['blob_sha1'][:12]} "
            f"(captured {p['captured']})")


# --------------------------------------------------------------------------- #
# Drift check against the authority, when it is reachable
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class DriftCheck:
    """Whether the vendored copy still matches its source.

    `in_sync` is a TRI-STATE on purpose. None means the source was not
    reachable, which is the normal case on a survey host and must not be
    reported as agreement.
    """
    in_sync: bool | None
    detail: str
    source_path: Path | None = None

    @property
    def checked(self) -> bool:
        return self.in_sync is not None


def check_duty_cycles_drift(source_repo: Path | str | None = None) -> DriftCheck:
    """Re-hash the source file and compare it with what was vendored.

    Compares the WHOLE FILE, not the extracted rows: a reworded basis or a
    changed footnote is a change to the claim, and parsing only the table
    would miss both.
    """
    p = duty_cycles_provenance()
    root = Path(source_repo).expanduser() if source_repo else \
        (Path.home() / "Code/hp8935_sinad_serial")
    src = root / p["path"]
    if not src.is_file():
        return DriftCheck(
            None,
            f"source not present at {src}; the vendored copy is pinned to "
            f"blob {p['blob_sha1'][:12]} and was NOT re-verified",
            None)

    actual = hashlib.sha256(src.read_bytes()).hexdigest()
    if actual == p["file_sha256"]:
        return DriftCheck(True, f"matches {src} exactly", src)
    return DriftCheck(
        False,
        f"{src} has changed since {p['captured']} (sha256 "
        f"{actual[:12]} vs vendored {p['file_sha256'][:12]}). Re-run "
        f"tools/revendor_reference.py and review what moved before "
        f"trusting a comparison against it.",
        src)


# --------------------------------------------------------------------------- #
# sota-rf data contracts
# --------------------------------------------------------------------------- #

# The per-emitter CSV sota-rf writes as <ASSOCIATION>_rf_sources.csv, as
# emitted 2026-11-01. Recorded in full so a column that disappears is a
# named failure rather than a KeyError or, worse, a silent None.
SOTA_RF_SOURCES_COLUMNS: tuple[str, ...] = (
    "summit", "summit_name", "region", "summit_lat", "summit_lon",
    "summit_alt_m", "distance_m", "rf_source_db", "rf_ref", "rf_owner",
    "rf_lat", "rf_lon", "rf_struct_type", "rf_height_agl_m",
    "rf_height_amsl_m", "rf_freqs_mhz", "rf_max_power_w", "rf_services",
    "rf_link_reg", "rf_bays", "rf_spacing", "terrain_loss_db",
)

# LOAD-BEARING. Absent, these do not raise -- they produce a prediction
# with None where the channel, the ERP, the distance or the service class
# should be, which ranks emitters off incomplete data and looks like a
# result. That is the failure worth a hard error.
#   rf_freqs_mhz    the channel being predicted
#   rf_services     the continuity class, via channels.continuity_for()
#   distance_m  }   the ERP/d^2 field proxy, which is the whole ordering
#   rf_max_power_w }
SOTA_RF_REQUIRED_COLUMNS: tuple[str, ...] = (
    "distance_m", "rf_freqs_mhz", "rf_max_power_w", "rf_services",
)

# CARRIED THROUGH as metadata. Absent, a field goes None and nothing about
# the measurement changes: rf_ref only identifies the emitter for the
# summit-tag join, and owner/lat/lon are descriptive. Kept separate from
# the list above so "required" means exactly one thing.
SOTA_RF_CARRIED_COLUMNS: tuple[str, ...] = (
    "rf_ref", "rf_owner", "rf_lat", "rf_lon",
)

# sota-rf's summit list: it finds the header by startswith("SummitCode")
# and filters on the prefix before the "/", which is what lets a one-row
# pseudo-summit point the whole pipeline at a fixed QTH.
SUMMIT_CSV_HEADER: tuple[str, ...] = (
    "SummitCode", "SummitName", "RegionName", "AssociationName", "AltM",
    "AltFt", "GridRef1", "GridRef2", "Longitude", "Latitude", "Points",
    "BonusPoints", "ValidFrom", "ValidTo", "ActivationCount",
    "ActivationDate", "ActivationCall",
)

# The prediction file tools/qth_predict.py writes and
# tools/validate_occupancy.py reads. Owned by THIS repo, unlike the two
# above, so it is the one contract here that is authoritative.
PREDICTION_SCHEMA = "occupancy-survey/qth-prediction/1"


def missing_sota_rf_columns(fieldnames) -> list[str]:
    """Required columns absent from a sota-rf CSV header."""
    present = {str(f).strip() for f in (fieldnames or ())}
    return [c for c in SOTA_RF_REQUIRED_COLUMNS if c not in present]
