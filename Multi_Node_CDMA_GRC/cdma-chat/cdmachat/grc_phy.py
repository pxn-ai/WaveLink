"""GNU Radio Companion physical layer (the .grc flowgraph *is* the PHY).

How it fits together (Option 5: the app runs the flowgraph)::

    grc/cdma_phy.grc  --grcc-->  build/grc/cdma_phy.py  --import-->  top_block
                                                                        |
    app (MAC, web UI)  <--- registry of interface blocks (by node id) ----+
         | submit(frame, code, mod)                   ^ frames (bytes + metadata)
         v                                            |
    [MAC -> PHY] -> CRC32 -> Framer -> code lanes -> Combiner -> RRC -> Pluto sink
    [PHY -> MAC] <- CRC32 check <- repack <- Constellation Decoder (x3) <- Despreader <- RRC <- Pluto source

The five Embedded Python blocks in the .grc are thin wrappers around the
classes here (FramerCore, CombinerCore, DespreaderCore) and register
themselves in REGISTRY[node_id] so the app can find them after it has built
the flowgraph.  Everything else in the flowgraph is stock GNU Radio blocks.
"""
from __future__ import annotations

import collections
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .config import PhyConfig, BROADCAST_CODE, code_index_for
from .modem import Modulator, Demodulator, HEADER_BITS, frame_symbols
from .radio_base import TxMixer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRC_DIR = os.path.join(ROOT, "grc")
BUILD_DIR = os.path.join(ROOT, "build", "grc")
PHY_CRC_LEN = 4                         # stock CRC32 blocks in the flowgraph

# node_id -> {"mac_tx": blk, "mac_rx": blk, "framer": core, "combiner": core, "despreader": core}
REGISTRY: dict[int, dict] = collections.defaultdict(dict)


def register(node_id: int, role: str, obj):
    REGISTRY[int(node_id)][role] = obj


def phy_config(samp_rate: float, sps: int, gold_degree: int = 5, preamble_bits: int = 48,
               max_payload: int = 1024, center_freq: float = 2.5e9) -> PhyConfig:
    """The PhyConfig matching the flowgraph's variables."""
    return PhyConfig(samp_rate=float(samp_rate), sps=int(sps), gold_degree=int(gold_degree),
                     preamble_bits=int(preamble_bits), max_phy_payload=int(max_payload),
                     center_freq=float(center_freq), rf_bandwidth=float(samp_rate), profile="grc")


# ============================================================================ block cores
class FramerCore:
    """Payload (+CRC) -> symbol indices: preamble | BPSK header | payload (BPSK/QPSK/16-QAM).

    The stock Chunks-to-Symbols block maps the indices through the combined
    constellation table [BPSK | QPSK | 16-QAM] (see modem.SYMBOL_TABLE)."""

    def __init__(self, cfg: PhyConfig):
        self.mod = Modulator(cfg)

    PAD = 16       # burst length is padded to a multiple of 16 symbols (see below)

    def indices(self, payload: bytes, code: int, mod: str) -> np.ndarray:
        # the GNU Radio PHY sends BPSK coherently so the stock decoder can slice it
        idx = self.mod.burst_indices(payload, code, "bpskc" if mod == "bpsk" else mod)
        # Pad with idle symbols so every burst is a multiple of 16 symbols (= 16 x 31
        # chips).  Stock blocks such as Multiply process in multiples of their SIMD
        # alignment and would otherwise hold the last few chips of a burst back until
        # the next burst arrives.  The receiver stops at the length in the header.
        extra = (-idx.size) % self.PAD
        if extra:
            idx = np.concatenate([idx, np.zeros(extra, dtype=np.uint8)])
        return idx


class CombinerCore:
    """Bursts of chips from the code lanes -> one continuous chip stream.

    One FIFO per spreading code: bursts on different codes are summed (multi-code
    CDMA from one transmitter), bursts on the same code follow each other, and
    zeros are sent when idle so the radio always has samples."""

    def __init__(self, chip_rate: float, tx_buffer_samples: int, samp_rate: float):
        # chips of unit amplitude; lanes are summed without clipping (the RRC
        # filter and the TX gain block after the combiner set the level)
        self.mixer = TxMixer(PhyConfig(), clip=None)
        self.chip_rate = chip_rate
        self.slack_chips = 0.25 * tx_buffer_samples * chip_rate / samp_rate
        self._t0 = None
        self.made = 0
        self.underruns = 0
        self.bursts = 0

    def push(self, code: int, chips: np.ndarray):
        self.mixer.enqueue(int(code), np.asarray(chips, dtype=np.complex64))
        self.bursts += 1

    def read_into(self, out: np.ndarray):
        """Fill the block's output buffer; idle fast path writes zeros directly."""
        m = self.mixer
        if not m.active and not any(m.queues.values()):
            out[:] = 0
            m.sample_count += len(out)
            self._account(len(out))
        else:
            out[:] = self.read(len(out))

    def _account(self, n: int):
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        self.made += n
        behind = (now - self._t0) * self.chip_rate - self.made
        if behind > self.slack_chips:
            self.underruns += 1
            self._t0 = now - self.made / self.chip_rate

    def read(self, n: int) -> np.ndarray:
        x, _ = self.mixer.read(n)
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        self.made += n
        # the DAC consumes chip_rate chips/s; if we fell behind by more than the
        # driver buffer can hide, the radio transmitted silence (underrun)
        behind = (now - self._t0) * self.chip_rate - self.made
        if behind > self.slack_chips:
            self.underruns += 1
            self._t0 = now - self.made / self.chip_rate
        return x

    def backlog(self, code: int) -> int:
        return self.mixer.backlog(int(code))


class DespreaderCore:
    """Matched-filtered samples -> despread, phase-corrected payload symbols per frame.

    Code acquisition (code-specific preamble correlators for this node's code and
    the broadcast code), CFO estimate, timing tracking (DLL) and carrier tracking
    run in a worker thread so the GNU Radio scheduler never waits for Python DSP.
    Each detected frame is published with its modulation, so the flowgraph can
    route it to the matching stock Constellation Decoder."""

    def __init__(self, cfg: PhyConfig, node_id: int, publish, chunk: int = 16384):
        self.cfg = cfg
        self.node_id = int(node_id)
        self.publish = publish                         # callable(frame)
        codes = [code_index_for(self.node_id), BROADCAST_CODE]
        self.demod = Demodulator(cfg, codes, on_frame=self._frame, matched_filter=False)
        self.q: collections.deque = collections.deque()
        self.q_samples = 0                                  # samples waiting in q
        self.q_max = int(3.0 * cfg.samp_rate)               # up to 3 s of backlog
        self.counters = collections.Counter()
        self.last_frame = None
        self._spec = np.zeros(0, dtype=np.complex64)
        self._spec_t = 0.0
        self._lock = threading.Lock()
        self._run = False

    def push(self, x: np.ndarray):
        if self.q_samples < self.q_max:
            self.q.append(np.array(x, dtype=np.complex64, copy=True))
            self.q_samples += len(x)
        else:                                          # PC too slow: drop, but keep time
            self.counters["rx_overflow_chunks"] += 1
            self.q.append(len(x))

    def start(self):
        if not self._run:
            self._run = True
            threading.Thread(target=self._worker, name=f"despread{self.node_id}", daemon=True).start()

    def stop(self):
        self._run = False

    def _worker(self):
        """Process queued samples in large batches: GNU Radio hands over ~4k samples at
        a time, and per-call numpy overhead would dominate at that size."""
        q = self.q
        batch = max(16384, int(0.02 * self.cfg.samp_rate))   # ~20 ms: big enough, low latency
        while self._run:
            if not q:
                time.sleep(0.004)
                continue
            parts, n = [], 0
            while q and n < batch:
                x = q.popleft()
                if isinstance(x, int):                 # dropped samples: flush, then skip
                    if parts:
                        q.appendleft(x)
                        break
                    self.demod.skip(x)
                    continue
                parts.append(x)
                n += x.size
                self.q_samples -= x.size
            if not parts:
                continue
            x = parts[0] if len(parts) == 1 else np.concatenate(parts)
            t = time.monotonic()
            if t - self._spec_t > 0.25 and x.size >= 4096:
                with self._lock:
                    self._spec = x[-4096:].copy()
                    self._spec_t = t
            try:
                self.demod.process(x)
            except Exception as e:                      # never kill the receiver
                self.counters["rx_errors"] += 1
                print("despreader error:", repr(e), file=sys.stderr)

    def _frame(self, f):
        self.last_frame = f
        self.counters["frames"] += 1
        self.publish(f)

    def spectrum_snapshot(self) -> np.ndarray:
        with self._lock:
            return self._spec


# ============================================================================ compile + load
def find_grcc() -> str:
    exe = shutil.which("grcc")
    if not exe:
        raise RuntimeError("grcc (GNU Radio Companion compiler) not found. Install GNU Radio 3.10 "
                           "(sudo apt install gnuradio) or use --phy numpy / the 'sim' command.")
    return exe


def grc_id(grc_file: str) -> str:
    import yaml
    with open(grc_file) as f:
        return yaml.safe_load(f)["options"]["parameters"]["id"]


def compile_grc(grc_file: str, out_dir: str = BUILD_DIR, log=print) -> str:
    """Compile the .grc with grcc (always, so edits made in GRC take effect)."""
    grc_file = os.path.abspath(grc_file)
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    r = subprocess.run([find_grcc(), "-o", out_dir, grc_file], capture_output=True, text=True)
    out = os.path.join(out_dir, grc_id(grc_file) + ".py")
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"grcc failed for {grc_file}:\n{r.stdout}\n{r.stderr}")
    log(f"compiled {os.path.relpath(grc_file, ROOT)} -> {os.path.relpath(out, ROOT)} "
        f"({time.time() - t0:.1f} s)")
    return out


def load_top_block(py_file: str, **params):
    """Import the generated flowgraph module and instantiate its top_block."""
    d = os.path.dirname(os.path.abspath(py_file))
    if d not in sys.path:
        sys.path.insert(0, d)                          # generated epy block modules live here
    name = os.path.splitext(os.path.basename(py_file))[0]
    for mod in [m for m in sys.modules if m == name or m.startswith(name + "_epy_block")]:
        del sys.modules[mod]                           # fresh import after a recompile
    spec = importlib.util.spec_from_file_location(name, py_file)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, name)
    # pass only the parameters this flowgraph declares (it may have been edited in GRC)
    import inspect
    accepted = inspect.signature(cls.__init__).parameters
    return cls(**{k: v for k, v in params.items() if k in accepted and v is not None})


# ============================================================================ radio adapter
@dataclass
class GrcFrame:
    """What the MAC needs from a received frame (built from the PDU metadata)."""
    payload: bytes
    code_idx: int
    snr_db: float
    esn0_db: float
    chip_snr_db: float
    cfo_hz: float
    metric: float
    mod: str
    rx_time: float = field(default_factory=time.time)
    constellation: np.ndarray = field(repr=False, default=None)


class GrcFlowgraph:
    """Owns one running top_block shared by one or more stations."""

    def __init__(self, tb, log=print):
        self.tb = tb
        self.log = log
        self._started = False
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if not self._started:
                self._started = True
                self.tb.start()
                self.log("GNU Radio flowgraph running")

    def stop(self):
        with self._lock:
            if self._started:
                self._started = False
                self.tb.stop()
                self.tb.wait()


class GrcPhyRadio:
    """Radio interface for the link layer, backed by the .grc flowgraph."""

    name = "grc"
    provides_crc = True                    # the flowgraph's stock CRC32 blocks append/check

    def __init__(self, fg: GrcFlowgraph, node_id: int, cfg: PhyConfig, tx_buffer: int = 131072):
        self.fg = fg
        self.addr = int(node_id)
        self.cfg = cfg
        reg = REGISTRY.get(self.addr, {})
        missing = {"mac_tx", "mac_rx", "combiner", "despreader"} - set(reg)
        if missing:
            raise RuntimeError(f"flowgraph has no {sorted(missing)} block for node {self.addr}; "
                               f"check the node_id parameters of the CDMA blocks in the .grc")
        self.reg = reg
        self.on_frame = lambda f: None
        self.on_reject = lambda reason, code: None
        self.last_frame = None
        self.counters = collections.Counter()
        self.latency = 2 * tx_buffer / cfg.samp_rate + 0.01
        self.busy_until: dict[int, float] = collections.defaultdict(float)
        self._lock = threading.Lock()
        reg["mac_rx"].handler = self._rx_pdu

    # ---------------------------------------------------------------- link-layer API
    def now(self) -> float:
        return time.monotonic()

    def airtime(self, n_bytes: int, mod: str = "bpsk") -> float:
        nsym = frame_symbols(self.cfg, n_bytes + PHY_CRC_LEN, mod)
        return nsym * self.cfg.sf / self.cfg.chip_rate

    def send(self, code_idx: int, payload: bytes, urgent: bool = False, mod: str = "bpsk") -> float:
        """Hand one MAC frame to the flowgraph; returns when it should have left the antenna."""
        air = self.airtime(len(payload), mod)
        now = self.now()
        with self._lock:
            start = max(now, self.busy_until[code_idx])
            self.busy_until[code_idx] = start + air
            done = start + air
        self.reg["mac_tx"].submit(payload, int(code_idx), mod)
        self.counters["tx_bursts"] += 1
        self.counters["tx_bytes"] += len(payload)
        return done + self.latency

    def tx_backlog(self, code_idx: int) -> float:
        return max(0.0, self.busy_until[code_idx] - self.now())

    def tx_busy(self) -> bool:
        return any(v > self.now() for v in self.busy_until.values())

    def rx_busy(self) -> bool:
        return self.reg["despreader"].demod.busy()

    # ---------------------------------------------------------------- RX path
    def _rx_pdu(self, meta: dict, data: bytes):
        d = self.reg["despreader"]
        es = float(meta.get("esn0_db", meta.get("snr_db", 0.0)))
        f = GrcFrame(payload=data, code_idx=int(meta.get("code", -1)), snr_db=float(meta.get("snr_db", 0.0)),
                     esn0_db=es, chip_snr_db=es - 10 * np.log10(self.cfg.sf),
                     cfo_hz=float(meta.get("cfo_hz", 0.0)), metric=float(meta.get("metric", 0.0)),
                     mod=str(meta.get("mod", "bpsk")),
                     constellation=(d.last_frame.constellation if d.last_frame is not None else None))
        self.counters["rx_frames"] += 1
        self.on_frame(f)

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.fg.start()

    def stop(self):
        self.fg.stop()

    # ---------------------------------------------------------------- dashboard
    def spectrum(self, nfft: int = 256) -> dict:
        x = self.reg["despreader"].spectrum_snapshot()
        if x.size < nfft:
            return dict(freqs=[], psd=[])
        segs = x[: (x.size // nfft) * nfft].reshape(-1, nfft)
        w = np.hanning(nfft).astype(np.float32)
        P = np.mean(np.abs(np.fft.fft(segs * w, axis=1)) ** 2, axis=0) / np.sum(w ** 2)
        P = np.fft.fftshift(10 * np.log10(P + 1e-12))
        f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / self.cfg.samp_rate))
        return dict(freqs=(f / 1e3).round(1).tolist(), psd=P.round(1).tolist())

    def status(self) -> dict:
        d = self.reg["despreader"]
        c = self.reg["combiner"]
        out = dict(d.demod.stats)
        out.update(d.counters)
        out.update(self.counters)
        out["tx_underruns"] = c.underruns
        out["tx_active_codes"] = c.mixer.active_codes()
        # frames the despreader found but the stock CRC32 check dropped (approx.)
        out["crc_drops"] = max(0, d.counters["frames"] - self.counters["rx_frames"])
        lf = self.last_frame
        if lf is not None:
            out.update(last_snr_db=round(lf.snr_db, 1), last_chip_snr_db=round(lf.chip_snr_db, 1),
                       last_mod=lf.mod, last_esn0_db=round(lf.esn0_db, 1),
                       last_cfo_hz=round(lf.cfo_hz), last_metric=round(lf.metric, 2))
            if lf.constellation is not None:
                out["constellation"] = [[round(float(z.real), 2), round(float(z.imag), 2)]
                                        for z in lf.constellation[-160:]]
        return out


def open_flowgraph(grc_file: str, params: dict, log=print) -> GrcFlowgraph:
    """Compile + instantiate a .grc; its CDMA blocks register themselves in REGISTRY."""
    REGISTRY.clear()
    py = compile_grc(grc_file, log=log)
    tb = load_top_block(py, **params)
    return GrcFlowgraph(tb, log=log)
