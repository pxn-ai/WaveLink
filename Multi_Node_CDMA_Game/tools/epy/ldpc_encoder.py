"""
LDPC Encoder

Forward error correction for the payload: the 5G NR LDPC code (3GPP TS 38.212,
base graph 1).  Each MAC frame + CRC-32 is one codeword: filler bits up to
22 x Z (Z = smallest 5G lifting size that fits), systematic encoding, then rate
matching to the code rate the link layer asked for (1/2, 2/3, 3/4 or 5/6; the
first 2 Z bits are punctured as in 5G) and the 5G bit interleaver for the
modulation.  An uncoded frame ('16qam', 'qpsk', 'bpsk') passes as plain bits.

Input PDU:  frame bytes, metadata code, mod = modulation-and-coding ('16qam-3/4')
Output PDU: one byte per coded bit (0/1), metadata code, mod, rate, length
"""
#HEADER#


class blk(gr.sync_block):
    def __init__(self, max_payload=1024):
        gr.sync_block.__init__(self, name="LDPC Encoder", in_sig=None, out_sig=None)
        self.max_payload = int(max_payload)
        self.port_in, self.port_out = pmt.intern("pdus"), pmt.intern("pdus")
        self.message_port_register_in(self.port_in)
        self.message_port_register_out(self.port_out)
        self.set_msg_handler(self.port_in, self.handle)
        self.core = None

    def start(self):
        _project()
        from cdmachat.grc_phy import LdpcEncoderCore, phy_config
        self.core = LdpcEncoderCore(phy_config(4e6, 2, max_payload=self.max_payload))
        return True

    def handle(self, msg):
        if self.core is None:
            self.start()
        meta, vec = pmt.car(msg), pmt.cdr(msg)
        frame = bytes(pmt.u8vector_elements(vec))
        mcs = pmt.symbol_to_string(pmt.dict_ref(meta, pmt.intern("mod"), pmt.intern("qpsk-1/2")))
        bits, mod, rate = self.core.encode(frame, mcs)
        meta = pmt.dict_add(meta, pmt.intern("mod"), pmt.intern(mod))
        meta = pmt.dict_add(meta, pmt.intern("rate"), pmt.from_long(int(rate)))
        meta = pmt.dict_add(meta, pmt.intern("length"), pmt.from_long(len(frame)))
        self.message_port_pub(self.port_out, pmt.cons(meta, pmt.init_u8vector(len(bits), bits.tolist())))
