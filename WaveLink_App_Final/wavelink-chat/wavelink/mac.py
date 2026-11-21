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
stronger than one further away; with spread ALOHA's asynchronous Gold codes the strong
station's code still leaks about 1/31 of its power into the weak one's after despreading, so
without control a near station transmitting can drown a far one.  (With TD-CDMA only one
station transmits at a time: there it keeps receivers from being overdriven and the band
quieter.)  Every station therefore sends each frame at the lowest power its receiver needs,
and no lower:
  * Every frame carries its own power backoff (flags bits 3-7).  A receiver adds it to the
    Es/N0 it measures, so it knows each path at full power whatever power was used.
  * It reports that number back: in every ACK (snr byte) and in its beacons (n entries of
    address + Es/N0 heard, every 5 s), so a station learns how every other one hears it
    even with no traffic between them.
  * The sender backs off to the target: what the fastest allowed modulation needs plus 3 dB
    (TD-CDMA, uncoded 64-QAM: 27.5 + 3 = 30.5 dB; spread ALOHA, uncoded 16-QAM: 22.5 + 3 =
    25.5 dB).  Only power a link cannot use is taken away: it keeps its fastest modulation
    (unless interference at its receiver lowers its rate, see below), and a link that needs
    less than the target keeps full power.  With spread ALOHA a station next to its receiver
    then arrives at 25.5 dB instead of 40-50 dB, which
    is what drowns a weak link at a receiver close by: a transmission raises the noise of
    everything else heard there by its chip-level SNR (despread Es/N0 minus the 14.9 dB
    processing gain), ~11 dB at 25.5 dB instead of 25-35 dB at full power.  --pc-target 17
    (16-QAM 5/6 + 3 dB) protects weak links more (~4 dB) but makes every strong link coded,
    i.e. slower and with an LDPC decode for every frame.
    It turns power up at once and down in steps of 2 dB or more (no hunting), and goes back to
    full power when a station's reports stop (60 s).  At most 30 dB of backoff.
  * Outer loop: a link that keeps losing frames while backed off (two lossy windows within 3 s,
    the same signal that steps its modulation down) although its receiver's latest report shows
    no interference, aims 3 dB higher, up to +12 dB, and 1 dB lower again for every 30 s without
    losses.  Radio imperfections and fading that the Es/N0 estimate does not show are handled per
    link, and the worst case is full power, i.e. what the link did without power control.
  * Power from the path, rate from the interference.  The reports are measured with whatever
    else is on the air (SINR).  The power follows the *path*: the best recent report, falling by
    0.5 dB/s, so a burst of interference does not make the sender louder (louder would be more
    noise for the station causing it, which turns up in turn, until both are back at full power).
    The modulation follows the latest reports (SINR), so under interference a link steps down
    in rate instead.
  * Broadcasts (group messages, race car states, beacons) go at the power the weakest station
    on the air needs.  A station that has not reported yet counts as needing full power, so a
    newcomer is found as before.  (All bursts on the broadcast code at one level: the receiver
    rejects a burst much weaker than one that just started on the same code as a sidelobe.)
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
import json
import math
import os
import random
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field

from .config import BROADCAST_ADDR, code_index_for

T_DATA, T_ACK, T_BEACON, T_DGRAM = 1, 2, 3, 4
T_SLOT, T_FWD = 5, 6                   # TD-CDMA slot header; a data frame passed on by a relay
FWD = struct.Struct(">BHH")            # T_FWD: relay, frag_idx, frag_count (then the payload)
MAC_MODES = ("tdcdma", "aloha")
F_ACKREQ = 0x04
BO_SHIFT = 3                           # flags bits 3-7: this frame's transmit power backoff (dB)
HDR = struct.Struct(">BBBBH")          # type, flags, dst, src, msg_id   (6 bytes)
CRC_LEN = 4
MAX_FRAGS = 0xFFFF                     # 16-bit fragment index
SACK_BITS = 1024                       # bitmap span carried by one ACK
REC_SPAN = 512                         # TD-CDMA: fragments beyond the base one ACK record reports
                                       # (tdma.REC_MAX_BITMAP bytes); received ones further on are unknown
NO_TRIGGER = 0xFFFF
GROUP_RETRIES = 4                      # TD-CDMA group message: timeouts before a member is tried alone
INBOX_MAX_AGE_S = 600.0                # a partly received message is forgotten after this long without a
                                       # fragment (the application gives up sooner, and tells the sender)
ID_BLOCK = 256                         # message ids reserved in the id file at a time (set_id_store)

PRIO_BULK, PRIO_NORMAL, PRIO_HIGH, PRIO_CONTROL = 0, 1, 2, 3

PC_MARGIN_DB = 3.0                     # power control: target = fastest MCS threshold + this
PC_MAX_BACKOFF = 30                    # dB (31 fits the flags; at 30 dB a burst still spans ~30 DAC steps)
PC_STALE_S = 60.0                      # no report for this long: back to full power
PC_REPORT_S = 60.0                     # stations heard within this long go into the beacon report
PC_OUTER_STEP_DB = 3.0                 # outer loop: a lossy link aims this much higher ...
PC_OUTER_MAX_DB = 12.0                 # ... up to this much ...
PC_OUTER_DECAY_S = 30.0                # ... and 1 dB lower again per this many seconds without losses
PC_PATH_FALL_DB_S = 0.5                # the path estimate (best recent report) falls this fast

# Modulation and coding table: (name, despread Es/N0 in dB needed for ~100 % delivery of 1 KB
# frames + about 1 dB margin, information bits per symbol).  Measured with the full receiver
# (detection, header, tracking, LDPC decoder) on the numpy PHY: coded QPSK below ~5 dB is limited
# by burst detection, not by the code.  Uncoded rates are the old BPSK/QPSK/16-QAM thresholds.
# 64-QAM (Walsh codes, 1 KB frames, carrier offset up to 12 kHz, 20 ppm clock offset): 95-100 %
# delivered at 13 / 15 / 17 / 18 dB at rate 1/2 / 2/3 / 3/4 / 5/6 and at 26 dB uncoded, all from 1 dB
# more; the table adds 1-1.5 dB.
MCS_TABLE = [
    ("bpsk-1/2", 5.0, 0.5), ("bpsk-3/4", 6.0, 0.75), ("bpsk-5/6", 7.0, 0.833), ("bpsk", 13.0, 1.0),
    ("qpsk-1/2", 5.0, 1.0), ("qpsk-3/4", 6.0, 1.5), ("qpsk-5/6", 7.0, 1.667), ("qpsk", 15.5, 2.0),
    ("16qam-1/2", 9.5, 2.0), ("16qam-2/3", 11.0, 2.667), ("16qam-3/4", 12.5, 3.0),
    ("16qam-5/6", 14.0, 3.333), ("16qam", 22.5, 4.0),
    ("64qam-1/2", 14.0, 3.0), ("64qam-2/3", 16.5, 4.0), ("64qam-3/4", 17.5, 4.5),
    ("64qam-5/6", 19.0, 5.0), ("64qam", 27.5, 6.0),
]
MCS_THRESH = {m: t for m, t, _ in MCS_TABLE}
MCS_BITS = {m: b for m, _, b in MCS_TABLE}
# automatic ladder (both --mod and --fec auto): every step is faster than the one below, and needs
# more SNR.  (64-QAM 2/3 carries what uncoded 16-QAM does at 6 dB less SNR, so the latter is out;
# 64-QAM 1/2 carries no more than 16-QAM 3/4 and needs more.)
AUTO_LADDER = ["qpsk-1/2", "qpsk-3/4", "qpsk-5/6", "16qam-1/2", "16qam-2/3", "16qam-3/4",
               "16qam-5/6", "64qam-2/3", "64qam-3/4", "64qam-5/6", "64qam"]
# Spread ALOHA (--mac aloha) uses 64-QAM only when --mod 64qam asks for it.  Its stations send
# whenever they like, so each link's power is interference for the others (asynchronous Gold
# codes leak ~1/31 of it into each other): 64-QAM would raise everybody's power-control target
# by 5 dB (a worse near-far problem) for a rate that rarely survives while others are on the
# air.  It keeps the ladder it had: up to uncoded 16-QAM.
AUTO_LADDER_ALOHA = ["qpsk-1/2", "qpsk-3/4", "qpsk-5/6", "16qam-1/2", "16qam-2/3", "16qam-3/4",
                     "16qam-5/6", "16qam"]


def mcs_ladder(mod_policy: str = "auto", fec_policy: str = "auto", qam64: bool = True) -> list[str]:
    """MCS entries allowed by --mod / --fec, slowest first.  Coded entries need the 'rate' form
    ('16qam-3/4'); fec 'off' keeps the uncoded ones.  qam64=False (spread ALOHA): 64-QAM only
    with --mod 64qam."""
    if mod_policy == "auto" and fec_policy == "auto":
        return list(AUTO_LADDER if qam64 else AUTO_LADDER_ALOHA)
    out = []
    for m, _, _ in MCS_TABLE:
        mod, _, rate = m.partition("-")
        if mod_policy != "auto" and mod != mod_policy:
            continue
        if mod_policy == "auto" and mod == "64qam" and not qam64:
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
    first_seq: dict = field(default_factory=dict)    # frag -> first tx sequence number (TD-CDMA)
    tx_seq: int = 0
    since_req: int = 0
    hold_until: float = 0.0            # ACK gap: no new frames before this (or the ACK)
    req_done: float = 0.0              # when the last ACK-requesting frame left the antenna
    cum_base: int = 0
    tx_frames: int = 0
    first_tx: float | None = None
    done_time: float | None = None
    defer_since: float | None = None
    # TD-CDMA: who must receive it (one station, or every member of a group message), per member
    members: set = field(default_factory=set)        # still to deliver to
    acked_by: dict = field(default_factory=dict)     # member -> fragments it has
    base_by: dict = field(default_factory=dict)      # member -> first fragment it still misses
    mstate: dict = field(default_factory=dict)       # member -> sending | delivered | failed | left
    retries_by: dict = field(default_factory=dict)   # member -> timeouts without progress
    retx_set: set = field(default_factory=set)
    sent_t: dict = field(default_factory=dict)       # fragment -> when its slot ended (radio clock)
    via: int | None = None                           # relay in use
    ev_member: int | None = None                     # the member an event is about

    @property
    def progress(self) -> float:
        if self.acked_by and len(self.acked_by) > 1:
            return min(len(s) for s in self.acked_by.values()) / len(self.frags)
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
                 on_message=None, on_beacon=None, on_datagram=None, log=None,
                 mode: str = "aloha", relay: bool = False):
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
        # (11: room for the relay header, so any fragment can be passed on by a relay)
        self.max_frag = radio.cfg.max_phy_payload - 11 - CRC_LEN
        self.lock = threading.RLock()
        self.out: dict[int, list[OutMsg]] = collections.defaultdict(list)   # dst -> msgs
        self.inbox: dict[tuple, InMsg] = {}
        self.done_in: collections.OrderedDict = collections.OrderedDict()   # (src,id)->count
        self.dropped_in: collections.OrderedDict = collections.OrderedDict()  # (src,id) cancelled incoming
        self._next_id = random.randrange(0, 1 << 16)
        self._id_path: str | None = None        # set_id_store: the counter carries on after a restart
        self._id_left = 0
        self._inbox_checked = 0.0
        self.stats = collections.Counter()
        self.link_quality: dict[int, dict] = {}
        self.mod_penalty: dict[int, tuple[int, float]] = {}                 # dst -> (levels, until)
        self._last_rx_snr: dict[int, float] = {}                            # src -> full-power Es/N0 of last data
        self.rtt: dict[int, float] = {}         # dst -> smoothed ACK round trip (s), measured
        self._last_loss: dict[int, float] = {}  # dst -> time of the last lossy window
        self._td_loss: dict[int, list] = {}     # TD-CDMA: station -> [frames lost, frames resolved] lately
        self.last_heard: dict[int, float] = {}  # src -> wall-clock time of its last valid frame
        self.last_heard_r: dict[int, float] = {}  # src -> radio-clock time of its last valid frame
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
        self.pc_extra: dict[int, list] = {}     # dst -> [extra dB on the target (outer loop), since]
        self._pc_on = True
        self.pc_target: float | None = None     # Es/N0 (dB) to aim at; None = fastest MCS + margin
        self._pc_refreshed = 0.0
        self.pc_status: dict = {}
        # medium access: TD-CDMA time slots (needs a PHY that starts bursts on exact samples and
        # reports where it received them) or spread ALOHA
        if mode not in MAC_MODES:
            raise ValueError(f"--mac must be one of {', '.join(MAC_MODES)}")
        self.tdma = None
        if mode == "tdcdma":
            why = self.tdcdma_unsupported()
            if why:
                self.log(f"TD-CDMA not available ({why}): using spread ALOHA")
            else:
                from .tdma import Tdma
                self.tdma = Tdma(self, relay=relay)
        if self.tdma is None and getattr(getattr(radio, "cfg", None), "codes", "gold") == "walsh":
            self.log("spread ALOHA with Walsh codes: bursts from different stations arrive at any code phase, "
                     "where Walsh codes are not orthogonal (use --codes gold with --mac aloha)")

    def tdcdma_unsupported(self) -> str | None:
        r = self.radio
        if not hasattr(r, "send_at") or not hasattr(r, "tx_count"):
            return "this PHY cannot start bursts on exact samples"
        if getattr(r, "supports_send_at", True) is False:
            return "the flowgraph is older than this app: rebuild it with tools/make_grc.py"
        return None

    @property
    def mode(self) -> str:
        return "tdcdma" if self.tdma is not None else "aloha"

    # ================================================================ relaying (TD-CDMA)
    @property
    def relay(self) -> bool:
        return bool(self.tdma and self.tdma.relay_on)

    def set_relay(self, on: bool) -> bool:
        if self.tdma is None:
            return False
        with self.lock:
            self.tdma.set_relay(on)
        return True

    # ================================================================ adaptive modulation + coding
    @property
    def ladder(self) -> list[str]:
        key = (self.mod_policy, self.fec_policy, self.tdma is not None)
        if getattr(self, "_ladder_key", None) != key:
            self._ladder_key, self._ladder = key, mcs_ladder(*key)
        return self._ladder

    def control_mcs(self) -> str:
        """ACKs, beacons, broadcasts: the most robust allowed entry."""
        return self.ladder[0]

    def _level(self, dst: int, bo: int | None = None) -> int:
        lad = self.ladder
        q = self.link_quality.get(dst)
        snr = None
        if bo is None:
            bo = self.tx_bo.get(dst, 0) if self.pc_active else 0
        if q:
            if q.get("fb_full") is not None:                # the receiver's report, at the power we use
                snr = q["fb_full"] - bo
            else:                                           # before any report: how we hear it
                snr = q.get("esn0_full", q.get("esn0_avg"))
        if snr is None:
            return 0
        lvl = max([0] + [i for i, m in enumerate(lad) if snr >= MCS_THRESH[m]])
        pen, until = self.mod_penalty.get(dst, (0, 0.0))
        if self.radio.now() < until:
            lvl = max(0, lvl - pen)
        return lvl

    def mod_for(self, dst: int, bo: int | None = None) -> str:
        """Modulation and coding for data to dst, e.g. '16qam-3/4', for frames sent at a power
        backoff of bo dB (default: the backoff in use)."""
        return self.ladder[self._level(dst, bo)]

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
            # outer loop: a link that keeps losing frames while backed off gets more margin, unless
            # its receiver reports interference (then the lower rate is the answer, not more power)
            q = self.link_quality.get(dst) or {}
            clean = q.get("fb_last") is not None and q.get("fb_path") is not None and q["fb_last"] >= q["fb_path"] - 3.0
            if self.pc_active and self.tx_bo.get(dst, 0) > 0 and clean:
                self.pc_extra_db(dst)                                 # (its decay so far)
                e = self.pc_extra.setdefault(dst, [0.0, now])
                e[0] = min(PC_OUTER_MAX_DB, e[0] + PC_OUTER_STEP_DB)
                e[1] = now
                self.stats["pc_margin_raises"] += 1

    # ================================================================ transmit power control
    @property
    def power_control(self) -> bool:
        return self._pc_on

    @power_control.setter
    def power_control(self, on: bool):
        self._pc_on = bool(on)
        if not on:                                       # full power everywhere, at once
            self.tx_bo.clear()
            self.pc_extra.clear()

    @property
    def pc_active(self) -> bool:
        """Power control on, and a PHY that can scale each burst (the numpy PHY, the simulator and a
        flowgraph built from the current .grc can)."""
        return self._pc_on and getattr(self.radio, "supports_tx_gain", False)

    def pc_target_db(self) -> float:
        """Es/N0 a receiver should hear us at: what the fastest allowed entry needs (with --mod/--fec
        auto: uncoded 64-QAM in TD-CDMA, uncoded 16-QAM in spread ALOHA) + 3 dB, unless --pc-target
        says otherwise."""
        if self.pc_target is not None:
            return self.pc_target
        return MCS_THRESH[self.ladder[-1]] + PC_MARGIN_DB

    def pc_basis(self) -> str:
        """What the target is, in words (dashboard)."""
        if self.pc_target is not None:
            return "set with --pc-target"
        top = self.ladder[-1]
        mod, _, rate = top.partition("-")
        name = {"bpsk": "BPSK", "qpsk": "QPSK", "16qam": "16-QAM", "64qam": "64-QAM"}.get(mod, mod)
        return f"{'uncoded ' if not rate else ''}{name}{' ' + rate if rate else ''} + {PC_MARGIN_DB:g} dB"

    def pc_extra_db(self, dst: int) -> float:
        """The outer loop's extra margin for dst (dB), after its decay."""
        e = self.pc_extra.get(dst)
        if not e or e[0] <= 0:
            return 0.0
        steps = int((self.radio.now() - e[1]) // PC_OUTER_DECAY_S)
        if steps > 0:
            e[0] = max(0.0, e[0] - steps)
            e[1] += steps * PC_OUTER_DECAY_S
        return e[0]

    def backoff_for(self, dst: int) -> int:
        """Power backoff (dB) for a frame to dst: how far above the target (plus the outer loop's
        margin) dst hears us at full power, from its reports."""
        if not self.pc_active:
            return 0
        if dst == BROADCAST_ADDR:
            return self.broadcast_backoff()
        q = self.link_quality.get(dst) or {}
        full = q.get("fb_path")
        if full is None or time.time() - q.get("fb_t", 0.0) > PC_STALE_S:
            want = 0
        else:
            want = int(max(0, min(PC_MAX_BACKOFF, math.floor(full - self.pc_target_db() - self.pc_extra_db(dst)))))
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
        # (a copy: the receive thread adds a station the first time it hears one)
        peers = [a for a, t in list(self.last_heard.items()) if now - t < 30 and a != BROADCAST_ADDR]
        return min((self.backoff_for(a) for a in peers), default=0)

    def _pc_refresh(self):
        """For the dashboard and the MCS choice: our power and the Es/N0 expected at each peer."""
        bc = self.broadcast_backoff()
        self.pc_status = dict(on=self.pc_active, target=round(self.pc_target_db(), 1), basis=self.pc_basis(),
                              broadcast_db=-bc, supported=getattr(self.radio, "supports_tx_gain", False))
        for a, q in list(self.link_quality.items()):
            bo = self.backoff_for(a)
            q["tx_pwr_db"] = -bo
            q["pc_extra_db"] = self.pc_extra_db(a)
            if q.get("fb_full") is not None:
                q["fb_esn0"] = round(q["fb_full"] - bo, 1)

    def _note_report(self, src: int, full: float, weight: float):
        """src told us the Es/N0 (SINR) at which it hears us, scaled to full power.  fb_full: the
        recent average (the modulation); fb_path: the best recent value, falling 0.5 dB/s (the
        power: interference makes reports lower, not the path)."""
        q = self.link_quality.setdefault(src, {})
        now = time.time()
        q["fb_full"] = full if q.get("fb_full") is None else (1 - weight) * q["fb_full"] + weight * full
        p = q.get("fb_path")
        p = None if p is None else p - PC_PATH_FALL_DB_S * max(0.0, now - q.get("fb_t", now))
        q["fb_path"] = round(full if p is None else max(full, p), 2)
        q["fb_last"] = full
        q["fb_t"] = now

    def beacon_report(self, max_age: float | None = None) -> bytes:
        """(address, Es/N0 at full power, 0.5 dB) of each station heard lately: within PC_REPORT_S
        (beacons, for power control), or within max_age seconds of radio time (the link table of a
        TD-CDMA slot header, which relay routes are built from: a station gone must drop out of it
        within seconds, or the others would think it reachable through us)."""
        now, rnow, out = time.time(), self.radio.now(), b""
        for a, q in sorted(self.link_quality.items()):
            if q.get("esn0_full") is None or len(out) >= 32:
                continue
            if max_age is None:
                fresh = now - self.last_heard.get(a, 0) < PC_REPORT_S
            else:
                fresh = rnow - self.last_heard_r.get(a, -1e9) < max_age
            if fresh:
                out += struct.pack(">Bb", a, int(max(-127, min(127, round(2 * q["esn0_full"])))))
        return out

    def window_frames(self, mod: str) -> int:
        air = self.radio.airtime(self.max_frag + 14, mod)
        return max(4, min(64, int(round(self.window_airtime / air))))

    # ================================================================ sending
    def max_message_bytes(self) -> int:
        return self.max_frag * MAX_FRAGS

    def send(self, dst: int, data: bytes, prio: int = PRIO_NORMAL, on_event=None, members=None) -> OutMsg:
        """Queue a reliable message.  dst = BROADCAST_ADDR: a group message; with TD-CDMA it goes
        out once on the broadcast code and every one of `members` acknowledges it (default: every
        station with a slot); with ALOHA it is sent once, unacknowledged."""
        if dst == self.addr:
            raise ValueError("cannot send to self")
        if len(data) > self.max_message_bytes():
            raise ValueError(f"message too large ({len(data)} > {self.max_message_bytes()} bytes)")
        frags = [data[i:i + self.max_frag] for i in range(0, max(len(data), 1), self.max_frag)] or [b""]
        with self.lock:
            m = OutMsg(self._alloc_id(), dst, data, prio, frags, self.radio.now(), on_event)
            if self.tdma is not None:
                if dst == BROADCAST_ADDR:
                    mem = set(members) if members is not None else set(self.tdma.members_now())
                    mem.discard(self.addr)
                    mem.discard(BROADCAST_ADDR)
                    if not mem:
                        raise ValueError("no recipients on the air yet")
                else:
                    mem = {dst}
                m.members = set(mem)
                for a in mem:
                    m.acked_by[a] = set()
                    m.base_by[a] = 0
                    m.mstate[a] = "sending"
                    m.retries_by[a] = 0
                self.out[dst].append(m)
            elif dst == BROADCAST_ADDR:
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
        if self.tdma is not None:                       # goes out in our next slot
            with self.lock:
                self.tdma.beacon = bytes(info)
            return
        # at the broadcast power (what the weakest station on the air needs; full power while any
        # station hasn't reported, e.g. a newcomer); it carries how we hear each station, for
        # their power control
        rep = self.beacon_report()
        bo = self.broadcast_backoff()
        body = HDR.pack(T_BEACON, PRIO_CONTROL | (bo << BO_SHIFT), BROADCAST_ADDR, self.addr, len(rep) // 2) + rep + info
        self._send(code_index_for(BROADCAST_ADDR), body, bo, mod=self.control_mcs())
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
        if self.tdma is not None:                       # in our next slot
            with self.lock:
                q = self.tdma.dgrams[dst]
                q.append(bytes(payload))
                while len(q) > 16:
                    q.popleft()
                    self.stats["dgram_dropped"] += 1
            return self.radio.now() + (self.tdma.cycle_s or 0.1)
        bo = self.backoff_for(dst)
        body = HDR.pack(T_DGRAM, PRIO_CONTROL | (bo << BO_SHIFT), dst, self.addr, 0) + payload
        kw = {"at": at, "latest": latest} if at is not None and getattr(self.radio, "timed_tx", False) else {}
        done = self._send(code_index_for(dst), body, bo, urgent=urgent, mod=self.control_mcs(), **kw)
        self.stats["dgram_tx" if done is not None else "dgram_late"] += 1
        return done

    def add_slot_source(self, fn):
        """TD-CDMA: fn() -> [(dst, payload)] is asked for datagrams each time one of our slots is
        built (the newest game state goes out once per slot, never a stale one)."""
        if self.tdma is not None:
            with self.lock:
                self.tdma.sources.append(fn)

    def remove_slot_source(self, fn):
        if self.tdma is not None:
            with self.lock:
                if fn in self.tdma.sources:
                    self.tdma.sources.remove(fn)

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
        if self._id_path is not None:
            if self._id_left <= 0:
                self._reserve_ids()
            self._id_left -= 1
        self._next_id = (self._next_id + 1) & 0xFFFF
        return self._next_id

    def set_id_store(self, path: str):
        """Keep the message-id counter in a file, so a restarted station carries on where it stopped.
        Its peers remember the ids of the last messages they got from it as finished, and would take a
        new message under an old id for a repeat: they would acknowledge it and throw it away.  Ids are
        reserved ID_BLOCK at a time (one small write per block)."""
        start = None
        try:
            with open(path, encoding="utf-8") as f:
                start = int(json.load(f)["next"]) & 0xFFFF
        except (OSError, ValueError, KeyError, TypeError):
            pass
        with self.lock:
            self._id_path = path
            if start is not None:
                self._next_id = (start - 1) & 0xFFFF     # (_alloc_id counts up first)
            self._reserve_ids()

    def _reserve_ids(self):
        """Write down that ids up to ID_BLOCK past the current one may be used, before using them."""
        nxt = (self._next_id + 1 + ID_BLOCK) & 0xFFFF
        tmp = self._id_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"next": nxt}, f)
            os.replace(tmp, self._id_path)
        except OSError as e:
            self.log("message-id counter not saved", e)
        self._id_left = ID_BLOCK

    def _emit(self, m: OutMsg, ev: str):
        if m.on_event:
            try:
                m.on_event(ev, m)
            except Exception as e:      # never let UI code kill the MAC thread
                self.log("on_event error", e)

    def _tx_data(self, m: OutMsg, i: int, ack_req: bool, mod: str, bo: int | None = None) -> float:
        if bo is None:
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
        hooks = getattr(self.radio, "tick_hooks", None)
        if getattr(self.radio, "drives_mac", False) and hooks is not None:
            hooks.append(self._hook_tick)          # simulator: run in step with the simulated air
        else:
            threading.Thread(target=self._loop, name=f"mac{self.addr}", daemon=True).start()

    def stop(self):
        self._running = False

    def _hook_tick(self):
        if self._running:
            self.tick()

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
            if self.tdma is not None:
                self._td_timers(now)
                for dst in list(self.out):
                    self.out[dst] = [m for m in self.out[dst] if m.state not in FINISHED]
                if now - self._inbox_checked > 5.0:
                    self._inbox_checked = now
                    self._purge_inbox(now, INBOX_MAX_AGE_S)
                self.tdma.service()
                return
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
                # the power first, then the modulation for that power; the frames of this pass use
                # both (a report arriving meanwhile counts from the next pass)
                bo = self.backoff_for(dst)
                mod = self.mod_for(dst, bo)
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
                        self._send_one(m, now, W, mod, bo)
                    if self.radio.tx_backlog(code) >= self.window_airtime and not jump:
                        break

    def _send_one(self, m: OutMsg, now: float, W: int, mod: str, bo: int | None = None):
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
        done = self._tx_data(m, i, ack_req, mod, bo)
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

    # ================================================================ TD-CDMA reliability
    # Fragments go out in our slots; every receiver acknowledges in its next slot header (records:
    # cumulative base + SACK bitmap + the fragment it heard last).  A group message has several
    # members, each with its own acknowledged set; a fragment is done when every member has it.
    def inflight_ok(self, m: OutMsg) -> bool:
        while m.retx and m.retx[0] in m.acked:          # (acknowledged meanwhile)
            m.retx_set.discard(m.retx.popleft())
        if m.retx:
            return True
        if m.next_new >= len(m.frags):
            return False
        lo = min((m.base_by.get(a, 0) for a in m.members), default=m.cum_base)
        return m.next_new - lo < REC_SPAN - 16 and len(m.inflight) < 900

    def mark_sent(self, m: OutMsg, i: int, t_end: float, relay: int | None):
        """Fragment i of m has been put into one of our slots, which ends at radio time t_end."""
        if m.retx and m.retx[0] == i:
            m.retx.popleft()
            m.retx_set.discard(i)
            self.stats["retransmissions"] += 1
        else:
            m.next_new = max(m.next_new, i + 1)
        m.tx_seq += 1
        m.inflight[i] = m.tx_seq
        m.last_seq[i] = m.tx_seq
        m.first_seq.setdefault(i, m.tx_seq)
        m.sent_t[i] = t_end
        m.tx_frames += 1
        m.via = relay
        self.stats["data_tx"] += 1
        m.deadline = t_end + self._td_timeout(m)
        if m.first_tx is None:
            m.first_tx = self.radio.now()
            m.state = MsgState.SENDING
            self._emit(m, "sent")

    def _td_timeout(self, m: OutMsg) -> float:
        """How long to wait for a member's ACK record after a frame's end: its next header comes
        within a cycle, built one transmit lead before it goes out, and our receiver needs a moment."""
        cyc = (self.tdma.cycle_s or 0.1) if self.tdma else 0.1
        lead = self.radio.tx_lead() / self.radio.fs if hasattr(self.radio, "tx_lead") else 0.02
        floor = max(self.ack_timeout, 3 * cyc + 2 * lead + 0.1)
        rtts = [self.rtt[a] for a in m.members if a in self.rtt]
        if rtts:
            return max(floor, 2.5 * max(rtts) + cyc)
        return max(floor, 1.0)

    def group_mcs(self, bo: int) -> str:
        """A group message reaches every station with a slot: the rate the weakest one can take."""
        mem = [a for a in (self.tdma.members_now() if self.tdma else []) if a != self.addr]
        if not mem:
            return self.control_mcs()
        return self.ladder[min(self._level(a, bo) for a in mem)]

    def failing(self, dst: int) -> bool:
        """Unicast messages to dst keep timing out (two in a row without progress)."""
        return any(m.retries_by.get(dst, 0) >= 2 for m in self.out.get(dst, []) if m.state == MsgState.SENDING)

    def ack_record(self, src: int, msg_id: int, info: dict) -> bytes | None:
        """The ACK record for message (src, msg_id) as we hold it now, for our slot header."""
        from .tdma import REC, REC_MAX_BITMAP
        key = (src, msg_id)
        if key in self.done_in:
            count = base = self.done_in[key]
            bm = b""
        else:
            im = self.inbox.get(key)
            if im is None:
                return None
            count, base = im.count, im.base
            hi = min(count, base + 1 + 8 * REC_MAX_BITMAP)
            top = max((i for i in im.frags if i < hi), default=base)
            nbits = max(0, top - base)
            arr = bytearray((nbits + 7) // 8)
            for i in im.frags:
                j = i - base - 1
                if 0 <= j < nbits:
                    arr[j >> 3] |= 0x80 >> (j & 7)
            bm = bytes(arr)
        snr = None if info.get("via") else self._last_rx_snr.get(src)
        fb = -128 if snr is None else int(max(-127, min(127, round(2 * snr))))
        trig = info.get("trigger", NO_TRIGGER)
        self.stats["ack_records_tx"] += 1
        return REC.pack(src, self.addr, msg_id, count, base, trig if trig is not None else NO_TRIGGER,
                        fb, len(bm)) + bm

    def on_record(self, acker: int, msg_id: int, count: int, base: int, trigger: int, snr: int,
                  bitmap: bytes, direct: bool = True):
        """An ACK record about one of our messages (from acker's header, or passed on by a relay)."""
        with self.lock:
            self.stats["ack_records_rx"] += 1
            if snr != -128 and direct:
                self._note_report(acker, snr / 2.0, 0.4)
            for lst in (self.out.get(acker, []), self.out.get(BROADCAST_ADDR, [])):
                for m in lst:
                    if (m.msg_id == msg_id and len(m.frags) == count and acker in m.mstate
                            and m.state in (MsgState.QUEUED, MsgState.SENDING)):
                        self._apply_record(m, acker, base, trigger, bitmap)
                        return

    def _apply_record(self, m: OutMsg, a: int, base: int, trigger: int, bitmap: bytes):
        count = len(m.frags)
        have = m.acked_by.setdefault(a, set())
        before = len(have)
        new = set()
        if base > m.base_by.get(a, 0):
            new.update(i for i in range(m.base_by.get(a, 0), min(base, count)) if i not in have)
            m.base_by[a] = base
        for j in range(min(len(bitmap) * 8, count - base - 1)):
            if bitmap[j >> 3] & (0x80 >> (j & 7)) and base + 1 + j not in have:
                new.add(base + 1 + j)
        have |= new
        active = [x for x in m.members if m.mstate.get(x) in ("sending", "delivered")]
        for i in new:
            if all(i in m.acked_by.get(x, ()) for x in active):
                m.acked.add(i)
                m.inflight.pop(i, None)
        m.cum_base = min((m.base_by.get(x, 0) for x in active), default=base)
        # anything we sent before the frame it heard last, and it still misses, is lost (as far as
        # the record can tell: it reports REC_SPAN fragments beyond its base)
        # (through a relay, the frame it heard last may be a copy that waited in the relay's queue
        # while we repeated it - the relay passes a fragment on once: count from its first
        # transmission, or everything we repeated meanwhile would look lost)
        seqs = m.first_seq if m.via is not None else m.last_seq
        tseq = seqs.get(trigger) if trigger != NO_TRIGGER else None
        lost = 0
        if tseq is not None:
            span = base + 1 + REC_SPAN
            for i, seq in sorted(m.inflight.items(), key=lambda kv: kv[1]):
                if seq < tseq and i < span and i not in have and i not in m.retx_set:
                    m.retx.append(i)
                    m.retx_set.add(i)
                    del m.inflight[i]
                    lost += 1
            if lost:
                self.stats["lost_detected"] += lost
            # adaptive modulation: frames that keep getting lost step the link down for a while, as
            # lossy windows do with ALOHA (a window here: the records' next 8 frames or more)
            acc = self._td_loss.setdefault(a, [0, 0])
            acc[0] += lost
            acc[1] += lost + len(new)
            if acc[1] >= 8:
                self._note_loss(m.via or a, acc[0], acc[1])
                self._td_loss[a] = [0, 0]
            t = m.sent_t.get(trigger)
            if t is not None:
                sample = self.radio.now() - t
                if 0 < sample < 10.0:
                    r = self.rtt.get(a)
                    self.rtt[a] = sample if r is None else 0.8 * r + 0.2 * sample
        if len(have) > before:
            m.retries_by[a] = 0
            m.deadline = max(m.deadline, self.radio.now() + self._td_timeout(m))
        if len(have) >= count and m.mstate.get(a) == "sending":
            m.mstate[a] = "delivered"
            if m.dst == BROADCAST_ADDR:
                m.ev_member = a
                self._emit(m, "member_delivered")
        self._td_check_done(m)
        if m.state == MsgState.SENDING:
            self._emit(m, "progress")

    def peer_restarted(self, a: int):
        """Station a's app was restarted (TD-CDMA: a new nonce in its slot headers).  Its new messages may
        reuse ids we remember as finished: forget those.  And it has lost what it had received of ours:
        send that again from the start (otherwise it would never complete those messages while we
        showed them as delivered)."""
        with self.lock:
            for d in (self.done_in, self.dropped_in, self.inbox):
                for key in [k for k in d if k[0] == a]:
                    del d[key]
            if self.tdma is not None:
                for key in [k for k in self.tdma.ack_due if k[0] == a]:
                    del self.tdma.ack_due[key]
            self._td_loss.pop(a, None)
            for lst in (self.out.get(a, []), self.out.get(BROADCAST_ADDR, [])):
                for m in lst:
                    if (m.state in (MsgState.QUEUED, MsgState.SENDING) and m.mstate.get(a) == "sending"
                            and (m.acked_by.get(a) or m.base_by.get(a))):
                        self._td_member_reset(m, a)

    def _td_member_reset(self, m: OutMsg, a: int):
        """Member a no longer has any of m: what it had acknowledged goes again."""
        m.acked_by[a] = set()
        m.base_by[a] = 0
        m.retries_by[a] = 0
        active = [x for x in m.members if m.mstate.get(x) in ("sending", "delivered")]
        m.acked = set(i for i in range(len(m.frags)) if all(i in m.acked_by.get(x, ()) for x in active))
        m.cum_base = min((m.base_by.get(x, 0) for x in active), default=0)
        for i in range(m.next_new):
            if i not in m.acked and i not in m.inflight and i not in m.retx_set:
                m.retx.append(i)
                m.retx_set.add(i)
        m.deadline = self.radio.now() + self._td_timeout(m)
        self.stats["resent_after_restart"] += 1

    def _td_check_done(self, m: OutMsg):
        if m.state not in (MsgState.QUEUED, MsgState.SENDING):
            return
        states = [m.mstate.get(x) for x in m.mstate]
        if any(s == "sending" for s in states):
            return
        if any(s == "delivered" for s in states):
            m.acked = set(range(len(m.frags)))
            m.state = MsgState.DELIVERED
            m.done_time = self.radio.now()
            self.stats["msgs_delivered"] += 1
            self._emit(m, "delivered")
        else:
            m.state = MsgState.FAILED
            self.stats["msgs_failed"] += 1
            self._emit(m, "failed")

    def _td_drop_member(self, m: OutMsg, a: int, why: str):
        """A member that will not get this message (left the network, or never answers)."""
        if m.mstate.get(a) != "sending":
            return
        m.mstate[a] = why
        m.members.discard(a)
        active = [x for x in m.members if m.mstate.get(x) in ("sending", "delivered")]
        m.acked = set(i for i in range(len(m.frags)) if all(i in m.acked_by.get(x, ()) for x in active)) \
            if active else m.acked
        for i in list(m.inflight):
            if i in m.acked:
                del m.inflight[i]
        if m.dst == BROADCAST_ADDR:
            m.ev_member = a
            self._emit(m, "member_" + why)
        self._td_check_done(m)

    def _td_timers(self, now: float):
        """Messages whose acknowledgements stopped: resend what is in flight; give up on a member
        after max_retries timeouts, or soon if it has left the network."""
        tdma = self.tdma
        here = set(tdma.members_now()) if tdma else set()
        for lst in list(self.out.values()):
            for m in lst:
                if m.state != MsgState.SENDING or not m.inflight or now < m.deadline:
                    continue
                self.stats["timeouts"] += 1
                lag = [a for a in m.members if m.mstate.get(a) == "sending"
                       and any(i not in m.acked_by.get(a, ()) for i in m.inflight)]
                # (a group member that never answers is soon given up here: the app then sends it
                # the message on its own, through a relay if there is one)
                limit = GROUP_RETRIES if m.dst == BROADCAST_ADDR else self.max_retries
                for a in lag:
                    m.retries_by[a] = m.retries_by.get(a, 0) + 1
                    gone = a not in here and now - self.last_heard_r.get(a, -1e9) > 5.0
                    if gone and m.retries_by[a] >= 2:
                        self._td_drop_member(m, a, "left")
                    elif m.retries_by[a] > limit:
                        self._td_drop_member(m, a, "failed")
                if m.state != MsgState.SENDING:
                    continue
                # (through a relay, a timeout mostly means the frames are still queued there, waiting
                # for its slot to grow: no sign of a bad first hop, so the rate stays; the losses the
                # destination's records show still count)
                for a in lag:
                    if m.mstate.get(a) == "sending" and m.via is None:
                        self._note_loss(a, len(m.inflight), max(len(m.inflight), 4))
                for i in sorted(m.inflight, key=m.inflight.get):
                    if i not in m.acked and i not in m.retx_set:
                        m.retx.append(i)
                        m.retx_set.add(i)
                m.inflight.clear()
                m.deadline = now + self._td_timeout(m)
                self._emit(m, "retry")

    # ================================================================ receiving
    def _on_phy_frame(self, f):
        """Every frame the PHY decoded.  Never raises: a malformed frame, or a bug in the code that
        handles one, must not stop reception (in the flowgraph an exception here would end the PHY -> MAC
        block's thread, and the station would hear nothing until restarted)."""
        try:
            self._rx_frame(f)
        except Exception as e:
            self.stats["rx_frame_errors"] += 1
            if self.stats["rx_frame_errors"] <= 5:
                self.log("MAC: error handling a received frame", repr(e))

    def _rx_frame(self, f):
        body = self._open(f.payload)
        if body is None:
            self.stats["crc_fail"] += 1
            return
        ftype, flags, dst, src, msg_id = HDR.unpack_from(body)
        rest = body[HDR.size:]
        tx = src                                         # the station that transmitted this frame
        if ftype == T_SLOT and self.tdma is not None:
            if src != self.addr:
                self._note_link(src, flags, f)
            with self.lock:
                self.tdma.on_header(src, msg_id, rest, f)  # (our own header too: TX->RX calibration)
            return
        if src == self.addr:
            return                                       # our own transmission
        if ftype == T_FWD and self.tdma is not None and len(rest) >= FWD.size:
            relay, idx, count = FWD.unpack_from(rest)
            if dst == self.addr:                         # passed on to us by a relay
                self._note_link(relay, flags, f)
                self.stats["frames_ok"] += 1
                self.stats["relayed_rx"] += 1
                self._on_data(src, dst, msg_id, flags, idx, count, rest[FWD.size:], f, via=relay)
            elif relay == self.addr:                     # we are the relay
                self._note_link(src, flags, f)
                with self.lock:
                    self.tdma.on_forward(body, dst, src, msg_id)
            else:
                self.stats["not_for_me"] += 1
            return
        if dst != self.addr and dst != BROADCAST_ADDR:
            self.stats["not_for_me"] += 1                # addressing filter
            return
        self.stats["frames_ok"] += 1
        self._note_link(tx, flags, f)
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
            if len(rest) < 7:
                self.stats["bad_frames"] += 1
                return
            count, base, trigger, fb = struct.unpack_from(">HHHb", rest)
            if fb != -128:
                self._note_report(src, fb / 2.0, 0.4)
            self._on_ack(src, msg_id, count, base, trigger, rest[7:])
        elif ftype == T_DATA:
            if len(rest) < 4:
                self.stats["bad_frames"] += 1
                return
            idx, count = struct.unpack_from(">HH", rest)
            self._on_data(src, dst, msg_id, flags, idx, count, rest[4:], f)
        elif ftype == T_DGRAM:
            self.stats["dgram_rx"] += 1
            try:
                self.on_datagram(src, dst, rest, f)
            except Exception as e:
                self.log("on_datagram error", repr(e))

    def _note_link(self, tx: int, flags: int, f):
        """Presence and link quality of the station that transmitted a frame we decoded."""
        self.last_heard[tx] = time.time()                # presence: any frame counts
        self.last_heard_r[tx] = self.radio.now()
        self.radio.last_frame = f                        # dashboard: last frame from a peer
        q = self.link_quality.setdefault(tx, {})
        es = getattr(f, "esn0_db", f.snr_db)
        bo = flags >> BO_SHIFT                           # the power the sender used for this frame
        full = es + bo                                   # this path at full power
        q["esn0_avg"] = es if "esn0_avg" not in q else 0.8 * q["esn0_avg"] + 0.2 * es
        # (each frame's estimate is good to ~0.5 dB; beacons may be the only frames, every 5 s)
        q["esn0_full"] = round(full if q.get("esn0_full") is None else 0.5 * q["esn0_full"] + 0.5 * full, 2)
        q.update(snr_db=round(f.snr_db, 1), esn0_db=round(es, 1), cfo_hz=round(f.cfo_hz), rx_pwr_db=-bo,
                 mod=getattr(f, "mcs", None) or getattr(f, "mod", "bpsk"), t=time.time())

    def _on_data(self, src, dst, msg_id, flags, idx, count, payload, f, via=None):
        if count == 0 or idx >= count:
            self.stats["bad_frames"] += 1
            return
        key = (src, msg_id)
        ack_req = bool(flags & F_ACKREQ)
        td = self.tdma
        with self.lock:
            if key in self.dropped_in:                   # a transfer that was cancelled
                self.stats["dropped_frames"] += 1
                return
            now = self.radio.now()
            if key in self.done_in:                      # duplicate of a finished message
                self.stats["dup_frames"] += 1
                if td is not None:
                    td.note_rx(src, msg_id, idx, via)    # say again that we have it all
                elif dst != BROADCAST_ADDR and ack_req:
                    self._send_ack(src, msg_id, count, None, idx)
                return
            im = self.inbox.get(key)
            if im is None or im.count != count:
                im = self.inbox[key] = InMsg(src, msg_id, count, prio=flags & 3)
            if idx in im.frags:
                self.stats["dup_frames"] += 1
            if via is None:
                self._last_rx_snr[src] = getattr(f, "esn0_db", f.snr_db) + (flags >> BO_SHIFT)   # at full power
            im.frags[idx] = payload
            while im.base in im.frags:
                im.base += 1
            im.last_rx = now
            im.need_ack = True
            if td is not None:
                td.note_rx(src, msg_id, idx, via)
            if len(im.frags) == count:
                data = b"".join(im.frags[i] for i in range(count))
                del self.inbox[key]
                self.done_in[key] = count
                while len(self.done_in) > 2048:
                    self.done_in.popitem(last=False)
                if dst != BROADCAST_ADDR and td is None:
                    self._send_ack(src, msg_id, count, None, idx)
                self.stats["msgs_rx"] += 1
                meta = dict(src=src, msg_id=msg_id, snr_db=f.snr_db, prio=flags & 3,
                            broadcast=dst == BROADCAST_ADDR, via=via)
                try:
                    self.on_message(src, data, meta)
                except Exception as e:
                    self.log("on_message error", repr(e))
            elif ack_req and dst != BROADCAST_ADDR and td is None:
                self._send_ack(src, msg_id, count, im, idx)
                im.need_ack = False

    def _receiver_timers(self, now: float):
        # if the ACK-requesting frame was lost, ACK what we have after a quiet gap
        for im in self.inbox.values():
            if im.need_ack and now - im.last_rx > 0.6 * self.ack_timeout:
                self._send_ack(im.src, im.msg_id, im.count, im, NO_TRIGGER)
                im.need_ack = False
        self._purge_inbox(now, INBOX_MAX_AGE_S)

    def _purge_inbox(self, now: float, max_age: float):
        for key in [k for k, im in self.inbox.items() if now - im.last_rx > max_age]:
            del self.inbox[key]
            self.stats["incoming_expired"] += 1

    def stale_incoming(self, max_age: float) -> list:
        """(src, msg_id) of partly received messages with no new fragment for max_age seconds."""
        now = self.radio.now()
        with self.lock:
            return [k for k, im in self.inbox.items() if now - im.last_rx > max_age]

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
