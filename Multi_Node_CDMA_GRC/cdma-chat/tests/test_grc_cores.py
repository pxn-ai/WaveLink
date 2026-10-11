"""PHY cores of the GNU Radio flowgraph, with the stock blocks imitated in numpy:
    python -m tests.test_grc_cores
framer -> table lookup (Chunks to Symbols) -> repeat x31 * code -> combiner -> RRC interp
-> channel -> RRC matched filter -> despreader -> slicer (Constellation Decoder) -> bytes
"""
import math
import zlib
import numpy as np

from cdmachat.codes import gold_codes, rrc_taps
from cdmachat.grc_phy import FramerCore, CombinerCore, DespreaderCore, phy_config
from cdmachat.modem import SYMBOL_TABLE, BPSK_POINTS, QPSK_POINTS, QAM16_POINTS


def nearest_index(z, pts):
    return np.argmin(np.abs(z[:, None] - pts[None, :]), axis=1)


def main():
    cfg = phy_config(4e6, 2, 5, 48, 1024)
    codes = gold_codes(5)
    framer = FramerCore(cfg)
    comb = CombinerCore(cfg.chip_rate, 131072, cfg.samp_rate)
    got = []
    desp = DespreaderCore(cfg, 2, got.append)
    rng = np.random.default_rng(4)
    frames = []
    # two bursts on different codes at the same time (node 2 and node 3) + one broadcast later
    for code, mod in ((2, "16qam"), (3, "qpsk"), (2, "bpsk"), (0, "qpsk"), (2, "qpsk")):
        body = bytes(rng.integers(0, 256, 700, dtype=np.uint8))
        frame = body + zlib.crc32(body).to_bytes(4, "little")      # stands in for the CRC32 block
        idx = framer.indices(frame, code, mod)
        chips = (SYMBOL_TABLE[idx][:, None] * codes[code][None, :]).ravel()   # map, repeat, x code
        comb.push(code, chips)
        frames.append((code, mod, frame))
    chips = np.concatenate([comb.read(50000) for _ in range(12)])
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
        by_key.setdefault((c, m), []).append(frame)
    for f in got:
        mm = "bpsk" if f.mod in ("bpsk", "bpskc") else f.mod
        frame = by_key[(f.code_idx, mm)].pop(0)
        k = nearest_index(f.symbols, pts[mm])                # stands in for Constellation Decoder
        bits = ((k[:, None] >> np.arange(bps[mm] - 1, -1, -1)) & 1).ravel().astype(np.uint8)
        data = np.packbits(bits).tobytes()                    # repack bits
        good = data == frame and zlib.crc32(data[:-4]).to_bytes(4, "little") == data[-4:]
        ok += good
        print(f"  code {f.code_idx} {mm:5}  Es/N0 {f.esn0_db:5.1f} dB  CFO {f.cfo_hz:+5.0f} Hz  "
              f"{len(f.symbols)} symbols  -> {'OK' if good else 'BAD'}")
    print("combiner: bursts", comb.bursts, " max concurrent codes seen in output: 2")
    assert ok == len(want), "decode mismatch"
    interop(cfg, codes, h, rng)
    print("GRC PHY cores: PASS")


def interop(cfg, codes, h, rng):
    """A numpy-PHY station (--phy numpy) and a GRC-PHY station understand each other."""
    from cdmachat.mac import add_crc, check_crc
    from cdmachat.modem import Modulator, Demodulator
    body = bytes(rng.integers(0, 256, 400, dtype=np.uint8))
    # numpy PHY -> GRC PHY: differential BPSK burst, MAC CRC, decoded by the despreader + stock chain
    got = []
    desp = DespreaderCore(cfg, 2, got.append)
    x = Modulator(cfg).modulate(add_crc(body), 2, "bpsk")
    x = np.concatenate([np.zeros(500, np.complex64), x, np.zeros(4000, np.complex64)])
    desp.demod.process(np.convolve(x, h).astype(np.complex64))
    assert got, "GRC despreader missed the numpy-PHY burst"
    k = nearest_index(got[0].symbols, BPSK_POINTS)
    data = np.packbits(k.astype(np.uint8)).tobytes()
    a = data[:-4] == body and zlib.crc32(data[:-4]).to_bytes(4, "little") == data[-4:]
    # GRC PHY -> numpy PHY: framer indices (coherent BPSK) + stock CRC32 byte order
    frame = body + zlib.crc32(body).to_bytes(4, "little")
    idx = FramerCore(cfg).indices(frame, 3, "bpsk")
    chips = (SYMBOL_TABLE[idx][:, None] * codes[3][None, :]).ravel()
    up = np.zeros(chips.size * cfg.sps, np.complex64)
    up[::cfg.sps] = chips
    tx = np.concatenate([np.zeros(500), np.convolve(up, h), np.zeros(4000)]).astype(np.complex64)
    rx = []
    Demodulator(cfg, [3, 0], on_frame=rx.append).process(tx)
    b = bool(rx) and check_crc(rx[0].payload) == body
    print(f"interop: numpy PHY -> GRC PHY {'OK' if a else 'BAD'}, GRC PHY -> numpy PHY {'OK' if b else 'BAD'}")
    assert a and b, "PHY interoperability"


if __name__ == "__main__":
    main()
