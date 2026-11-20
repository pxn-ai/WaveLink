"""The 32-chip Walsh codes: one scrambling sequence s and five Hadamard rows w_k (codes 0-4).

    python3 tools/walsh_search.py              # check the codes in wavelink/codes.py
    python3 tools/walsh_search.py --search 120 # look for a set again (120 s of random restarts)

Code k is c_k = w_k * s (chip by chip).  Codes that start together - the bursts of a TD-CDMA slot -
are exactly orthogonal, whatever s is.  Off the peak they are not, and that matters for burst
detection and timing (a code's own correlation one or more chips off), and for bursts that arrive
at another code phase.  The receiver correlates one symbol, while the symbol before may have the
same or the opposite sign, so both the even (sum) and the odd (difference) periodic correlation
count.  A set is scored by: the largest |even| or |odd| autocorrelation off the peak and
cross-correlation at any lag, then the largest |sum of chips| (DC: the ANTSDR's DC tracking removes
it), then the mean square of all of them; smaller is better.  The search keeps the eight rows with
the lowest autocorrelation sidelobes for each candidate s, tries every five of them, and improves s
one chip flip at a time.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from itertools import combinations

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from wavelink.codes import WALSH_ROWS_32, WALSH_SCRAMBLE_32, gold_codes, hadamard  # noqa: E402

N = 32
H = hadamard(N).astype(np.int64)


def even_odd(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """|even| and |odd| periodic correlation of a with b at lags 0..n-1."""
    n = a.size
    c = np.correlate(b, a, mode="full")                 # c[n-1+t] = sum a[m] b[m+t]
    pos = c[n - 1:]
    neg = np.concatenate([[0], c[:n - 1]])
    return np.abs(pos + neg), np.abs(pos - neg)


def score(codes: np.ndarray) -> tuple[int, int, float]:
    worst, ms, cnt = 0, 0.0, 0
    for i, a in enumerate(codes):
        e, o = even_odd(a, a)
        side = np.concatenate([e[1:], o[1:]])
        worst = max(worst, int(side.max()))
        ms, cnt = ms + float((side ** 2).sum()), cnt + side.size
        for b in codes[i + 1:]:
            x = np.concatenate(even_odd(a, b) + even_odd(b, a))
            worst = max(worst, int(x.max()))
            ms, cnt = ms + float((x ** 2).sum()), cnt + x.size
    bal = int(max(abs(int(c.sum())) for c in codes))
    return worst, bal, ms / cnt


def best_rows(s: np.ndarray, keep: int = 8):
    c = H * s[None, :]
    per = sorted((max(int(e[1:].max()), int(o[1:].max())), k) for k in range(N)
                 for e, o in [even_odd(c[k], c[k])] if abs(int(c[k].sum())) <= 2)
    best = None
    for rows in combinations([k for _, k in per[:keep]], 5):
        sc = score(c[list(rows)])
        if best is None or sc < best[0]:
            best = (sc, rows)
    return best


def report(name: str, codes: np.ndarray):
    worst, bal, ms = score(codes.astype(np.int64))
    auto = max(int(max(e[1:].max(), o[1:].max())) for c in codes.astype(np.int64) for e, o in [even_odd(c, c)])
    zero = max(abs(int(np.dot(a, b))) for i, a in enumerate(codes.astype(np.int64)) for b in codes[i + 1:].astype(np.int64))
    n = codes.shape[1]
    print(f"{name}: {n} chips; worst sidelobe / cross-correlation {worst}/{n} ({worst / n:.3f}), "
          f"autocorrelation sidelobes {auto}/{n}, cross-correlation at zero lag {zero}/{n}, "
          f"largest |sum of chips| {bal}, mean square {ms:.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--search", type=float, default=0, metavar="SECONDS")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    s = np.array([1 if ch == "+" else -1 for ch in WALSH_SCRAMBLE_32], dtype=np.int64)
    report(f"Walsh rows {WALSH_ROWS_32} x scrambling", H[list(WALSH_ROWS_32)] * s[None, :])
    report("Gold codes #0-4", gold_codes(5)[:5])
    if not a.search:
        return
    rng = np.random.default_rng(a.seed)
    t_end = time.time() + a.search
    found = None
    while time.time() < t_end:
        s = rng.choice([-1, 1], size=N)
        b = best_rows(s)
        improved = b is not None
        while improved and time.time() < t_end:   # one chip flip at a time, while it helps
            improved = False
            for i in rng.permutation(N):
                s2 = s.copy()
                s2[i] = -s2[i]
                b2 = best_rows(s2)
                if b2 is not None and b2[0] < b[0]:
                    s, b, improved = s2, b2, True
                    break
        if b is not None and (found is None or b[0] < found[0]):
            found = (b[0], s.copy(), b[1])
            print(f"  worst {found[0][0]}/{N}, |sum| {found[0][1]}, mean square {found[0][2]:.1f}: rows {found[2]}, "
                  f"s = \"{''.join('+' if v > 0 else '-' for v in s)}\"", flush=True)


if __name__ == "__main__":
    main()
