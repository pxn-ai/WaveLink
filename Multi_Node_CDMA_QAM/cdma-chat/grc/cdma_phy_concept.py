#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: EN2130 DS-CDMA concept: spreading and despreading
# Author: EN2130 group
# Description: Two-user DS-CDMA with stock blocks
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
from gnuradio import blocks
import numpy
from gnuradio import digital
from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
import sip
import threading



class cdma_phy_concept(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "EN2130 DS-CDMA concept: spreading and despreading", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("EN2130 DS-CDMA concept: spreading and despreading")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "cdma_phy_concept")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.user2_gain = user2_gain = 1.0
        self.sf = sf = 31
        self.samp_rate = samp_rate = 1e6
        self.noise_amp = noise_amp = 1.0
        self.code3 = code3 = [-1, 1, -1, -1, 1, -1, 1, -1, -1, 1, 1, 1, -1, 1, 1, -1, 1, -1, -1, 1, -1, -1, 1, 1, -1, -1, 1, 1, -1, -1, 1]
        self.code2 = code2 = [-1, -1, 1, -1, -1, -1, -1, -1, -1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, -1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1]
        self.code1 = code1 = [-1, -1, -1, -1, -1, 1, 1, -1, 1, 1, -1, -1, 1, 1, 1, 1, -1, 1, -1, -1, 1, -1, 1, -1, 1, 1, 1, -1, -1, -1, 1]

        ##################################################
        # Blocks
        ##################################################

        self._user2_gain_range = qtgui.Range(0, 5, 0.1, 1.0, 200)
        self._user2_gain_win = qtgui.RangeWidget(self._user2_gain_range, self.set_user2_gain, "User 2 amplitude (near-far)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._user2_gain_win, 1, 0, 1, 2)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._noise_amp_range = qtgui.Range(0, 5, 0.1, 1.0, 200)
        self._noise_amp_win = qtgui.RangeWidget(self._noise_amp_range, self.set_noise_amp, "Noise amplitude", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._noise_amp_win, 0, 0, 1, 2)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.u2_spread = blocks.multiply_vcc(1)
        self.u2_repeat = blocks.repeat(gr.sizeof_gr_complex*1, sf)
        self.u2_level = blocks.multiply_const_cc(user2_gain)
        self.u2_code = blocks.vector_source_c(code2, True, 1, [])
        self.u2_bpsk = digital.chunks_to_symbols_bc([-1, 1], 1)
        self.u2_bits = blocks.vector_source_b(list(map(int, numpy.random.randint(0, 2, 10000))), True)
        self.u1_spread = blocks.multiply_vcc(1)
        self.u1_repeat = blocks.repeat(gr.sizeof_gr_complex*1, sf)
        self.u1_code = blocks.vector_source_c(code1, True, 1, [])
        self.u1_bpsk = digital.chunks_to_symbols_bc([-1, 1], 1)
        self.u1_bits = blocks.vector_source_b(list(map(int, numpy.random.randint(0, 2, 10000))), True)
        self.spec = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            samp_rate, #bw
            "Narrowband BPSK (user 1) vs spread signal on air", #name
            2,
            None # parent
        )
        self.spec.set_update_time(0.10)
        self.spec.set_y_axis((-120), (-10))
        self.spec.set_y_label('Relative Gain', 'dB')
        self.spec.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.spec.enable_autoscale(False)
        self.spec.enable_grid(True)
        self.spec.set_fft_average(0.2)
        self.spec.enable_axis_labels(True)
        self.spec.enable_control_panel(False)
        self.spec.set_fft_window_normalized(False)



        labels = ['BPSK before spreading', 'On air (2 users + noise)', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(2):
            if len(labels[i]) == 0:
                self.spec.set_line_label(i, "Data {0}".format(i))
            else:
                self.spec.set_line_label(i, labels[i])
            self.spec.set_line_width(i, widths[i])
            self.spec.set_line_color(i, colors[i])
            self.spec.set_line_alpha(i, alphas[i])

        self._spec_win = sip.wrapinstance(self.spec.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._spec_win, 2, 0, 1, 2)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.noise_src = analog.noise_source_c(analog.GR_GAUSSIAN, noise_amp, 42)
        self.d3_norm = blocks.multiply_const_cc(1/sf)
        self.d3_mix = blocks.multiply_vcc(1)
        self.d3_dump = blocks.integrate_cc(sf, 1)
        self.d3_code = blocks.vector_source_c(code3, True, 1, [])
        self.d2_norm = blocks.multiply_const_cc(1/sf)
        self.d2_mix = blocks.multiply_vcc(1)
        self.d2_dump = blocks.integrate_cc(sf, 1)
        self.d2_code = blocks.vector_source_c(code2, True, 1, [])
        self.d1_norm = blocks.multiply_const_cc(1/sf)
        self.d1_mix = blocks.multiply_vcc(1)
        self.d1_dump = blocks.integrate_cc(sf, 1)
        self.d1_code = blocks.vector_source_c(code1, True, 1, [])
        self.const = qtgui.const_sink_c(
            512, #size
            "Despread symbols: right code vs wrong code", #name
            3, #number of inputs
            None # parent
        )
        self.const.set_update_time(0.10)
        self.const.set_y_axis((-2.5), 2.5)
        self.const.set_x_axis((-2.5), 2.5)
        self.const.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.const.enable_autoscale(False)
        self.const.enable_grid(True)
        self.const.enable_axis_labels(True)


        labels = ['despread with code 1 (user 1)', 'despread with code 2 (user 2)', 'despread with code 3 (nobody)', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(3):
            if len(labels[i]) == 0:
                self.const.set_line_label(i, "Data {0}".format(i))
            else:
                self.const.set_line_label(i, labels[i])
            self.const.set_line_width(i, widths[i])
            self.const.set_line_color(i, colors[i])
            self.const.set_line_style(i, styles[i])
            self.const.set_line_marker(i, markers[i])
            self.const.set_line_alpha(i, alphas[i])

        self._const_win = sip.wrapinstance(self.const.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._const_win, 3, 0, 2, 2)
        for r in range(3, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.chips_scope = qtgui.time_sink_c(
            310, #size
            samp_rate, #samp_rate
            "Chips on air (noise-like)", #name
            1, #number of inputs
            None # parent
        )
        self.chips_scope.set_update_time(0.2)
        self.chips_scope.set_y_axis(-4, 4)

        self.chips_scope.set_y_label('Amplitude', "")

        self.chips_scope.enable_tags(False)
        self.chips_scope.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.chips_scope.enable_autoscale(False)
        self.chips_scope.enable_grid(False)
        self.chips_scope.enable_axis_labels(True)
        self.chips_scope.enable_control_panel(False)
        self.chips_scope.enable_stem_plot(False)


        labels = ['I', 'Q', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(2):
            if len(labels[i]) == 0:
                if (i % 2 == 0):
                    self.chips_scope.set_line_label(i, "Re{{Data {0}}}".format(i/2))
                else:
                    self.chips_scope.set_line_label(i, "Im{{Data {0}}}".format(i/2))
            else:
                self.chips_scope.set_line_label(i, labels[i])
            self.chips_scope.set_line_width(i, widths[i])
            self.chips_scope.set_line_color(i, colors[i])
            self.chips_scope.set_line_style(i, styles[i])
            self.chips_scope.set_line_marker(i, markers[i])
            self.chips_scope.set_line_alpha(i, alphas[i])

        self._chips_scope_win = sip.wrapinstance(self.chips_scope.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._chips_scope_win, 5, 0, 1, 2)
        for r in range(5, 6):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.blocks_throttle2_0 = blocks.throttle( gr.sizeof_gr_complex*1, samp_rate, True, 0 if "auto" == "auto" else max( int(float(0.1) * samp_rate) if "auto" == "time" else int(0.1), 1) )
        self.air = blocks.add_vcc(1)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.air, 0), (self.blocks_throttle2_0, 0))
        self.connect((self.blocks_throttle2_0, 0), (self.chips_scope, 0))
        self.connect((self.blocks_throttle2_0, 0), (self.d1_mix, 0))
        self.connect((self.blocks_throttle2_0, 0), (self.d2_mix, 0))
        self.connect((self.blocks_throttle2_0, 0), (self.d3_mix, 0))
        self.connect((self.blocks_throttle2_0, 0), (self.spec, 1))
        self.connect((self.d1_code, 0), (self.d1_mix, 1))
        self.connect((self.d1_dump, 0), (self.d1_norm, 0))
        self.connect((self.d1_mix, 0), (self.d1_dump, 0))
        self.connect((self.d1_norm, 0), (self.const, 0))
        self.connect((self.d2_code, 0), (self.d2_mix, 1))
        self.connect((self.d2_dump, 0), (self.d2_norm, 0))
        self.connect((self.d2_mix, 0), (self.d2_dump, 0))
        self.connect((self.d2_norm, 0), (self.const, 1))
        self.connect((self.d3_code, 0), (self.d3_mix, 1))
        self.connect((self.d3_dump, 0), (self.d3_norm, 0))
        self.connect((self.d3_mix, 0), (self.d3_dump, 0))
        self.connect((self.d3_norm, 0), (self.const, 2))
        self.connect((self.noise_src, 0), (self.air, 2))
        self.connect((self.u1_bits, 0), (self.u1_bpsk, 0))
        self.connect((self.u1_bpsk, 0), (self.u1_repeat, 0))
        self.connect((self.u1_code, 0), (self.u1_spread, 1))
        self.connect((self.u1_repeat, 0), (self.spec, 0))
        self.connect((self.u1_repeat, 0), (self.u1_spread, 0))
        self.connect((self.u1_spread, 0), (self.air, 0))
        self.connect((self.u2_bits, 0), (self.u2_bpsk, 0))
        self.connect((self.u2_bpsk, 0), (self.u2_repeat, 0))
        self.connect((self.u2_code, 0), (self.u2_spread, 1))
        self.connect((self.u2_level, 0), (self.air, 1))
        self.connect((self.u2_repeat, 0), (self.u2_spread, 0))
        self.connect((self.u2_spread, 0), (self.u2_level, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "cdma_phy_concept")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_user2_gain(self):
        return self.user2_gain

    def set_user2_gain(self, user2_gain):
        self.user2_gain = user2_gain
        self.u2_level.set_k(self.user2_gain)

    def get_sf(self):
        return self.sf

    def set_sf(self, sf):
        self.sf = sf
        self.d1_norm.set_k(1/self.sf)
        self.d2_norm.set_k(1/self.sf)
        self.d3_norm.set_k(1/self.sf)
        self.u1_repeat.set_interpolation(self.sf)
        self.u2_repeat.set_interpolation(self.sf)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.blocks_throttle2_0.set_sample_rate(self.samp_rate)
        self.chips_scope.set_samp_rate(self.samp_rate)
        self.spec.set_frequency_range(0, self.samp_rate)

    def get_noise_amp(self):
        return self.noise_amp

    def set_noise_amp(self, noise_amp):
        self.noise_amp = noise_amp
        self.noise_src.set_amplitude(self.noise_amp)

    def get_code3(self):
        return self.code3

    def set_code3(self, code3):
        self.code3 = code3
        self.d3_code.set_data(self.code3, [])

    def get_code2(self):
        return self.code2

    def set_code2(self, code2):
        self.code2 = code2
        self.d2_code.set_data(self.code2, [])
        self.u2_code.set_data(self.code2, [])

    def get_code1(self):
        return self.code1

    def set_code1(self, code1):
        self.code1 = code1
        self.d1_code.set_data(self.code1, [])
        self.u1_code.set_data(self.code1, [])




def main(top_block_cls=cdma_phy_concept, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
