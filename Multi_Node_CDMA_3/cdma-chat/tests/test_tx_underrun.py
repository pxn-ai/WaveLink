"""Reproduce mid-burst TX underruns: python -m tests.test_tx_underrun [buffer_size]

A fake ANTSDR (tests/fakegr) plays its DAC in real time and transmits zeros
whenever GNU Radio feeds it late.  A background thread hogs the Python
interpreter the way the receiver's decode loop does on a real station.
"""
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fakegr"))
import numpy as np
from gnuradio import iio
from cdmachat.config import PhyConfig
from cdmachat import radio_gr
from cdmachat.modem import Demodulator


def hog(stop, burst_ms=40, period_ms=120):
    while not stop.is_set():
        t = time.monotonic()
        while time.monotonic() - t < burst_ms / 1000:   # pure-Python work holds the GIL
            sum(i * i for i in range(2000))
        time.sleep(period_ms / 1000)


def run(buffer_size, n=40, hog_on=True):
    iio._AIR.clear(); iio._SEQ.clear()
    cfg = PhyConfig()
    tx = radio_gr.GrRadio(cfg, 1, backend="iio", uri="ip:192.168.1.10", tx_buffer=buffer_size, log=lambda *a: None)
    tx.start()
    time.sleep(0.3)
    stop = threading.Event()
    if hog_on:
        threading.Thread(target=hog, args=(stop,), daemon=True).start()
    rng = np.random.default_rng(1)
    payloads = [bytes(rng.integers(0, 256, 200, dtype=np.uint8)) for _ in range(n)]
    for p in payloads:
        tx.send(2, p)
    t_air = n * tx.airtime(200) + 1.0
    time.sleep(t_air)
    stop.set(); tx.stop()
    sink = tx.tb.chains[0][-1]
    # receive everything that went on air
    with iio._LOCK:
        air = np.concatenate([x for _, x in iio._AIR["ip:192.168.1.10"]])
    got = []
    d = Demodulator(cfg, [2, 0], on_frame=got.append)
    for i in range(0, air.size, 65536):
        d.process(air[i:i + 65536])
    ok = sum(any(g.payload == p for g in got) for p in payloads)
    bad = len(got) - ok
    print(f"  TX buffer {buffer_size:>6} samples ({buffer_size/cfg.samp_rate*1e3:4.1f} ms), hog={'on' if hog_on else 'off'}: "
          f"DAC underruns={sink.underruns:3d}  frames intact {ok}/{n}, corrupted (header ok, payload bad) {bad}")
    return ok, n


if __name__ == "__main__":
    sizes = [int(a) for a in sys.argv[1:]] or [16384, 131072]
    for b in sizes:
        run(b, hog_on=False)
        run(b, hog_on=True)
