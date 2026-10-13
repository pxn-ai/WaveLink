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

    def send(src, dst, code, mod, size, tag, expect=True):
        data = bytes([len(sent)]) + bytes(rng.integers(0, 256, size - 1, dtype=np.uint8))
        REGISTRY[src]["mac_tx"].submit(data, code, mod)
        sent.append((src, dst, code, mod, data, tag, expect))

    # phase 1: one burst at a time, every modulation, coded and uncoded, unicast and broadcast
    for src, dst, mod, size in ((1, 2, "bpsk", 300), (1, 2, "qpsk-1/2", 1000), (1, 2, "16qam", 1010),
                                (1, 2, "16qam-3/4", 1020), (3, 2, "qpsk-3/4", 600), (2, 3, "16qam-5/6", 800),
                                (3, 1, "16qam-2/3", 700), (2, 0, "qpsk-1/2", 120), (1, 3, "bpsk-1/2", 400)):
        send(src, dst, dst, mod, size, "alone")
        time.sleep(1.0)
    # phase 2: three stations transmit at the same time on three different codes: each link sees
    # the other two as interference (SINR ~12 dB) - uncoded 16-QAM would need ~20 dB
    send(1, 2, 2, "16qam-1/2", 1000, "3 at once")
    send(3, 1, 1, "16qam-1/2", 1000, "3 at once")
    send(2, 3, 3, "qpsk-3/4", 1000, "3 at once")
    time.sleep(3.0)
    # phase 3: a noisy channel (Es/N0 ~ 6 dB): LDPC-coded QPSK gets through, uncoded cannot
    if hasattr(fg.tb, "set_noise"):
        fg.tb.set_noise(1.0)
        time.sleep(0.5)
        for mod, expect in (("qpsk-1/2", True), ("qpsk-3/4", True), ("qpsk", False), ("16qam-1/2", False)):
            send(1, 2, 2, mod, 1000, "Es/N0~6dB", expect)
            time.sleep(1.2)
        fg.tb.set_noise(0.01)
    time.sleep(1.0)
    fg.stop()
    ok = 0
    expected_ok = 0
    for src, dst, code, mod, data, tag, expect in sent:
        receivers = [n for n in REGISTRY if n != src] if dst == 0 else [dst]
        for r in receivers:
            hit = [m for m, d in inbox[r] if d == data]
            good = bool(hit)
            ok += good
            expected_ok += good == expect
            info = (f"Es/N0 {hit[0].get('esn0_db', 0):5.1f} dB  CFO {hit[0].get('cfo_hz', 0):+5.0f} Hz  "
                    f"{hit[0].get('mcs')}  LDPC iterations {hit[0].get('fec_iters', 0)}") if hit else ""
            verdict = ("OK " if good else "MISSING") + ("" if good == expect else "  <-- unexpected")
            print(f"  [{tag:9}] node {src} -> node {r} (code {code}) {mod:10} {len(data):5d} B : {verdict} {info}")
    total = sum(len([n for n in REGISTRY if n != s]) if d == 0 else 1 for s, d, *_ in sent)
    for n, reg in REGISTRY.items():
        dsp, comb = reg["despreader"], reg["combiner"]
        dec = reg["ldpc_decoder"]
        print(f"  node {n}: despreader frames={dsp.counters['frames']} detections={dsp.demod.stats['detections']} "
              f"header_fail={dsp.demod.stats['header_fail']} overflow={dsp.counters['rx_overflow_chunks']}  "
              f"combiner bursts={comb.bursts} underruns={comb.underruns}  LDPC {dict(dec.stats)} "
              f"{1000 * dec.busy_s / max(1, dec.stats['fec_frames']):.1f} ms/frame")
    print(f"GRC flowgraph: {ok}/{total} deliveries, {expected_ok}/{total} as expected  ({time.time() - t0:.1f} s)")
    return expected_ok >= total - 1


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
