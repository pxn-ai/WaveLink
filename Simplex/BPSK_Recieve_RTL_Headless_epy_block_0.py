import numpy as np
from gnuradio import gr
import pmt

class blk(gr.sync_block):
    def __init__(self):
        gr.sync_block.__init__(self, name='PDU to Chat Log', in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern('pdu_in'))
        self.message_port_register_out(pmt.intern('msg_out'))
        self.set_msg_handler(pmt.intern('pdu_in'), self.handle_msg)
        self.chat_history = []

    def handle_msg(self, msg):
        if pmt.is_pair(msg):
            data = pmt.cdr(msg)
            if pmt.is_u8vector(data):
                bytes_data = bytes(pmt.u8vector_elements(data))
                new_text = bytes_data.decode('utf-8', errors='ignore').strip()
                
                if new_text:
                    self.chat_history.append(f"Received: {new_text}")
                    if len(self.chat_history) > 10:
                        self.chat_history.pop(0)
                    
                    display_text = "\n".join(self.chat_history)
                    self.message_port_pub(pmt.intern('msg_out'), pmt.intern(display_text))
