"""TD-CDMA: time slots for the DS-CDMA stations (--mac tdcdma, the default).

Why.  With every station on the air whenever it likes (spread ALOHA, --mac aloha), a station
next to a receiver drowns a far one (the near-far problem: after despreading a 31-chip Gold
code still leaks ~1/31 of its power into the other codes), and a station's own transmitter
deafens its receiver, so ACKs need pauses.  TD-CDMA gives every station its own time slot in a
repeating cycle.  In its slot a station transmits, at one power, to all the others at once: one
burst per destination, each spread with that destination's Gold code (CDMA inside the slot),
plus its slot header on the broadcast code.  Everybody else listens.  So at any moment only one
transmitter is on the air: no near-far, nobody deaf, and every ACK rides in a slot header.

Cycle (all positions in ticks of 0.25 ms)::

    | lead's slot | slot of 2 | slot of 3 | ... | join slot |    then the next cycle
      ^ each slot starts with its owner's slot header (broadcast code, QPSK 1/2), data on the
        destinations' codes alongside; 0.5 ms guard between slots

The lead station (the first on the air; when two networks meet the lower address leads) owns
the first slot.  Its header carries the cycle number and the slot plan; a plan change (a station
joins, leaves or wants more airtime) is announced >= 0.5 s ahead, so every station knows it
before it has to transmit by it.  A newcomer listens (1.2 s + 0.25 s per address, so stations
started together do not all lead), then sends its header in the join slot; the lead gives it a
slot.  A slot is ~15 ms when its owner has nothing to send (header + small messages on the
other codes alongside) and up to 100 ms while it has data queued.  A station whose headers stop
for 6 cycles (and 1.5 s) loses its slot.  If the lead goes silent for 2 cycles, the station
with the next lowest address takes over (cycle numbering and timing carry on) - unless it hears
nobody at all (then it is the one cut off) or another station still hears the lead (then the
others repeat the lead's plan for it); one further down only while the ones before it are silent.  A station sends in cycle n only if it heard the lead's
plan in cycle n - 4 or later: every plan change is announced at least 4 cycles ahead, so it can
never follow a stale plan into somebody else's slot.

Timing works in samples, not in the PC's clock.  Followers time everything from where they
receive the lead's header (receive sample index from the preamble correlator, ~0.25 sample
accurate).  To transmit at receive-side time j a station uses transmit sample j - D, where D is
its own TX -> RX offset: every slot header carries the transmit sample it was put at, and the
station hears its own header (the TX leaks into the RX), so D = receive index - transmit sample
is measured in every slot (it jumps after a TX underrun or a lost RX buffer).  Each header also
reports how far off the other stations' headers arrived (timing feedback, shown on the
dashboard, and the fallback if a station cannot hear itself).

Slot header (frame type 5 on the broadcast code)::

    MAC header (type 5, flags, dst 0xFF, src, cycle number & 0xFFFF)
    tx_sample u32 | nonce u16 | status u8 | want (ms) u8 | lead u8 | last lead cycle heard u16
    sections (tag, length, data):
      PLAN  cycle u32, the plan in force: version, cycle length, slots (owner, start, length)
      NEXT  the next plan and the cycle it takes effect (lead only)
      ACK   records: sender, acker, msg_id, count, base, trigger, Es/N0, SACK bitmap
      LINK  how this station hears each other one (power control, relay routes)
      TFB   timing error of each other station's header as heard here (samples)
      DUP   two stations with the same address were heard (their nonces)

Reliable messages: a receiver acknowledges every message it got frames of in its next slot
header (records repeated in two headers), so a sender needs no ACK requests and no pauses.  A
group message is one broadcast; every member acknowledges it in its own header; pieces a
member misses are sent again; a member that never answers is dropped from that message (the
app then tries it as a unicast message, via a relay if there is one).
"""
from __future__ import annotations

import collections
import math
import random
import struct
from dataclasses import dataclass

from .config import BROADCAST_ADDR, BROADCAST_CODE, code_index_for

TICK_S = 0.00025                 # unit of the slot plan
GUARD_S = 0.0005                 # gap between slots
IDLE_MS = 15                     # a slot whose owner has nothing (much) to send
DATA_MS = 100                    # ... and while it has data queued (a whole number of frames)
MAX_SLOT_MS = 250                # (one QPSK 1/2 frame of 1 KB takes 126 ms)
MAX_CYCLE_MS = 700
MIN_CYCLE_MS = 100               # a shorter cycle gets the spare time added to its slots (10 slot
                                 # headers per station and second keep the receivers' CPU load down)
JOIN_SPREAD_MS = 4.0             # join requests start somewhere in the first 4 ms of the join slot
PLAN_AHEAD_S = 1.0               # a plan change takes effect this long after it is first announced
PLAN_AHEAD_CYCLES = 4            # ... and at least this many cycles later.  (1 s: on the radios a burst
                                 # is built 0.2-0.6 s before it goes out - TX buffers and lead time - and
                                 # a plan repeated by a relaying member is that much older again)
LEAD_MISS = 2                    # lead headers missed before its successor takes over
EVICT_CYCLES = 6                 # a member silent this many cycles ...
EVICT_MIN_S = 1.5                # ... and this long loses its slot
LISTEN_S = 1.2                   # start-up: listen this long (+ 0.25 s per address) before leading
LISTEN_STEP_S = 0.25
LOST_S = 3.0                     # a follower that hears no lead this long starts over
GATHER_S = 0.3                   # a slot that should grow waits this long for other wishes (a member's header
                                 # takes ~0.25 s to reach the lead over GNU Radio), so that transfers that
                                 # start together get their airtime in one plan change, not 1 s apart ...
GATHER_MAX_S = 1.0               # ... and, up to this long, until every member has had a slot under the
                                 # plan in force to say what it wants (a newcomer has had none yet)
SHRINK_S = 1.0                   # a slot shrinks after its owner wanted less for this long ...
SHRINK_MIN_MS = 5.0              # ... and clearly less; a slot that grows gets a little headroom:
GROW_PAD_MS = 2.0                # so a header a few bytes longer or shorter doesn't change the plan
ACK_REPEAT = 2                   # each ACK record goes into this many headers
MAX_ACK_BYTES = 520              # ACK records per header (the rest waits for the next slot)
REC_MAX_BITMAP = 64              # SACK bytes per record (512 fragments beyond the base)
RELAY_MIN_DB = 7.0               # a link this good (Es/N0 at full power) carries a relay hop
RELAY_DIRECT_DB = 5.0            # direct below this (or unheard): look for a relay
RELAY_FRESH_S = 6.0
LINK_FRESH_S = 3.0               # the LINK table lists stations heard this recently (s of radio time)
FULL_SEARCH_EVERY = 8            # slot-aware search: every 8th cycle the receiver searches everywhere
RECAL_S = 1.5                    # after a possible timing slip: search everywhere, hear ourselves again
PROBE_BACKOFF_DB = 20            # a newcomer's first transmission: a probe on its own code, this much down
PROBE_TRIES = 8                  # ... at most this many times, then it joins on a guess
DOUBT_PROBES = 4                 # a member in doubt about D (after a slip) probes at most this many slots

LISTEN, JOINING, MEMBER, LEAD = "listening", "joining", "member", "lead"


def plan_window(latest) -> int:
    """Cycles between the first announcement of a plan change and the cycle it takes effect in (the
    lead counts them with its newest plan's cycle length; followers know that plan, so they count alike)."""
    return max(PLAN_AHEAD_CYCLES, int(math.ceil(PLAN_AHEAD_S / max(1e-3, latest.T * TICK_S))))

# status bits of a slot header
ST_LEAD, ST_MEMBER, ST_RELAY, ST_JOIN, ST_YIELD, ST_DOK, ST_RSYNC = 1, 2, 4, 8, 16, 32, 64
ST_NOLEAD = 128                          # a member that has stopped hearing the lead: repeat its plan
SEC_PLAN, SEC_NEXT, SEC_ACK, SEC_LINK, SEC_TFB, SEC_DUP, SEC_JREQ = 1, 2, 3, 4, 5, 6, 7

FIX = struct.Struct(">IHBBBH")           # tx_sample, nonce, status, want_ms, lead, lead_seen
REC = struct.Struct(">BBHHHHbB")         # sender, acker, msg_id, count, base, trigger, snr, nbm
PLAN_HEAD = struct.Struct(">BIHB")       # version, cycle, cycle length (ticks), number of slots
SLOT = struct.Struct(">BHH")             # owner (0 = join slot), start, length (ticks)
TFB = struct.Struct(">Bh")               # station, timing error (samples)
DUP = struct.Struct(">BHH")              # station, nonce a, nonce b
JREQ = struct.Struct(">BHB")             # station, nonce, want (ms): a join request passed on
NO_TRIGGER = 0xFFFF


# ============================================================================ plan and timeline
@dataclass(frozen=True)
class Plan:
    ver: int
    T: int                               # cycle length (ticks)
    slots: tuple                         # ((owner, start, length), ...) in ticks; owner 0 = join

    def slot(self, owner: int):
        for o, s, l in self.slots:
            if o == owner:
                return s, l
        return None

    def owners(self) -> list[int]:
        return [o for o, _, _ in self.slots if o]

    def encode(self, cycle: int) -> bytes:
        return PLAN_HEAD.pack(self.ver & 0xFF, cycle & 0xFFFFFFFF, self.T, len(self.slots)) + b"".join(
            SLOT.pack(o, s, l) for o, s, l in self.slots)

    @staticmethod
    def decode(b: bytes):
        ver, cycle, T, n = PLAN_HEAD.unpack_from(b)
        slots = tuple(SLOT.unpack_from(b, PLAN_HEAD.size + SLOT.size * i) for i in range(n))
        return cycle, Plan(ver, T, slots)

    @staticmethod
    def build(ver: int, lens: list, join_len: int) -> "Plan":
        """lens: [(owner, length in ticks)] in slot order (the lead first)."""
        slots, t = [], 0
        for o, l in lens:
            slots.append((o, t, int(l)))
            t += int(l)
        slots.append((0, t, int(join_len)))
        return Plan(ver & 0xFF, t + int(join_len), tuple(slots))

    def describe(self) -> list[dict]:
        return [dict(owner=o, start_ms=s * TICK_S * 1e3, len_ms=l * TICK_S * 1e3) for o, s, l in self.slots]


class Timeline:
    """Where cycle n starts, in one of this station's sample counters (receive samples for a follower,
    transmit samples for the lead), from an anchor (a cycle whose start is known) and the plans in
    force from given cycles on."""

    def __init__(self, spt: float):
        self.spt = spt                   # samples per tick (nominal)
        self.eps = 0.0                   # this clock vs the lead's (follower: measured)
        self.n: int | None = None
        self.s: int | None = None
        self.sched: list[tuple[int, Plan]] = []

    def valid(self) -> bool:
        return self.n is not None and bool(self.sched)

    def copy(self) -> "Timeline":
        t = Timeline(self.spt)
        t.eps, t.n, t.s, t.sched = self.eps, self.n, self.s, list(self.sched)
        return t

    def plan(self, n: int) -> Plan:
        p = self.sched[0][1]
        for eff, pl in self.sched:
            if eff <= n:
                p = pl
            else:
                break
        return p

    def latest(self) -> tuple[int, Plan]:
        return self.sched[-1]

    def ticks_between(self, n1: int, n2: int) -> int:
        """Ticks from the start of cycle n1 to the start of cycle n2 (negative if n2 < n1)."""
        if n2 < n1:
            return -self.ticks_between(n2, n1)
        total, m = 0, n1
        effs = [e for e, _ in self.sched]
        while m < n2:
            pl = self.plan(m)
            nxt = min([e for e in effs if e > m] + [n2])
            total += (nxt - m) * pl.T
            m = nxt
        return total

    def to_samples(self, ticks: float) -> int:
        return int(round(ticks * self.spt * (1.0 + self.eps)))

    def start(self, n: int) -> int:
        return self.s + self.to_samples(self.ticks_between(self.n, n))

    def cycle_at(self, x: int) -> int:
        """The cycle that contains sample x."""
        n = self.n
        while self.start(n) > x:
            n -= 1
        while self.start(n + 1) <= x:
            n += 1
        return n

    def add_plan(self, eff: int, plan: Plan):
        """plan is in force from cycle eff on (it replaces anything announced for eff or later)."""
        self.sched = [(e, p) for e, p in self.sched if e < eff] + [(eff, plan)]

    def prune(self, n: int):
        """Forget plans that ended before cycle n."""
        while len(self.sched) > 1 and self.sched[1][0] <= n:
            self.sched.pop(0)

    def set_anchor(self, n: int, s: int, learn: bool = False):
        if learn and self.n is not None and 1 <= n - self.n <= 64:
            nominal = self.ticks_between(self.n, n) * self.spt
            if nominal > 0:
                e = (s - self.s) / nominal - 1.0
                if abs(e) < 300e-6:
                    self.eps = 0.8 * self.eps + 0.2 * e
        self.n, self.s = int(n), int(s)


def _sec(tag: int, data: bytes) -> bytes:
    out = b""
    while True:                          # (a long section is split into several)
        out += bytes([tag, min(255, len(data))]) + data[:255]
        data = data[255:]
        if not data:
            return out


def _sections(b: bytes):
    i = 0
    while i + 2 <= len(b):
        tag, n = b[i], b[i + 1]
        yield tag, b[i + 2:i + 2 + n]
        i += 2 + n


# ============================================================================ the scheduler
class Tdma:
    """TD-CDMA medium access for one station (owned by LinkLayer, runs under its lock)."""

    def __init__(self, mac, relay: bool = False):
        self.mac = mac
        self.radio = mac.radio
        self.addr = mac.addr
        self.fs = float(self.radio.fs)
        self.spt = self.fs * TICK_S
        self.guard = int(round(GUARD_S * self.fs))
        self.gh = self.guard // 2
        self.nonce = random.randrange(1, 1 << 16)
        self.role = LISTEN
        self.t_start = None
        self.listen_s = LISTEN_S + LISTEN_STEP_S * ((self.addr - 1) % 8)
        self.D: int | None = None            # TX -> RX offset (samples)
        self._d_meas: collections.deque = collections.deque(maxlen=5)
        self._d_odd: list = []
        self.d_jumps = 0
        self.tl = Timeline(self.spt)         # follower: receive samples / lead: transmit samples
        self.lead_id: int | None = None
        self.last_lead_n: int | None = None
        self.last_lead_t = 0.0
        self.plan_known_n: int | None = None  # we know every plan in force up to this cycle + plan_window()
        self._rejoin = False                 # dropped from the plan: ask again in every join slot
        self.next_n: int | None = None
        self.relay_on = bool(relay)
        # lead
        self.members: dict[int, dict] = {}   # addr -> nonce, want, heard (radio time), since
        # everyone
        self.heard: dict[int, dict] = {}     # addr -> last header: n, j, t, nonce, status, want, lead
        self.links: dict[int, dict] = {}     # addr -> {peer: Es/N0 full power} from its LINK section
        self.links_t: dict[int, float] = {}
        self.tfb_mine: dict[int, int] = {}   # timing error of each station's header as heard here
        self.tfb_about_me: dict[int, tuple] = {}   # what others report about my header (err, time)
        self.dups: dict[int, tuple] = {}     # addr -> (nonce_a, nonce_b, time)
        self.other_leads: dict[int, tuple] = {}   # lead of another network heard -> (first, last time)
        self.dup_warn: str | None = None
        self.ack_due: collections.OrderedDict = collections.OrderedDict()   # (src, id) -> info
        self.relay_recs: collections.OrderedDict = collections.OrderedDict()  # ACK records to pass on
        self.relayed: dict[tuple, float] = {}  # (origin, msg_id) -> time we last forwarded a piece
        self.fwd_q: dict[int, collections.deque] = collections.defaultdict(collections.deque)
        self.routes: dict[int, dict] = {}    # dst -> {relay, since, why}
        self.dgrams: dict[int, collections.deque] = collections.defaultdict(collections.deque)
        self.sources: list = []              # fn() -> [(dst, payload, commit)] called when a slot is built
        self.beacon: bytes | None = None
        self.want_ms = IDLE_MS
        self._want_hi_t = 0.0
        self._grow_t = None                  # (lead) since when a slot has wanted to grow
        self._nonce_prev: dict[int, tuple] = {}   # station -> (its previous nonce, when it changed)
        self._probes = 0                     # calibration probes sent (while D is unknown)
        self._tx_doubt = False               # a TX underrun may have moved D: probe until measured again
        self._doubt_probes = 0
        self.stats = collections.Counter()
        self.slot_log: collections.deque = collections.deque(maxlen=40)    # dashboard: recent slots
        self.cycle_s = None
        self._listen_heard = False
        self.rsync = False                   # our timing comes from plans a relaying member repeats
        self.rsync_ref: int | None = None
        self.rsync_t = 0.0
        self.last_lead_j: int | None = None  # receive index of the last lead (or reference) header
        self.q = max(1, int(getattr(self.radio, "tx_quantum", 1)))
        # our own header is recognised by where it arrives (no need to decode it to calibrate)
        self._own_hdr: collections.deque = collections.deque(maxlen=8)   # (expected rx index, tx sample)
        self._self_meas: collections.deque = collections.deque(maxlen=32)
        setf = getattr(self.radio, "set_detect_filter", None)
        if setf is not None:
            setf(self._detect_filter)
        self._faults = self._fault_count()
        self.recal_until = 0.0               # until then: no search windows, own slot searched too
        self._own_miss = 0                   # own headers not heard back in a row
        self._fb_next = 0.0
        self.d_source = None                 # "self" (own headers heard back) or "peers" (their reports)
        self._win_key = None
        self.windowed = False

    # ------------------------------------------------------------------ helpers
    def now(self) -> float:
        return self.radio.now()

    def _blen(self, n_body: int, mcs: str) -> int:
        """Samples a frame with an n_body-byte MAC body occupies."""
        return self.radio.burst_len(n_body + (0 if getattr(self.radio, "provides_crc", False) else 4), mcs)

    def _d_guess(self) -> int:
        g = getattr(self.radio, "tx_rx_guess", None)
        if g is not None:
            return int(g())
        return int(self.radio.rx_count() - self.radio.tx_count())

    def offset(self) -> int:
        return self.D if self.D is not None else self._d_guess()

    def build_margin(self) -> int:
        """How long before the transmit horizon reaches a slot it is built: one service interval."""
        n = getattr(getattr(self.radio, "air", None), "N", None)
        return int(n) if n else int(0.015 * self.fs)

    def cycle_ms(self) -> float | None:
        if not self.tl.valid():
            return None
        n = self.next_n if self.next_n is not None else self.tl.n
        return self.tl.plan(n).T * TICK_S * 1e3

    def members_now(self) -> list[int]:
        """Stations with a slot in the plan in force (or about to be)."""
        if not self.tl.valid():
            return []
        n = self.next_n if self.next_n is not None else self.tl.n
        return [a for a in self.tl.plan(n).owners()]

    # ------------------------------------------------------------------ main entry (MAC tick)
    def service(self):
        r = self.radio
        now = self.now()
        if self.t_start is None:
            self.t_start = now
        while self._self_meas:
            j, k = self._self_meas.popleft()
            self._own_miss = 0
            self._calibrate_k(j, k)
        f = self._fault_count()
        if f != self._faults:                      # TX underrun / lost RX buffer: D may have moved
            self._faults = f
            self._recalibrate(now, "radio stream gap")
            if self.role == MEMBER and self.D is not None:
                # our bursts may now reach the air late, into the next slot: until we have heard
                # ourselves again, only quiet probes (the lead's slots are the reference: it goes on)
                self._tx_doubt, self._doubt_probes = True, 0
        self._peer_timing(now)
        if self.role != LISTEN:
            self._update_windows(now)
        horizon = r.tx_count() + r.tx_lead()
        if self.role == LISTEN:
            if now - self.t_start > self.listen_s:
                self._become_lead(horizon)
            return
        if self.role == LEAD:
            self._service_lead(horizon, now)
        else:
            self._service_follower(horizon, now)

    # ------------------------------------------------------------------ lead
    def _own_len(self) -> int:
        return int(math.ceil(max(IDLE_MS, min(MAX_SLOT_MS, self.want_ms)) * 1e-3 / TICK_S))

    def _join_len(self) -> int:
        b = self._blen(6 + FIX.size + 2 + 2 * 8, self.mac.control_mcs())
        ms = b / self.fs * 1e3 + JOIN_SPREAD_MS + GUARD_S * 1e3 + 0.5
        return int(math.ceil(ms * 1e-3 / TICK_S))

    def _become_lead(self, horizon: int):
        self._grow_t = None
        self._tx_doubt = False
        self.role = LEAD
        self.lead_id = self.addr
        self.members = {}
        tl = Timeline(self.spt)
        plan = Plan.build(1, [(self.addr, self._own_len())], self._join_len())
        tl.add_plan(1, plan)
        tl.set_anchor(1, horizon + self.build_margin() + int(0.02 * self.fs))
        self.tl = tl
        self.next_n = 1
        self.last_lead_n = None
        self.plan_known_n = None                   # (the lead makes the plans)
        self._rejoin = False
        self.stats["became_lead"] += 1
        self.mac.log(f"tdma{self.addr}: no network heard - leading a new one")

    def _service_lead(self, horizon: int, now: float):
        tl = self.tl
        margin = self.build_margin()
        if self.next_n is None:
            self.next_n = tl.cycle_at(horizon)
        for _ in range(64):
            n = self.next_n
            self._lead_plan_update(n, now)
            plan = tl.plan(n)
            s = plan.slot(self.addr)
            if s is None:                          # (a takeover: our slot is still elsewhere)
                self.next_n += 1
                continue
            base = tl.start(n)
            k0 = base + tl.to_samples(s[0]) + self.gh
            if k0 < horizon:
                self.stats["slots_missed"] += 1
                self.next_n += 1
                continue
            if k0 > horizon + margin:
                break
            k_end = base + tl.to_samples(s[0] + s[1]) - self.gh
            if self.members or n % 4 == 0 or self._has_work():
                self._build_slot(n, k0, k_end)
            else:
                self.stats["lone_quiet"] += 1      # alone: listen (another network?) 3 cycles in 4
            tl.set_anchor(n, base)
            tl.prune(n)
            self.last_lead_n = n
            self.next_n += 1

    def _has_work(self) -> bool:
        mac = self.mac
        return bool(self.beacon is not None or any(self.dgrams.values()) or any(self.fwd_q.values())
                    or any(m.has_work() for lst in mac.out.values() for m in lst))

    def _lead_plan_update(self, n: int, now: float):
        tl = self.tl
        # members whose headers stopped lose their slot
        cyc = tl.plan(n).T * TICK_S
        for a, m in list(self.members.items()):
            if now > m.get("grace", 0.0) and now - m["heard"] > max(EVICT_MIN_S, EVICT_CYCLES * cyc):
                del self.members[a]
                self.stats["members_evicted"] += 1
                self.mac.log(f"tdma{self.addr}: station {a} silent - slot freed")
        eff_last, latest = tl.latest()
        if eff_last > n:
            return                                 # a change is pending: one at a time
        old = {o: l for o, _, l in latest.slots}
        own, self._want_hi_t = self._settle(self._own_len(), old.get(self.addr), self._want_hi_t, now)
        lens = [(self.addr, own)]
        for a in sorted(self.members):
            m = self.members[a]
            want = int(math.ceil(max(IDLE_MS * 0.6, min(MAX_SLOT_MS, m["want"] or IDLE_MS)) * 1e-3 / TICK_S))
            want, m["hi_t"] = self._settle(want, old.get(a), m.get("hi_t", 0.0), now)
            lens.append((a, want))
        # growth waits GATHER_S for the wishes of the others (ours is known at once, a member's arrives
        # cycles later): one change for all of them instead of one plan window (1 s) per station
        same_owners = {o for o, _ in lens} == set(latest.owners())
        if same_owners and any(l > old.get(o, 0) for o, l in lens):
            if self._grow_t is None:
                self._grow_t = now
            waited = now - self._grow_t
            # (a member's header tells the wish from its previous slot: its second slot under this plan
            # is the first one that can ask for more)
            unheard = any((self.heard.get(a) or {}).get("n") is None or self.heard[a]["n"] < eff_last + 1
                          for a in self.members)
            if waited < GATHER_S or (unheard and waited < GATHER_MAX_S):
                return
        else:
            self._grow_t = None
        total = sum(l for _, l in lens) + self._join_len()
        cap = int(MAX_CYCLE_MS * 1e-3 / TICK_S)
        low = int(MIN_CYCLE_MS * 1e-3 / TICK_S)
        if (total < low and abs(latest.T - low) <= len(lens) and {o for o, _ in lens} == set(latest.owners())
                and all(l <= old.get(o, 0) for o, l in lens)):
            return                                 # the cycle we have, padded to the minimum, fits every wish
        if total < low:                            # spare time: room in every slot for small things
            extra = (low - total) // len(lens)
            lens = [(a, l + extra) for a, l in lens]
        if total > cap:                            # too long: share the data airtime
            idle = int(IDLE_MS * 1e-3 / TICK_S)
            extra = sum(max(0, l - idle) for _, l in lens)
            room = max(0, cap - self._join_len() - idle * len(lens))
            lens = [(a, idle + (max(0, l - idle) * room) // max(1, extra)) for a, l in lens]
        new = Plan.build(latest.ver + 1, lens, self._join_len())
        if new.slots == latest.slots:
            return
        # (every change plan_window() cycles ahead: a station that heard a lead header of the last
        # plan_window() cycles knows the plan of the cycle it is about to send in)
        ahead = plan_window(latest)
        tl.add_plan(n + ahead, new)
        self._grow_t = None
        self.stats["plan_changes"] += 1
        # a station new in the plan can't be heard before its first slot
        until = now + ahead * latest.T * TICK_S + new.T * TICK_S + EVICT_MIN_S
        for o in new.owners():
            if o in self.members and o not in latest.owners():
                self.members[o]["grace"] = until

    @staticmethod
    def _settle(want: int, cur: int | None, hi_t: float, now: float) -> tuple:
        """A slot's length (ticks) in the next plan, and when its owner last wanted (about) as much:
        it grows at once, with a little headroom; it shrinks only when the owner has wanted clearly
        less for SHRINK_S; small wobbles of the wish leave it as it is."""
        if cur is None:
            return want, now
        if want > cur:
            return min(int(MAX_SLOT_MS * 1e-3 / TICK_S), want + int(GROW_PAD_MS * 1e-3 / TICK_S)), now
        if cur - want < int(SHRINK_MIN_MS * 1e-3 / TICK_S):
            return cur, now
        return (cur if now - hi_t < SHRINK_S else want), hi_t

    def _lead_on_header(self, src: int, st: int, want: int, nonce: int, lead: int, now: float,
                        relayed: bool = False):
        m = self.members.get(src)
        if relayed and m is not None:
            m["heard"] = max(m["heard"], now - (self.cycle_s or 0.1))
            if now - m.get("direct_t", -1e9) > 1.0:        # (only if we don't hear it ourselves)
                m["want"] = want
            return
        if m is None:
            if lead not in (0, self.addr) and not st & ST_JOIN:
                return                             # follows another lead
            if st & (ST_JOIN | ST_MEMBER):
                self.members[src] = dict(nonce=nonce, want=want, heard=now, since=now, hi_t=now)
                self.stats["members_joined"] += 1
                self.mac.log(f"tdma{self.addr}: station {src} joins")
            return
        if m["nonce"] != nonce:
            self._nonce_changed(src, m["nonce"], nonce, now)
            m["nonce"] = nonce
        m["heard"] = now
        m["direct_t"] = now
        m["want"] = want

    # ------------------------------------------------------------------ followers
    def _rank(self, plan: Plan) -> int:
        others = sorted(a for a in plan.owners() if a != self.lead_id)
        return others.index(self.addr) if self.addr in others else len(others)

    def lead_silent_for(self, now: float) -> float:
        """Cycles the lead's header has been missing: counted in receive samples since the last one
        arrived (so a receiver that runs late can't make the lead look silent), less one cycle and the
        time to receive and decode a header."""
        cyc = self.cycle_s or (self.tl.plan(self.tl.n).T * TICK_S if self.tl.valid() else 0.1)
        if self.last_lead_j is None:
            return max(0.0, (now - self.last_lead_t - 0.03) / max(cyc, 1e-3) - 1.0)
        lag = self.radio.rx_count() - self.last_lead_j - 0.03 * self.fs
        return max(0.0, lag / max(1.0, cyc * self.fs) - 1.0)

    def _service_follower(self, horizon: int, now: float):
        tl = self.tl
        if not tl.valid():
            return
        if now - self.last_lead_t > LOST_S and self.role != LEAD:
            self.mac.log(f"tdma{self.addr}: lost the network - listening again")
            self._reset()
            return
        D = self.offset()
        margin = self.build_margin()
        missed = self.lead_silent_for(now)
        for _ in range(64):
            if self.next_n is None:
                self.next_n = tl.cycle_at(horizon + D)
            n = self.next_n
            plan = tl.plan(n)
            # lead silent?  its successor (lowest address after it) takes over - if there is anybody to
            # lead (hearing nobody at all, it is we who are cut off) and nobody else still hears the
            # lead (then it is only we who don't: its plan comes repeated by the others)
            if (self.role == MEMBER and self.last_lead_n is not None and self.D is not None
                    and missed > LEAD_MISS + 2 * self._rank(plan) and self._others_heard(now)
                    and not self._lead_heard_by_others(now) and not self._first_successor_here(plan, now)):
                self._take_over(n)
                return
            if self.role == MEMBER:
                s = plan.slot(self.addr)
                if s is None:
                    if not any(p.slot(self.addr) for e, p in tl.sched if e > n):
                        self.role = JOINING        # dropped from the plan: ask again (every cycle)
                        self._rejoin = True
                        self.stats["evicted"] += 1
                    self.next_n += 1
                    continue
            else:
                s = plan.slot(0)
                if s is None:
                    self.next_n += 1
                    continue
            base = tl.start(n)
            k0 = base + tl.to_samples(s[0]) + self.gh - D
            if k0 < horizon:
                self.stats["slots_missed"] += 1
                self.next_n += 1
                continue
            if k0 > horizon + margin:
                break
            k_end = base + tl.to_samples(s[0] + s[1]) - self.gh - D
            # A plan change takes effect plan_window() cycles after the lead first announces it (counted
            # with the newest plan's cycle length, which we know too), and the lead decides the next one
            # only once the newest plan is in force.  So having heard the lead's plan at cycle L we know
            # every plan up to max(L, the newest plan's first cycle - 1) + that; beyond it we might follow
            # a stale plan into somebody else's slot: hold.  (Counting from L alone held members for
            # cycles while a slot grew: 4 long cycles were all that was left of the window.)
            eff_l, newest = tl.latest()
            if self.plan_known_n is None or n > max(self.plan_known_n, eff_l - 1) + plan_window(newest):
                self.stats["slots_held" if self.role == MEMBER else "joins_held"] += 1
            elif self.role == MEMBER:
                self._build_slot(n, k0, k_end)
            else:
                self._send_join(n, k0, k_end)
            self.next_n += 1

    def _send_join(self, n: int, k0: int, k_end: int):
        if self.D is None and self._probes < PROBE_TRIES:
            # We don't know yet when our bursts reach the air (the TX -> RX offset D); a guess can be tens
            # of ms off on the radios (their RX buffers can't be seen) and would put the join request over
            # the next slot.  So first a quiet probe: the same header on our own code (nobody else
            # despreads it) 20 dB down; we hear it, measure D, and join with that.  (A station that can't
            # hear itself at all joins with the guess and steers by the others' reports.)
            body = self._header_body(n, k0, join=True)
            self._emit(k0, code_index_for(self.addr), body, self.mac.control_mcs(), PROBE_BACKOFF_DB)
            self._probes += 1
            self.stats["probes"] += 1
            return
        if random.random() < 0.5 and not self._rejoin:
            return                                 # (two newcomers rarely pick the same cycles)
        spread = int(JOIN_SPREAD_MS * 1e-3 * self.fs)
        k = k0 + random.randrange(0, max(1, spread))
        body = self._header_body(n, k, join=True)
        mcs = self.mac.control_mcs()
        if k + self._blen(len(body), mcs) > k_end + self.guard:
            k = k0
        self._emit(k, BROADCAST_CODE, body, mcs, 0)
        self.stats["join_requests"] += 1

    def _take_over(self, n: int):
        old = self.lead_id
        tl = self.tl.copy()
        tl.s -= self.D                             # to transmit samples
        tl.eps = 0.0
        self.tl = tl
        self.role = LEAD
        self.lead_id = self.addr
        self.plan_known_n = None                   # (the lead makes the plans)
        now = self.now()
        plan = tl.plan(n)
        self.members = {a: dict(nonce=self.heard.get(a, {}).get("nonce", 0), want=IDLE_MS, heard=now,
                                since=now, hi_t=now)
                        for a in plan.owners() if a not in (old, self.addr)}
        # the new plan (without the old lead, us first) takes effect a few cycles from now
        self.stats["took_over"] += 1
        self._grow_t = None
        self._tx_doubt = False
        self.mac.log(f"tdma{self.addr}: lead {old} silent - taking over")

    def _reset(self):
        self._tx_doubt = False
        self.last_lead_j = None
        self.role = LISTEN
        self.t_start = self.now()
        self.listen_s = LISTEN_S + LISTEN_STEP_S * ((self.addr - 1) % 8)
        self.tl = Timeline(self.spt)
        self.lead_id = None
        self.last_lead_n = None
        self.plan_known_n = None
        self._rejoin = False
        self.next_n = None
        self.members = {}
        self.rsync = False
        self.rsync_ref = None

    # ------------------------------------------------------------------ slot assembly
    def _emit(self, k: int, code: int, body: bytes, mcs: str, bo: int) -> int | None:
        body = bytearray(body)
        body[1] = (body[1] & 0x07) | (min(31, int(bo)) << 3)
        sealed = self.mac._seal(bytes(body))
        gain = -float(bo) if bo else 0.0
        end = self.radio.send_at(code, sealed, mcs, int(k), gain_db=gain)
        if end is None:
            self.stats["late"] += 1
        return end

    def _slot_bo(self, ncodes: int) -> int:
        """One power for the whole slot, equal on every code: what the station that needs most power
        needs (everyone must decode the header), and with more than two codes 10 log10(codes / 2)
        less, so the sum of the codes never exceeds the level of two full-power codes (RMS 0.35 of
        DAC full scale: 9 dB of headroom for the peaks, the clip counter shows what is left)."""
        if not getattr(self.radio, "supports_tx_gain", True):
            return 0
        pc = self.mac.broadcast_backoff() if self.mac.pc_active else 0
        norm = int(math.ceil(10 * math.log10(ncodes / 2.0) - 0.2)) if ncodes > 2 else 0
        return min(31, pc + norm)

    def _build_slot(self, n: int, k0: int, k_end: int):
        mac = self.mac
        now = self.now()
        k0 = -(-k0 // self.q) * self.q                 # (the PHY's start granularity)
        if self._tx_doubt and self._doubt_probes < DOUBT_PROBES:   # (after a slip: measure D again first)
            body = self._header_body(n, k0)
            self._emit(k0, code_index_for(self.addr), body, mac.control_mcs(), PROBE_BACKOFF_DB)
            self._doubt_probes += 1
            self.stats["probes"] += 1
            self.stats["slots_probed"] += 1
            return
        if self._tx_doubt:                         # (not heard back: a PC too busy to hear itself - better
            self._tx_doubt = False                 #  send by the D we have than lose the slot altogether)
            self.stats["doubt_given_up"] += 1
        if self.D is not None:
            self._own_hdr.append((k0 + self.D, k0))
        hmcs = mac.control_mcs()
        hdr = self._header_body(n, k0, budget=self._max_body(k_end - k0, hmcs))
        h_len = self._blen(len(hdr), hmcs)
        work = self._collect_work(now)             # code -> list of candidate makers
        codes = sorted(set([BROADCAST_CODE] + [c for c, lst in work.items() if lst]))
        bo = self._slot_bo(len(codes))
        cursor = {c: k0 for c in codes}
        cursor[BROADCAST_CODE] = k0 + h_len
        sent = [(k0, BROADCAST_CODE, hdr, hmcs)]
        more, need_ms = False, 0.0
        tx_now = self.radio.tx_count()
        h_ms = h_len / self.fs * 1e3
        for code in codes:
            for cand in work.get(code, []):
                frames, left, unit = cand(bo, cursor[code], k_end, now, tx_now)
                for body, mcs in frames:
                    sent.append((cursor[code], code, body, mcs))
                    cursor[code] += self._blen(len(body), mcs)
                if left:
                    more = True
                    # what this lane would need for everything it has; a big backlog gets a data
                    # slot of a whole number of its frames, as close to DATA_MS as possible
                    lane_ms = (cursor[code] - k0 + left) / self.fs * 1e3
                    if lane_ms > DATA_MS and unit:
                        f_ms = max(0.5, unit / self.fs * 1e3)
                        lane_ms = max(1, int(round(DATA_MS / f_ms))) * f_ms + (h_ms if code == BROADCAST_CODE else 0.0)
                    need_ms = max(need_ms, lane_ms)
                    break                          # this lane is full
        for k, code, body, mcs in sorted(sent, key=lambda x: x[0]):
            self._emit(k, code, body, mcs, bo)
        used = max(cursor.values()) - k0
        # airtime to ask for in the next header: a data slot while work is left (big enough for at
        # least one more of the frames that did not fit), else what this slot used
        if more:
            self.want_ms = int(min(MAX_SLOT_MS, math.ceil(need_ms + GUARD_S * 1e3 + 0.6)))
        else:
            self.want_ms = max(IDLE_MS, int(math.ceil(used / self.fs * 1e3 + GUARD_S * 1e3 + 1.0)))
        self.stats["slots_sent"] += 1
        self.stats["frames_in_slots"] += len(sent)
        self.slot_log.append(dict(n=n, t=round(now, 3), frames=len(sent), codes=len(codes), bo=bo,
                                  ms=round(used / self.fs * 1e3, 1), full=more))
        # our own receiver needn't search our slot except around our header (calibration)
        blank = getattr(self.radio, "blank_rx", None)
        alone = self.role == LEAD and not self.members
        if (blank is not None and self.D is not None and now >= self.recal_until and not alone
                and n % FULL_SEARCH_EVERY != 0):
            blank(k0 + self.D + 4 * mac.radio.cfg.samples_per_symbol, k_end + self.D)
        # we should hear each of our headers come back; if they stop, D has probably moved
        self._own_miss += 1
        if self._own_miss % 30 == 7 and self.D is not None:
            self._recalibrate(now, "own headers not heard")

    # ------------------------------------------------------------------ what goes into a slot
    def _collect_work(self, now: float) -> dict:
        """code -> ordered list of candidate makers.  A maker(bo, cursor, k_end, now, tx_now) returns
        (frames [(body, mcs)], samples of the work it could not fit (0: done), samples of one of its
        frames (bulk data: the slot is a whole number of these))."""
        mac = self.mac
        work: dict[int, list] = collections.defaultdict(list)
        # datagrams: game states (sources: the newest one, built for this slot) and queued ones
        for fn in list(self.sources):
            try:
                for item in fn() or ():
                    dst, payload = item[0], item[1]
                    commit = item[2] if len(item) > 2 else None
                    work[code_index_for(dst)].append(self._dgram_maker(dst, payload, None, commit))
            except Exception as e:
                mac.log("slot source error", repr(e))
        for dst, q in list(self.dgrams.items()):
            while q:
                work[code_index_for(dst)].append(self._dgram_maker(dst, q.popleft(), True))
        if self.beacon is not None:
            work[BROADCAST_CODE].append(self._beacon_maker())
        # relayed frames we pass on
        for dst, q in list(self.fwd_q.items()):
            if q:
                work[code_index_for(dst)].append(self._fwd_maker(dst))
        # reliable messages
        for dst, msgs in list(mac.out.items()):
            live = [m for m in msgs if m.state.value in ("queued", "sending") and m.has_work()
                    and now >= m.not_before]
            if not live:
                continue
            if dst == BROADCAST_ADDR:
                work[BROADCAST_CODE].append(self._data_maker(dst, live, None))
                continue
            relay = self.route(dst)
            work[code_index_for(relay if relay else dst)].append(self._data_maker(dst, live, relay))
        return work

    def _dgram_maker(self, dst: int, payload: bytes, queued, commit=None):
        """A datagram at the most robust rate (QPSK 1/2).  queued: it came from the queue (put it back if
        the slot is full); commit: a slot source's callback, run only if the datagram goes out."""
        mac = self.mac

        def make(bo, cur, k_end, now, tx_now):
            from .mac import HDR, T_DGRAM, PRIO_CONTROL
            body = HDR.pack(T_DGRAM, PRIO_CONTROL, dst, self.addr, 0) + payload
            mcs = mac.control_mcs()
            ln = self._blen(len(body), mcs)
            if cur + ln > k_end:
                if queued:                         # (keep it for the next slot)
                    self.dgrams[dst].appendleft(payload)
                else:
                    mac.stats["dgram_no_room"] += 1
                return [], ln, 0
            if commit is not None:
                commit()
            mac.stats["dgram_tx"] += 1
            return [(body, mcs)], 0, 0
        return make

    def _beacon_maker(self):
        mac = self.mac

        def make(bo, cur, k_end, now, tx_now):
            from .mac import HDR, T_BEACON, PRIO_CONTROL
            rep = mac.beacon_report()
            body = HDR.pack(T_BEACON, PRIO_CONTROL, BROADCAST_ADDR, self.addr, len(rep) // 2) + rep + self.beacon
            mcs = mac.control_mcs()
            ln = self._blen(len(body), mcs)
            if cur + ln > k_end:
                return [], ln, 0
            self.beacon = None
            mac.stats["beacons_tx"] += 1
            return [(body, mcs)], 0, 0
        return make

    def _fwd_maker(self, dst: int):
        mac = self.mac

        def make(bo, cur, k_end, now, tx_now):
            out, q = [], self.fwd_q[dst]
            mcs = mac.mod_for(dst, bo)
            while q:
                body = q[0]
                ln = self._blen(len(body), mcs)
                if cur + ln > k_end:
                    return out, ln * min(len(q), 64), ln
                q.popleft()
                out.append((body, mcs))
                cur += ln
                mac.stats["relay_fwd"] += 1
            return out, 0, 0
        return make

    def _data_maker(self, dst: int, msgs: list, relay: int | None):
        mac = self.mac

        def make(bo, cur, k_end, now, tx_now):
            from .mac import HDR, T_DATA, T_FWD
            out = []
            if dst == BROADCAST_ADDR:
                mcs_all = mac.group_mcs(bo)
            else:
                mcs_all = mac.mod_for(relay if relay else dst, bo)
            lad = mac.ladder
            for m in sorted(msgs, key=lambda x: (-x.prio, x.created)):
                # short messages: one loss costs a round trip, so two rungs lower - but no lower than
                # the slot allows: a full fragment at the slow rung can be longer than an idle slot
                # (44 ms with two stations), and the message would wait ~1.5 s for its slot to grow
                short = len(m.frags) <= 2 and mcs_all in lad
                while m.has_work() and mac.inflight_ok(m):
                    i = m.retx[0] if m.retx else m.next_new
                    if relay:
                        body = HDR.pack(T_FWD, m.prio & 3, dst, self.addr, m.msg_id) + struct.pack(
                            ">BHH", relay, i, len(m.frags)) + m.frags[i]
                    else:
                        body = HDR.pack(T_DATA, m.prio & 3, dst, self.addr, m.msg_id) + struct.pack(
                            ">HH", i, len(m.frags)) + m.frags[i]
                    mcs = mcs_all
                    if short:
                        top = lad.index(mcs_all)
                        mcs = lad[max(0, top - 2)]
                        for j in range(max(0, top - 2), top + 1):
                            if cur + self._blen(len(body), lad[j]) <= k_end:
                                mcs = lad[j]
                                break
                    ln = self._blen(len(body), mcs)
                    if cur + ln > k_end:
                        left = sum(len(x.retx) + len(x.frags) - x.next_new for x in msgs if x.has_work())
                        return out, ln * min(max(1, left), 64), ln
                    cur += ln
                    mac.mark_sent(m, i, now + (cur - tx_now) / self.fs, relay)
                    out.append((body, mcs))
            return out, 0, 0
        return make

    # ------------------------------------------------------------------ the header
    def _status(self, join: bool = False) -> int:
        st = 0
        if self.role == LEAD:
            st |= ST_LEAD
        if self.role == MEMBER or self.role == LEAD:
            st |= ST_MEMBER
        if join or self.role == JOINING:
            st |= ST_JOIN
        if self.relay_on:
            st |= ST_RELAY
        if self.D is not None:
            st |= ST_DOK
        if self.rsync:
            st |= ST_RSYNC
        if (self.role == MEMBER and not self.rsync and self.last_lead_n is not None
                and self.lead_silent_for(self.now()) > 1):
            st |= ST_NOLEAD                        # lead headers missing: whoever hears it, repeat its plan
        return st

    def _max_body(self, samples: int, mcs: str) -> int:
        """The longest MAC body whose burst fits into `samples`."""
        lo, hi = 0, int(getattr(self.radio.cfg, "max_phy_payload", 1024)) - 4
        if self._blen(hi, mcs) <= samples:
            return hi
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._blen(mid, mcs) <= samples:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _header_body(self, n: int, k: int, join: bool = False, budget: int | None = None) -> bytes:
        """Our slot header.  budget: the most bytes that fit into the slot; the plan sections always
        go in, the rest (dup reports, timing feedback, link table, ACK records) as far as they fit -
        what is left out goes in the next slot (it is resent every cycle anyway)."""
        from .mac import HDR, T_SLOT, PRIO_CONTROL
        mac = self.mac
        fix = FIX.pack(int(k) & 0xFFFFFFFF, self.nonce, self._status(join), max(0, min(255, int(self.want_ms))),
                       (self.lead_id or 0) & 0xFF, (self.plan_known_n if self.plan_known_n is not None
                                                     else (self.last_lead_n or 0)) & 0xFFFF)
        secs = b""
        now = self.now()
        relay_sync = not join and self.role == MEMBER and self._relay_sync_needed(now)
        if (self.role == LEAD and not join) or relay_sync:
            # the lead's plan; a relaying member repeats it for stations that can't hear the lead
            tl = self.tl
            secs += _sec(SEC_PLAN, tl.plan(n).encode(n))
            eff, latest = tl.latest()
            if eff > n:
                secs += _sec(SEC_NEXT, latest.encode(eff))
            if relay_sync:
                self.stats["plan_relayed"] += 1
                # join requests and airtime wishes of stations the lead may not hear
                jr = b"".join(JREQ.pack(a, h["nonce"], h["want"]) for a, h in self.heard.items()
                              if h["status"] & (ST_JOIN | ST_RSYNC) and h["lead"] in (0, self.lead_id)
                              and now - h["t"] < 1.0)
                if jr:
                    secs += _sec(SEC_JREQ, jr)
        head = HDR.pack(T_SLOT, PRIO_CONTROL, BROADCAST_ADDR, self.addr, n & 0xFFFF) + fix
        opt = []                                   # optional sections, most important first
        if not join:
            dup = b"".join(DUP.pack(a, x, y) for a, (x, y, t) in self.dups.items() if now - t < 15)
            if dup:
                opt.append(_sec(SEC_DUP, dup))
            # timing errors of the stations heard in the last few cycles (also tells the lead who is
            # still on the air, for stations it can't hear itself)
            fresh = max(0.5, 3 * (self.cycle_s or 0.1))
            tfb = b"".join(TFB.pack(a, max(-32768, min(32767, e))) for a, e in sorted(self.tfb_mine.items())
                           if now - self.heard.get(a, {}).get("t", 0) < fresh)
            if tfb:
                opt.append(_sec(SEC_TFB, tfb))
        rep = mac.beacon_report(max_age=LINK_FRESH_S)
        if rep:
            opt.append(_sec(SEC_LINK, rep))
        room = None if budget is None else budget - len(head) - len(secs)
        for x in opt:
            if room is None or len(x) <= room:
                secs += x
                room = None if room is None else room - len(x)
            else:
                self.stats["hdr_sections_deferred"] += 1
        if not join:
            cap = MAX_ACK_BYTES if room is None else min(MAX_ACK_BYTES, room - 2)
            recs = self._ack_records(cap) if cap > REC.size else b""
            if recs:
                secs += _sec(SEC_ACK, recs)
        return head + secs

    def _ack_records(self, cap: int = MAX_ACK_BYTES) -> bytes:
        """Records for the messages we received pieces of (repeated in ACK_REPEAT headers), and the
        ones we pass on for stations we relay for; at most cap bytes (the rest waits for the next slot)."""
        mac = self.mac
        out = b""
        for key in list(self.ack_due):
            info = self.ack_due[key]
            rec = mac.ack_record(key[0], key[1], info)
            if rec is None:
                del self.ack_due[key]
                continue
            if len(out) + len(rec) > cap:
                break
            out += rec
            info["repeat"] -= 1
            if info["repeat"] <= 0:
                del self.ack_due[key]
        for key in list(self.relay_recs):
            rec, left = self.relay_recs[key]
            if len(out) + len(rec) > cap:
                break
            out += rec
            if left <= 1:
                del self.relay_recs[key]
            else:
                self.relay_recs[key] = (rec, left - 1)
        return out

    def note_rx(self, src: int, msg_id: int, trigger: int, via: int | None):
        """A data frame arrived: acknowledge it in our next headers."""
        key = (src, msg_id)
        info = self.ack_due.get(key)
        if info is None:
            info = self.ack_due[key] = dict(repeat=ACK_REPEAT, trigger=trigger, via=via)
        else:
            info.update(repeat=ACK_REPEAT, trigger=trigger, via=via)
        self.ack_due.move_to_end(key)
        while len(self.ack_due) > 64:
            self.ack_due.popitem(last=False)

    # ------------------------------------------------------------------ receiving headers
    def on_header(self, src: int, msg_id: int, rest: bytes, f):
        j = getattr(f, "sample_index", None)
        if len(rest) < FIX.size:
            return
        tx_low, nonce, st, want, lead, lead_seen = FIX.unpack_from(rest)
        secs = rest[FIX.size:]
        now = self.now()
        if src == self.addr:
            if nonce == self.nonce:
                if j is not None:
                    self._calibrate(int(j), tx_low)
            else:
                self._note_dup(src, self.nonce, nonce, now)
            return
        self.stats["headers_rx"] += 1
        h = self.heard.get(src)
        if h is not None and h["nonce"] != nonce and now - h["t"] < 10:
            self._nonce_changed(src, h["nonce"], nonce, now)
        plan_cur = plan_next = None
        acks, link, jreqs, seen = [], None, [], []
        for tag, data in _sections(secs):
            try:
                if tag == SEC_PLAN:
                    plan_cur = Plan.decode(data)
                elif tag == SEC_NEXT:
                    plan_next = Plan.decode(data)
                elif tag == SEC_ACK:
                    acks.append(data)
                elif tag == SEC_LINK:
                    link = data
                elif tag == SEC_TFB:
                    for i in range(0, len(data) - TFB.size + 1, TFB.size):
                        a, e = TFB.unpack_from(data, i)
                        seen.append(a)
                        if a == self.addr and st & ST_DOK:
                            self.tfb_about_me[src] = (e, now)
                            # (only matters when we can't hear ourselves: then it is all we know)
                            if abs(e) > 2 * self.gh and self._own_miss > 2:
                                self._recalibrate(now, f"station {src} hears us {e} samples off")
                elif tag == SEC_JREQ:
                    for i in range(0, len(data) - JREQ.size + 1, JREQ.size):
                        jreqs.append(JREQ.unpack_from(data, i))
                elif tag == SEC_DUP:
                    for i in range(0, len(data) - DUP.size + 1, DUP.size):
                        a, x, y = DUP.unpack_from(data, i)
                        if a == self.addr:
                            self._dup_about_me(x, y, now)
            except struct.error:
                self.stats["bad_sections"] += 1
        n_full = plan_cur[0] if plan_cur else self._full_cycle(msg_id)
        seen_full = None
        if n_full is not None:                     # (lead_seen: a cycle number up to the header's own)
            seen_full = n_full - ((n_full - lead_seen) & 0xFFFF)
        self.heard[src] = dict(n=n_full, j=j, t=now, nonce=nonce, status=st, want=want, lead=lead,
                               lead_seen=seen_full)
        if lead and self.lead_id and lead != self.lead_id and self.role in (MEMBER, LEAD):
            first, last = self.other_leads.get(lead, (now, -1e9))
            self.other_leads[lead] = (first if now - last < 2.0 else now, now)   # (dashboard warning)
        if link is not None:
            tab = {}
            for i in range(0, len(link) - 1, 2):
                a, v = struct.unpack_from(">Bb", link, i)
                tab[a] = v / 2.0
            self.links[src] = tab
            self.links_t[src] = now
        # timing: where its header arrived vs where the plan puts it (our receive samples)
        if j is not None and n_full is not None and self.tl.valid() and not (st & ST_JOIN and not st & ST_MEMBER):
            exp = self._expected_rx(src, n_full)
            if exp is not None and not (st & ST_LEAD and src == self.lead_id):
                self.tfb_mine[src] = int(j - exp)
        if st & ST_LEAD and plan_cur is not None and j is not None:
            self._on_lead_header(src, plan_cur, plan_next, int(j), now)
        elif plan_cur is not None and j is not None and lead:
            self._on_lead_header(src, plan_cur, plan_next, int(j), now, lead=lead,   # a relayed plan
                                 known=seen_full)
        if self.role == LEAD and not st & ST_LEAD:
            self._lead_on_header(src, st, want, nonce, lead, now)
            for a, nc, w in jreqs:                 # join requests a member heard and passed on
                if a != self.addr:
                    self._lead_on_header(a, ST_JOIN, w, nc, self.addr, now, relayed=True)
            for a in seen:                         # stations the member heard (we may not)
                m = self.members.get(a)
                if m is not None:
                    m["heard"] = max(m["heard"], now - (self.cycle_s or 0.1))
        for data in acks:
            self._on_records(src, data)

    def _full_cycle(self, low: int) -> int | None:
        ref = self.next_n if self.next_n is not None else self.tl.n
        if ref is None:
            return None
        n = ref - ((ref - low) & 0xFFFF)
        if ref - n > 0x8000:
            n += 0x10000
        return n

    def _expected_rx(self, src: int, n: int) -> int | None:
        """Receive sample at which src's header of cycle n should arrive here."""
        tl = self.tl
        try:
            s = tl.plan(n).slot(src)
        except IndexError:
            return None
        if s is None:
            return None
        pos = tl.start(n) + tl.to_samples(s[0]) + self.gh
        if self.role == LEAD:
            if self.D is None:
                return None
            pos += self.D
        return pos

    def _on_lead_header(self, src: int, plan_cur, plan_next, j: int, now: float, lead: int | None = None,
                        known: int | None = None):
        """A header carrying the slot plan: from the lead itself (lead == src), or repeated by a relaying
        member (lead = the lead it follows) for stations that can't hear the lead.  Either way the
        transmitter's slot in the plan tells where the cycle started.  known: up to which cycle the
        repeating member had heard the lead (its plans are as current as that)."""
        n, plan = plan_cur
        lead = src if lead is None else lead
        relayed = lead != src
        if not lead or plan.slot(src) is None:
            return
        if self.role == LEAD:
            if lead < self.addr:                   # two networks met: the lower address leads
                self.mac.log(f"tdma{self.addr}: station {lead} leads here too - joining it")
                self.stats["yielded"] += 1
                self._reset()
                self.role = JOINING
            else:
                self.stats["other_lead"] += 1
                return
        if self.lead_id not in (None, lead):
            lost = self.last_lead_n is None or self.lead_silent_for(now) > LEAD_MISS
            if lead > self.lead_id and not lost:
                self.stats["other_lead"] += 1
                return
            self.mac.log(f"tdma{self.addr}: following station {lead}" + (f" (via {src})" if relayed else ""))
            self.next_n = None
        if relayed:
            # only while the lead itself is not heard (it is the better reference)
            if self.lead_id == lead and not self.rsync and self.lead_silent_for(now) <= LEAD_MISS:
                return
            if self.rsync_ref not in (None, src) and now - self.rsync_t < 2 * (self.cycle_s or 0.1) + 0.1:
                return                             # (stick to one relaying station)
        s = plan.slot(src)
        anchor = j - self.tl.to_samples(s[0]) - self.gh
        same = self.lead_id == lead and self.tl.valid() and (self.rsync_ref if relayed else None) == (src if relayed else None)
        if same and self.last_lead_n is not None:
            self.tl.set_anchor(n, anchor, learn=True)
        else:
            self.tl = Timeline(self.spt)
            self.tl.set_anchor(n, anchor)
        self.lead_id = lead
        self.rsync = relayed
        self.rsync_ref = src if relayed else None
        if relayed:
            self.rsync_t = now
            self.stats["synced_via_relay"] += 1
        self.tl.add_plan(n, plan)
        if plan_next is not None:
            eff, nxt = plan_next
            if eff > n:
                self.tl.add_plan(eff, nxt)
        self.tl.prune(n)
        k = n if not relayed else min(n, known if known is not None else n - PLAN_AHEAD_CYCLES)
        if not same or self.plan_known_n is None or k > self.plan_known_n:
            self.plan_known_n = k
        self.last_lead_n = n
        self.last_lead_t = now
        self.last_lead_j = j
        mine = plan.slot(self.addr) is not None or (plan_next is not None and plan_next[1].slot(self.addr) is not None)
        if mine:
            self._rejoin = False
        if self.role in (LISTEN, JOINING):
            if self.role == LISTEN:
                self.role = JOINING
                self.next_n = None
            if mine:
                self.role = MEMBER
                self.next_n = None
                self.stats["joined"] += 1
                self.mac.log(f"tdma{self.addr}: got a slot from station {lead}" + (f" (via {src})" if relayed else ""))
        self.cycle_s = plan.T * TICK_S

    def _relay_sync_needed(self, now: float) -> bool:
        """Repeat the lead's plan in our header (whether or not we relay messages: it is only timing):
        we hear the lead ourselves, and we hear a station that is not synced to our lead (a newcomer, a
        lone network of its own), that keeps its timing from repeated plans, or that has stopped
        hearing the lead."""
        if not self.tl.valid() or self.rsync or self.lead_silent_for(now) > 1:
            return False
        for a, h in self.heard.items():
            if a == self.lead_id or now - h["t"] > 3.0:
                continue
            st = h["status"]
            if st & ST_RSYNC or (st & ST_JOIN and h["lead"] in (0, self.lead_id)):
                return True
            if st & ST_NOLEAD and h["lead"] == self.lead_id and now - h["t"] < 1.0:
                return True
            if st & ST_LEAD and self.lead_id is not None and a > self.lead_id:
                return True                        # a lone network that can't hear our lead
        return False

    def _first_successor_here(self, plan: Plan, now: float) -> bool:
        """A station ranked before us as the lead's successor is still heard: it takes over, not we."""
        others = sorted(a for a in plan.owners() if a != self.lead_id)
        mine = others.index(self.addr) if self.addr in others else len(others)
        cyc = self.cycle_s or 0.1
        return any(now - self.heard.get(a, {}).get("t", -1e9) < 2 * cyc + 0.1 for a in others[:mine])

    def _others_heard(self, now: float) -> bool:
        """Some station other than the lead was heard within the last two cycles."""
        cyc = self.cycle_s or 0.1
        return any(a not in (self.lead_id, self.addr) and now - h["t"] < 2 * cyc + 0.1 for a, h in self.heard.items())

    def _lead_heard_by_others(self, now: float) -> bool:
        """Another station's latest header (sent within the last two cycles) says it heard our lead
        within LEAD_MISS + 1 cycles before sending it: the lead is alive, only we lost it."""
        cyc = self.cycle_s or 0.1
        for a, h in self.heard.items():
            if a in (self.lead_id, self.addr) or h.get("lead") != self.lead_id or now - h["t"] > 2 * cyc + 0.1:
                continue
            if h.get("lead_seen") is not None and h.get("n") is not None and h["n"] - h["lead_seen"] <= LEAD_MISS + 1:
                return True
        return False

    def _peer_timing(self, now: float):
        """Fallback for a station that cannot hear its own transmissions: the others report where its
        header arrived against where the plan puts it (TFB); steer D by their median report."""
        if self.role != MEMBER or self._own_miss <= 8 or now < self._fb_next:
            return
        errs = sorted(e for e, t in self.tfb_about_me.values() if now - t < 2.0)
        if not errs:
            return
        med = errs[len(errs) // 2]
        if self.D is None:
            self.D = self._d_guess()
        if abs(med) > 4:
            self.D += int(round(0.7 * med))         # late (med > 0): transmit earlier = larger D
            self.next_n = None
            self.stats["d_from_peers"] += 1
        self.d_source = "peers"
        self._fb_next = now + 3 * (self.cycle_s or 0.1) + 0.1

    def _fault_count(self) -> int:
        fn = getattr(self.radio, "timing_faults", None)
        return int(fn()) if fn else 0

    def _recalibrate(self, now: float, why: str):
        if now >= self.recal_until:
            self.stats["recalibrations"] += 1
            self.mac.log(f"tdma{self.addr}: {why} - checking the TX->RX offset again")
        self.recal_until = now + RECAL_S

    def _update_windows(self, now: float):
        """Tell the receiver where bursts can start: around every other station's slot start (their
        header and first bursts; the rest follow back to back and the receiver chains along them),
        around our own header (calibration), the whole join slot (broadcast code only).  Every 8th
        cycle, while the lead is not heard, or after a possible timing slip: everywhere."""
        setw = getattr(self.radio, "set_search_windows", None)
        if setw is None:
            return
        tl = self.tl
        ok = (tl.valid() and self.role in (MEMBER, LEAD, JOINING) and now >= self.recal_until
              and (self.role == LEAD or self.lead_silent_for(now) < 1.0))
        D = self.D if self.role == LEAD else 0
        if self.role == LEAD and (self.D is None or not self.members):
            ok = False                             # (alone: listen everywhere for another network)
        if not ok:
            if self.windowed:
                setw(None, 0)
                self.windowed = False
            return
        rx = self.radio.rx_count()
        here = rx - D
        n0 = tl.cycle_at(here) - 1
        probing = self.D is None or self._tx_doubt
        key = (n0, tl.n, len(tl.sched), self.D, self.role, probing)
        if key == self._win_key:
            return
        self._win_key = key
        W = self.gh + 64
        P0, P1 = int(0.01 * self.fs), int(0.1 * self.fs)   # (where a probe may land: up to 0.1 s late)
        mine = code_index_for(self.addr)
        wins = []
        for n in range(n0, n0 + 4):
            base = tl.start(n) + D
            if n % FULL_SEARCH_EVERY == 0:
                wins.append((base, tl.start(n + 1) + D, -1))
                continue
            for owner, st, ln in tl.plan(n).slots:
                a = base + tl.to_samples(st)
                if owner == 0:
                    wins.append((a, a + tl.to_samples(ln), BROADCAST_CODE))
                    if self.D is None:             # our probe, sent at the join slot by a guessed D
                        wins.append((a - P0, a + P1, mine))
                elif owner == self.addr:
                    if self.D is not None:
                        wins.append((a + self.gh - W, a + self.gh + W, BROADCAST_CODE))
                    if self._tx_doubt:             # our probe, sent at our slot after a TX slip
                        wins.append((a - P1, a + P1, mine))
                else:
                    wins.append((a + self.gh - W, a + self.gh + W, -1))
        setw(wins, tl.start(n0 + 4) + D)
        self.windowed = True

    def _detect_filter(self, code: int, t: int) -> bool:
        """Receive thread: a broadcast-code preamble right where our own header must arrive is our
        header; its position calibrates D, decoding it would tell us nothing more."""
        if code != BROADCAST_CODE or self.D is None:
            return False
        for exp, k in list(self._own_hdr):
            if abs(t - exp) <= 24:
                self._self_meas.append((t, k))
                return True
        return False

    def _calibrate(self, j: int, tx_low: int):
        now_tx = self.radio.tx_count()
        k = now_tx - ((now_tx - tx_low) & 0xFFFFFFFF)
        self._calibrate_k(j, k)

    def _calibrate_k(self, j: int, k: int):
        self._own_miss = 0
        self.d_source = "self"
        d = j - k
        self.stats["self_heard"] += 1
        if self.D is not None and abs(d - self.D) <= 4:
            self._tx_doubt = False                 # (D confirmed: send again)
        if self.D is None:
            g = self._d_guess()
            self.D = d
            self._d_meas.append(d)
            self.stats["d_guess_err"] = g - d
            self.mac.log(f"tdma{self.addr}: TX->RX offset D = {d} samples (the guess was {g - d:+d} off)")
            return
        if abs(d - self.D) <= 4:
            self._d_meas.append(d)
            vals = sorted(self._d_meas)
            self.D = vals[len(vals) // 2]
            self._d_odd = []
            return
        # a jump (TX underrun, lost RX buffer): believe it when two measurements agree; meanwhile a member
        # sends only quiet probes (with D in doubt its bursts could land in the next or previous slot)
        self._d_odd.append(d)
        if self.role == MEMBER and not self._tx_doubt:
            self._tx_doubt, self._doubt_probes = True, 0
        if len(self._d_odd) >= 2 and abs(self._d_odd[-1] - self._d_odd[-2]) <= 4:
            self.D = self._d_odd[-1]
            self._d_meas.clear()
            self._d_meas.append(self.D)
            self._d_odd = []
            self.d_jumps += 1
            self._tx_doubt = False
            if self.role != LEAD:                  # (the lead keeps time in transmit samples: unaffected)
                self.next_n = None
            self.mac.log(f"tdma{self.addr}: TX->RX offset jumped to D = {self.D}")

    def _nonce_changed(self, addr: int, old: int, new: int, now: float):
        """Station addr's headers carry a new nonce.  Its app was restarted (the old nonce is gone for good) or
        two radios share the address (both keep coming): only the second is a duplicate, so it takes hearing
        the old nonce again.  (A restart within 10 s was reported as a duplicate: seen with fake ANTSDRs.)"""
        prev = self._nonce_prev.get(addr)
        if prev is not None and prev[0] == new and now - prev[1] < 10:
            self._note_dup(addr, old, new, now)
        self._nonce_prev[addr] = (old, now)

    def _note_dup(self, addr: int, a: int, b: int, now: float):
        if a == b:
            return
        self.dups[addr] = (a, b, now)
        self.stats["dup_seen"] += 1
        if addr == self.addr:
            self._dup_about_me(a, b, now)

    def _dup_about_me(self, a: int, b: int, now: float):
        msg = f"Another station is using address {self.addr}: give every station its own --id"
        if self.dup_warn != msg:
            self.mac.log("tdma: " + msg)
        self.dup_warn = msg
        self.dup_t = now

    # ------------------------------------------------------------------ ACK records
    def _on_records(self, hdr_src: int, data: bytes):
        mac = self.mac
        i = 0
        while i + REC.size <= len(data):
            sender, acker, mid, count, base, trig, snr, nbm = REC.unpack_from(data, i)
            bm = data[i + REC.size:i + REC.size + nbm]
            raw = data[i:i + REC.size + nbm]
            i += REC.size + nbm
            if sender == self.addr:
                mac.on_record(acker, mid, count, base, trig, snr, bm, direct=(acker == hdr_src))
            elif self.relay_on and acker == hdr_src and (sender, mid) in self.relayed:
                # we relay this message: its receiver's ACK goes on to the sender in our header
                self.relay_recs[(sender, acker, mid)] = (raw, ACK_REPEAT)
                self.relay_recs.move_to_end((sender, acker, mid))
                while len(self.relay_recs) > 32:
                    self.relay_recs.popitem(last=False)

    # ------------------------------------------------------------------ relaying
    def hears(self, a: int, b: int) -> float | None:
        """How station a hears station b (Es/N0 at full power, dB) from a's latest link table; for
        a == self, our own measurement."""
        now = self.now()
        if a == self.addr:
            q = self.mac.link_quality.get(b) or {}
            if now - self.mac.last_heard_r.get(b, -1e9) > RELAY_FRESH_S:
                return None
            return q.get("esn0_full")
        if now - self.links_t.get(a, -1e9) > RELAY_FRESH_S:
            return None
        return self.links.get(a, {}).get(b)

    def route(self, dst: int) -> int | None:
        """The relay to send dst's messages through, or None (direct)."""
        if not self.relay_on or dst == BROADCAST_ADDR:
            self.routes.pop(dst, None)
            return None
        now = self.now()
        direct = self.hears(dst, self.addr)
        r = self.routes.get(dst)
        m_fail = self.mac.failing(dst)
        good_direct = direct is not None and direct >= RELAY_DIRECT_DB and not m_fail
        if r is not None:
            # back to direct once it has been good for a while
            if good_direct and now - r["since"] > 5.0:
                del self.routes[dst]
                self.stats["route_direct"] += 1
                return None
            rel = r["relay"]
            b = self.hears(dst, rel)
            if b is None:
                b = self.hears(rel, dst)
            ok = (self.heard.get(rel, {}).get("status", 0) & ST_RELAY and (self.hears(rel, self.addr) or -99) >= RELAY_MIN_DB
                  and (b if b is not None else -99) >= RELAY_MIN_DB)
            if ok:
                return rel
            del self.routes[dst]
        if good_direct:
            return None
        if direct is not None and not m_fail:
            return None                            # weak but working: stay direct
        best, q = None, RELAY_MIN_DB
        for rel, h in self.heard.items():
            if rel in (dst, self.addr) or not h.get("status", 0) & ST_RELAY or now - h["t"] > RELAY_FRESH_S:
                continue
            a = self.hears(rel, self.addr)
            b = self.hears(dst, rel)
            if b is None:
                b = self.hears(rel, dst)           # (dst's own table may not reach us: assume symmetry)
            if a is None or b is None:
                continue
            v = min(a, b)
            if v >= q:
                best, q = rel, v
        if best is not None:
            self.routes[dst] = dict(relay=best, since=now, why="no direct link" if direct is None else "direct failing")
            self.stats["route_relay"] += 1
            self.mac.log(f"tdma{self.addr}: messages to {dst} go via {best}")
        return best

    def reachable_via(self) -> dict[int, int]:
        """Stations we can't hear but a relaying station can: addr -> relay."""
        out = {}
        if not self.relay_on:
            return out
        now = self.now()
        for rel, h in self.heard.items():
            if not h.get("status", 0) & ST_RELAY or now - h["t"] > RELAY_FRESH_S:
                continue
            for a, v in (self.links.get(rel) or {}).items():
                if a != self.addr and v >= RELAY_MIN_DB and self.hears(self.addr, a) is None:
                    out.setdefault(a, rel)
        return out

    def on_forward(self, body: bytes, dst: int, origin: int, msg_id: int):
        """A frame we relay: send it on to dst in our next slot."""
        if not self.relay_on:
            self.stats["relay_refused"] += 1
            return
        q = self.fwd_q[dst]
        # a fragment the sender repeated (its ACK timer is shorter than our slot takes to grow) while
        # the first copy still waits here: pass it on once.  (dst, src, msg_id, frag_idx; the flags
        # byte differs between copies)
        key = body[2:6] + body[7:9]
        if any(x[2:6] + x[7:9] == key for x in q):
            self.stats["relay_dup_dropped"] += 1
            return
        q.append(bytes(body))
        while len(q) > 256:
            q.popleft()
            self.stats["relay_dropped"] += 1
        self.relayed[(origin, msg_id)] = self.now()
        if len(self.relayed) > 256:
            for k in sorted(self.relayed, key=self.relayed.get)[:128]:
                del self.relayed[k]

    def set_relay(self, on: bool):
        self.relay_on = bool(on)
        if not on:
            self.fwd_q.clear()
            self.relay_recs.clear()
            self.routes.clear()

    # ------------------------------------------------------------------ dashboard
    def _other_net_warning(self, now: float) -> str | None:
        """A second network has been on the air here for a few seconds (stations out of each other's
        range, not joined into one): two sets of time slots overlap."""
        other = sorted(a for a, (first, last) in self.other_leads.items()
                       if now - last < 2.0 and now - first > 3.0 and a != self.lead_id)
        if not other:
            return None
        return (f"Another network (lead: station {', '.join(map(str, other))}) is on the air here and its time "
                f"slots overlap ours. A station that hears both leads joins them into one; if this stays, "
                f"move the stations closer together.")

    def status(self) -> dict:
        now = self.now()
        tl = self.tl
        plan = None
        nxt = None
        if tl.valid():
            n = self.next_n if self.next_n is not None else tl.n
            p = tl.plan(n)
            plan = dict(ver=p.ver, cycle_ms=round(p.T * TICK_S * 1e3, 2), slots=p.describe())
            eff, latest = tl.latest()
            if eff > n:
                nxt = dict(ver=latest.ver, in_cycles=eff - n, cycle_ms=round(latest.T * TICK_S * 1e3, 2),
                           slots=latest.describe())
        fb = {str(a): e for a, (e, t) in self.tfb_about_me.items() if now - t < 5}
        return dict(mode="tdcdma", role=self.role, lead=self.lead_id, cycle=self.next_n, plan=plan, next=nxt,
                    D=self.D, d_source=self.d_source, d_jumps=self.d_jumps, drift_ppm=round(tl.eps * 1e6, 2),
                    timing_err_me=fb, timing_err_peers={str(a): e for a, e in self.tfb_mine.items()},
                    late=self.radio.late_bursts() if hasattr(self.radio, "late_bursts") else 0,
                    relay=self.relay_on, routes={str(d): dict(r) for d, r in self.routes.items()},
                    relay_queue=sum(len(q) for q in self.fwd_q.values()),
                    reach_via={str(a): r for a, r in self.reachable_via().items()},
                    dup=self.dup_warn, other_net=self._other_net_warning(now),
                    want_ms=self.want_ms, nonce=self.nonce, windowed=self.windowed,
                    recal=now < self.recal_until,
                    heard={str(a): dict(age=round(now - h["t"], 2), status=h["status"], lead=h["lead"])
                           for a, h in self.heard.items() if now - h["t"] < 30},
                    slots=list(self.slot_log)[-12:], stats=dict(self.stats))
