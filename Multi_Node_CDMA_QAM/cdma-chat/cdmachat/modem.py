r"""DS-CDMA / BPSK burst modem (pure numpy, used by both GNU Radio and the simulator).

Burst structure (one PHY frame)::

    | preamble: P bits (code specific) | header: len(16) + CRC-8 | payload bytes |
      \_____ known, not diff. coded __/ \_______ differentially encoded BPSK ____/

Every bit is spread by a 2^n-1 chip Gold code, every chip is RRC pulse shaped
(sps samples/chip).  The spreading code is the *destination's* code, so a
station only ever despreads bursts addressed to it (plus broadcasts).

Receiver chain (streaming):

1. RRC matched filter.
2. Code correlation c(t) at every sample offset.
3. Differential-coherent preamble detector
       M(t) = | sum_k q_k c(t+kT) c*(t+(k-1)T) |,   q_k = b_k b_{k-1}
   normalised by the despread energy.  Immune to carrier frequency offset
   (CFO), gives symbol timing and a coarse CFO estimate from arg(M).
4. CFO correction, preamble phase/frequency fit, then per-symbol despreading
   with a decision-directed 2nd-order PLL (carrier) and an early-late DLL
   (code timing, tracks the sample-clock offset between two SDRs).
5. Differential decoding (removes 180 deg PLL ambiguity), header CRC-8 check,
   payload handed up to the link layer (which checks CRC-32).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import time
import numpy as np

from .config import PhyConfig
from .codes import gold_codes, preamble_patterns, rrc_taps

HEADER_BITS = 24

# Payload modulations.  The header (BPSK) carries the modulation in the top two
# bits of its 16-bit length field, so the receiver adapts frame by frame.
MODS = {"bpsk": 0, "qpsk": 1, "16qam": 2}
MOD_NAMES = {v: k for k, v in MODS.items()}
BITS_PER_SYM = {"bpsk": 1, "qpsk": 2, "16qam": 4}
_PAM4 = np.array([-3, -1, 3, 1], dtype=np.float64) / np.sqrt(10)   # Gray: 00 01 10 11 -> -3 -1 +3 +1


def map_bits(u: np.ndarray, mod: str) -> np.ndarray:
    """Gray-mapped QPSK / 16-QAM symbols with unit average energy."""
    if mod == "qpsk":
        b = u.reshape(-1, 2)
        return ((1 - 2 * b[:, 0]) + 1j * (1 - 2 * b[:, 1])) / np.sqrt(2)
    if mod == "16qam":
        b = u.reshape(-1, 4).astype(np.int64)
        return _PAM4[2 * b[:, 0] + b[:, 1]] + 1j * _PAM4[2 * b[:, 2] + b[:, 3]]
    raise ValueError(mod)


def slice_bits(z: np.ndarray, mod: str) -> tuple[np.ndarray, np.ndarray]:
    """Hard decisions -> (bits, nearest constellation points)."""
    if mod == "qpsk":
        bi, bq = (z.real < 0), (z.imag < 0)
        bits = np.stack([bi, bq], axis=1).ravel().astype(np.int8)
        return bits, ((1 - 2 * bi) + 1j * (1 - 2 * bq)) / np.sqrt(2)
    if mod == "16qam":
        def pam(x):
            lv = np.clip(np.round((x * np.sqrt(10) + 3) / 2), 0, 3).astype(np.int64)  # 0..3 = -3,-1,1,3
            b0 = (lv >= 2).astype(np.int8)                     # sign bit
            b1 = ((lv == 0) | (lv == 3)).astype(np.int8)      # outer level
            b1 = 1 - b1                                       # Gray: -3->00 -1->01 +1->11 +3->10
            return b0, b1, (2 * lv - 3) / np.sqrt(10)
        i0, i1, ri = pam(z.real)
        q0, q1, rq = pam(z.imag)
        bits = np.stack([i0, i1, q0, q1], axis=1).ravel().astype(np.int8)
        return bits, ri + 1j * rq
    raise ValueError(mod)


# ----------------------------------------------------------------------------- helpers
_FD_TAPS = 8
_FD_PHASES = 64


def _make_fd_bank() -> np.ndarray:
    """Windowed-sinc fractional-delay filters: row p interpolates at +p/64 sample."""
    n = np.arange(_FD_TAPS) - (_FD_TAPS // 2 - 1)          # -3..4
    bank = np.empty((_FD_PHASES + 1, _FD_TAPS), dtype=np.float64)
    for p in range(_FD_PHASES + 1):
        f = p / _FD_PHASES
        x = n - f
        w = 0.42 + 0.5 * np.cos(np.pi * x / (_FD_TAPS / 2 + 0.5)) + 0.08 * np.cos(2 * np.pi * x / (_FD_TAPS / 2 + 0.5))
        h = np.sinc(x) * w
        bank[p] = h / h.sum()
    return bank.astype(np.complex64)


_FD_BANK = _make_fd_bank()


def _fd_filter(frac: float) -> np.ndarray:
    return _FD_BANK[int(round(frac * _FD_PHASES))]

def crc8(data: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(np.int8)


def bits_to_bytes(bits: np.ndarray) -> bytes:
    return np.packbits(bits.astype(np.uint8)).tobytes()


# ----------------------------------------------------------------------------- modulator
class Modulator:
    def __init__(self, cfg: PhyConfig):
        self.cfg = cfg
        self.codes = gold_codes(cfg.gold_degree)
        self.pre = preamble_patterns(self.codes.shape[0], cfg.preamble_bits)
        self.h = rrc_taps(cfg.sps, cfg.rolloff, cfg.rrc_span)

    def burst_symbols(self, payload: bytes, code_idx: int, mod: str = "bpsk") -> np.ndarray:
        if len(payload) > self.cfg.max_phy_payload or len(payload) >= 1 << 14:
            raise ValueError("payload too long for one PHY burst")
        hdr = (len(payload) | (MODS[mod] << 14)).to_bytes(2, "big")
        hdr += bytes([crc8(hdr)])
        pre = self.pre[code_idx]
        if mod == "bpsk":
            # whole frame differentially encoded BPSK: s_k = s_{k-1} * (-1)^{u_k}
            u = bytes_to_bits(hdr + payload)
            s = pre[-1] * np.cumprod(np.where(u == 1, -1.0, 1.0))
            return np.concatenate([pre, s]).astype(np.complex64)
        # header: differential BPSK (robust, needs no amplitude/phase reference)
        uh = bytes_to_bits(hdr)
        sh = pre[-1] * np.cumprod(np.where(uh == 1, -1.0, 1.0))
        # payload: coherent Gray-mapped QPSK / 16-QAM (phase anchored on the preamble)
        up = bytes_to_bits(payload)
        bps = BITS_PER_SYM[mod]
        if up.size % bps:
            up = np.concatenate([up, np.zeros(bps - up.size % bps, np.int8)])
        return np.concatenate([pre, sh, map_bits(up, mod)]).astype(np.complex64)

    def modulate(self, payload: bytes, code_idx: int, mod: str = "bpsk") -> np.ndarray:
        """Return the complex baseband burst (complex64) for one PHY frame."""
        cfg = self.cfg
        sym = self.burst_symbols(payload, code_idx, mod)
        chips = (sym[:, None] * self.codes[code_idx][None, :]).ravel()
        up = np.zeros(chips.size * cfg.sps, dtype=np.complex64)
        up[::cfg.sps] = chips
        x = np.convolve(up, self.h)                      # adds filter tails
        # scale so a single stream has RMS = amplitude / 2 (peak ~ amplitude)
        x *= (cfg.amplitude / 2) / np.sqrt(np.mean(np.abs(x) ** 2) + 1e-20)
        return x.astype(np.complex64)

    def burst_duration(self, n_bytes: int, mod: str = "bpsk") -> float:
        return frame_symbols(self.cfg, n_bytes, mod) * self.cfg.samples_per_symbol / self.cfg.samp_rate


def frame_symbols(cfg: PhyConfig, n_bytes: int, mod: str) -> int:
    bps = BITS_PER_SYM[mod]
    return cfg.preamble_bits + HEADER_BITS + (8 * n_bytes + bps - 1) // bps


# ----------------------------------------------------------------------------- demodulator
@dataclass
class RxFrame:
    payload: bytes
    code_idx: int
    snr_db: float            # despread Eb/N0 estimate
    chip_snr_db: float       # SNR per chip (before processing gain)
    cfo_hz: float
    metric: float            # detector metric (0..1)
    sample_index: int        # absolute sample index of burst start
    rx_time: float
    constellation: np.ndarray = field(repr=False, default=None)
    mod: str = "bpsk"
    esn0_db: float = 0.0     # despread symbol SNR (modulation independent)


@dataclass
class _CodeState:
    code_idx: int
    search_pos: int = 0              # absolute index from where to search
    excl_until: int = 0              # no new detection before this index (same preamble)
    pending: list = field(default_factory=list)   # [t0_abs, cfo, metric, total_symbols|None, rec]
    recent: list = field(default_factory=list)    # [t_start, t_end, peak] of accepted bursts
    recent_payloads: list = field(default_factory=list)  # (t0, hash) for duplicate suppression


class Demodulator:
    """Streaming multi-code receiver.  Feed raw complex samples with process()."""

    def __init__(self, cfg: PhyConfig, code_indices: list[int], on_frame=None,
                 on_reject=None):
        self.cfg = cfg
        self.codes = gold_codes(cfg.gold_degree)
        self.pre = preamble_patterns(self.codes.shape[0], cfg.preamble_bits)
        self.h = rrc_taps(cfg.sps, cfg.rolloff, cfg.rrc_span).astype(np.float32)
        self.P = cfg.samples_per_symbol
        self.K = cfg.preamble_bits
        self.on_frame = on_frame or (lambda f: None)
        self.on_reject = on_reject or (lambda reason, code: None)
        self.states = [_CodeState(c) for c in code_indices]
        self._tail = np.zeros(self.h.size - 1, dtype=np.complex64)
        self.buf = np.zeros(0, dtype=np.complex64)  # matched-filter output
        self.buf_start = 0                           # absolute index of buf[0]
        self.max_frame_symbols = self.K + HEADER_BITS + 8 * cfg.max_phy_payload
        self.stats = dict(detections=0, header_fail=0, frames=0, samples=0, ghosts=0)
        self.min_payload = 10        # shorter than any MAC frame -> random header match
        self.ghost_ratio = 0.2       # a detection overlapping a burst >7 dB stronger is a sidelobe
        self.last_constellation = np.zeros(0, dtype=np.complex64)
        self.last_metric_peak = 0.0

    # ---------------------------------------------------------------- public
    def set_codes(self, code_indices: list[int]):
        old = {s.code_idx: s for s in self.states}
        start = self.buf_start + self.buf.size
        self.states = [old.get(c) or _CodeState(c, search_pos=start) for c in code_indices]

    def busy(self) -> bool:
        """True while at least one burst is being received."""
        return any(s.pending for s in self.states)

    def skip(self, n: int):
        """Advance the sample clock by n samples of silence (simulator speed-up)."""
        self._tail[:] = 0
        self.buf_start += self.buf.size + n
        self.buf = np.zeros(0, dtype=np.complex64)
        self.stats["samples"] += n
        for s in self.states:
            s.search_pos = max(s.search_pos, self.buf_start)
            s.pending = []

    def process(self, x: np.ndarray):
        x = np.asarray(x, dtype=np.complex64)
        if x.size == 0:
            return
        self.stats["samples"] += x.size
        xin = np.concatenate([self._tail, x])
        y = np.convolve(xin, self.h, mode="valid").astype(np.complex64)
        self._tail = xin[-(self.h.size - 1):].copy()
        self.buf = np.concatenate([self.buf, y]) if self.buf.size else y
        for st in self.states:
            self._search(st)
            self._service_pending(st)
        self._trim()

    # ---------------------------------------------------------------- internals
    def _trim(self):
        margin = 4 * self.cfg.sps
        keep_from = self.buf_start + self.buf.size - 2 * self.K * self.P
        for st in self.states:
            keep_from = min(keep_from, st.search_pos - margin)
            for p in st.pending:
                keep_from = min(keep_from, p[0] - margin)
        drop = keep_from - self.buf_start
        max_keep = 2 * self.max_frame_symbols * self.P      # bound memory
        if self.buf.size - drop > max_keep:
            drop = self.buf.size - max_keep
        if drop > 0:
            self.buf = self.buf[drop:]
            self.buf_start += drop
            for st in self.states:
                st.search_pos = max(st.search_pos, self.buf_start)
                st.pending = [p for p in st.pending if p[0] >= self.buf_start + margin]

    def _corr(self, y: np.ndarray, code_idx: int) -> np.ndarray:
        """c[t] = sum_m code[m] * y[t + m*sps]"""
        code = self.codes[code_idx]
        sps = self.cfg.sps
        n = y.size - (code.size - 1) * sps
        if n <= 0:
            return np.zeros(0, dtype=np.complex64)
        c = np.zeros(n, dtype=np.complex64)
        for m, cm in enumerate(code):
            (np.add if cm > 0 else np.subtract)(c, y[m * sps: m * sps + n], out=c)
        return c

    def _search(self, st: _CodeState):
        """Find every preamble of this code in the new samples.

        Bursts on the same code from different senders usually arrive with
        different code phases, so each is detected and demodulated on its own
        (asynchronous 'spread-ALOHA' reception); the other burst is just
        interference that the despreading suppresses."""
        P, K = self.P, self.K
        rel = st.search_pos - self.buf_start
        c = self._corr(self.buf[rel:], st.code_idx)
        n = c.size - K * P
        if n <= P:
            return
        q = self.pre[st.code_idx]
        q = q[1:] * q[:-1]
        D = c[P:] * np.conj(c[:-P])
        M = np.zeros(n, dtype=np.complex64)
        for k in range(K - 1):          # q_k = +/-1 -> add / subtract in place
            (np.add if q[k] > 0 else np.subtract)(M, D[k * P: k * P + n], out=M)
        pc = c.real * c.real + c.imag * c.imag
        E = np.zeros(n, dtype=np.float32)
        for k in range(K):
            np.add(E, pc[k * P: k * P + n], out=E)
        Mabs = np.abs(M)
        E += 1e-30
        R = Mabs / E
        lim = n - P
        hits = np.flatnonzero(R[:lim] > self.cfg.det_threshold)
        thr2 = 0.5 * self.cfg.det_threshold
        for i0 in hits:
            i0 = int(i0)
            if st.search_pos + i0 < st.excl_until:
                continue
            win = slice(i0, i0 + P)
            t_rel = i0 + int(np.argmax(Mabs[win] * (R[win] > thr2)))
            t_abs = st.search_pos + t_rel
            st.excl_until = t_abs + P // 2
            peak = float(Mabs[t_rel])
            # partial-correlation 'ghosts' of a strong burst are self-similar (high
            # normalised metric) but much weaker in absolute terms -> reject them
            if any(r[0] - P <= t_abs <= r[1] and peak < self.ghost_ratio * r[2] for r in st.recent):
                self.stats["ghosts"] += 1
                continue
            metric = float(R[t_rel])
            cfo = float(np.angle(M[t_rel])) / (2 * np.pi * P / self.cfg.samp_rate)
            self.stats["detections"] += 1
            self.last_metric_peak = metric
            rec = [t_abs, t_abs + self.max_frame_symbols * P, peak]
            st.recent.append(rec)
            st.pending.append([t_abs, cfo, metric, None, rec, ("bpsk", 0)])
        st.search_pos += lim
        st.recent = [r for r in st.recent if r[1] >= st.search_pos - self.max_frame_symbols * P]

    def _service_pending(self, st: _CodeState):
        still = []
        for p in st.pending:
            while p is not None:
                r = self._try_demod(st, p)
                if r == "wait":
                    still.append(p)
                    p = None
                elif r == "more":          # header ok, now wait for / decode the payload
                    continue
                else:
                    p = None
        st.pending = still

    def _try_demod(self, st: _CodeState, p: list) -> str:
        t0_abs, cfo, metric, total, rec = p[:5]
        P, K = self.P, self.K
        nsym = total if total is not None else K + HEADER_BITS
        guard = 2 * self.cfg.sps + 8
        if t0_abs + nsym * P + guard > self.buf_start + self.buf.size:
            return "wait"
        mod, length = p[5]
        res = self._demod(t0_abs - self.buf_start, cfo, st.code_idx, nsym, mod if total else "bpsk")
        bits = res["bits"]
        if total is None:
            hdr = bits_to_bytes(bits[:HEADER_BITS])
            word = int.from_bytes(hdr[:2], "big")
            length, mcode = word & 0x3FFF, word >> 14
            if (crc8(hdr[:2]) != hdr[2] or mcode not in MOD_NAMES
                    or not self.min_payload <= length <= self.cfg.max_phy_payload):
                self.stats["header_fail"] += 1
                self.on_reject("header_crc", st.code_idx)
                rec[1] = t0_abs + K * P                  # not a real burst: shrink its span
                return "done"
            mod = MOD_NAMES[mcode]
            p[3] = frame_symbols(self.cfg, length, mod)
            p[5] = (mod, length)
            rec[1] = t0_abs + p[3] * P
            return "more"
        payload = bits_to_bytes(bits[HEADER_BITS:HEADER_BITS + 8 * length])
        h = hash(payload)
        if any(abs(t - t0_abs) < 2 * P and hh == h for t, hh in st.recent_payloads):
            self.stats["ghosts"] += 1                    # same burst decoded twice
            return "done"
        st.recent_payloads = st.recent_payloads[-15:] + [(t0_abs, h)]
        self.stats["frames"] += 1
        self.last_constellation = res["const"][-256:]
        frame = RxFrame(payload=payload, code_idx=st.code_idx, snr_db=res["snr_db"],
                        chip_snr_db=res["esn0_db"] - 10 * math.log10(self.cfg.sf),
                        cfo_hz=res["cfo"], metric=metric, sample_index=t0_abs,
                        rx_time=time.time(), constellation=res["const"][-256:],
                        mod=mod, esn0_db=res["esn0_db"])
        self.on_frame(frame)
        return "done"

    def _demod(self, t0: int, cfo: float, code_idx: int, nsym: int, mod: str = "bpsk") -> dict:
        """Despread + track + decide one burst.  Fully vectorised:

        1. CFO removal over the whole burst.
        2. One gather despreads every symbol at every timing offset in
           [-R, R] samples -> matrix S[k, offset].
        3. DLL: early/late energy per block of W symbols (cumulative sums),
           choosing the timing offset per block (tracks clock drift).
        4. Carrier: preamble phase/frequency fit, then a block-wise BPSK
           (squaring) phase estimate that follows residual drift; the 180 deg
           ambiguity of squaring is removed by the differential coding.
        """
        cfg = self.cfg
        K = self.K
        P, sps = self.P, cfg.sps
        code = self.codes[code_idx].astype(np.complex64)
        # ---- cut the burst and remove the CFO ------------------------------------
        d = max(1, sps // 2)                           # early/late spacing: half a chip
        R0 = d + sps + 1                               # initial timing search (samples)
        drift = int(np.ceil(nsym * P * 30e-6)) + 2     # worst-case clock drift (30 ppm)
        span = R0 + drift + d + _FD_TAPS
        lo = max(0, t0 - span)
        hi = min(self.buf.size, t0 + nsym * P + span)
        ph = (-2 * np.pi * cfo / cfg.samp_rate) * np.arange(lo - t0, hi - t0, dtype=np.float64)
        seg = self.buf[lo:hi] * np.exp(1j * ph).astype(np.complex64)
        pad = P + span
        seg = np.concatenate([np.zeros(pad, np.complex64), seg, np.zeros(pad, np.complex64)])
        base = t0 - lo + pad
        chips = np.arange(code.size) * sps

        def despread_int(k: np.ndarray, offs: np.ndarray) -> np.ndarray:
            """Despread symbols k at integer sample offsets -> (len(k), len(offs))."""
            idx = (base + k * P)[:, None, None] + offs[None, :, None] + chips[None, None, :]
            return seg[idx] @ code

        # ---- initial timing: preamble energy peak, refined to a fraction of a sample
        offs = np.arange(-R0, R0 + 1)
        pre_e = (np.abs(despread_int(np.arange(K), offs)) ** 2).sum(axis=0)
        i = int(np.argmax(pre_e))
        tau = float(offs[i])
        if 0 < i < offs.size - 1:
            a_, b_, c_ = pre_e[i - 1], pre_e[i], pre_e[i + 1]
            den = a_ - 2 * b_ + c_
            if den < 0:
                tau += float(np.clip(0.5 * (a_ - c_) / den, -0.5, 0.5))

        # ---- per block: despreading at fractional timing ------------------------
        # Despreading is linear, so despreading at a fractional delay equals a short
        # fractional-delay FIR applied across despread values at integer offsets.
        # This gives fine timing (1/64 sample) without resampling the burst.
        # Early-late DLL (proportional) tracks the clock offset between radios.
        W = 32
        y = np.empty(nsym, dtype=np.complex128)
        half = _FD_TAPS // 2
        lim = R0 + drift
        for k0 in range(0, nsym, W):
            ks = np.arange(k0, min(k0 + W, nsym))
            n0 = int(np.floor(tau))
            frac = tau - n0
            o = np.arange(n0 - d - half + 1, n0 + d + half + 1)          # integer offsets needed
            Dv = despread_int(ks, o)                                      # (n, len(o))
            hp = _fd_filter(frac)                                         # taps for 'frac'
            j0 = d                                                        # index of n0-half+1 in o
            yp = Dv[:, j0:j0 + _FD_TAPS] @ hp
            ye = Dv[:, j0 - d:j0 - d + _FD_TAPS] @ hp
            yl = Dv[:, j0 + d:j0 + d + _FD_TAPS] @ hp
            y[k0:k0 + ks.size] = yp
            e = float(np.sum(np.abs(ye) ** 2))
            l = float(np.sum(np.abs(yl) ** 2))
            disc = (e - l) / (e + l + 1e-30)
            tau = float(np.clip(tau - 0.6 * d * disc, -lim, lim))

        # ---- carrier: preamble fit, then block-wise squaring tracker ------------
        b = self.pre[code_idx]
        kk = np.arange(K)
        v = y[:K] * b
        ph = np.unwrap(np.angle(v))
        slope, icpt = np.polyfit(kk, ph, 1)
        allk = np.arange(nsym)
        z = y * np.exp(-1j * (icpt + slope * allk))
        Wp = 32
        nbp = (nsym + Wp - 1) // Wp
        centers = np.arange(nbp) * Wp + (Wp - 1) / 2
        hb = K + HEADER_BITS
        if mod == "bpsk":
            # BPSK: block-wise squaring estimate (data-independent), interpolated;
            # its 180 deg ambiguity is harmless thanks to differential coding.
            zz = np.zeros(nbp * Wp, dtype=np.complex128)
            zz[:nsym] = z
            zz[:K] *= b                                    # known symbols: remove modulation
            sq = (zz * zz).reshape(nbp, Wp).sum(axis=1)
            phi = np.unwrap(np.angle(sq)) / 2              # residual phase per block
            phi_k = np.interp(allk, centers, phi) if nbp > 1 else np.full(nsym, phi[0])
            out = (z * np.exp(-1j * phi_k)).astype(np.complex64)
            if np.sum(out[:K].real * b) < 0:
                out = -out
        else:
            # QPSK / 16-QAM: coherent, decision-directed block tracker anchored on the
            # preamble.  Each block is de-rotated by the predicted phase, sliced, and
            # the phase error measured against the decisions (z * conj(decision)).
            # A 2nd-order loop follows residual frequency.  No M-th power is used, so
            # there is no 90 deg ambiguity to slip into (that made some payloads fail
            # deterministically - and a retransmission of the same data fails again).
            amp0 = float(np.mean(z[:K].real * b))
            amp0 = amp0 if amp0 > 0 else 1.0
            zn_all = z / amp0
            theta, omega = 0.0, 0.0                        # rad, rad/symbol
            phi_k = np.empty(nsym)
            for j in range(nbp):
                s0, s1 = j * Wp, min((j + 1) * Wp, nsym)
                kk_b = np.arange(s0, s1)
                pred = theta + omega * (kk_b - (s0 + s1 - 1) / 2)
                zb = zn_all[s0:s1] * np.exp(-1j * pred)
                ref = np.empty(s1 - s0, dtype=np.complex128)
                if s1 <= K:
                    ref = b[s0:s1].astype(np.complex128)
                else:
                    ref[:] = 0
                    m_pre = kk_b < K
                    m_hdr = (kk_b >= K) & (kk_b < hb)
                    m_dat = kk_b >= hb
                    ref[m_pre] = b[kk_b[m_pre]]
                    ref[m_hdr] = np.where(zb[m_hdr].real >= 0, 1.0, -1.0)
                    if m_dat.any():
                        ref[m_dat] = slice_bits(zb[m_dat], mod)[1]
                err = float(np.angle(np.sum(zb * np.conj(ref))))
                phi_k[s0:s1] = pred + err
                theta = theta + err + omega * Wp           # phase at next block centre
                omega += 0.15 * err / Wp
            out = (z * np.exp(-1j * phi_k)).astype(np.complex64)

        # ---- decisions ----------------------------------------------------------
        amp = float(np.mean(out[:K].real * b))            # preamble amplitude = unit energy
        amp = amp if amp > 0 else 1.0
        zn = out / amp
        hard = np.where(zn.real >= 0, 1.0, -1.0)
        if mod == "bpsk":
            data = hard[K - 1:]                        # include last preamble symbol
            u = (data[1:] * data[:-1] < 0).astype(np.int8)  # differential decode
            ref = hard.astype(np.complex64)
        else:
            dh = hard[K - 1:hb]
            uh = (dh[1:] * dh[:-1] < 0).astype(np.int8)
            ub, pts = slice_bits(zn[hb:], mod)
            u = np.concatenate([uh, ub])
            ref = np.concatenate([hard[:hb], pts]).astype(np.complex64)
        # ---- SNR: error-vector based, Es/N0 per despread symbol ---------------
        err = float(np.mean(np.abs(zn - ref) ** 2)) + 1e-12
        es = float(np.mean(np.abs(ref) ** 2))
        esn0 = 10 * math.log10(max(es / err, 1e-6))
        ebn0 = esn0 - 10 * math.log10(BITS_PER_SYM[mod]) if nsym > hb else esn0
        res_slope = float(np.polyfit(allk, phi_k, 1)[0]) if nsym > 2 else 0.0
        cfo_total = cfo + (slope + res_slope) / (2 * np.pi) * cfg.symbol_rate
        return dict(bits=u, const=zn.astype(np.complex64), snr_db=ebn0, esn0_db=esn0, cfo=cfo_total, amp=amp)
