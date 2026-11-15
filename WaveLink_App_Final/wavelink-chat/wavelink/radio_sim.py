"""Software 'air': N simulated stations sharing one 2.5 GHz channel.

Models, per link i->j: path gain, propagation/processing delay, carrier frequency offset from
each node's LO error (ppm of 2.5 GHz) and a continuous phase, plus AWGN at every receiver and
leakage of a node's own transmitter into its own receiver.  Time is *simulated* (sample
counter), so ARQ timers stay correct even if the PC is slower than real time.

What a real radio adds, and the TD-CDMA time slots have to cope with, is modelled too:
  * every receiver numbers its samples from an arbitrary start (rx_offset), so the TX -> RX
    offset D of each station is unknown until it has heard itself;
  * sample-clock error (ppm per node): each station's TX and RX run on its own clock, the air on
    true time; signals are resampled (8-tap windowed sinc) between the clocks;
  * faults: a TX underrun (the station's transmit stream slips by n samples from then on), a lost
    RX buffer (n samples vanish without the receiver knowing: its sample count slips), a link cut
    (out of range), bursts of wideband interference.
The air calls every radio's tick hooks before each chunk, in its own thread, so the link layer's
scheduler runs in step with the simulated time at any simulation speed (and deterministically).
"""
from __future__ import annotations

import math
import threading
import time
import numpy as np

from .config import PhyConfig
from .radio_base import RadioBase
from .modem import _FD_BANK, _FD_PHASES, _FD_TAPS


class SimRadio(RadioBase):
    name = "sim"
    drives_mac = True                     # the air thread runs the link layer (see LinkLayer.start)

    def __init__(self, cfg: PhyConfig, addr: int, air: "SimAir"):
        super().__init__(cfg, addr)
        self.air = air
        self.tick_hooks: list = []        # fn() called by the air before each chunk

    def now(self) -> float:
        return self.air.time

    def sample_at(self, t: float) -> int:
        # every station's mixer is read one chunk per air chunk
        return self.mixer.sample_count + int(round((t - self.air.time) * self.cfg.samp_rate))

    def tx_lead(self) -> int:
        """The air reads each mixer one chunk ahead of mixing it."""
        return 2 * self.air.N

    def timing_faults(self) -> int:
        return self.counters.get("tx_underruns", 0) + self.counters.get("rx_overflow_chunks", 0)

    def start(self):
        self.air.start()

    def stop(self):
        self.air.stop()


class _Resampler:
    """y[i] = x(pos0 + i * ratio): reads a stream that is fed in pieces at fractional positions
    (8-tap windowed-sinc interpolation, 1/64 sample)."""

    def __init__(self, ratio: float):
        self.ratio = float(ratio)
        self.pos = 0.0                                   # input position of the next output sample
        self.buf = np.zeros(_FD_TAPS, dtype=np.complex64)
        self.base = -_FD_TAPS // 2                       # input index of buf[0]

    def need(self, n_out: int) -> int:
        """Input index (exclusive) required to produce n_out more samples."""
        last = self.pos + (n_out - 1) * self.ratio
        return int(math.floor(last)) + _FD_TAPS // 2 + 1

    def feed(self, x: np.ndarray):
        self.buf = np.concatenate([self.buf, x.astype(np.complex64)])

    def available(self) -> int:
        return self.base + self.buf.size

    def produce(self, n_out: int) -> np.ndarray:
        p = self.pos + np.arange(n_out) * self.ratio
        i0 = np.floor(p).astype(np.int64)
        ph = np.rint((p - i0) * _FD_PHASES).astype(np.int64)   # 0..64 (row 64 = one sample later)
        first = i0 - self.base - (_FD_TAPS // 2 - 1)            # buffer index of each output's first tap
        # A clock error of a few ppm keeps the phase for hundreds of samples: filter those runs as
        # 8 shifted slices instead of gathering 8 inputs per output sample.
        brk = np.flatnonzero((ph[1:] != ph[:-1]) | (first[1:] - first[:-1] != 1)) + 1
        if brk.size < n_out // 64:
            y = np.empty(n_out, dtype=np.complex64)
            bounds = np.concatenate(([0], brk, [n_out]))
            for s, e in zip(bounds[:-1], bounds[1:]):
                taps = _FD_BANK[ph[s]]
                r0, L = int(first[s]), int(e - s)
                acc = self.buf[r0:r0 + L] * taps[0]
                for t in range(1, _FD_TAPS):
                    acc += self.buf[r0 + t:r0 + t + L] * taps[t]
                y[s:e] = acc
        else:
            taps = _FD_BANK[ph]                            # (n, 8): offsets -3..4
            idx = first[:, None] + np.arange(_FD_TAPS)[None, :]
            y = np.einsum("ij,ij->i", self.buf[idx], taps).astype(np.complex64)
        self.pos += n_out * self.ratio
        drop = int(math.floor(self.pos)) - _FD_TAPS - self.base
        if drop > 0:
            self.buf = self.buf[drop:]
            self.base += drop
        return y


class SimAir:
    def __init__(self, cfg: PhyConfig, ebn0_db: float = 18.0, self_leak_db: float = 3.0,
                 lo_ppm: float = 2.0, max_delay: int = 400, chunk_ms: float = 10.0,
                 speed: float = 1.0, seed: int = 1, rx_offsets: bool = True):
        self.cfg = cfg
        self.rng = np.random.default_rng(seed)
        self.nodes: list[SimRadio] = []
        self.lo_ppm = lo_ppm
        self.max_delay = max_delay
        self.N = int(cfg.samp_rate * chunk_ms / 1000)
        self.speed = speed
        self.time = 0.0
        self.sample = 0                               # air (true time) sample index
        self.self_leak_db = self_leak_db
        self.rx_offsets = rx_offsets
        self.gain_db: dict[tuple[int, int], float] = {}
        self.delay: dict[tuple[int, int], int] = {}
        self.phase: dict[tuple[int, int], float] = {}
        self.cut: set[tuple[int, int]] = set()        # links with no signal at all (out of range)
        self.lo_off: dict[int, float] = {}
        self.ppm: dict[int, float] = {}
        self._hist: dict[int, np.ndarray] = {}
        self._rot: dict[tuple[int, int], np.ndarray] = {}
        self._txq: dict[int, np.ndarray] = {}         # TX underrun: samples held back
        self._rx_drop: dict[int, list] = {}           # [samples still to drop, receiver knows?]
        self._tx_rs: dict[int, _Resampler] = {}       # clock model: node clock -> air
        self._rx_rs: dict[int, _Resampler] = {}       # air -> node clock
        self._rx_pending: dict[int, np.ndarray] = {}
        self.interference = None                      # dict(level_db, duty, burst_ms)
        self._intf_on = False
        # tests: transmissions of two stations at the same time (time slots must never overlap) that some
        # third station hears both of: [(air time, station a, station b, samples)]
        self.overlaps: list = []
        self._running = False
        self._thread = None
        self.cpu_load = 0.0
        self.lock = threading.Lock()
        self.set_ebn0(ebn0_db)

    # ---------------------------------------------------------------- setup
    def set_ebn0(self, ebn0_db: float):
        """Noise level such that a 0 dB link has the given Eb/N0."""
        self.ebn0_db = float(ebn0_db)
        rms = self.cfg.amplitude / 2
        n0 = rms ** 2 * self.cfg.sps * self.cfg.sf / 10 ** (ebn0_db / 10)
        self.noise_std = math.sqrt(n0 / 2)          # per real dimension

    def add_node(self, addr: int, ppm: float = 0.0) -> SimRadio:
        """A station with address addr.  Its links are keyed by addr (or, for a second station with the same
        address, by addr + 1000 ...: a misconfigured network) - r.key."""
        r = SimRadio(self.cfg, addr, self)
        key = addr
        while key in self._hist:
            key += 1000
        r.key = key
        addr = key
        for other in self.nodes:
            for a, b in ((addr, other.key), (other.key, addr)):
                self.gain_db[(a, b)] = float(self.rng.uniform(-3, 3))
                self.delay[(a, b)] = int(self.rng.integers(0, self.max_delay))
                self.phase[(a, b)] = float(self.rng.uniform(0, 2 * np.pi))
        self.gain_db[(addr, addr)] = self.self_leak_db
        self.delay[(addr, addr)] = 0
        self.phase[(addr, addr)] = 0.0
        self.lo_off[addr] = float(self.rng.uniform(-1, 1)) * self.lo_ppm * 1e-6 * self.cfg.center_freq
        self._hist[addr] = np.zeros(self.max_delay, dtype=np.complex64)
        self._txq[addr] = np.zeros(0, dtype=np.complex64)
        self.ppm[addr] = float(ppm)
        if self.rx_offsets:                        # the receiver's sample counter starts anywhere
            r.demod.set_start_index(int(self.rng.integers(-2 ** 30, 2 ** 30)))
        self.nodes.append(r)
        self._rot.clear()
        self._setup_clocks()
        return r

    def set_ppm(self, addr: int, ppm: float):
        """Sample-clock error of one station (its TX and RX); set before start()."""
        self.ppm[addr] = float(ppm)
        self._setup_clocks()

    def _setup_clocks(self):
        self._tx_rs.clear()
        self._rx_rs.clear()
        if not any(self.ppm.values()):
            return
        for r in self.nodes:
            e = self.ppm[r.key] * 1e-6
            self._tx_rs[r.key] = _Resampler(1.0 + e)            # air sample i <- node sample i(1+e)
            self._rx_rs[r.key] = _Resampler(1.0 / (1.0 + e))    # node sample j <- air sample j/(1+e)
            self._rx_pending[r.key] = np.zeros(0, dtype=np.complex64)

    def set_link(self, a: int, b: int, gain_db: float | None = None, both: bool = True, cut: bool | None = None):
        """Path a -> b (and b -> a): a gain in dB, and/or cut=True (no signal: out of range)."""
        for key in ((a, b), (b, a)) if both else ((a, b),):
            if gain_db is not None:
                self.gain_db[key] = float(gain_db)
            if cut is True:
                self.cut.add(key)
            elif cut is False:
                self.cut.discard(key)

    def inject_tx_gap(self, addr: int, n: int):
        """TX underrun: the station's transmit stream reaches the air n samples later from now on."""
        with self.lock:
            self._txq[addr] = np.concatenate([np.zeros(int(n), np.complex64), self._txq[addr]])
        r = next((x for x in self.nodes if x.key == addr), None)
        if r is not None:
            r.counters["tx_underruns"] += 1             # (the driver notices an underrun)

    def inject_rx_drop(self, addr: int, n: int, known: bool = False):
        """Lost RX buffer: the next n samples never reach the receiver.  known=False: the receiver does
        not notice (its sample count slips by n); True: it is told (demod.skip, count stays right)."""
        with self.lock:
            self._rx_drop[addr] = [int(n), bool(known)]
        r = next((x for x in self.nodes if x.key == addr), None)
        if r is not None and known:
            r.counters["rx_overflow_chunks"] += 1

    def links(self) -> list[dict]:
        out = []
        for (a, b), g in sorted(self.gain_db.items()):
            if a == b:
                continue
            out.append(dict(tx=a, rx=b, gain_db=round(g, 1), ebn0_db=round(self.ebn0_db + g, 1),
                            cfo_hz=round(self.lo_off[a] - self.lo_off[b]), delay=self.delay[(a, b)],
                            cut=(a, b) in self.cut))
        return out

    # ---------------------------------------------------------------- run
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="sim-air", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _rotation(self, key, f: float) -> np.ndarray:
        rot = self._rot.get(key)
        if rot is None:
            n = np.arange(self.N, dtype=np.float64)
            rot = self._rot[key] = np.exp(2j * np.pi * f * n / self.cfg.samp_rate).astype(np.complex64)
        return rot

    def _tx_chunk(self, r: SimRadio, N: int) -> tuple[np.ndarray, bool]:
        """N air samples of station r's transmitter (its clock, its TX slips)."""
        rs = self._tx_rs.get(r.key)
        if rs is None:
            x, on = r.mixer.read(N)
        else:
            k = max(0, rs.need(N) - rs.available())
            x, on = r.mixer.read(k) if k else (np.zeros(0, np.complex64), False)
        q = self._txq[r.key]
        if q.size:                                       # held-back samples (after an underrun) go first
            x = np.concatenate([q, x])
            on = on or bool(np.any(q))
            keep = x.size - (N if rs is None else k)
            self._txq[r.key] = x[x.size - keep:] if keep > 0 else np.zeros(0, np.complex64)
            x = x[:x.size - keep] if keep > 0 else x
        if rs is not None:
            rs.feed(x)
            if not on and not np.any(rs.buf):
                rs.produce(N)
                return np.zeros(N, np.complex64), False
            return rs.produce(N), True
        return x, on

    def _rx_deliver(self, rx: SimRadio, y: np.ndarray):
        """Hand received air samples to station rx (its clock, lost buffers)."""
        rs = self._rx_rs.get(rx.key)
        if rs is not None:
            rs.feed(y)
            n_out = max(0, int(math.floor((rs.available() - (_FD_TAPS // 2 + 1) - rs.pos) / rs.ratio)) + 1)
            while n_out and rs.need(n_out) > rs.available():
                n_out -= 1
            y = rs.produce(n_out) if n_out else np.zeros(0, np.complex64)
        drop = self._rx_drop.get(rx.key)
        if drop and drop[0] > 0 and y.size:
            k = min(drop[0], y.size)
            drop[0] -= k
            if drop[1]:
                rx.demod.skip(k)
            y = y[k:]
        if y.size:
            rx._rx_samples(y)

    def _loop(self):
        fs = self.cfg.samp_rate
        N = self.N
        wall0 = time.monotonic()
        sim0 = self.time
        prev_active = True
        while self._running:
            t_cpu = time.monotonic()
            for r in self.nodes:                          # link layers schedule the next bursts
                for fn in r.tick_hooks:
                    try:
                        fn()
                    except Exception as e:                # never stop the air
                        print("sim hook error:", repr(e))
            txs, active = {}, False
            with self.lock:
                for r in self.nodes:
                    x, on = self._tx_chunk(r, N)
                    txs[r.key] = (x, on or bool(np.any(self._hist[r.key])))
                    active |= on
            self._note_overlaps(txs)
            intf = self._interference(N)
            clocked = bool(self._rx_rs)
            if not active and not prev_active and intf is None and not clocked and not self._rx_drop_pending():
                for r in self.nodes:
                    r.demod.skip(N)
                for a in self._hist:
                    self._hist[a][:] = 0
            else:
                for rx in self.nodes:
                    y = (self.rng.standard_normal(N) + 1j * self.rng.standard_normal(N)).astype(np.complex64)
                    y *= np.float32(self.noise_std)
                    if intf is not None:
                        y += intf
                    for tx in self.nodes:
                        x, on = txs[tx.key]
                        key = (tx.key, rx.key)
                        if not on or key in self.cut:
                            continue
                        d = self.delay[key]
                        seg = np.concatenate([self._hist[tx.key][self.max_delay - d:], x])[:N] if d else x
                        f = self.lo_off[tx.key] - self.lo_off[rx.key]
                        ph = self.phase[key]
                        g = 10 ** (self.gain_db[key] / 20)
                        y += seg * (self._rotation(key, f) * np.complex64(g * np.exp(1j * ph)))
                        self.phase[key] = (ph + 2 * np.pi * f * N / fs) % (2 * np.pi)
                    with self.lock:
                        self._rx_deliver(rx, y)
                for a, (x, _) in txs.items():
                    h = self._hist[a]
                    if N >= self.max_delay:
                        h[:] = x[-self.max_delay:]
                    else:
                        h[:] = np.concatenate([h[N:], x])
            prev_active = active
            self.sample += N
            self.time += N / fs
            dt = time.monotonic() - t_cpu
            self.cpu_load = 0.9 * self.cpu_load + 0.1 * (dt / (N / fs))
            # pace to (speed x) real time; if we are slower, just run flat out
            ahead = (self.time - sim0) / self.speed - (time.monotonic() - wall0)
            if ahead > 0:
                time.sleep(min(ahead, 0.05))
            elif ahead < -1.0:
                wall0 = time.monotonic() - (self.time - sim0) / self.speed

    def _note_overlaps(self, txs: dict):
        busy = {k: np.abs(x) > 1e-7 for k, (x, on) in txs.items() if on and x.size and np.any(x)}
        if len(busy) < 2:
            return
        keys = sorted(busy)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                n = int(np.count_nonzero(busy[a] & busy[b]))
                if n <= 64:                               # (filter tails)
                    continue
                if any(r.key not in (a, b) and (a, r.key) not in self.cut and (b, r.key) not in self.cut
                       for r in self.nodes):
                    self.overlaps.append((round(self.time, 3), a, b, n))

    def _rx_drop_pending(self) -> bool:
        return any(v[0] > 0 for v in self._rx_drop.values())

    def _interference(self, N: int):
        """Bursty wideband interference at every receiver: on/off periods of mean burst_ms, on for a
        fraction duty of the time, power level_db relative to the signal of a 0 dB link."""
        p = self.interference
        if not p:
            return None
        chunk_ms = 1000 * N / self.cfg.samp_rate
        mean_on = max(chunk_ms, float(p.get("burst_ms", 20)))
        duty = min(0.95, max(0.01, float(p.get("duty", 0.2))))
        mean_off = mean_on * (1 - duty) / duty
        if self._intf_on:
            self._intf_on = self.rng.random() > chunk_ms / mean_on
        else:
            self._intf_on = self.rng.random() < chunk_ms / mean_off
        if not self._intf_on:
            return None
        rms = self.cfg.amplitude / 2 * 10 ** (float(p.get("level_db", 0)) / 20)
        return ((self.rng.standard_normal(N) + 1j * self.rng.standard_normal(N)) * (rms / math.sqrt(2))).astype(np.complex64)
