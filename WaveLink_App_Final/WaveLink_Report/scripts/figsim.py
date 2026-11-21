"""Simulation data for the report figures, computed with the project's own PHY code.

Run from the wavelink-chat folder:   python3 <this file> <outdir> [what ...]
    what: psd despread const fer fine merge theory se   (default: all of them, in this order)
Writes whitespace-separated .dat tables for pgfplots.

    fer     coarse FER grid of every rung (ONLY=q64 runs just the names starting with q64)
    fine    a 0.1 dB grid across each waterfall found by 'fer' (the *_fine.dat files)
    merge   coarse + fine -> fer_<name>_all.dat (ferplot = nan where no frame was lost)
    theory  exact frame error rate of uncoded Gray-mapped 16-QAM and 64-QAM (1 KB frames)
    se      se.dat: the 10 % FER point of every rung (from the *_all.dat files), the full receiver's
            measured thresholds and the link layer's ladder (mac.MCS_TABLE)
"""
import math
import os
import sys
import time
import numpy as np
from multiprocessing import Pool

sys.path.insert(0, os.getcwd())
from wavelink import ldpc, modem, mac                # noqa: E402
from wavelink.config import PhyConfig                # noqa: E402
from wavelink.codes import walsh_codes               # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "data"
os.makedirs(OUT, exist_ok=True)
NBYTES = 1024

MCS = [  # name, mod, rate code, coarse Es/N0 grid
    ("qpsk12", "qpsk", 1, np.arange(-1.0, 3.01, 0.5)),
    ("qpsk34", "qpsk", 3, np.arange(2.0, 6.01, 0.5)),
    ("qpsk56", "qpsk", 4, np.arange(3.0, 7.01, 0.5)),
    ("qam12", "16qam", 1, np.arange(5.0, 9.01, 0.5)),
    ("qam23", "16qam", 2, np.arange(7.0, 11.01, 0.5)),
    ("qam34", "16qam", 3, np.arange(8.0, 12.01, 0.5)),
    ("qam56", "16qam", 4, np.arange(9.5, 13.51, 0.5)),
    ("qamunc", "16qam", 0, np.arange(15.0, 23.01, 1.0)),
    ("q64_12", "64qam", 1, np.arange(9.0, 13.01, 0.5)),
    ("q64_23", "64qam", 2, np.arange(11.5, 15.51, 0.5)),
    ("q64_34", "64qam", 3, np.arange(12.5, 16.51, 0.5)),
    ("q64_56", "64qam", 4, np.arange(14.0, 18.01, 0.5)),
    ("q64unc", "64qam", 0, np.arange(21.0, 28.01, 1.0)),
]
BITS = {"qpsk12": 1.0, "qpsk34": 1.5, "qpsk56": 5 / 3, "qam12": 2.0, "qam23": 8 / 3, "qam34": 3.0,
        "qam56": 10 / 3, "qamunc": 4.0, "q64_12": 3.0, "q64_23": 4.0, "q64_34": 4.5, "q64_56": 5.0,
        "q64unc": 6.0}
MCS_OF = {"qpsk12": "qpsk-1/2", "qpsk34": "qpsk-3/4", "qpsk56": "qpsk-5/6", "qam12": "16qam-1/2",
          "qam23": "16qam-2/3", "qam34": "16qam-3/4", "qam56": "16qam-5/6", "qamunc": "16qam",
          "q64_12": "64qam-1/2", "q64_23": "64qam-2/3", "q64_34": "64qam-3/4", "q64_56": "64qam-5/6",
          "q64unc": "64qam"}
# Es/N0 at which the full receiver (detection, synchronisation, tracking, decoder) delivers
# practically every 1 KB frame (numpy PHY; QPSK 1/2 is detection-limited; 64-QAM: 95-100 % with
# carrier offsets up to 12 kHz and 20 ppm clock offset, tests.test_modem and the threshold sweep)
FULL_RX = {"qpsk12": 5.0, "qpsk34": 5.5, "qpsk56": 6.0, "qam12": 9.0, "qam23": 10.0, "qam34": 11.0,
           "qam56": 12.5, "qamunc": 21.0, "q64_12": 13.0, "q64_23": 15.0, "q64_34": 17.0,
           "q64_56": 18.0, "q64unc": 26.0}


def selected():
    only = os.environ.get("ONLY")
    return [m for m in MCS if not only or m[0].startswith(only)]


def selected_names():
    return {m[0] for m in selected()}


def fer_point(args):
    name, mod, rc, es, seed = args
    rng = np.random.default_rng(seed)
    bps = modem.BITS_PER_SYM[mod]
    pts = modem.POINTS[mod].astype(np.complex128)
    n0 = 10 ** (-es / 10)
    errs = trials = iters = 0
    t0 = time.time()
    while trials < 300 and errs < 25 and time.time() - t0 < 60:
        data = bytes(rng.integers(0, 256, NBYTES, dtype=np.uint8))
        e = ldpc.encode_frame(data, rc, bps)
        u = e if e.size % bps == 0 else np.concatenate([e, np.zeros(bps - e.size % bps, np.uint8)])
        s = modem.map_bits(u.astype(np.int8), mod)
        z = s + np.sqrt(n0 / 2) * (rng.standard_normal(s.size) + 1j * rng.standard_normal(s.size))
        llr = ldpc.demap_maxlog(z, pts, bps, n0)
        out, ok, it = ldpc.decode_frame(llr, NBYTES, rc, bps)
        trials += 1
        errs += out != data
        iters += it
    return name, es, trials, errs, iters / trials


def _run_points(jobs, suffix):
    with Pool(2) as p:
        res = p.map(fer_point, jobs, chunksize=1)
    by = {}
    for name, es, n, e, it in res:
        by.setdefault(name, []).append((es, n, e, it))
    for name, rows in by.items():
        with open(f"{OUT}/fer_{name}{suffix}.dat", "w") as f:
            f.write("esn0 trials errors fer iters\n")
            for es, n, e, it in sorted(rows):
                f.write(f"{es:.2f} {n} {e} {e / n:.5f} {it:.2f}\n")
        print(name + suffix, [(r[0], r[2], r[1], round(r[3], 1)) for r in sorted(rows)], flush=True)


def _read(path):
    rows = np.loadtxt(path, skiprows=1, ndmin=2)
    return rows


def fer():
    jobs = []
    for i, (name, mod, rc, grid) in enumerate(MCS):
        if name not in selected_names():
            continue
        for j, es in enumerate(grid):
            jobs.append((name, mod, rc, float(es), 1000 * i + j))
    _run_points(jobs, "")


def fine():
    """0.1 dB steps from the last coarse point that lost (nearly) every frame to the first that
    lost none: the waterfall of an 8-kbit codeword is a few tenths of a dB wide."""
    jobs = []
    for i, (name, mod, rc, grid) in enumerate(MCS):
        if name not in selected_names() or rc == 0 or not os.path.exists(f"{OUT}/fer_{name}.dat"):
            continue
        r = _read(f"{OUT}/fer_{name}.dat")
        hi_fer = r[r[:, 3] >= 0.9][:, 0]
        zero = r[r[:, 2] == 0][:, 0]
        lo = hi_fer.max() if hi_fer.size else r[0, 0]
        hi = zero.min() if zero.size else r[-1, 0]
        for j, es in enumerate(np.arange(lo + 0.1, hi - 0.05, 0.1)):
            jobs.append((name, mod, rc, round(float(es), 2), 5000 + 1000 * i + j))
    if jobs:
        _run_points(jobs, "_fine")


def merge():
    for name, *_ in selected():
        rows = {}
        for suffix in ("", "_fine"):
            p = f"{OUT}/fer_{name}{suffix}.dat"
            if os.path.exists(p):
                for es, n, e, f, it in _read(p):
                    rows[round(es, 2)] = (int(n), int(e), f, it)
        if not rows:
            continue
        with open(f"{OUT}/fer_{name}_all.dat", "w") as fo:
            fo.write("esn0 trials errors fer ferplot iters\n")
            for es in sorted(rows):
                n, e, f, it = rows[es]
                fo.write(f"{es:.2f} {n} {e} {f:.5f} {f if e else 'nan'} {it:.2f}\n")
        print("merged", name, len(rows), "points")


def _q(x):
    return 0.5 * math.erfc(x / math.sqrt(2))


def theory():
    """Uncoded Gray-mapped square QAM: a frame is lost if any of its symbols is wrong;
    P_s = 1 - (1 - P_pam)^2 with P_pam = 2 (1 - 1/L) Q(sqrt(3 Es/N0 / (M - 1))) per axis."""
    for name, m, lo, hi in (("qamunc", 16, 14.0, 24.0), ("q64unc", 64, 20.0, 30.0)):
        L = int(math.isqrt(m))
        nsym = NBYTES * 8 / math.log2(m)
        with open(f"{OUT}/fer_{name}_theory.dat", "w") as f:
            f.write("esn0 fer\n")
            for es in np.arange(lo, hi + 0.001, 0.1):
                g = 10 ** (es / 10)
                p_pam = 2 * (1 - 1 / L) * _q(math.sqrt(3 * g / (m - 1)))
                p_s = 1 - (1 - p_pam) ** 2
                f.write(f"{es:.2f} {1 - (1 - p_s) ** nsym:.6g}\n")
        print("theory", name)


def _cross(path, level=0.1):
    """Es/N0 where the FER first falls through `level` (log-linear between grid points)."""
    r = _read(path)
    r = r[np.argsort(r[:, 0])]
    for (e0, _, _, f0, *_), (e1, _, _, f1, *_) in zip(r[:-1], r[1:]):
        if f0 >= level > f1:
            if f1 <= 0:
                return e1 if f0 == level else e0 + (e1 - e0) * 0.5
            return e0 + (e1 - e0) * (math.log(f0) - math.log(level)) / (math.log(f0) - math.log(f1))
    return float("nan")


def se():
    with open(f"{OUT}/se.dat", "w") as fo:
        fo.write("name bits awgn fullrx ladder\n")
        for name, *_ in MCS:
            p = f"{OUT}/fer_{name}_all.dat"
            aw = _cross(p) if os.path.exists(p) else float("nan")
            fo.write(f"{name} {BITS[name]:.4f} {aw:.2f} {FULL_RX[name]:.2f} {mac.MCS_THRESH[MCS_OF[name]]:.2f}\n")
            print("se", name, round(aw, 2))


def psd():
    cfg = PhyConfig.from_profile("fast")                 # Walsh codes, 2 Mchip/s
    mo = modem.Modulator(cfg)
    rng = np.random.default_rng(7)
    xs = [mo.modulate(bytes(rng.integers(0, 256, 1000, dtype=np.uint8)), 2, "64qam") for _ in range(3)]
    x = np.concatenate(xs)
    n = 1024
    w = np.hanning(n)
    segs = [(x[k:k + n] * w) for k in range(0, x.size - n, n // 2)]
    P = np.mean([np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2 for s in segs], axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / cfg.samp_rate)) / 1e6
    P = 10 * np.log10(P / P.max())
    with open(f"{OUT}/psd.dat", "w") as fo:
        fo.write("f p\n")
        for a, b in zip(f[::2], P[::2]):
            fo.write(f"{a:.4f} {max(b, -70):.2f}\n")
    occ = f[P > -20]
    print("psd -20 dB width (MHz):", occ.min(), occ.max())


def despread_demo():
    """One station's slot: BPSK symbols for two destinations on Walsh codes 1 and 2, sent together
    (chip and symbol aligned), chip SNR -3 dB each.  Despread with code 1 and with code 3 (unused)."""
    rng = np.random.default_rng(11)
    W = walsh_codes(5).astype(float)
    c1, c2, c3 = W[1], W[2], W[3]
    L = c1.size
    nsym = 10
    b1 = rng.choice([-1.0, 1.0], nsym)
    b2 = rng.choice([-1.0, 1.0], nsym)
    chips = np.kron(b1, c1) + np.kron(b2, c2)
    snr_chip_db = -3.0  # per chip, per destination
    sigma = np.sqrt(10 ** (-snr_chip_db / 10))
    r = chips + sigma * rng.standard_normal(chips.size)
    with open(f"{OUT}/chips.dat", "w") as f:
        f.write("n r\n")
        for i in range(0, 5 * L):
            f.write(f"{i} {r[i]:.3f}\n")
    pad = np.concatenate([r, np.zeros(L)])
    corr = lambda c: np.array([pad[n:n + L] @ c for n in range(nsym * L - L + 1)])
    k1, k3 = corr(c1), corr(c3)
    with open(f"{OUT}/despread.dat", "w") as f:
        f.write("n right wrong\n")
        for i in range(k1.size):
            f.write(f"{i / L:.4f} {k1[i]:.2f} {k3[i]:.2f}\n")
    # without noise: what the other destination's burst leaves in code 1 and code 3 at the symbol instants
    clean = np.kron(b2, c2)
    leak1 = [clean[k * L:(k + 1) * L] @ c1 for k in range(nsym)]
    leak3 = [(np.kron(b1, c1) + clean)[k * L:(k + 1) * L] @ c3 for k in range(nsym)]
    print("despread: peak", np.abs(k1[::L]).mean(), "wrong-code rms", np.sqrt(np.mean(k3 ** 2)),
          "| noiseless leakage at the symbol instants: code 1", max(np.abs(leak1)), "code 3", max(np.abs(leak3)))


def _decode(cfg, y, code):
    frames = []
    modem.Demodulator(cfg, [code], on_frame=frames.append).process(y.astype(np.complex64))
    return frames


def _noise(cfg, esn0_db, rms, n, rng):
    ecn0 = 10 ** (esn0_db / 10) / cfg.sf
    sd = math.sqrt(rms ** 2 * cfg.sps / ecn0)
    return sd / math.sqrt(2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))


def _save_const(tag, fr, note):
    s = fr.symbols if fr.symbols is not None else fr.constellation
    s = s[:1200]
    with open(f"{OUT}/const_{tag}.dat", "w") as f:
        f.write("i q\n")
        for v in s:
            f.write(f"{v.real:.4f} {v.imag:.4f}\n")
    print("constellation", tag, "Es/N0 %.1f dB" % fr.esn0_db, "cfo %.0f Hz" % fr.cfo_hz, len(s), "pts", note)


def constellations():
    rng = np.random.default_rng(5)
    # (a)-(c): one burst alone, Walsh codes (the default), 3 kHz carrier offset and a random phase
    cfg = PhyConfig.from_profile("fast")
    mo = modem.Modulator(cfg)
    fs = cfg.samp_rate
    for tag, mcs, target in (("qpsk", "qpsk", 12.0), ("qam", "16qam", 25.0), ("q64", "64qam", 31.0)):
        for attempt in range(6):
            data = bytes(rng.integers(0, 256, 600, dtype=np.uint8))
            x = mo.modulate(data, 2, mcs)
            rms = math.sqrt(np.mean(np.abs(x) ** 2))
            n = np.arange(x.size + 4000)
            xx = np.concatenate([np.zeros(2000, np.complex64), x, np.zeros(2000, np.complex64)])
            xx = xx * np.exp(1j * (2 * np.pi * 3000 * n / fs + 0.7)) + _noise(cfg, target, rms, xx.size, rng)
            fr = [f for f in _decode(cfg, xx, 2) if f.payload == data]
            if fr:
                _save_const(tag, fr[0], "decoded")
                break
        else:
            print("constellation", tag, "not decoded")
    # (d), (e): one TD-CDMA slot, uncoded 64-QAM bursts for four destinations (codes 1-4) starting
    # together, Es/N0 35 dB per code: Walsh codes stay orthogonal, Gold codes leave cross-correlation
    for fam in ("walsh", "gold"):
        c = PhyConfig.from_profile("fast", codes=fam)
        m = modem.Modulator(c)
        pays = [bytes(rng.integers(0, 256, 600, dtype=np.uint8)) for _ in range(4)]
        bursts = [m.modulate(p, k, "64qam") for k, p in zip((1, 2, 3, 4), pays)]
        nmax = max(b.size for b in bursts)
        y = np.zeros(nmax + 4000, complex)
        for b in bursts:
            y[2000:2000 + b.size] += b
        rms = math.sqrt(np.mean(np.abs(bursts[0]) ** 2))
        n = np.arange(y.size)
        y = y * np.exp(1j * (2 * np.pi * 3000 * n / fs + 0.7)) + _noise(c, 35.0, rms, y.size, rng)
        fr = _decode(c, y, 1)
        if fr:
            _save_const(f"slot_{fam}", fr[0], "decoded" if fr[0].payload == pays[0] else "NOT decoded")
        else:
            print("constellation", f"slot_{fam}", "no burst found")
    # (f): spread ALOHA, Gold codes: 16-QAM 3/4 with an equal-power burst on another code at another code phase
    c = PhyConfig.from_profile("fast", codes="gold")
    m = modem.Modulator(c)
    for attempt in range(6):
        data = bytes(rng.integers(0, 256, 600, dtype=np.uint8))
        x = m.modulate(data, 2, "16qam-3/4")
        rms = math.sqrt(np.mean(np.abs(x) ** 2))
        o = m.modulate(bytes(rng.integers(0, 256, 600, dtype=np.uint8)), 3, "16qam-3/4")
        o = o[:x.size] if o.size >= x.size else np.concatenate([o, np.zeros(x.size - o.size, np.complex64)])
        x = x + np.roll(o, 37) * np.exp(1j * 1.1)          # 37 samples: another code phase
        xx = np.concatenate([np.zeros(2000, np.complex64), x, np.zeros(2000, np.complex64)])
        n = np.arange(xx.size)
        xx = xx * np.exp(1j * (2 * np.pi * 3000 * n / fs + 0.7)) + _noise(c, 22.0, rms, xx.size, rng)
        fr = [f for f in _decode(c, xx, 2) if f.payload == data]
        if fr:
            _save_const("qammui", fr[0], "decoded")
            break
    else:
        print("constellation qammui not decoded")


if __name__ == "__main__":
    what = sys.argv[2:] or ["psd", "despread", "const", "fer", "fine", "merge", "theory", "se"]
    steps = {"psd": psd, "despread": despread_demo, "const": constellations, "fer": fer, "fine": fine,
             "merge": merge, "theory": theory, "se": se}
    for w in what:
        steps[w]()
