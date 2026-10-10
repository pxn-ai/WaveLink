"""Glue between GNU Radio Companion flowgraphs and a CDMA Chat station.

The .grc files in ../grc contain two small Embedded Python Blocks:

    CDMA Chat TX  (source)  ->  ANTSDR sink         calls  station.radio.tx_read(n)
    ANTSDR source ->  CDMA Chat RX  (sink)          calls  station.radio.rx_push(x)

Both blocks ask get_station(node_id, ...) for the same object, so one station
(link layer + chat application + web UI) is shared by its TX and RX block.
The station starts when the flowgraph starts and stops when it stops.
Several stations can live in one flowgraph (e.g. the two-node simulation).
"""
from __future__ import annotations

import os
import threading

from .config import PhyConfig
from .radio_base import StreamingRadio
from .node import Node
from .web.server import serve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIONS: dict[int, "GrcStation"] = {}
_LOCK = threading.Lock()


class GrcRadio(StreamingRadio):
    """Radio whose samples are pumped by blocks inside a GRC flowgraph."""
    name = "grc"

    def start(self):
        self.start_worker()

    def stop(self):
        self.stop_worker()


class GrcStation:
    def __init__(self, node_id: int, name: str, web_port: int, enc_key: str, samp_rate: float,
                 center_freq: float, tx_buffer: int, gold_degree: int, mode: str):
        cfg = PhyConfig(samp_rate=float(samp_rate), center_freq=float(center_freq),
                        rf_bandwidth=float(samp_rate), gold_degree=int(gold_degree))
        self.radio = GrcRadio(cfg, int(node_id), tx_buffer=int(tx_buffer))
        data = os.path.join(ROOT, "data", f"grc-node{node_id}")
        self.node = Node(self.radio, int(node_id), name, data, enc_key or None, mode=mode)
        self.node.mac.ack_timeout = 0.5
        self.node.mac.half_duplex_defer = 0.25 if mode == "antsdr" else 0.0
        self.web_port = int(web_port)
        self.users = 0
        self.server = None

    def start(self):
        with _LOCK:
            self.users += 1
            if self.users > 1:
                return
        self.node.start()
        self.server = serve(self.node, "127.0.0.1", self.web_port)
        print(f"[CDMA Chat] station {self.node.addr} ({self.node.name}) on Gold code #{self.node.addr}"
              f"  ->  open http://localhost:{self.web_port}", flush=True)

    def stop(self):
        with _LOCK:
            self.users -= 1
            if self.users > 0:
                return
            _STATIONS.pop(self.node.addr, None)
        self.node.stop()
        if self.server is not None:
            self.server.shutdown()


def get_station(node_id: int, station_name: str = "", web_port: int = 8080, enc_key: str = "",
                samp_rate: float = 4e6, center_freq: float = 2.5e9, tx_buffer: int = 131072,
                gold_degree: int = 5, mode: str = "antsdr") -> GrcStation:
    node_id = int(node_id)
    with _LOCK:
        st = _STATIONS.get(node_id)
        if st is None:
            st = GrcStation(node_id, station_name or f"Node {node_id}", web_port, enc_key, samp_rate,
                            center_freq, tx_buffer, gold_degree, mode)
            _STATIONS[node_id] = st
        return st
