"""The SDRplay receivers this tool drives, and how they differ.

Every model here goes through the same gr-sdrplay3 interface and the same
SDRplay API, so the gain-reduction convention, the gain readback and the
overload reports are common to all of them. What differs is the hardware
around the tuner: whether there is an antenna switch, an HDR mode, and which
broadcast notches exist. The differences are checked against gr-sdrplay3's
own Python classes (3.11.0.8) -- rsp1a and rsp1b have no set_antenna,
get_antennas or set_hdr_mode -- not against a datasheet.

WHAT DOES NOT TRANSFER BETWEEN MODELS, or between two units of one model:
the per-band RF reduction that keeps FM from clipping, the receiver spur
list, and the size of an RF step (an RSPdx read back 12 dB for a UHF step
that moved real signals 8.3). Those are measurements of one receiver. A new
one needs its own --intermod-check and an open-port spur check before its
data is pooled with another's.

Left out on purpose: the RSP1 has no FM notch at all, so the land-mobile
bands would be measured with the broadcast band hitting the front end --
the overload-and-intermod case this project treats as its dominant error.
The RSP2 and RSPduo are reachable through the same interface but have their
own port and tuner semantics, and none has been run here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SdrplayModel:
    device_type: str
    block: str                         # the gr-sdrplay3 class
    name: str
    antenna_ports: tuple = ()          # () = one input, no switch
    hdr: bool = False
    notches: tuple = ("rf_notch", "dab_notch")

    @property
    def has_antenna_switch(self) -> bool:
        return bool(self.antenna_ports)


_DX_PORTS = ("Antenna A", "Antenna B", "Antenna C")

SDRPLAY_MODELS = {m.device_type: m for m in (
    SdrplayModel("rspdx", "rspdx", "RSPdx", _DX_PORTS, hdr=True),
    SdrplayModel("rspdxr2", "rspdxr2", "RSPdx-R2", _DX_PORTS, hdr=True),
    SdrplayModel("rsp1a", "rsp1a", "RSP1A"),
    SdrplayModel("rsp1b", "rsp1b", "RSP1B"),
)}


def sdrplay_model(device_type) -> SdrplayModel | None:
    """The model for a config's device_type, or None if it is not SDRplay."""
    return SDRPLAY_MODELS.get(str(device_type or "").lower().strip())
