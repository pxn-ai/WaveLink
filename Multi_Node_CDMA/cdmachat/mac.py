"""Link layer: addressing, framing, CRC-32, fragmentation, selective-repeat ARQ.

MAC frame (= one PHY payload, max 255 bytes)::

    0     1      2    3    4..5     6..7       8..9        10..           last 4
    type  flags  dst  src  msg_id   frag_idx   frag_count  payload        CRC-32   (DATA)
    type  flags  dst  src  msg_id   frag_count bitmap...                  CRC-32   (ACK)
    type  flags  dst  src  msg_id   payload (beacon info)                 CRC-32   (BEACON)

flags: bits 0-1 priority (0 bulk .. 3 control), bit 2 ACK-request.

Reliability: the sender transmits a window of up to W missing fragments, the
last one carrying ACK-request.  The receiver answers with a block ACK holding a
bitmap of every fragment it has.  Missing fragments are resent in the next
window; a timeout resends with random back-off; after max_retries the message
fails.  Text = 1 fragment, so this degenerates to classic stop-and-wait.
Frames with a bad CRC or a foreign destination address are silently dropped,
so a node only ever ACKs frames that are addressed to it.
"""
from __future__ import annotations

import collections
import enum
import random
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

from .config import BROADCAST_ADDR, code_index_for

T_DATA, T_ACK, T_BEACON = 1, 2, 3
F_ACKREQ = 0x04
HDR = struct.Struct(">BBBBH")          # type, flags, dst, src, msg_id   (6 bytes)
CRC_LEN = 4

PRIO_BULK, PRIO_NORMAL, PRIO_HIGH, PRIO_CONTROL = 0, 1, 2, 3


def add_crc(b: bytes) -> bytes:
    return b + struct.pack(">I", zlib.crc32(b) & 0xFFFFFFFF)


def check_crc(b: bytes) -> bytes | None:
    if len(b) < HDR.size + CRC_LEN:
        return None
    body, crc = b[:-CRC_LEN], struct.unpack(">I", b[-CRC_LEN:])[0]
    return body if zlib.crc32(body) & 0xFFFFFFFF == crc else None


class MsgState(enum.Enum):
    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass
class OutMsg:
    msg_id: int
    dst: int
    data: bytes
    prio: int
    frags: list
    created: float
    on_event: object = None            # callable(event, msg)
    acked: set = field(default_factory=set)
    state: MsgState = MsgState.QUEUED
    retries: int = 0
    deadline: float = 0.0
    not_before: float = 0.0
    window: list = field(default_factory=list)
    tx_frames: int = 0
    first_tx: float | None = None
    defer_since: float | None = None
    done_time: float | None = None

    @property
    def progress(self) -> float:
        return len(self.acked) / len(self.frags)


@dataclass
class InMsg:
    src: int
    msg_id: int
    count: int
    frags: dict = field(default_factory=dict)
    last_rx: float = 0.0
    need_ack: bool = False
    acked_upto: int = -1
    prio: int = PRIO_NORMAL


class LinkLayer:
    def __init__(self, radio, addr: int, *, window: int = 8, ack_timeout: float = 0.35,
                 max_retries: int = 15, half_duplex_defer: float = 0.0,
                 on_message=None, on_beacon=None, log=None):
        self.radio = radio
        self.addr = addr
        self.window = window
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        # >0: wait up to this long for an incoming burst to finish before starting
        # a new window (useful on hardware where our own TX deafens our RX)
        self.half_duplex_defer = half_duplex_defer
        self.on_message = on_message or (lambda src, data, meta: None)
        self.on_beacon = on_beacon or (lambda src, data, frame: None)
        self.log = log or (lambda *a: None)
        self.max_frag = radio.cfg.max_phy_payload - 10 - CRC_LEN
        self.lock = threading.RLock()
        self.out: dict[int, list[OutMsg]] = collections.defaultdict(list)   # dst -> msgs
        self.inbox: dict[tuple, InMsg] = {}
        self.done_in: collections.OrderedDict = collections.OrderedDict()   # (src,id)->count
        self._next_id = random.randrange(0, 1 << 16)
        self.stats = collections.Counter()
        self.link_quality: dict[int, dict] = {}
        self._running = False
        radio.on_frame = self._on_phy_frame
        radio.on_reject = lambda reason, code: self.stats.update([reason])

    # ================================================================ sending
    def max_message_bytes(self) -> int:
        # the ACK bitmap (one bit per fragment) must fit in one frame
        bitmap_bytes = self.radio.cfg.max_phy_payload - HDR.size - 2 - CRC_LEN
        return self.max_frag * 8 * bitmap_bytes

    def send(self, dst: int, data: bytes, prio: int = PRIO_NORMAL, on_event=None) -> OutMsg:
        if dst == self.addr:
            raise ValueError("cannot send to self")
        if len(data) > self.max_message_bytes():
            raise ValueError(f"message too large ({len(data)} > {self.max_message_bytes()} bytes)")
        frags = [data[i:i + self.max_frag] for i in range(0, max(len(data), 1), self.max_frag)] or [b""]
        with self.lock:
            m = OutMsg(self._alloc_id(), dst, data, prio, frags, self.radio.now(), on_event)
            if dst == BROADCAST_ADDR:
                # unacknowledged broadcast: send every fragment once
                for i in range(len(frags)):
                    self._tx_data(m, i, ack_req=False)
                m.state = MsgState.DELIVERED
            else:
                self.out[dst].append(m)
        self._emit(m, "queued")
        return m

    def send_beacon(self, info: bytes):
        body = HDR.pack(T_BEACON, PRIO_CONTROL, BROADCAST_ADDR, self.addr, 0) + info
        self.radio.send(code_index_for(BROADCAST_ADDR), add_crc(body))
        self.stats["beacons_tx"] += 1

    def cancel(self, dst: int, msg_id: int):
        with self.lock:
            for m in self.out.get(dst, []):
                if m.msg_id == msg_id:
                    m.state = MsgState.FAILED
                    self._emit(m, "failed")
            self.out[dst] = [m for m in self.out.get(dst, []) if m.state not in
                             (MsgState.FAILED, MsgState.DELIVERED)]

    def pending_summary(self) -> list[dict]:
        with self.lock:
            return [dict(dst=m.dst, id=m.msg_id, prio=m.prio, frags=len(m.frags),
                         acked=len(m.acked), retries=m.retries, state=m.state.value)
                    for lst in self.out.values() for m in lst]

    def _alloc_id(self) -> int:
        self._next_id = (self._next_id + 1) & 0xFFFF
        return self._next_id

    def _emit(self, m: OutMsg, ev: str):
        if m.on_event:
            try:
                m.on_event(ev, m)
            except Exception as e:      # never let UI code kill the MAC thread
                self.log("on_event error", e)

    def _tx_data(self, m: OutMsg, i: int, ack_req: bool) -> float:
        flags = (m.prio & 3) | (F_ACKREQ if ack_req else 0)
        body = HDR.pack(T_DATA, flags, m.dst, self.addr, m.msg_id) + struct.pack(
            ">HH", i, len(m.frags)) + m.frags[i]
        m.tx_frames += 1
        self.stats["data_tx"] += 1
        return self.radio.send(code_index_for(m.dst), add_crc(body))

    # ================================================================ scheduler
    def start(self):
        self._running = True
        threading.Thread(target=self._loop, name=f"mac{self.addr}", daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self.tick()
            except Exception as e:
                self.log("MAC tick error", repr(e))
            time.sleep(0.003)

    def tick(self):
        now = self.radio.now()
        with self.lock:
            self._receiver_timers(now)
            for dst, msgs in list(self.out.items()):
                msgs = [m for m in msgs if m.state not in (MsgState.DELIVERED, MsgState.FAILED)]
                self.out[dst] = msgs
                if not msgs:
                    continue
                busy = [m for m in msgs if m.window]
                if busy:
                    m = busy[0]
                    if now >= m.deadline:
                        self._on_timeout(m, now)
                    continue
                # pick the highest-priority ready message (FIFO inside a class)
                ready = [m for m in msgs if now >= m.not_before]
                if not ready:
                    continue
                m = max(ready, key=lambda x: (x.prio, -x.created))
                if self.half_duplex_defer > 0 and self.radio.rx_busy():
                    if m.defer_since is None:
                        m.defer_since = now
                    if now - m.defer_since < self.half_duplex_defer:
                        continue                    # let the incoming burst finish first
                m.defer_since = None
                self._send_window(m, now)

    def _send_window(self, m: OutMsg, now: float):
        missing = [i for i in range(len(m.frags)) if i not in m.acked]
        win = missing[: self.window]
        done = now
        for j, i in enumerate(win):
            done = self._tx_data(m, i, ack_req=(j == len(win) - 1))
        m.window = win
        ack_air = self.radio.airtime(14 + (len(m.frags) + 7) // 8)
        m.deadline = max(done, now) + self.ack_timeout + 2 * ack_air
        if m.first_tx is None:
            m.first_tx = now
            m.state = MsgState.SENDING
            self._emit(m, "sent")
        self._emit(m, "progress")

    def _on_timeout(self, m: OutMsg, now: float):
        m.window = []
        m.retries += 1
        self.stats["timeouts"] += 1
        if m.retries > self.max_retries:
            m.state = MsgState.FAILED
            self.stats["msgs_failed"] += 1
            self._emit(m, "failed")
            return
        self.stats["retransmissions"] += 1
        # random back-off grows with consecutive failures (de-synchronises colliding senders)
        m.not_before = now + random.uniform(0.03, 0.15) * min(m.retries, 8)
        self._emit(m, "retry")

    def _on_ack(self, src: int, msg_id: int, count: int, bitmap: bytes):
        with self.lock:
            for m in self.out.get(src, []):
                if m.msg_id != msg_id or len(m.frags) != count:
                    continue
                before = len(m.acked)
                for i in range(count):
                    if bitmap[i >> 3] & (0x80 >> (i & 7)):
                        m.acked.add(i)
                if len(m.acked) > before:
                    m.retries = 0
                m.window = []
                m.not_before = 0.0
                if len(m.acked) == count and m.state != MsgState.DELIVERED:
                    m.state = MsgState.DELIVERED
                    m.done_time = self.radio.now()
                    self.stats["msgs_delivered"] += 1
                    self._emit(m, "delivered")
                else:
                    self._emit(m, "progress")

    # ================================================================ receiving
    def _on_phy_frame(self, f):
        body = check_crc(f.payload)
        if body is None:
            self.stats["crc_fail"] += 1
            return
        ftype, flags, dst, src, msg_id = HDR.unpack_from(body)
        if src == self.addr:
            return                                       # our own transmission
        if dst != self.addr and dst != BROADCAST_ADDR:
            self.stats["not_for_me"] += 1                # addressing filter
            return
        self.stats["frames_ok"] += 1
        self.radio.last_frame = f                        # dashboard: last frame from a peer
        q = self.link_quality.setdefault(src, {})
        q.update(snr_db=round(f.snr_db, 1), cfo_hz=round(f.cfo_hz), t=time.time())
        rest = body[HDR.size:]
        if ftype == T_BEACON:
            self.on_beacon(src, rest, f)
        elif ftype == T_ACK:
            count = struct.unpack_from(">H", rest)[0]
            self._on_ack(src, msg_id, count, rest[2:])
        elif ftype == T_DATA:
            idx, count = struct.unpack_from(">HH", rest)
            self._on_data(src, dst, msg_id, flags, idx, count, rest[4:], f)

    def _on_data(self, src, dst, msg_id, flags, idx, count, payload, f):
        key = (src, msg_id)
        ack_req = bool(flags & F_ACKREQ)
        with self.lock:
            now = self.radio.now()
            if key in self.done_in:                      # duplicate of a finished message
                self.stats["dup_frames"] += 1
                if dst != BROADCAST_ADDR:
                    self._send_ack(src, msg_id, count, set(range(count)))
                return
            im = self.inbox.get(key)
            if im is None or im.count != count:
                im = self.inbox[key] = InMsg(src, msg_id, count, prio=flags & 3)
            im.frags[idx] = payload
            im.last_rx = now
            im.need_ack = True
            if len(im.frags) == count:
                data = b"".join(im.frags[i] for i in range(count))
                del self.inbox[key]
                self.done_in[key] = count
                while len(self.done_in) > 2048:
                    self.done_in.popitem(last=False)
                if dst != BROADCAST_ADDR:
                    self._send_ack(src, msg_id, count, set(range(count)))
                self.stats["msgs_rx"] += 1
                meta = dict(src=src, msg_id=msg_id, snr_db=f.snr_db, prio=flags & 3,
                            broadcast=dst == BROADCAST_ADDR)
                try:
                    self.on_message(src, data, meta)
                except Exception as e:
                    self.log("on_message error", repr(e))
            elif ack_req and dst != BROADCAST_ADDR:
                self._send_ack(src, msg_id, count, set(im.frags))
                im.need_ack = False

    def _receiver_timers(self, now: float):
        # if the ACK-requesting frame was lost, ACK what we have after a quiet gap
        stale = []
        for key, im in self.inbox.items():
            if im.need_ack and now - im.last_rx > 0.6 * self.ack_timeout:
                self._send_ack(im.src, im.msg_id, im.count, set(im.frags))
                im.need_ack = False
            if now - im.last_rx > 120:
                stale.append(key)
        for k in stale:
            del self.inbox[k]

    def _send_ack(self, dst: int, msg_id: int, count: int, have: set):
        bm = bytearray((count + 7) // 8)
        for i in have:
            bm[i >> 3] |= 0x80 >> (i & 7)
        body = HDR.pack(T_ACK, PRIO_CONTROL, dst, self.addr, msg_id) + struct.pack(">H", count) + bytes(bm)
        self.radio.send(code_index_for(dst), add_crc(body), urgent=True)
        self.stats["acks_tx"] += 1

    def incoming_progress(self) -> list[dict]:
        with self.lock:
            return [dict(src=im.src, id=im.msg_id, have=len(im.frags), count=im.count)
                    for im in self.inbox.values()]
