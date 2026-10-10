"""GNU Radio backend for the ANTSDR E200 (or any AD936x / UHD radio).

Flowgraph (one per station)::

    +-------------------+      +----------------------+
    | CdmaBurstSource   |----->| ANTSDR TX            |   fmcomms2_sink (Pluto/libiio firmware)
    |  (TxMixer: per-   |      |  2.5 GHz             |   or usrp_sink (UHD firmware, type=ant)
    |   code burst FIFOs)|      +----------------------+
    +-------------------+
    +----------------------+      +-------------------+
    | ANTSDR RX            |----->| CdmaBurstSink     |--> worker thread: RRC MF, Gold-code
    |  2.5 GHz             |      |  (queue, no DSP   |    correlators, PLL/DLL demod,
    +----------------------+      |   in the GR thread)|    -> link layer
                                  +-------------------+

Backends:  iio      ANTSDR E200 with the Pluto-compatible firmware (QSPI boot), uri ip:192.168.1.10
           uhd      ANTSDR E200 with the UHD firmware (SD boot), args "type=ant,addr=192.168.1.10"
           loopback no hardware: TX -> channel_model (noise, CFO, clock offset) -> RX, to test an install
"""
from __future__ import annotations

import queue
import threading
import time
import numpy as np

from .config import PhyConfig
from .radio_base import RadioBase

try:
    from gnuradio import gr, blocks
    HAVE_GR = True
except ImportError:          # the simulator works without GNU Radio
    gr = None
    HAVE_GR = False


def _require_gr():
    if not HAVE_GR:
        raise RuntimeError("GNU Radio (python3 gnuradio module) is not installed in this Python. "
                           "Install GNU Radio 3.10 (e.g. sudo apt install gnuradio gr-iio) or use --radio sim.")


if HAVE_GR:
    class CdmaBurstSource(gr.sync_block):
        """Continuous complex stream: zeros when idle, CDMA bursts when the MAC sends."""

        def __init__(self, radio: "GrRadio"):
            gr.sync_block.__init__(self, name="cdma_burst_source", in_sig=None, out_sig=[np.complex64])
            self.radio = radio

        def work(self, input_items, output_items):
            out = output_items[0]
            x, on = self.radio.mixer.read(len(out))
            out[:] = x
            if on:
                self.radio._tx_last_active = time.monotonic()
            return len(out)

    class CdmaBurstSink(gr.sync_block):
        """Hands received samples to the demodulator thread (keeps the GR scheduler fast)."""

        def __init__(self, radio: "GrRadio"):
            gr.sync_block.__init__(self, name="cdma_burst_sink", in_sig=[np.complex64], out_sig=None)
            self.radio = radio

        def work(self, input_items, output_items):
            x = input_items[0]
            try:
                self.radio._rxq.put_nowait(x.copy())
            except queue.Full:
                self.radio.counters["rx_overflow_chunks"] += 1
            return len(x)


class GrRadio(RadioBase):
    name = "gnuradio"

    def __init__(self, cfg: PhyConfig, addr: int, backend: str = "iio",
                 uri: str = "ip:192.168.1.10", uhd_args: str = "type=ant,addr=192.168.1.10",
                 buffer_size: int = 16384, loopback_noise: float = 0.02, log=print):
        _require_gr()
        super().__init__(cfg, addr)
        self.backend = backend
        self.uri = uri
        self.uhd_args = uhd_args
        self.buffer_size = buffer_size
        self.loopback_noise = loopback_noise
        self.log = log
        self._rxq: queue.Queue = queue.Queue(maxsize=400)
        self._tx_last_active = 0.0
        self.tb = None
        self._running = False
        # samples between the mixer and the antenna (GR buffer + driver buffer)
        self.tx_latency = 2 * buffer_size / cfg.samp_rate + 0.01

    # MAC timers: wall clock, plus the TX pipeline latency for "burst on air" estimates
    def _sample_to_time(self, sample_idx: int) -> float:
        return super()._sample_to_time(sample_idx) + self.tx_latency

    # ---------------------------------------------------------------- flowgraph
    def _build(self):
        cfg = self.cfg
        tb = gr.top_block(f"cdma_node_{self.addr}")
        self.tx_blk = CdmaBurstSource(self)
        self.rx_blk = CdmaBurstSink(self)
        try:
            self.tx_blk.set_max_output_buffer(self.buffer_size)
        except Exception:
            pass
        if self.backend == "iio":
            src, snk = self._iio_blocks()
        elif self.backend == "uhd":
            src, snk = self._uhd_blocks()
        elif self.backend == "loopback":
            from gnuradio import channels
            snk_chan = channels.channel_model(
                noise_voltage=self.loopback_noise, frequency_offset=3000.0 / cfg.samp_rate,
                epsilon=1.0 + 10e-6, taps=[1.0 + 0j], noise_seed=self.addr, block_tags=False)
            thr = blocks.throttle(gr.sizeof_gr_complex, cfg.samp_rate, True)
            tb.connect(self.tx_blk, thr, snk_chan, self.rx_blk)
            self.tb = tb
            return
        else:
            raise ValueError(f"unknown GNU Radio backend {self.backend}")
        tb.connect(self.tx_blk, snk)
        tb.connect(src, self.rx_blk)
        self.tb = tb

    def _iio_blocks(self):
        from gnuradio import iio
        cfg = self.cfg

        def make(fn, *extra):
            last = None
            for ch in ([True, True, False, False], [True, True]):
                try:
                    return fn(self.uri, ch, self.buffer_size, *extra)
                except Exception as e:      # channel-mask layout differs between firmwares
                    last = e
            raise last

        src = make(iio.fmcomms2_source_fc32)
        src.set_len_tag_key("")
        src.set_frequency(int(cfg.center_freq))
        src.set_samplerate(int(cfg.samp_rate))
        if hasattr(src, "set_bandwidth"):
            src.set_bandwidth(int(cfg.rf_bandwidth))
        src.set_gain_mode(0, cfg.rx_gain_mode)
        if cfg.rx_gain_mode == "manual":
            src.set_gain(0, float(cfg.rx_gain_db))
        src.set_quadrature(True)
        src.set_rfdc(True)
        src.set_bbdc(True)
        src.set_filter_params("Auto", "", 0, 0)

        snk = make(iio.fmcomms2_sink_fc32, False)
        snk.set_len_tag_key("")
        snk.set_bandwidth(int(cfg.rf_bandwidth))
        snk.set_frequency(int(cfg.center_freq))
        snk.set_samplerate(int(cfg.samp_rate))
        snk.set_attenuation(0, float(cfg.tx_atten_db))
        snk.set_filter_params("Auto", "", 0, 0)
        return src, snk

    def _uhd_blocks(self):
        from gnuradio import uhd
        cfg = self.cfg
        sa = uhd.stream_args(cpu_format="fc32", args="", channels=[0])
        src = uhd.usrp_source(self.uhd_args, sa)
        src.set_samp_rate(cfg.samp_rate)
        src.set_center_freq(cfg.center_freq, 0)
        src.set_bandwidth(cfg.rf_bandwidth, 0)
        src.set_antenna("RX2", 0)
        if cfg.rx_gain_mode == "manual":
            src.set_gain(cfg.rx_gain_db, 0)
        else:
            try:
                src.set_auto_gain(True, 0)
            except Exception:
                src.set_gain(cfg.rx_gain_db, 0)
        snk = uhd.usrp_sink(self.uhd_args, uhd.stream_args(cpu_format="fc32", args="", channels=[0]), "")
        snk.set_samp_rate(cfg.samp_rate)
        snk.set_center_freq(cfg.center_freq, 0)
        snk.set_bandwidth(cfg.rf_bandwidth, 0)
        snk.set_antenna("TX/RX", 0)
        snk.set_gain(max(0.0, 89.75 - cfg.tx_atten_db), 0)     # B210-style gain range 0..89.75 dB
        return src, snk

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self._running:
            return
        self._build()
        self._running = True
        threading.Thread(target=self._rx_worker, name=f"rx{self.addr}", daemon=True).start()
        self.tb.start(self.buffer_size)
        self.log(f"GNU Radio flowgraph running: backend={self.backend} "
                 f"{self.uri if self.backend == 'iio' else self.uhd_args}  "
                 f"fc={self.cfg.center_freq/1e9:.3f} GHz fs={self.cfg.samp_rate/1e6} MS/s")

    def stop(self):
        self._running = False
        if self.tb is not None:
            self.tb.stop()
            self.tb.wait()

    def _rx_worker(self):
        while self._running:
            try:
                x = self._rxq.get(timeout=0.2)
            except queue.Empty:
                continue
            # if the PC falls behind, shed load instead of drifting further behind
            if self._rxq.qsize() > 300:
                self.counters["rx_shed_chunks"] += self._rxq.qsize()
                while not self._rxq.empty():
                    n = self._rxq.get_nowait().size
                    self.demod.skip(n)
            self._rx_samples(x)
