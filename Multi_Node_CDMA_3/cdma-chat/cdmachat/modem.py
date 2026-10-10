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


# ----------------------------------------------------------------------------- helpers
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

    def burst_symbols(self, payload: bytes, code_idx: int) -> np.ndarray:
        if len(payload) > self.cfg.max_phy_payload:
            raise ValueError("payload too long for one PHY burst")
        hdr = len(payload).to_bytes(2, "big")
        hdr += bytes([crc8(hdr)])
        u = bytes_to_bits(hdr + payload)
        pre = self.pre[code_idx]
        # differential encoding: s_k = s_{k-1} * (-1)^{u_k}
        flips = np.where(u == 1, -1.0, 1.0)
        s = pre[-1] * np.cumprod(flips)
        return np.concatenate([pre, s]).astype(np.float32)

    def modulate(self, payload: bytes, code_idx: int) -> np.ndarray:
        """Return the complex baseband burst (complex64) for one PHY frame."""
        cfg = self.cfg
        sym = self.burst_symbols(payload, code_idx)
        chips = (sym[:, None] * self.codes[code_idx][None, :]).ravel()
        up = np.zeros(chips.size * cfg.sps, dtype=np.float32)
        up[::cfg.sps] = chips
        x = np.convolve(up, self.h)                      # adds filter tails
        # scale so a single stream has RMS = amplitude / 2 (peak ~ amplitude)
        x *= (cfg.amplitude / 2) / np.sqrt(np.mean(x ** 2) + 1e-20)
        return x.astype(np.complex64)

    def burst_duration(self, n_bytes: int) -> float:
        nsym = self.cfg.preamble_bits + HEADER_BITS + 8 * n_bytes
        return nsym * self.cfg.samples_per_symbol / self.cfg.samp_rate


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
            st.pending.append([t_abs, cfo, metric, None, rec])
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
        t0_abs, cfo, metric, total, rec = p
        P, K = self.P, self.K
        nsym = total if total is not None else K + HEADER_BITS
        guard = 2 * self.cfg.sps + 8
        if t0_abs + nsym * P + guard > self.buf_start + self.buf.size:
            return "wait"
        res = self._demod(t0_abs - self.buf_start, cfo, st.code_idx, nsym)
        bits = res["bits"]
        if total is None:
            hdr = bits_to_bytes(bits[:HEADER_BITS])
            length = int.from_bytes(hdr[:2], "big")
            if crc8(hdr[:2]) != hdr[2] or not self.min_payload <= length <= self.cfg.max_phy_payload:
                self.stats["header_fail"] += 1
                self.on_reject("header_crc", st.code_idx)
                rec[1] = t0_abs + K * P                  # not a real burst: shrink its span
                return "done"
            p[3] = K + HEADER_BITS + 8 * length
            rec[1] = t0_abs + p[3] * P
            return "more"
        payload = bits_to_bytes(bits[HEADER_BITS:])
        h = hash(payload)
        if any(abs(t - t0_abs) < 2 * P and hh == h for t, hh in st.recent_payloads):
            self.stats["ghosts"] += 1                    # same burst decoded twice
            return "done"
        st.recent_payloads = st.recent_payloads[-15:] + [(t0_abs, h)]
        self.stats["frames"] += 1
        self.last_constellation = res["const"][-256:]
        frame = RxFrame(payload=payload, code_idx=st.code_idx, snr_db=res["snr_db"],
                        chip_snr_db=res["snr_db"] - 10 * math.log10(self.cfg.sf),
                        cfo_hz=res["cfo"], metric=metric, sample_index=t0_abs,
                        rx_time=time.time(), constellation=res["const"][-256:])
        self.on_frame(frame)
        return "done"

    def _demod(self, t0: int, cfo: float, code_idx: int, nsym: int) -> dict:
        cfg = self.cfg
        P, K, sps = self.P, self.K, cfg.sps
        code = self.codes[code_idx]
        d = max(1, sps // 2)                       # early/late spacing: half a chip
        lo = max(0, t0 - 4 * sps)
        hi = min(self.buf.size, t0 + nsym * P + 4 * sps)
        seg = self.buf[lo:hi]
        n = np.arange(lo - t0, hi - t0, dtype=np.float64)
        seg = (seg * np.exp(-2j * np.pi * cfo * n / cfg.samp_rate)).astype(np.complex64)
        # zero-pad so early/late taps near the edges never index out of range
        pad = P + 4 * sps
        seg = np.concatenate([np.zeros(pad, np.complex64), seg, np.zeros(pad, np.complex64)])
        base = t0 - lo + pad
        chips = np.arange(code.size) * sps                 # chip offsets inside one symbol
        code_c = code.astype(np.complex64)

        def despread(starts: np.ndarray) -> np.ndarray:
            """Despread many symbols at once: one gather + one mat-vec (releases the GIL)."""
            return seg[starts[:, None] + chips[None, :]] @ code_c

        # ---- preamble: phase & residual frequency fit -----------------------
        b = self.pre[code_idx]
        kk = np.arange(K)
        v = despread(base + kk * P) * b
        ph = np.unwrap(np.angle(v))
        slope, icpt = np.polyfit(kk, ph, 1)
        theta = float(icpt)
        omega = float(slope)                           # rad / symbol
        amp = float(np.mean(np.abs(v)))
        # ---- tracking loops, processed in blocks of W symbols ----------------
        # Despreading (the expensive part) is vectorised per block; only the
        # scalar PLL update runs per symbol.  The DLL updates once per block.
        alpha, beta = 0.08, 0.002                      # PLL gains (per symbol)
        tau = 0                                        # timing correction (samples)
        W, dll_thr = 16, 0.15
        out = np.empty(nsym, dtype=np.complex64)
        for k0 in range(0, nsym, W):
            ks = np.arange(k0, min(k0 + W, nsym))
            pos = base + ks * P + tau
            yp = despread(pos)
            e_acc = float(np.sum(np.abs(despread(pos - d)) ** 2))
            l_acc = float(np.sum(np.abs(despread(pos + d)) ** 2))
            for j, k in enumerate(ks):
                z = complex(yp[j]) * complex(math.cos(-theta), math.sin(-theta))
                out[k] = z
                ref = b[k] if k < K else (1.0 if z.real >= 0 else -1.0)
                err = math.atan2(z.imag * ref, z.real * ref)
                omega += beta * err
                theta += omega + alpha * err
            if ks.size == W:
                disc = (e_acc - l_acc) / (e_acc + l_acc + 1e-30)
                if disc > dll_thr:
                    tau -= 1
                elif disc < -dll_thr:
                    tau += 1
        hard = np.where(out.real >= 0, 1.0, -1.0)
        data = hard[K - 1:]                            # include last preamble symbol
        u = (data[1:] * data[:-1] < 0).astype(np.int8)  # differential decode
        # ---- SNR estimate ---------------------------------------------------
        zr = out.real * hard
        mu = float(np.mean(zr))
        noise = float(np.mean(out.imag ** 2) + np.var(zr)) / 2 + 1e-30
        snr = 10 * math.log10(max(mu * mu / (2 * noise), 1e-6))
        cfo_total = cfo + omega / (2 * np.pi) * cfg.symbol_rate
        const = out / (mu if mu > 0 else 1.0)
        return dict(bits=u, const=const, snr_db=snr, cfo=cfo_total, amp=amp)
