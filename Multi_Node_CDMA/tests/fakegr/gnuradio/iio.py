"""Fake gr-iio: every fmcomms2 sink writes into a shared 'air'; every source reads the sum of the others."""
import threading, time, collections
import numpy as np
from . import gr
_AIR = collections.defaultdict(lambda: collections.deque(maxlen=400))   # uri -> (seq, chunk)
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
    def set_len_tag_key(self, k=""): self._rec("len_tag", k)
    def set_bandwidth(self, bw): assert isinstance(bw, int); self._rec("bw", bw)
    def set_frequency(self, f): self._rec("freq", f)
    def set_samplerate(self, fs): assert isinstance(fs, int); self.fs = fs; self._rec("fs", fs)
    def set_attenuation(self, ch, att): assert 0 <= att <= 89.75; self._rec("att", ch, att)
    def set_filter_params(self, src, fn="", fp=0.0, fs=0.0): self._rec("filter", src)
    def work(self, inp, out):
        x = inp[0]
        if self.t0 is None: self.t0 = time.monotonic()
        self.n += x.size
        ahead = self.n / self.fs - (time.monotonic() - self.t0)       # the DAC paces the flowgraph
        if ahead > 0: time.sleep(ahead)
        with _LOCK:
            _SEQ[self.uri] += 1
            _AIR[self.uri].append((_SEQ[self.uri], x.copy()))
        return x.size
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
