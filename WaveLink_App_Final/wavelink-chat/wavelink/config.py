"""Physical-layer and radio configuration shared by every station.

All stations in a network MUST use the same PhyConfig (sample rate, chip
oversampling, spreading factor, roll-off, preamble length, carrier).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class PhyConfig:
    # --- RF ---------------------------------------------------------------
    center_freq: float = 2.5e9        # Hz  (course requirement: 2.5 GHz)
    samp_rate: float = 4e6            # complex samples / s at the SDR
    rf_bandwidth: float = 4e6         # analog filter bandwidth of the AD936x
    tx_atten_db: float = 10.0         # ANTSDR TX attenuation (0 = max power)
    rx_gain_db: float = 40.0          # ANTSDR RX gain when gain_mode == manual
    rx_gain_mode: str = "manual"      # manual | slow_attack | fast_attack

    # --- DS-CDMA ------------------------------------------------------------
    sps: int = 4                      # samples per chip
    gold_degree: int = 5              # code degree n: 2^n-chip Walsh codes (32) or 2^n-1-chip Gold codes (31)
    codes: str = "walsh"              # walsh: Walsh rows x one scrambling sequence, exactly orthogonal
                                      # when sent together (a TD-CDMA slot); gold: Gold codes, for
                                      # bursts that arrive at any code phase (spread ALOHA)
    rolloff: float = 0.35             # RRC excess bandwidth
    rrc_span: int = 8                 # RRC filter span in chips (each side = span/2)
    preamble_bits: int = 48           # known, code-specific preamble symbols
    det_threshold: float = 0.60       # normalised differential-correlation threshold (0 false
                                      # alarms in 3 s of noise; detects bursts down to ~4 dB Es/N0)
    amplitude: float = 0.5            # one stream has RMS = amplitude/2 (headroom for QAM + overlaps)

    # --- framing -------------------------------------------------------------
    max_phy_payload: int = 512        # bytes carried by one PHY burst

    # ---------------------------------------------------------------------------
    def __post_init__(self):
        if self.codes not in CODE_FAMILIES:
            raise ValueError(f"codes must be one of {', '.join(CODE_FAMILIES)}, not {self.codes!r}")

    @property
    def sf(self) -> int:
        """Spreading factor (chips per symbol): 2^n Walsh, 2^n - 1 Gold."""
        return (1 << self.gold_degree) - (0 if self.codes == "walsh" else 1)

    @property
    def chip_rate(self) -> float:
        return self.samp_rate / self.sps

    @property
    def symbol_rate(self) -> float:
        return self.chip_rate / self.sf

    @property
    def samples_per_symbol(self) -> int:
        return self.sf * self.sps

    @property
    def num_codes(self) -> int:
        return (1 << self.gold_degree) + (0 if self.codes == "walsh" else 1)

    @property
    def max_node_id(self) -> int:
        """Code 0 is the broadcast code, so node ids are 1 .. num_codes-1."""
        return min(MAX_NODE_ID, self.num_codes - 1)

    profile: str = "robust"

    @classmethod
    def from_profile(cls, name: str = "robust", **overrides) -> "PhyConfig":
        if name not in PROFILES:
            raise ValueError(f"unknown profile {name!r}; choose from {', '.join(PROFILES)}")
        kw = dict(PROFILES[name]["phy"])
        kw.update({k: v for k, v in overrides.items() if v is not None})
        return cls(profile=name, **kw)

    def summary(self) -> dict:
        d = asdict(self)
        d.update(sf=self.sf, chip_rate=self.chip_rate, bit_rate=self.symbol_rate,
                 processing_gain_db=round(10 * __import__("math").log10(self.sf), 2),
                 max_node_id=self.max_node_id)
        return d


# Speed profiles.  Every station in a network must use the same profile (and code family).
# Uncoded rates with 32-chip Walsh codes (31-chip Gold codes: 3 % more):
#   robust: 1 Mchip/s (4 samples/chip): BPSK 31 / QPSK 62 / 16-QAM 125 / 64-QAM 188 kb/s
#   fast:   2 Mchip/s (2 samples/chip): BPSK 62 / QPSK 125 / 16-QAM 250 / 64-QAM 375 kb/s
# The payload modulation and LDPC code rate are chosen per frame by the link layer.
PROFILES = {
    "robust": dict(phy=dict(samp_rate=4e6, sps=4, gold_degree=5, max_phy_payload=512)),
    "fast":   dict(phy=dict(samp_rate=4e6, sps=2, gold_degree=5, max_phy_payload=1024)),
}

CODE_FAMILIES = ("walsh", "gold")
BROADCAST_ADDR = 0xFF
BROADCAST_CODE = 0          # code index used for broadcast / beacons
MAX_NODE_ID = 30            # node id N uses code index N (1..30)


def code_index_for(addr: int) -> int:
    """Receiver-based code assignment: a frame is spread with the code of the
    station it is addressed to.  Broadcasts use the common code 0."""
    if addr == BROADCAST_ADDR:
        return BROADCAST_CODE
    if not 1 <= addr <= MAX_NODE_ID:
        raise ValueError(f"node address must be 1..{MAX_NODE_ID}, got {addr}")
    return addr
