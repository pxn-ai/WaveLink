"""Command line entry point.

    python -m cdmachat run   --id 1 --name Alice --radio iio --uri ip:192.168.1.10
    python -m cdmachat sim   --nodes 4                      (4 stations, no hardware)
    python -m cdmachat tx    --id 1 --to 2 --text "hello"   (mid-evaluation simplex TX)
    python -m cdmachat rx    --id 2                         (mid-evaluation simplex RX)
    python -m cdmachat selftest --radio loopback            (checks a GNU Radio install)
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from .config import PhyConfig, BROADCAST_ADDR, MAX_NODE_ID, code_index_for

NAMES = ["Alice", "Bob", "Carol", "Dave", "Erin", "Frank", "Grace", "Heidi"]


def phy_from_args(a) -> PhyConfig:
    cfg = PhyConfig()
    cfg.center_freq = a.freq
    cfg.samp_rate = a.rate
    cfg.rf_bandwidth = a.bw or a.rate
    cfg.tx_atten_db = a.tx_atten
    cfg.rx_gain_db = a.rx_gain
    cfg.rx_gain_mode = a.gain_mode
    cfg.gold_degree = a.gold
    cfg.sps = a.sps
    return cfg


def make_radio(a, cfg, addr):
    if a.radio == "sim":
        raise SystemExit("use the 'sim' sub-command for simulation")
    from .radio_gr import GrRadio
    uri = a.uri
    return GrRadio(cfg, addr, backend=a.radio, uri=uri,
                   uhd_args=a.uhd_args or f"type=ant,addr={uri.split(':')[-1]}",
                   buffer_size=a.buffer, tx_buffer=a.tx_buffer)


def add_phy_args(p):
    g = p.add_argument_group("radio / PHY (must match on every station)")
    g.add_argument("--radio", default="iio", choices=["iio", "uhd", "loopback", "sim"],
                   help="iio = ANTSDR Pluto firmware, uhd = ANTSDR UHD firmware")
    g.add_argument("--uri", default="ip:192.168.1.10", help="libiio URI of the ANTSDR (iio backend)")
    g.add_argument("--uhd-args", default="", help='UHD device args, default "type=ant,addr=<uri ip>"')
    g.add_argument("--freq", type=float, default=2.5e9, help="carrier frequency in Hz")
    g.add_argument("--rate", type=float, default=4e6, help="sample rate in S/s")
    g.add_argument("--bw", type=float, default=0, help="RF filter bandwidth (default = sample rate)")
    g.add_argument("--sps", type=int, default=4, help="samples per chip")
    g.add_argument("--gold", type=int, default=5, choices=[5, 6, 7], help="Gold code degree: 5=31, 6=63, 7=127 chips")
    g.add_argument("--tx-atten", type=float, default=10.0, help="TX attenuation dB (0 = full power)")
    g.add_argument("--rx-gain", type=float, default=40.0, help="RX gain dB (manual mode)")
    g.add_argument("--gain-mode", default="manual", choices=["manual", "slow_attack", "fast_attack"])
    g.add_argument("--buffer", type=int, default=32768, help="RX driver buffer size in samples")
    g.add_argument("--tx-buffer", type=int, default=131072,
                   help="TX driver buffer in samples (131072 = 33 ms at 4 MS/s). Raise it if the "
                        "dashboard shows TX underruns.")


def cmd_run(a):
    cfg = phy_from_args(a)
    from .node import Node
    from .web.server import serve
    radio = make_radio(a, cfg, a.id)
    data = os.path.join(a.data, f"node{a.id}")
    node = Node(radio, a.id, a.name or NAMES[(a.id - 1) % len(NAMES)], data, a.key, mode=a.radio)
    node.mac.ack_timeout = a.ack_timeout
    node.mac.half_duplex_defer = a.hd_defer
    node.start()
    serve(node, a.host, a.port)
    print(f"Station {a.id} ({node.name}) on code #{code_index_for(a.id)}  ->  open http://localhost:{a.port}")
    _wait_forever(node)


def cmd_sim(a):
    from .radio_sim import SimAir
    from .node import Node
    from .web.server import serve
    cfg = PhyConfig(gold_degree=a.gold, sps=a.sps, samp_rate=a.rate)
    air = SimAir(cfg, ebn0_db=a.ebn0, self_leak_db=a.self_leak, speed=a.speed, seed=a.seed)
    nodes = []
    for i in range(1, a.nodes + 1):
        r = air.add_node(i)
        n = Node(r, i, NAMES[i - 1], os.path.join(a.data, f"sim-node{i}"), a.key, mode="sim")
        n.mac.ack_timeout = a.ack_timeout
        nodes.append(n)
    for n in nodes:
        n.start()
        serve(n, a.host, a.port + n.addr - 1)
    print(f"Simulated air: {a.nodes} stations, Eb/N0 {a.ebn0} dB on a 0 dB link, "
          f"{cfg.sf}-chip Gold codes, {cfg.symbol_rate/1e3:.1f} kb/s")
    for n in nodes:
        print(f"  {n.name:<6} node {n.addr}  code #{n.addr}  ->  http://localhost:{a.port + n.addr - 1}")
    _wait_forever(*nodes)


def cmd_tx(a):
    """Mid-evaluation: simplex text transmission (no ACK), repeated every --every seconds."""
    from .mac import LinkLayer
    cfg = phy_from_args(a)
    radio = make_radio(a, cfg, a.id)
    mac = LinkLayer(radio, a.id)
    radio.start()
    i = 0
    try:
        while a.count <= 0 or i < a.count:
            i += 1
            text = a.text if a.count == 1 else f"{a.text} #{i}"
            _unacked(mac, a.to or BROADCAST_ADDR, text.encode())
            print(f"TX {i}: '{text}' -> node {a.to or 'broadcast'} (code #{code_index_for(a.to or BROADCAST_ADDR)})")
            time.sleep(a.every)
    except KeyboardInterrupt:
        pass
    time.sleep(0.5)
    radio.stop()


def _unacked(mac, dst, data):
    """Simplex unicast: one DATA frame spread with the destination's code, no ACK expected."""
    from .mac import HDR, T_DATA, PRIO_NORMAL, add_crc
    import struct
    body = HDR.pack(T_DATA, PRIO_NORMAL, dst, mac.addr, mac._alloc_id()) + struct.pack(">HH", 0, 1) + data[: mac.max_frag]
    mac.radio.send(code_index_for(dst), add_crc(body))


def cmd_rx(a):
    """Mid-evaluation: print every text frame addressed to this node (or broadcast)."""
    from .mac import check_crc, HDR, T_DATA
    import struct
    cfg = phy_from_args(a)
    radio = make_radio(a, cfg, a.id)

    def on_frame(f):
        body = check_crc(f.payload)
        if body is None:
            print(f"  [CRC-32 error, frame discarded]  Eb/N0 {f.snr_db:.1f} dB")
            return
        t, flags, dst, src, mid = HDR.unpack_from(body)
        if dst not in (a.id, BROADCAST_ADDR):
            return
        text = body[HDR.size + 4:] if t == T_DATA else body[HDR.size:]
        print(f"RX from node {src}: '{text.decode('utf-8', 'replace')}'   "
              f"Eb/N0 {f.snr_db:.1f} dB  CFO {f.cfo_hz/1e3:+.2f} kHz  metric {f.metric:.2f}")

    radio.on_frame = on_frame
    radio.start()
    print(f"Listening as node {a.id} on codes #{code_index_for(a.id)} and #0 (broadcast) ... Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        radio.stop()


def cmd_selftest(a):
    """Loopback through GNU Radio (or a real radio cabled/antenna'd to itself)."""
    cfg = phy_from_args(a)
    radio = make_radio(a, cfg, 1)
    got = []
    radio.on_frame = got.append
    radio.start()
    time.sleep(0.5)
    payloads = [f"selftest frame {i} ".encode() * 6 for i in range(5)]
    for p in payloads:
        radio.send(code_index_for(1), p)
        time.sleep(0.15)
    time.sleep(1.0)
    radio.stop()
    ok = sum(any(g.payload == p for g in got) for p in payloads)
    for g in got:
        print(f"  frame: {len(g.payload)} B  Eb/N0 {g.snr_db:.1f} dB  CFO {g.cfo_hz:+.0f} Hz")
    print(f"selftest: {ok}/{len(payloads)} frames decoded via backend '{a.radio}'")
    sys.exit(0 if ok == len(payloads) else 1)


def _wait_forever(*nodes):
    stop = []
    signal.signal(signal.SIGINT, lambda *x: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *x: stop.append(1))
    while not stop:
        time.sleep(0.3)
    print("stopping…")
    for n in nodes:
        n.stop()


def main(argv=None):
    p = argparse.ArgumentParser(prog="cdmachat", description="DS-CDMA multi-node chat over ANTSDR / GNU Radio")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one station with its web UI")
    r.add_argument("--id", type=int, required=True, help=f"station address 1..{MAX_NODE_ID} (= its Gold code)")
    r.add_argument("--name", default="")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open the UI from other PCs")
    r.add_argument("--key", default=None, help="shared passphrase -> AES-256-GCM encryption")
    r.add_argument("--data", default="data")
    r.add_argument("--ack-timeout", type=float, default=0.5)
    r.add_argument("--hd-defer", type=float, default=0.25,
                   help="max seconds to hold a new TX window while a burst for us is arriving (0 = off)")
    add_phy_args(r)
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("sim", help="run N simulated stations in one process (no hardware)")
    s.add_argument("--nodes", type=int, default=3)
    s.add_argument("--port", type=int, default=8081, help="first web port; station k uses port+k-1")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--ebn0", type=float, default=18.0, help="Eb/N0 (dB) of a 0 dB link; links vary +/-3 dB")
    s.add_argument("--self-leak", type=float, default=3.0, help="own-TX leakage into own RX, dB")
    s.add_argument("--speed", type=float, default=1.0, help="simulated seconds per real second (max)")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--key", default=None)
    s.add_argument("--data", default="data")
    s.add_argument("--ack-timeout", type=float, default=0.35)
    s.add_argument("--gold", type=int, default=5, choices=[5, 6, 7])
    s.add_argument("--sps", type=int, default=4)
    s.add_argument("--rate", type=float, default=4e6)
    s.set_defaults(fn=cmd_sim)

    t = sub.add_parser("tx", help="simplex text transmitter (mid-evaluation)")
    t.add_argument("--id", type=int, default=1)
    t.add_argument("--to", type=int, default=2, help="destination address, 0 = broadcast")
    t.add_argument("--text", default="Hello from EN2130 CDMA!")
    t.add_argument("--count", type=int, default=0, help="0 = repeat forever")
    t.add_argument("--every", type=float, default=1.0)
    add_phy_args(t)
    t.set_defaults(fn=cmd_tx)

    x = sub.add_parser("rx", help="simplex text receiver (mid-evaluation)")
    x.add_argument("--id", type=int, default=2)
    add_phy_args(x)
    x.set_defaults(fn=cmd_rx)

    st = sub.add_parser("selftest", help="send 5 frames to yourself through GNU Radio")
    add_phy_args(st)
    st.set_defaults(fn=cmd_selftest, radio="loopback")

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
