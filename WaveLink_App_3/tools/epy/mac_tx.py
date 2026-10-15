"""
MAC -> PHY (from app)

Entry point of the physical layer.  The chat application's link layer calls
submit(frame, code, mod) on this block; it publishes the MAC frame as a PDU
whose metadata says which Gold code to spread it with (= destination station,
0 = broadcast) and which modulation and coding to use: bpsk | qpsk | 16qam,
plus an LDPC code rate, e.g. '16qam-3/4' (no rate = uncoded).
"""
#HEADER#


class blk(gr.sync_block):
    def __init__(self, node_id=1):
        gr.sync_block.__init__(self, name="MAC -> PHY (from app)", in_sig=None, out_sig=None)
        self.node_id = int(node_id)
        self.port = pmt.intern("pdus")
        self.message_port_register_out(self.port)
        _register(self.node_id, "mac_tx", self)

    def submit(self, frame, code, mod):
        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, pmt.intern("code"), pmt.from_long(int(code)))
        meta = pmt.dict_add(meta, pmt.intern("mod"), pmt.intern(str(mod)))
        self.message_port_pub(self.port, pmt.cons(meta, pmt.init_u8vector(len(frame), list(frame))))
