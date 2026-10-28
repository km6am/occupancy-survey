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
    # RETRACTED 2026-10-28, and worth reading before anyone restores it.
    # This was CONTINUOUS / 1.0 on the reasoning that NWR is a continuous
    # automated broadcast. An operator reported from listening experience
    # that transmissions are finite with dead time between them, which is
    # an observation against an inference, so the inference loses.
    #
    # Re-checking the sources afterwards, the inference was weaker than it
    # looked: NWS documents a 24/7 service and a 3-6 minute programme
    # cycle, but nothing addresses TRANSMITTER KEYING. "The service runs
    # 24/7" is a claim about availability, not about carrier continuity.
    #
    # The distinction this tool can settle and a listener cannot:
    #   dead AUDIO, carrier up   -> interference duty is still 100%, since
    #                               a carrier desenses a front end whether
    #                               or not it is modulated
    #   dead CARRIER             -> genuinely below 100%
    # Both are silence from a speaker. See docs/nwr-measurement.md.
    "NWR": ServiceContinuity("NWR", "NOAA Weather Radio", UNKNOWN, None,
                             "UNCERTAIN -- retracted from 'continuous'; "
                             "carrier keying is not documented and this "
                             "survey is intended to settle it"),
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


# --------------------------------------------------------------------------- #
# Front-end filtering
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FrontEnd:
    """Filter state a band needs at the receiver's front end.

    FILTERS ARE PART OF THE MEASUREMENT, not an accessory, and they are
    per-band rather than per-session.

    A wideband front end near a city sees megawatt broadcast transmitters
    alongside the watt-level land mobile this survey is about. Two things
    then go wrong, and neither announces itself:

      COMPRESSION raises the noise floor across the whole passband. Every
      threshold here is noise-floor-relative, so occupancy reads LOW.

      INTERMODULATION puts third-order products on channels where nothing
      is transmitting. This is the worse of the two, because the phantoms
      appear exactly when the real transmitters key up -- so they correlate
      with the strong predicted emitters and make the validation plot look
      BETTER while being wrong. A thermal false alarm is random; an intermod
      false alarm is structured like real traffic.

    THE INTERACTION THAT BITES: the broadcast-FM notch must be ON while
    surveying land mobile and OFF while surveying the FM control band.
    Leave it on for the control and the control reads ~0%, the run is
    declared untrustworthy, and the fault is in the filter plan rather than
    anywhere the message points. That is why this is attached to the band.

    `external` names what the operator should fit ahead of the receiver.
    It is recorded, never actuated -- the tool cannot know what is in the
    coax, and a dataset that assumed a filter that was not fitted would be
    worse than one that admits it does not know.
    """
    rf_notch: bool = False          # broadcast FM / MW notch
    dab_notch: bool = False         # Band III DAB notch (174-240 MHz)
    external: str = ""              # what to fit ahead of the receiver
    reason: str = ""

    def as_metadata(self) -> dict:
        return {"rf_notch": self.rf_notch, "dab_notch": self.dab_notch,
                "external_filter_recommended": self.external,
                "reason": self.reason,
                "note": "rf_notch/dab_notch are actuated on receivers that "
                        "support them and recorded either way; the external "
                        "filter is RECORDED ONLY -- the tool cannot see what "
                        "is in the coax."}


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
      CONTROL -- a band whose carrier continuity follows from how the
                 transmission itself works. A control is the calibration
                 for the detection threshold: a control channel that must
                 read ~100% and does not means the threshold or the receive
                 path is wrong, and the target numbers from the same run
                 inherit the fault.

    ONLY BROADCAST FM IS A CONTROL, and the reason the list is short is
    worth keeping. NOAA Weather Radio was one until 2026-10-28, on the
    reasoning that a 24/7 automated service implies a continuous carrier.
    It does not: "the service runs 24/7" is a claim about availability, and
    carrier keying is a separate question that no source addressed. An
    operator's listening experience contradicted it and the row was
    retracted.

    Broadcast FM survives the same scrutiny where NWR did not, because the
    claim is about the modulation scheme rather than about a schedule: an
    FM transmitter radiates an unmodulated carrier during programme
    silence, so carrier presence follows from being on the air rather than
    from any assumption about content. That is still *architectural* and
    not measured -- and this survey measures it, so a control that reads
    well below 100% is also evidence about the control.

    THE GENERAL LESSON, since it cost a working calibration: an assumption
    used as a calibration is the most dangerous kind, because everything
    measured against it inherits its error silently. Prefer a control whose
    basis is a mechanism to one whose basis is a schedule.

    `expected_occupancy` is only set for controls. It is an assumption
    being used as a check, not a measurement.
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
    front_end: FrontEnd = field(default_factory=FrontEnd)
    targeted: bool = False
    # Absence of this band indicts the receiver rather than the band. A
    # weaker claim than a control's -- it says nothing about duty cycle --
    # and more robust for exactly that reason. See presence.py.
    presence: "PresenceReference" = None
    note: str = ""

    def __post_init__(self):
        if self.presence is None:
            from occupancy_survey.presence import PresenceReference
            object.__setattr__(self, "presence", PresenceReference())

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


def _nwr_presence():
    """NWR as a PRESENCE reference -- the surviving half of the retraction.

    The duty-cycle claim was withdrawn because carrier keying is not
    documented. This one does not need it: over a long enough observation
    NWR must appear, because the NWS publishes the transmitter list and
    coverage, so absence contradicts PUBLISHED DATA rather than an
    inference. It indicts the measuring end, not the band.
    """
    from occupancy_survey.presence import PresenceReference
    return PresenceReference(
        enabled=True,
        basis="NWS publishes the transmitter list and coverage, so an "
              "extended observation that sees nothing indicts the receive "
              "path. Says nothing about duty cycle -- that claim was "
              "retracted 2026-10-28 and is now a measurement target.")


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
        front_end=FrontEnd(
            rf_notch=True, dab_notch=True,
            external="150-174 MHz bandpass ahead of the receiver",
            reason="broadcast FM is 40-60 dB stronger than the land mobile "
                   "being measured and its intermod products land in this "
                   "band"),
        note="VHF business/public-safety pool, lower half. 12.5 kHz grid; "
             "the real plan has 7.5 and 6.25 kHz offsets in places.",
    ),
    "vhf_lmr_high": BandPlan(
        name="vhf_lmr_high",
        freq_min_hz=157_000_000.0, freq_max_hz=161_600_000.0,
        channel_step_hz=12_500.0, channel_bandwidth_hz=11_250.0,
        service="IG", role=TARGET,
        front_end=FrontEnd(
            rf_notch=True, dab_notch=True,
            external="150-174 MHz bandpass ahead of the receiver",
            reason="as vhf_lmr_low"),
        note="VHF business/public-safety pool, upper half.",
    ),
    "uhf_lmr": BandPlan(
        name="uhf_lmr",
        freq_min_hz=450_000_000.0, freq_max_hz=470_000_000.0,
        channel_step_hz=12_500.0, channel_bandwidth_hz=11_250.0,
        service="IG", role=TARGET,
        front_end=FrontEnd(
            rf_notch=True, dab_notch=True,
            external="450-470 MHz bandpass ahead of the receiver",
            reason="rejects broadcast FM, DAB and the 2x VHF products that "
                   "fall in UHF"),
        note="UHF business/public-safety pool. 12.5 kHz grid.",
    ),
    "nwr": BandPlan(
        name="nwr",
        freq_min_hz=162_400_000.0, freq_max_hz=162_550_000.0,
        channel_step_hz=25_000.0, channel_bandwidth_hz=16_000.0,
        service="NWR", role=TARGET, expected_occupancy=None,
        explicit_channels_hz=NWR_CHANNELS_HZ,
        presence=_nwr_presence(),
        front_end=FrontEnd(
            rf_notch=True, dab_notch=True,
            external="150-174 MHz bandpass (same as the VHF targets)",
            reason="shares a front end with the VHF targets, so its noise "
                   "floor is comparable with theirs"),
        note="MEASUREMENT TARGET, not a control -- demoted 2026-10-28. It "
             "was the detection-threshold reference on the assumption of a "
             "continuous carrier; that assumption is retracted and nothing "
             "establishes NWR transmitter keying either way. Do NOT anchor "
             "a threshold to it. It is instead a good FIRST target: one "
             "known frequency, strong from most QTHs, and the closest "
             "continuous-SERVICE allocation to 2 m in the catalogue "
             "(~14 MHz out), which the sota-rf risk model leans on. See "
             "docs/nwr-measurement.md.",
    ),
    "bcast_fm": BandPlan(
        name="bcast_fm",
        freq_min_hz=88_100_000.0, freq_max_hz=107_900_000.0,
        channel_step_hz=200_000.0, channel_bandwidth_hz=180_000.0,
        service="FM", role=CONTROL, expected_occupancy=1.0,
        front_end=FrontEnd(
            rf_notch=False, dab_notch=False,
            external="none, or a 88-108 MHz bandpass",
            reason="THE FM NOTCH MUST BE OFF HERE. It is on for every other "
                   "band in the schedule; leaving it on for this one makes "
                   "the control read ~0%, which declares the whole run "
                   "untrustworthy for a fault that is in the filter plan"),
        note="The schedule's only positive control. An FM transmitter "
             "radiates an unmodulated carrier through programme silence, so "
             "carrier presence follows from the modulation scheme rather "
             "than from a schedule -- which is why this survived the "
             "scrutiny that retired the NWR control. Still architectural, "
             "not measured: a strong local station reading well below 100% "
             "is evidence about this assumption too, not only about the "
             "receiver. Also the widest-dynamic-range band in the schedule, "
             "so it is where a fixed gain setting will clip first.",
    ),
}

DEFAULT_SCHEDULE = ("vhf_lmr_low", "nwr", "vhf_lmr_high", "uhf_lmr",
                    "bcast_fm")


def load_channel_list(path) -> list[BandPlan]:
    """Build targeted BandPlans from a tools/make_channel_list.py file.

    A targeted band keeps the role, service and expected occupancy of the
    grid band it came from -- so the controls stay controls -- but carries
    explicit FCC frequencies instead of a grid. Two consequences:

      - the grid-offset problem disappears, since FCC frequencies are exact
        and nothing is split across two channels;
      - the channels are SPARSE, so schedule.tune_steps() covers them
        rather than sweeping the span they straddle, which is where the
        airtime saving comes from.

    It also inherits the band's front-end filter plan, which matters: a
    targeted VHF band measured without the FM notch is no better off than a
    grid one.
    """
    import json
    from pathlib import Path as _Path

    data = json.loads(_Path(path).read_text(encoding="utf-8"))
    if not str(data.get("schema", "")).startswith(
            "occupancy-survey/channel-list"):
        raise ValueError(
            f"{path}: schema '{data.get('schema')}' is not a channel list "
            f"from tools/make_channel_list.py")

    plans = []
    for name, entry in sorted(data.get("bands", {}).items()):
        freqs = tuple(float(c["freq_hz"]) for c in entry["channels"])
        if not freqs:
            continue
        base = BAND_PLANS.get(name)
        plans.append(BandPlan(
            name=name,
            freq_min_hz=min(freqs) - 0.5 * entry["channel_bandwidth_hz"],
            freq_max_hz=max(freqs) + 0.5 * entry["channel_bandwidth_hz"],
            channel_step_hz=(base.channel_step_hz if base else 12_500.0),
            channel_bandwidth_hz=float(entry["channel_bandwidth_hz"]),
            service=entry.get("service", "IG"),
            role=entry.get("role", TARGET),
            expected_occupancy=entry.get("expected_occupancy"),
            explicit_channels_hz=freqs,
            front_end=(base.front_end if base else FrontEnd()),
            targeted=True,
            note=f"targeted: {len(freqs)} FCC-derived channels from "
                 f"{path}. A targeted list CANNOT find a busy channel with "
                 f"no FCC record; keep a grid band alongside it for that.",
        ))
    if not plans:
        raise ValueError(f"{path}: no bands with channels")
    return plans


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
