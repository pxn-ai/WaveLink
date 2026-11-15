"""
CDMA Burst Combiner

One input per code lane (chips of complete bursts, tagged with 'packet_len').
Bursts on different codes are added together sample by sample (several links
active at once on one carrier = CDMA); bursts on the same code follow each other.
When no lane is sending, zeros are output so the radio always gets a continuous
stream (the stock 'PDU to Stream' block stops instead, which starves the Pluto sink).
Each burst is scaled by its 'pwr' tag (dB, transmit power control), which 'PDU to
Tagged Stream' put on its first chip next to 'packet_len'.
"""
#HEADER#

import collections


class blk(gr.basic_block):
    def __init__(self, node_id=1, codes=(0, 1, 2, 3, 4), chip_rate=2e6, samp_rate=4e6,
                 tx_buffer=131072):
        self.codes = [int(c) for c in codes]
        gr.basic_block.__init__(self, name="CDMA Burst Combiner",
                                in_sig=[np.complex64] * len(self.codes), out_sig=[np.complex64])
        self.node_id = int(node_id)
        self.len_key = pmt.intern("packet_len")
        self.pwr_key = pmt.intern("pwr")
        self.lens = [collections.deque() for _ in self.codes]
        self.bufs = [collections.deque() for _ in self.codes]
        self.have = [0] * len(self.codes)
        # produce in large chunks: each Python call has a fixed overhead, so fewer,
        # bigger calls keep the CPU load low (costs <= 4 ms of TX latency at 2 Mchip/s)
        self.set_output_multiple(8192)
        self.core = None
        try:
            if _project():
                from wavelink.grc_phy import CombinerCore
                self.core = CombinerCore(float(chip_rate), int(tx_buffer), float(samp_rate))
                _register(self.node_id, "combiner", self.core)
        except ImportError:
            pass

    def forecast(self, noutput_items, ninputs):
        return [0] * ninputs                  # run even when every lane is idle

    def _take(self, i, n):
        parts, need = [], n
        while need:
            b = self.bufs[i][0]
            if b.size <= need:
                parts.append(self.bufs[i].popleft())
                need -= b.size
            else:
                parts.append(b[:need])
                self.bufs[i][0] = b[need:]
                need = 0
        self.have[i] -= n
        return np.concatenate(parts)

    def general_work(self, input_items, output_items):
        for i, x in enumerate(input_items):
            n = len(x)
            if not n:
                continue
            tags = self.get_tags_in_window(i, 0, n)
            pwr = {t.offset: pmt.to_double(t.value) for t in tags if pmt.eq(t.key, self.pwr_key)}
            for t in tags:
                if pmt.eq(t.key, self.len_key):
                    self.lens[i].append((pmt.to_long(t.value), pwr.get(t.offset, 0.0)))
            self.bufs[i].append(np.array(x, dtype=np.complex64))
            self.have[i] += n
            self.consume(i, n)
            # only complete bursts are scheduled, so a burst is never cut by a gap
            while self.lens[i] and self.have[i] >= self.lens[i][0][0]:
                length, gain_db = self.lens[i].popleft()
                burst = self._take(i, length)
                if self.core is not None:
                    self.core.push(self.codes[i], burst, gain_db)
        out = output_items[0]
        if self.core is None:
            out[:] = 0
        else:
            self.core.read_into(out)
        return len(out)
