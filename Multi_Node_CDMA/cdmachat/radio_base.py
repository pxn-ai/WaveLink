"""Radio abstraction shared by the GNU Radio (ANTSDR) backend and the simulator.

The link layer only needs four things from a radio:
    send(code_idx, payload, urgent) -> estimated time the burst leaves the antenna
    now()                           -> clock used for ARQ timers
    rx_busy()                       -> True while a burst for us is being received
    on_frame callback               -> every decoded PHY frame
"""
from __future__ import annotations

import collections
import threading
import time
import numpy as np

from .config import PhyConfig, BROADCAST_CODE, code_index_for
from .modem import Modulator, Demodulator, RxFrame


class TxMixer:
    """Code-division multiplexer for the transmit side.

    One FIFO per spreading code.  Bursts on *different* codes are summed and
    transmitted simultaneously (multi-code CDMA), bursts on the same code are
    serialised.  Urgent bursts (ACKs) jump to the head of their code's queue.
    """

    def __init__(self, cfg: PhyConfig):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.queues: dict[int, collections.deque] = collections.defaultdict(collections.deque)
        self.active: dict[int, list] = {}          # code -> [samples, pos]
        self.sample_count = 0                      # samples emitted so far
        self.bursts_sent = 0
        self.busy_samples = 0

    def enqueue(self, code: int, samples: np.ndarray, urgent: bool = False) -> int:
        """Queue a burst; returns the sample index at which it will be finished."""
        with self.lock:
            q = self.queues[code]
            if urgent:
                q.appendleft(samples)
                ahead = [samples]
            else:
                q.append(samples)
                ahead = list(q)
            rem = 0
            if code in self.active:
                s, pos = self.active[code]
                rem = s.size - pos
            return self.sample_count + rem + sum(a.size for a in ahead)

    def pending(self) -> bool:
        with self.lock:
            return bool(self.active) or any(self.queues.values())

    def active_codes(self) -> list[int]:
        with self.lock:
            return list(self.active.keys())

    def read(self, n: int) -> tuple[np.ndarray, bool]:
        """Produce n output samples.  Returns (samples, any_signal)."""
        out = np.zeros(n, dtype=np.complex64)
        with self.lock:
            # start new bursts on idle codes
            for code, q in self.queues.items():
                if code not in self.active and q:
                    self.active[code] = [q.popleft(), 0]
                    self.bursts_sent += 1
            if not self.active:
                self.sample_count += n
                return out, False
            nstreams = 0
            for code in list(self.active):
                written = 0
                while written < n:
                    if code not in self.active:
                        q = self.queues.get(code)
                        if q:
                            self.active[code] = [q.popleft(), 0]
                            self.bursts_sent += 1
                        else:
                            break
                    s, pos = self.active[code]
                    k = min(n - written, s.size - pos)
                    out[written:written + k] += s[pos:pos + k]
                    written += k
                    pos += k
                    if pos >= s.size:
                        del self.active[code]
                    else:
                        self.active[code][1] = pos
                nstreams += 1
            if nstreams > 1:
                out /= np.sqrt(nstreams)
            mag = np.abs(out)
            over = mag > 0.95
            if over.any():
                out[over] *= 0.95 / mag[over]
            self.sample_count += n
            self.busy_samples += n
            return out, True


class RadioBase:
    """Common bookkeeping: modulator, demodulator, mixer, stats, spectrum."""

    name = "radio"

    def __init__(self, cfg: PhyConfig, node_addr: int):
        self.cfg = cfg
        self.addr = node_addr
        self.mod = Modulator(cfg)
        self.mixer = TxMixer(cfg)
        self.on_frame = lambda f: None
        self.on_reject = lambda reason, code: None
        self.demod = Demodulator(cfg, [code_index_for(node_addr), BROADCAST_CODE],
                                 on_frame=self._frame, on_reject=self._reject)
        self._spec_lock = threading.Lock()
        self._spec_snap = np.zeros(0, dtype=np.complex64)
        self._spec_t = 0.0
        self.last_frame: RxFrame | None = None
        self.counters = collections.Counter()

    # ---------------------------------------------------------------- link-layer API
    def now(self) -> float:
        return time.monotonic()

    def send(self, code_idx: int, payload: bytes, urgent: bool = False) -> float:
        burst = self.mod.modulate(payload, code_idx)
        done = self.mixer.enqueue(code_idx, burst, urgent)
        self.counters["tx_bursts"] += 1
        self.counters["tx_bytes"] += len(payload)
        return self._sample_to_time(done)

    def rx_busy(self) -> bool:
        return self.demod.busy()

    def tx_busy(self) -> bool:
        return self.mixer.pending()

    def airtime(self, n_bytes: int) -> float:
        return self.mod.burst_duration(n_bytes)

    # ---------------------------------------------------------------- hooks for backends
    def _sample_to_time(self, sample_idx: int) -> float:
        return self.now() + (sample_idx - self.mixer.sample_count) / self.cfg.samp_rate

    def _frame(self, f: RxFrame):
        self.counters["rx_frames"] += 1
        self.on_frame(f)

    def _reject(self, reason: str, code: int):
        self.counters["rx_" + reason] += 1
        self.on_reject(reason, code)

    def _rx_samples(self, x: np.ndarray):
        """Backends call this with every received chunk."""
        t = time.monotonic()
        if t - self._spec_t > 0.25 and x.size >= 4096:
            with self._spec_lock:
                self._spec_snap = x[-4096:].copy()
                self._spec_t = t
        self.demod.process(x)

    # ---------------------------------------------------------------- dashboard data
    def spectrum(self, nfft: int = 256) -> dict:
        with self._spec_lock:
            x = self._spec_snap
        if x.size < nfft:
            return dict(freqs=[], psd=[])
        segs = x[: (x.size // nfft) * nfft].reshape(-1, nfft)
        w = np.hanning(nfft).astype(np.float32)
        P = np.mean(np.abs(np.fft.fft(segs * w, axis=1)) ** 2, axis=0) / np.sum(w ** 2)
        P = np.fft.fftshift(10 * np.log10(P + 1e-12))
        f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / self.cfg.samp_rate))
        return dict(freqs=(f / 1e3).round(1).tolist(), psd=P.round(1).tolist())

    def status(self) -> dict:
        d = dict(self.demod.stats)
        d.update(self.counters)
        d["tx_active_codes"] = self.mixer.active_codes()
        lf = self.last_frame
        if lf is not None:
            d.update(last_snr_db=round(lf.snr_db, 1), last_chip_snr_db=round(lf.chip_snr_db, 1),
                     last_cfo_hz=round(lf.cfo_hz), last_metric=round(lf.metric, 2),
                     constellation=[[round(float(c.real), 2), round(float(c.imag), 2)]
                                    for c in lf.constellation[-160:]])
        return d

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        raise NotImplementedError

    def stop(self):
        pass
