"""Generate the GNU Radio Companion flowgraphs in ../grc.

    python3 tools/make_grc.py

  grc/cdma_phy.grc         the physical layer of one station (ANTSDR E200, Pluto/libiio firmware).
                           The chat app compiles and runs this file: it IS the station's PHY.
  grc/cdma_phy_sim.grc     the same PHY for three stations sharing a simulated channel (no hardware).
  grc/cdma_phy_concept.grc DS-CDMA spreading/despreading with stock blocks only (teaching aid).

The Embedded Python blocks are read from tools/epy/*.py.  After generation the
.grc files are the source of truth: edit them in GNU Radio Companion if you like,
the app recompiles them every time it starts.
"""
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from cdmachat.codes import gold_codes  # noqa: E402

OUT = os.path.join(HERE, "..", "grc")
EPY = os.path.join(HERE, "epy")
GRC_VERSION = "3.10.12.0"
SF = 31
LANE_CODES = (0, 1, 2, 3, 4)            # broadcast + node ids 1..4
MODS = (("bpsk", 1, "const_bpsk"), ("qpsk", 2, "const_qpsk"), ("16qam", 4, "const_16qam"))


def epy_source(name: str) -> str:
    header = open(os.path.join(EPY, "_header.py")).read().strip()
    return open(os.path.join(EPY, name + ".py")).read().replace("#HEADER#", header)


# ============================================================================ helpers
def states(x, y, rot=0, state="enabled"):
    return dict(bus_sink=False, bus_source=False, bus_structure=None,
                coordinate=[int(x), int(y)], rotation=rot, state=state)


def blk(_name, bid, xy, **params):
    p = {k: (v if isinstance(v, str) else repr(v)) for k, v in params.items()}
    p.setdefault("comment", "")
    if bid not in ("variable", "parameter", "note", "variable_qtgui_range", "import_",
                   "variable_constellation"):
        p.setdefault("affinity", "")
        p.setdefault("alias", "")
        p.setdefault("maxoutbuf", "0")
        p.setdefault("minoutbuf", "0")
    return dict(name=_name, id=bid, parameters=p, states=states(*xy))


def epy(_name, src, xy, **params):
    return blk(_name, "epy_block", xy, _source_code=epy_source(src), **params)


def note(_name, xy, text):
    return blk(_name, "note", xy, note=text)


def flowgraph(fid, title, desc, blocks, connections, gui=True, window=(1400, 900)):
    opts = dict(
        parameters=dict(
            author="EN2130 group", catch_exceptions="True", category="[GRC Hier Blocks]",
            cmake_opt="", comment="", copyright="", description=desc, gen_cmake="On",
            gen_linking="dynamic", generate_options="qt_gui" if gui else "no_gui",
            hier_block_src_path=".:", id=fid, max_nouts="0", output_language="python",
            placement="(0,0)", qt_qss_theme="", realtime_scheduling="", run="True",
            run_command="{python} -u {filename}", run_options="prompt" if gui else "run",
            sizing_mode="fixed", thread_safe_setters="", title=title,
            window_size=f"({window[0]},{window[1]})"),
        states=states(8, 8))
    return dict(options=opts, blocks=blocks,
                connections=[[a, str(pa), b, str(pb)] for a, pa, b, pb in connections],
                metadata=dict(file_format=1, grc_version=GRC_VERSION))


def write(fname, fg):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, fname)
    with open(path, "w") as f:
        yaml.safe_dump(fg, f, sort_keys=False, default_flow_style=False, width=1000, allow_unicode=True)
    print("wrote", os.path.relpath(path), f"({len(fg['blocks'])} blocks, {len(fg['connections'])} connections)")


def spectrum(_name, xy, title, hint, fc="center_freq", n=1, labels=("",)):
    p = dict(type="complex", name=f'"{title}"', fftsize="1024", fc=fc, bw="samp_rate",
             ymin="-120", ymax="-10", average="0.2", nconnections=str(n), update_time="0.10",
             gui_hint=hint, grid="True")
    for i, l in enumerate(labels, 1):
        p[f"label{i}"] = f"'{l}'"
    return blk(_name, "qtgui_freq_sink_x", xy, **p)


def cplx(z: complex) -> str:
    return f"({z.real:g}{z.imag:+g}j)"


# ============================================================================ shared PHY definition
import textwrap  # noqa: E402

COL = 270          # horizontal pitch of a processing chain
LANE = 200         # vertical pitch of the code lanes
BRANCH = 180       # vertical pitch of the RX decoder branches


def wrap(text, width=95):
    return "\n".join(textwrap.wrap(text, width))


def phy_variables(x, y, samp_rate="4e6", sps="2"):
    """Variables shared by every station: rates, pulse shape, Gold codes, constellations."""
    g = gold_codes(5)
    pam = [-3, -1, 3, 1]                                   # Gray order: 00 01 10 11
    qam = [complex(pam[(k >> 2) & 3], pam[k & 3]) for k in range(16)]
    row1 = [
        ("samp_rate", samp_rate, "ANTSDR sample rate (S/s)"),
        ("sps", sps, "samples per chip"),
        ("chip_rate", "samp_rate / sps", ""),
        ("sf", str(SF), "spreading factor =\nGold code length"),
        ("gold_degree", "5", ""),
        ("preamble_bits", "48", ""),
        ("max_payload", "1024", "max bytes per burst\n(MAC frame + CRC-32)"),
        ("rolloff", "0.35", "RRC roll-off"),
        ("ldpc_max_iter", "20", "LDPC decoder: max\nmin-sum iterations\n(stops early when\nall checks pass)"),
    ]
    B = [blk("imports", "import_", (x, y), imports="from gnuradio.filter import firdes\nimport pmt")]
    for i, (name, val, com) in enumerate(row1):
        B.append(blk(name, "variable", (x + 260 + 200 * i, y), value=val, comment=com))
    B += [
        blk("rrc_taps", "variable", (x, y + 110),
            value="firdes.root_raised_cosine(sps, sps, 1.0, rolloff, 8 * sps + 1)",
            comment="root-raised-cosine chip pulse"),
        blk("tx_scale", "variable", (x + 460, y + 110),
            value="0.25 / (sum(t * t for t in rrc_taps) / sps) ** 0.5",
            comment="RMS 0.25 per code lane:\nheadroom for 16-QAM peaks\nand several lanes at once"),
    ]
    for k in LANE_CODES:
        B.append(blk(f"code_{k}", "variable", (x + 330 * k, y + 230),
                     value="[" + ", ".join(f"{v:+.0f}" for v in g[k]) + "]",
                     comment=("Gold code #0 (broadcast)" if k == 0 else f"Gold code #{k} (node {k})")))
    B += [
        blk("const_bpsk", "variable_constellation", (x, y + 350), type="calcdist",
            const_points="[1+0j, -1+0j]", sym_map="[0, 1]", rot_sym="2", dims="1",
            normalization="digital.constellation.POWER_NORMALIZATION", precision="8", npwr="1.0",
            soft_dec_lut="None"),
        blk("const_qpsk", "variable_constellation", (x + 300, y + 350), type="calcdist",
            const_points="[1+1j, 1-1j, -1+1j, -1-1j]", sym_map="[0, 1, 2, 3]", rot_sym="4", dims="1",
            normalization="digital.constellation.POWER_NORMALIZATION", precision="8", npwr="1.0",
            soft_dec_lut="None", comment="Gray: index = bits"),
        blk("const_16qam", "variable_constellation", (x + 600, y + 350), type="calcdist",
            const_points="[" + ", ".join(cplx(z) for z in qam) + "]",
            sym_map="[" + ", ".join(str(k) for k in range(16)) + "]", rot_sym="4", dims="1",
            normalization="digital.constellation.POWER_NORMALIZATION", precision="8", npwr="0.05",
            soft_dec_lut="None", comment="Gray: index = bits\n(I = bits 0-1, Q = bits 2-3)\n"
                                         "npwr 0.05: soft bits shaped\nfor ~13 dB (max-log-like)"),
        blk("sym_table", "variable", (x + 900, y + 350),
            value="list(const_bpsk.points()) + list(const_qpsk.points()) + list(const_16qam.points())",
            comment="combined table for the TX\nChunks to Symbols blocks:\n"
                    "0-1 BPSK, 2-5 QPSK, 6-21 16-QAM"),
    ]
    return B


VARS_HEIGHT = 470


def station_phy(p, x0, y0, node_id, with_notes=True):
    """Blocks + connections of one station's PHY (prefix p).

    Returns (blocks, connections, tx_out, rx_in, layout) where layout gives the
    coordinates the caller needs for the radio blocks."""
    B, C = [], []
    n = lambda s: p + s                                      # noqa: E731
    # ------------------------------------------------------------------ transmitter
    ty = y0
    if with_notes:
        B.append(note(n("note_tx"), (x0, ty - 110), wrap(
            "TRANSMITTER: MAC frame -> CRC-32 -> LDPC Encoder (5G NR code, rate 1/2, 2/3, 3/4, 5/6 or uncoded, "
            "chosen per frame by the link layer) -> CDMA PHY Framer (48 preamble symbols, Golay-coded header, "
            "payload as constellation indices) -> one lane per Gold code: Chunks to Symbols (BPSK / QPSK / "
            "16-QAM), Repeat x31 and Multiply by the code = spreading -> Burst Combiner (adds the lanes, "
            "zero-fills) -> RRC pulse shaping -> ANTSDR at 2.5 GHz", 120)))
    B += [
        epy(n("mac_tx"), "mac_tx", (x0, ty), node_id=node_id),
        blk(n("tx_stream"), "pdu_pdu_to_tagged_stream", (x0 + COL, ty), type="byte", tag="packet_len"),
        blk(n("tx_crc"), "digital_crc32_bb", (x0 + 2 * COL, ty), check="False",
            lengthtagname='"packet_len"', packed="True", comment="append CRC-32"),
        blk(n("tx_pdu"), "pdu_tagged_stream_to_pdu", (x0 + 3 * COL, ty), type="byte", tag="packet_len"),
        epy(n("ldpc_enc"), "ldpc_encoder", (x0 + 4 * COL, ty), max_payload="max_payload"),
    ]
    C += [(n("mac_tx"), "pdus", n("tx_stream"), "pdus"), (n("tx_stream"), 0, n("tx_crc"), 0),
          (n("tx_crc"), 0, n("tx_pdu"), 0), (n("tx_pdu"), "pdus", n("ldpc_enc"), "pdus"),
          (n("ldpc_enc"), "pdus", n("framer"), "pdus")]
    ly = ty + 150
    mid = ly + (len(LANE_CODES) // 2) * LANE
    B.append(epy(n("framer"), "framer", (x0, mid), gold_degree="gold_degree",
                 preamble_bits="preamble_bits", max_payload="max_payload"))
    lx = x0 + COL + 30
    for i, k in enumerate(LANE_CODES):
        y = ly + i * LANE
        L = lambda s: n(f"lane{k}_{s}")                      # noqa: E731,B023
        B += [
            blk(L("select"), "pdu_pdu_filter", (lx, y), k='pmt.intern("code")', v=f"pmt.from_long({k})",
                invert="False", comment=("code 0 = broadcast" if k == 0 else f"bursts for node {k}")),
            blk(L("stream"), "pdu_pdu_to_tagged_stream", (lx + COL, y), type="byte", tag="packet_len",
                minoutbuf="32768"),
            blk(L("map"), "digital_chunks_to_symbols_xx", (lx + 2 * COL, y), in_type="byte",
                out_type="complex", symbol_table="sym_table", dimension="1", num_ports="1"),
            blk(L("repeat"), "blocks_repeat", (lx + 3 * COL, y), type="complex", interp="sf", vlen="1"),
            blk(L("code"), "blocks_vector_source_x", (lx + 3 * COL, y + 80), type="complex",
                vector=f"code_{k}", repeat="True", tags="[]", vlen="1"),
            blk(L("spread"), "blocks_multiply_xx", (lx + 4 * COL, y + 30), type="complex", num_inputs="2",
                vlen="1"),
            blk(L("len"), "blocks_tagged_stream_multiply_length", (lx + 5 * COL - 60, y + 30),
                type="complex", lengthtagname='"packet_len"', vlen="1", c="sf"),
        ]
        C += [(n("framer"), "pdus", L("select"), "pdus"), (L("select"), "pdus", L("stream"), "pdus"),
              (L("stream"), 0, L("map"), 0), (L("map"), 0, L("repeat"), 0), (L("repeat"), 0, L("spread"), 0),
              (L("code"), 0, L("spread"), 1), (L("spread"), 0, L("len"), 0), (L("len"), 0, n("combiner"), i)]
    cx = lx + 6 * COL
    B += [
        epy(n("combiner"), "combiner", (cx, mid - 40), node_id=node_id,
            codes=repr(LANE_CODES), chip_rate="chip_rate", samp_rate="samp_rate", tx_buffer="tx_buffer"),
        blk(n("tx_rrc"), "interp_fir_filter_xxx", (cx + COL, mid), type="ccf", interp="sps",
            taps="rrc_taps", samp_delay="0", comment="RRC pulse shaping\n(chips -> samples)"),
        blk(n("tx_gain"), "blocks_multiply_const_vxx", (cx + 2 * COL, mid), type="complex", const="tx_scale",
            vlen="1"),
    ]
    C += [(n("combiner"), 0, n("tx_rrc"), 0), (n("tx_rrc"), 0, n("tx_gain"), 0)]
    tx_sink_xy = (cx + 3 * COL, mid - 40)
    # ------------------------------------------------------------------ receiver
    ry = ly + len(LANE_CODES) * LANE + 160
    if with_notes:
        B.append(note(n("note_rx"), (x0, ry - 110), wrap(
            "RECEIVER: ANTSDR -> RRC matched filter -> CDMA Despreader (preamble correlation for this node's "
            "code and the broadcast code, carrier-offset estimate, DLL timing, despreading, Golay header, "
            "carrier tracking) -> one branch per modulation: Constellation Soft Decoder (soft bits) -> "
            "LDPC Decoder (min-sum) -> CRC-32 check (bad frames dropped) -> MAC", 120)))
    B += [
        blk(n("rx_mf"), "fft_filter_xxx", (x0 + COL, ry), type="ccf", decim="1", taps="rrc_taps",
            samp_delay="0", nthreads="1", comment="RRC matched filter"),
        epy(n("despreader"), "despreader", (x0 + 2 * COL, ry), node_id=node_id, samp_rate="samp_rate",
            sps="sps", gold_degree="gold_degree", preamble_bits="preamble_bits", max_payload="max_payload"),
    ]
    C += [(n("rx_mf"), 0, n("despreader"), 0)]
    bx = x0 + 3 * COL + 120
    for j, (m, bps, const) in enumerate(MODS):
        y = ry + j * BRANCH
        D = lambda s: n(f"rx_{m.replace('16qam', 'qam16')}_{s}")   # noqa: E731,B023
        B += [
            blk(D("select"), "pdu_pdu_filter", (bx, y), k='pmt.intern("mod")', v=f'pmt.intern("{m}")',
                invert="False", comment=f"{m.upper()} bursts"),
            blk(D("stream"), "pdu_pdu_to_tagged_stream", (bx + COL, y), type="complex", tag="packet_len",
                minoutbuf="32768", comment="buffer holds a whole burst\n(1 KB BPSK-1/2 = 16.5k symbols)"),
            blk(D("soft"), "digital_constellation_soft_decoder_cf", (bx + 2 * COL, y), constellation=const,
                npwr="-1", minoutbuf="32768", comment=f"{bps} soft bit{'s' if bps > 1 else ''} per symbol"),
            blk(D("len"), "blocks_tagged_stream_multiply_length", (bx + 3 * COL, y), type="float",
                lengthtagname='"packet_len"', vlen="1", c=str(bps), minoutbuf="32768"),
            blk(D("pdu"), "pdu_tagged_stream_to_pdu", (bx + 4 * COL, y), type="float", tag="packet_len"),
        ]
        C += [(n("despreader"), "pdus", D("select"), "pdus"), (D("select"), "pdus", D("stream"), "pdus"),
              (D("stream"), 0, D("soft"), 0), (D("soft"), 0, D("len"), 0), (D("len"), 0, D("pdu"), 0),
              (D("pdu"), "pdus", n("ldpc_dec"), "pdus")]
    cy = ry + BRANCH
    B += [
        epy(n("ldpc_dec"), "ldpc_decoder", (bx + 5 * COL, cy), node_id=node_id, max_iter="ldpc_max_iter"),
        blk(n("rx_bytes"), "pdu_pdu_to_tagged_stream", (bx + 6 * COL, cy), type="byte", tag="packet_len"),
        blk(n("rx_crc"), "digital_crc32_bb", (bx + 7 * COL, cy), check="True", lengthtagname='"packet_len"',
            packed="True", comment="check + strip CRC-32\n(bad frames dropped)"),
        blk(n("rx_pdu"), "pdu_tagged_stream_to_pdu", (bx + 8 * COL, cy), type="byte", tag="packet_len"),
        epy(n("mac_rx"), "mac_rx", (bx + 9 * COL, cy), node_id=node_id),
    ]
    C += [(n("ldpc_dec"), "pdus", n("rx_bytes"), "pdus"), (n("rx_bytes"), 0, n("rx_crc"), 0),
          (n("rx_crc"), 0, n("rx_pdu"), 0), (n("rx_pdu"), "pdus", n("mac_rx"), "pdus")]
    layout = dict(tx_sink=tx_sink_xy, rx_source=(x0, ry), bottom=ry + 3 * BRANCH, right=bx + 10 * COL)
    return B, C, n("tx_gain"), n("rx_mf"), layout


# ============================================================================ 1. station PHY (ANTSDR)
def cdma_phy():
    B = [note("note_about", (8, 90), wrap(
        "PHYSICAL LAYER of one CDMA Chat station. The chat app compiles this file with grcc, runs it and "
        "attaches its link layer to the 'MAC -> PHY' and 'PHY -> MAC' blocks. Every station uses the same "
        "file with its own Node ID (1-4 = its Gold code). ANTSDR E200 with the Pluto (libiio) firmware, "
        "carrier 2.5 GHz, DS-CDMA with 31-chip Gold codes, payload BPSK, QPSK or 16-QAM chosen per frame "
        "by the link layer.", 150))]
    params = [("node_id", "Node ID (= Gold code)", "intx", "1", "i"),
              ("uri", "ANTSDR URI", "str", "ip:192.168.1.10", "u"),
              ("center_freq", "Carrier (Hz)", "eng_float", "2.5e9", "f"),
              ("rx_gain", "RX gain (dB)", "eng_float", "40", "g"),
              ("tx_atten", "TX attenuation (dB)", "eng_float", "10", "a"),
              ("tx_buffer", "TX buffer (samples)", "intx", "131072", "b")]
    for i, (name, label, typ, val, sid) in enumerate(params):
        B.append(blk(name, "parameter", (8 + 240 * i, 170), label=label, type=typ, value=val,
                     short_id=sid, hide="none"))
    B += phy_variables(8, 290)
    sb, sc, tx_out, rx_in, lay = station_phy("", 8, 290 + VARS_HEIGHT + 150, "node_id")
    B += sb
    C = list(sc)
    B += [
        blk("antsdr_tx", "iio_pluto_sink", lay["tx_sink"], type="fc32", uri="uri",
            frequency="int(center_freq)", samplerate="int(samp_rate)", bandwidth="int(samp_rate)",
            buffer_size="tx_buffer", cyclic="False", attenuation1="tx_atten", len_tag_key="",
            filter_source="'Auto'", filter="", fpass="0", fstop="0"),
        blk("antsdr_rx", "iio_pluto_source", lay["rx_source"], type="fc32", uri="uri",
            frequency="int(center_freq)", samplerate="int(samp_rate)", bandwidth="int(samp_rate)",
            buffer_size="32768", gain1="'manual'", manual_gain1="rx_gain", quadrature="True", rfdc="True",
            bbdc="True", len_tag_key="", filter_source="'Auto'", filter="", fpass="0", fstop="0"),
    ]
    C += [(tx_out, 0, "antsdr_tx", 0), ("antsdr_rx", 0, rx_in, 0)]
    return flowgraph("cdma_phy", "CDMA Chat - physical layer (ANTSDR E200)",
                     "DS-CDMA BPSK/QPSK/16-QAM physical layer of one station", B, C, gui=False)


# ============================================================================ 2. three stations, simulated channel
def cdma_phy_sim(nodes=(1, 2, 3)):
    B = [note("note_about", (8, 90), wrap(
        "Three complete station PHYs (nodes 1-3) sharing one simulated 2.5 GHz channel: their transmit "
        "signals are added, passed through a Channel Model (noise + carrier offset) and fed to every "
        "receiver. Run with:  python3 -m cdmachat grc-sim   (web UIs on ports 8081-8083). Sample rate is "
        "1 MS/s because all three PHYs share one Python process.", 150))]
    B += [
        blk("tx_buffer", "variable", (8, 170), value="32768"),
        blk("noise", "variable", (220, 170), value="0.01", comment="channel noise voltage"),
        blk("cfo_hz", "variable", (420, 170), value="300", comment="carrier offset (Hz)"),
    ]
    B += phy_variables(8, 290, samp_rate="1e6", sps="2")   # 3 PHYs share one Python process
    C = []
    txs, rxs = [], []
    y0 = 290 + VARS_HEIGHT + 260
    right = 0
    for i, nid in enumerate(nodes):
        B.append(note(f"n{nid}_title", (8, y0 - 200),
                      f"STATION {nid}   (Node ID {nid}, listens on Gold code #{nid} and #0)"))
        sb, sc, tx_out, rx_in, lay = station_phy(f"n{nid}_", 8, y0, str(nid), with_notes=True)
        B += sb
        C += sc
        txs.append(tx_out)
        rxs.append(rx_in)
        right = max(right, lay["right"], lay["tx_sink"][0])
        y0 = lay["bottom"] + 380
    cx, cy = right + 200, 290 + VARS_HEIGHT + 600
    B += [
        note("note_channel", (cx, cy - 90), "SIMULATED CHANNEL\n(all transmitters -> all receivers)"),
        blk("air", "blocks_add_xx", (cx, cy), type="complex", num_inputs=str(len(nodes)), vlen="1"),
        blk("throttle", "blocks_throttle", (cx + COL, cy), type="complex", samples_per_second="samp_rate",
            ignoretag="True", vlen="1"),
        blk("channel", "channels_channel_model", (cx + 2 * COL, cy), noise_voltage="noise",
            freq_offset="cfo_hz / samp_rate", epsilon="1.0", taps="[1.0 + 0j]", seed="0",
            block_tags="False"),
    ]
    C += [(t, 0, "air", i) for i, t in enumerate(txs)]
    C += [("air", 0, "throttle", 0), ("throttle", 0, "channel", 0)]
    C += [("channel", 0, r, 0) for r in rxs]
    return flowgraph("cdma_phy_sim", "CDMA Chat - 3 station PHYs, simulated channel",
                     "Three DS-CDMA station PHYs over a simulated channel (no hardware)", B, C, gui=False)


# ============================================================================ 4. PHY concept
def phy_concept():
    g = gold_codes(5)
    c1, c2, c3 = ([int(v) for v in g[i]] for i in (1, 2, 3))
    B = [blk("note_about", "note", (8, 100), note=(
        "DS-CDMA from stock GNU Radio blocks. Two users send random BPSK bits, each spread by its own "
        "31-chip Gold code (the same codes the chat system gives node 1 and node 2). The sum plus noise "
        "is despread with code 1, code 2, and an unused code 3. Despreading with the right code "
        "recovers +/-1; the wrong code gives only noise. Compare the narrow BPSK spectrum with the "
        "31x wider spread spectrum. (Rectangular chips, perfect sync, for teaching.)"))]
    B += [
        blk("samp_rate", "variable", (232, 8), value="1e6", comment="1 sample per chip"),
        blk("sf", "variable", (360, 8), value="31"),
        blk("code1", "variable", (460, 8), value=str(c1)),
        blk("code2", "variable", (580, 8), value=str(c2)),
        blk("code3", "variable", (700, 8), value=str(c3)),
        blk("noise_amp", "variable_qtgui_range", (840, 8), label="Noise amplitude", rangeType="float",
            value="1.0", start="0", stop="5", step="0.1", widget="counter_slider", gui_hint="0,0,1,2"),
        blk("user2_gain", "variable_qtgui_range", (1060, 8), label="User 2 amplitude (near-far)",
            rangeType="float", value="1.0", start="0", stop="5", step="0.1", widget="counter_slider",
            gui_hint="1,0,1,2"),
    ]
    rows = [("u1", 220, "code1"), ("u2", 400, "code2")]
    C = []
    for u, y, code in rows:
        B += [
            blk(f"{u}_bits", "analog_random_source_x", (8, y), type="byte", min="0", max="2",
                num_samps="10000", repeat="True"),
            blk(f"{u}_bpsk", "digital_chunks_to_symbols_xx", (200, y + 8), in_type="byte", out_type="complex",
                symbol_table="[-1, 1]", dimension="1", num_ports="1"),
            blk(f"{u}_repeat", "blocks_repeat", (400, y + 8), type="complex", interp="sf", vlen="1"),
            blk(f"{u}_code", "blocks_vector_source_x", (400, y + 80), type="complex", vector=code,
                repeat="True", tags="[]", vlen="1"),
            blk(f"{u}_spread", "blocks_multiply_xx", (600, y + 20), type="complex", num_inputs="2", vlen="1"),
        ]
        C += [(f"{u}_bits", 0, f"{u}_bpsk", 0), (f"{u}_bpsk", 0, f"{u}_repeat", 0),
              (f"{u}_repeat", 0, f"{u}_spread", 0), (f"{u}_code", 0, f"{u}_spread", 1)]
    B += [
        blk("u2_level", "blocks_multiply_const_vxx", (760, 420), type="complex", const="user2_gain", vlen="1"),
        blk("noise_src", "analog_noise_source_x", (760, 540), type="complex", noise_type="analog.GR_GAUSSIAN",
            amp="noise_amp", seed="42"),
        blk("air", "blocks_add_xx", (960, 320), type="complex", num_inputs="3", vlen="1"),
        blk("throttle", "blocks_throttle", (1100, 330), type="complex", samples_per_second="samp_rate",
            ignoretag="True", vlen="1"),
        spectrum("spec", (1100, 120), "Narrowband BPSK (user 1) vs spread signal on air", "2,0,1,2", fc="0",
                 n=2, labels=("BPSK before spreading", "On air (2 users + noise)")),
    ]
    C += [("u1_spread", 0, "air", 0), ("u2_spread", 0, "u2_level", 0), ("u2_level", 0, "air", 1),
          ("noise_src", 0, "air", 2), ("air", 0, "throttle", 0),
          ("u1_repeat", 0, "spec", 0), ("throttle", 0, "spec", 1)]
    # despreaders: x code -> integrate & dump over one bit -> /31
    for i, (k, code, y) in enumerate((("d1", "code1", 220), ("d2", "code2", 400), ("d3", "code3", 580))):
        B += [
            blk(f"{k}_code", "blocks_vector_source_x", (1300, y + 70), type="complex", vector=code,
                repeat="True", tags="[]", vlen="1"),
            blk(f"{k}_mix", "blocks_multiply_xx", (1500, y + 20), type="complex", num_inputs="2", vlen="1"),
            blk(f"{k}_dump", "blocks_integrate_xx", (1660, y + 20), type="complex", decim="sf", vlen="1"),
            blk(f"{k}_norm", "blocks_multiply_const_vxx", (1820, y + 20), type="complex", const="1/sf", vlen="1"),
        ]
        C += [("throttle", 0, f"{k}_mix", 0), (f"{k}_code", 0, f"{k}_mix", 1), (f"{k}_mix", 0, f"{k}_dump", 0),
              (f"{k}_dump", 0, f"{k}_norm", 0), (f"{k}_norm", 0, "const", i)]
    B += [
        blk("const", "qtgui_const_sink_x", (2000, 380), type="complex",
            name='"Despread symbols: right code vs wrong code"', size="512", nconnections="3",
            xmin="-2.5", xmax="2.5", ymin="-2.5", ymax="2.5", gui_hint="3,0,2,2", grid="True",
            label1="'despread with code 1 (user 1)'", label2="'despread with code 2 (user 2)'",
            label3="'despread with code 3 (nobody)'", color3='"green"', style1="0", style2="0", style3="0",
            marker1="0", marker2="0", marker3="0"),
        blk("chips_scope", "qtgui_time_sink_x", (1300, 720), type="complex", name='"Chips on air (noise-like)"',
            size="310", srate="samp_rate", ymin="-4", ymax="4", update_time="0.2", gui_hint="5,0,1,2",
            label1="'I'", label2="'Q'", entags="False"),
    ]
    C += [("throttle", 0, "chips_scope", 0)]
    return flowgraph("cdma_phy_concept", "EN2130 DS-CDMA concept: spreading and despreading",
                     "Two-user DS-CDMA with stock blocks", B, C, window=(1300, 1100))


if __name__ == "__main__":
    for old in ("cdma_station.grc", "cdma_station_uhd.grc", "cdma_two_nodes_sim.grc"):
        p = os.path.join(OUT, old)
        if os.path.exists(p):
            os.remove(p)
            print("removed", os.path.relpath(p))
    write("cdma_phy.grc", cdma_phy())
    write("cdma_phy_sim.grc", cdma_phy_sim())
    write("cdma_phy_concept.grc", phy_concept())
