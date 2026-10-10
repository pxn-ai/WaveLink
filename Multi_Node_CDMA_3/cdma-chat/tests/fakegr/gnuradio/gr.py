import threading, time
import numpy as np
sizeof_gr_complex = 8

class basic_block:
    def __init__(self, name="", in_sig=None, out_sig=None):
        self._name, self.in_sig, self.out_sig = name, in_sig, out_sig
    def set_max_output_buffer(self, *a): self._maxbuf = a[-1]
    def process(self, x):                      # used for pass-through blocks
        return x

class sync_block(basic_block):
    def __init__(self, name="", in_sig=None, out_sig=None):
        assert isinstance(name, str)
        basic_block.__init__(self, name, in_sig, out_sig)

class top_block:
    """Runs each connected chain in its own thread: source.work -> pass-through -> sink.work"""
    def __init__(self, name=""):
        self.chains, self._run = [], False
    def connect(self, *blks):
        assert len(blks) >= 2
        self.chains.append(blks)
    def start(self, max_noutput_items=8192):
        self._run = True; self.n = max_noutput_items; self.th = []
        for ch in self.chains:
            t = threading.Thread(target=self._go, args=(ch,), daemon=True); t.start(); self.th.append(t)
    def _go(self, ch):
        src, *mid, snk = ch
        while self._run:
            out = np.zeros(self.n, np.complex64)
            k = src.work([], [out])
            if k is None or k <= 0:
                time.sleep(0.001); continue
            x = out[:k]
            for m in mid: x = m.process(x)
            if x is None or x.size == 0: continue
            snk.work([x], [])
    def stop(self): self._run = False
    def wait(self):
        for t in getattr(self, "th", []): t.join(1)
