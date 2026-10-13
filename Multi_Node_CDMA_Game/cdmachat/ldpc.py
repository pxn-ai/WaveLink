"""LDPC forward error correction: the 5G NR code (3GPP TS 38.212, base graph 1).

One frame (MAC frame + CRC-32, at most 1,056 bytes) is always ONE codeword:

    info bits K  ->  pad with filler zeros to K' = 22 Z  (Z = smallest 5G lifting size with 22 Z >= K)
                 ->  systematic encoding with the lifted parity-check matrix H (46 Z x 68 Z)
                 ->  rate matching: drop the first 2 Z info bits (punctured, as in 5G) and the filler
                     bits, send the first E bits of the rest   (E = K / R rounded to whole symbols)
                 ->  5G bit interleaver (Qm rows) so each QAM symbol mixes systematic and parity bits

The code is rate compatible: every rate (1/3 ... 8/9) is a prefix of the same mother codeword, so
one matrix serves the whole adaptive-coding ladder.  The decoder only uses the rows of H whose
parity columns were sent.

Decoder: layered normalised min-sum (scale invariant, so it needs no noise-power estimate), fully
vectorised over each layer's Z check nodes, with rows that share no variables merged into one layer,
and an early stop as soon as all parity checks are satisfied.

LLR convention in this module: L = log P(b=0) / P(b=1)   (positive -> bit 0).
"""
from __future__ import annotations

from functools import lru_cache
import numpy as np

from .nr_bg1 import BG1

KB = 22                                  # information columns of base graph 1
NB = 68                                  # columns
MB = 46                                  # rows
LIFTING = sorted({a * 2 ** j for a in (2, 3, 5, 7, 9, 11, 13, 15) for j in range(9) if a * 2 ** j <= 384})
K_MAX = KB * 384                         # 8448 bits = 1056 bytes

# code rates of the adaptive ladder (header field 'rate', 3 bits; 5..7 reserved); 0 = uncoded
RATES = {0: None, 1: (1, 2), 2: (2, 3), 3: (3, 4), 4: (5, 6)}
RATE_NAMES = {0: "", 1: "1/2", 2: "2/3", 3: "3/4", 4: "5/6"}
RATE_CODES = {v: k for k, v in RATE_NAMES.items()}
BIG = 1e6                                # LLR of a known (filler) bit


def _ils(Z: int) -> int:
    for i, a in enumerate((2, 3, 5, 7, 9, 11, 13, 15)):
        z = Z
        while z % 2 == 0 and z > a:
            z //= 2
        if z == a:
            return i
    raise ValueError(f"{Z} is not a 5G lifting size")


def lifting_size(K: int) -> int:
    for Z in LIFTING:
        if KB * Z >= K:
            return Z
    raise ValueError(f"{K} info bits exceed one base-graph-1 codeword ({K_MAX})")


def coded_bits(K: int, rate_code: int, bps: int) -> int:
    """E: number of coded bits sent for K info bits at a rate, a whole number of symbols."""
    num, den = RATES[rate_code]
    Z = lifting_size(K)
    E = -(-K * den // num)
    E = -(-E // bps) * bps
    nmax = (NB - 2) * Z - (KB * Z - K)                 # whole mother codeword (minus fillers)
    return min(E, nmax - nmax % bps)


# ============================================================================= the lifted code
class Code:
    """Base graph 1 lifted with Z; encoder and decoder data for one Z."""

    def __init__(self, Z: int):
        self.Z = Z
        ils = _ils(Z)
        self.entries = [(r, c, v[ils] % Z) for r, c, v in BG1]
        self.rows = [[(c, s) for r, c, s in self.entries if r == i] for i in range(MB)]
        z = np.arange(Z)
        # gather index of every row: (deg, Z) variable indices, check node r <-> var c*Z + (r+s)%Z
        self.row_idx = [np.array([c * Z + (z + s) % Z for c, s in row], dtype=np.int64)
                        for row in self.rows]
        self._check_core()

    def _check_core(self):
        """Encoder assumes the 5G core: col 22 has shifts (a, b, -, a) in rows 0,1,3; cols 23..25
        form a double diagonal of identities; each extension row i>=4 has identity at col 22+i."""
        d = {(r, c): s for r, c, s in self.entries}
        a, b, c = d[(0, 22)], d[(1, 22)], d[(3, 22)]
        assert a == c and (2, 22) not in d, "unexpected base graph core"
        for (r, cc) in ((0, 23), (1, 23), (1, 24), (2, 24), (2, 25), (3, 25)):
            assert d[(r, cc)] == 0
        for i in range(4, MB):
            assert d[(i, 22 + i)] == 0
        self.core = (a, b)

    # ----------------------------------------------------------------- encoder
    def encode(self, u: np.ndarray, ncols: int = NB) -> np.ndarray:
        """u: (22 Z,) info bits incl. fillers -> codeword bits of columns 0..ncols-1."""
        Z = self.Z
        c = np.zeros(ncols * Z, dtype=np.uint8)
        c[:KB * Z] = u
        blocks = c.reshape(ncols, Z)
        z = np.arange(Z)

        def row_sum(i, upto):
            acc = np.zeros(Z, dtype=np.uint8)
            for col, s in self.rows[i]:
                if col < upto:
                    acc ^= blocks[col][(z + s) % Z]
            return acc

        lam = [row_sum(i, KB) for i in range(4)]
        a, b = self.core
        tot = lam[0] ^ lam[1] ^ lam[2] ^ lam[3]
        p22 = tot[(z - b) % Z]                          # P^b p22 = sum(lambda)
        blocks[22] = p22
        blocks[23] = lam[0] ^ p22[(z + a) % Z]
        blocks[24] = lam[1] ^ p22[(z + b) % Z] ^ blocks[23]
        blocks[25] = lam[3] ^ p22[(z + a) % Z]
        for i in range(4, ncols - 22):
            blocks[22 + i] = row_sum(i, 22 + i)
        return c

    def syndrome_ok(self, c: np.ndarray, nrows: int) -> bool:
        for i in range(nrows):
            if np.any(np.bitwise_xor.reduce(c[self.row_idx[i]], axis=0)):
                return False
        return True

    # ----------------------------------------------------------------- decoder
    @lru_cache(maxsize=None)
    def layers(self, nrows: int):
        """Group consecutive rows that touch disjoint variables into one layer (processed together)."""
        Z = self.Z
        out, cur, used = [], [], set()
        for i in range(nrows):
            cols = {c for c, _ in self.rows[i]}
            if cur and cols & used:
                out.append(cur)
                cur, used = [], set()
            cur.append(i)
            used |= cols
        if cur:
            out.append(cur)
        res = []
        n = (KB + nrows) * Z                                  # padding entries point past the code
        for rows in out:
            deg = max(len(self.rows[i]) for i in rows)
            idx = np.full((len(rows), deg, Z), n, dtype=np.int64)
            for k, i in enumerate(rows):
                idx[k, :len(self.rows[i])] = self.row_idx[i]
            res.append(idx)
        # all check-node edges, ordered by check, for the syndrome test
        flat = np.concatenate([self.row_idx[i].T.ravel() for i in range(nrows)])
        starts = np.concatenate([[0], np.cumsum([len(self.rows[i]) for i in range(nrows) for _ in range(Z)])[:-1]])
        return res, flat, starts

    def decode(self, llr: np.ndarray, nrows: int, max_iter: int = 20, alpha: float = 0.75):
        """llr: (ncols*Z,) channel LLRs (0 = punctured, +BIG = known 0).  Returns (bits, ok, iters)."""
        layers, flat, starts = self.layers(nrows)
        n = llr.size
        L = np.empty(n + 1, dtype=np.float32)            # last entry: padding variable
        L[:n] = llr
        L[n] = BIG
        lay = layers
        R = [np.zeros(idx.shape, dtype=np.float32) for idx in lay]
        inf = np.float32(np.inf)
        a = np.float32(alpha)
        it = 0
        ok = False
        for it in range(1, max_iter + 1):
            for idx, Rl in zip(lay, R):
                Q = np.take(L, idx)                               # (rows, deg, Z)
                Q -= Rl
                neg = Q < 0
                mag = np.abs(Q)
                flip = neg ^ np.logical_xor.reduce(neg, axis=1, keepdims=True)
                m1 = mag.min(axis=1, keepdims=True)              # smallest |Q| per check
                ismin = mag == m1
                np.putmask(mag, ismin, inf)
                m2 = mag.min(axis=1, keepdims=True)              # second smallest
                tie = ismin.sum(axis=1, keepdims=True) > 1       # two equal minima (e.g. punctured 0s)
                if tie.any():
                    m2 = np.where(tie, m1, m2)
                Rn = ismin.astype(np.float32)                    # min-sum: others' minimum
                Rn *= m2 - m1
                Rn += m1
                Rn *= a
                np.negative(Rn, out=Rn, where=flip)              # sign: product of the others
                Q += Rn
                L[idx] = Q
                Rl[...] = Rn
            hard = (L[:n] < 0).astype(np.uint8)
            if not np.any(np.add.reduceat(hard[flat], starts) & 1):
                ok = True
                break
        return (L[:n] < 0).astype(np.uint8), ok, it


@lru_cache(maxsize=None)
def code_for(Z: int) -> Code:
    return Code(Z)


# ============================================================================= frame level
def interleave(e: np.ndarray, bps: int) -> np.ndarray:
    """5G bit interleaver: write row-wise into bps rows, read column-wise."""
    return e if bps == 1 else e.reshape(bps, -1).T.ravel()


def deinterleave(f: np.ndarray, bps: int) -> np.ndarray:
    return f if bps == 1 else f.reshape(-1, bps).T.ravel()


def encode_frame(data: bytes, rate_code: int, bps: int) -> np.ndarray:
    """Frame bytes -> the E coded bits to transmit (uint8 0/1), interleaved for the modulation."""
    u = np.unpackbits(np.frombuffer(data, dtype=np.uint8))
    if not rate_code:
        return u
    K = u.size
    Z = lifting_size(K)
    E = coded_bits(K, rate_code, bps)
    F = KB * Z - K
    ncols = min(NB, -(-(2 * Z + E + F) // Z))
    ncols = max(ncols, 26)
    code = code_for(Z)
    full = np.zeros(KB * Z, dtype=np.uint8)
    full[:K] = u
    c = code.encode(full, ncols)
    # rate matching (redundancy version 0): skip the 2 Z punctured bits and the fillers
    d = np.concatenate([c[2 * Z:K], c[KB * Z:]])
    return interleave(d[:E], bps)


def decode_frame(llr: np.ndarray, nbytes: int, rate_code: int, bps: int, max_iter: int = 20):
    """Soft bits of the E coded bits (LLR, positive = 0) -> (frame bytes, ok, iterations)."""
    K = 8 * nbytes
    if not rate_code:
        bits = (llr[:K] < 0).astype(np.uint8)
        return np.packbits(bits).tobytes(), True, 0
    Z = lifting_size(K)
    E = coded_bits(K, rate_code, bps)
    F = KB * Z - K
    e = deinterleave(np.asarray(llr[:E], dtype=np.float32), bps)
    ncols = max(26, min(NB, -(-(2 * Z + E + F) // Z)))
    nrows = ncols - KB
    full = np.zeros(ncols * Z, dtype=np.float32)
    nsys = K - 2 * Z                                   # systematic bits that were sent
    full[2 * Z:K] = e[:nsys]
    full[K:KB * Z] = BIG                               # fillers: known zeros
    npar = E - nsys
    full[KB * Z:KB * Z + npar] = e[nsys:]
    bits, ok, it = code_for(Z).decode(full, nrows, max_iter=max_iter)
    return np.packbits(bits[:K]).tobytes(), ok, it


def rate_value(rate_code: int) -> float:
    r = RATES[rate_code]
    return 1.0 if r is None else r[0] / r[1]


# ============================================================================= soft demapping
def demap_maxlog(z: np.ndarray, points: np.ndarray, bps: int, n0: float = 1.0) -> np.ndarray:
    """Max-log LLRs (positive = 0) of Gray-mapped symbols; points[k] carries the bits of k (MSB first)."""
    d = np.abs(z[:, None] - points[None, :]) ** 2          # (n, M)
    M = points.size
    out = np.empty((z.size, bps), dtype=np.float32)
    k = np.arange(M)
    for j in range(bps):
        bit = (k >> (bps - 1 - j)) & 1
        out[:, j] = (d[:, bit == 1].min(axis=1) - d[:, bit == 0].min(axis=1)) / n0
    return out.ravel()
