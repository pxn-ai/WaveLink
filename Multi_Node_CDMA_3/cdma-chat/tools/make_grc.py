"""Regenerate the GNU Radio Companion flowgraphs in ../grc.

    python3 tools/make_grc.py

Writes:
  grc/cdma_station.grc            one station, ANTSDR E200 with Pluto (libiio) firmware
  grc/cdma_station_uhd.grc        one station, ANTSDR E200 with UHD firmware
  grc/cdma_two_nodes_sim.grc      two stations + simulated channel, no hardware needed
  grc/cdma_phy_concept.grc        DS-CDMA spreading/despreading built from stock GNU Radio blocks
"""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from cdmachat.codes import gold_codes  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "..", "grc")
GRC_VERSION = "3.10.9.2"

# ============================================================================ embedded python blocks
EPY_HEADER = '''import os
import sys
import numpy as np
from gnuradio import gr

# Make the cdmachat package importable: this file is generated next to the .grc,
# and the .grc lives in <project>/grc/.
_HERE = os.path.dirname(os.path.abspath(globals().get("__file__", os.getcwd() + "/x")))
for _cand in (os.path.join(_HERE, ".."), _HERE, os.getcwd(), os.path.join(os.getcwd(), "..")):
    if os.path.isdir(os.path.join(_cand, "cdmachat")):
        sys.path.insert(0, os.path.abspath(_cand))
        break
'''

EPY_TX = '''"""
CDMA Chat TX - one DS-CDMA station (transmit side + the whole station).

When the flowgraph starts this block starts the station: link layer
(addressing, CRC-32, fragmentation, selective-repeat ARQ, priority),
chat application and the web UI on http://localhost:<web_port>.
Its output is the station's transmit signal: zeros when idle, and
DS-CDMA BPSK bursts (31-chip Gold codes, RRC pulses) whenever the link
layer sends. Bursts for different stations are spread with different
codes and summed, so several links share 2.5 GHz at the same time.

Connect the output to the ANTSDR sink.  Pair it with a 'CDMA Chat RX'
block that has the same node_id.
"""
''' + EPY_HEADER + '''

class blk(gr.sync_block):
    def __init__(self, node_id=1, station_name="Alice", web_port=8080, enc_key="",
                 samp_rate=4e6, center_freq=2.5e9, tx_buffer=131072, mode="antsdr"):
        gr.sync_block.__init__(self, name="CDMA Chat TX", in_sig=None, out_sig=[np.complex64])
        self.node_id = int(node_id)
        self.station_name = station_name
        self.web_port = int(web_port)
        self.enc_key = enc_key
        self.samp_rate = float(samp_rate)
        self.center_freq = float(center_freq)
        self.tx_buffer = int(tx_buffer)
        self.mode = mode
        self.station = None

    def start(self):
        from cdmachat.grc_glue import get_station
        self.station = get_station(self.node_id, self.station_name, self.web_port, self.enc_key,
                                   self.samp_rate, self.center_freq, self.tx_buffer, mode=self.mode)
        self.station.start()
        return True

    def stop(self):
        if self.station is not None:
            self.station.stop()
            self.station = None
        return True

    def work(self, input_items, output_items):
        out = output_items[0]
        if self.station is None:
            out[:] = 0
        else:
            out[:] = self.station.radio.tx_read(len(out))
        return len(out)
'''

EPY_RX = '''"""
CDMA Chat RX - receive side of a DS-CDMA station.

Feeds the received baseband samples to the station's demodulator, which
runs in its own thread: RRC matched filter, Gold-code correlators for this
node's code and the broadcast code, CFO-immune differential preamble
detector, PLL + DLL despreading, CRC-8 header check, then the link layer
(CRC-32, addressing, ACKs).  Several same-code bursts that overlap in time
are decoded independently.

Connect the ANTSDR source to its input.  Use the same node_id as the
'CDMA Chat TX' block of this station.
"""
''' + EPY_HEADER + '''

class blk(gr.sync_block):
    def __init__(self, node_id=1, station_name="Alice", web_port=8080, enc_key="",
                 samp_rate=4e6, center_freq=2.5e9, tx_buffer=131072, mode="antsdr"):
        gr.sync_block.__init__(self, name="CDMA Chat RX", in_sig=[np.complex64], out_sig=None)
        self.args = (int(node_id), station_name, int(web_port), enc_key, float(samp_rate),
                     float(center_freq), int(tx_buffer))
        self.mode = mode
        self.station = None

    def start(self):
        from cdmachat.grc_glue import get_station
        self.station = get_station(*self.args, mode=self.mode)
        self.station.start()
        return True

    def stop(self):
        if self.station is not None:
            self.station.stop()
            self.station = None
        return True

    def work(self, input_items, output_items):
        if self.station is not None:
            self.station.radio.rx_push(input_items[0])
        return len(input_items[0])
'''

STATION_ARGS = dict(node_id="node_id", station_name="station_name", web_port="web_port",
                    enc_key="enc_key", samp_rate="samp_rate", center_freq="center_freq",
                    tx_buffer="tx_buffer", mode="'antsdr'")


# ============================================================================ helpers
def states(x, y, rot=0):
    return dict(bus_sink=False, bus_source=False, bus_structure=None,
                coordinate=[int(x), int(y)], rotation=rot, state="enabled")


def blk(_name, bid, xy, **params):
    p = {k: (v if isinstance(v, str) else repr(v)) for k, v in params.items()}
    p.setdefault("comment", "")
    if bid not in ("variable", "parameter", "note", "variable_qtgui_range", "import"):
        p.setdefault("affinity", "")
        p.setdefault("alias", "")
        p.setdefault("maxoutbuf", "0")
        p.setdefault("minoutbuf", "0")
    return dict(name=_name, id=bid, parameters=p, states=states(*xy))


def epy(_name, code, xy, **params):
    return blk(_name, "epy_block", xy, _source_code=code, **params)


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
    print("wrote", os.path.relpath(path))


def station_params(x, y, node_id=1, name="Alice", port=8080):
    """Command-line parameters: python3 cdma_station.py --node-id 2 --station-name Bob ..."""
    return [
        blk("node_id", "parameter", (x, y), label="Node ID (= Gold code #)", type="intx",
            value=str(node_id), short_id="i", hide="none"),
        blk("station_name", "parameter", (x + 200, y), label="Station name", type="str",
            value=name, short_id="n", hide="none"),
        blk("web_port", "parameter", (x + 400, y), label="Web UI port", type="intx",
            value=str(port), short_id="p", hide="none"),
        blk("enc_key", "parameter", (x + 600, y), label="Encryption passphrase ('' = off)",
            type="str", value="", short_id="k", hide="none"),
    ]


def common_vars(x, y):
    return [
        blk("samp_rate", "variable", (x, y), value="4e6"),
        blk("center_freq", "variable", (x + 140, y), value="2.5e9"),
        blk("tx_buffer", "variable", (x + 280, y), value="131072",
            comment="TX driver buffer (33 ms).\nLarger = no mid-burst underruns."),
    ]


def spectrum(_name, xy, title, hint, fc="center_freq", n=1, labels=("",)):
    p = dict(type="complex", name=f'"{title}"', fftsize="1024", fc=fc, bw="samp_rate",
             ymin="-120", ymax="-10", average="0.2", nconnections=str(n), update_time="0.10",
             gui_hint=hint, grid="True")
    for i, l in enumerate(labels, 1):
        p[f"label{i}"] = f"'{l}'"
    return blk(_name, "qtgui_freq_sink_x", xy, **p)


# ============================================================================ 1. station (iio)
def station_iio():
    B = [blk("note_about", "note", (8, 140), note=(
        "DS-CDMA chat station on an ANTSDR E200 (Pluto/libiio firmware, boot switch = QSPI). "
        "Run, then open http://localhost:<web_port>. All stations must share samp_rate and center_freq; "
        "each needs a different node_id. Command line: python3 cdma_station.py -i 2 -n Bob"))]
    B += station_params(232, 8)
    B += common_vars(1064, 8)
    B += [
        blk("uri", "parameter", (1064, 100), label="ANTSDR URI", type="str", value="ip:192.168.1.10",
            short_id="u", hide="none"),
        blk("rx_gain", "variable_qtgui_range", (1240, 100), label="RX gain (dB)", rangeType="float",
            value="40", start="0", stop="71", step="1", widget="counter_slider", gui_hint="0,0,1,1"),
        blk("tx_atten", "variable_qtgui_range", (1440, 100), label="TX attenuation (dB, 0 = max power)",
            rangeType="float", value="10", start="0", stop="89", step="1", widget="counter_slider",
            gui_hint="0,1,1,1"),
        blk("antsdr_rx", "iio_fmcomms2_source", (40, 300), type="fc32", uri="uri", frequency="int(center_freq)",
            samplerate="int(samp_rate)", bandwidth="int(samp_rate)", buffer_size="32768",
            rx1_en="True", rx2_en="False", gain1="'manual'", manual_gain1="rx_gain", quadrature="True",
            rfdc="True", bbdc="True", len_tag_key="''", filter_source="'Auto'"),
        epy("cdma_rx", EPY_RX, (440, 330), **STATION_ARGS),
        spectrum("rx_spectrum", (440, 440), "Received spectrum (2.5 GHz)", "1,0,1,2"),
        blk("rx_waterfall", "qtgui_waterfall_sink_x", (440, 600), type="complex",
            name='"Received waterfall: CDMA bursts"', fftsize="512", fc="center_freq", bw="samp_rate",
            int_min="-110", int_max="-30", gui_hint="2,0,1,2"),
        epy("cdma_tx", EPY_TX, (40, 760), **STATION_ARGS),
        blk("antsdr_tx", "iio_fmcomms2_sink", (440, 780), type="fc32", uri="uri", frequency="int(center_freq)",
            samplerate="int(samp_rate)", bandwidth="int(samp_rate)", buffer_size="tx_buffer",
            tx1_en="True", tx2_en="False", cyclic="False", attenuation1="tx_atten", len_tag_key="''",
            filter_source="'Auto'"),
        blk("tx_scope", "qtgui_time_sink_x", (440, 940), type="complex", name='"Transmitted baseband (bursts)"',
            size="40000", srate="samp_rate", ymin="-1", ymax="1", update_time="0.2", gui_hint="3,0,1,2",
            label1="'I'", label2="'Q'", entags="False"),
    ]
    C = [("antsdr_rx", 0, "cdma_rx", 0), ("antsdr_rx", 0, "rx_spectrum", 0), ("antsdr_rx", 0, "rx_waterfall", 0),
         ("cdma_tx", 0, "antsdr_tx", 0), ("cdma_tx", 0, "tx_scope", 0)]
    return flowgraph("cdma_station", "EN2130 DS-CDMA Chat Station (ANTSDR E200, libiio)",
                     "One station of the multi-node DS-CDMA chat system", B, C)


# ============================================================================ 2. station (UHD)
def station_uhd():
    B = [blk("note_about", "note", (8, 140), note=(
        "Same station for the ANTSDR E200 with the UHD firmware (boot switch = SD) and MicroPhase's "
        "antsdr_uhd driver installed. Device args: type=ant,addr=<ip>."))]
    B += station_params(232, 8)
    B += common_vars(1064, 8)
    B += [
        blk("dev_args", "parameter", (1064, 100), label="UHD device args", type="str",
            value="type=ant,addr=192.168.1.10", short_id="a", hide="none"),
        blk("rx_gain", "variable_qtgui_range", (1240, 100), label="RX gain (dB)", rangeType="float",
            value="40", start="0", stop="76", step="1", widget="counter_slider", gui_hint="0,0,1,1"),
        blk("tx_gain", "variable_qtgui_range", (1440, 100), label="TX gain (dB)", rangeType="float",
            value="70", start="0", stop="89", step="1", widget="counter_slider", gui_hint="0,1,1,1"),
        blk("antsdr_rx", "uhd_usrp_source", (40, 300), type="fc32", otw="", dev_addr="dev_args",
            dev_args='""', sync="none", num_mboards="1", nchan="1", samp_rate="samp_rate",
            center_freq0="center_freq", gain0="rx_gain", gain_type0="default", ant0='"RX2"',
            bw0="samp_rate", rx_agc0="Default", clock_source0="", time_source0="", sd_spec0=""),
        epy("cdma_rx", EPY_RX, (440, 330), **STATION_ARGS),
        spectrum("rx_spectrum", (440, 440), "Received spectrum (2.5 GHz)", "1,0,1,2"),
        epy("cdma_tx", EPY_TX, (40, 640), **STATION_ARGS),
        blk("antsdr_tx", "uhd_usrp_sink", (440, 650), type="fc32", otw="", dev_addr="dev_args",
            dev_args='""', sync="none", num_mboards="1", nchan="1", samp_rate="samp_rate",
            center_freq0="center_freq", gain0="tx_gain", gain_type0="default", ant0='"TX/RX"',
            bw0="samp_rate", len_tag_name='""', clock_source0="", time_source0="", sd_spec0=""),
        blk("tx_scope", "qtgui_time_sink_x", (440, 820), type="complex", name='"Transmitted baseband (bursts)"',
            size="40000", srate="samp_rate", ymin="-1", ymax="1", update_time="0.2", gui_hint="2,0,1,2",
            label1="'I'", label2="'Q'", entags="False"),
    ]
    C = [("antsdr_rx", 0, "cdma_rx", 0), ("antsdr_rx", 0, "rx_spectrum", 0),
         ("cdma_tx", 0, "antsdr_tx", 0), ("cdma_tx", 0, "tx_scope", 0)]
    return flowgraph("cdma_station_uhd", "EN2130 DS-CDMA Chat Station (ANTSDR E200, UHD)",
                     "One station of the multi-node DS-CDMA chat system (UHD firmware)", B, C)


# ============================================================================ 3. two-node simulation
def two_nodes_sim():
    def args(i, name, port):
        a = dict(STATION_ARGS)
        a.update(node_id=str(i), station_name=f"'{name}'", web_port=str(port), enc_key="''", mode="'grc-sim'")
        return a
    B = [blk("note_about", "note", (8, 100), note=(
        "No hardware needed: two complete stations share one simulated 2.5 GHz channel. "
        "Open http://localhost:8081 (Alice) and http://localhost:8082 (Bob). "
        "Move the noise slider to watch CRC drops and ARQ retransmissions in the web dashboard."))]
    B += common_vars(232, 8)
    B[-3] = blk("samp_rate", "variable", (232, 8), value="2e6",
                comment="2 MS/s: two receivers run in this one process,\nso use half the rate of the hardware graph.")
    B += [
        blk("noise", "variable_qtgui_range", (680, 8), label="Channel noise voltage", rangeType="float",
            value="0.05", start="0", stop="0.6", step="0.01", widget="counter_slider", gui_hint="0,0,1,2"),
        blk("cfo_hz", "variable_qtgui_range", (900, 8), label="Carrier offset between radios (Hz)",
            rangeType="float", value="3000", start="-15000", stop="15000", step="500",
            widget="counter_slider", gui_hint="1,0,1,2"),
        epy("alice_tx", EPY_TX, (40, 220), **args(1, "Alice", 8081)),
        epy("bob_tx", EPY_TX, (40, 420), **args(2, "Bob", 8082)),
        blk("air_sum", "blocks_add_xx", (400, 330), type="complex", num_inputs="2"),
        blk("throttle", "blocks_throttle", (560, 340), type="complex", samples_per_second="samp_rate",
            ignoretag="True"),
        blk("channel", "channels_channel_model", (760, 310), noise_voltage="noise",
            freq_offset="cfo_hz/samp_rate", epsilon="1.0", taps="[1.0+0j]", seed="0", block_tags="False"),
        epy("alice_rx", EPY_RX, (1080, 220), **args(1, "Alice", 8081)),
        epy("bob_rx", EPY_RX, (1080, 420), **args(2, "Bob", 8082)),
        spectrum("air_spectrum", (1080, 600), "Shared channel spectrum", "2,0,1,2", fc="0"),
        blk("air_scope", "qtgui_time_sink_x", (1080, 760), type="complex", name='"Shared channel (time)"',
            size="40000", srate="samp_rate", ymin="-1.5", ymax="1.5", update_time="0.2", gui_hint="3,0,1,2",
            label1="'I'", label2="'Q'", entags="False"),
    ]
    C = [("alice_tx", 0, "air_sum", 0), ("bob_tx", 0, "air_sum", 1), ("air_sum", 0, "throttle", 0),
         ("throttle", 0, "channel", 0), ("channel", 0, "alice_rx", 0), ("channel", 0, "bob_rx", 0),
         ("channel", 0, "air_spectrum", 0), ("channel", 0, "air_scope", 0)]
    return flowgraph("cdma_two_nodes_sim", "EN2130 DS-CDMA Chat - two stations, simulated channel",
                     "Two full stations over a simulated channel (no hardware)", B, C)


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
    write("cdma_station.grc", station_iio())
    write("cdma_station_uhd.grc", station_uhd())
    write("cdma_two_nodes_sim.grc", two_nodes_sim())
    write("cdma_phy_concept.grc", phy_concept())
