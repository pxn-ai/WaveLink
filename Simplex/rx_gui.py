import sys
import os
import pmt
import zmq
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from BPSK_Recieve_RTL_GUI import BPSK_Recieve_RTL_GUI

class CustomRxGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WaveLink BPSK Receiver")
        self.resize(1200, 800)
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)
        
        # 1. Instantiate the GNU Radio flowgraph widget
        self.tb = BPSK_Recieve_RTL_GUI()
        
        # Add the GNU Radio widget to the left side
        layout.addWidget(self.tb, stretch=2)
        
        # 2. Create the custom Chat panel on the right side
        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)
        
        chat_label = QtWidgets.QLabel("Received Messages")
        chat_label.setFont(QtGui.QFont("Arial", 16, QtGui.QFont.Bold))
        chat_layout.addWidget(chat_label)
        
        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setStyleSheet("background-color: #f8f9fa;")
        chat_layout.addWidget(self.chat_history)
        
        layout.addWidget(chat_panel, stretch=1)
        
        # 3. Setup ZeroMQ connection to the flowgraph
        self.zmq_context = zmq.Context()
        self.zmq_sock = self.zmq_context.socket(zmq.PULL)
        # We use bind here because the flowgraph's zeromq_push_msg_sink uses bind: 'False'
        self.zmq_sock.bind("tcp://127.0.0.1:5002")
        
        # 4. QTimer to poll ZMQ
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.poll_zmq)
        self.timer.start(100) # Poll every 100ms
        
        # 5. Start the GNU Radio flowgraph
        self.tb.start()
        
    def poll_zmq(self):
        try:
            while True:
                msg = self.zmq_sock.recv(zmq.NOBLOCK)
                p = pmt.deserialize_str(msg)
                
                # The python block in GRC outputs PMT symbols containing the string
                if pmt.is_symbol(p):
                    text = pmt.symbol_to_string(p)
                elif pmt.is_string(p):
                    text = pmt.string_to_python(p)
                else:
                    text = str(p)
                    
                if text.strip():
                    self.chat_history.append(f"Received: {text}")
                    # Auto-scroll to bottom
                    scrollbar = self.chat_history.verticalScrollBar()
                    scrollbar.setValue(scrollbar.maximum())
                    
        except zmq.Again:
            pass # No messages available

    def closeEvent(self, event):
        self.timer.stop()
        self.tb.stop()
        self.tb.wait()
        self.zmq_sock.close()
        self.zmq_context.term()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    from gnuradio import qtgui
    qtgui.util.check_set_qss()
    gui = CustomRxGUI()
    gui.show()
    sys.exit(app.exec_())
