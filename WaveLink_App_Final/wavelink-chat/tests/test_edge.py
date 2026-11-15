"""Edge cases of the TD-CDMA link layer and the game center, on the simulated air.

    python3 -m tests.test_edge            # all of them (takes a while; 5 runs 10 simulated minutes)
    python3 -m tests.test_edge 3 9 12     # just these
    python3 -m tests.test_edge --quick    # shorter versions (5: 2 minutes)

  1  start orders: one after another (3, 1, 2), all at once, a late lone station
  2  a station joins and one leaves while a big transfer runs
  3  the lead leaves mid-transfer (its successor takes over), then comes back
  4  two newcomers at the same moment; two stations with the same address
  5  clock drift of +/-25 ppm (50 ppm between stations) for 10 minutes
  6  TX underruns and lost RX buffers during a transfer
  7  4 stations all sending at once: TD-CDMA vs spread ALOHA
  8  near-far without power control: TD-CDMA vs spread ALOHA
  9  relaying switched off and on again in the middle of a relayed transfer
 10  a group message with one member out of range (reached through a relay), then leaving
 11  race with 4 players: one leaves and comes back, one leaves for good
 12  UNO: a player leaves and comes back; the host leaves; a player back after the grace period
 13  low SNR (5 dB) and bursty interference
 14  a station misses every announcement of the slot plan changes
 15  a station stops hearing the lead (the others still do), with relaying on and off
 16  a station off the air for 1 s (keeps its slot) and for 2.5 s (rejoins at once)
 17  a hidden station with relaying off: still one network

Every station's receiver counts samples from a random start, so TX -> RX offsets must be
measured, and every link has a random delay of up to 100 us (more than real propagation: the
guard time is tested too).  The simulated air records any two stations transmitting at the same
time that a third one hears both of: in a formed TD-CDMA network that must never happen.
"""
import collections
import os
import random
import shutil
import sys
import tempfile
import time

from wavelink.config import PhyConfig
from wavelink.mac import LinkLayer, PRIO_BULK, PRIO_NORMAL
from wavelink.radio_sim import SimAir

QUICK = "--quick" in sys.argv
RESULTS = []


def say(*a):
    print(*a, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    say(f"    {'ok ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    return ok


# ============================================================================ helpers
class Net:
    """N stations (link layer only) on one simulated air."""

    def __init__(self, ids=(1, 2, 3), ebn0=25.0, seed=1, mode="tdcdma", relay=True, ppm=None, pc=True,
                 start=True, profile="fast", self_leak=3.0, hw=False):
        """self_leak: how much stronger a station hears its own transmitter than a 0 dB link (the
        simulator's default 3 dB lets it receive while sending; on the ANTSDRs it is ~25 dB or more).
        hw: the settings `run` uses on hardware (spread ALOHA: half-duplex pause, ACK gap)."""
        self.cfg = PhyConfig.from_profile(profile)
        self.air = SimAir(self.cfg, ebn0_db=ebn0, seed=seed, speed=1000, self_leak_db=self_leak)
        self.mode, self.relay, self.pc, self.hw = mode, relay, pc, hw
        self.check_from = None                    # air time from which overlapping slots are errors
        self.macs, self.inbox, self.logs = {}, collections.defaultdict(list), []
        self.radios = {}
        for a in ids:
            self.radios[a] = self.air.add_node(a, ppm=(ppm or {}).get(a, 0.0))
        for a in ids:
            self.macs[a] = self._mac(a)
        if start:
            for a in ids:
                self.macs[a].start()
            self.air.start()

    def _mac(self, a):
        m = LinkLayer(self.radios[a], a, mode=self.mode, relay=self.relay,
                      on_message=lambda s, d, meta, a=a: self.inbox[a].append((s, d, meta)),
                      log=lambda *x, a=a: self.logs.append((round(self.air.time, 2), a) + x))
        m.power_control = self.pc
        if self.hw and m.tdma is None:                # (what tune_mac does for `run`)
            m.half_duplex_defer, m.ack_gap, m.window_airtime, m.ack_timeout = 0.25, 0.15, 0.6, 0.5
        return m

    def restart(self, a):
        """The app on station a is started again (a new link layer on the same radio)."""
        self.macs[a].stop()
        self.macs[a] = self._mac(a)
        self.macs[a].start()

    def run(self, t):
        t0 = self.air.time
        while self.air.time - t0 < t:
            time.sleep(0.02)

    def wait(self, cond, t):
        t0 = self.air.time
        while not cond():
            if self.air.time - t0 > t:
                return False
            time.sleep(0.02)
        return True

    def td(self, a):
        return self.macs[a].tdma

    def steady(self):
        """From now on (the network has formed) no two stations may transmit at once."""
        self.check_from = self.air.time

    def overlaps(self):
        return [o for o in self.air.overlaps if self.check_from is not None and o[0] >= self.check_from]

    def roles(self):
        return {a: (m.tdma.role, m.tdma.lead_id) for a, m in self.macs.items() if m.tdma}

    def one_network(self, ids=None):
        ids = ids or list(self.macs)
        r = [self.td(a) for a in ids]
        leads = {t.lead_id for t in r}
        return (all(t.role in ("lead", "member") for t in r) and len(leads) == 1
                and sum(t.role == "lead" for t in r) == 1)

    def late(self):
        return sum(m.radio.late_bursts() for m in self.macs.values())

    def stop(self):
        for m in self.macs.values():
            m.stop()
        self.air.stop()
        time.sleep(0.2)


def no_overlap(net, what="no two stations ever on the air at once"):
    ov = net.overlaps()
    return check(what, not ov, f"{len(ov)} times, first {ov[:3]}" if ov else "")


def delivered(*msgs):
    return all(m.state.value == "delivered" for m in msgs)


def done(*msgs):
    return all(m.state.value in ("delivered", "failed") for m in msgs)


def goodput(m):
    if m.done_time is None or m.first_tx is None:
        return 0.0
    return len(m.data) * 8 / max(1e-3, m.done_time - m.first_tx) / 1e3


# ============================================================================ 1
def t1_start_orders():
    say("1. start orders")
    # one after another: 3 first, then 1, then 2 (an address lower than the lead's joins later)
    n = Net((1, 2, 3), seed=11, start=False)
    n.macs[3].start()
    n.air.start()
    n.run(2.0)
    n.macs[1].start()
    n.run(1.0)
    n.macs[2].start()
    ok = n.wait(lambda: n.one_network(), 10)
    check("3, then 1, then 2: one network", ok, f"{n.roles()} after {n.air.time:.1f} s")
    check("the station first on the air leads (3; later lower addresses join it)", n.td(3).role == "lead")
    n.stop()
    # all at once: the lowest address listens shortest and leads
    n = Net((1, 2, 3, 4), seed=12)
    ok = n.wait(lambda: n.one_network(), 10)
    check("4 stations started together: one network", ok, f"{n.roles()} after {n.air.time:.1f} s")
    check("... led by station 1", n.td(1).role == "lead")
    m = n.macs[4].send(1, b"hello after start" * 10)
    check("and a message goes through", n.wait(lambda: delivered(m), 10))
    n.stop()
    # two stations far apart start alone, then come into range: the networks merge (lower address)
    n = Net((2, 3), seed=13)
    n.air.set_link(2, 3, cut=True)
    n.run(3.0)
    two = n.td(2).role == "lead" and n.td(3).role == "lead"
    n.air.set_link(2, 3, cut=False)
    ok = n.wait(lambda: n.one_network(), 10)
    check("two lone stations meet: one network led by the lower address", two and ok and n.td(2).role == "lead",
          f"{n.roles()}")
    n.stop()


# ============================================================================ 2
def t2_join_leave_mid_transfer():
    say("2. join and leave during a transfer")
    n = Net((1, 2, 3, 4), seed=21, start=False)
    for a in (1, 2, 3):
        n.macs[a].start()
    n.air.start()
    n.wait(lambda: n.one_network((1, 2, 3)), 10)
    n.steady()
    data = os.urandom(150_000 if QUICK else 300_000)
    m = n.macs[2].send(3, data, PRIO_BULK)
    n.run(2.0)
    n.macs[4].start()                                 # 4 joins in the middle
    ok_join = n.wait(lambda: n.td(4).role == "member", 10)
    n.run(1.0)
    n.macs[1].stop()                                  # the lead leaves in the middle
    n.air.set_link(1, 2, cut=True); n.air.set_link(1, 3, cut=True); n.air.set_link(1, 4, cut=True)
    ok = n.wait(lambda: done(m), 120)
    check("station 4 joins during the transfer", ok_join)
    check("the transfer survives a join and the lead leaving", ok and delivered(m),
          f"{m.state.value}, {goodput(m):.0f} kb/s, {m.tx_frames}/{len(m.frags)} frames sent")
    check("its data arrived intact", any(d == data for s, d, _ in n.inbox[3]))
    check("no burst missed its slot", n.late() == 0, f"late {n.late()}")
    no_overlap(n)
    n.stop()


# ============================================================================ 3
def t3_lead_leaves():
    say("3. the lead leaves, then comes back")
    n = Net((1, 2, 3), seed=31)
    n.wait(lambda: n.one_network(), 10)
    n.steady()
    data = os.urandom(100_000)
    m = n.macs[2].send(3, data, PRIO_BULK)
    n.run(1.5)
    t_off = n.air.time
    n.macs[1].stop()                                  # the lead's app is closed
    ok = n.wait(lambda: n.td(2).role == "lead", 5)
    check("its successor (station 2) takes over", ok, f"after {n.air.time - t_off:.2f} s, {n.roles()}")
    ok = n.wait(lambda: done(m), 60)
    check("the transfer goes on", delivered(m), f"{m.state.value}, {goodput(m):.0f} kb/s")
    check("... and only the successor took the lead (station 3 followed it)",
          n.td(3).stats.get("took_over", 0) == 0 and n.td(3).role == "member", f"{n.roles()}")
    n.restart(1)                                      # the lead's app is started again
    ok = n.wait(lambda: n.one_network(), 15)
    check("station 1 comes back and the network is one again", ok, f"{n.roles()}")
    m2 = n.macs[1].send(3, b"back again" * 30)
    check("and it can send", n.wait(lambda: delivered(m2), 10))
    no_overlap(n)
    n.stop()


# ============================================================================ 4
def t4_simultaneous_join_and_duplicate():
    say("4. two newcomers at once; a duplicate address")
    n = Net((1, 2, 3), seed=41, start=False)
    n.macs[1].start()
    n.air.start()
    n.run(2.0)
    t0 = n.air.time
    n.macs[2].start()
    n.macs[3].start()
    ok = n.wait(lambda: n.one_network(), 15)
    check("two stations started at the same moment both get a slot", ok, f"after {n.air.time - t0:.1f} s, {n.roles()}")
    n.stop()
    # two radios both set to address 2
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=25, seed=42, speed=1000)
    r1, r2a = air.add_node(1), air.add_node(2)
    r2b = air.add_node(2)                             # a second radio configured with the same address
    macs = [LinkLayer(r, r.addr, mode="tdcdma") for r in (r1, r2a, r2b)]
    for m in macs:
        m.start()
    air.start()
    t0 = air.time
    while air.time - t0 < 12 and not (macs[1].tdma.dup_warn or macs[2].tdma.dup_warn):
        time.sleep(0.05)
    warn = macs[1].tdma.dup_warn or macs[2].tdma.dup_warn
    check("two stations with the same address are told so", bool(warn), warn or "no warning")
    for m in macs:
        m.stop()
    air.stop()


# ============================================================================ 5
def t5_drift():
    minutes = 2 if QUICK else 10
    say(f"5. clock drift +/-25 ppm (50 ppm between stations) for {minutes} minutes")
    n = Net((1, 2, 3), seed=51, ppm={1: 25.0, 2: -25.0, 3: 10.0})
    n.wait(lambda: n.one_network(), 10)
    n.steady()
    d0 = {a: n.td(a).D for a in n.macs}
    sent, lost = [], 0
    t_end = n.air.time + 60 * minutes
    k = 0
    while n.air.time < t_end:
        a, b = random.Random(k).sample([1, 2, 3], 2)
        sent.append(n.macs[a].send(b, os.urandom(2500), PRIO_NORMAL))
        sent[-1].src_ = a
        k += 1
        n.run(2.0)
    n.wait(lambda: done(*sent), 30)
    ok = sum(m.state.value == "delivered" for m in sent)
    bad = [f"#{k} {m.src_}->{m.dst} at {m.created:.1f}: {m.state.value}, {len(m.acked)}/{len(m.frags)} acked, {m.tx_frames} frames sent, "
           f"retries {m.retries}, {m.mstate}" for k, m in enumerate(sent) if m.state.value != "delivered"]
    check("every message delivered", ok == len(sent), f"{ok}/{len(sent)}" + (f"; not delivered: {bad[:3]}" if bad else ""))
    d1 = {a: n.td(a).D for a in n.macs}
    check("the TX->RX offsets stayed put", all(abs(d1[a] - d0[a]) <= 2 for a in d0), f"{[d1[a] - d0[a] for a in d0]}")
    drift = {a: round(n.td(a).tl.eps * 1e6, 1) for a in (2, 3)}
    check("each follower knows its clock against the lead's", abs(drift[2] + 50) < 5 and abs(drift[3] + 15) < 5,
          f"{drift} ppm (expected -50, -15)")
    st = {a: dict(n.td(a).stats) for a in n.macs}
    check("still one network, no burst late", n.one_network() and n.late() == 0,
          f"late {n.late()}, slots missed {[s.get('slots_missed', 0) for s in st.values()]}, recalibrations "
          f"{[s.get('recalibrations', 0) for s in st.values()]}")
    no_overlap(n)
    n.stop()


# ============================================================================ 6
def t6_underruns():
    say("6. TX underruns and lost RX buffers during a transfer")
    n = Net((1, 2, 3), seed=61)
    n.wait(lambda: n.one_network(), 10)
    data = os.urandom(120_000)
    m = n.macs[2].send(3, data, PRIO_BULK)
    m2 = n.macs[3].send(1, os.urandom(60_000), PRIO_BULK)
    faults = [(1.0, "tx", 2, 6000), (2.5, "rx", 3, 4000), (4.0, "tx", 1, 65536), (5.5, "rx", 2, 20000),
              (7.0, "rxk", 1, 30000), (8.5, "tx", 3, 1500)]
    t0 = n.air.time
    for at, kind, who, k in faults:
        while n.air.time - t0 < at:
            time.sleep(0.02)
        if kind == "tx":
            n.air.inject_tx_gap(who, k)
        else:
            n.air.inject_rx_drop(who, k, known=(kind == "rxk"))
    ok = n.wait(lambda: done(m, m2), 90)
    check("both transfers complete through 6 timing slips", delivered(m, m2),
          f"{m.state.value} {goodput(m):.0f} kb/s, {m2.state.value} {goodput(m2):.0f} kb/s")
    jumps = {a: n.td(a).d_jumps for a in n.macs}
    check("each slip that moved a TX->RX offset was measured again", jumps[1] >= 1 and jumps[2] >= 2 and jumps[3] >= 2,
          f"D jumps {jumps}")
    check("still one network", n.one_network(), f"{n.roles()}")
    n.stop()
    # a station that can't hear its own transmitter (and guesses D 1500 samples wrong)
    n = Net((1, 2, 3), seed=62, start=False)
    n.air.set_link(3, 3, cut=True)
    r3 = n.radios[3]
    r3.tx_rx_guess = lambda: r3.rx_count() - r3.tx_count() + 13 - 1500
    for m in n.macs.values():
        m.start()
    n.air.start()
    n.wait(lambda: n.one_network(), 20)
    n.run(8.0)
    t = n.td(3)
    errs = {a: e for a, (e, _) in t.tfb_about_me.items()}
    # (what is left is the simulator's random link delays, up to 100 us each: inside the half guard)
    check("a station deaf to itself steers its timing by the others' reports",
          t.d_source == "peers" and errs and all(abs(e) < t.gh for e in errs.values()), f"their reports {errs} samples")
    m = n.macs[3].send(1, os.urandom(40_000), PRIO_BULK)
    check("... and delivers", n.wait(lambda: delivered(m), 60), f"{goodput(m):.0f} kb/s")
    n.stop()


# ============================================================================ 7
def t7_four_senders():
    say("7. 4 stations all sending at once: TD-CDMA vs spread ALOHA")
    size = 60_000 if QUICK else 100_000
    out = {}
    # ideal: a station hears itself only 3 dB above the others (so it can receive while it sends);
    # as on the ANTSDRs: its own transmitter 25 dB above the others, ALOHA with its hardware pauses
    for setting, leak, hw in (("ideal radios", 3.0, False), ("own TX 25 dB above the others", 25.0, True)):
        for mode in ("tdcdma", "aloha"):
            n = Net((1, 2, 3, 4), seed=71, mode=mode, self_leak=leak, hw=hw)
            if mode == "tdcdma":
                n.wait(lambda: n.one_network(), 10)
                n.steady()
            else:
                n.run(2.0)
            t0 = n.air.time
            msgs = [n.macs[a].send(a % 4 + 1, os.urandom(size), PRIO_BULK) for a in (1, 2, 3, 4)]
            n.wait(lambda: done(*msgs), 300)
            dt = max((m.done_time or n.air.time) for m in msgs) - t0
            out[(setting, mode)] = dict(ok=sum(delivered(m) for m in msgs), total=4 * size * 8 / dt / 1e3,
                                        each=[round(goodput(m)) for m in msgs], t=dt,
                                        retx=sum(x.stats.get("retransmissions", 0) for x in n.macs.values()))
            if mode == "tdcdma":
                no_overlap(n, f"{setting}, tdcdma: no two stations ever on the air at once")
            n.stop()
        for mode in ("tdcdma", "aloha"):
            r = out[(setting, mode)]
            check(f"{setting}, {mode}: all 4 transfers delivered", r["ok"] == 4,
                  f"together {r['total']:.0f} kb/s in {r['t']:.1f} s, each {r['each']} kb/s, {r['retx']} retransmissions")
        t = out[(setting, "tdcdma")]
        fair = min(t["each"]) / max(1, max(t["each"]))
        # (60 KB in --quick mode is over in a few seconds: a slot grows one plan window (1 s) after its
        # owner asks, so whoever asked first has a head start the short transfers can't even out)
        check(f"{setting}: TD-CDMA shares the air fairly", fair > (0.45 if QUICK else 0.6), f"slowest/fastest {fair:.2f}")
        say(f"    ({setting}: TD-CDMA {t['total']:.0f} kb/s vs ALOHA {out[(setting, 'aloha')]['total']:.0f} kb/s together)")


# ============================================================================ 8
def t8_near_far():
    say("8. near-far without power control: TD-CDMA vs spread ALOHA")
    out = {}
    for mode in ("tdcdma", "aloha"):
        n = Net((1, 2, 3), seed=81, mode=mode, pc=False, ebn0=18)
        # 1 and 3 side by side (+20 dB), 2 far from both (-6 dB)
        n.air.set_link(1, 3, 20.0)
        n.air.set_link(1, 2, -6.0)
        n.air.set_link(2, 3, -6.0)
        if mode == "tdcdma":
            n.wait(lambda: n.one_network(), 15)
            n.steady()
        else:
            n.run(2.0)
        t0 = n.air.time
        near = n.macs[1].send(3, os.urandom(80_000), PRIO_BULK)       # the strong pair talks ...
        far = n.macs[2].send(3, os.urandom(30_000), PRIO_BULK)        # ... while the far one sends too
        n.wait(lambda: done(near, far), 120)
        out[mode] = dict(near=near.state.value, far=far.state.value, g_far=goodput(far), g_near=goodput(near),
                         t=n.air.time - t0, retx=n.macs[2].stats.get("retransmissions", 0))
        if mode == "tdcdma":
            no_overlap(n, "tdcdma: no two stations ever on the air at once")
        n.stop()
    for mode, r in out.items():
        check(f"{mode}: both delivered", r["near"] == "delivered" and r["far"] == "delivered",
              f"far station {r['g_far']:.0f} kb/s ({r['retx']} retransmissions), near pair {r['g_near']:.0f} kb/s")


# ============================================================================ 9
def t9_relay_flip():
    say("9. relaying off and on again during a relayed transfer")
    n = Net((1, 2, 3), seed=91, relay=True)
    n.air.set_link(1, 3, cut=True)                    # 1 and 3 can't hear each other; 2 hears both
    ok = n.wait(lambda: n.one_network(), 20)
    check("one network although 3 can't hear the lead (it keeps time through 2)", ok, f"{n.roles()}")
    n.run(1.0)
    n.steady()
    data = os.urandom(80_000)
    m = n.macs[1].send(3, data, PRIO_BULK)
    n.run(4.0)
    via = n.td(1).routes.get(3, {}).get("relay")
    part = len(m.acked) / len(m.frags)
    check("messages to 3 go through 2", via == 2, f"route {n.td(1).routes}, {part:.0%} delivered so far")
    n.macs[2].set_relay(False)
    n.run(3.0)
    stall = len(m.acked) / len(m.frags)
    n.run(3.0)
    check("relaying off at 2: the transfer waits", len(m.acked) / len(m.frags) - stall < 0.05 and m.state.value == "sending",
          f"{stall:.0%} -> {len(m.acked) / len(m.frags):.0%}")
    n.macs[2].set_relay(True)
    ok = n.wait(lambda: done(m), 90)
    check("relaying on again: it completes", delivered(m) and any(d == data for s, d, _ in n.inbox[3]),
          f"{m.state.value}, {goodput(m):.0f} kb/s overall")
    check("... and the network stayed one throughout (3 keeps time through 2 with relaying off too)",
          n.one_network(), f"{n.roles()}")
    no_overlap(n)
    n.stop()
    # a fragment its sender repeats (its ACK timer is shorter than the relay's slot takes to grow)
    # while the first copy still waits at the relay is passed on once
    from wavelink.mac import FWD, HDR, T_FWD
    td2 = n.td(2)
    td2.fwd_q[3].clear()
    body = HDR.pack(T_FWD, 0, 3, 1, 0x1234) + FWD.pack(2, 7, 9) + b"x" * 50
    td2.on_forward(body, 3, 1, 0x1234)
    td2.on_forward(body[:1] + bytes([body[1] | 0x40]) + body[2:], 3, 1, 0x1234)   # (another power backoff)
    td2.on_forward(HDR.pack(T_FWD, 0, 3, 1, 0x1234) + FWD.pack(2, 8, 9) + b"y" * 50, 3, 1, 0x1234)
    check("a repeated fragment still waiting at the relay is passed on once", len(td2.fwd_q[3]) == 2,
          f"queue {len(td2.fwd_q[3])}, dropped {td2.stats.get('relay_dup_dropped', 0)}")


# ============================================================================ node-level helpers
class Stations:
    """N app stations (Node: chat + game center) on one simulated air."""

    def __init__(self, ids=(1, 2, 3), ebn0=25.0, seed=1, relay=True, key=None, cut=()):
        from wavelink.node import Node
        names = {1: "Alice", 2: "Bob", 3: "Carol", 4: "Dave"}
        self.cfg = PhyConfig.from_profile("fast")
        self.air = SimAir(self.cfg, ebn0_db=ebn0, seed=seed, speed=1000)
        self.tmp = tempfile.mkdtemp(prefix="edge-")
        self.nodes = {}
        for a in ids:
            r = self.air.add_node(a)
            self.nodes[a] = Node(r, a, names[a], os.path.join(self.tmp, f"n{a}"), key, beacon_period=2.0,
                                 mode="sim", log=lambda *x: None, relay=relay)
        for a, b in cut:
            self.air.set_link(a, b, cut=True)
        self.check_from = None
        for nd in self.nodes.values():
            nd.start()

    def steady(self):
        self.check_from = self.air.time

    def overlaps(self):
        return [o for o in self.air.overlaps if self.check_from is not None and o[0] >= self.check_from]

    def run(self, t):
        t0 = self.air.time
        while self.air.time - t0 < t:
            time.sleep(0.02)

    def wait(self, cond, t):
        t0 = self.air.time
        while not cond():
            if self.air.time - t0 > t:
                return False
            time.sleep(0.05)
        return True

    def online(self, a, b):
        return bool(self.nodes[a].contacts.get(b, {}).get("online"))

    def isolate(self, a, on=True):
        for b in self.nodes:
            if b != a:
                self.air.set_link(a, b, cut=on)

    def stop(self):
        for nd in self.nodes.values():
            nd.stop()
        self.air.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ============================================================================ 10
def t10_group_out_of_range():
    say("10. a group message with a member out of range, then leaving")
    st = Stations((1, 2, 3), seed=101, relay=True, cut=[(1, 3)])
    ok = st.wait(lambda: st.online(1, 2) and st.online(1, 3), 25)
    st.run(1.0)
    st.steady()
    c3 = st.nodes[1].contacts.get(3, {})
    check("Carol (out of Alice's range) shows as reachable through Bob", ok and c3.get("via") == 2, f"{c3.get('via')}")
    a = st.nodes[1]
    rec = a.send("g:all", "text", b"hello group", text="hello group")
    ok = st.wait(lambda: a.messages[rec["uid"]]["recip"].get("2") == "delivered"
                 and a.messages[rec["uid"]]["recip"].get("3") == "delivered", 30)
    r = a.messages[rec["uid"]]
    check("one broadcast for Bob, a relayed copy for Carol; both tick", ok, f"{r['recip']} via {r.get('via')}")
    got3 = [m for m in st.nodes[3].messages.values() if m.get("text") == "hello group"]
    check("Carol files it in the group chat, marked as relayed", got3 and got3[0]["chat"] == "g:all" and got3[0].get("via") == 2)
    # Carol leaves for good: a big group message goes on for Bob, Carol is given up
    st.isolate(3)
    st.run(4.0)
    rec2 = a.send("g:all", "file", os.urandom(30_000), name="notes.bin", mime="application/octet-stream")
    ok = st.wait(lambda: a.messages[rec2["uid"]]["recip"].get("2") == "delivered", 60)
    r2 = a.messages[rec2["uid"]]
    check("the next group file reaches Bob", ok, f"{r2['recip']}")
    ok = st.wait(lambda: a.messages[rec2["uid"]]["recip"].get("3") in ("failed",) or "3" not in a.messages[rec2["uid"]]["recip"], 90)
    check("... and Carol, gone, is given up instead of holding it", ok, f"{a.messages[rec2['uid']]['recip']}")
    ov = st.overlaps()
    check("no two stations ever on the air at once", not ov, f"{len(ov)} times, first {ov[:3]}" if ov else "")
    st.stop()


# ============================================================================ 11
def t11_race_four():
    say("11. race with 4 players: one leaves and comes back, one leaves for good")
    import threading
    import math
    import wavelink.games.hub as H
    old = H.AWAY_GRACE
    H.AWAY_GRACE = 12.0                               # (shorter than 60 s, to keep the test short)
    st = Stations((1, 2, 3, 4), seed=111)
    try:
        ok = st.wait(lambda: all(st.online(a, b) for a in st.nodes for b in st.nodes if a != b), 30)
        check("4 stations up", ok)
        host = st.nodes[1]
        rid = host.hub.create("race", [2, 3, 4], {"laps": 5})
        st.wait(lambda: all(rid in nd.hub.rooms for nd in st.nodes.values()), 15)
        for a in (2, 3, 4):
            st.nodes[a].hub.answer(rid, True)
        ok = st.wait(lambda: all(host.hub.rooms[rid]["sync"].get(a) for a in (2, 3, 4)), 40)
        check("all joined and clock-synced", ok)
        host.hub.start_game(rid)
        stop = threading.Event()

        def driver(nd):
            while not stop.is_set() and nd.addr not in (nd.hub.rooms.get(rid) or {}).get("players", []):
                time.sleep(0.05)                      # (the start message is still on its way)
            if stop.is_set():
                return
            seat = nd.hub.rooms[rid]["players"].index(nd.addr)
            while not stop.is_set():
                tg = nd.hub.host_now(rid) - (nd.hub.rooms[rid].get("t0") or 0)
                a = 0.3 * tg + seat
                s = dict(t=tg * 1000, x=1500 + 900 * math.cos(a), y=1200 + 700 * math.sin(a), h=a + math.pi / 2,
                         vx=-270 * math.sin(a), vy=210 * math.cos(a), w=0.3, d=max(0.0, 250 * tg), fl=0, it=0)
                nd.hub.race_post(rid, s, [])
                time.sleep(0.05)
        ths = [threading.Thread(target=driver, args=(nd,), daemon=True) for nd in st.nodes.values()]
        for th in ths:
            th.start()
        st.run(8.0)
        st.isolate(3)                                 # Carol drives into a tunnel ...
        ok = st.wait(lambda: host.hub.rooms[rid]["st"].get(3) == "away", 10)
        check("Carol off the air: 'away' (a ghost on the others' screens)", ok)
        n_before = host.hub.race_stats(rid)["rx"].get(1, {}).get("n", 0)
        st.run(4.0)
        n_after = host.hub.race_stats(rid)["rx"].get(1, {}).get("n", 0)
        check("the race goes on meanwhile (Bob's car still heard)", n_after > n_before + 10, f"{n_before} -> {n_after}")
        st.isolate(3, False)                          # ... and out again
        ok = st.wait(lambda: host.hub.rooms[rid]["st"].get(3) == "joined", 15)
        check("Carol back within the grace period: racing again", ok, f"{host.hub.rooms[rid]['st']}")
        st.isolate(4)                                 # Dave leaves for good
        ok = st.wait(lambda: host.hub.rooms[rid]["st"].get(4) == "gone", 40)
        check("Dave away longer than the grace period: dropped", ok, f"{host.hub.rooms[rid]['st']}")
        others = [st.nodes[a].hub.rooms[rid]["st"].get(4) for a in (2, 3)]
        check("the others see him dropped too", st.wait(lambda: all(st.nodes[a].hub.rooms[rid]["st"].get(4) == "gone" for a in (2, 3)), 15),
              f"{others}")
        stop.set()
    finally:
        H.AWAY_GRACE = old
        st.stop()


# ============================================================================ 12
def t12_uno():
    say("12. UNO: a player leaves and comes back; the host leaves; back after the grace period")
    import wavelink.games.hub as H
    from wavelink.games.uno import CARDS, WILD, WILD4
    old = H.AWAY_GRACE
    H.AWAY_GRACE = 15.0                               # (shorter than 60 s, to keep the test short)
    st = Stations((1, 2, 3), seed=121)
    views = collections.defaultdict(dict)
    try:
        st.wait(lambda: all(st.online(a, b) for a in st.nodes for b in st.nodes if a != b), 30)
        host = st.nodes[1]
        rid = host.hub.create("uno", [2, 3])
        st.wait(lambda: all(rid in nd.hub.rooms for nd in st.nodes.values()), 15)
        for a in (2, 3):
            st.nodes[a].hub.answer(rid, True)
        st.wait(lambda: all(host.hub.rooms[rid]["st"].get(a) == "joined" for a in (2, 3)), 15)
        for nd in st.nodes.values():
            nd.hub.listeners.append(lambda ev, d, a=nd.addr: ev == "uno" and d.get("r") == rid and views[a].update(v=d))
        host.hub.start_game(rid)
        rng = random.Random(3)
        acted = {}

        def play(secs, who=(1, 2, 3)):
            t_end = st.air.time + secs
            while st.air.time < t_end:
                time.sleep(0.05)
                for a in who:
                    v = views[a].get("v")
                    if not v or v["phase"] == "over" or v["turn"] != v["seat"] or acted.get(a) == v["v"] or v.get("pause"):
                        continue
                    acted[a] = v["v"]
                    hub = st.nodes[a].hub
                    try:
                        if v["phase"] == "color":
                            hub.uno_action(rid, "color", col=rng.randrange(4))
                        elif v["phase"] == "drawn":
                            hub.uno_action(rid, "keep")
                        elif v["legal"] and len(v["hand"]) > 2:   # (nobody goes out: the round lasts the test)
                            c = v["legal"][0]
                            hub.uno_action(rid, "play", c=c, col=rng.randrange(4) if CARDS[c][1] in (WILD, WILD4) else None)
                        else:
                            hub.uno_action(rid, "draw")
                    except ValueError:
                        pass
        st.wait(lambda: all(views[a].get("v") for a in (1, 2, 3)), 15)
        play(6.0)
        # --- Carol leaves and comes back
        st.isolate(3)
        ok = st.wait(lambda: (views[1].get("v") or {}).get("pause") is not None, 12)
        check("Carol off the air: the game pauses for everybody", ok, f"{(views[1].get('v') or {}).get('pause')}")
        v_paused = views[1]["v"]["v"]
        play(3.0, who=(1, 2))
        check("... no moves while paused", views[1]["v"]["v"] == v_paused or views[1]["v"].get("pause") is not None)
        st.isolate(3, False)
        ok = st.wait(lambda: (views[1].get("v") or {}).get("pause") is None and host.hub.rooms[rid]["st"].get(3) == "joined", 20)
        check("Carol back: the game goes on", ok)
        play(4.0)
        hand3 = len(views[3]["v"]["hand"])
        check("... and Carol still has her hand", hand3 > 0, f"{hand3} cards")
        # --- the host leaves: Bob takes over the table
        st.isolate(1)
        ok = st.wait(lambda: st.nodes[2].hub.rooms[rid]["h"] == 2 and st.nodes[3].hub.rooms[rid]["h"] == 2, 20)
        check("the host off the air: Bob hosts the game now", ok,
              f"host at Bob {st.nodes[2].hub.rooms[rid]['h']}, at Carol {st.nodes[3].hub.rooms[rid]['h']}")
        hand_a = len(st.nodes[2].hub.rt[rid]["uno"].hands[0]) if st.nodes[2].hub.rt[rid].get("uno") else -1
        check("... with Alice's hand kept for her", hand_a > 0, f"{hand_a} cards")
        st.isolate(1, False)
        ok = st.wait(lambda: st.nodes[1].hub.rooms[rid]["h"] == 2 and st.nodes[2].hub.rooms[rid]["st"].get(1) == "joined", 25)
        check("Alice back: she plays on as a player under Bob", ok, f"{st.nodes[1].hub.rooms[rid]['h']}, {st.nodes[2].hub.rooms[rid]['st']}")
        play(4.0)
        # --- Carol away longer than the grace period
        st.isolate(3)
        ok = st.wait(lambda: st.nodes[2].hub.rooms[rid]["st"].get(3) == "gone", H.AWAY_GRACE + 15)
        check("Carol away too long: dropped, the game goes on", ok, f"{st.nodes[2].hub.rooms[rid]['st']}")
        st.isolate(3, False)
        ok = st.wait(lambda: st.nodes[3].hub.rooms[rid]["state"] == "closed", 25)
        r3 = st.nodes[3].hub.rooms[rid]
        check("Carol back afterwards: she is told the game went on without her", ok,
              f"{r3.get('why', '')} (state {r3['state']}, host {r3['h']} v{r3.get('hv', 0)}, {r3['st']})")
        play(6.0, who=(1, 2))
        v = views[1].get("v") or {}
        r1, r2 = st.nodes[1].hub.rooms[rid], st.nodes[2].hub.rooms[rid]
        check("the game still runs between Alice and Bob", v.get("phase") in ("play", "drawn", "color", "over") and not v.get("pause"),
              f"Alice's view: phase {v.get('phase')}, pause {v.get('pause')}, v{v.get('v')}; host at Alice {r1['h']} "
              f"v{r1.get('hv', 0)}, at Bob {r2['h']} v{r2.get('hv', 0)}, Bob's room {r2['state']} {r2['st']}")
    finally:
        H.AWAY_GRACE = old
        st.stop()


# ============================================================================ 13
def t13_low_snr():
    say("13. low SNR (5 dB) and bursty interference")
    n = Net((1, 2, 3), seed=131, ebn0=5.0)
    ok = n.wait(lambda: n.one_network(), 20)
    check("the network forms at 5 dB (Es/N0 of a 0 dB link)", ok, f"{n.roles()}")
    n.run(1.0)
    n.steady()
    msgs = [n.macs[1].send(2, os.urandom(8000)), n.macs[3].send(1, os.urandom(8000)), n.macs[2].send(3, b"short" * 20)]
    ok = n.wait(lambda: done(*msgs), 120)
    check("messages get through at 5 dB", delivered(*msgs), f"{[m.state.value for m in msgs]}, "
          f"{sum(m.stats.get('retransmissions', 0) for m in n.macs.values())} retransmissions")
    tk = sum(n.td(a).stats.get("took_over", 0) for a in n.macs)
    check("... no station took the lead over while the lead was there (headers lost at 5 dB)", tk == 0,
          f"{tk} takeovers, {n.roles()}")
    no_overlap(n, "... and no two stations ever on the air at once")
    n.stop()
    n = Net((1, 2, 3), seed=132, ebn0=22.0)
    n.wait(lambda: n.one_network(), 15)
    n.steady()
    n.air.interference = dict(level_db=-3.0, duty=0.25, burst_ms=30)
    m = n.macs[2].send(3, os.urandom(60_000), PRIO_BULK)
    m2 = n.macs[1].send(2, os.urandom(20_000), PRIO_BULK)
    ok = n.wait(lambda: done(m, m2), 120)
    check("bursty interference (25 % of the time, 3 dB below the signal): delivered", delivered(m, m2),
          f"{goodput(m):.0f} kb/s and {goodput(m2):.0f} kb/s, {sum(x.stats.get('retransmissions', 0) for x in n.macs.values())} retransmissions")
    check("the network stays one", n.one_network(), f"{n.roles()}")
    no_overlap(n)
    n.stop()


# ============================================================================ 14
def t14_missed_announcements():
    say("14. a station misses every announcement of a slot plan change")
    from wavelink.tdma import _sections, FIX, SEC_NEXT
    n = Net((1, 2, 3, 4), seed=141)
    n.wait(lambda: n.one_network(), 15)
    n.run(1.0)
    n.steady()
    td4 = n.td(4)
    orig = td4.on_header

    def deaf_to_news(src, msg_id, rest, f):          # every header that announces a new plan is lost
        if src != 4 and any(tag == SEC_NEXT for tag, _ in _sections(rest[FIX.size:])):
            td4.stats["test_dropped_next"] += 1
            return
        return orig(src, msg_id, rest, f)
    td4.on_header = deaf_to_news
    held0 = td4.stats.get("slots_held", 0)
    pc0 = n.td(1).stats.get("plan_changes", 0)
    m4 = n.macs[4].send(1, os.urandom(40_000), PRIO_BULK)
    msgs = []
    for _ in range(2 if QUICK else 3):                # slots grow and shrink: one plan change after another
        m = n.macs[2].send(3, os.urandom(25_000), PRIO_BULK)
        n.wait(lambda: done(m), 60)
        msgs.append(m)
        n.run(2.5)
    n.wait(lambda: done(m4), 60)
    st = td4.stats
    check("station 4 heard none of the announcements", st.get("test_dropped_next", 0) > 0,
          f"{st.get('test_dropped_next', 0)} headers dropped, {n.td(1).stats.get('plan_changes', 0) - pc0} plan changes")
    # (it holds a slot only when the plan in force has not reached it by then: with plan changes 1 s
    # ahead, station 4's slot - last in the cycle - usually comes after the lead's header that brings it)
    no_overlap(n, f"... and never sent by a stale plan ({st.get('slots_held', 0) - held0} slots held until it heard "
                  f"the plan in force): no two stations ever on the air at once")
    tk = sum(n.td(a).stats.get("took_over", 0) for a in n.macs)
    check("everything delivered, one network, nobody took the lead over", delivered(m4, *msgs) and n.one_network()
          and tk == 0, f"{[x.state.value for x in [m4] + msgs]}, {n.roles()}, {tk} takeovers")
    n.stop()


# ============================================================================ 15
def t15_lead_unheard():
    say("15. a station stops hearing the lead (the others still do)")
    for relay in (True, False):
        tag = "relaying on" if relay else "relaying off"
        n = Net((1, 2, 3), seed=151, relay=relay)
        n.wait(lambda: n.one_network(), 15)
        n.run(1.0)
        n.steady()
        m = n.macs[3].send(2, os.urandom(60_000), PRIO_BULK)
        n.run(1.0)
        n.air.set_link(1, 3, cut=True, both=False)    # Carol stops hearing Alice (who still hears Carol)
        n.run(4.0)
        t3 = n.td(3)
        check(f"{tag}: Carol keeps time by the plan the others repeat, instead of taking the lead over",
              t3.role == "member" and t3.rsync and t3.stats.get("took_over", 0) == 0 and n.one_network(),
              f"role {t3.role}, plans repeated by {t3.rsync_ref}, {n.roles()}")
        n.air.set_link(1, 3, cut=False, both=False)
        n.wait(lambda: done(m), 60)
        n.run(1.0)
        check(f"{tag}: her transfer went on; then back to the lead's own headers",
              delivered(m) and not t3.rsync and n.one_network(), f"{m.state.value}, {goodput(m):.0f} kb/s")
        no_overlap(n, f"{tag}: no two stations ever on the air at once")
        n.stop()


# ============================================================================ 16
def t16_off_the_air():
    say("16. a station off the air for 1 s, then for 2.5 s")
    n = Net((1, 2, 3), seed=161)
    n.wait(lambda: n.one_network(), 15)
    n.run(1.0)
    n.steady()
    lead, t3 = n.td(1), n.td(3)

    def off(a, secs):
        for b in n.macs:
            if b != a:
                n.air.set_link(a, b, cut=True)
        n.run(secs)
        for b in n.macs:
            if b != a:
                n.air.set_link(a, b, cut=False)
    ev0 = lead.stats.get("members_evicted", 0)
    off(3, 1.0)
    n.run(1.0)
    check("off the air for 1 s: Carol keeps her slot and leads nothing of her own",
          lead.stats.get("members_evicted", 0) == ev0 and t3.stats.get("evicted", 0) == 0
          and t3.stats.get("took_over", 0) == 0 and n.one_network(), f"{n.roles()}")
    off(3, 2.5)
    s0 = t3.stats.get("slots_sent", 0)
    t_back = n.air.time
    ok = n.wait(lambda: t3.role == "member" and t3.stats.get("slots_sent", 0) > s0, 10)
    check("off for 2.5 s: her slot was freed, and she has one again soon after she is back",
          ok and lead.stats.get("members_evicted", 0) > ev0, f"{n.air.time - t_back:.2f} s after coming back")
    m = n.macs[3].send(1, b"back again" * 30)
    check("... and she can send", n.wait(lambda: delivered(m), 10))
    no_overlap(n)
    n.stop()


# ============================================================================ 17
def t17_hidden_relay_off():
    say("17. a hidden station with relaying off everywhere")
    n = Net((1, 2, 3), seed=171, relay=False)
    n.air.set_link(1, 3, cut=True)                   # Alice and Carol can't hear each other; Bob hears both
    ok = n.wait(lambda: n.one_network(), 25)
    check("one network: Carol keeps time by the plan Bob repeats (timing only: no messages passed on)", ok,
          f"{n.roles()}")
    n.run(1.0)
    n.steady()
    m = n.macs[3].send(2, os.urandom(30_000), PRIO_BULK)
    m2 = n.macs[1].send(2, os.urandom(30_000), PRIO_BULK)
    n.wait(lambda: done(m, m2), 60)
    check("Carol and Alice both send to Bob", delivered(m, m2), f"{goodput(m):.0f} and {goodput(m2):.0f} kb/s")
    no_overlap(n)
    n.stop()


TESTS = {1: t1_start_orders, 2: t2_join_leave_mid_transfer, 3: t3_lead_leaves, 4: t4_simultaneous_join_and_duplicate,
         5: t5_drift, 6: t6_underruns, 7: t7_four_senders, 8: t8_near_far, 9: t9_relay_flip, 10: t10_group_out_of_range,
         11: t11_race_four, 12: t12_uno, 13: t13_low_snr, 14: t14_missed_announcements, 15: t15_lead_unheard,
         16: t16_off_the_air, 17: t17_hidden_relay_off}


def main():
    pick = [int(x) for x in sys.argv[1:] if x.isdigit()] or list(TESTS)
    t0 = time.time()
    for k in pick:
        t = time.time()
        try:
            TESTS[k]()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"test {k} ran", False, repr(e))
        say(f"    ({time.time() - t:.0f} s)")
    bad = [n for n, ok in RESULTS if not ok]
    say(f"edge cases: {'PASS' if not bad else 'FAIL'} ({len(RESULTS) - len(bad)}/{len(RESULTS)} checks, "
        f"{time.time() - t0:.0f} s)")
    for b in bad:
        say("   failed:", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
