"""Channel plans to survey, and the continuity class of each service.

Two separate things live here, and they are known in different ways.

CHANNEL GRIDS are *uniform grids*, not the FCC channel plan. The real plan
for 150-174 MHz is a patchwork of 7.5 and 6.25 kHz offsets with historical
exceptions, and encoding it wrongly would be worse than not encoding it: a
grid that is off by half a channel splits every carrier across two channels
and halves the occupancy of both. So the grids below are declared as grids,
their step is recorded in every dataset, and the authoritative channel list
for a given survey is the one `tools/qth_predict.py` derives from the FCC
licence records for that location. Treat the grids as a way to find activity
you did not predict, and the FCC-derived list as the way to measure a
channel you did predict.

CONTINUITY CLASSES mirror
    ~/Code/hp8935_sinad_serial/docs/reference/duty_cycles.md  section 2
which is the authority for them. That document must not be edited from here;
if a mapping below turns out to be wrong, propose the change there rather
than diverging. The rule it establishes, and the reason the mapping is
cheap: a `Y` prefix in the FCC radio service code means trunked, and a
trunked site's control channel is a dedicated always-on carrier.

Evidential status of the continuity column: *architectural* -- it follows
from how the systems work, not from traffic statistics. Nothing in it has
been measured. Producing the measured version is this repo's whole purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# Continuity classes (mirror of duty_cycles.md section 2 + section 3)
# --------------------------------------------------------------------------- #

CONTINUOUS = "continuous"
INTERMITTENT = "intermittent"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ServiceContinuity:
    """What a radio service's carrier does when nobody is talking.

    `expected_duty` is the figure from duty_cycles.md, carried here so a
    measured result can be compared against the assumption it replaces. It
    is NOT evidence; `basis` says what kind of claim it is.
    """
    code: str
    name: str
    continuity: str
    expected_duty: float | None
    basis: str


# Keyed by FCC radio service code as it appears in sota-rf's `rf_services`.
SERVICE_CONTINUITY: dict[str, ServiceContinuity] = {
    "PW": ServiceContinuity("PW", "Public Safety Pool, conventional",
                            INTERMITTENT, 0.225,
                            "standard -- TIA-603 intermittent test cycle"),
    "YW": ServiceContinuity("YW", "Public Safety Pool, trunked",
                            CONTINUOUS, 1.0,
                            "architectural -- dedicated control channel"),
    "IG": ServiceContinuity("IG", "Industrial/Business Pool, conventional",
                            INTERMITTENT, 0.225,
                            "standard -- TIA-603 intermittent test cycle"),
    "YG": ServiceContinuity("YG", "Industrial/Business Pool, trunked",
                            CONTINUOUS, 1.0,
                            "architectural -- dedicated control channel"),
    "IK": ServiceContinuity("IK", "Industrial/Business 470-512, conventional",
                            INTERMITTENT, 0.225, "standard -- as IG"),
    "YK": ServiceContinuity("YK", "Industrial/Business 470-512, trunked",
                            CONTINUOUS, 1.0, "architectural -- as YG"),
    "RS": ServiceContinuity("RS", "Land Mobile Radiolocation",
                            UNKNOWN, None, "varies"),
    "CF": ServiceContinuity("CF", "Common carrier fixed microwave",
                            CONTINUOUS, 1.0,
                            "architectural -- backhaul carrier is continuous"),
    "MW": ServiceContinuity("MW", "Private operational fixed microwave",
                            CONTINUOUS, 1.0, "architectural -- as CF"),
    "MG": ServiceContinuity("MG", "Private microwave",
                            CONTINUOUS, 1.0, "architectural -- as CF"),
    "TS": ServiceContinuity("TS", "Studio-transmitter link",
                            CONTINUOUS, 1.0,
                            "architectural -- up while the parent is on air"),
    "TI": ServiceContinuity("TI", "TV intercity relay",
                            CONTINUOUS, 1.0, "architectural -- as TS"),
    "FM": ServiceContinuity("FM", "Broadcast FM",
                            CONTINUOUS, 1.0,
                            "architectural -- a carrier with programme on it"),
    "TV": ServiceContinuity("TV", "Broadcast TV", CONTINUOUS, 1.0,
                            "architectural"),
    "AM": ServiceContinuity("AM", "Broadcast AM", CONTINUOUS, 1.0,
                            "architectural"),
    "NWR": ServiceContinuity("NWR", "NOAA Weather Radio", CONTINUOUS, 1.0,
                             "architectural -- continuous automated broadcast"),
    "PG": ServiceContinuity("PG", "Paging", INTERMITTENT, None,
                            "OET 65 -- high but not 100%"),
    "AR": ServiceContinuity("AR", "Amateur repeater", INTERMITTENT, None,
                            "estimated"),
}


def continuity_for(service_code: str) -> ServiceContinuity:
    """Look up a service code, tolerating the forms sota-rf emits.

    sota-rf's `rf_services` can hold several codes separated by punctuation.
    The first recognised code wins; an unrecognised string is UNKNOWN rather
    than a guess, because a wrong continuity class would silently move an
    emitter between the continuous and intermittent sets in the risk model.
    """
    if not service_code:
        return ServiceContinuity("", "unspecified", UNKNOWN, None, "no code")
    for token in str(service_code).replace("/", " ").replace(",", " ").split():
        key = token.strip().upper()
        if key in SERVICE_CONTINUITY:
            return SERVICE_CONTINUITY[key]
    return ServiceContinuity(str(service_code), "unrecognised", UNKNOWN, None,
                             "code not in duty_cycles.md section 2")


# --------------------------------------------------------------------------- #
# Channels and band plans
# --------------------------------------------------------------------------- #

TARGET = "target"
CONTROL = "control"


@dataclass(frozen=True)
class Channel:
    """One channel to measure occupancy on."""
    freq_hz: float
    bandwidth_hz: float
    service: str
    label: str = ""

    @property
    def continuity(self) -> ServiceContinuity:
        return continuity_for(self.service)


@dataclass(frozen=True)
class BandPlan:
    """A contiguous span to survey in one receiver visit.

    `role` is the measurement's own control structure:

      TARGET  -- a band whose occupancy is the thing being measured.
      CONTROL -- a band whose occupancy is already known from architecture
                 (NOAA Weather Radio and broadcast FM are continuous
                 carriers). A control band is the calibration for the
                 detection threshold: if a control channel that must be
                 ~100% occupied does not read ~100%, the threshold or the
                 receive path is wrong, and the target numbers from the same
                 run are not trustworthy either.

    `expected_occupancy` is only set for controls, and only where the
    architecture fixes it. It is an assumption being used as a check, not a
    measurement.
    """
    name: str
    freq_min_hz: float
    freq_max_hz: float
    channel_step_hz: float
    channel_bandwidth_hz: float
    service: str
    role: str = TARGET
    expected_occupancy: float | None = None
    explicit_channels_hz: tuple[float, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def span_hz(self) -> float:
        return self.freq_max_hz - self.freq_min_hz

    @property
    def center_hz(self) -> float:
        return 0.5 * (self.freq_min_hz + self.freq_max_hz)

    def channels(self) -> list[Channel]:
        """The channels in this band.

        Explicit channel lists are used verbatim. Otherwise a uniform grid
        is generated -- which is a grid, not the FCC channel plan; see the
        module docstring.
        """
        if self.explicit_channels_hz:
            freqs = list(self.explicit_channels_hz)
        else:
            freqs = []
            f = self.freq_min_hz
            # Integer stepping, so a long span cannot accumulate float drift
            # into a half-channel offset by the far end.
            n = int(round(self.span_hz / self.channel_step_hz))
            freqs = [self.freq_min_hz + i * self.channel_step_hz
                     for i in range(n + 1)]
        return [
            Channel(freq_hz=f, bandwidth_hz=self.channel_bandwidth_hz,
                    service=self.service,
                    label=f"{self.name}:{f / 1e6:.4f}")
            for f in freqs
        ]


# NOAA Weather Radio: seven exact channels, not a grid.
NWR_CHANNELS_HZ = (162_400_000.0, 162_425_000.0, 162_450_000.0,
                   162_475_000.0, 162_500_000.0, 162_525_000.0,
                   162_550_000.0)


# --------------------------------------------------------------------------- #
# The default schedule: targets plus known-continuous controls
# --------------------------------------------------------------------------- #

BAND_PLANS: dict[str, BandPlan] = {
    "vhf_lmr_low": BandPlan(
        name="vhf_lmr_low",
        freq_min_hz=150_800_000.0, freq_max_hz=156_000_000.0,
        channel_step_hz=12_500.0, channel_bandwidth_hz=11_250.0,
        service="IG", role=TARGET,
        note="VHF business/public-safety pool, lower half. 12.5 kHz grid; "
             "the real plan has 7.5 and 6.25 kHz offsets in places.",
    ),
    "vhf_lmr_high": BandPlan(
        name="vhf_lmr_high",
        freq_min_hz=157_000_000.0, freq_max_hz=161_600_000.0,
        channel_step_hz=12_500.0, channel_bandwidth_hz=11_250.0,
        service="IG", role=TARGET,
        note="VHF business/public-safety pool, upper half.",
    ),
    "uhf_lmr": BandPlan(
        name="uhf_lmr",
        freq_min_hz=450_000_000.0, freq_max_hz=470_000_000.0,
        channel_step_hz=12_500.0, channel_bandwidth_hz=11_250.0,
        service="IG", role=TARGET,
        note="UHF business/public-safety pool. 12.5 kHz grid.",
    ),
    "nwr": BandPlan(
        name="nwr",
        freq_min_hz=162_400_000.0, freq_max_hz=162_550_000.0,
        channel_step_hz=25_000.0, channel_bandwidth_hz=16_000.0,
        service="NWR", role=CONTROL, expected_occupancy=1.0,
        explicit_channels_hz=NWR_CHANNELS_HZ,
        note="Positive control. A receivable NWR transmitter is a continuous "
             "carrier; at least one of these seven should read ~100%. If none "
             "does, either no NWR station is receivable here (check the "
             "prediction) or the detector is broken.",
    ),
    "bcast_fm": BandPlan(
        name="bcast_fm",
        freq_min_hz=88_100_000.0, freq_max_hz=107_900_000.0,
        channel_step_hz=200_000.0, channel_bandwidth_hz=180_000.0,
        service="FM", role=CONTROL, expected_occupancy=1.0,
        note="Positive control. Each local FM station is a continuous "
             "carrier, so the strong ones should read ~100%. Also the "
             "widest-dynamic-range band in the schedule, which makes it the "
             "place a fixed gain setting will clip first.",
    ),
}

DEFAULT_SCHEDULE = ("vhf_lmr_low", "nwr", "vhf_lmr_high", "uhf_lmr",
                    "bcast_fm")


def resolve_bands(names) -> list[BandPlan]:
    """Resolve band names to plans, erroring on an unknown name."""
    out = []
    for n in names:
        key = str(n).strip().lower()
        if key not in BAND_PLANS:
            raise ValueError(
                f"Unknown band '{n}'. Known: {', '.join(sorted(BAND_PLANS))}"
            )
        out.append(BAND_PLANS[key])
    return out


def control_bands(plans) -> list[BandPlan]:
    """The subset of `plans` that are positive controls."""
    return [p for p in plans if p.role == CONTROL]
