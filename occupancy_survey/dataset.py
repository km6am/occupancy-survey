"""The on-disk format: one file per site per interval, self-describing.

Designed for merging from the start, because more receivers may appear and a
format that needs a migration to accept a second site is a format that gets
one site.

WHAT MAKES IT MERGEABLE is that the per-channel histograms are in units of
**dB above that sweep's own band baseline**, not absolute dBFS. Two
receivers with different gain settings, different antennas and different
absolute noise floors still produce histograms on a common axis, because
each has had its own noise floor subtracted. Absolute levels are recorded
too, but only as metadata -- they are uncalibrated dBFS, not dBm, and
nothing should compare them across receivers.

The one thing that is NOT common across sites is `bins_per_channel`. It sets
how far the baseline sits above the mean noise power (see
detector.baseline_elevation_db), so a 12-bin channel and a 180-bin channel
have excess-dB axes whose zero points differ by about 3 dB. `merge_channels`
reports that rather than silently pooling across it.

Every file carries, without exception:
  - the detection threshold and the baseline estimator that produced it
  - the gain settings, and that AGC was off
  - dwell, integration, revisit and jitter, with the derived blind spot
  - bins_per_channel
  - the site's receiver and antenna, and a coarse grid square or nothing

A dataset missing any of those cannot be compared with anything, including
itself next month, which is the whole reason the writer refuses to omit them.
"""

from __future__ import annotations

import gzip
import json
import platform
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SCHEMA = "occupancy-survey/1"


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class AcquisitionMetadata:
    """Everything needed to interpret one interval's numbers.

    Assembled from the site, the sampling design, the threshold policy and
    the histogram spec, so there is no way to write a file that records the
    occupancy but not the conditions.
    """
    site: dict
    sampling: dict
    detection: dict
    histogram: dict
    receiver: dict
    software: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        sw = {
            "schema": SCHEMA,
            "tool": "occupancy-survey",
            "python": platform.python_version(),
            "platform": platform.platform(),
            **self.software,
        }
        return {"site": self.site, "sampling": self.sampling,
                "detection": self.detection, "histogram": self.histogram,
                "receiver": self.receiver, "software": sw}

    def validate(self) -> None:
        """Refuse to write a file that cannot be interpreted later."""
        required = [
            ("detection", "threshold_db"),
            ("detection", "estimator"),
            ("detection", "baseline_percentile"),
            ("sampling", "dwell_s"),
            ("sampling", "integration_s"),
            ("histogram", "bin_db"),
            ("receiver", "agc_enabled"),
            ("receiver", "gain"),
            ("site", "site_id"),
            ("site", "antenna"),
        ]
        missing = [f"{a}.{b}" for a, b in required
                   if getattr(self, a).get(b, None) is None]
        if missing:
            raise ValueError(
                "refusing to write a dataset missing acquisition metadata: "
                + ", ".join(missing)
                + ". A dataset whose threshold, gain or dwell is not recorded "
                  "cannot be compared with anything, including itself."
            )
        if self.receiver.get("agc_enabled"):
            raise ValueError(
                "refusing to write a dataset taken with AGC enabled: the "
                "noise floor moved during acquisition and every "
                "noise-floor-relative threshold in the file is meaningless."
            )


@dataclass
class IntervalDataset:
    """One site, one logging interval, all bands."""
    metadata: AcquisitionMetadata
    interval_start: datetime
    interval_end: datetime
    bands: dict = field(default_factory=dict)

    def add_band(self, band, accumulator, bins_per_channel: int,
                 channels_observed: int, front_end: dict | None = None,
                 visits: int = 0, dwell_total_s: float = 0.0,
                 predicted_strongest_hz: float | None = None) -> None:
        from occupancy_survey.detector import (false_alarm_rate,
                                               occupancy_floor_warning)
        thr = self.metadata.detection.get("threshold_db")
        pct = self.metadata.detection.get("baseline_percentile", 25.0)
        floor = (false_alarm_rate(thr, bins_per_channel, pct)
                 if thr is not None and bins_per_channel else None)
        entry = {
            "role": band.role,
            "service": band.service,
            "freq_min_hz": band.freq_min_hz,
            "freq_max_hz": band.freq_max_hz,
            "channel_step_hz": band.channel_step_hz,
            "channel_bandwidth_hz": band.channel_bandwidth_hz,
            "channel_plan": ("explicit" if band.explicit_channels_hz
                             else "uniform_grid"),
            "grid_caveat": (
                "uniform grid, not the FCC channel plan; a carrier offset "
                "from this grid is split across two channels"
                if not band.explicit_channels_hz else None),
            "bins_per_channel": bins_per_channel,
            "channels_observed": channels_observed,
            # The occupancy an EMPTY channel reads at this threshold and
            # channel width. Travels with the data because a reader has to
            # know whether this file's 3% figures might be nothing at all.
            "noise_floor_occupancy": floor,
            "noise_floor_warning": (
                occupancy_floor_warning(thr, bins_per_channel, pct)
                if thr is not None and bins_per_channel else None),
            "note": band.note,
            # What was in front of the receiver. Two datasets taken with
            # different notch states are not comparable at the same
            # threshold, and nothing else in the file would reveal it.
            "front_end": front_end if front_end is not None else (
                band.front_end.as_metadata()),
            "integration_s": self.metadata.sampling.get("integration_s"),
            "timing_note": "runs/gaps/keying are in SWEEPS; multiply by "
                           "integration_s for seconds",
            "visits": int(visits),
            "summary": accumulator.stats.summary(),
            "control_check": accumulator.control_check(),
            "channels": accumulator.channel_records(),
        }
        # A band whose absence indicts the receiver gets a presence check,
        # which is a different and weaker claim than a control's. See
        # occupancy_survey/presence.py.
        presence = getattr(band, "presence", None)
        if presence is not None and presence.enabled:
            from occupancy_survey.presence import presence_check
            mean_dwell = (dwell_total_s / visits) if visits else 0.0
            entry["presence_check"] = presence_check(
                entry["channels"], self.metadata.histogram,
                self.metadata.sampling.get("integration_s") or 0.0,
                n_visits=visits, dwell_s=mean_dwell, reference=presence,
                predicted_strongest_hz=predicted_strongest_hz)
        else:
            entry["presence_check"] = None
        self.bands[band.name] = entry

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "interval": {
                "start_utc": self.interval_start.astimezone(
                    timezone.utc).isoformat(),
                "end_utc": self.interval_end.astimezone(
                    timezone.utc).isoformat(),
                "duration_s": (self.interval_end
                               - self.interval_start).total_seconds(),
            },
            "acquisition": self.metadata.to_dict(),
            "bands": self.bands,
        }

    def filename(self) -> str:
        site = self.metadata.site["site_id"]
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in site)
        return f"occ_{safe}_{_utc_stamp(self.interval_start)}.json.gz"

    def write(self, out_dir, compress: bool = True) -> Path:
        self.metadata.validate()
        if not self.bands:
            raise ValueError("refusing to write an interval with no bands")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = self.filename() if compress else self.filename()[:-3]
        path = out_dir / name
        payload = json.dumps(self.to_dict(), indent=1, allow_nan=False)
        if compress:
            with gzip.open(path, "wt", encoding="utf-8") as f:
                f.write(payload)
        else:
            path.write_text(payload, encoding="utf-8")
        return path


# --------------------------------------------------------------------------- #
# Reading and merging
# --------------------------------------------------------------------------- #

def read_dataset(path) -> dict:
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(
            f"{path.name}: schema '{data.get('schema')}' is not {SCHEMA}. "
            f"Refusing to guess at a format this tool did not write."
        )
    return data


def load_many(paths) -> list[dict]:
    return [read_dataset(p) for p in paths]


@dataclass
class MergedChannel:
    """One channel pooled over every interval and site that observed it."""
    freq_hz: float
    service: str
    hist: np.ndarray
    underflow: int
    overflow: int
    n_decided: int
    sites: set
    bins_per_channel: set
    hist_lo_db: float
    hist_bin_db: float

    def occupancy_at(self, threshold_db: float) -> float | None:
        if self.n_decided == 0:
            return None
        hi = self.hist_lo_db + self.hist_bin_db * self.hist.size
        if threshold_db >= hi:
            above = self.overflow
        else:
            first = max(0, int(np.ceil(
                (threshold_db - self.hist_lo_db) / self.hist_bin_db)))
            above = int(self.hist[first:].sum()) + self.overflow
        return above / self.n_decided

    @property
    def comparable(self) -> bool:
        """False when pooled across different channel widths.

        Different bins_per_channel means different baseline elevation, so
        the excess-dB axes do not share a zero point. The merge still
        happens -- refusing would make a multi-receiver dataset unusable
        over a trivial difference -- but the flag travels with the result.
        """
        return len(self.bins_per_channel) <= 1


def merge_channels(datasets, band_name: str,
                   freq_tolerance_hz: float = 1.0) -> list[MergedChannel]:
    """Pool one band's channels across intervals and sites.

    Channels are matched on frequency to within `freq_tolerance_hz`, so two
    receivers whose grids agree pool together and a receiver on a different
    grid offset does not silently merge into the wrong channel.
    """
    specs = {(d["acquisition"]["histogram"]["lo_db"],
              d["acquisition"]["histogram"]["bin_db"],
              d["acquisition"]["histogram"]["n_bins"])
             for d in datasets if band_name in d.get("bands", {})}
    if not specs:
        return []
    if len(specs) > 1:
        raise ValueError(
            f"datasets use {len(specs)} different histogram specs for "
            f"'{band_name}': {sorted(specs)}. Re-binning would need the raw "
            f"samples, which are not stored. Merge only matching specs."
        )
    lo_db, bin_db, n_bins = specs.pop()

    buckets: dict[int, MergedChannel] = {}
    for d in datasets:
        band = d.get("bands", {}).get(band_name)
        if band is None:
            continue
        site = d["acquisition"]["site"]["site_id"]
        bpc = band.get("bins_per_channel")
        for ch in band["channels"]:
            key = int(round(ch["freq_hz"] / max(freq_tolerance_hz, 1e-9)))
            m = buckets.get(key)
            if m is None:
                m = MergedChannel(
                    freq_hz=ch["freq_hz"], service=ch.get("service", ""),
                    hist=np.zeros(n_bins, dtype=np.int64), underflow=0,
                    overflow=0, n_decided=0, sites=set(),
                    bins_per_channel=set(), hist_lo_db=lo_db,
                    hist_bin_db=bin_db)
                buckets[key] = m
            m.hist += np.asarray(ch["hist"], dtype=np.int64)
            m.underflow += int(ch.get("underflow", 0))
            m.overflow += int(ch.get("overflow", 0))
            m.n_decided += int(ch["n_decided"])
            m.sites.add(site)
            if bpc is not None:
                m.bins_per_channel.add(int(bpc))

    return sorted(buckets.values(), key=lambda m: m.freq_hz)


def service_occupancy(datasets, threshold_db: float | None = None,
                      min_decided: int = 100) -> dict:
    """Service-level occupancy: the statistic this survey exists to produce.

    "VHF business-pool channels around here are busy X% of the time."

    Reported two ways, because they answer different questions and are
    routinely confused:

      `mean_channel_occupancy` -- average over the band's channels,
          including the quiet ones. Low by construction on a grid where most
          channels have no licensee. This is the number that belongs in a
          risk model as "how often is a RANDOM channel here busy".
      `active_channel_occupancy` -- average over channels that were busy at
          all. This is the number comparable with the TIA-603 figures in
          duty_cycles.md, which describe a channel in use, not a channel
          picked at random off a grid.

    Only target bands are summarised. Control bands are reported separately
    with their verdicts, because folding a 100%-occupied broadcast-FM
    carrier into a land-mobile average would be nonsense.
    """
    out = {"threshold_db": threshold_db, "targets": {}, "controls": {}}
    band_names = set()
    for d in datasets:
        band_names.update(d.get("bands", {}).keys())

    for name in sorted(band_names):
        roles = {d["bands"][name]["role"] for d in datasets
                 if name in d.get("bands", {})}
        role = roles.pop() if len(roles) == 1 else "mixed"

        if threshold_db is None:
            thr = {d["acquisition"]["detection"]["threshold_db"]
                   for d in datasets if name in d.get("bands", {})}
            band_thr = thr.pop() if len(thr) == 1 else None
            if band_thr is None:
                raise ValueError(
                    f"'{name}' spans datasets with different default "
                    f"thresholds; pass threshold_db explicitly so the figure "
                    f"states which one it used."
                )
        else:
            band_thr = threshold_db

        merged = merge_channels(datasets, name)
        usable = [m for m in merged if m.n_decided >= min_decided]
        occs = [m.occupancy_at(band_thr) for m in usable]
        occs = [o for o in occs if o is not None]
        active = [o for o in occs if o > 0.0]

        entry = {
            "role": role,
            "threshold_db": band_thr,
            "channels_considered": len(usable),
            "channels_active": len(active),
            "mean_channel_occupancy": (float(np.mean(occs)) if occs else None),
            "active_channel_occupancy": (float(np.mean(active))
                                         if active else None),
            "max_channel_occupancy": (float(max(occs)) if occs else None),
            "sites": sorted({s for m in usable for s in m.sites}),
            "comparable_across_channel_widths": all(m.comparable
                                                    for m in usable),
        }
        if role == "control":
            entry["control_checks"] = [
                d["bands"][name].get("control_check") for d in datasets
                if name in d.get("bands", {})
            ]
            out["controls"][name] = entry
        else:
            out["targets"][name] = entry
    return out
