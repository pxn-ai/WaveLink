"""Simulation data for the report figures, computed with the project's own PHY code.
Run from the repo root:  python3 <this file> <outdir>
Writes whitespace-separated .dat tables for pgfplots.
"""
import os
import sys
import time
import numpy as np
from multiprocessing import Pool

sys.path.insert(0, os.getcwd())
from wavelink import ldpc, modem                     # noqa: E402
from wavelink.config import PhyConfig                # noqa: E402
from wavelink.codes import gold_codes                # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "data"
os.makedirs(OUT, exist_ok=True)
NBYTES = 1024

MCS = [  # name, mod, rate code, Es/N0 grid
    ("qpsk12", "qpsk", 1, np.arange(-1.0, 3.01, 0.5)),
    ("qpsk34", "qpsk", 3, np.arange(2.0, 6.01, 0.5)),
    ("qpsk56", "qpsk", 4, np.arange(3.0, 7.01, 0.5)),
    ("qam12", "16qam", 1, np.arange(5.0, 9.01, 0.5)),
    ("qam23", "16qam", 2, np.arange(7.0, 11.01, 0.5)),
    ("qam34", "16qam", 3, np.arange(8.0, 12.01, 0.5)),
    ("qam56", "16qam", 4, np.arange(9.5, 13.51, 0.5)),
    ("qamunc", "16qam", 0, np.arange(15.0, 23.01, 1.0)),
]
if os.environ.get("FINE"):
    MCS = [
        ("qpsk12", "qpsk", 1, np.arange(0.8, 1.61, 0.1)),
        ("qpsk34", "qpsk", 3, np.arange(3.8, 4.61, 0.1)),
        ("qpsk56", "qpsk", 4, np.arange(4.8, 5.61, 0.1)),
        ("qam12", "16qam", 1, np.arange(6.3, 7.11, 0.1)),
        ("qam23", "16qam", 2, np.arange(8.5, 9.31, 0.1)),
        ("qam34", "16qam", 3, np.arange(9.8, 10.61, 0.1)),
        ("qam56", "16qam", 4, np.arange(11.1, 11.91, 0.1)),
    ]


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


def fer():
    jobs = []
    only = os.environ.get('ONLY')
    for i, (name, mod, rc, grid) in enumerate(MCS):
        if only and not name.startswith(only):
            continue
        for j, es in enumerate(grid):
            jobs.append((name, mod, rc, float(es), 1000 * i + j))
    with Pool(2) as p:
        res = p.map(fer_point, jobs, chunksize=1)
    by = {}
    for name, es, n, e, it in res:
        by.setdefault(name, []).append((es, n, e, it))
    for name, rows in by.items():
        with open(f"{OUT}/fer_{name}{os.environ.get('FINE', '')}.dat", "w") as f:
            f.write("esn0 trials errors fer iters\n")
            for es, n, e, it in sorted(rows):
                f.write(f"{es:.2f} {n} {e} {max(e, 0) / n:.5f} {it:.2f}\n")
        print(name, [(r[0], r[2], r[1], round(r[3], 1)) for r in sorted(rows)], flush=True)


def psd():
    cfg = PhyConfig.from_profile("fast")
    mo = modem.Modulator(cfg)
    rng = np.random.default_rng(7)
    xs = [mo.modulate(bytes(rng.integers(0, 256, 1000, dtype=np.uint8)), 2, "16qam") for _ in range(3)]
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
    rng = np.random.default_rng(11)
    G = gold_codes(5).astype(float)
    c1, c2, c3 = G[1], G[2], G[3]
    nsym = 10
    b1 = rng.choice([-1.0, 1.0], nsym)
    b2 = rng.choice([-1.0, 1.0], nsym)
    chips = np.kron(b1, c1) + np.kron(b2, c2)
    snr_chip_db = -3.0  # per chip, per user
    sigma = np.sqrt(10 ** (-snr_chip_db / 10))
    r = chips + sigma * rng.standard_normal(chips.size)
    L = 31
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
    with open(f"{OUT}/despread_bits.dat", "w") as f:
        f.write("k bit\n")
        for i in range(nsym):
            f.write(f"{i} {b1[i]:.0f}\n")
    print("despread: peak", np.abs(k1[::L]).mean(), "wrong-code rms", np.sqrt(np.mean(k3 ** 2)))


def constellations():
    cfg = PhyConfig.from_profile("fast")
    mo = modem.Modulator(cfg)
    rng = np.random.default_rng(5)
    fs = cfg.samp_rate
    cases = [("qpsk", "qpsk", 9.0, False), ("qam", "16qam", 22.0, False), ("qammui", "16qam-3/4", 22.0, True)]
    for tag, mcs, target, mui in cases:
        got = None
        for attempt in range(6):
            data = bytes(rng.integers(0, 256, 600, dtype=np.uint8))
            x = mo.modulate(data, 2, mcs)
            if mui:
                y = mo.modulate(bytes(rng.integers(0, 256, 600, dtype=np.uint8)), 3, mcs)
                y = y[:x.size] if y.size >= x.size else np.concatenate([y, np.zeros(x.size - y.size, np.complex64)])
                d = 37  # samples: another code phase
                x = x + np.roll(y, d) * np.exp(1j * 1.1)
            sig_p = 0.0625
            # Es/N0 after despreading = chip SNR + 10log10(31); per-sample noise for sps = 2
            sigma2 = sig_p * cfg.sps * 31 / 10 ** (target / 10) * 0.5
            n = np.arange(x.size + 4000)
            xx = np.concatenate([np.zeros(2000, np.complex64), x, np.zeros(2000, np.complex64)])
            xx = xx * np.exp(1j * (2 * np.pi * 3000 * n / fs + 0.7))
            xx = xx + np.sqrt(sigma2 / 2) * (rng.standard_normal(xx.size) + 1j * rng.standard_normal(xx.size))
            frames = []
            dm = modem.Demodulator(cfg, [2], on_frame=frames.append)
            dm.process(xx.astype(np.complex64))
            if frames and frames[0].payload == data:
                got = frames[0]
                break
        if got is None:
            print("constellation", tag, "not decoded")
            continue
        s = got.symbols if got.symbols is not None else got.constellation
        s = s[:900]
        with open(f"{OUT}/const_{tag}.dat", "w") as f:
            f.write("i q\n")
            for v in s:
                f.write(f"{v.real:.4f} {v.imag:.4f}\n")
        print("constellation", tag, "esn0 %.1f dB" % got.esn0_db, "cfo %.0f Hz" % got.cfo_hz, len(s), "pts")


if __name__ == "__main__":
    what = sys.argv[2:] or ["psd", "despread", "const", "fer"]
    if "psd" in what:
        psd()
    if "despread" in what:
        despread_demo()
    if "const" in what:
        constellations()
    if "fer" in what:
        fer()
