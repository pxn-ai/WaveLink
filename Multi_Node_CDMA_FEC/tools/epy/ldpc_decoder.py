"""
LDPC Decoder

Input PDUs: the soft bits of one burst's payload from a stock Constellation Soft
Decoder (log P(1)/P(0) per bit), metadata mod, rate, length and the receiver's
measurements.  Undoes the bit interleaver and rate matching, then decodes the
5G NR LDPC codeword with a layered normalised min-sum decoder (scale invariant,
so it needs no noise estimate; early stop when every parity check is met;
at most 'Max iterations').  Uncoded frames get hard decisions.

Output PDU: the frame bytes incl. CRC-32 (checked by the stock block after this),
metadata + fec_iters.
"""
#HEADER#


class blk(gr.sync_block):
    def __init__(self, node_id=1, max_iter=20):
        gr.sync_block.__init__(self, name="LDPC Decoder", in_sig=None, out_sig=None)
        self.node_id = int(node_id)
        self.port_in, self.port_out = pmt.intern("pdus"), pmt.intern("pdus")
        self.message_port_register_in(self.port_in)
        self.message_port_register_out(self.port_out)
        self.set_msg_handler(self.port_in, self.handle)
        self.core = None
        try:
            if _project():
                from cdmachat.grc_phy import LdpcDecoderCore
                self.core = LdpcDecoderCore(max_iter)
                _register(self.node_id, "ldpc_decoder", self.core)
        except ImportError:
            pass

    def handle(self, msg):
        if self.core is None:
            return
        meta, vec = pmt.car(msg), pmt.cdr(msg)
        get = lambda k, d: pmt.to_python(pmt.dict_ref(meta, pmt.intern(k), pmt.to_pmt(d)))  # noqa: E731
        soft = np.asarray(pmt.f32vector_elements(vec), dtype=np.float32)
        data, ok, it = self.core.decode(soft, int(get("length", 0)), str(get("mod", "qpsk")),
                                        int(get("rate", 0)))
        meta = pmt.dict_add(meta, pmt.intern("fec_iters"), pmt.from_long(int(it)))
        meta = pmt.dict_add(meta, pmt.intern("fec_ok"), pmt.from_bool(bool(ok)))
        self.message_port_pub(self.port_out, pmt.cons(meta, pmt.init_u8vector(len(data), list(data))))
