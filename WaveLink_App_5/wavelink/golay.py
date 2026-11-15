"""Extended Golay (24,12) code for the PHY header, with soft maximum-likelihood decoding.

The 24 header bits (length, modulation, code rate, CRC-8) are sent as two Golay codewords,
48 coherent BPSK symbols.  Minimum distance 8; decoding correlates the received soft values
with all 4,096 codewords and keeps the best, which is cheap at this size and gives the full
soft-decision gain (about 5 dB at the error rates that matter), so the header is never the
weak point of an LDPC-coded frame.
"""
from __future__ import annotations

import numpy as np

_G = 0b110001110101            # g(x) = x^11 + x^10 + x^6 + x^5 + x^4 + x^2 + 1  (cyclic (23,12) Golay)


def _encode_int(m: int) -> int:
    c = 0
    for i in range(12):
        if m >> i & 1:
            c ^= _G << i
    c &= (1 << 23) - 1
    return (c << 1) | (bin(c).count("1") & 1)       # extend with overall parity -> 24 bits


CODEWORDS = np.array([_encode_int(m) for m in range(4096)], dtype=np.int64)
_BITS = ((CODEWORDS[:, None] >> np.arange(23, -1, -1)[None, :]) & 1).astype(np.int8)   # (4096, 24)
_PM = (1.0 - 2.0 * _BITS).astype(np.float32)                                          # bit 0 -> +1


def encode(bits12: np.ndarray) -> np.ndarray:
    m = int((bits12.astype(np.int64) << np.arange(11, -1, -1)).sum())
    return _BITS[m].copy()


def decode(soft24: np.ndarray) -> tuple[np.ndarray, float]:
    """soft24: received values, positive = bit 0.  Returns (12 message bits, reliability 0..1)."""
    corr = _PM @ np.asarray(soft24, dtype=np.float32)
    m = int(np.argmax(corr))
    rel = float(corr[m] / (np.abs(soft24).sum() + 1e-30))
    return ((m >> np.arange(11, -1, -1)) & 1).astype(np.int8), rel


def min_distance() -> int:
    return int(_BITS[1:].sum(axis=1).min())
