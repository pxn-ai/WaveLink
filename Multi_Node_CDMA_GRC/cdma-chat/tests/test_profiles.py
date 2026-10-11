"""Speed-profile tests:  python -m tests.test_profiles"""
import math
import time
import numpy as np

from cdmachat.config import PhyConfig
from cdmachat.modem import Modulator, Demodulator
from tests.test_modem import channel, noise_for


def decode_cost(cfg, mod, m="bpsk"):
    from cdmachat.modem import frame_symbols
    x = mod.modulate(bytes(cfg.max_phy_payload), 3, m)
    x = np.concatenate([np.zeros(5000, np.complex64), x, np.zeros(5000, np.complex64)])
    d = Demodulator(cfg, [3])
    d.buf = np.convolve(x, d.h, "same").astype(np.complex64)
    nsym = frame_symbols(cfg, cfg.max_phy_payload, m)
    t = time.perf_counter()
    for _ in range(5):
        d._demod(5000, 0.0, 3, nsym, m)
    return (time.perf_counter() - t) / 5, mod.burst_duration(cfg.max_phy_payload, m)


def sweep(cfg, mod, rng, ebn0s=(8, 10, 12), trials=8, m="bpsk"):
    res = []
    for eb in ebn0s:
        ok = 0
        for _ in range(trials):
            p = bytes(rng.integers(0, 256, cfg.max_phy_payload, dtype=np.uint8))
            x = mod.modulate(p, 3, m)
            rms = math.sqrt(np.mean(np.abs(x) ** 2))
            b = channel(x, cfg.samp_rate, cfo=rng.uniform(-12e3, 12e3), phase=rng.uniform(0, 6),
                        delay=int(rng.integers(100, 3000)), sco_ppm=20)
            b = np.concatenate([b, np.zeros(3000)])
            b = b + noise_for(cfg, eb, rms) / math.sqrt(2) * (rng.standard_normal(b.size) + 1j * rng.standard_normal(b.size))
            got = []
            d = Demodulator(cfg, [3, 0], on_frame=got.append)
            for i in range(0, b.size, 40000):
                d.process(b[i:i + 40000].astype(np.complex64))
            ok += any(g.payload == p for g in got)
        res.append(f"{eb} dB: {ok}/{trials}")
    return ", ".join(res)


def two_links(cfg, mod, rng, trials=6, m="bpsk"):
    ok = 0
    for _ in range(trials):
        pa = bytes(rng.integers(0, 256, cfg.max_phy_payload, dtype=np.uint8))
        pb = bytes(rng.integers(0, 256, cfg.max_phy_payload, dtype=np.uint8))
        xa, xb = mod.modulate(pa, 1, m), mod.modulate(pb, 2, m)
        rms = math.sqrt(np.mean(np.abs(xa) ** 2))
        a = channel(xa, cfg.samp_rate, cfo=2000, delay=500)
        b = channel(xb, cfg.samp_rate, cfo=-3000, delay=500 + int(rng.integers(10, 3000)))
        n = max(a.size, b.size) + 2000
        y = np.zeros(n, complex); y[:a.size] += a; y[:b.size] += b
        y += noise_for(cfg, 20, rms) / math.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        got = []
        d = Demodulator(cfg, [1, 0], on_frame=got.append)
        for i in range(0, n, 40000):
            d.process(y[i:i + 40000].astype(np.complex64))
        ok += any(g.payload == pa for g in got)
    return f"{ok}/{trials}"


def realtime(cfg, mod, rng, seconds=2.0, m="bpsk", es=18):
    """Back-to-back max-size frames for `seconds`: can one receiver keep up?"""
    frames, total = [], 0
    while total < seconds * cfg.samp_rate:
        p = bytes(rng.integers(0, 256, cfg.max_phy_payload, dtype=np.uint8))
        x = mod.modulate(p, 3, m)
        frames.append((p, x)); total += x.size
    sig = np.concatenate([x for _, x in frames])
    sig = channel(sig, cfg.samp_rate, cfo=4000, delay=777)
    rms = math.sqrt(np.mean(np.abs(frames[0][1]) ** 2))
    sig = (sig + noise_for(cfg, es, rms) / math.sqrt(2) * (rng.standard_normal(sig.size) + 1j * rng.standard_normal(sig.size))).astype(np.complex64)
    got = []
    d = Demodulator(cfg, [3, 0], on_frame=got.append)
    t = time.perf_counter()
    for i in range(0, sig.size, 32768):
        d.process(sig[i:i + 32768])
    cpu = time.perf_counter() - t
    ok = len({g.payload for g in got} & {p for p, _ in frames})
    air = sig.size / cfg.samp_rate
    return f"{ok}/{len(frames)} frames, {air:.1f} s of air decoded in {cpu:.1f} s CPU ({air/cpu:.1f}x real time)"


if __name__ == "__main__":
    # noise_for() sets the per-symbol SNR (Es/N0) here: same symbol energy for every modulation
    for prof in ("robust", "fast"):
        cfg = PhyConfig.from_profile(prof)
        mod = Modulator(cfg)
        for m, eb in (("bpsk", (10, 12, 14)), ("qpsk", (12, 14, 16)), ("16qam", (18, 20, 22))):
            rng = np.random.default_rng(3)
            dc, air = decode_cost(cfg, mod, m)
            bps = {"bpsk": 1, "qpsk": 2, "16qam": 4}[m]
            print(f"[{prof} {m}] {cfg.symbol_rate * bps / 1e3:.0f} kb/s, {cfg.max_phy_payload}-byte frames "
                  f"({air*1e3:.0f} ms on air), decode {dc*1e3:.1f} ms")
            print(f"   Es/N0 sweep (CFO +/-12 kHz, 20 ppm clock): {sweep(cfg, mod, rng, eb, 6, m)}")
            print(f"   with a 2nd equal-power link on another code (Es/N0 20 dB): {two_links(cfg, mod, rng, 4, m)}")
            print(f"   continuous traffic (Es/N0 {26 if m == \"16qam\" else 18} dB): {realtime(cfg, mod, rng, 1.5, m, 26 if m == \"16qam\" else 18)}")
