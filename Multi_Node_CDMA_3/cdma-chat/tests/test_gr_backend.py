"""Exercise cdmachat.radio_gr against a fake GNU Radio (API-shape + data-path test).
On a lab PC with real GNU Radio, run instead:  python -m cdmachat selftest --radio loopback
"""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fakegr"))
from cdmachat.config import PhyConfig, code_index_for
from cdmachat import radio_gr
from cdmachat.mac import LinkLayer
assert radio_gr.HAVE_GR


def loopback():
    r = radio_gr.GrRadio(PhyConfig(), 1, backend="loopback", log=lambda *a: None)
    got = []
    r.on_frame = got.append
    r.start(); time.sleep(0.3)
    ps = [f"loopback {i} ".encode() * 8 for i in range(4)]
    for p in ps:
        r.send(code_index_for(1), p); time.sleep(0.1)
    time.sleep(1.0); r.stop()
    ok = sum(any(g.payload == p for g in got) for p in ps)
    print(f"  loopback through flowgraph: {ok}/{len(ps)} frames, CFO est {[round(g.cfo_hz) for g in got]}")
    assert ok == len(ps)


def two_antsdr_iio():
    from gnuradio import iio
    cfg = PhyConfig()
    ra = radio_gr.GrRadio(cfg, 1, backend="iio", uri="ip:192.168.1.10", log=lambda *a: None)
    rb = radio_gr.GrRadio(cfg, 2, backend="iio", uri="ip:192.168.1.11", log=lambda *a: None)
    inbox = []
    ma = LinkLayer(ra, 1, ack_timeout=0.5)
    mb = LinkLayer(rb, 2, ack_timeout=0.5, on_message=lambda s, d, m: inbox.append(d))
    for r in (ra, rb): r.start()
    for m in (ma, mb): m.start()
    data = os.urandom(3000)
    msg = ma.send(2, data)
    t0 = time.time()
    while msg.state.value not in ("delivered", "failed") and time.time() - t0 < 20:
        time.sleep(0.05)
    for m in (ma, mb): m.stop()
    for r in (ra, rb): r.stop()
    print(f"  two fake ANTSDRs (gr-iio): {msg.state.value} in {time.time()-t0:.1f}s, "
          f"frames {msg.tx_frames} for {len(msg.frags)} fragments, rx ok={inbox and inbox[0] == data}")
    calls = {(u, n) for u, n, a in iio.CALLS}
    for need in ("freq", "fs", "att", "gmode", "gain", "bw"):
        assert any(n == need for u, n in calls), need
    f = [a for u, n, a in iio.CALLS if n == "freq"][0][0]
    print(f"  iio configuration calls ok (centre frequency {f/1e9} GHz)")
    assert msg.state.value == "delivered" and inbox[0] == data


if __name__ == "__main__":
    loopback()
    two_antsdr_iio()
    print("GNU Radio backend tests passed (fake GR)")
