import numpy as np
from . import gr
class channel_model(gr.basic_block):
    def __init__(self, noise_voltage=0.0, frequency_offset=0.0, epsilon=1.0, taps=(1,), noise_seed=0, block_tags=False):
        super().__init__("channel_model"); self.nv=noise_voltage; self.fo=frequency_offset; self.ph=0.0
        self.rng=np.random.default_rng(noise_seed)
    def process(self, x):
        n = np.arange(x.size); y = x * np.exp(1j*(self.ph + 2*np.pi*self.fo*n)); self.ph += 2*np.pi*self.fo*x.size
        return (y + self.nv/np.sqrt(2)*(self.rng.standard_normal(x.size)+1j*self.rng.standard_normal(x.size))).astype(np.complex64)
