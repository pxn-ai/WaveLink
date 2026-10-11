"""Run grc/cdma_phy_sim.grc (3 station PHYs + channel) on real GNU Radio:
    python -m tests.test_grc_flowgraph
Requires GNU Radio 3.10 with grcc on PATH."""
import os
import sys
import time

import numpy as np

from cdmachat.grc_phy import open_flowgraph, REGISTRY

GRC = os.path.join(os.path.dirname(__file__), "..", "grc", "cdma_phy_sim.grc")


def main():
    fg = open_flowgraph(GRC, {})
    print("stations registered:", sorted(REGISTRY), {k: sorted(v) for k, v in REGISTRY.items()})
    inbox = {n: [] for n in REGISTRY}
    for n, reg in REGISTRY.items():
        reg["mac_rx"].handler = (lambda meta, data, n=n: inbox[n].append((meta, data)))
    fg.start()
    time.sleep(1.0)
    rng = np.random.default_rng(1)
    sent = []
    t0 = time.time()

    def send(src, dst, code, mod, size, tag):
        data = bytes([len(sent)]) + bytes(rng.integers(0, 256, size - 1, dtype=np.uint8))
        REGISTRY[src]["mac_tx"].submit(data, code, mod)
        sent.append((src, dst, code, mod, data, tag))

    # phase 1: one burst at a time, every modulation, unicast and broadcast
    for src, dst, mod, size in ((1, 2, "bpsk", 300), (1, 2, "qpsk", 1000), (1, 2, "16qam", 1010),
                                (3, 2, "qpsk", 600), (2, 3, "16qam", 800), (2, 0, "bpsk", 120)):
        send(src, dst, dst, mod, size, "alone")
        time.sleep(1.2)
    # phase 2: three stations transmit at the same time on three different codes
    send(1, 2, 2, "qpsk", 1000, "3 at once")
    send(3, 1, 1, "qpsk", 1000, "3 at once")
    send(2, 3, 3, "bpsk", 1000, "3 at once")   # 16-QAM needs ~20 dB; with two equal-power
                                               # interferers SINR is ~12 dB (link layer steps down)
    time.sleep(3.0)
    fg.stop()
    ok = 0
    for src, dst, code, mod, data, tag in sent:
        receivers = [n for n in REGISTRY if n != src] if dst == 0 else [dst]
        for r in receivers:
            hit = [m for m, d in inbox[r] if d == data]
            good = bool(hit)
            ok += good
            info = (f"Es/N0 {hit[0].get('esn0_db', 0):5.1f} dB  CFO {hit[0].get('cfo_hz', 0):+5.0f} Hz  "
                    f"mod={hit[0].get('mod')}") if hit else ""
            print(f"  [{tag:9}] node {src} -> node {r} (code {code}) {mod:5} {len(data):5d} B : "
                  f"{'OK ' if good else 'MISSING'} {info}")
    total = sum(len([n for n in REGISTRY if n != s]) if d == 0 else 1 for s, d, *_ in sent)
    for n, reg in REGISTRY.items():
        dsp, comb = reg["despreader"], reg["combiner"]
        print(f"  node {n}: despreader frames={dsp.counters['frames']} detections={dsp.demod.stats['detections']} "
              f"header_fail={dsp.demod.stats['header_fail']} overflow={dsp.counters['rx_overflow_chunks']}  "
              f"combiner bursts={comb.bursts} underruns={comb.underruns}")
    print(f"GRC flowgraph: {ok}/{total} deliveries  ({time.time() - t0:.1f} s)")
    return ok >= total - 1          # simultaneous equal-power bursts sit near the QPSK threshold


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
