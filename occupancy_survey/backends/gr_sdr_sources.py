"""SDR source factory for GNU Radio.

VENDORED, with one documented change, from:
    morse_code_recorder/cw_recorder/sdr_sources.py
    (km6am/morse-code-recorder, GPL v3) as of commit 45db8ed.

Why a copy and not a shared import: the two projects share about 150 lines
and nothing else -- occupancy logging needs no IQ ring buffer, narrowband
extractor, recorder or Morse timing -- and the volunteer-facing build of this
project must install without GNU Radio at all. A copy with provenance is
more honest than a package dependency that exists to share one file.

The cost of the copy, stated so it is not forgotten: a fix to the SDR setup
now has to land in two places. If this file and its origin drift, the origin
is authoritative for anything CW-related and this file is authoritative for
anything occupancy-related.

THE CHANGES: the SDRplay branch reads flat attributes off the config object
instead of calling `config.to_rspdx_config()`, because that method belongs to
cw_recorder's much larger config machinery. And since 2026-11-02 it covers
the SDRplay models in sdrplay_models.py, not only the RSPdx: one creator,
with the antenna switch and HDR set only on a model that has them. Gain is
expressed here the way the SDRplay API expresses it -- as a *reduction* in
dB, where larger means less gain.

AGC is refused rather than supported. See occupancy_survey/acquisition.py
for why: an AGC moves the noise floor and takes the occupancy number with it.

Supported devices:
  rspdx, rspdxr2, rsp1a, rsp1b
          - SDRplay (via gr-sdrplay3); see sdrplay_models.py
  rtlsdr  - RTL-SDR dongles (via SoapySDR)
  hackrf  - HackRF One (via SoapySDR)
  lime    - LimeSDR (via SoapySDR)
  pluto   - ADALM-PlutoSDR (via SoapySDR)
  usrp    - Ettus USRP (via gr-uhd)
  soapy   - Generic SoapySDR (specify driver in soapy_driver)
"""

import logging

from occupancy_survey.backends.sdrplay_models import (SDRPLAY_MODELS,
                                                      sdrplay_model)

logger = logging.getLogger(__name__)


class AgcRefused(ValueError):
    """Raised when a config asks for AGC.

    Not a warning. An occupancy dataset taken with AGC on records the
    receiver's gain decisions, not the band's activity, and there is no way
    to tell the two apart after the fact.
    """


def create_source(config):
    """Create a configured SDR source block.

    Args:
        config: an object with device_type, center_freq_hz, sample_rate_hz,
            bw_hz and the device-specific attributes used below.

    Returns:
        GNU Radio source block outputting complex64 IQ.
    """
    if getattr(config, "agc_enabled", False):
        raise AgcRefused(
            "AGC is enabled. An occupancy survey requires fixed gain: an AGC "
            "moves the noise floor, and every threshold in this tool is "
            "noise-floor-relative. Set agc_enabled: false and an explicit gain."
        )

    device_type = config.device_type.lower().strip()

    creators = {
        **{name: _create_sdrplay_source for name in SDRPLAY_MODELS},
        "rtlsdr": _create_soapy_source,
        "hackrf": _create_soapy_source,
        "lime": _create_soapy_source,
        "pluto": _create_soapy_source,
        "soapy": _create_soapy_source,
        "usrp": _create_uhd_source,
    }

    if device_type not in creators:
        raise ValueError(
            f"Unknown SDR device type '{config.device_type}'. "
            f"Supported: {', '.join(creators.keys())}"
        )

    source = creators[device_type](config)
    logger.info("SDR source: %s, %.3f MHz, %.0f kHz BW",
                device_type, config.center_freq_hz / 1e6,
                config.sample_rate_hz / 1e3)
    return source


def _create_sdrplay_source(config, sdrplay3=None):
    """Create an SDRplay source via gr-sdrplay3.

    `sdrplay3` is injectable so the per-model calls can be tested without
    the library or a device.
    """
    if sdrplay3 is None:
        from gnuradio import sdrplay3
    model = sdrplay_model(config.device_type)

    source = getattr(sdrplay3, model.block)(
        getattr(config, "device_selector", "") or "",
        sdrplay3.stream_args("fc32", 1),
    )
    source.set_sample_rate(config.sample_rate_hz)
    source.set_center_freq(config.center_freq_hz)
    source.set_bandwidth(config.bw_hz)
    source.set_gain_mode(False)  # False == manual gain; see AgcRefused above
    source.set_gain(-config.if_gain_reduction_db, "IF")
    source.set_gain(-config.rf_gain_reduction_db, "RF")
    # The RSP1A and RSP1B have one input and no setter for either; calling
    # one would raise AttributeError from inside a constructor.
    if model.has_antenna_switch:
        source.set_antenna(config.antenna_port)
    if model.hdr:
        source.set_hdr_mode(getattr(config, "hdr_mode", False))
    # Notch filters are set per BAND at run time, not here; see
    # GnuRadioBackend.set_filters(). Start with them off so the initial
    # state is known rather than inherited from the last application that
    # touched the device.
    for setter in ("set_rf_notch_filter", "set_dab_notch_filter"):
        fn = getattr(source, setter, None)
        if fn is not None:
            fn(False)
    source.set_biasT(getattr(config, "biasT", False))
    source.set_dc_offset_mode(getattr(config, "dc_offset_correction", True))
    source.set_iq_balance_mode(getattr(config, "iq_balance_correction", True))

    logger.info("%s: IF -%.0f dB, RF -%.0f dB, antenna %s", model.name,
                config.if_gain_reduction_db, config.rf_gain_reduction_db,
                config.antenna_port if model.has_antenna_switch
                else "(single input)")
    return source


# SoapySDR driver names for known device types
_SOAPY_DRIVERS = {
    "rtlsdr": "rtlsdr",
    "hackrf": "hackrf",
    "lime": "lime",
    "pluto": "plutosdr",
}


def _create_soapy_source(config):
    """Create SoapySDR source (RTL-SDR, HackRF, LimeSDR, PlutoSDR, etc.)."""
    from gnuradio import soapy

    device_type = config.device_type.lower().strip()

    if device_type == "soapy" and getattr(config, "soapy_driver", ""):
        driver = config.soapy_driver
    elif device_type in _SOAPY_DRIVERS:
        driver = _SOAPY_DRIVERS[device_type]
    else:
        driver = device_type

    dev_args = f"driver={driver}"
    if getattr(config, "device_selector", ""):
        dev_args += f",{config.device_selector}"

    source = soapy.source(dev_args, "fc32", 1, "")
    source.set_sample_rate(0, config.sample_rate_hz)
    source.set_frequency(0, config.center_freq_hz)
    source.set_bandwidth(0, config.bw_hz)
    source.set_gain_mode(0, False)
    source.set_gain(0, config.gain)

    logger.info("SoapySDR: driver=%s, gain=%.0f", driver, config.gain)
    return source


def _create_uhd_source(config):
    """Create USRP source via gr-uhd."""
    from gnuradio import uhd

    dev_addr = getattr(config, "device_selector", "") or ""
    source = uhd.usrp_source(dev_addr, uhd.stream_args("fc32", "", 1))
    source.set_samp_rate(config.sample_rate_hz)
    source.set_center_freq(config.center_freq_hz, 0)
    source.set_bandwidth(config.bw_hz, 0)
    source.set_gain(config.gain, 0)
    if getattr(config, "antenna_port", ""):
        source.set_antenna(config.antenna_port, 0)

    logger.info("UHD/USRP: addr=%s, gain=%.0f", dev_addr, config.gain)
    return source
