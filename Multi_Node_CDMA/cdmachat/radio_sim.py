"""Software 'air': N simulated stations sharing one 2.5 GHz channel.

Models, per link i->j: path gain, propagation/processing delay, carrier
frequency offset from each node's LO error (ppm of 2.5 GHz) and a continuous
phase, plus AWGN at every receiver and leakage of a node's own transmitter into
its own receiver.  Time is *simulated* (sample counter), so ARQ timers stay
correct even if the PC is slower than real time.
"""
from __future__ import annotations

import math
import threading
import time
import numpy as np

from .config import PhyConfig
from .radio_base import RadioBase


class SimRadio(RadioBase):
    name = "sim"

    def __init__(self, cfg: PhyConfig, addr: int, air: "SimAir"):
        super().__init__(cfg, addr)
        self.air = air

    def now(self) -> float:
        return self.air.time

    def start(self):
        self.air.start()

    def stop(self):
        self.air.stop()


class SimAir:
    def __init__(self, cfg: PhyConfig, ebn0_db: float = 18.0, self_leak_db: float = 3.0,
                 lo_ppm: float = 2.0, max_delay: int = 400, chunk_ms: float = 10.0,
                 speed: float = 1.0, seed: int = 1):
        self.cfg = cfg
        self.rng = np.random.default_rng(seed)
        self.nodes: list[SimRadio] = []
        self.lo_ppm = lo_ppm
        self.max_delay = max_delay
        self.N = int(cfg.samp_rate * chunk_ms / 1000)
        self.speed = speed
        self.time = 0.0
        self.self_leak_db = self_leak_db
        self.gain_db: dict[tuple[int, int], float] = {}
        self.delay: dict[tuple[int, int], int] = {}
        self.phase: dict[tuple[int, int], float] = {}
        self.lo_off: dict[int, float] = {}
        self._hist: dict[int, np.ndarray] = {}
        self._running = False
        self._thread = None
        self.cpu_load = 0.0
        self.set_ebn0(ebn0_db)

    # ---------------------------------------------------------------- setup
    def set_ebn0(self, ebn0_db: float):
        """Noise level such that a 0 dB link has the given Eb/N0."""
        self.ebn0_db = float(ebn0_db)
        rms = self.cfg.amplitude / 2
        n0 = rms ** 2 * self.cfg.sps * self.cfg.sf / 10 ** (ebn0_db / 10)
        self.noise_std = math.sqrt(n0 / 2)          # per real dimension

    def add_node(self, addr: int) -> SimRadio:
        r = SimRadio(self.cfg, addr, self)
        for other in self.nodes:
            for a, b in ((addr, other.addr), (other.addr, addr)):
                self.gain_db[(a, b)] = float(self.rng.uniform(-3, 3))
                self.delay[(a, b)] = int(self.rng.integers(0, self.max_delay))
                self.phase[(a, b)] = float(self.rng.uniform(0, 2 * np.pi))
        self.gain_db[(addr, addr)] = self.self_leak_db
        self.delay[(addr, addr)] = 0
        self.phase[(addr, addr)] = 0.0
        self.lo_off[addr] = float(self.rng.uniform(-1, 1)) * self.lo_ppm * 1e-6 * self.cfg.center_freq
        self._hist[addr] = np.zeros(self.max_delay, dtype=np.complex64)
        self.nodes.append(r)
        return r

    def links(self) -> list[dict]:
        out = []
        for (a, b), g in sorted(self.gain_db.items()):
            if a == b:
                continue
            out.append(dict(tx=a, rx=b, gain_db=round(g, 1), ebn0_db=round(self.ebn0_db + g, 1),
                            cfo_hz=round(self.lo_off[a] - self.lo_off[b]), delay=self.delay[(a, b)]))
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

    def _loop(self):
        fs = self.cfg.samp_rate
        N = self.N
        wall0 = time.monotonic()
        sim0 = self.time
        prev_active = True
        n = np.arange(N, dtype=np.float64)
        while self._running:
            t_cpu = time.monotonic()
            txs, active = {}, False
            for r in self.nodes:
                x, on = r.mixer.read(N)
                txs[r.addr] = (x, on or bool(np.any(self._hist[r.addr])))
                active |= on
            if not active and not prev_active:
                for r in self.nodes:
                    r.demod.skip(N)
                for a in self._hist:
                    self._hist[a][:] = 0
            else:
                for rx in self.nodes:
                    y = (self.rng.standard_normal(N) + 1j * self.rng.standard_normal(N)) * self.noise_std
                    for tx in self.nodes:
                        x, on = txs[tx.addr]
                        if not on:
                            continue
                        key = (tx.addr, rx.addr)
                        d = self.delay[key]
                        seg = np.concatenate([self._hist[tx.addr][self.max_delay - d:], x])[:N] if d else x
                        f = self.lo_off[tx.addr] - self.lo_off[rx.addr]
                        ph = self.phase[key]
                        g = 10 ** (self.gain_db[key] / 20)
                        y += g * seg * np.exp(1j * (ph + 2 * np.pi * f * n / fs))
                        self.phase[key] = (ph + 2 * np.pi * f * N / fs) % (2 * np.pi)
                    rx._rx_samples(y.astype(np.complex64))
                for a, (x, _) in txs.items():
                    h = self._hist[a]
                    if N >= self.max_delay:
                        h[:] = x[-self.max_delay:]
                    else:
                        h[:] = np.concatenate([h[N:], x])
            prev_active = active
            self.time += N / fs
            dt = time.monotonic() - t_cpu
            self.cpu_load = 0.9 * self.cpu_load + 0.1 * (dt / (N / fs))
            # pace to (speed x) real time; if we are slower, just run flat out
            ahead = (self.time - sim0) / self.speed - (time.monotonic() - wall0)
            if ahead > 0:
                time.sleep(min(ahead, 0.05))
            elif ahead < -1.0:
                wall0 = time.monotonic() - (self.time - sim0) / self.speed
