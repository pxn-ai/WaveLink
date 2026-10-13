"""Goodput of one file transfer through grc/cdma_phy_sim.grc (real GNU Radio), FEC on vs off:
    python -m tests.test_grc_goodput [kilobytes] [noise]
noise 0.01 -> Es/N0 ~46 dB (clean), 0.5 -> ~12 dB.  The simulated graph runs at 1 MS/s, so rates
are a quarter of the 4 MS/s hardware rates."""
import os
import sys
import time

from cdmachat.grc_phy import open_flowgraph, REGISTRY, GrcPhyRadio, phy_config
from cdmachat.mac import LinkLayer, PRIO_NORMAL

GRC = os.path.join(os.path.dirname(__file__), "..", "grc", "cdma_phy_sim.grc")


def run(kb=100, noise=0.5, fec="auto", fg=None):
    tb = fg.tb
    tb.set_noise(noise)
    cfg = phy_config(tb.samp_rate, tb.sps, tb.gold_degree, tb.preamble_bits, tb.max_payload)
    macs = {}
    for n in (1, 2):
        r = GrcPhyRadio(fg, n, cfg, tx_buffer=int(tb.tx_buffer))
        m = LinkLayer(r, n, ack_timeout=0.5, mod_policy="auto", fec_policy=fec, window_airtime=0.6,
                      ack_gap=0.15)
        m.start()
        macs[n] = m
    got = []
    macs[2].on_message = lambda src, data, meta: got.append(data)
    data = os.urandom(kb * 1024)
    t0 = time.time()
    msg = macs[1].send(2, data, PRIO_NORMAL)
    while msg.state.value not in ("delivered", "failed") and time.time() - t0 < 600:
        time.sleep(0.2)
    dt = time.time() - t0
    for m in macs.values():
        m.stop()
    st = macs[1].stats
    used = {k[8:]: v for k, v in st.items() if k.startswith("data_tx_")}
    ok = msg.state.value == "delivered" and got and got[-1] == data
    q = macs[1].link_quality.get(2, {})
    print(f"  fec={fec:4}  {kb} KB: {msg.state.value}, intact={bool(ok)}, {dt:5.1f} s, "
          f"goodput {8 * len(data) / dt / 1e3:5.1f} kb/s, Es/N0 reported {q.get('fb_esn0', float('nan')):.1f} dB, "
          f"frames {used}, retransmissions {st['retransmissions']}", flush=True)
    return ok, 8 * len(data) / dt


if __name__ == "__main__":
    kb = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    noise = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    fg = open_flowgraph(GRC, {})
    for n, reg in REGISTRY.items():
        reg["mac_rx"].handler = lambda meta, data: None
    fg.start()
    time.sleep(1.0)
    print(f"GNU Radio PHY simulation, channel noise {noise}")
    a = run(kb, noise, "auto", fg)
    time.sleep(2.0)
    b = run(kb, noise, "off", fg)
    fg.stop()
    print(f"LDPC adaptive coding vs uncoded: {a[1] / max(b[1], 1):.1f}x goodput")
