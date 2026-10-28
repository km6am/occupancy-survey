"""Ingest `rtl_power` CSV. A fallback, with its bias written into the data.

rtl_power is attractive for a volunteer: it is already packaged everywhere
and needs no Python at all. It is also the WRONG PRIMITIVE for occupancy,
and the dataset says so rather than leaving the reader to find out.

Its CSV column is the level per bin INTEGRATED OVER THE WHOLE INTERVAL --
10 s by default. Averaging and then thresholding is the mistake
duty_cycles.md section 1 warns about, moved from the model into the
measurement:

  - a channel keyed 20% of a 10 s window reads as one continuous carrier
    about 7 dB weaker, so its occupancy reads 0% or 100% and never 20%;
  - a 2 s burst in a 10 s window loses 7 dB and may not clear the threshold
    at all.

So occupancy from this backend is only meaningful when the interval is short
compared with the transmissions -- `rtl_power -i 1s` or shorter -- and even
then each row is one sample, so a 1 s interval gives 3600 samples an hour
against the 720,000 an rtl_sdr pipe at 0.1 s gives in 20 s of dwell.

Every dataset built this way carries
`receiver.averaging_bias = "pre-threshold"` and the interval used.
`tools/validate_occupancy.py` reports it on every plot built from such data.

Use rtl_sdr_pipe.py instead wherever the binary is available.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RtlPowerRow:
    """One rtl_power CSV row: a frequency span and its averaged levels."""
    t_unix: float
    hz_low: float
    hz_high: float
    hz_step: float
    n_samples: int
    levels_db: np.ndarray

    def freq_axis(self) -> np.ndarray:
        return self.hz_low + self.hz_step * np.arange(self.levels_db.size)


def parse_rtl_power(path, interval_s: float | None = None):
    """Yield RtlPowerRow from an rtl_power CSV.

    Columns are: date, time, Hz low, Hz high, Hz step, samples, then one dB
    value per bin.
    """
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for lineno, row in enumerate(csv.reader(f), start=1):
            if len(row) < 7:
                continue
            try:
                stamp = datetime.strptime(
                    f"{row[0].strip()} {row[1].strip()}",
                    "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                levels = np.array([float(x) for x in row[6:] if x.strip()],
                                  dtype=np.float64)
                yield RtlPowerRow(
                    t_unix=stamp.timestamp(),
                    hz_low=float(row[2]), hz_high=float(row[3]),
                    hz_step=float(row[4]), n_samples=int(row[5]),
                    levels_db=levels,
                )
            except (ValueError, IndexError) as e:
                logger.warning("%s:%d: skipping unparseable row (%s)",
                               path, lineno, e)


def receiver_metadata(interval_s: float, gain_db: float,
                      csv_path: str) -> dict:
    """Metadata for an rtl_power-derived dataset, bias included."""
    return {
        "backend": "rtl_power_csv",
        "source_file": str(csv_path),
        "gain": gain_db,
        "gain_units": "rtl_power tuner gain, dB",
        "agc_enabled": False,
        "integration_s": interval_s,
        "averaging_bias": "pre-threshold",
        "averaging_bias_note": (
            f"rtl_power averaged each bin over {interval_s:g} s BEFORE this "
            f"tool could threshold it. A transmission shorter than "
            f"{interval_s:g} s is attenuated by 10*log10(burst/"
            f"{interval_s:g}) dB and an intermittent channel tends to read "
            f"0% or 100% rather than its true duty cycle. Occupancy from "
            f"this backend is a lower bound for short transmissions and is "
            f"not comparable with rtl_sdr_pipe data at the same threshold."
        ),
        "min_trustworthy_burst_s": interval_s,
    }
