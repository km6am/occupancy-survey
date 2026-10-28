"""Configuration, with the coherence traps closed by construction.

Two things are derived rather than configured, because getting them
inconsistent is silent:

  - `sampling.usable_bandwidth_hz` defaults to the SDR's sample rate.
    Configuring them separately is how a survey ends up spacing tune steps
    further apart than the receiver can see, leaving gaps that look like
    quiet channels. It can still be set explicitly -- a receiver whose
    passband is narrower than its sample rate has a reason to -- but it
    will not silently default to something unrelated.

  - AGC is not a setting. It is absent from the schema and forced off, and
    `gr_sdr_sources.create_source()` raises if a config carries it. There is
    no valid occupancy survey with AGC on, so offering the option would only
    create datasets that have to be thrown away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from occupancy_survey.channels import DEFAULT_SCHEDULE, resolve_bands
from occupancy_survey.detector import ThresholdPolicy
from occupancy_survey.occupancy import HistogramSpec
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata


@dataclass
class SdrConfig:
    """Device settings. Duck-typed to suit the vendored source factory."""
    backend: str = "synthetic"
    device_type: str = "rspdx"
    device_selector: str = ""
    soapy_driver: str = ""
    sample_rate_hz: float = 2_048_000.0
    bw_hz: float = 2_048_000.0
    center_freq_hz: float = 162_475_000.0
    fft_size: int = 4096
    gain: float = 40.0
    if_gain_reduction_db: float = 40.0
    rf_gain_reduction_db: float = 0.0
    antenna_port: str = "Antenna B"
    hdr_mode: bool = False
    biasT: bool = False
    dc_offset_correction: bool = True
    iq_balance_correction: bool = True
    device_index: int = 0
    ppm: int = 0
    rtl_power_csv: str = ""
    rtl_power_interval_s: float = 1.0

    # Not configurable. See the module docstring.
    agc_enabled: bool = field(default=False, init=False)


@dataclass
class AppConfig:
    site: SiteMetadata
    sdr: SdrConfig
    sampling: SamplingDesign
    detection: ThresholdPolicy
    histogram: HistogramSpec
    bands: tuple = DEFAULT_SCHEDULE
    out_dir: str = "./data"

    def band_plans(self):
        return resolve_bands(self.bands)

    @classmethod
    def from_yaml(cls, path) -> "AppConfig":
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "AppConfig":
        site_raw = dict(raw.get("site") or {})
        if not site_raw:
            raise ValueError(
                "config has no `site:` block. A dataset without a site_id, "
                "receiver and antenna cannot be interpreted or merged, so "
                "there is no useful default to fall back on."
            )
        site = SiteMetadata(
            site_id=site_raw.get("site_id", ""),
            receiver_model=site_raw.get("receiver_model", ""),
            antenna=site_raw.get("antenna", ""),
            grid_square=site_raw.get("grid_square"),
            antenna_height_m=site_raw.get("antenna_height_m"),
            preamp=site_raw.get("preamp", "none"),
            feedline=site_raw.get("feedline", "unspecified"),
            front_end_filters=site_raw.get("front_end_filters", "none"),
            operator_note=site_raw.get("operator_note", ""),
        )

        sdr_raw = dict(raw.get("sdr") or {})
        if "agc_enabled" in sdr_raw:
            raise ValueError(
                "`sdr.agc_enabled` is not a setting. An AGC moves the noise "
                "floor that every threshold in this tool is relative to, so "
                "there is no valid occupancy survey with it on. Remove the "
                "key and set an explicit gain."
            )
        known = {f for f in SdrConfig.__dataclass_fields__
                 if f != "agc_enabled"}
        unknown = set(sdr_raw) - known
        if unknown:
            raise ValueError(
                f"unknown sdr settings: {', '.join(sorted(unknown))}. "
                f"Refusing to ignore them silently -- a misspelled gain key "
                f"would leave the default gain in place and the dataset "
                f"would record the default as if it had been chosen."
            )
        sdr = SdrConfig(**sdr_raw)
        if sdr.bw_hz == SdrConfig.bw_hz and "bw_hz" not in sdr_raw:
            sdr.bw_hz = sdr.sample_rate_hz

        samp_raw = dict(raw.get("sampling") or {})
        # Derived unless explicit; see the module docstring.
        samp_raw.setdefault("usable_bandwidth_hz", sdr.sample_rate_hz)
        sampling = SamplingDesign(**samp_raw)

        detection = ThresholdPolicy(**(raw.get("detection") or {}))
        histogram = HistogramSpec(**(raw.get("histogram") or {}))

        bands = tuple(raw.get("bands") or DEFAULT_SCHEDULE)
        resolve_bands(bands)  # fail now, not after an hour of acquisition

        out = dict(raw.get("output") or {})
        return cls(site=site, sdr=sdr, sampling=sampling, detection=detection,
                   histogram=histogram, bands=bands,
                   out_dir=out.get("dir", "./data"))
