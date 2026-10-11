"""
CDMA PHY Framer

Builds one burst per PDU and outputs it as constellation *indices* (one byte per
symbol) for the Chunks-to-Symbols blocks of the code lanes:

  preamble   48 known BPSK symbols, different for every Gold code (burst detection,
             timing, carrier phase and frequency estimation at the receiver)
  header     24 BPSK symbols, differentially coded: 14-bit length, 2-bit modulation, CRC-8
  payload    MAC frame + CRC-32, Gray-mapped to BPSK (index 0-1), QPSK (2-5)
             or 16-QAM (6-21) of the combined symbol table

Output metadata 'code' selects the lane (spreading code) downstream.
"""
#HEADER#


class blk(gr.sync_block):
    def __init__(self, gold_degree=5, preamble_bits=48, max_payload=1024):
        gr.sync_block.__init__(self, name="CDMA PHY Framer", in_sig=None, out_sig=None)
        self.args = (int(gold_degree), int(preamble_bits), int(max_payload))
        self.port_in, self.port_out = pmt.intern("pdus"), pmt.intern("pdus")
        self.message_port_register_in(self.port_in)
        self.message_port_register_out(self.port_out)
        self.set_msg_handler(self.port_in, self.handle)
        self.core = None

    def start(self):
        _project()
        from cdmachat.grc_phy import FramerCore, phy_config
        g, k, m = self.args
        self.core = FramerCore(phy_config(4e6, 2, g, k, m))
        return True

    def handle(self, msg):
        if self.core is None:
            self.start()
        meta, vec = pmt.car(msg), pmt.cdr(msg)
        code = pmt.to_long(pmt.dict_ref(meta, pmt.intern("code"), pmt.from_long(0)))
        mod = pmt.symbol_to_string(pmt.dict_ref(meta, pmt.intern("mod"), pmt.intern("bpsk")))
        idx = self.core.indices(bytes(pmt.u8vector_elements(vec)), code, mod)
        out = pmt.dict_add(pmt.make_dict(), pmt.intern("code"), pmt.from_long(code))
        self.message_port_pub(self.port_out, pmt.cons(out, pmt.init_u8vector(len(idx), idx.tolist())))
