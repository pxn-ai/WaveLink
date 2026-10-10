"""Gold spreading codes and code-specific preamble patterns.

Every station owns one Gold code (index = node address) and listens on it plus
on the broadcast code (index 0).  Gold codes of length N = 2^n - 1 have a
three-valued periodic cross-correlation {-1, -t(n), t(n)-2}, which keeps
the interference between simultaneously active links low.
"""
from __future__ import annotations

from functools import lru_cache
import numpy as np


def _mseq(taps: tuple[int, ...], n: int) -> np.ndarray:
    """Fibonacci LFSR m-sequence (0/1) for feedback polynomial with given taps."""
    state = [1] * n
    out = np.empty((1 << n) - 1, dtype=np.int8)
    for i in range(out.size):
        out[i] = state[-1]
        fb = 0
        for t in taps:
            fb ^= state[t - 1]
        state = [fb] + state[:-1]
    return out


def _is_msequence(seq: np.ndarray) -> bool:
    n_len = seq.size
    # a maximal-length sequence has a two-valued autocorrelation (N, -1)
    s = 1 - 2 * seq.astype(np.int32)
    ac = np.array([np.dot(s, np.roll(s, k)) for k in range(1, n_len)])
    return bool(np.all(ac == -1))


def _t_value(n: int) -> int:
    return (1 << ((n + 2) // 2)) + 1


def _xcorr_values(a: np.ndarray, b: np.ndarray) -> set[int]:
    sa = 1 - 2 * a.astype(np.int32)
    sb = 1 - 2 * b.astype(np.int32)
    return {int(np.dot(sa, np.roll(sb, k))) for k in range(a.size)}


@lru_cache(maxsize=None)
def preferred_pair(n: int) -> tuple[np.ndarray, np.ndarray]:
    """Find a preferred pair of m-sequences of degree n (n not divisible by 4)."""
    if n % 4 == 0 or not 3 <= n <= 9:
        raise ValueError("Gold codes need degree 3..9, not a multiple of 4")
    known = {5: ((5, 2), (5, 4, 3, 2)), 6: ((6, 1), (6, 5, 2, 1)),
             7: ((7, 3), (7, 3, 2, 1))}
    t = _t_value(n)
    allowed = {-1, -t, t - 2}
    if n in known:
        a, b = (_mseq(k, n) for k in known[n])
        if _is_msequence(a) and _is_msequence(b) and _xcorr_values(a, b) <= allowed:
            return a, b
    # brute-force search (small n only)
    from itertools import combinations
    cands = []
    for r in range(1, n):
        for extra in combinations(range(1, n), r):
            taps = (n,) + tuple(sorted(extra, reverse=True))
            s = _mseq(taps, n)
            if _is_msequence(s):
                cands.append(s)
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            if _xcorr_values(cands[i], cands[j]) <= allowed:
                return cands[i], cands[j]
    raise RuntimeError("no preferred pair found")


@lru_cache(maxsize=None)
def gold_codes(n: int) -> np.ndarray:
    """All 2^n + 1 Gold codes of degree n as a (num_codes, N) array of +/-1."""
    a, b = preferred_pair(n)
    N = a.size
    codes = [a, b] + [a ^ np.roll(b, -k) for k in range(N)]
    arr = np.array(codes, dtype=np.int8)
    # reorder: put balanced codes (one more -1 than +1 or vice versa) first so
    # the low indices used by real stations have the smallest DC component
    bal = np.abs(arr.astype(int).sum(axis=1) * 2 - N)
    order = np.argsort(bal, kind="stable")
    return (1 - 2 * arr[order]).astype(np.float32)


@lru_cache(maxsize=None)
def preamble_patterns(n_codes: int, n_bits: int) -> np.ndarray:
    """Code-specific +/-1 preamble patterns (n_codes, n_bits).

    The receiver detects bursts with a differential correlator
    sum_k q_k c_k c*_{k-1}, where q_k = b_k b_{k-1}.  Patterns are chosen
    greedily so that q has low autocorrelation sidelobes (unambiguous symbol
    timing) and low cross-correlation with every other code's q (a strong
    burst on another code that leaks through the code cross-correlation is
    not mistaken for ours).
    """
    rng_base = 0x5EED
    chosen: list[np.ndarray] = []
    qs: list[np.ndarray] = []
    for c in range(n_codes):
        best, best_score = None, None
        for trial in range(400):
            rng = np.random.default_rng(rng_base + 1000 * c + trial)
            b = rng.choice([-1.0, 1.0], size=n_bits)
            q = b[1:] * b[:-1]
            L = q.size
            side = max(abs(np.dot(q[s:], q[:L - s])) for s in range(1, L // 2))
            cross = max((abs(np.dot(q, q2)) for q2 in qs), default=0.0)
            score = max(side, cross)
            if best_score is None or score < best_score:
                best, best_score = b, score
        chosen.append(best)
        qs.append(best[1:] * best[:-1])
    return np.array(chosen, dtype=np.float32)


def rrc_taps(sps: int, rolloff: float, span: int) -> np.ndarray:
    """Root-raised-cosine taps (unit energy), span in chips."""
    n = np.arange(-span * sps // 2, span * sps // 2 + 1, dtype=np.float64)
    t = n / sps
    a = rolloff
    h = np.empty_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-12:
            h[i] = 1.0 - a + 4 * a / np.pi
        elif a > 0 and abs(abs(ti) - 1 / (4 * a)) < 1e-9:
            h[i] = (a / np.sqrt(2)) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * a))
                                       + (1 - 2 / np.pi) * np.cos(np.pi / (4 * a)))
        else:
            h[i] = (np.sin(np.pi * ti * (1 - a)) + 4 * a * ti * np.cos(np.pi * ti * (1 + a))) / (
                np.pi * ti * (1 - (4 * a * ti) ** 2))
    h /= np.sqrt(np.sum(h ** 2))
    return h.astype(np.float32)
