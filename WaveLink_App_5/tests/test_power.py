"""Transmit power control (the near-far problem).

    python3 -m tests.test_power            # all three parts (the last one takes a few minutes)
    python3 -m tests.test_power --quick    # parts 1 and 2 only

1. The link layer: each frame carries its power backoff; a receiver adds it to what it
   measures (the path at full power) and reports that back in ACKs and beacons; the sender backs
   off to the target, turns up at once and down only in 2 dB steps, goes back to full power when
   reports stop, sends broadcasts at what the weakest station needs, beacons at full power.
2. The GNU Radio flowgraph's own blocks (run here with stand-ins for gnuradio.gr and pmt, so no GNU
   Radio is needed): MAC -> PHY puts the power in the metadata, the LDPC Encoder and the Framer
   keep it, 'PDU to Tagged Stream' turns it into a tag (imitated), and the Combiner scales the burst.
3. Near-far on the simulated air: Alice, Carol and Dave sit together (24 dB stronger paths), Bob is
   across the room.  While Alice sends a long file to Carol, Bob sends 40 KB to Dave (at a fixed
   16-QAM rate 1/2).  At full power Alice drowns Bob at Dave; with power control Bob gets through.
   (Alice backs off somewhat less than 46 - 17 = 29 dB: Bob's own signal raises the noise at
   Carol, Carol's reports are SINR, so they ask Alice for a few dB more power.)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from dataclasses import dataclass

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wavelink import mac as M                      # noqa: E402
from wavelink.config import PhyConfig, BROADCAST_ADDR   # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'ok ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")


# ============================================================================ 1. link layer
class FakeRadio:
    supports_tx_gain = True
    supports_urgent = True
    timed_tx = False

    def __init__(self):
        self.cfg = PhyConfig.from_profile("fast")
        self.sent = []                       # (code, payload, kw)
        self.t = 0.0
        self.on_frame = None

    def now(self):
        return self.t

    def send(self, code, payload, **kw):
        self.sent.append((code, payload, kw))
        return self.t + 0.01

    def airtime(self, n, mod="qpsk-1/2"):
        return n * 8 / 64000

    def tx_backlog(self, code):
        return 0.0

    def rx_busy(self):
        return False


@dataclass
class Frame:
    payload: bytes
    esn0_db: float
    snr_db: float = 0.0
    cfo_hz: float = 0.0
    mcs: str = "qpsk-1/2"


def deliver(src_mac, dst_mac, path_db, k=-1):
    """The k-th frame src sent, as dst hears it: Es/N0 = path at full power - the frame's backoff."""
    code, payload, kw = src_mac.radio.sent[k]
    es = path_db + kw.get("gain_db", 0.0)
    dst_mac._on_phy_frame(Frame(payload, es, es))
    return es


def part1():
    print("1. link layer")
    ra, rc = FakeRadio(), FakeRadio()
    alice, carol = M.LinkLayer(ra, 1), M.LinkLayer(rc, 3)
    check("target: 16-QAM 5/6 + 3 dB by default; uncoded 16-QAM + 3 dB with --fec off; QPSK 5/6 + 3 dB with --mod qpsk",
          alice.pc_target_db() == 17.0 and M.LinkLayer(FakeRadio(), 2, fec_policy="off").pc_target_db() == 25.5
          and M.LinkLayer(FakeRadio(), 2, mod_policy="qpsk").pc_target_db() == 10.0,
          f"{alice.pc_target_db()} / 25.5 / 10.0 dB")
    # Carol hears Alice's beacon at 40 dB (full power); her beacon reports it; Alice backs off
    alice.send_beacon(b"{}")
    deliver(alice, carol, 40.0)
    carol.send_beacon(b"{}")
    _, payload, kw = rc.sent[-1]
    deliver(carol, alice, 40.0)
    bo = alice.backoff_for(3)
    check("beacons go at full power and report how each station is heard", "gain_db" not in kw and bo == 23,
          f"Alice backs off {bo} dB (40 - 17)")
    # a data frame at that backoff: the receiver sees the backoff in the flags and adds it back
    alice.send(3, b"hello")
    alice.tick()
    es = deliver(alice, carol, 40.0)
    q = carol.link_quality[1]
    check("the frame carries its backoff; the receiver works out the path at full power",
          abs(es - 17.0) < 1e-9 and q["rx_pwr_db"] == -23 and abs(q["esn0_full"] - 40.0) < 0.01,
          f"heard at {es} dB, sent at {q['rx_pwr_db']} dB, path {q['esn0_full']} dB")
    # the ACK reports the full-power value (not the 17 dB it measured)
    deliver(carol, alice, 40.0)
    check("the ACK reports the path at full power", abs(alice.link_quality[3]["fb_full"] - 40.0) < 0.5,
          f"{alice.link_quality[3]['fb_full']:.1f} dB")
    check("the modulation is chosen for what the receiver will hear at that power",
          alice.mod_for(3) == "16qam-5/6", alice.mod_for(3))
    # hysteresis: 1 dB less needed changes nothing, 3 dB more needed turns up at once
    alice._note_report(3, 41.0, 1.0)
    a1 = alice.backoff_for(3)
    alice._note_report(3, 37.0, 1.0)
    a2 = alice.backoff_for(3)
    alice._note_report(3, 42.0, 1.0)
    a3 = alice.backoff_for(3)
    check("power goes up at once, down only in steps of 2 dB or more", (a1, a2, a3) == (23, 20, 25), f"{a1}, {a2}, {a3} dB")
    # broadcasts: the weakest station on the air decides
    alice.last_heard[2] = time.time()
    alice._note_report(2, 21.0, 1.0)                  # Bob is far away: 4 dB of backoff
    check("broadcasts at the power the weakest station needs", alice.backoff_for(BROADCAST_ADDR) == 4,
          f"{alice.backoff_for(BROADCAST_ADDR)} dB")
    alice.last_heard[4] = time.time()                 # Dave on the air, never reported: full power
    check("a station that hasn't reported yet: broadcasts at full power", alice.backoff_for(BROADCAST_ADDR) == 0)
    # stale reports: back to full power
    alice.link_quality[3]["fb_t"] -= M.PC_STALE_S + 1
    check("no report for a minute: full power again", alice.backoff_for(3) == 0)
    alice.link_quality[3]["fb_t"] = time.time()
    alice.backoff_for(3)
    alice.power_control = False
    check("power control off: full power, and the MCS uses the full-power report", alice.backoff_for(3) == 0
          and not alice.tx_bo and alice.mod_for(3) == "16qam")
    fr = FakeRadio()
    fr.supports_tx_gain = False
    check("a PHY that can't scale bursts: power control stays off", M.LinkLayer(fr, 1).pc_active is False)


# ============================================================================ 2. flowgraph blocks
def fake_gnuradio():
    pmt = types.ModuleType("pmt")
    pmt.intern = lambda s: ("sym", s)
    pmt.symbol_to_string = lambda s: s[1]
    pmt.from_long = int
    pmt.to_long = int
    pmt.from_double = float
    pmt.to_double = float
    pmt.make_dict = dict
    pmt.dict_add = lambda d, k, v: {**d, k: v}
    pmt.dict_ref = lambda d, k, default: d.get(k, default)
    pmt.cons = lambda a, b: (a, b)
    pmt.car = lambda p: p[0]
    pmt.cdr = lambda p: p[1]
    pmt.init_u8vector = lambda n, v: list(v)
    pmt.u8vector_elements = list
    pmt.eq = lambda a, b: a == b
    pmt.PMT_NIL = None
    pmt.is_null = lambda x: x is None

    class Block:
        def __init__(self, name=None, in_sig=None, out_sig=None):
            self.ports, self.handlers, self.tags, self.nread = {}, {}, {}, {}

        def message_port_register_out(self, p):
            self.ports[p] = []

        def message_port_register_in(self, p):
            pass

        def set_msg_handler(self, p, h):
            self.handlers[p] = h

        def message_port_pub(self, p, msg):
            for f in self.ports.get(p, []):
                f(msg)

        def set_output_multiple(self, n):
            pass

        def consume(self, i, n):
            self.nread[i] = self.nread.get(i, 0) + n

        def get_tags_in_window(self, i, a, b):
            base = self.nread.get(i, 0)
            return [t for t in self.tags.get(i, []) if base + a <= t.offset < base + b]

    gr = types.SimpleNamespace(sync_block=Block, basic_block=Block)
    gnuradio = types.ModuleType("gnuradio")
    gnuradio.gr = gr
    return pmt, gnuradio


def load_block(name, pmt, gnuradio, **kw):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    from make_grc import epy_source
    saved = {k: sys.modules.get(k) for k in ("pmt", "gnuradio")}
    sys.modules["pmt"], sys.modules["gnuradio"] = pmt, gnuradio
    try:
        ns = {"__file__": os.path.join(ROOT, "grc", f"epy_{name}.py"), "__name__": f"epy_{name}"}
        exec(compile(epy_source(name), f"epy_{name}", "exec"), ns)
        return ns["blk"](**kw)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def part2():
    print("2. the flowgraph's blocks carry each burst's power")
    from wavelink.grc_phy import GrcPhyRadio
    from wavelink.modem import SYMBOL_TABLE
    pmt, gnuradio = fake_gnuradio()
    mac_tx = load_block("mac_tx", pmt, gnuradio, node_id=9)
    enc = load_block("ldpc_encoder", pmt, gnuradio, max_payload=1024)
    framer = load_block("framer", pmt, gnuradio, gold_degree=5, preamble_bits=48, max_payload=1024)
    comb = load_block("combiner", pmt, gnuradio, node_id=9, codes=(0, 1, 2, 3, 4), chip_rate=2e6,
                      samp_rate=4e6, tx_buffer=131072)
    P = pmt.intern("pdus")
    mac_tx.ports[P].append(enc.handlers[P])
    enc.ports[P].append(framer.handlers[P])
    lanes = []

    @dataclass
    class Tag:
        offset: int
        key: object
        value: object

    def lane(msg):
        # pdu filter (code), PDU to Tagged Stream (metadata -> tags on the first item), Chunks to
        # Symbols, Repeat x31, Multiply by the code, Tagged Stream Multiply Length (tags follow)
        meta, vec = msg
        sym = SYMBOL_TABLE[np.asarray(vec, dtype=np.int64)]
        chips = (np.repeat(sym, 31) * np.tile(np.where(np.arange(31) % 3, 1, -1), sym.size)).astype(np.complex64)
        lanes.append((meta, chips))
    framer.ports[P].append(lane)
    sig = __import__("inspect").signature(mac_tx.submit).parameters
    rms = []
    for gain in (0.0, -12.0):
        lanes.clear()
        mac_tx.submit(os.urandom(200), 3, "qpsk-1/2", gain_db=gain)
        meta, chips = lanes[0]
        i = 3                                           # lane of code 3
        base = comb.nread.get(i, 0)
        comb.tags[i] = [Tag(base, pmt.intern(k[1]) if isinstance(k, tuple) else k, v) for k, v in
                        [(pmt.intern("packet_len"), chips.size)] + [(k, v) for k, v in meta.items()]]
        ins = [np.zeros(0, np.complex64)] * 5
        ins[i] = chips
        out = np.zeros(chips.size, np.complex64)
        comb.general_work(ins, [out])
        rms.append(float(np.sqrt(np.mean(np.abs(out) ** 2))))
    ratio = 20 * np.log10(rms[1] / rms[0])
    check("MAC -> PHY takes the burst's power; the Framer passes it on", "gain_db" in sig and meta.get(pmt.intern("pwr")) == -12.0,
          f"metadata pwr = {meta.get(pmt.intern('pwr'))}")
    check("the Combiner scales the burst by its 'pwr' tag", abs(ratio + 12.0) < 0.05, f"{ratio:.2f} dB")
    # the app sees that the flowgraph can do it (an older .grc without the argument: it can't)
    fake = GrcPhyRadio.__new__(GrcPhyRadio)
    fake.reg = {"mac_tx": mac_tx}
    old = types.SimpleNamespace(submit=lambda frame, code, mod: None)
    fake_old = GrcPhyRadio.__new__(GrcPhyRadio)
    fake_old.reg = {"mac_tx": old}
    check("the app turns power control on only with a flowgraph that supports it",
          fake.supports_tx_gain is True and fake_old.supports_tx_gain is False)


# ============================================================================ 3. near-far on the simulated air
def part3():
    print("3. near-far on the simulated air (4 stations; takes a few minutes)")
    from wavelink.node import Node
    from wavelink.radio_sim import SimAir
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=22, seed=7)
    tmp = tempfile.mkdtemp(prefix="pc-")
    nodes = [Node(air.add_node(i), i, n, os.path.join(tmp, f"n{i}"), None, beacon_period=5.0, log=lambda *a: None)
             for i, n in ((1, "Alice"), (2, "Bob"), (3, "Carol"), (4, "Dave"))]
    for a in (1, 3, 4):                                 # Alice, Carol, Dave together; Bob across the room
        for b in (1, 3, 4):
            if a != b:
                air.gain_db[(a, b)] = 24.0
        air.gain_db[(a, 2)] = air.gain_db[(2, a)] = 0.0
    A, B, C, D = nodes
    B.mac.mod_policy, B.mac.fec_policy = "16qam", "1/2"      # Bob at one fixed rate (needs 9.5 dB)
    for n in nodes:
        n.start()

    def wait(c, t):
        e = time.time() + t
        while time.time() < e:
            if c():
                return True
            time.sleep(0.05)
        return False

    def sleep_sim(s):
        t = air.time
        while air.time < t + s:
            time.sleep(0.05)

    try:
        assert wait(lambda: all(len([x for x in n.contacts.values() if x.get("online")]) == 3 for n in nodes), 60)
        res = {}
        for case in ("quiet", "off", "on"):
            for n in nodes:
                n.mac.power_control = case == "on"
            sleep_sim(10)                               # reports settle
            s0 = B.mac.stats["data_tx"]
            ma = None
            if case != "quiet":
                ma = A.mac.send(3, os.urandom(2_000_000))      # Alice -> Carol keeps the air busy
                sleep_sim(0.5)
            mb = B.mac.send(4, os.urandom(40_000))
            t0 = air.time
            while air.time < t0 + 15 and mb.state not in (M.MsgState.DELIVERED, M.MsgState.FAILED):
                time.sleep(0.05)
            sent = B.mac.stats["data_tx"] - s0
            got = len(mb.acked)
            res[case] = dict(done=mb.state == M.MsgState.DELIVERED, t=air.time - t0, ratio=got / max(1, sent),
                             bo=A.mac.tx_bo.get(3, 0), heard=C.mac.link_quality[1].get("esn0_db"))
            r = res[case]
            print(f"     {case:5s}: Bob's 40 KB to Dave {'delivered in %.1f s' % r['t'] if r['done'] else 'not delivered in 15 s'}, "
                  f"{got} of {len(mb.frags)} fragments for {sent} frames sent ({r['ratio']:.0%}); "
                  f"Alice -> Carol at {-r['bo']} dB" + (f", Carol hears her at {r['heard']} dB" if case == "on" else ""), flush=True)
            if not r["done"]:
                B.mac.cancel(4, mb.msg_id)
            if ma is not None:
                A.mac.cancel(3, ma.msg_id)
            sleep_sim(2)
        check("Alice quiet: Bob's file goes through", res["quiet"]["done"] and res["quiet"]["ratio"] > 0.85)
        check("Alice at full power next to Dave: Bob is drowned", not res["off"]["done"] and res["off"]["ratio"] < 0.3,
              f"{res['off']['ratio']:.0%} of frames get through")
        check("with power control Alice backs off and Bob gets through", res["on"]["done"] and res["on"]["ratio"] > 0.5
              and res["on"]["bo"] >= 15, f"{res['on']['ratio']:.0%} of frames, Alice at -{res['on']['bo']} dB")
    finally:
        for n in nodes:
            n.stop()


def main():
    part1()
    part2()
    if "--quick" not in sys.argv:
        part3()
    bad = [n for n, ok in RESULTS if not ok]
    print(f"power control: {'PASS' if not bad else 'FAIL'} ({len(RESULTS) - len(bad)}/{len(RESULTS)} checks)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
