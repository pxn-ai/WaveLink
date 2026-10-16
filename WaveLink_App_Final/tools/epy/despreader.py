"""
CDMA Despreader

Input: received samples after the RRC matched filter.
For this station's Gold code and the broadcast code it
  1. correlates with the code-specific preamble (differential detector: works
     with carrier frequency offset) -> burst start, timing, coarse CFO
  2. removes the CFO, despreads (x code, sum over 31 chips) with fractional-delay
     timing and an early-late DLL that follows the clock offset between radios
  3. decodes the header coherently (two Golay(24,12) words, soft ML decoding):
     length, modulation, code rate, CRC-8
  4. tracks the carrier phase over the payload (decision-directed loop anchored
     on the known preamble and header)
and outputs one PDU per burst: the despread payload symbols, normalised to the
unit-energy constellation, with metadata mod / rate / length / code / snr_db /
esn0_db / cfo_hz.  The stock Constellation Soft Decoder blocks downstream turn
them into soft bits for the LDPC Decoder.
"""
#HEADER#


class blk(gr.sync_block):
    def __init__(self, node_id=1, samp_rate=4e6, sps=2, gold_degree=5, preamble_bits=48,
                 max_payload=1024):
        gr.sync_block.__init__(self, name="CDMA Despreader", in_sig=[np.complex64], out_sig=None)
        self.node_id = int(node_id)
        self.port = pmt.intern("pdus")
        self.message_port_register_out(self.port)
        self.set_output_multiple(16384)        # hand samples over in big chunks (less overhead)
        self.core = None
        try:
            if _project():
                from wavelink.grc_phy import DespreaderCore, phy_config
                cfg = phy_config(samp_rate, sps, gold_degree, preamble_bits, max_payload)
                self.core = DespreaderCore(cfg, self.node_id, self._publish)
                _register(self.node_id, "despreader", self.core)
        except ImportError:
            pass

    def _publish(self, f):
        mod = "bpsk" if f.mod in ("bpsk", "bpskc") else f.mod
        meta = pmt.make_dict()
        for k, v in (("mod", pmt.intern(mod)), ("code", pmt.from_long(int(f.code_idx))),
                     ("rate", pmt.from_long(int(f.rate))), ("length", pmt.from_long(int(f.length))),
                     ("mcs", pmt.intern(f.mcs)),
                     ("snr_db", pmt.from_double(float(f.snr_db))),
                     ("esn0_db", pmt.from_double(float(f.esn0_db))),
                     ("cfo_hz", pmt.from_double(float(f.cfo_hz))),
                     ("metric", pmt.from_double(float(f.metric)))):
            meta = pmt.dict_add(meta, pmt.intern(k), v)
        s = f.symbols
        self.message_port_pub(self.port, pmt.cons(meta, pmt.init_c32vector(len(s), s.tolist())))

    def start(self):
        if self.core is not None:
            self.core.start()
        return True

    def stop(self):
        if self.core is not None:
            self.core.stop()
        return True

    def work(self, input_items, output_items):
        if self.core is not None:
            self.core.push(input_items[0])
        return len(input_items[0])
