"""LDPC + Golay unit tests:  python -m tests.test_ldpc
* encoder: every 5G lifting size, all 46 parity checks satisfied
* rate matching: noiseless round trip for many sizes x rates x modulations
* AWGN (BPSK, 1 KB frames): frame errors vs Eb/N0 per rate, decoder time
* Golay(24,12): minimum distance 8, soft decoding
"""
import time
import numpy as np

from wavelink import ldpc, golay


def encoder():
    rng = np.random.default_rng(1)
    bad = 0
    for Z in ldpc.LIFTING:
        code = ldpc.code_for(Z)
        u = rng.integers(0, 2, ldpc.KB * Z).astype(np.uint8)
        bad += not code.syndrome_ok(code.encode(u), ldpc.MB)
    print(f"  encoder: {len(ldpc.LIFTING)} lifting sizes, parity-check failures: {bad}")
    assert bad == 0


def round_trips():
    rng = np.random.default_rng(2)
    n = 0
    for nbytes in (12, 40, 153, 600, 1028, 1056):
        for rc in ldpc.RATES:
            for bps in (1, 2, 4):
                data = bytes(rng.integers(0, 256, nbytes, dtype=np.uint8))
                e = ldpc.encode_frame(data, rc, bps)
                out, ok, _ = ldpc.decode_frame((1 - 2.0 * e).astype(np.float32), nbytes, rc, bps)
                assert ok and out == data, (nbytes, rc, bps)
                n += 1
    print(f"  rate matching: {n} noiseless round trips OK")


def awgn(trials=20):
    rng = np.random.default_rng(3)
    nbytes = 1028
    K = 8 * nbytes
    print(f"  AWGN, BPSK, {nbytes}-byte frames ({trials} per point): frame errors / decoder time")
    for rc, points in ((1, (1.0, 1.5, 2.0)), (3, (2.5, 3.0, 3.5)), (4, (3.5, 4.0, 4.5))):
        row = []
        for eb in points:
            bad, t = 0, 0.0
            for _ in range(trials):
                data = bytes(rng.integers(0, 256, nbytes, dtype=np.uint8))
                e = ldpc.encode_frame(data, rc, 1)
                s2 = 1 / (2 * (K / e.size) * 10 ** (eb / 10))
                y = (1 - 2.0 * e) + np.sqrt(s2) * rng.standard_normal(e.size)
                t0 = time.perf_counter()
                out, _, _ = ldpc.decode_frame((2 * y / s2).astype(np.float32), nbytes, rc, 1)
                t += time.perf_counter() - t0
                bad += out != data
            row.append(f"Eb/N0 {eb} dB: {bad}/{trials} ({1000 * t / trials:.0f} ms)")
        print(f"    rate {ldpc.RATE_NAMES[rc]}: " + ", ".join(row))
    print("    (uncoded BPSK needs Eb/N0 ~10.5 dB for the same frame error rate)")


def golay_code():
    assert golay.min_distance() == 8
    rng = np.random.default_rng(4)
    bad = 0
    for _ in range(2000):
        m = rng.integers(0, 2, 12).astype(np.int8)
        y = (1 - 2.0 * golay.encode(m)) + np.sqrt(10 ** -0.2 / 2) * rng.standard_normal(24)  # 2 dB
        bad += not np.array_equal(golay.decode(y)[0], m)
    print(f"  Golay(24,12): d_min 8, soft decoding at Es/N0 2 dB: {bad}/2000 word errors")
    assert bad < 10


if __name__ == "__main__":
    encoder()
    round_trips()
    golay_code()
    awgn()
    print("LDPC tests passed")
