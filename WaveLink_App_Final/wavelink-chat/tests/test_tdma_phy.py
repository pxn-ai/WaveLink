"""The timing base of the TD-CDMA time slots on every PHY:

    python3 -m tests.test_tdma_phy

1. numpy PHY: send_at puts a burst on exactly the transmit sample asked for; a late one is dropped
   and counted; the receive index of a station's own burst minus its transmit sample is a constant
   (the TX -> RX offset D), whatever the receiver's sample counter started at.
2. The GNU Radio flowgraph's own blocks (MAC -> PHY, LDPC Encoder, Framer, the code lane, Burst
   Combiner), run with stand-ins for gnuradio.gr and pmt: the start chip travels as metadata 'at',
   becomes a stream tag on the burst's first chip and the combiner starts the burst exactly there;
   a burst that arrives after its chip is dropped; the despreader publishes each frame's receive
   sample index and the app reads it back; the app sees whether the flowgraph can do all this.
3. Simulated air: D survives clock drift, a TX underrun shifts it by exactly the gap, a lost RX
   buffer by minus its length.
"""
import os
import sys
import types
from dataclasses import dataclass

import numpy as np

from wavelink.config import PhyConfig
from wavelink.radio_sim import SimAir
from tests.test_power import fake_gnuradio, load_block

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"  {'ok ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")


# ============================================================================ 1. numpy PHY
def part1():
    print("1. numpy PHY: bursts on exact samples, own bursts at a constant offset")
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=30, seed=2, max_delay=1)
    r = air.add_node(1)
    got = []
    r.on_frame = got.append
    payload = os.urandom(60)
    k = 123_457
    end = r.send_at(0, payload, "qpsk-1/2", k)
    blen = r.burst_len(len(payload), "qpsk-1/2")
    check("send_at returns where the burst ends", end == k + blen, f"{end} = {k} + {blen}")
    x = np.concatenate([r.mixer.read(10_000)[0] for _ in range(20)])
    nz = np.flatnonzero(np.abs(x) > 1e-7)
    check("the burst starts exactly at the sample asked for", nz.size > 0 and nz[0] == k, f"first sample {nz[0] if nz.size else None}")
    late = r.send_at(0, payload, "qpsk-1/2", r.tx_count() - 5)
    check("a burst whose start has passed is dropped and counted", late is None and r.late_bursts() == 1)
    # own bursts through the air, receiver counter starting at a random value
    for i in range(3):
        r.send_at(0, os.urandom(60), "qpsk-1/2", r.tx_count() + 40_000 + 90_000 * i)
    air.start()
    import time
    t0 = time.time()
    while len(got) < 3 and time.time() - t0 < 30:
        time.sleep(0.05)
    air.stop()
    starts = [f.sample_index for f in got]
    diffs = np.diff(starts)
    check("three own bursts heard back", len(got) == 3, f"{len(got)} frames")
    check("their receive indices are exactly as far apart as their transmit samples",
          len(diffs) == 2 and all(d == 90_000 for d in diffs), f"{list(diffs)}")


# ============================================================================ 2. flowgraph blocks
@dataclass
class Tag:
    offset: int
    key: object
    value: object


def part2():
    print("2. the flowgraph's blocks start each burst on its chip")
    from wavelink.grc_phy import GrcPhyRadio
    from wavelink.modem import SYMBOL_TABLE
    pmt, gnuradio = fake_gnuradio()
    mac_tx = load_block("mac_tx", pmt, gnuradio, node_id=7)
    enc = load_block("ldpc_encoder", pmt, gnuradio, max_payload=1024)
    framer = load_block("framer", pmt, gnuradio, gold_degree=5, preamble_bits=48, max_payload=1024)
    comb = load_block("combiner", pmt, gnuradio, node_id=7, codes=(0, 1, 2, 3, 4), chip_rate=2e6,
                      samp_rate=4e6, tx_buffer=65536)
    P = pmt.intern("pdus")
    mac_tx.ports[P].append(enc.handlers[P])
    enc.ports[P].append(framer.handlers[P])
    lanes = []

    def lane(msg):
        # pdu filter, PDU to Tagged Stream (metadata -> tags on the first item), Chunks to Symbols,
        # Repeat x31, Multiply by the code, Tagged Stream Multiply Length (tags follow the items)
        meta, vec = msg
        sym = SYMBOL_TABLE[np.asarray(vec, dtype=np.int64)]
        chips = (np.repeat(sym, 31) * np.tile(np.where(np.arange(31) % 3, 1, -1), sym.size)).astype(np.complex64)
        lanes.append((meta, chips))
    framer.ports[P].append(lane)

    def feed(code, meta, chips, n_out):
        i = (0, 1, 2, 3, 4).index(code)
        base = comb.nread.get(i, 0)
        comb.tags[i] = [Tag(base, k, v) for k, v in [(pmt.intern("packet_len"), chips.size)] + list(meta.items())]
        ins = [np.zeros(0, np.complex64)] * 5
        ins[i] = chips
        out = np.zeros(n_out, np.complex64)
        comb.general_work(ins, [out])
        return out

    sig = __import__("inspect").signature(mac_tx.submit).parameters
    at = 20_000
    mac_tx.submit(os.urandom(80), 3, "qpsk-1/2", gain_db=-3.0, at=at)
    meta, chips = lanes[-1]
    check("MAC -> PHY takes the start chip; the encoder and the Framer pass it on as 'at'",
          "at" in sig and meta.get(pmt.intern("at")) == at, f"metadata at = {meta.get(pmt.intern('at'))}")
    out = feed(3, meta, chips, 8192)
    produced = [out]
    while comb.core.mixer.sample_count < at + chips.size + 8192:
        produced.append(feed(3, {}, np.zeros(0, np.complex64), 8192))
    y = np.concatenate(produced)
    nz = np.flatnonzero(np.abs(y) > 1e-7)
    check("the Combiner starts the burst exactly on that chip", nz.size and nz[0] == at, f"first chip {nz[0] if nz.size else None}")
    g = 20 * np.log10(np.abs(y[nz[0]]) / np.abs(chips[0]))
    check("... at the burst's power (pwr tag)", abs(g + 3.0) < 0.05, f"{g:.2f} dB")
    # late: its chip has been produced already
    mac_tx.submit(os.urandom(80), 3, "qpsk-1/2", at=comb.core.mixer.sample_count - 10)
    meta, chips = lanes[-1]
    feed(3, meta, chips, 8192)
    check("a burst that reaches the Combiner after its chip is dropped and counted",
          comb.core.mixer.late == 1, f"late = {comb.core.mixer.late}")
    # despreader metadata -> the app
    desp = load_block("despreader", pmt, gnuradio, node_id=7, samp_rate=4e6, sps=2, gold_degree=5,
                      preamble_bits=48, max_payload=1024)
    pub = []
    pmt.make_dict = dict
    pmt.from_long = int
    pmt.from_double = float
    pmt.init_c32vector = lambda n, v: list(v)
    desp.ports[pmt.intern("pdus")] = [pub.append]
    f = types.SimpleNamespace(mod="qpsk", code_idx=7, rate=1, length=84, mcs="qpsk-1/2", snr_db=12.0, esn0_db=12.0,
                              cfo_hz=100.0, metric=0.9, sample_index=987_654_321_012, symbols=np.zeros(4, np.complex64))
    desp._publish(f)
    meta = {k[1]: v for k, v in pub[-1][0].items()}
    check("the despreader publishes the receive sample index", meta.get("sample_index") == 987_654_321_012)
    radio = GrcPhyRadio.__new__(GrcPhyRadio)
    radio.reg = {"mac_tx": mac_tx, "combiner": comb.core, "despreader": types.SimpleNamespace(last_frame=None, counters={})}
    radio.cfg = types.SimpleNamespace(sf=31, sps=2, samp_rate=4e6, chip_rate=2e6)
    radio.counters = __import__("collections").Counter()
    seen = []
    radio.on_frame = seen.append
    radio._rx_pdu(meta, b"x" * 80)
    check("the app reads it back from the frame metadata", seen and seen[-1].sample_index == 987_654_321_012)
    old = types.SimpleNamespace(submit=lambda frame, code, mod, gain_db=0.0: None)
    r_old = GrcPhyRadio.__new__(GrcPhyRadio)
    r_old.reg = {"mac_tx": old}
    check("the app uses time slots only with a flowgraph that carries 'at'",
          radio.supports_send_at is True and r_old.supports_send_at is False)
    k = radio.tx_count()
    check("transmit samples are combiner chips x sps", k == comb.core.mixer.sample_count * 2, f"{k}")


# ============================================================================ 3. simulated air
def part3():
    print("3. simulated air: D with clock drift, after a TX underrun and a lost RX buffer")
    import time
    from wavelink.mac import LinkLayer
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=25, seed=11, speed=1000)
    macs = {}
    for a, ppm in ((1, 0.0), (2, 40.0), (3, -35.0)):
        r = air.add_node(a, ppm=ppm)
        macs[a] = LinkLayer(r, a, mode="tdcdma", log=lambda *x: None)
        macs[a].start()
    air.start()

    def run(t):
        t0 = air.time
        while air.time - t0 < t:
            time.sleep(0.02)
    run(4.0)
    d0 = {a: m.tdma.D for a, m in macs.items()}
    check("every station measured its D (with 40 and -35 ppm clocks)", all(v is not None for v in d0.values()),
          str({a: m.tdma.role for a, m in macs.items()}))
    run(6.0)
    d1 = {a: m.tdma.D for a, m in macs.items()}
    check("D stays put while the clocks drift", all(abs(d1[a] - d0[a]) <= 2 for a in d0), f"changes {[d1[a] - d0[a] for a in d0]}")
    drift = {a: round(macs[a].tdma.tl.eps * 1e6, 1) for a in (2, 3)}
    check("followers measure their clock against the lead's", abs(drift[2] - 40) < 4 and abs(drift[3] + 35) < 4, f"{drift} ppm")
    air.inject_tx_gap(2, 4000)
    run(3.0)
    check("a TX underrun of 4000 samples moves D by exactly that", macs[2].tdma.D - d1[2] in (3999, 4000, 4001),
          f"{macs[2].tdma.D - d1[2]}")
    air.inject_rx_drop(3, 3000)
    run(3.0)
    check("a lost RX buffer of 3000 samples moves D by minus that", macs[3].tdma.D - d1[3] in (-2999, -3000, -3001),
          f"{macs[3].tdma.D - d1[3]}")
    late = sum(m.radio.late_bursts() for m in macs.values())
    roles = sorted(m.tdma.role for m in macs.values())
    check("the network carries on", roles == ["lead", "member", "member"], f"{roles}, late bursts {late}")
    air.stop()


def main():
    part1()
    part2()
    part3()
    ok = all(RESULTS)
    print(f"TD-CDMA timing base: {'PASS' if ok else 'FAIL'} ({sum(RESULTS)}/{len(RESULTS)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
