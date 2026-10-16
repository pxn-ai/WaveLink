"""
PHY -> MAC (to app)

Exit point of the physical layer: frames that passed the CRC-32 check, with the
receiver's metadata (code, modulation and code rate, SNR, carrier offset,
LDPC iterations).  The chat
application's link layer installs a handler here; when the flowgraph is run on
its own (e.g. from GNU Radio Companion) received frames are just printed.
"""
#HEADER#


class blk(gr.sync_block):
    def __init__(self, node_id=1):
        gr.sync_block.__init__(self, name="PHY -> MAC (to app)", in_sig=None, out_sig=None)
        self.node_id = int(node_id)
        self.port = pmt.intern("pdus")
        self.message_port_register_in(self.port)
        self.set_msg_handler(self.port, self.handle)
        self.handler = None
        _register(self.node_id, "mac_rx", self)

    def handle(self, msg):
        meta = pmt.to_python(pmt.car(msg)) or {}
        data = bytes(pmt.u8vector_elements(pmt.cdr(msg)))
        if self.handler is not None:
            self.handler(meta, data)
        else:
            print(f"[node {self.node_id}] rx {len(data)} B  {meta.get('mcs') or meta.get('mod')} "
                  f"code={meta.get('code')} Es/N0={meta.get('esn0_db', 0):.1f} dB", flush=True)
