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

from occupancy_survey.backends.sdrplay_models import (SDRPLAY_MODELS,
                                                      sdrplay_model)
from occupancy_survey.channels import DEFAULT_SCHEDULE, resolve_bands
from occupancy_survey.detector import ThresholdPolicy
from occupancy_survey.occupancy import HistogramSpec
from occupancy_survey.schedule import SamplingDesign
from occupancy_survey.site import SiteMetadata


GNURADIO_BACKENDS = ("gnuradio", "gr", "rspdx")


def _check_sdrplay_hardware(sdr, sdr_raw: dict) -> None:
    """Refuse a setting the chosen SDRplay model has no hardware for.

    Caught here rather than when the device opens: an RSP1A has no setter
    for either an antenna port or HDR, and an RSPdx port name is matched
    exactly by gr-sdrplay3, so a typo would fail only after the receiver
    was already streaming -- or, worse, be ignored.
    """
    model = sdrplay_model(sdr.device_type)
    if model is None or sdr.backend.lower() not in GNURADIO_BACKENDS:
        return
    if not model.has_antenna_switch:
        if sdr_raw.get("antenna_port"):
            raise ValueError(
                f"sdr.antenna_port is set, but the {model.name} has one "
                f"antenna input and no port switch. Remove the key; the "
                f"antenna itself is described under site.antenna.")
        # The dataclass default names an RSPdx port; it means nothing here.
        sdr.antenna_port = ""
    elif sdr.antenna_port not in model.antenna_ports:
        raise ValueError(
            f"sdr.antenna_port {sdr.antenna_port!r} is not a {model.name} "
            f"port. Choose one of: {', '.join(model.antenna_ports)}.")
    if sdr.hdr_mode and not model.hdr:
        raise ValueError(f"sdr.hdr_mode is set, but the {model.name} has no "
                         f"HDR mode.")


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
    # Per-band RF reduction overriding rf_gain_reduction_db, e.g.
    # {"bcast_fm": 40}. Measured 2026-11-01: the FM band clipped at 2-3%
    # with the LMR setting and stopped clipping at 40. Each band's value is
    # fixed for the campaign and recorded per band in every dataset, so the
    # fixed-gain rule holds per band rather than per run.
    band_rf_gain_reduction_db: dict = field(default_factory=dict)
    # Frequencies (MHz) where THIS receiver makes its own signal: measured,
    # not guessed -- a channel that reads the same with no antenna on the
    # port, or that does not follow RF gain. Channels on them are tagged in
    # the dataset and left out of service figures. Tuning-centre DC spikes
    # are handled separately and do not belong here.
    spurs_mhz: list = field(default_factory=list)
    antenna_port: str = "Antenna B"
    hdr_mode: bool = False
    biasT: bool = False
    dc_offset_correction: bool = True
    iq_balance_correction: bool = True
    device_index: int = 0
    ppm: int = 0
    rtl_power_csv: str = ""
    rtl_power_interval_s: float = 1.0

    # backend: synthetic only. Carriers to simulate, so that an install
    # check or a demonstration shows a realistic display rather than an
    # empty one. Each entry: freq_hz, snr_db, duty, period_s, bandwidth_hz.
    synthetic_emitters: list = field(default_factory=list)

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

    # Set by --channels; overrides the grid schedule when present.
    targeted_bands: list | None = None

    def band_plans(self):
        if self.targeted_bands:
            return list(self.targeted_bands)
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
        _check_sdrplay_hardware(sdr, sdr_raw)

        samp_raw = dict(raw.get("sampling") or {})
        # Derived unless explicit; see the module docstring.
        samp_raw.setdefault("usable_bandwidth_hz", sdr.sample_rate_hz)
        sampling = SamplingDesign(**samp_raw)

        detection = ThresholdPolicy(**(raw.get("detection") or {}))
        histogram = HistogramSpec(**(raw.get("histogram") or {}))

        bands = tuple(raw.get("bands") or DEFAULT_SCHEDULE)
        resolve_bands(bands)  # fail now, not after an hour of acquisition

        per_band = dict(sdr.band_rf_gain_reduction_db or {})
        stray = set(per_band) - set(bands)
        if stray:
            # A misspelled band name would leave that band at the default
            # gain while the config appeared to set it.
            raise ValueError(
                f"sdr.band_rf_gain_reduction_db names band(s) not in the "
                f"schedule: {', '.join(sorted(stray))}")
        for name, value in per_band.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(
                    f"sdr.band_rf_gain_reduction_db.{name} must be a "
                    f"reduction in dB >= 0, got {value!r}")
        if per_band and not (sdr.backend.lower() in GNURADIO_BACKENDS
                             and sdrplay_model(sdr.device_type)):
            raise ValueError(
                "sdr.band_rf_gain_reduction_db is only actuated on an SDRplay "
                f"receiver ({', '.join(SDRPLAY_MODELS)}) through the gnuradio "
                "backend. Refusing rather than recording a per-band gain this "
                "receiver would not apply.")
        sdr.band_rf_gain_reduction_db = {k: float(v)
                                         for k, v in per_band.items()}
        try:
            sdr.spurs_mhz = [float(s) for s in (sdr.spurs_mhz or [])]
        except (TypeError, ValueError):
            raise ValueError(
                f"sdr.spurs_mhz must be a list of frequencies in MHz, got "
                f"{sdr.spurs_mhz!r}") from None

        out = dict(raw.get("output") or {})
        return cls(site=site, sdr=sdr, sampling=sampling, detection=detection,
                   histogram=histogram, bands=bands,
                   out_dir=out.get("dir", "./data"))
