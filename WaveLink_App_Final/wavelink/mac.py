"""Link layer: addressing, framing, CRC-32, fragmentation, pipelined selective-repeat ARQ,
adaptive modulation and coding.

MAC frame (= one PHY payload)::

    0     1      2    3    4..5     6..7       8..9        10..                       last 4
    type  flags  dst  src  msg_id   frag_idx   frag_count  payload                    CRC-32  (DATA)
    type  flags  dst  src  msg_id   frag_count base        trigger(2) snr(1) SACK bitmap  CRC-32  (ACK)
    type  flags  dst  src  n        n x (addr, Es/N0 heard)   payload (beacon info)   CRC-32  (BEACON)
    type  flags  dst  src  0        payload (game clock sync, race car states)        CRC-32  (DGRAM)

flags: bits 0-1 priority (0 bulk .. 3 control), bit 2 ACK-request, bits 3-7 transmit power
backoff in dB (0 = full power, up to 31) of this very frame.

Reliability (selective repeat with cumulative + selective ACKs):
  * The sender streams fragments, sets the ACK-request flag on every W-th frame
    (W chosen so a window is ~0.5 s on air) and then pauses for an "ACK gap" until
    the ACK arrives (or ack_gap passes).  The gap keeps the receiver from having to
    transmit its ACK on top of incoming frames: its own transmitter would swamp them.
  * The receiver answers each ACK request immediately with
        base   = first fragment it is still missing (everything below is received)
        bitmap = which of the next fragments (up to 1024) it already holds
        trigger= the fragment index that requested this ACK
        snr    = the despread Es/N0 it measured on that frame plus the frame's power backoff
                 (= the path at full power; link-adaptation and power-control feedback).
  * Any in-flight fragment sent *before* the trigger frame and not acknowledged is
    known lost and is queued for retransmission straight away (no timeout needed).
  * A timeout (no ACK) resends everything in flight with a random back-off that grows
    with each failure; after max_retries consecutive failures the message fails.
  * Duplicates are re-ACKed but delivered once.  Text = 1 fragment -> stop-and-wait.

Datagrams (DGRAM) are sent once, never ACKed or repeated, at the most robust modulation and
coding and ahead of anything queued on their code: for traffic where a late copy is useless
(the game center's clock-sync pings and race car states).  A queued message that has not
started yet can be withdrawn (the UNO host replaces a player's stale view with a newer one).

Transmit power control (the near-far problem).  A station close to a receiver arrives far
stronger than one further away; after despreading, the strong station's code still leaks
about 1/31 of its power into the weak one's (Gold-code cross-correlation), so without
control a near station transmitting can drown a far one.  Every station therefore sends
each frame at the lowest power its receiver needs, and no lower:
  * Every frame carries its own power backoff (flags bits 3-7).  A receiver adds it to the
    Es/N0 it measures, so it knows each path at full power whatever power was used.
  * It reports that number back: in every ACK (snr byte) and in its beacons (n entries of
    address + Es/N0 heard, every 5 s), so a station learns how every other one hears it
    even with no traffic between them.
  * The sender backs off to the target: what the fastest allowed *coded* modulation needs plus
    3 dB (16-QAM 5/6: 14 + 3 = 17 dB).  The target sets how much a transmission raises the
    noise of everything else heard nearby (its despread Es/N0 minus the 14.9 dB processing
    gain): at 17 dB a neighbour's weak link loses ~4 dB, at 25.5 dB (what uncoded 16-QAM would
    need) ~11 dB, at full power it is lost.  Uncoded 16-QAM would be only 20 % faster for
    8.5 dB more power, so strong links stop at 16-QAM 5/6 (--pc-target 25.5 keeps it).
    It turns power up at once and down in steps of 2 dB or more (no hunting), and goes back to
    full power when a station's reports stop (60 s).  At most 30 dB of backoff.
  * The reports are measured with whatever else is on the air, i.e. they are SINR: a link that
    suffers interference reports less, so its sender turns up by itself (the classic
    Foschini-Miljanic iteration, which settles wherever every link can reach its target).
    Frame losses step the modulation down, not the power up: more power for one link is more
    noise for all the others, and every sender doing that ends where it started, at full power.
  * Broadcasts (group messages, race car states) go at the power the weakest station that
    is on the air needs; beacons always at full power, so new stations can find us.
The backoff is applied digitally to each burst (the radio's TX attenuation sets the ceiling).

Adaptive modulation and coding (hybrid ARQ type I): each DATA frame to a station is
sent with the fastest modulation + LDPC code rate whose SNR requirement (MCS table) is
met by the SNR that station reports in its ACKs (falling back to the SNR we measure on
its frames before any ACK has arrived), and stepped down for a while if a window loses
frames.  ACKs, beacons and broadcasts use the most robust entry (QPSK, rate 1/2: the
airtime of uncoded BPSK with ~9 dB more margin).  --mod / --fec restrict the table.
"""
from __future__ import annotations

import collections
import enum
import math
import random
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

from .config import BROADCAST_ADDR, code_index_for

T_DATA, T_ACK, T_BEACON, T_DGRAM = 1, 2, 3, 4
F_ACKREQ = 0x04
BO_SHIFT = 3                           # flags bits 3-7: this frame's transmit power backoff (dB)
HDR = struct.Struct(">BBBBH")          # type, flags, dst, src, msg_id   (6 bytes)
CRC_LEN = 4
MAX_FRAGS = 0xFFFF                     # 16-bit fragment index
SACK_BITS = 1024                       # bitmap span carried by one ACK
NO_TRIGGER = 0xFFFF

PRIO_BULK, PRIO_NORMAL, PRIO_HIGH, PRIO_CONTROL = 0, 1, 2, 3

PC_MARGIN_DB = 3.0                     # power control: target = fastest coded MCS threshold + this
PC_MAX_BACKOFF = 30                    # dB (31 fits the flags; at 30 dB a burst still spans ~30 DAC steps)
PC_STALE_S = 60.0                      # no report for this long: back to full power
PC_REPORT_S = 60.0                     # stations heard within this long go into the beacon report

# Modulation and coding table: (name, despread Es/N0 in dB needed for ~100 % delivery of 1 KB
# frames + about 1 dB margin, information bits per symbol).  Measured with the full receiver
# (detection, header, tracking, LDPC decoder) on the numpy PHY: coded QPSK below ~5 dB is limited
# by burst detection, not by the code.  Uncoded rates are the old BPSK/QPSK/16-QAM thresholds.
MCS_TABLE = [
    ("bpsk-1/2", 5.0, 0.5), ("bpsk-3/4", 6.0, 0.75), ("bpsk-5/6", 7.0, 0.833), ("bpsk", 13.0, 1.0),
    ("qpsk-1/2", 5.0, 1.0), ("qpsk-3/4", 6.0, 1.5), ("qpsk-5/6", 7.0, 1.667), ("qpsk", 15.5, 2.0),
    ("16qam-1/2", 9.5, 2.0), ("16qam-2/3", 11.0, 2.667), ("16qam-3/4", 12.5, 3.0),
    ("16qam-5/6", 14.0, 3.333), ("16qam", 22.5, 4.0),
]
MCS_THRESH = {m: t for m, t, _ in MCS_TABLE}
MCS_BITS = {m: b for m, _, b in MCS_TABLE}
# automatic ladder (both --mod and --fec auto): every step is faster than the one below
AUTO_LADDER = ["qpsk-1/2", "qpsk-3/4", "qpsk-5/6", "16qam-1/2", "16qam-2/3", "16qam-3/4",
               "16qam-5/6", "16qam"]


def mcs_ladder(mod_policy: str = "auto", fec_policy: str = "auto") -> list[str]:
    """MCS entries allowed by --mod / --fec, slowest first.  Coded entries need the 'rate' form
    ('16qam-3/4'); fec 'off' keeps the uncoded ones."""
    if mod_policy == "auto" and fec_policy == "auto":
        return list(AUTO_LADDER)
    out = []
    for m, _, _ in MCS_TABLE:
        mod, _, rate = m.partition("-")
        if mod_policy != "auto" and mod != mod_policy:
            continue
        if fec_policy == "off" and rate:
            continue
        if fec_policy not in ("auto", "off") and rate != fec_policy:
            continue
        if fec_policy == "auto" and mod_policy != "auto" and rate == "2/3":
            continue                                   # adds nothing next to 3/4 (detection-limited)
        out.append(m)
    if not out:
        raise ValueError(f"no modulation/coding matches --mod {mod_policy} --fec {fec_policy}")
    return sorted(out, key=lambda m: (MCS_BITS[m], MCS_THRESH[m]))


def add_crc(b: bytes) -> bytes:
    return b + struct.pack("<I", zlib.crc32(b) & 0xFFFFFFFF)   # same byte order as GNU Radio's Stream CRC32


def check_crc(b: bytes) -> bytes | None:
    if len(b) < HDR.size + CRC_LEN:
        return None
    body, crc = b[:-CRC_LEN], struct.unpack("<I", b[-CRC_LEN:])[0]
    return body if zlib.crc32(body) & 0xFFFFFFFF == crc else None


class MsgState(enum.Enum):
    QUEUED = "queued"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"            # taken back before its first frame was sent


FINISHED = (MsgState.DELIVERED, MsgState.FAILED, MsgState.WITHDRAWN)


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
    req_done: float = 0.0              # when the last ACK-requesting frame left the antenna
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
                 half_duplex_defer: float = 0.0, mod_policy: str = "auto", fec_policy: str = "auto",
                 window_airtime: float = 0.5, ack_gap: float = 0.05,
                 on_message=None, on_beacon=None, on_datagram=None, log=None):
        self.radio = radio
        self.addr = addr
        self.ack_timeout = ack_timeout
        self.max_retries = max_retries
        # >0: wait up to this long for an incoming burst to finish before starting
        # to send to a station (on hardware our own TX deafens our RX)
        self.half_duplex_defer = half_duplex_defer
        self.mod_policy = mod_policy            # auto | bpsk | qpsk | 16qam
        self.fec_policy = fec_policy            # auto | off | 1/2 | 2/3 | 3/4 | 5/6
        self.window_airtime = window_airtime
        self.ack_gap = ack_gap                  # max pause after an ACK request (s)
        self.on_message = on_message or (lambda src, data, meta: None)
        self.on_beacon = on_beacon or (lambda src, data, frame: None)
        self.on_datagram = on_datagram or (lambda src, dst, data, frame: None)
        self.log = log or (lambda *a: None)
        self.max_frag = radio.cfg.max_phy_payload - 10 - CRC_LEN
        self.lock = threading.RLock()
        self.out: dict[int, list[OutMsg]] = collections.defaultdict(list)   # dst -> msgs
        self.inbox: dict[tuple, InMsg] = {}
        self.done_in: collections.OrderedDict = collections.OrderedDict()   # (src,id)->count
        self.dropped_in: collections.OrderedDict = collections.OrderedDict()  # (src,id) cancelled incoming
        self._next_id = random.randrange(0, 1 << 16)
        self.stats = collections.Counter()
        self.link_quality: dict[int, dict] = {}
        self.mod_penalty: dict[int, tuple[int, float]] = {}                 # dst -> (levels, until)
        self._last_rx_snr: dict[int, float] = {}                            # src -> full-power Es/N0 of last data
        self.rtt: dict[int, float] = {}         # dst -> smoothed ACK round trip (s), measured
        self._last_loss: dict[int, float] = {}  # dst -> time of the last lossy window
        self.last_heard: dict[int, float] = {}  # src -> wall-clock time of its last valid frame
        self._running = False
        # CRC-32: added/checked here, unless the PHY does it (the GNU Radio flowgraph
        # has stock CRC32 blocks; frames failing them never reach us)
        if getattr(radio, "provides_crc", False):
            self._seal = lambda b: b
            self._open = lambda b: b if len(b) >= HDR.size else None
        else:
            self._seal, self._open = add_crc, check_crc
        radio.on_frame = self._on_phy_frame
        radio.on_reject = lambda reason, code: self.stats.update([reason])
        # can the PHY put a frame ahead of the ones already queued on its code?  (the numpy PHY's
        # mixer can; the GNU Radio flowgraph's code lanes are first-in first-out)
        self._urgent_ok = getattr(radio, "supports_urgent", True)
        # transmit power control
        self.tx_bo: dict[int, int] = {}         # dst -> backoff in use (dB)
        self._pc_on = True
        self.pc_target: float | None = None     # Es/N0 (dB) to aim at; None = fastest MCS + margin
        self._pc_refreshed = 0.0
        self.pc_status: dict = {}

    # ================================================================ adaptive modulation + coding
    @property
    def ladder(self) -> list[str]:
        key = (self.mod_policy, self.fec_policy)
        if getattr(self, "_ladder_key", None) != key:
            self._ladder_key, self._ladder = key, mcs_ladder(*key)
        return self._ladder

    def control_mcs(self) -> str:
        """ACKs, beacons, broadcasts: the most robust allowed entry."""
        return self.ladder[0]

    def _level(self, dst: int) -> int:
        lad = self.ladder
        q = self.link_quality.get(dst)
        snr = None
        if q:
            if q.get("fb_full") is not None:                # the receiver's report, at the power we use
                snr = q["fb_full"] - (self.tx_bo.get(dst, 0) if self.pc_active else 0)
            else:                                           # before any report: how we hear it
                snr = q.get("esn0_full", q.get("esn0_avg"))
        if snr is None:
            return 0
        lvl = max([0] + [i for i, m in enumerate(lad) if snr >= MCS_THRESH[m]])
        pen, until = self.mod_penalty.get(dst, (0, 0.0))
        if self.radio.now() < until:
            lvl = max(0, lvl - pen)
        return lvl

    def mod_for(self, dst: int) -> str:
        """Modulation and coding for data to dst, e.g. '16qam-3/4'."""
        return self.ladder[self._level(dst)]

    def _note_loss(self, dst: int, lost: int, total: int):
        if total >= 4 and lost / total > 0.2:
            # one lossy window may just be a collision; step down only if losses repeat
            now = self.radio.now()
            prev = self._last_loss.get(dst, -1e9)
            self._last_loss[dst] = now
            if now - prev > 3.0:
                return
            pen, until = self.mod_penalty.get(dst, (0, 0.0))
            pen = min(pen + 1, len(self.ladder) - 1) if self.radio.now() < until else 1
            self.mod_penalty[dst] = (pen, self.radio.now() + 10.0)
            self.stats["mod_stepdowns"] += 1

    # ================================================================ transmit power control
    @property
    def power_control(self) -> bool:
        return self._pc_on

    @power_control.setter
    def power_control(self, on: bool):
        self._pc_on = bool(on)
        if not on:                                       # full power everywhere, at once
            self.tx_bo.clear()

    @property
    def pc_active(self) -> bool:
        """Power control on, and a PHY that can scale each burst (the numpy PHY, the simulator and a
        flowgraph built from the current .grc can)."""
        return self._pc_on and getattr(self.radio, "supports_tx_gain", False)

    def pc_target_db(self) -> float:
        """Es/N0 a receiver should hear us at: the fastest coded entry allowed (16-QAM 5/6) + 3 dB;
        with --fec off, the fastest entry."""
        if self.pc_target is not None:
            return self.pc_target
        coded = [m for m in self.ladder if "-" in m]
        return MCS_THRESH[(coded or self.ladder)[-1]] + PC_MARGIN_DB

    def backoff_for(self, dst: int) -> int:
        """Power backoff (dB) for a frame to dst: how far above the target dst hears us at full
        power, from its reports."""
        if not self.pc_active:
            return 0
        if dst == BROADCAST_ADDR:
            return self.broadcast_backoff()
        q = self.link_quality.get(dst) or {}
        full = q.get("fb_full")
        if full is None or time.time() - q.get("fb_t", 0.0) > PC_STALE_S:
            want = 0
        else:
            want = int(max(0, min(PC_MAX_BACKOFF, math.floor(full - self.pc_target_db()))))
        cur = self.tx_bo.get(dst, 0)
        # more power at once; less only when it is worth 2 dB or more (no hunting on noise)
        if want < cur or want >= cur + 2:
            if want != cur:
                self.stats["power_changes"] += 1
            cur = self.tx_bo[dst] = want
        return cur

    def broadcast_backoff(self) -> int:
        """Broadcasts must reach every station on the air: the power the weakest one needs."""
        if not self.pc_active:
            return 0
        now = time.time()
        peers = [a for a, t in self.last_heard.items() if now - t < 30 and a != BROADCAST_ADDR]
        return min((self.backoff_for(a) for a in peers), default=0)

    def _pc_refresh(self):
        """For the dashboard and the MCS choice: our power and the Es/N0 expected at each peer."""
        bc = self.broadcast_backoff()
        self.pc_status = dict(on=self.pc_active, target=round(self.pc_target_db(), 1), broadcast_db=-bc,
                              supported=getattr(self.radio, "supports_tx_gain", False))
        for a, q in list(self.link_quality.items()):
            bo = self.backoff_for(a)
            q["tx_pwr_db"] = -bo
            if q.get("fb_full") is not None:
                q["fb_esn0"] = round(q["fb_full"] - bo, 1)

    def _note_report(self, src: int, full: float, weight: float):
        """src told us the Es/N0 at which it hears us at full power."""
        q = self.link_quality.setdefault(src, {})
        q["fb_full"] = full if q.get("fb_full") is None else (1 - weight) * q["fb_full"] + weight * full
        q["fb_t"] = time.time()

    def beacon_report(self) -> bytes:
        """(address, Es/N0 at full power, 0.5 dB) of each station heard lately."""
        now, out = time.time(), b""
        for a, q in sorted(self.link_quality.items()):
            if q.get("esn0_full") is not None and now - self.last_heard.get(a, 0) < PC_REPORT_S and len(out) < 32:
                out += struct.pack(">Bb", a, int(max(-127, min(127, round(2 * q["esn0_full"])))))
        return out

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
                    self._tx_data(m, i, ack_req=False, mod=self.control_mcs())
                m.state = MsgState.DELIVERED
            else:
                self.out[dst].append(m)
        self._emit(m, "queued")
        return m

    def _send(self, code: int, body: bytes, bo: int, **kw):
        """Hand a frame to the PHY at a power backoff of bo dB."""
        if bo:
            kw["gain_db"] = -bo
        return self.radio.send(code, self._seal(body), **kw)

    def send_beacon(self, info: bytes):
        # always at full power (stations that haven't heard us yet must find us); it carries how
        # we hear each station, for their power control
        rep = self.beacon_report()
        body = HDR.pack(T_BEACON, PRIO_CONTROL, BROADCAST_ADDR, self.addr, len(rep) // 2) + rep + info
        self.radio.send(code_index_for(BROADCAST_ADDR), self._seal(body), mod=self.control_mcs())
        self.stats["beacons_tx"] += 1

    def max_datagram_bytes(self) -> int:
        return self.radio.cfg.max_phy_payload - HDR.size - CRC_LEN

    def send_datagram(self, dst: int, payload: bytes, urgent: bool = True, at: float | None = None,
                      latest: float | None = None):
        """One unacknowledged frame, handed to the PHY now (it bypasses the ARQ queues; with
        urgent=True it also goes ahead of frames already waiting on that code) at the most
        robust modulation and coding.  at: start it at this radio-clock time, if the radio can
        (time slots).  Returns when it should have left the antenna, None if 'at' had passed."""
        if len(payload) > self.max_datagram_bytes():
            raise ValueError(f"datagram too large ({len(payload)} > {self.max_datagram_bytes()} bytes)")
        bo = self.backoff_for(dst)
        body = HDR.pack(T_DGRAM, PRIO_CONTROL | (bo << BO_SHIFT), dst, self.addr, 0) + payload
        kw = {"at": at, "latest": latest} if at is not None and getattr(self.radio, "timed_tx", False) else {}
        done = self._send(code_index_for(dst), body, bo, urgent=urgent, mod=self.control_mcs(), **kw)
        self.stats["dgram_tx" if done is not None else "dgram_late"] += 1
        return done

    def datagram_airtime(self, n_payload: int) -> float:
        """Seconds on air of a datagram carrying n_payload bytes."""
        extra = 0 if getattr(self.radio, "provides_crc", False) else CRC_LEN
        return self.radio.airtime(HDR.size + n_payload + extra, self.control_mcs())

    def withdraw(self, m: OutMsg) -> bool:
        """Take back a queued message whose first frame has not been sent yet.  Returns
        False (and changes nothing) once it has started."""
        with self.lock:
            if m.state != MsgState.QUEUED or m.first_tx is not None or m.dst == BROADCAST_ADDR:
                return False
            lst = self.out.get(m.dst, [])
            if m in lst:
                lst.remove(m)
            m.state = MsgState.WITHDRAWN
            self.stats["msgs_withdrawn"] += 1
        self._emit(m, "withdrawn")
        return True

    def cancel(self, dst: int, msg_id: int):
        with self.lock:
            for m in self.out.get(dst, []):
                if m.msg_id == msg_id:
                    m.state = MsgState.FAILED
                    self._emit(m, "failed")
            self.out[dst] = [m for m in self.out.get(dst, []) if m.state not in FINISHED]

    def find_out(self, dst: int, msg_id: int) -> OutMsg | None:
        with self.lock:
            for m in self.out.get(dst, []):
                if m.msg_id == msg_id:
                    return m
        return None

    def drop_incoming(self, src: int, msg_id: int) -> bool:
        """Forget a partly received message and ignore its remaining frames (transfer cancelled)."""
        key = (src, msg_id)
        with self.lock:
            had = self.inbox.pop(key, None) is not None
            self.dropped_in[key] = time.time()
            while len(self.dropped_in) > 256:
                self.dropped_in.popitem(last=False)
            self.stats["incoming_dropped"] += 1
        return had

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
        bo = self.backoff_for(m.dst)
        flags = (m.prio & 3) | (F_ACKREQ if ack_req else 0) | (bo << BO_SHIFT)
        body = HDR.pack(T_DATA, flags, m.dst, self.addr, m.msg_id) + struct.pack(
            ">HH", i, len(m.frags)) + m.frags[i]
        m.tx_frames += 1
        self.stats["data_tx"] += 1
        self.stats["data_tx_" + mod] += 1
        urgent = m.prio >= PRIO_HIGH and self._urgent_ok and m.dst != BROADCAST_ADDR
        return self._send(code_index_for(m.dst), body, bo, urgent=urgent, mod=mod)

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
            if time.monotonic() - self._pc_refreshed > 0.5:
                self._pc_refreshed = time.monotonic()
                self._pc_refresh()
            self._receiver_timers(now)
            for dst, msgs in list(self.out.items()):
                msgs = [m for m in msgs if m.state not in FINISHED]
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
                    # high priority (game center, urgent chat) jumps the PHY queue where it can,
                    # so it does not wait behind a window of file-transfer frames
                    jump = m.prio >= PRIO_HIGH and self._urgent_ok
                    while (m.has_work() and now >= m.not_before and now >= m.hold_until
                           and len(m.inflight) < 2 * W
                           and (jump or self.radio.tx_backlog(code) < self.window_airtime)):
                        self._send_one(m, now, W, mod)
                    if self.radio.tx_backlog(code) >= self.window_airtime and not jump:
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
        if len(m.frags) <= 2:
            # short messages: one lost frame costs a whole round trip, rate hardly matters
            lad = self.ladder
            mod = lad[max(0, lad.index(mod) - 2)] if mod in lad else mod
        m.tx_seq += 1
        m.inflight[i] = m.tx_seq
        m.last_seq[i] = m.tx_seq
        done = self._tx_data(m, i, ack_req, mod)
        ack_air = self.radio.airtime(25 + SACK_BITS // 8, self.control_mcs())
        # timers follow the measured round trip (PHY + driver latency differ a lot
        # between the simulator, the Python PHY and the GNU Radio flowgraph)
        rtt = self.rtt.get(m.dst)
        timeout = self.ack_timeout if rtt is None else max(self.ack_timeout, 2.5 * rtt + ack_air)
        m.deadline = max(done, now) + timeout + 2 * ack_air
        if ack_req:
            m.req_done = max(done, now)
            if m.has_work():
                gap = self.ack_gap if rtt is None else min(max(self.ack_gap, 1.3 * rtt + 0.02),
                                                           max(self.ack_gap, 1.0))
                m.hold_until = m.req_done + gap + ack_air
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
                if trigger != NO_TRIGGER and m.req_done:
                    sample = self.radio.now() - m.req_done
                    if 0 < sample < 5.0:
                        r = self.rtt.get(src)
                        self.rtt[src] = sample if r is None else 0.8 * r + 0.2 * sample
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
        body = self._open(f.payload)
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
        self.last_heard[src] = time.time()               # presence: any frame counts
        self.radio.last_frame = f                        # dashboard: last frame from a peer
        q = self.link_quality.setdefault(src, {})
        es = getattr(f, "esn0_db", f.snr_db)
        bo = flags >> BO_SHIFT                           # the power the sender used for this frame
        full = es + bo                                   # this path at full power
        q["esn0_avg"] = es if "esn0_avg" not in q else 0.8 * q["esn0_avg"] + 0.2 * es
        # (each frame's estimate is good to ~0.5 dB; beacons may be the only frames, every 5 s)
        q["esn0_full"] = round(full if q.get("esn0_full") is None else 0.5 * q["esn0_full"] + 0.5 * full, 2)
        q.update(snr_db=round(f.snr_db, 1), esn0_db=round(es, 1), cfo_hz=round(f.cfo_hz), rx_pwr_db=-bo,
                 mod=getattr(f, "mcs", None) or getattr(f, "mod", "bpsk"), t=time.time())
        rest = body[HDR.size:]
        if ftype == T_BEACON:
            n = msg_id                                   # entries of the power-control report
            if 0 < n <= 16 and len(rest) >= 2 * n:
                for k in range(n):
                    a, v = struct.unpack_from(">Bb", rest, 2 * k)
                    if a == self.addr:
                        self._note_report(src, v / 2.0, 0.6)
                rest = rest[2 * n:]
            self.on_beacon(src, rest, f)
        elif ftype == T_ACK:
            count, base, trigger, fb = struct.unpack_from(">HHHb", rest)
            if fb != -128:
                self._note_report(src, fb / 2.0, 0.4)
            self._on_ack(src, msg_id, count, base, trigger, rest[7:])
        elif ftype == T_DATA:
            idx, count = struct.unpack_from(">HH", rest)
            self._on_data(src, dst, msg_id, flags, idx, count, rest[4:], f)
        elif ftype == T_DGRAM:
            self.stats["dgram_rx"] += 1
            try:
                self.on_datagram(src, dst, rest, f)
            except Exception as e:
                self.log("on_datagram error", repr(e))

    def _on_data(self, src, dst, msg_id, flags, idx, count, payload, f):
        key = (src, msg_id)
        ack_req = bool(flags & F_ACKREQ)
        with self.lock:
            if key in self.dropped_in:                   # a transfer that was cancelled
                self.stats["dropped_frames"] += 1
                return
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
            self._last_rx_snr[src] = getattr(f, "esn0_db", f.snr_db) + (flags >> BO_SHIFT)   # at full power
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
        snr = self._last_rx_snr.get(dst)                 # how we hear dst at full power
        fb = -128 if snr is None else int(max(-127, min(127, round(2 * snr))))
        bo = self.backoff_for(dst)
        body = (HDR.pack(T_ACK, PRIO_CONTROL | (bo << BO_SHIFT), dst, self.addr, msg_id)
                + struct.pack(">HHHb", count, base, trigger, fb) + bm)
        self._send(code_index_for(dst), body, bo, urgent=True, mod=self.control_mcs())
        self.stats["acks_tx"] += 1

    def incoming_progress(self) -> list[dict]:
        with self.lock:
            return [dict(src=im.src, id=im.msg_id, have=len(im.frags), count=im.count)
                    for im in self.inbox.values()]
