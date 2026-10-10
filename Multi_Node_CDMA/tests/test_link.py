"""Link-layer tests over the simulated air:  python -m tests.test_link"""
import os
import time

from cdmachat.config import PhyConfig, BROADCAST_ADDR
from cdmachat.radio_sim import SimAir
from cdmachat.mac import LinkLayer, PRIO_BULK, PRIO_HIGH


def build(n, ebn0=18.0, speed=50.0, seed=1):
    cfg = PhyConfig()
    air = SimAir(cfg, ebn0_db=ebn0, speed=speed, seed=seed)
    nodes, inbox = {}, {}
    for a in range(1, n + 1):
        r = air.add_node(a)
        inbox[a] = []
        mac = LinkLayer(r, a, on_message=lambda s, d, m, a=a: inbox[a].append((s, d, m)),
                        on_beacon=lambda s, d, f, a=a: inbox[a].append(("beacon", s, d)))
        nodes[a] = mac
        mac.start()
    air.start()
    return air, nodes, inbox


def wait(cond, air, timeout_sim=60.0):
    t0 = air.time
    while not cond():
        if air.time - t0 > timeout_sim:
            return False
        time.sleep(0.01)
    return True


def test_basic():
    air, nodes, inbox = build(3)
    events = []
    big = os.urandom(30_000)
    t0 = air.time
    m_img = nodes[1].send(3, big, PRIO_BULK, on_event=lambda e, m: events.append(("img", e, air.time)))
    m_txt = nodes[1].send(2, "hello node 2 ❤".encode(), on_event=lambda e, m: events.append(("txt", e, air.time)))
    m_rev = nodes[2].send(1, b"reply from 2" * 20, on_event=lambda e, m: events.append(("rev", e, air.time)))
    nodes[3].send_beacon(b'{"name":"C"}')
    ok = wait(lambda: m_img.state.value == "delivered" and m_txt.state.value == "delivered"
              and m_rev.state.value == "delivered", air)
    dt = air.time - t0
    air.stop()
    print(f"  delivered={ok} in {dt:.2f} s sim time")
    assert ok
    assert any(d == big for s, d, m in inbox[3] if s == 1)
    assert any(x[0] == "beacon" for x in inbox[1]) and any(x[0] == "beacon" for x in inbox[2])
    for e in events:
        if e[1] in ("delivered",):
            print(f"    {e[0]} delivered at t={e[2]-t0:.2f}s")
    print(f"    image: {len(m_img.frags)} fragments, {m_img.tx_frames} frames sent, "
          f"goodput {len(big)*8/(m_img.done_time - m_img.first_tx)/1e3:.1f} kbit/s")
    print("    stats node1:", dict(nodes[1].stats))
    print("    stats node3:", dict(nodes[3].stats))
    # addressing: node 2 must never have received the image
    assert not any(d == big for s, d, m in inbox[2] if s != "beacon")


def test_lossy():
    air, nodes, inbox = build(2, ebn0=9.0, seed=4)
    data = os.urandom(8000)
    m = nodes[1].send(2, data, PRIO_HIGH)
    ok = wait(lambda: m.state.value in ("delivered", "failed"), air, 120)
    air.stop()
    print(f"  lossy link (Eb/N0 9 dB): state={m.state.value}, frags={len(m.frags)}, frames sent={m.tx_frames}, "
          f"retransmissions={nodes[1].stats['retransmissions']}, rx header/crc fails={nodes[2].stats}")
    assert m.state.value == "delivered" and inbox[2][-1][1] == data


def test_priority():
    air, nodes, inbox = build(2)
    order = []
    nodes[1].send(2, os.urandom(20000), PRIO_BULK, on_event=lambda e, m: e == "delivered" and order.append("bulk"))
    time.sleep(0.05)
    nodes[1].send(2, b"URGENT", PRIO_HIGH, on_event=lambda e, m: e == "delivered" and order.append("urgent"))
    wait(lambda: len(order) == 2, air)
    air.stop()
    print("  priority: delivery order", order)
    assert order == ["urgent", "bulk"]


if __name__ == "__main__":
    test_basic()
    test_priority()
    test_lossy()
    print("link tests passed")
