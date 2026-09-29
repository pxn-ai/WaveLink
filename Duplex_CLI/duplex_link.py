#!/usr/bin/env python3
"""
Byte-stream <-> GNU Radio BPSK modem bridge (run one copy per node).

Talks to bpsk_duplex_node.grc over ZeroMQ:
  we bind  PUSH  tcp://127.0.0.1:5555  -> flowgraph ZMQ PULL Source (TX path)
  we connect PULL tcp://127.0.0.1:5556 <- flowgraph ZMQ PUSH Sink   (RX path)

Frame (exactly payload_size = 1024 bytes, one radio packet):
  [magic 0xA7][seq u8][len u16 big-endian][data <=1020][zero padding]
"""
import argparse, queue, socket, struct, sys, threading
import zmq

FRAME    = 1024                      # must equal payload_size in the flowgraph
MAGIC    = 0xA7
HDR      = struct.Struct(">BBH")     # magic, seq, length
MAX_DATA = FRAME - HDR.size          # 1020 user bytes per frame


class Link:
    def __init__(self, tx_addr="tcp://127.0.0.1:5555",
                 rx_addr="tcp://127.0.0.1:5556", beacon_s=None):
        """beacon_s=None: transmit only when there is data (low latency).
        beacon_s=0.5 (say): also send an empty frame every 0.5 s when idle."""
        ctx = zmq.Context.instance()
        self.tx = ctx.socket(zmq.PUSH)
        self.tx.setsockopt(zmq.SNDHWM, 2)
        self.tx.setsockopt(zmq.SNDTIMEO, 200)
        self.tx.setsockopt(zmq.LINGER, 0)
        self.tx.bind(tx_addr)
        self.rx = ctx.socket(zmq.PULL)
        self.rx.setsockopt(zmq.RCVTIMEO, 200)
        self.rx.setsockopt(zmq.LINGER, 0)
        self.rx.connect(rx_addr)

        self.beacon_s = beacon_s
        self.txq, self.rxq = queue.Queue(), queue.Queue()
        self.running = True
        self.frames_tx = self.frames_rx = self.frames_lost = 0
        self._threads = [threading.Thread(target=f, daemon=True)
                         for f in (self._tx_loop, self._rx_loop)]
        for t in self._threads:
            t.start()

    # ---- public API -------------------------------------------------
    def send(self, data: bytes):
        if data:
            self.txq.put(bytes(data))

    def recv(self, timeout=None) -> bytes:
        try:
            return self.rxq.get(timeout=timeout)
        except queue.Empty:
            return b""

    def close(self):
        self.running = False
        for t in self._threads:
            t.join(timeout=1)
        self.tx.close(); self.rx.close()

    # ---- internals --------------------------------------------------
    def _tx_loop(self):
        seq, pending = 0, bytearray()
        wait = self.beacon_s if self.beacon_s else 0.2
        while self.running:
            if not pending:
                try:
                    pending += self.txq.get(timeout=wait)
                except queue.Empty:
                    if not self.beacon_s:
                        continue              # nothing to send, stay quiet
            while len(pending) < MAX_DATA:    # coalesce whatever is waiting
                try:
                    pending += self.txq.get_nowait()
                except queue.Empty:
                    break
            data = bytes(pending[:MAX_DATA])
            del pending[:MAX_DATA]
            frame = HDR.pack(MAGIC, seq, len(data)) + data
            frame += bytes(FRAME - len(frame))
            while self.running:               # blocks = backpressure
                try:
                    self.tx.send(frame)
                    self.frames_tx += 1
                    break
                except zmq.Again:
                    continue
            seq = (seq + 1) & 0xFF

    def _rx_loop(self):
        buf, expect = bytearray(), None
        while self.running:
            try:
                buf += self.rx.recv()         # arbitrary-sized chunks
            except zmq.Again:
                continue
            while len(buf) >= FRAME:
                magic, seq, n = HDR.unpack_from(buf)
                if magic != MAGIC or n > MAX_DATA:
                    del buf[0]                # resync: slide one byte
                    continue
                data = bytes(buf[HDR.size:HDR.size + n])
                del buf[:FRAME]
                if expect is not None:
                    self.frames_lost += (seq - expect) & 0xFF
                expect = (seq + 1) & 0xFF
                self.frames_rx += 1
                if n:
                    self.rxq.put(data)


# ---- front-ends -----------------------------------------------------
def run_stdio(link):
    def up():
        while True:
            d = sys.stdin.buffer.read1(MAX_DATA)
            if not d:
                break
            link.send(d)
    threading.Thread(target=up, daemon=True).start()
    while True:
        d = link.recv(0.5)
        if d:
            sys.stdout.buffer.write(d)
            sys.stdout.buffer.flush()


def run_tcp(link, sock):
    def up():
        try:
            while True:
                d = sock.recv(MAX_DATA)
                if not d:
                    break
                link.send(d)
        except OSError:
            pass
    threading.Thread(target=up, daemon=True).start()
    try:
        while True:
            d = link.recv(0.5)
            if d:
                sock.sendall(d)
    except OSError:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["stdio", "listen", "connect"], default="stdio")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tx", default="tcp://127.0.0.1:5555")
    ap.add_argument("--rx", default="tcp://127.0.0.1:5556")
    ap.add_argument("--beacon", type=float, default=None,
                    help="also send an empty frame every N s when idle")
    a = ap.parse_args()

    link = Link(a.tx, a.rx, a.beacon)
    try:
        if a.mode == "stdio":
            run_stdio(link)
        else:
            if a.mode == "listen":
                srv = socket.create_server(("127.0.0.1", a.port))
                s, _ = srv.accept()
            else:
                s = socket.create_connection((a.host, a.port))
            run_tcp(link, s)
    except KeyboardInterrupt:
        pass
    finally:
        print(f"frames tx={link.frames_tx} rx={link.frames_rx} lost={link.frames_lost}",
              file=sys.stderr)
