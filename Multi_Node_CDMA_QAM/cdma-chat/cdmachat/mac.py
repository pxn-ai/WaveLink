"""Link layer: addressing, framing, CRC-32, fragmentation, pipelined selective-repeat ARQ,
adaptive modulation.

MAC frame (= one PHY payload)::

    0     1      2    3    4..5     6..7       8..9        10..                       last 4
    type  flags  dst  src  msg_id   frag_idx   frag_count  payload                    CRC-32  (DATA)
    type  flags  dst  src  msg_id   frag_count base        trigger(2) snr(1) SACK bitmap  CRC-32  (ACK)
    type  flags  dst  src  msg_id   payload (beacon info)                             CRC-32  (BEACON)

flags: bits 0-1 priority (0 bulk .. 3 control), bit 2 ACK-request.

Reliability (selective repeat with cumulative + selective ACKs):
  * The sender streams fragments, sets the ACK-request flag on every W-th frame
    (W chosen so a window is ~0.5 s on air) and then pauses for an "ACK gap" until
    the ACK arrives (or ack_gap passes).  The gap keeps the receiver from having to
    transmit its ACK on top of incoming frames: its own transmitter would swamp them.
  * The receiver answers each ACK request immediately with
        base   = first fragment it is still missing (everything below is received)
        bitmap = which of the next fragments (up to 1024) it already holds
        trigger= the fragment index that requested this ACK
        snr    = the despread SNR it measured on that frame (link-adaptation feedback).
  * Any in-flight fragment sent *before* the trigger frame and not acknowledged is
    known lost and is queued for retransmission straight away (no timeout needed).
  * A timeout (no ACK) resends everything in flight with a random back-off that grows
    with each failure; after max_retries consecutive failures the message fails.
  * Duplicates are re-ACKed but delivered once.  Text = 1 fragment -> stop-and-wait.

Adaptive modulation: each DATA frame to a station is sent with BPSK, QPSK or 16-QAM
chosen from the SNR that station reports in its ACKs (falling back to the SNR we
measure on its frames before any ACK has arrived), and stepped down for a while if a
window loses frames.  ACKs and beacons are always BPSK.
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
MAX_FRAGS = 0xFFFF                     # 16-bit fragment index
SACK_BITS = 1024                       # bitmap span carried by one ACK
NO_TRIGGER = 0xFFFF

PRIO_BULK, PRIO_NORMAL, PRIO_HIGH, PRIO_CONTROL = 0, 1, 2, 3
MOD_LEVELS = ["bpsk", "qpsk", "16qam"]
# despread symbol SNR (dB) needed to use a modulation (with margin for long frames)
MOD_THRESH = {"bpsk": -99.0, "qpsk": 15.0, "16qam": 23.0}


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
    retries: int = 0                   # consecutive timeouts without progress
    deadline: float = 0.0
    not_before: float = 0.0
    next_new: int = 0                  # next never-sent fragment
    retx: collections.deque = field(default_factory=collections.deque)
    inflight: dict = field(default_factory=dict)     # frag -> tx sequence number
    last_seq: dict = field(default_factory=dict)     # frag -> last tx sequence number
    tx_seq: int = 0
    since_req: int = 0
    hold_until: float = 0.0            # ACK gap: no new frames before this (or the ACK)
    cum_base: int = 0
    tx_frames: int = 0
    first_tx: float | None = None
    done_time: float | None = None
    defer_since: float | None = None

    @property
    def progress(self) -> float:
        return len(self.acked) / len(self.frags)

    def has_work(self) -> bool:
        return bool(self.retx) or self.next_new < len(self.frags)


@dataclass
class InMsg:
    src: int
    msg_id: int
    count: int
    frags: dict = field(default_factory=dict)
    base: int = 0                      # first missing fragment
    last_rx: float = 0.0
    need_ack: bool = False
    prio: int = PRIO_NORMAL


class LinkLayer:
    def __init__(self, radio, addr: int, *, ack_timeout: float = 0.35, max_retries: int = 30,
                 half_duplex_defer: float = 0.0, mod_policy: str = "auto",
                 window_airtime: float = 0.5, ack_gap: float = 0.05,
                 on_message=None, on_beacon=None, log=None):
        self.radio = radio
        self.addr = addr
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        # >0: wait up to this long for an incoming burst to finish before starting
        # to send to a station (on hardware our own TX deafens our RX)
        self.half_duplex_defer = half_duplex_defer
        self.mod_policy = mod_policy            # auto | bpsk | qpsk | 16qam
        self.window_airtime = window_airtime
        self.ack_gap = ack_gap                  # max pause after an ACK request (s)
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
        self.mod_penalty: dict[int, tuple[int, float]] = {}                 # dst -> (levels, until)
        self._last_rx_snr: dict[int, float] = {}                            # src -> Es/N0 of last data
        self._running = False
        radio.on_frame = self._on_phy_frame
        radio.on_reject = lambda reason, code: self.stats.update([reason])

    # ================================================================ adaptive modulation
    def mod_for(self, dst: int) -> str:
        if self.mod_policy != "auto":
            return self.mod_policy
        q = self.link_quality.get(dst)
        if not q:
            return "bpsk"
        snr = q.get("fb_esn0", q.get("esn0_avg"))      # receiver feedback beats reciprocity
        if snr is None:
            return "bpsk"
        lvl = max(i for i, m in enumerate(MOD_LEVELS) if snr >= MOD_THRESH[m])
        pen, until = self.mod_penalty.get(dst, (0, 0.0))
        if self.radio.now() < until:
            lvl = max(0, lvl - pen)
        return MOD_LEVELS[lvl]

    def _note_loss(self, dst: int, lost: int, total: int):
        if total >= 4 and lost / total > 0.2:
            pen, until = self.mod_penalty.get(dst, (0, 0.0))
            pen = min(pen + 1, len(MOD_LEVELS) - 1) if self.radio.now() < until else 1
            self.mod_penalty[dst] = (pen, self.radio.now() + 10.0)
            self.stats["mod_stepdowns"] += 1

    def window_frames(self, mod: str) -> int:
        air = self.radio.airtime(self.max_frag + 14, mod)
        return max(4, min(64, int(round(self.window_airtime / air))))

    # ================================================================ sending
    def max_message_bytes(self) -> int:
        return self.max_frag * MAX_FRAGS

    def send(self, dst: int, data: bytes, prio: int = PRIO_NORMAL, on_event=None) -> OutMsg:
        if dst == self.addr:
            raise ValueError("cannot send to self")
        if len(data) > self.max_message_bytes():
            raise ValueError(f"message too large ({len(data)} > {self.max_message_bytes()} bytes)")
        frags = [data[i:i + self.max_frag] for i in range(0, max(len(data), 1), self.max_frag)] or [b""]
        with self.lock:
            m = OutMsg(self._alloc_id(), dst, data, prio, frags, self.radio.now(), on_event)
            if dst == BROADCAST_ADDR:
                for i in range(len(frags)):                 # unacknowledged broadcast
                    self._tx_data(m, i, ack_req=False, mod="bpsk")
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
                         acked=len(m.acked), retries=m.retries, state=m.state.value,
                         mod=self.mod_for(m.dst))
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

    def _tx_data(self, m: OutMsg, i: int, ack_req: bool, mod: str) -> float:
        flags = (m.prio & 3) | (F_ACKREQ if ack_req else 0)
        body = HDR.pack(T_DATA, flags, m.dst, self.addr, m.msg_id) + struct.pack(
            ">HH", i, len(m.frags)) + m.frags[i]
        m.tx_frames += 1
        self.stats["data_tx"] += 1
        self.stats["data_tx_" + mod] += 1
        return self.radio.send(code_index_for(m.dst), add_crc(body), mod=mod)

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
                for m in msgs:
                    if m.inflight and now >= m.deadline:
                        self._on_timeout(m, now)
                # keep at most ~a window of airtime queued on this code, so an urgent
                # message can still get in ahead of a big transfer
                code = code_index_for(dst)
                mod = self.mod_for(dst)
                W = self.window_frames(mod)
                if self.half_duplex_defer > 0 and self.radio.rx_busy():
                    first = msgs[0]
                    if first.defer_since is None:
                        first.defer_since = now
                    if now - first.defer_since < self.half_duplex_defer:
                        continue
                    first.defer_since = None
                for m in sorted(msgs, key=lambda x: (-x.prio, x.created)):
                    while (m.has_work() and now >= m.not_before and now >= m.hold_until
                           and len(m.inflight) < 2 * W
                           and self.radio.tx_backlog(code) < self.window_airtime):
                        self._send_one(m, now, W, mod)
                    if self.radio.tx_backlog(code) >= self.window_airtime:
                        break

    def _send_one(self, m: OutMsg, now: float, W: int, mod: str):
        if m.retx:
            i = m.retx.popleft()
            if i in m.acked:
                return
            self.stats["retransmissions"] += 1
        else:
            i = m.next_new
            m.next_new += 1
        m.since_req += 1
        last = not m.has_work()
        ack_req = m.since_req >= W or last
        if ack_req:
            m.since_req = 0
        if len(m.frags) <= 2 and mod == "16qam":
            mod = "qpsk"          # short messages: latency matters more than rate
        m.tx_seq += 1
        m.inflight[i] = m.tx_seq
        m.last_seq[i] = m.tx_seq
        done = self._tx_data(m, i, ack_req, mod)
        ack_air = self.radio.airtime(25 + SACK_BITS // 8, "bpsk")
        m.deadline = max(done, now) + self.ack_timeout + 2 * ack_air
        if ack_req and m.has_work():
            m.hold_until = max(done, now) + self.ack_gap + ack_air
        if m.first_tx is None:
            m.first_tx = now
            m.state = MsgState.SENDING
            self._emit(m, "sent")

    def _on_timeout(self, m: OutMsg, now: float):
        m.retries += 1
        self.stats["timeouts"] += 1
        if m.retries > self.max_retries:
            m.state = MsgState.FAILED
            self.stats["msgs_failed"] += 1
            self._emit(m, "failed")
            return
        self._note_loss(m.dst, len(m.inflight), max(len(m.inflight), 4))
        # resend everything in flight, oldest first, after a random back-off
        for i in sorted(m.inflight, key=m.inflight.get):
            if i not in m.acked:
                m.retx.append(i)
        m.inflight.clear()
        m.since_req = 0
        m.hold_until = 0.0
        m.not_before = now + random.uniform(0.03, 0.15) * min(m.retries, 8)
        self._emit(m, "retry")

    def _on_ack(self, src: int, msg_id: int, count: int, base: int, trigger: int, bitmap: bytes):
        with self.lock:
            for m in self.out.get(src, []):
                if m.msg_id != msg_id or len(m.frags) != count or m.state != MsgState.SENDING:
                    continue
                before = len(m.acked)
                if base > m.cum_base:                       # cumulative part: all < base received
                    m.acked.update(range(m.cum_base, min(base, count)))
                    m.cum_base = base
                for j in range(min(len(bitmap) * 8, count - base - 1)):
                    if bitmap[j >> 3] & (0x80 >> (j & 7)):
                        m.acked.add(base + 1 + j)
                for i in [i for i in m.inflight if i in m.acked]:
                    del m.inflight[i]
                # anything sent before the triggering frame and still missing is lost
                tseq = m.last_seq.get(trigger) if trigger != NO_TRIGGER else None
                lost = 0
                if tseq is not None:
                    for i, seq in sorted(m.inflight.items(), key=lambda kv: kv[1]):
                        if seq <= tseq:
                            del m.inflight[i]
                            m.retx.append(i)
                            lost += 1
                    self._note_loss(m.dst, lost, max(self.window_frames(self.mod_for(m.dst)), 1))
                if len(m.acked) > before:
                    m.retries = 0
                m.hold_until = 0.0                          # ACK arrived: end the gap now
                if len(m.acked) == count:
                    m.state = MsgState.DELIVERED
                    m.done_time = self.radio.now()
                    self.stats["msgs_delivered"] += 1
                    self._emit(m, "delivered")
                else:
                    if not m.inflight and m.has_work():
                        m.not_before = 0.0
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
        es = getattr(f, "esn0_db", f.snr_db)
        q["esn0_avg"] = es if "esn0_avg" not in q else 0.8 * q["esn0_avg"] + 0.2 * es
        q.update(snr_db=round(f.snr_db, 1), esn0_db=round(es, 1), cfo_hz=round(f.cfo_hz),
                 mod=getattr(f, "mod", "bpsk"), t=time.time())
        rest = body[HDR.size:]
        if ftype == T_BEACON:
            self.on_beacon(src, rest, f)
        elif ftype == T_ACK:
            count, base, trigger, fb = struct.unpack_from(">HHHb", rest)
            if fb != -128:
                v = fb / 2.0
                q["fb_esn0"] = v if "fb_esn0" not in q else 0.7 * q["fb_esn0"] + 0.3 * v
            self._on_ack(src, msg_id, count, base, trigger, rest[7:])
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
                if dst != BROADCAST_ADDR and ack_req:
                    self._send_ack(src, msg_id, count, None, idx)
                return
            im = self.inbox.get(key)
            if im is None or im.count != count:
                im = self.inbox[key] = InMsg(src, msg_id, count, prio=flags & 3)
            if idx in im.frags:
                self.stats["dup_frames"] += 1
            self._last_rx_snr[src] = getattr(f, "esn0_db", f.snr_db)
            im.frags[idx] = payload
            while im.base in im.frags:
                im.base += 1
            im.last_rx = now
            im.need_ack = True
            if len(im.frags) == count:
                data = b"".join(im.frags[i] for i in range(count))
                del self.inbox[key]
                self.done_in[key] = count
                while len(self.done_in) > 2048:
                    self.done_in.popitem(last=False)
                if dst != BROADCAST_ADDR:
                    self._send_ack(src, msg_id, count, None, idx)
                self.stats["msgs_rx"] += 1
                meta = dict(src=src, msg_id=msg_id, snr_db=f.snr_db, prio=flags & 3,
                            broadcast=dst == BROADCAST_ADDR)
                try:
                    self.on_message(src, data, meta)
                except Exception as e:
                    self.log("on_message error", repr(e))
            elif ack_req and dst != BROADCAST_ADDR:
                self._send_ack(src, msg_id, count, im, idx)
                im.need_ack = False

    def _receiver_timers(self, now: float):
        # if the ACK-requesting frame was lost, ACK what we have after a quiet gap
        stale = []
        for key, im in self.inbox.items():
            if im.need_ack and now - im.last_rx > 0.6 * self.ack_timeout:
                self._send_ack(im.src, im.msg_id, im.count, im, NO_TRIGGER)
                im.need_ack = False
            if now - im.last_rx > 600:
                stale.append(key)
        for k in stale:
            del self.inbox[k]

    def _send_ack(self, dst: int, msg_id: int, count: int, im: InMsg | None, trigger: int):
        if im is None:                                   # complete
            base, bm = count, b""
        else:
            base = im.base
            hi = min(count, base + 1 + SACK_BITS)
            top = max((i for i in im.frags if i < hi), default=base)
            nbits = max(0, top - base)
            arr = bytearray((nbits + 7) // 8)
            for i in im.frags:
                j = i - base - 1
                if 0 <= j < nbits:
                    arr[j >> 3] |= 0x80 >> (j & 7)
            bm = bytes(arr)
        snr = self._last_rx_snr.get(dst)
        fb = -128 if snr is None else int(max(-127, min(127, round(2 * snr))))
        body = (HDR.pack(T_ACK, PRIO_CONTROL, dst, self.addr, msg_id)
                + struct.pack(">HHHb", count, base, trigger, fb) + bm)
        self.radio.send(code_index_for(dst), add_crc(body), urgent=True)
        self.stats["acks_tx"] += 1

    def incoming_progress(self) -> list[dict]:
        with self.lock:
            return [dict(src=im.src, id=im.msg_id, have=len(im.frags), count=im.count)
                    for im in self.inbox.values()]
