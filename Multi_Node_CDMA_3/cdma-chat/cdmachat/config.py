"""Physical-layer and radio configuration shared by every station.

All stations in a network MUST use the same PhyConfig (sample rate, chip
oversampling, spreading factor, roll-off, preamble length, carrier).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class PhyConfig:
    # --- RF ---------------------------------------------------------------
    center_freq: float = 2.5e9        # Hz  (course requirement: 2.5 GHz)
    samp_rate: float = 4e6            # complex samples / s at the SDR
    rf_bandwidth: float = 4e6         # analog filter bandwidth of the AD936x
    tx_atten_db: float = 10.0         # ANTSDR TX attenuation (0 = max power)
    rx_gain_db: float = 40.0          # ANTSDR RX gain when gain_mode == manual
    rx_gain_mode: str = "manual"      # manual | slow_attack | fast_attack

    # --- DS-CDMA / BPSK ----------------------------------------------------
    sps: int = 4                      # samples per chip
    gold_degree: int = 5              # 5 -> 31-chip Gold codes (33 codes)
    rolloff: float = 0.35             # RRC excess bandwidth
    rrc_span: int = 8                 # RRC filter span in chips (each side = span/2)
    preamble_bits: int = 48           # known, code-specific preamble symbols
    det_threshold: float = 0.80       # normalised differential-correlation threshold
    amplitude: float = 0.7            # peak-ish baseband amplitude of one stream

    # --- framing -------------------------------------------------------------
    max_phy_payload: int = 255        # bytes carried by one PHY burst

    # ---------------------------------------------------------------------------
    @property
    def sf(self) -> int:
        """Spreading factor (chips per bit)."""
        return (1 << self.gold_degree) - 1

    @property
    def chip_rate(self) -> float:
        return self.samp_rate / self.sps

    @property
    def symbol_rate(self) -> float:
        return self.chip_rate / self.sf

    @property
    def samples_per_symbol(self) -> int:
        return self.sf * self.sps

    def summary(self) -> dict:
        d = asdict(self)
        d.update(sf=self.sf, chip_rate=self.chip_rate, bit_rate=self.symbol_rate,
                 processing_gain_db=round(10 * __import__("math").log10(self.sf), 2))
        return d


BROADCAST_ADDR = 0xFF
BROADCAST_CODE = 0          # Gold-code index used for broadcast / beacons
MAX_NODE_ID = 30            # node id N uses Gold code index N (1..30)


def code_index_for(addr: int) -> int:
    """Receiver-based code assignment: a frame is spread with the code of the
    station it is addressed to.  Broadcasts use the common code 0."""
    if addr == BROADCAST_ADDR:
        return BROADCAST_CODE
    if not 1 <= addr <= MAX_NODE_ID:
        raise ValueError(f"node address must be 1..{MAX_NODE_ID}, got {addr}")
    return addr
