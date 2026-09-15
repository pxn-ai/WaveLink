import numpy as np
from gnuradio import gr
import pmt

class blk(gr.sync_block):
    def __init__(self):
        gr.sync_block.__init__(self, name='Text to PDU', in_sig=None, out_sig=None)
        self.message_port_register_in(pmt.intern('msg_in'))
        self.message_port_register_out(pmt.intern('pdu_out'))
        self.set_msg_handler(pmt.intern('msg_in'), self.handle_msg)

    def handle_msg(self, msg):
        if pmt.is_pair(msg):
            msg = pmt.cdr(msg)
        
        text = ""
        if pmt.is_symbol(msg):
            text = pmt.symbol_to_string(msg)
        elif pmt.is_string(msg):
            text = pmt.string_to_python(msg)
        else:
            return

        text += '\n'
        
        # --- PACKET 1: The Real Message ---
        bytes_list = list(text.encode('utf-8'))
        if len(bytes_list) < 1024:
            bytes_list.extend([32] * (1024 - len(bytes_list)))
        
        pdu_real = pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(bytes_list), bytes_list))
        self.message_port_pub(pmt.intern('pdu_out'), pdu_real)
        
        # --- PACKET 2: The Dummy Flush Packet ---
        # Fires immediately after to push the real packet through the DSP filters.
        # The receiver's .strip() logic automatically ignores this packet.
        flush_list = [32] * 1024 
        pdu_flush = pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(flush_list), flush_list))
        self.message_port_pub(pmt.intern('pdu_out'), pdu_flush)
