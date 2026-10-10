"""Modem tests: run with  python -m tests.test_modem  (prints a small report)."""
import math
import time
import numpy as np

from cdmachat.config import PhyConfig
from cdmachat.modem import Modulator, Demodulator


def channel(x, fs, cfo=0.0, phase=0.0, delay=0, sco_ppm=0.0, gain=1.0):
    if sco_ppm:
        n = np.arange(int(x.size / (1 + sco_ppm * 1e-6)))
        t = n * (1 + sco_ppm * 1e-6)
        x = (np.interp(t, np.arange(x.size), x.real) + 1j * np.interp(t, np.arange(x.size), x.imag))
    n = np.arange(x.size)
    x = gain * x * np.exp(1j * (2 * np.pi * cfo * n / fs + phase))
    return np.concatenate([np.zeros(delay, complex), x])


def noise_for(cfg, ebn0_db, sig_rms):
    """Noise std per complex sample for a target Eb/N0 of a stream with given RMS."""
    ps = sig_rms ** 2
    ecn0 = 10 ** (ebn0_db / 10) / cfg.sf
    n0 = ps * cfg.sps / ecn0            # chip energy = ps * sps
    return math.sqrt(n0)


def run(cfg, bursts, total_len, n_std, codes, chunk=None, rng=None):
    rng = rng or np.random.default_rng(1)
    y = np.zeros(total_len, complex)
    for b in bursts:
        y[: b.size] += b[: total_len]
    y += n_std / math.sqrt(2) * (rng.standard_normal(total_len) + 1j * rng.standard_normal(total_len))
    got = []
    dem = Demodulator(cfg, codes, on_frame=got.append)
    chunk = chunk or 40000
    i = 0
    while i < total_len:
        c = chunk if isinstance(chunk, int) else int(rng.integers(500, 60000))
        dem.process(y[i:i + c].astype(np.complex64))
        i += c
    return got, dem


def test_clean_and_impairments():
    cfg = PhyConfig()
    mod = Modulator(cfg)
    rng = np.random.default_rng(7)
    payload = bytes(rng.integers(0, 256, 200, dtype=np.uint8))
    x = mod.modulate(payload, 3)
    sig_rms = math.sqrt(np.mean(np.abs(x) ** 2))
    b = channel(x, cfg.samp_rate, cfo=9000, phase=1.0, delay=12345, sco_ppm=20)
    got, dem = run(cfg, [b], b.size + 20000, noise_for(cfg, 12, sig_rms), [3, 0], chunk="rand", rng=rng)
    assert len(got) == 1 and got[0].payload == payload, (got, dem.stats)
    f = got[0]
    print(f"  impaired decode ok: Eb/N0 est {f.snr_db:.1f} dB, CFO est {f.cfo_hz:.0f} Hz (true 9000)")


def sweep():
    cfg = PhyConfig()
    mod = Modulator(cfg)
    rng = np.random.default_rng(11)
    print("  Eb/N0 sweep (200-byte frames, CFO +/-12 kHz, SCO 20 ppm, random delay)")
    for ebn0 in (4, 6, 8, 10):
        ok = 0
        trials = 12
        for t in range(trials):
            payload = bytes(rng.integers(0, 256, 200, dtype=np.uint8))
            x = mod.modulate(payload, 5)
            rms = math.sqrt(np.mean(np.abs(x) ** 2))
            b = channel(x, cfg.samp_rate, cfo=rng.uniform(-12e3, 12e3), phase=rng.uniform(0, 6.28),
                        delay=int(rng.integers(0, 5000)), sco_ppm=20)
            got, _ = run(cfg, [b], b.size + 2000, noise_for(cfg, ebn0, rms), [5], rng=rng)
            ok += any(g.payload == payload for g in got)
        print(f"    Eb/N0 {ebn0:>2} dB (chip SNR {ebn0 - 10*math.log10(cfg.sf):5.1f} dB): {ok}/{trials} frames")


def false_alarms():
    cfg = PhyConfig()
    rng = np.random.default_rng(3)
    n = int(cfg.samp_rate * 2)
    got, dem = run(cfg, [], n, 1.0, [1, 0], rng=rng)
    print(f"  noise only, 2 s, 2 codes: detections={dem.stats['detections']} frames={len(got)}")
    assert len(got) == 0


def multiuser():
    cfg = PhyConfig()
    mod = Modulator(cfg)
    rng = np.random.default_rng(5)
    pa = b"to node 1: " + bytes(rng.integers(0, 256, 150, dtype=np.uint8))
    pb = b"to node 2: " + bytes(rng.integers(0, 256, 150, dtype=np.uint8))
    xa, xb = mod.modulate(pa, 1), mod.modulate(pb, 2)
    rms = math.sqrt(np.mean(np.abs(xa) ** 2))
    for nf_db in (0, 6, 10):
        a = channel(xa, cfg.samp_rate, cfo=3000, delay=1000)
        bb = channel(xb, cfg.samp_rate, cfo=-5000, delay=7000, gain=10 ** (nf_db / 20))
        got1, _ = run(cfg, [a, bb], max(a.size, bb.size) + 2000, noise_for(cfg, 15, rms), [1, 0], rng=rng)
        got2, _ = run(cfg, [a, bb], max(a.size, bb.size) + 2000, noise_for(cfg, 15, rms), [2, 0], rng=rng)
        ok1 = any(g.payload == pa for g in got1)
        ok2 = any(g.payload == pb for g in got2)
        print(f"  two overlapping bursts, interferer +{nf_db} dB: node1 {'ok' if ok1 else 'FAIL'}, node2 {'ok' if ok2 else 'FAIL'}"
              f"  (node1 Eb/N0 est {got1[0].snr_db:.1f} dB)" if got1 else "")


def multicode_tx():
    """One transmitter sending to two receivers at the same time (code multiplexing)."""
    cfg = PhyConfig()
    mod = Modulator(cfg)
    pa, pb = b"hello one" * 10, b"hello two" * 12
    xa, xb = mod.modulate(pa, 1), mod.modulate(pb, 2)
    n = max(xa.size, xb.size)
    s = np.zeros(n, complex); s[:xa.size] += xa; s[:xb.size] += xb
    rms = math.sqrt(np.mean(np.abs(xa) ** 2))
    b = channel(s, cfg.samp_rate, cfo=4000, delay=3000)
    g1, _ = run(cfg, [b], b.size + 1000, noise_for(cfg, 14, rms), [1, 0])
    g2, _ = run(cfg, [b], b.size + 1000, noise_for(cfg, 14, rms), [2, 0])
    print(f"  multi-code TX to 2 nodes simultaneously: node1 {[g.payload == pa for g in g1]}, node2 {[g.payload == pb for g in g2]}")
    assert any(g.payload == pa for g in g1) and any(g.payload == pb for g in g2)


def same_code():
    """Two senders to the SAME receiver at the same time (asynchronous code phases)."""
    cfg = PhyConfig()
    mod = Modulator(cfg)
    rng = np.random.default_rng(9)
    ok = 0
    trials = 10
    for t in range(trials):
        pa = b"from A " + bytes(rng.integers(0, 256, 120, dtype=np.uint8))
        pb = b"from B " + bytes(rng.integers(0, 256, 120, dtype=np.uint8))
        xa, xb = mod.modulate(pa, 4), mod.modulate(pb, 4)
        rms = math.sqrt(np.mean(np.abs(xa) ** 2))
        a = channel(xa, cfg.samp_rate, cfo=2000, delay=1000)
        b = channel(xb, cfg.samp_rate, cfo=-4000, delay=1000 + int(rng.integers(8, 20000)), gain=10 ** (rng.uniform(-3, 3) / 20))
        got, _ = run(cfg, [a, b], max(a.size, b.size) + 2000, noise_for(cfg, 15, rms), [4, 0], rng=rng)
        ok += any(g.payload == pa for g in got) + any(g.payload == pb for g in got)
    print(f"  same code, two overlapping senders: {ok}/{2*trials} bursts decoded")


def throughput():
    cfg = PhyConfig()
    rng = np.random.default_rng(0)
    x = (rng.standard_normal(int(cfg.samp_rate)) + 1j * rng.standard_normal(int(cfg.samp_rate))).astype(np.complex64)
    dem = Demodulator(cfg, [1, 0])
    t = time.time()
    for i in range(0, x.size, 32768):
        dem.process(x[i:i + 32768])
    dt = time.time() - t
    print(f"  RX CPU: {dt*1000:.0f} ms per 1 s of samples at {cfg.samp_rate/1e6:.0f} MS/s (2 codes) -> {1/dt:.1f}x real time")


if __name__ == "__main__":
    cfg = PhyConfig()
    print(f"PHY: {cfg.sf}-chip Gold codes, {cfg.chip_rate/1e6:.2f} Mchip/s, {cfg.symbol_rate/1e3:.2f} kbit/s, "
          f"processing gain {10*math.log10(cfg.sf):.1f} dB")
    test_clean_and_impairments()
    false_alarms()
    multicode_tx()
    multiuser()
    same_code()
    sweep()
    throughput()
