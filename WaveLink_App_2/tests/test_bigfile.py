"""Large transfer over the simulated air:
    python -m tests.test_bigfile [MB] [profile] [EsN0_dB] [mod] [fec]
e.g. python -m tests.test_bigfile 10 fast 28 auto
"""
import os
import sys
import time

from wavelink.config import PhyConfig
from wavelink.radio_sim import SimAir
from wavelink.mac import LinkLayer, PRIO_BULK


def main(mb=2.0, profile="fast", ebn0=28.0, mod="auto", fec="auto"):
    cfg = PhyConfig.from_profile(profile)
    air = SimAir(cfg, ebn0_db=ebn0, speed=1.0, seed=7)
    got = []
    ra, rb = air.add_node(1), air.add_node(2)
    a = LinkLayer(ra, 1, mod_policy=mod, fec_policy=fec)
    b = LinkLayer(rb, 2, mod_policy=mod, fec_policy=fec, on_message=lambda s, d, m: got.append(d))
    for m in (a, b):
        m.start()
    air.start()
    a.send_beacon(b"{}"); b.send_beacon(b"{}")
    t0 = air.time
    while air.time - t0 < 0.5:
        time.sleep(0.01)
    data = os.urandom(int(mb * 1e6))
    w0 = time.time()
    msg = a.send(2, data, PRIO_BULK)
    last = 0
    while msg.state.value not in ("delivered", "failed"):
        time.sleep(0.5)
        if time.time() - last > 20:
            last = time.time()
            print(f"   ... {len(msg.acked)}/{len(msg.frags)} fragments, sim t={air.time - t0:.0f}s, "
                  f"mod={a.mod_for(2)}, wall {time.time() - w0:.0f}s", flush=True)
    air.stop()
    dt = msg.done_time - msg.first_tx if msg.done_time else float("nan")
    ok = bool(got) and got[0] == data
    print(f"[{profile}, Es/N0 {ebn0} dB, mod={mod} fec={fec}] {mb} MB: {msg.state.value}, intact={ok}, "
          f"{dt:.1f} s on air -> goodput {len(data) * 8 / dt / 1e3:.1f} kb/s "
          f"(raw {cfg.symbol_rate / 1e3:.0f} kb/s per bit/symbol)")
    print(f"   fragments {len(msg.frags)} x {a.max_frag} B, frames sent {msg.tx_frames}, "
          f"modulation use: { {k[8:]: v for k, v in a.stats.items() if k.startswith('data_tx_')} }")
    print(f"   sender stats: retx={a.stats['retransmissions']} timeouts={a.stats['timeouts']} "
          f"stepdowns={a.stats['mod_stepdowns']} | receiver: crc_fail={b.stats['crc_fail']} "
          f"acks={b.stats['acks_tx']} dups={b.stats['dup_frames']} | wall {time.time() - w0:.0f}s")
    return ok


if __name__ == "__main__":
    args = sys.argv[1:]
    main(float(args[0]) if args else 2.0, args[1] if len(args) > 1 else "fast",
         float(args[2]) if len(args) > 2 else 28.0, args[3] if len(args) > 3 else "auto",
         args[4] if len(args) > 4 else "auto")
