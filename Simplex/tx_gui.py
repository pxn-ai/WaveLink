import sys
import os
import pmt
import zmq
from PyQt5 import QtWidgets, QtCore, QtGui
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLineEdit, QPushButton

# Ensure the local directory is in path to import the generated python files
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from BPSK_Transmission_GUI import BPSK_Transmission_GUI

class CustomTxGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WaveLink BPSK Transmitter")
        self.resize(1200, 800)
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QHBoxLayout(main_widget)
        
        # 1. Instantiate the GNU Radio flowgraph widget
        self.tb = BPSK_Transmission_GUI()
        
        # Add the GNU Radio widget to the left side
        layout.addWidget(self.tb, stretch=2)
        
        # 2. Create the custom Chat panel on the right side
        chat_panel = QWidget()
        chat_layout = QVBoxLayout(chat_panel)
        
        chat_label = QtWidgets.QLabel("Chat Transmitter")
        chat_label.setFont(QtGui.QFont("Arial", 16, QtGui.QFont.Bold))
        chat_layout.addWidget(chat_label)
        
        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setStyleSheet("background-color: #f0f0f0;")
        chat_layout.addWidget(self.chat_history)
        
        input_layout = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Enter message to transmit...")
        self.input_field.returnPressed.connect(self.send_msg)
        input_layout.addWidget(self.input_field)
        
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self.send_msg)
        input_layout.addWidget(self.send_btn)
        
        chat_layout.addLayout(input_layout)
        layout.addWidget(chat_panel, stretch=1)
        
        # 3. Setup ZeroMQ connection to the flowgraph
        self.zmq_context = zmq.Context()
        self.zmq_sock = self.zmq_context.socket(zmq.PUSH)
        # We use bind here because the flowgraph's zeromq_pull_msg_source uses bind: 'False'
        self.zmq_sock.bind("tcp://127.0.0.1:5001")
        
        # 4. Start the GNU Radio flowgraph
        self.tb.start()
        
    def send_msg(self):
        text = self.input_field.text().strip()
        if text:
            # Display locally
            self.chat_history.append(f"You: {text}")
            self.input_field.clear()
            
            # Send to GNU Radio over ZMQ
            try:
                p = pmt.intern(text)
                msg = pmt.serialize_str(p)
                self.zmq_sock.send(msg)
            except Exception as e:
                self.chat_history.append(f"[Error sending ZMQ]: {e}")

    def closeEvent(self, event):
        self.tb.stop()
        self.tb.wait()
        self.zmq_sock.close()
        self.zmq_context.term()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    from gnuradio import qtgui
    qtgui.util.check_set_qss()
    gui = CustomTxGUI()
    gui.show()
    sys.exit(app.exec_())
