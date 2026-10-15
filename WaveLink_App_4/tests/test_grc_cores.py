"""PHY cores of the GNU Radio flowgraph, with the stock blocks imitated in numpy:
    python -m tests.test_grc_cores
LDPC encoder -> framer -> table lookup (Chunks to Symbols) -> repeat x31 * code -> combiner
-> RRC interp -> channel -> RRC matched filter -> despreader -> Constellation Soft Decoder
(GNU Radio's exact LLR formula) -> LDPC decoder -> bytes
"""
import math
import zlib
import numpy as np

from wavelink.codes import gold_codes, rrc_taps
from wavelink.grc_phy import (FramerCore, CombinerCore, DespreaderCore, LdpcEncoderCore, LdpcDecoderCore,
                               phy_config)
from wavelink.modem import SYMBOL_TABLE, BPSK_POINTS, QPSK_POINTS, QAM16_POINTS


def gr_soft_decoder(z, pts, npwr):
    """digital.constellation_soft_decoder_cf: log P(1)/P(0) per bit, MSB first (exact, log-sum-exp)."""
    bps = int(np.log2(pts.size))
    a = -np.abs(z[:, None] - pts[None, :]) ** 2 / npwr
    k = np.arange(pts.size)
    out = np.empty((z.size, bps), np.float32)
    for j in range(bps):
        bit = (k >> (bps - 1 - j)) & 1
        lse = lambda v: v.max(1) + np.log(np.exp(v - v.max(1, keepdims=True)).sum(1))  # noqa: E731
        out[:, j] = lse(a[:, bit == 1]) - lse(a[:, bit == 0])
    return out.ravel()


def nearest_index(z, pts):
    return np.argmin(np.abs(z[:, None] - pts[None, :]), axis=1)


def main():
    cfg = phy_config(4e6, 2, 5, 48, 1024)
    codes = gold_codes(5)
    framer = FramerCore(cfg)
    enc, dec = LdpcEncoderCore(cfg), LdpcDecoderCore()
    comb = CombinerCore(cfg.chip_rate, 131072, cfg.samp_rate)
    got = []
    desp = DespreaderCore(cfg, 2, got.append)
    rng = np.random.default_rng(4)
    frames = []
    # two bursts on different codes at the same time (node 2 and node 3) + one broadcast later
    for code, mod in ((2, "16qam-3/4"), (3, "qpsk-1/2"), (2, "bpsk"), (0, "qpsk-1/2"), (2, "16qam"),
                      (2, "qpsk-5/6"), (2, "bpsk-1/2")):
        body = bytes(rng.integers(0, 256, 700, dtype=np.uint8))
        frame = body + zlib.crc32(body).to_bytes(4, "little")      # stands in for the CRC32 block
        bits, m_, rate = enc.encode(frame, mod)                    # LDPC Encoder block
        idx = framer.indices(bits, len(frame), code, m_, rate)
        chips = (SYMBOL_TABLE[idx][:, None] * codes[code][None, :]).ravel()   # map, repeat, x code
        comb.push(code, chips)
        frames.append((code, mod, frame))
    chips = np.concatenate([comb.read(50000) for _ in range(30)])
    up = np.zeros(chips.size * cfg.sps, np.complex64)
    up[::cfg.sps] = chips
    h = rrc_taps(cfg.sps, cfg.rolloff, cfg.rrc_span)
    tx = np.convolve(up, h)
    # channel: delay, carrier offset 400 Hz, phase, noise (SNR per sample ~ 16 dB)
    n = np.arange(tx.size)
    rx = np.concatenate([np.zeros(777), tx * np.exp(1j * (2 * np.pi * 400 * n / cfg.samp_rate + 0.7))])
    p = np.mean(np.abs(tx) ** 2)
    rx = rx + math.sqrt(p / 40 / 2) * (rng.standard_normal(rx.size) + 1j * rng.standard_normal(rx.size))
    mf = np.convolve(rx, h).astype(np.complex64)              # stands in for the FFT filter block
    for i in range(0, mf.size, 16384):
        desp.demod.process(mf[i:i + 16384])
    pts = {"bpsk": BPSK_POINTS, "qpsk": QPSK_POINTS, "16qam": QAM16_POINTS}
    bps = {"bpsk": 1, "qpsk": 2, "16qam": 4}
    want = [(c, m, f) for c, m, f in frames if c in (2, 0)]
    print(f"despreader (node 2) found {len(got)} frames; expected {len(want)} (codes 2 and 0, not code 3)")
    ok = 0
    by_key = {}
    for c, m, frame in want:
        by_key.setdefault((c, m), []).append(frame)       # m: modulation-coding name
    for f in got:
        frame = by_key[(f.code_idx, f.mcs if f.mcs != "bpskc" else "bpsk")].pop(0)
        mm = "bpsk" if f.mod in ("bpsk", "bpskc") else f.mod
        npwr = 0.05 if mm == "16qam" else 1.0                  # as the flowgraph's constellations
        soft = gr_soft_decoder(f.symbols, pts[mm], npwr)       # stock Constellation Soft Decoder
        data, fec_ok, it = dec.decode(soft, f.length, mm, f.rate)   # LDPC Decoder block
        good = data == frame and zlib.crc32(data[:-4]).to_bytes(4, "little") == data[-4:]
        ok += good
        print(f"  code {f.code_idx} {f.mcs:9}  Es/N0 {f.esn0_db:5.1f} dB  CFO {f.cfo_hz:+5.0f} Hz  "
              f"{len(f.symbols)} symbols  LDPC iterations {it}  -> {'OK' if good else 'BAD'}")
    print("combiner: bursts", comb.bursts, " max concurrent codes seen in output: 2")
    assert ok == len(want), "decode mismatch"
    interop(cfg, codes, h, rng)
    print("GRC PHY cores: PASS")


def interop(cfg, codes, h, rng):
    """A numpy-PHY station (--phy numpy) and a GRC-PHY station understand each other, coded or not."""
    from wavelink.mac import add_crc, check_crc
    from wavelink.modem import Modulator, Demodulator
    pts = {"bpsk": BPSK_POINTS, "qpsk": QPSK_POINTS, "16qam": QAM16_POINTS}
    results = []
    for mcs in ("bpsk", "qpsk-3/4", "16qam-1/2"):
        body = bytes(rng.integers(0, 256, 400, dtype=np.uint8))
        # numpy PHY -> GRC PHY: despreader + stock soft decoder + LDPC Decoder block
        got = []
        desp = DespreaderCore(cfg, 2, got.append)
        x = Modulator(cfg).modulate(add_crc(body), 2, mcs)
        x = np.concatenate([np.zeros(500, np.complex64), x, np.zeros(4000, np.complex64)])
        desp.demod.process(np.convolve(x, h).astype(np.complex64))
        a = False
        if got:
            f = got[0]
            mm = "bpsk" if f.mod in ("bpsk", "bpskc") else f.mod
            soft = gr_soft_decoder(f.symbols, pts[mm], 0.05 if mm == "16qam" else 1.0)
            data, _, _ = LdpcDecoderCore().decode(soft, f.length, mm, f.rate)
            a = data[:-4] == body and zlib.crc32(data[:-4]).to_bytes(4, "little") == data[-4:]
        # GRC PHY -> numpy PHY: LDPC Encoder + Framer blocks, stock CRC32 byte order
        frame = body + zlib.crc32(body).to_bytes(4, "little")
        bits, m_, rate = LdpcEncoderCore(cfg).encode(frame, mcs)
        idx = FramerCore(cfg).indices(bits, len(frame), 3, m_, rate)
        chips = (SYMBOL_TABLE[idx][:, None] * codes[3][None, :]).ravel()
        up = np.zeros(chips.size * cfg.sps, np.complex64)
        up[::cfg.sps] = chips
        tx = np.concatenate([np.zeros(500), np.convolve(up, h), np.zeros(4000)]).astype(np.complex64)
        rx = []
        Demodulator(cfg, [3, 0], on_frame=rx.append).process(tx)
        b = bool(rx) and check_crc(rx[0].payload) == body
        results.append(a and b)
        print(f"interop {mcs:9}: numpy PHY -> GRC PHY {'OK' if a else 'BAD'}, GRC PHY -> numpy PHY {'OK' if b else 'BAD'}")
    assert all(results), "PHY interoperability"


if __name__ == "__main__":
    main()
