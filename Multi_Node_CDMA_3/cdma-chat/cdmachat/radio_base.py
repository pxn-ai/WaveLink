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


class StreamingRadio(RadioBase):
    """A radio fed by an external sample pump (a GNU Radio flowgraph).

    The flowgraph's TX block calls tx_read(n) and its RX block calls rx_push(x);
    demodulation runs in a worker thread so the GNU Radio scheduler never waits
    for Python DSP.  Used by both the command-line flowgraph (radio_gr.GrRadio)
    and the GNU Radio Companion blocks (grc_glue).
    """

    def __init__(self, cfg: PhyConfig, addr: int, rx_chunk: int = 32768, tx_buffer: int = 131072):
        super().__init__(cfg, addr)
        # TX driver buffer.  Must cover the longest time Python may be busy
        # elsewhere (decoding a frame, GC, the web server ...); if it runs dry
        # mid-burst the frame goes out with a hole -> CRC-32 failure.
        self.tx_buffer = tx_buffer
        self.tx_latency = 2 * tx_buffer / cfg.samp_rate + 0.01
        self._rxq: collections.deque = collections.deque()
        self.rx_queue_max = max(50, int(2.0 * cfg.samp_rate / rx_chunk))      # ~2 s of backlog
        self._tx_t0 = None
        self._tx_made = 0
        self._tx_last_active = 0.0
        self._worker_running = False

    # MAC timers: wall clock + the TX pipeline latency for "burst on air" estimates
    def _sample_to_time(self, sample_idx: int) -> float:
        return super()._sample_to_time(sample_idx) + self.tx_latency

    # ---------------------------------------------------------------- called by GR blocks
    def tx_read(self, n: int) -> np.ndarray:
        x, on = self.mixer.read(n)
        now = time.monotonic()
        if on:
            self._tx_last_active = now
        # Underrun watch: the DAC consumes fs samples/s.  If we have produced fewer
        # samples than wall-clock time requires, the device ran dry and sent silence.
        if self._tx_t0 is None:
            self._tx_t0 = now
        self._tx_made += n
        behind = (now - self._tx_t0) * self.cfg.samp_rate - self._tx_made
        if behind > 0.25 * self.tx_buffer:
            self.counters["tx_underruns"] += 1
            self._tx_t0 = now - self._tx_made / self.cfg.samp_rate
        return x

    def rx_push(self, x: np.ndarray):
        if len(self._rxq) < self.rx_queue_max:
            self._rxq.append(np.array(x, dtype=np.complex64, copy=True))
        else:
            # can't keep up: drop the samples but tell the demodulator how many, so
            # it doesn't splice two pieces of signal into one corrupted frame
            self.counters["rx_overflow_chunks"] += 1
            self._rxq.append(len(x))

    # ---------------------------------------------------------------- worker
    def start_worker(self):
        if self._worker_running:
            return
        self._worker_running = True
        threading.Thread(target=self._rx_worker, name=f"rx{self.addr}", daemon=True).start()

    def stop_worker(self):
        self._worker_running = False

    def _rx_worker(self):
        q = self._rxq
        while self._worker_running:
            if not q:
                time.sleep(0.002)
                continue
            x = q.popleft()
            if isinstance(x, int):          # samples dropped at the sink -> discontinuity
                self.demod.skip(x)
                continue
            self._rx_samples(x)
