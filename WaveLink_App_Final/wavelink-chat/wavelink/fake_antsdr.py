"""Fake ANTSDR E200s: GNU Radio runs every station's real flowgraph, the radios are simulated.

Each station runs exactly what it runs on its PC with a real ANTSDR - `python3 -m wavelink run` and
the GNU Radio flowgraph grc/cdma_phy.grc - only the flowgraph's two radio blocks are stand-ins.
A `fake:` URI makes the app install them as `gnuradio.iio` before the flowgraph is loaded:

    python3 -m wavelink fake-air                                          (the air between them)
    python3 -m wavelink run --id 1 --name Alice --uri fake: --port 8081
    python3 -m wavelink run --id 2 --name Bob   --uri fake: --port 8082
    python3 -m wavelink run --id 3 --name Carol --uri fake: --port 8083

Towards GNU Radio the stand-ins behave like gr-iio's FMCOMMS2/Pluto Sink and Source (same
arguments, same setters):
  * Sink: takes whole buffers of `buffer_size` samples (set_output_multiple) and queues each in one
    of 4 kernel buffers (libiio's default) that the DAC plays at the sample rate; work() blocks
    while all 4 are queued, as iio_buffer_push() does.  The fc32 -> int16 conversion clips samples
    beyond full scale.  If the queue runs dry the DAC sends zeros (an underrun) and everything after
    it goes out later.
  * Source: hands out whole buffers of `buffer_size` samples as the ADC fills them, as
    iio_buffer_refill() does.  Samples the PC hasn't fetched within 4 buffers are lost (an overflow):
    the receive stream slips.  The ADC clips at full scale.
The air (FakeAir, a process of its own) plays every station's DAC and fills every ADC on the PC's
clock.  Per link: a path gain, a delay, a constant phase and the carrier offset between the two
LOs; a station's own transmitter leaks into its own receiver (+25 dB over the others: TX and RX
antennas side by side); AWGN at every receiver; optionally a sample-clock error per station
(ppm, its TX and RX), bursty interference, cut links and injected faults (TX underruns, lost RX
buffers).  Stations and air exchange samples through shared memory: SHM_DIR/wavelink-<air>-<slot>
(see _shm_dir()), a header and two rings of 2^20 samples per station.  The air also records every
time two stations transmit at once and some third station hears both (TD-CDMA slots must never overlap).

A slot is where one station's radio plugs in; by default the station's address (`fake:` = air
"wavelink", slot = --id).  `fake:lab/5` plugs into slot 5 of air "lab" (e.g. a second station
with the same address).

A pulled cable (FakeAir.set_unplugged, control command {"cmd": "unplug", "slot": N, "on": true}):
no samples either way, as when the ANTSDR's Ethernet link is gone.  The radio blocks wait, as
libiio's buffer refill and push do, and give up after IIO_TIMEOUT_S (libiio's network timeout):
they end (work() returns -1) and never run again.  Radio blocks made while the cable is out fail
("Unable to create context"); made after it is back they start fresh streams, as a new libiio
context does.  A cable back within IIO_TIMEOUT_S: the streams go on, with a gap (TX underrun, lost
receive samples).
"""
from __future__ import annotations

import collections
import json
import math
import mmap
import os
import random
import socket
import sys
import tempfile
import threading
import time
import types

import numpy as np


def _shm_dir() -> str:
    """The folder that holds the slot files.  The air and every station work it out on their own, in
    separate processes, so it must come out the same in each of them.  (Linux has /dev/shm, a RAM disk;
    macOS has none.)"""
    d = os.environ.get("WAVELINK_SHM_DIR")
    if d:
        return d
    if os.path.isdir("/dev/shm"):
        return "/dev/shm"
    return tempfile.gettempdir()                    # macOS: the per-user $TMPDIR, the same in every terminal


SHM_DIR = _shm_dir()
MAGIC = 0x574C414952                 # "WLAIR"
VERSION = 1
HDR = 64                             # int64 header fields
RING = 1 << 20                       # samples per ring: 262 ms at 4 MS/s
KERNEL_BUFFERS = 4                   # libiio's default number of DMA buffers per direction
SLOTS = 6

# header fields
(H_MAGIC, H_VER, H_RING, H_TXW, H_TXR, H_RXW, H_RXR, H_GEN, H_ACK, H_BEAT, H_AIRBEAT, H_TXATT,
 H_RXGAIN, H_FREQ, H_RATE, H_TXB, H_RXB, H_RXON, H_UNDER, H_UNDER_S, H_OVER, H_OVER_S, H_PID,
 H_AIRCLK, H_CLIP_TX, H_AIRWALL, H_SPEED, H_BLOCK, H_UNPLUG) = range(29)

IIO_TIMEOUT_S = 5.0                  # libiio's network timeout: a buffer refill / push waiting this long fails
CONNECT_FAIL_S = 1.0                 # a context to a radio that can't be reached fails after about this long


def shm_path(air: str, slot: int) -> str:
    return os.path.join(SHM_DIR, f"wavelink-{air}-{int(slot)}")


def parse_uri(uri: str):
    """'fake:[air][/slot]' -> (air name, slot or None)."""
    rest = uri[len("fake:"):] if uri.startswith("fake:") else uri
    name, _, slot = rest.partition("/")
    return (name or "wavelink"), (int(slot) if slot else None)


class _Shm:
    """One station's shared memory: header + TX ring (station -> air) + RX ring (air -> station)."""

    def __init__(self, path: str, create: bool = False, ring: int = RING):
        size = HDR * 8 + 2 * ring * 8
        if create:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            os.ftruncate(fd, size)
        else:
            fd = os.open(path, os.O_RDWR)
            size = os.fstat(fd).st_size
        try:
            self.mm = mmap.mmap(fd, size)
        finally:
            os.close(fd)
        self.hdr = np.frombuffer(self.mm, dtype=np.int64, count=HDR, offset=0)
        if create:
            self.hdr[:] = 0
            self.hdr[H_RING] = ring
            self.hdr[H_VER] = VERSION
            self.hdr[H_MAGIC] = MAGIC                 # (last: a station never sees half a header)
        ring = int(self.hdr[H_RING])
        if self.hdr[H_MAGIC] != MAGIC or self.hdr[H_VER] != VERSION or size < HDR * 8 + 2 * ring * 8:
            raise RuntimeError(f"{path} is not a WaveLink fake-air slot (or from another version)")
        self.ring = ring
        self.tx = np.frombuffer(self.mm, dtype=np.complex64, count=ring, offset=HDR * 8)
        self.rx = np.frombuffer(self.mm, dtype=np.complex64, count=ring, offset=HDR * 8 + ring * 8)


def _ring_write(ring: np.ndarray, idx: int, x: np.ndarray):
    R = ring.size
    i = int(idx) % R
    n = x.size
    k = min(n, R - i)
    ring[i:i + k] = x[:k]
    if k < n:
        ring[:n - k] = x[k:]


def _ring_read(ring: np.ndarray, idx: int, n: int, out: np.ndarray):
    R = ring.size
    i = int(idx) % R
    k = min(n, R - i)
    out[:k] = ring[i:i + k]
    if k < n:
        out[k:n] = ring[:n - k]


# ============================================================================ station side
_STATIONS: dict = {}
_LOCK = threading.Lock()
_DEFAULT_SLOT = [1]


class _StationLink:
    """This station's end of its shared memory (the sink and source of one flowgraph share it)."""

    @classmethod
    def get(cls, uri: str) -> "_StationLink":
        air, slot = parse_uri(uri)
        if slot is None:
            slot = _DEFAULT_SLOT[0]
        path = shm_path(air, slot)
        with _LOCK:
            st = _STATIONS.get(path)
            if st is None:
                st = _STATIONS[path] = cls(path, air, slot)
        return st

    def __init__(self, path: str, air: str, slot: int):
        if not os.path.exists(path):
            raise RuntimeError(f"fake ANTSDR: no fake air '{air}' with a slot {slot} ({path} is missing). "
                               f"Start it first: python3 -m wavelink fake-air --name {air}")
        self.shm = _Shm(path)
        self.air, self.slot = air, slot
        self.fs = 0
        self._warned = False
        self.air_lost = 0                              # buffers dropped / zeros made up while the air was gone
        self.epoch = 0                                 # flowgraph generation using this radio (see block())
        self.made = collections.Counter()              # radio blocks made, by kind
        self._attach()

    def _attach(self):
        """Fresh streams: the air resets both rings, then acknowledges."""
        h = self.shm.hdr
        token = (os.getpid() << 16) | random.getrandbits(16)
        h[H_PID] = os.getpid()
        h[H_RXON] = 0
        h[H_GEN] = token
        t_end = time.monotonic() + 5.0
        while h[H_ACK] != token:
            if time.monotonic() > t_end:
                raise RuntimeError(f"fake ANTSDR: the fake air '{self.air}' does not answer. Is "
                                   f"'python3 -m wavelink fake-air --name {self.air}' running?")
            time.sleep(0.01)
        self.beat()

    def block(self, kind: str) -> int:
        """A radio block of this kind ('sink', 'source') is being made: its flowgraph's generation.  The first
        block of a newer flowgraph (the app started it again after the radio stopped) starts fresh streams,
        as a new libiio context does; the blocks of an older one then end.  With the cable out it fails
        after a while, as creating a libiio context does."""
        if int(self.shm.hdr[H_UNPLUG]):
            time.sleep(CONNECT_FAIL_S)
            raise RuntimeError("Unable to create context")
        self.made[kind] += 1
        if self.made[kind] > self.epoch:
            if self.epoch:
                self._attach()
            self.epoch = self.made[kind]
        return self.epoch

    def gone(self, epoch: int, since: list, what: str) -> bool:
        """A radio block waiting on the air: has it to give up?  Yes when a newer flowgraph uses the radio,
        or when the cable has been out for IIO_TIMEOUT_S (since[0]: when the wait began with the cable out)."""
        if epoch != self.epoch:
            return True
        if not int(self.shm.hdr[H_UNPLUG]):
            since[0] = None
            return False
        t = time.monotonic()
        if since[0] is None:
            since[0] = t
            return False
        if t - since[0] < IIO_TIMEOUT_S:
            return False
        print(f"fake ANTSDR slot {self.slot}: Unable to {what} buffer: Connection timed out", file=sys.stderr)
        return True

    def beat(self):
        self.shm.hdr[H_BEAT] = time.monotonic_ns()

    def air_alive(self) -> bool:
        """The air has run within the last 10 s.  (A real radio's DMA never stops; this is only so that a
        station whose air is gone can still be stopped.  10 s: a busy PC must not trip it.)"""
        ok = time.monotonic_ns() - int(self.shm.hdr[H_AIRBEAT]) < 10_000_000_000
        if not ok and not self._warned:
            self._warned = True
            print(f"fake ANTSDR slot {self.slot}: the fake air '{self.air}' stopped", file=sys.stderr)
        elif ok:
            self._warned = False
        return ok

    def status(self) -> dict:
        h = self.shm.hdr
        return dict(air=self.air, slot=self.slot, air_lost=self.air_lost, underruns=int(h[H_UNDER]), underrun_samples=int(h[H_UNDER_S]),
                    overflows=int(h[H_OVER]), overflow_samples=int(h[H_OVER_S]), tx_clipped=int(h[H_CLIP_TX]),
                    tx_queued=int(h[H_TXW] - h[H_TXR]), rx_waiting=int(h[H_RXW] - h[H_RXR]))


_last_time = [0.0]


def radio_time() -> float:
    """The fake radios' sample clock in seconds (air time).  Stations run their timers on it, so that
    an air slower than real time (FakeAir speed < 1, for a PC too slow for all its stations) slows
    everything down alike, as the numpy simulator's clock does."""
    for st in list(_STATIONS.values()):
        h = st.shm.hdr
        clk, wall, sp, blk = int(h[H_AIRCLK]), int(h[H_AIRWALL]), int(h[H_SPEED]), int(h[H_BLOCK])
        fs = float(st.fs or 4e6)
        dt = max(0.0, (time.monotonic_ns() - wall) / 1e9) * (sp / 1e6 if sp else 1.0)
        t = (clk + min(dt * fs, blk)) / fs              # (between the air's blocks: interpolated)
        if t < _last_time[0]:
            t = _last_time[0]
        _last_time[0] = t
        return t
    return time.monotonic()


def station_status() -> list:
    """Counters of the fake radios in this process (for the dashboard / tests)."""
    return [s.status() for s in list(_STATIONS.values())]


def _make_blocks():
    from gnuradio import gr

    class fmcomms2_sink_fc32(gr.sync_block):
        """Stand-in for gr-iio's FMCOMMS2/Pluto Sink (complex float): a DAC behind 4 kernel buffers."""

        def __init__(self, uri, ch_en=(True, True), buffer_size=32768, cyclic=False):
            gr.sync_block.__init__(self, name="fake_fmcomms2_sink", in_sig=[np.complex64], out_sig=None)
            self.link = _StationLink.get(uri)
            self.epoch = self.link.block("sink")
            self._since = [None]
            self.B = int(buffer_size)
            if not 0 < self.B * KERNEL_BUFFERS < self.link.shm.ring:
                raise ValueError(f"fake ANTSDR: buffer size {self.B} out of range")
            self.set_output_multiple(self.B)
            self.link.shm.hdr[H_TXB] = self.B
            self.cyclic = bool(cyclic)
            self.len_tag_key = "packet_len"
            self._f = np.empty(2 * self.B, np.float32)
            self.pushed = 0

        # ---- gr-iio setters
        def set_len_tag_key(self, key=""):
            self.len_tag_key = str(key)

        def set_bandwidth(self, bandwidth):
            self.bandwidth = int(bandwidth)

        def set_frequency(self, frequency):
            self.link.shm.hdr[H_FREQ] = int(frequency)

        def set_samplerate(self, samplerate):
            self.link.fs = int(samplerate)
            self.link.shm.hdr[H_RATE] = int(samplerate)

        def set_attenuation(self, chan, attenuation):
            if int(chan) == 0:
                self.link.shm.hdr[H_TXATT] = int(round(float(attenuation) * 1000))

        def set_filter_params(self, *args):
            pass

        def work(self, input_items, output_items):
            x = input_items[0]
            B = self.B
            n = len(x) - len(x) % B
            link = self.link
            h = link.shm.hdr
            fs = float(link.fs or 4e6)
            limit = (KERNEL_BUFFERS - 1) * B
            for k in range(0, n, B):
                while True:                            # iio_buffer_push(): wait for a free kernel buffer
                    if link.gone(self.epoch, self._since, "push"):
                        return -1                      # (libiio gave up, or a newer flowgraph has the radio)
                    queued = int(h[H_TXW]) - int(h[H_TXR])
                    if queued <= limit:
                        break
                    link.beat()                        # (still here, only waiting)
                    if not link.air_alive():           # nothing plays the DAC: drop at the sample rate
                        time.sleep(B / fs)
                        link.air_lost += 1
                        break
                    time.sleep(max(0.0005, (queued - limit) / fs))
                v = x[k:k + B]
                f = v.view(np.float32)
                if f.max() > 1.0 or f.min() < -1.0:    # fc32 -> int16: full scale
                    np.clip(f, -1.0, 1.0, out=self._f)
                    v = self._f.view(np.complex64)
                    h[H_CLIP_TX] = int(h[H_CLIP_TX]) + 1
                if int(h[H_TXW]) - int(h[H_TXR]) <= limit:
                    _ring_write(link.shm.tx, h[H_TXW], v)
                    h[H_TXW] = int(h[H_TXW]) + B
                self.pushed += 1
            link.beat()
            return n

    class fmcomms2_source_fc32(gr.sync_block):
        """Stand-in for gr-iio's FMCOMMS2/Pluto Source (complex float): an ADC behind 4 kernel buffers."""

        def __init__(self, uri, ch_en=(True, True), buffer_size=32768):
            gr.sync_block.__init__(self, name="fake_fmcomms2_source", in_sig=None, out_sig=[np.complex64])
            self.link = _StationLink.get(uri)
            self.epoch = self.link.block("source")
            self._since = [None]
            self.B = int(buffer_size)
            if not 0 < self.B * KERNEL_BUFFERS < self.link.shm.ring:
                raise ValueError(f"fake ANTSDR: buffer size {self.B} out of range")
            self.set_output_multiple(self.B)
            self.link.shm.hdr[H_RXB] = self.B
            self.len_tag_key = "packet_len"

        # ---- gr-iio setters
        def set_len_tag_key(self, key=""):
            self.len_tag_key = str(key)

        def set_frequency(self, frequency):
            self.link.shm.hdr[H_FREQ] = int(frequency)

        def set_samplerate(self, samplerate):
            self.link.fs = int(samplerate)
            self.link.shm.hdr[H_RATE] = int(samplerate)

        def set_bandwidth(self, bandwidth):
            self.bandwidth = int(bandwidth)

        def set_gain_mode(self, chan, mode):
            if mode not in ("manual", "slow_attack", "fast_attack", "hybrid"):
                raise ValueError(f"gain mode {mode!r}")
            self.gain_mode = mode

        def set_gain(self, chan, gain):
            if int(chan) == 0:
                self.link.shm.hdr[H_RXGAIN] = int(round(float(gain) * 1000))

        def set_quadrature(self, on):
            pass

        def set_rfdc(self, on):
            pass

        def set_bbdc(self, on):
            pass

        def set_filter_params(self, *args):
            pass

        def start(self):
            self.link.shm.hdr[H_RXON] = 1              # the ADC starts filling buffers
            return True

        def work(self, input_items, output_items):
            out = output_items[0]
            B = self.B
            link = self.link
            h = link.shm.hdr
            if not h[H_RXON]:
                h[H_RXON] = 1
            fs = float(link.fs or 4e6)
            while True:                                # iio_buffer_refill(): wait for a full buffer
                if link.gone(self.epoch, self._since, "refill"):
                    return -1                          # (libiio gave up, or a newer flowgraph has the radio)
                avail = int(h[H_RXW]) - int(h[H_RXR])
                if avail >= B:
                    break
                link.beat()
                if not link.air_alive():               # no air: silence at the sample rate
                    time.sleep(B / fs)
                    link.air_lost += 1
                    out[:B] = 0
                    link.beat()
                    return B
                time.sleep(max(0.0005, (B - avail) / fs))
            n = min(len(out) - len(out) % B, avail - avail % B)
            _ring_read(link.shm.rx, h[H_RXR], n, out)
            h[H_RXR] = int(h[H_RXR]) + n
            link.beat()
            return n

    return fmcomms2_sink_fc32, fmcomms2_source_fc32


def install(default_slot: int | None = None):
    """Make `from gnuradio import iio` give the fake ANTSDR blocks (before the flowgraph is loaded)."""
    import gnuradio
    if default_slot is not None:
        _DEFAULT_SLOT[0] = int(default_slot)
    sink, source = _make_blocks()
    mod = types.ModuleType("gnuradio.iio")
    mod.__doc__ = "WaveLink fake ANTSDR (stand-ins for gr-iio's FMCOMMS2/Pluto blocks)"
    mod.fmcomms2_sink_fc32 = sink
    mod.fmcomms2_source_fc32 = source
    mod.pluto_sink = sink
    mod.pluto_source = source
    mod.get_pluto_uri = lambda: "fake:"
    mod.FAKE = True
    sys.modules["gnuradio.iio"] = mod
    gnuradio.iio = mod
    return mod


def installed() -> bool:
    return bool(getattr(sys.modules.get("gnuradio.iio"), "FAKE", False))


# ============================================================================ the air
def _lock_air(air: str):
    """Hold SHM_DIR/wavelink-<air>.lock while this air runs.  A second air of the same name would create
    the slot files again under the running one (zeroing them) and garble its stations' streams.  The OS
    drops the lock when the process ends, however it ends, so a crashed air leaves no stale lock."""
    import fcntl
    f = open(os.path.join(SHM_DIR, f"wavelink-{air}.lock"), "a")     # ("a": never truncates)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError(f"a fake air '{air}' is already running: stop it first, or start this one with "
                           f"--name OTHER (its stations: --uri fake:OTHER)") from None
    return f


class _Slot:
    """The air's side of one slot (one station's radio)."""

    def __init__(self, air: "FakeAir", slot: int):
        self.air = air
        self.id = slot
        self.path = shm_path(air.name, slot)
        self.shm = _Shm(self.path, create=True, ring=air.ring)
        self.gen = 0
        self.attached = False
        self.alive = False
        self.pid = 0
        self.tx_on = False            # its DAC has been fed (underruns count from then on)
        self.in_under = False
        self.gap = 0                  # injected TX underrun: zeros the DAC plays before what's queued
        self.drop = 0                 # injected lost RX buffer: samples the receiver never gets
        self.hist = np.zeros(air.max_delay + air.N, np.complex64)
        self.cur_on = False
        self.ph_tx = 0.0
        self.ph_rx = 0.0
        self.lo_hz = 0.0
        self.ppm = 0.0
        self.rs_tx = self.rs_rx = None
        self.unplugged = False        # its radio's cable is out: no samples either way
        self.stats = collections.Counter()

    def setup_clock(self):
        from .radio_sim import _Resampler
        e = self.ppm * 1e-6
        if e:
            self.rs_tx = _Resampler(1.0 + e)           # air sample i <- station sample i(1+e)
            self.rs_rx = _Resampler(1.0 / (1.0 + e))   # station sample j <- air sample j/(1+e)
        else:
            self.rs_tx = self.rs_rx = None

    def poll(self, now_ns: int):
        h = self.shm.hdr
        h[H_AIRBEAT] = now_ns
        h[H_SPEED] = int(self.air.speed * 1e6)
        h[H_BLOCK] = self.air.N
        h[H_AIRCLK] = self.air.clock
        h[H_AIRWALL] = now_ns
        g = int(h[H_GEN])
        if g and g != self.gen:                        # a station (re)attached: fresh streams
            for k in (H_TXW, H_TXR, H_RXW, H_RXR, H_UNDER, H_UNDER_S, H_OVER, H_OVER_S, H_CLIP_TX):
                h[k] = 0
            self.gen = g
            self.attached = True
            self.pid = int(h[H_PID])
            self.tx_on = self.in_under = self.cur_on = False
            self.gap = self.drop = 0
            self.hist[:] = 0
            self.setup_clock()
            self.stats["attaches"] += 1
            h[H_ACK] = g
            self.air.log(f"slot {self.id}: station attached (pid {self.pid})")
        alive = self.attached and now_ns - int(h[H_BEAT]) < 1_000_000_000
        if self.attached and not alive and self.alive is False and self.gen:
            self.stats["dead_blocks"] += 1
        if alive != self.alive:
            self.alive = alive
            self.air.log(f"slot {self.id}: station {'on the air' if alive else 'gone (no heartbeat)'}")
            if not alive:
                self.cur_on = False
                self.hist[:] = 0

    @property
    def rx_on(self) -> bool:
        return self.alive and bool(self.shm.hdr[H_RXON])

    def tx_amp(self) -> float:
        att = int(self.shm.hdr[H_TXATT])
        att_db = att / 1000 if att else 10.0
        return 10 ** (-(att_db - 10.0) / 20)

    def rx_amp(self) -> float:
        g = int(self.shm.hdr[H_RXGAIN])
        g_db = g / 1000 if g else 40.0
        return 10 ** ((g_db - 40.0) / 20)

    def take_tx(self, N: int, out: np.ndarray) -> bool:
        """Put the next N air samples of this station's DAC into out.  False: nothing written
        (station off the air; out is left as it was)."""
        if not self.alive or self.unplugged:
            return False
        h = self.shm.hdr
        rs = self.rs_tx
        need = N if rs is None else max(0, rs.need(N) - rs.available())
        x = out if rs is None else np.empty(need, np.complex64)
        z = min(self.gap, need)
        if z:
            x[:z] = 0
            self.gap -= z
        k = need - z
        avail = int(h[H_TXW]) - int(h[H_TXR])
        if avail > 0:
            self.tx_on = True
        got = min(k, max(0, avail))
        if got:
            _ring_read(self.shm.tx, h[H_TXR], got, x[z:z + got])
            h[H_TXR] = int(h[H_TXR]) + got
        if got < k:
            x[z + got:] = 0
            if self.tx_on:                             # the DAC ran dry: zeros go out, the stream slips
                if not self.in_under:
                    h[H_UNDER] = int(h[H_UNDER]) + 1
                    self.stats["underruns"] += 1
                    self.air.slips.append((self.air.clock / self.air.fs, self.id, "underrun"))
                self.in_under = True
                h[H_UNDER_S] = int(h[H_UNDER_S]) + (k - got)
        else:
            self.in_under = False
        if rs is not None:
            rs.feed(x)
            out[:] = rs.produce(N)
        return True

    def deliver(self, y: np.ndarray):
        """Receive samples into this station's ADC buffers (resampled to its clock, faults)."""
        if self.unplugged:                             # (they never reach the PC)
            self.stats["unplugged_lost"] += y.size
            return
        rs = self.rs_rx
        if rs is not None:
            from .modem import _FD_TAPS
            rs.feed(y)
            n_out = max(0, int(math.floor((rs.available() - (_FD_TAPS // 2 + 1) - rs.pos) / rs.ratio)) + 1)
            while n_out and rs.need(n_out) > rs.available():
                n_out -= 1
            y = rs.produce(n_out) if n_out else np.zeros(0, np.complex64)
        if self.drop and y.size:
            k = min(self.drop, y.size)
            self.drop -= k
            self.stats["dropped_injected"] += k
            y = y[k:]
        if not y.size:
            return
        h = self.shm.hdr
        B = int(h[H_RXB]) or 32768
        room = KERNEL_BUFFERS * B - (int(h[H_RXW]) - int(h[H_RXR]))
        if room < y.size:                              # the PC didn't fetch its buffers: samples lost
            h[H_OVER] = int(h[H_OVER]) + 1
            h[H_OVER_S] = int(h[H_OVER_S]) + (y.size - max(0, room))
            self.stats["overflows"] += 1
            self.air.slips.append((self.air.clock / self.air.fs, self.id, "overflow"))
            y = y[:max(0, room)]
            if not y.size:
                return
        _ring_write(self.shm.rx, h[H_RXW], y)
        h[H_RXW] = int(h[H_RXW]) + y.size

    def close(self):
        try:
            self.shm.mm.close()
        except (BufferError, ValueError):
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class FakeAir:
    """The air between fake ANTSDRs: one thread, on the PC's clock, block by block.

    Levels: the flowgraph transmits each code lane at RMS 0.25 (DAC full scale 1.0).  A 0 dB link
    arrives ref_db below that (-30 dB: -42 dBFS per lane), a station's own transmitter self_leak_db
    above the 0 dB link; links get a random spread of +/-spread_db.  esn0_db: Es/N0 a 0 dB link gives
    after despreading (sf x sps = 62 samples per symbol = +17.9 dB over the per-sample SNR).
    tx_atten (10 dB) and rx_gain (40 dB) set at the radios scale the signal relative to those defaults."""

    def __init__(self, name: str = "wavelink", slots=SLOTS, fs: float = 4e6, sf: int = 31, sps: int = 2,
                 esn0_db: float = 28.0, self_leak_db: float = 25.0, ref_db: float = -30.0, spread_db: float = 3.0,
                 lo_ppm: float = 1.0, max_delay: int = 40, block: int = 16384, seed: int = 1,
                 center_freq: float = 2.5e9, ring: int = RING, speed: float = 1.0, log=print):
        self.name = name
        self.fs = float(fs)
        self.speed = float(speed)                       # air seconds per real second
        self.sf, self.sps = int(sf), int(sps)
        self.N = int(block)
        self.max_delay = int(max_delay)
        if self.max_delay > self.N:
            raise ValueError("max_delay must not exceed the block length")
        self.ring = int(ring)
        self.ref_db = float(ref_db)
        self.self_leak_db = float(self_leak_db)
        self.log = log
        self.rng = np.random.default_rng(seed)
        ids = list(range(1, slots + 1)) if isinstance(slots, int) else [int(s) for s in slots]
        self._lockf = _lock_air(self.name)              # (before any slot file is touched)
        self.slots: dict[int, _Slot] = {}
        for i in ids:
            self.slots[i] = _Slot(self, i)
        self.gain_db: dict[tuple, float] = {}
        self.delay: dict[tuple, int] = {}
        self.phase: dict[tuple, float] = {}
        for a in ids:
            for b in ids:
                if a == b:
                    self.gain_db[(a, a)] = self.self_leak_db
                    self.delay[(a, a)] = 0
                    self.phase[(a, a)] = 0.0
                elif (b, a) in self.gain_db:           # reciprocal path
                    self.gain_db[(a, b)] = self.gain_db[(b, a)]
                    self.delay[(a, b)] = self.delay[(b, a)]
                    self.phase[(a, b)] = self.phase[(b, a)]
                else:
                    self.gain_db[(a, b)] = float(self.rng.uniform(-spread_db, spread_db))
                    self.delay[(a, b)] = int(self.rng.integers(0, self.max_delay + 1))
                    self.phase[(a, b)] = float(self.rng.uniform(0, 2 * np.pi))
            self.slots[a].lo_hz = float(self.rng.uniform(-1, 1)) * lo_ppm * 1e-6 * center_freq
        self.cut: set = set()
        self.interference = None
        self._intf_on = False
        self._prng = random.Random(seed)
        tbl = 1 << 20                                   # (noise: random slices of a 1 M-sample table)
        self._unit_noise = ((self.rng.standard_normal(tbl + self.N) + 1j * self.rng.standard_normal(tbl + self.N))
                            * math.sqrt(0.5)).astype(np.complex64)
        self._rot: dict = {}
        self._amp: dict = {}
        self.set_esn0(esn0_db)
        self.clock = 0                                  # air samples played so far
        self.overlaps: list = []
        self.slips: list = []                           # (air time, slot, kind): every stream slip
        self.stats = collections.Counter()
        self.cpu_s = 0.0
        self.t_start = None
        self._run = False
        self._paused = False
        self._thread = None
        self._lock = threading.Lock()
        self._ctl = None

    # ---------------------------------------------------------------- setup / control
    def lane_rms(self) -> float:
        """RMS of one code lane of a 0 dB link at a receiver."""
        return 0.25 * 10 ** (self.ref_db / 20)

    def set_esn0(self, esn0_db: float):
        self.esn0_db = float(esn0_db)
        snr = self.esn0_db - 10 * math.log10(self.sf * self.sps)
        self.noise_amp = self.lane_rms() / 10 ** (snr / 20)
        self.noise_tbl = self._unit_noise * np.float32(self.noise_amp)

    def set_link(self, a: int, b: int, gain_db: float | None = None, cut: bool | None = None, both: bool = True):
        with self._lock:
            for key in ((a, b), (b, a)) if both else ((a, b),):
                if gain_db is not None:
                    self.gain_db[key] = float(gain_db)
                if cut is True:
                    self.cut.add(key)
                elif cut is False:
                    self.cut.discard(key)
            self._amp.clear()

    def set_off_air(self, slot: int, off: bool = True):
        """Nobody hears this station and it hears nobody (out of range); its own echo stays."""
        for other in self.slots:
            if other != slot:
                self.set_link(slot, other, cut=off)

    def set_unplugged(self, slot: int, out: bool = True):
        """Pull (out=True) or plug back the cable of a slot's radio: no samples either way meanwhile; after
        IIO_TIMEOUT_S its radio blocks give up (see the module docstring)."""
        s = self.slots[slot]
        with self._lock:
            s.unplugged = bool(out)
            s.shm.hdr[H_UNPLUG] = 1 if out else 0
            self.slips.append((self.clock / self.fs, slot, "cable out" if out else "cable back"))
        self.log(f"slot {slot}: cable {'pulled' if out else 'back'}")

    def set_ppm(self, slot: int, ppm: float):
        """Sample-clock error of a station's radio (its TX and RX); takes effect when it attaches."""
        self.slots[slot].ppm = float(ppm)
        self.slots[slot].setup_clock()

    def inject_tx_gap(self, slot: int, n: int):
        """TX underrun: the station's transmit stream reaches the air n samples later from now on."""
        with self._lock:
            self.slots[slot].gap += int(n)
            self.slots[slot].stats["gap_injected"] += int(n)
            self.slips.append((self.clock / self.fs, slot, "tx gap"))

    def inject_rx_drop(self, slot: int, n: int):
        """Lost RX buffer: the next n samples never reach the station (its receive stream slips by n)."""
        with self._lock:
            self.slots[slot].drop += int(n)
            self.slips.append((self.clock / self.fs, slot, "rx drop"))

    def set_interference(self, level_db: float | None, duty: float = 0.2, burst_ms: float = 20.0):
        """Bursty wideband interference at every receiver, level relative to one lane of a 0 dB link."""
        self.interference = None if level_db is None else dict(level_db=float(level_db), duty=float(duty),
                                                                  burst_ms=float(burst_ms))

    def _link_amp(self, key) -> np.complex64:
        g = self._amp.get(key)
        if g is None:
            g = self._amp[key] = np.complex64(10 ** ((self.ref_db + self.gain_db[key]) / 20) * np.exp(1j * self.phase[key]))
        return g

    def _rotation(self, f: float) -> np.ndarray:
        """exp(j 2 pi f n / fs), n = 0 .. N-1 (one block of a carrier offset f; f < 0: the conjugate)."""
        rot = self._rot.get(f)
        if rot is None:
            n = np.arange(self.N, dtype=np.float64)
            rot = self._rot[f] = np.exp(2j * np.pi * f * n / self.fs).astype(np.complex64)
        return rot

    def _noise(self, unit: bool = False) -> np.ndarray:
        """A block of receiver noise (a read-only view)."""
        o = self._prng.randrange(self.noise_tbl.size - self.N)
        return (self._unit_noise if unit else self.noise_tbl)[o:o + self.N]

    def _intf(self):
        p = self.interference
        if not p:
            return None
        chunk_ms = 1000 * self.N / self.fs
        mean_on = max(chunk_ms, p["burst_ms"])
        duty = min(0.95, max(0.01, p["duty"]))
        mean_off = mean_on * (1 - duty) / duty
        if self._intf_on:
            self._intf_on = self.rng.random() > chunk_ms / mean_on
        else:
            self._intf_on = self.rng.random() < chunk_ms / mean_off
        if not self._intf_on:
            return None
        return self._noise(unit=True) * np.float32(self.lane_rms() * 10 ** (p["level_db"] / 20))

    # ---------------------------------------------------------------- one block of air
    def _step(self):
        N, md = self.N, self.max_delay
        tau = 2 * np.pi
        now_ns = time.monotonic_ns()
        for s in self.slots.values():
            s.poll(now_ns)
        on_now, sending = [], []
        with self._lock:
            # what each DAC plays: hist = [last max_delay samples of the previous block | this block],
            # at the station's TX power and LO (carrier offset, continuous phase)
            for s in self.slots.values():
                hist = s.hist
                tail_on = s.cur_on                     # (by induction hist[md:] is all zeros when not on)
                if md:
                    hist[:md] = hist[N:]
                cur = s.take_tx(N, hist[md:])
                if cur:
                    cur = bool(hist[md:].any())
                elif tail_on:
                    hist[md:] = 0
                if cur:
                    g = np.complex64(s.tx_amp() * np.exp(1j * s.ph_tx))
                    if s.lo_hz:
                        hist[md:] *= self._rotation(s.lo_hz) * g
                    elif g != 1:
                        hist[md:] *= g
                    on_now.append(s)
                s.ph_tx = (s.ph_tx + tau * s.lo_hz * N / self.fs) % tau
                s.cur_on = cur
                if cur or tail_on:
                    sending.append(s)
            if len(on_now) >= 2:
                self._note_overlaps(on_now)
            intf = self._intf()
            freq = {s.id: int(s.shm.hdr[H_FREQ]) for s in self.slots.values()}
            for r in self.slots.values():
                if not r.rx_on:
                    continue
                acc, peak = None, 0.0
                if sending:
                    rph = np.exp(-1j * r.ph_rx)          # the receiver's LO phase (its ramp below)
                    for s in sending:
                        key = (s.id, r.id)
                        if key in self.cut or freq[s.id] != freq[r.id]:
                            continue
                        d = self.delay[key]
                        g = np.complex64(self._link_amp(key) * rph)
                        term = s.hist[md - d:md - d + N] * g
                        if acc is None:
                            acc = term
                        else:
                            acc += term
                        peak += abs(g) * 1.415 * s.tx_amp()
                    if acc is not None and r.lo_hz:
                        acc *= self._rotation(-r.lo_hz)
                r.ph_rx = (r.ph_rx + tau * r.lo_hz * N / self.fs) % tau
                a = r.rx_amp()
                if acc is None and intf is None:
                    y = self._noise()                  # (read-only view: deliver() copies it)
                    if a != 1.0:
                        y = y * np.float32(a)
                else:
                    y = self._noise() + (acc if intf is None else (intf if acc is None else acc + intf))
                    if a != 1.0:
                        y *= np.float32(a)
                    if intf is not None or (peak + 6 * self.noise_amp) * a > 0.95:
                        f = y.view(np.float32)
                        np.clip(f, -1.0, 1.0, out=f)    # ADC full scale
                r.deliver(y)
        self.clock += N

    def _note_overlaps(self, on_now):
        """Two stations transmitting at once that a third hears.  Only bursts at most ~12 dB below a full-power
        code lane count: a newcomer's calibration probe (its own code, 20 dB down) is meant to coexist."""
        md = self.max_delay
        busy = {s.id: (np.abs(s.hist[md:]) > 0.06) for s in on_now}
        keys = sorted(busy)
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                n = int(np.count_nonzero(busy[a] & busy[b]))
                if n <= 64:                            # (filter tails)
                    continue
                if any(r not in (a, b) and self.slots[r].alive and (a, r) not in self.cut and (b, r) not in self.cut
                       for r in self.slots):
                    self.overlaps.append((round(self.clock / self.fs, 3), a, b, n))

    # ---------------------------------------------------------------- run
    def start(self):
        if not self._run:
            self._run = True
            self._thread = threading.Thread(target=self._loop, name="fake-air", daemon=True)
            self._thread.start()

    def stop(self):
        self._run = False
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._ctl is not None:
            try:
                self._ctl.close()
            except OSError:
                pass

    def close(self):
        self.stop()
        for s in self.slots.values():
            s.close()
        lk, self._lockf = self._lockf, None
        if lk is not None:                              # (after the slots: the next air may start now)
            try:
                os.unlink(lk.name)
            except OSError:
                pass
            lk.close()

    def _loop(self):
        N = self.N
        t0 = time.monotonic()
        n0 = self.clock
        self.t_start = t0
        while self._run:
            if self._paused:
                now_ns = time.monotonic_ns()
                for s in self.slots.values():
                    s.poll(now_ns)
                time.sleep(0.01)
                t0, n0 = time.monotonic(), self.clock
                continue
            fs = self.fs * self.speed                     # samples per real second
            due = n0 + (time.monotonic() - t0) * fs       # samples the DACs/ADCs have played by now
            if self.clock + N > due:
                time.sleep(min(0.004, (self.clock + N - due) / fs))
                continue
            if due - self.clock > 0.25 * fs:              # the air itself fell behind (PC overloaded)
                self.stats["air_stalls"] += 1
                self.stats["air_stall_ms"] += int(1000 * (due - self.clock) / fs)
                t0, n0 = time.monotonic(), self.clock
            t = time.perf_counter()
            try:
                self._step()
            except Exception as e:                         # never stop the air
                self.stats["errors"] += 1
                self.log(f"fake air error: {e!r}")
            self.cpu_s += time.perf_counter() - t

    def pause(self, on: bool = True):
        """Stop the air's clock (stations still attach; their radios and timers wait).  Stations started
        while paused all begin at the same moment of air time when it goes on: a simultaneous start."""
        self._paused = bool(on)

    def set_speed(self, speed: float):
        """Air seconds per real second (< 1: everything slower, for a PC too slow for all its stations)."""
        self.speed = max(0.05, min(1.0, float(speed)))

    def load(self) -> float:
        """CPU seconds the air needs per second of air time."""
        return self.cpu_s / max(1e-9, self.clock / self.fs)

    def status(self) -> dict:
        out = dict(name=self.name, air_s=round(self.clock / self.fs, 2), load=round(self.load(), 3), speed=self.speed,
                   esn0_db=self.esn0_db, overlaps=len(self.overlaps), stats=dict(self.stats), slots={})
        for i, s in self.slots.items():
            h = s.shm.hdr
            if not s.attached:
                continue
            out["slots"][i] = dict(alive=s.alive, pid=s.pid, attaches=s.stats["attaches"], unplugged=s.unplugged,
                                   underruns=int(h[H_UNDER]), underrun_ms=round(1000 * int(h[H_UNDER_S]) / self.fs, 1),
                                   overflows=int(h[H_OVER]), overflow_ms=round(1000 * int(h[H_OVER_S]) / self.fs, 1),
                                   underrun_samples=int(h[H_UNDER_S]), overflow_samples=int(h[H_OVER_S]),
                                   injected_gap=int(s.stats["gap_injected"]), injected_drop=int(s.stats["dropped_injected"]),
                                   dead_ms=round(1000 * s.stats["dead_blocks"] * self.N / self.fs),
                                   tx_queued_ms=round(1000 * int(h[H_TXW] - h[H_TXR]) / self.fs, 1),
                                   tx_clipped=int(h[H_CLIP_TX]), lo_hz=round(s.lo_hz), ppm=s.ppm,
                                   freq=int(h[H_FREQ]), rx_gain=int(h[H_RXGAIN]) / 1000, tx_atten=int(h[H_TXATT]) / 1000)
        return out

    def links(self) -> list:
        out = []
        for (a, b), g in sorted(self.gain_db.items()):
            if a != b:
                out.append(dict(tx=a, rx=b, gain_db=round(g, 1), esn0_db=round(self.esn0_db + g, 1),
                                cfo_hz=round(self.slots[a].lo_hz - self.slots[b].lo_hz),
                                delay=self.delay[(a, b)], cut=(a, b) in self.cut))
        return out

    # ---------------------------------------------------------------- control port (JSON lines)
    def serve_control(self, port: int, host: str = "127.0.0.1"):
        """{"cmd": "status"} | {"cmd": "cut", "a": 1, "b": 3, "on": true} | {"cmd": "gain", "a": 1, "b": 3,
        "db": 20} | {"cmd": "off", "slot": 2, "on": true} | {"cmd": "esn0", "db": 10} |
        {"cmd": "txgap", "slot": 2, "n": 6000} | {"cmd": "rxdrop", "slot": 2, "n": 4000} |
        {"cmd": "interference", "db": 0, "duty": 0.2, "ms": 20} (db null: off) | {"cmd": "links"}"""
        srv = socket.create_server((host, port))
        self._ctl = srv

        def handle(conn):
            with conn, conn.makefile("rw") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                        f.write(json.dumps(self.command(j)) + "\n")
                    except Exception as e:  # noqa: BLE001 - report to the client
                        f.write(json.dumps({"error": repr(e)}) + "\n")
                    f.flush()

        def accept():
            while self._run:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=handle, args=(c,), daemon=True).start()

        threading.Thread(target=accept, name="fake-air-ctl", daemon=True).start()

    def command(self, j: dict) -> dict:
        c = j.get("cmd")
        if c == "status":
            return self.status()
        if c == "links":
            return {"links": self.links()}
        if c == "cut":
            self.set_link(int(j["a"]), int(j["b"]), cut=bool(j.get("on", True)))
        elif c == "gain":
            self.set_link(int(j["a"]), int(j["b"]), gain_db=float(j["db"]))
        elif c == "off":
            self.set_off_air(int(j["slot"]), bool(j.get("on", True)))
        elif c == "esn0":
            self.set_esn0(float(j["db"]))
        elif c == "txgap":
            self.inject_tx_gap(int(j["slot"]), int(j["n"]))
        elif c == "rxdrop":
            self.inject_rx_drop(int(j["slot"]), int(j["n"]))
        elif c == "unplug":
            self.set_unplugged(int(j["slot"]), bool(j.get("on", True)))
        elif c == "speed":
            self.set_speed(float(j["x"]))
        elif c == "interference":
            self.set_interference(j.get("db"), float(j.get("duty", 0.2)), float(j.get("ms", 20)))
        else:
            return {"error": f"unknown command {c!r}"}
        return {"ok": True}
