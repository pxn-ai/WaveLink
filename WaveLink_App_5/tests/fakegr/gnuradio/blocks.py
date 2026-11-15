import time
from . import gr
class throttle(gr.basic_block):
    def __init__(self, itemsize, rate, ignore_tags=True):
        super().__init__("throttle"); self.rate = rate; self.t0 = None; self.n = 0
    def process(self, x):
        if self.t0 is None: self.t0 = time.monotonic()
        self.n += x.size
        ahead = self.n / self.rate - (time.monotonic() - self.t0)
        if ahead > 0: time.sleep(ahead)
        return x
