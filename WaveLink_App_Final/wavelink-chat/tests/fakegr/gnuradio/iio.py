"""Fake gr-iio: every fmcomms2 sink writes into a shared 'air'; every source reads the sum of the others."""
import threading, time, collections
import numpy as np
from . import gr
_AIR = collections.defaultdict(lambda: collections.deque(maxlen=20000))   # uri -> (seq, chunk)
_SEQ = collections.Counter()
_LOCK = threading.Lock()
CALLS = []
def _chk(uri, ch_en, buffer_size):
    assert isinstance(uri, str) and uri.startswith("ip:"), uri
    assert isinstance(ch_en, list) and all(isinstance(c, bool) for c in ch_en) and len(ch_en) in (2, 4)
    assert isinstance(buffer_size, int)
class _Dev(gr.basic_block):
    def _rec(self, name, *a):
        CALLS.append((self.uri, name, a))
class fmcomms2_sink_fc32(_Dev):
    def __init__(self, uri, ch_en, buffer_size, cyclic):
        _chk(uri, ch_en, buffer_size); assert isinstance(cyclic, bool)
        super().__init__("fmcomms2_sink"); self.uri = uri; self.fs = 4e6; self.t0=None; self.n=0
        self.buf = buffer_size; self.underruns = 0; self.dev_t = None
    def set_len_tag_key(self, k=""): self._rec("len_tag", k)
    def set_bandwidth(self, bw): assert isinstance(bw, int); self._rec("bw", bw)
    def set_frequency(self, f): self._rec("freq", f)
    def set_samplerate(self, fs): assert isinstance(fs, int); self.fs = fs; self._rec("fs", fs)
    def set_attenuation(self, ch, att): assert 0 <= att <= 89.75; self._rec("att", ch, att)
    def set_filter_params(self, src, fn="", fp=0.0, fs=0.0): self._rec("filter", src)
    def work(self, inp, out):
        # Model of a real DAC: it plays at fs no matter what.  The device holds
        # ~2 buffers; if the host is late the DAC transmits zeros (underrun) and
        # the late samples are played afterwards -> a hole inside the burst.
        x = inp[0]
        now = time.monotonic()
        if self.dev_t is None: self.dev_t = now
        depth = 2 * self.buf / self.fs
        if now > self.dev_t + depth:                       # buffer ran dry
            gap = int((now - self.dev_t - depth) * self.fs)
            self.underruns += 1
            self._emit(np.zeros(gap, np.complex64))
            self.dev_t = now - depth
        self._emit(x)
        self.dev_t += x.size / self.fs
        ahead = self.dev_t - depth / 2 - time.monotonic()   # host can only run ~1 buffer ahead
        if ahead > 0: time.sleep(ahead)
        return x.size
    def _emit(self, x):
        with _LOCK:
            _SEQ[self.uri] += 1
            _AIR[self.uri].append((_SEQ[self.uri], x.copy()))
    @classmethod
    def make(cls, *a): return cls(*a)
class fmcomms2_source_fc32(_Dev):
    def __init__(self, uri, ch_en, buffer_size):
        _chk(uri, ch_en, buffer_size); super().__init__("fmcomms2_source"); self.uri = uri
        self.rng = np.random.default_rng(abs(hash(uri)) % 1000); self.buf = buffer_size
        self.cursor = {}
    def set_len_tag_key(self, k="packet_len"): self._rec("len_tag", k)
    def set_frequency(self, f): self._rec("freq", f)
    def set_samplerate(self, fs): assert isinstance(fs, int); self._rec("fs", fs)
    def set_bandwidth(self, bw): self._rec("bw", bw)
    def set_gain_mode(self, ch, mode): assert mode in ("manual", "slow_attack", "fast_attack", "hybrid"); self._rec("gmode", mode)
    def set_gain(self, ch, g): self._rec("gain", g)
    def set_quadrature(self, v): pass
    def set_rfdc(self, v): pass
    def set_bbdc(self, v): pass
    def set_filter_params(self, src, fn="", fp=0.0, fs=0.0): pass
    def work(self, inp, out):
        # wait for the other radios' chunks; sum them with a fixed CFO per transmitter
        o = out[0]; got = None
        t_end = time.monotonic() + 0.05
        while got is None and time.monotonic() < t_end:
            with _LOCK:
                for uri, dq in _AIR.items():
                    if uri == self.uri: continue
                    last = self.cursor.get(uri, 0)
                    nxt = next(((sq, x) for sq, x in dq if sq > last), None)
                    if nxt is not None:
                        sq, x = nxt; self.cursor[uri] = sq
                        cfo = 2500.0 if uri.endswith("10") else -1800.0
                        k = min(x.size, o.size)
                        n = np.arange(k) + sq * x.size
                        seg = 0.5 * x[:k] * np.exp(2j*np.pi*cfo*n/4e6)
                        if got is None: got = seg
                        else:
                            m = min(got.size, k); got = got[:m] + seg[:m]
            if got is None: time.sleep(0.002)
        if got is None: return 0
        k = got.size
        o[:k] = got + 0.02 * (self.rng.standard_normal(k) + 1j*self.rng.standard_normal(k))
        return k
