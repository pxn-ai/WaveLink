import sys
import socket
from PyQt5.QtWidgets import QApplication, QMainWindow, QTextEdit, QLineEdit, QVBoxLayout, QWidget
from PyQt5.QtCore import QThread, pyqtSignal
import subprocess

# Background thread to listen for incoming UDP packets from the GNU Radio RX chain
class ReceiverThread(QThread):
    message_received = pyqtSignal(str)
    
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 5002))
        while True:
            data, _ = sock.recvfrom(2048)
            # Strip null bytes AND whitespace
            text = data.decode('utf-8', errors='ignore').replace('\x00', '').strip()
            if text:
                self.message_received.emit(text)

class WaveLinkApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WaveLink Chat")
        self.resize(400, 600)
        self.setStyleSheet("background-color: #E5DDD5;") # WhatsApp default background

        self.tx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Build UI
        layout = QVBoxLayout()
        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setStyleSheet("border: none; font-size: 15px; padding: 10px;")

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type a message...")
        self.input_field.returnPressed.connect(self.send_message)
        self.input_field.setStyleSheet("background-color: white; border-radius: 20px; padding: 10px; font-size: 15px;")

        layout.addWidget(self.chat_history)
        layout.addWidget(self.input_field)
        
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Start GNU Radio Flowgraph
        self.engine_process = subprocess.Popen([sys.executable, '-u', 'bpskrxtx.py'])

        # Start Receiver Thread
        self.rx_thread = ReceiverThread()
        self.rx_thread.message_received.connect(self.display_message)
        self.rx_thread.start()

    def send_message(self):
        text = self.input_field.text()
        if text:
            # Pad to 1024 to match GNU Radio's default UDP MTU
            padded_text = (text + '\n').ljust(1024, ' ')
            self.tx_socket.sendto(padded_text.encode('utf-8'), ("127.0.0.1", 5001))
            
            dummy = " ".ljust(1024, ' ')
            self.tx_socket.sendto(dummy.encode('utf-8'), ("127.0.0.1", 5001))
            
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            self.input_field.clear()
            
            # Force auto-scroll to bottom
            scrollbar = self.chat_history.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

            bubble = f"<table width='100%'><tr><td align='left'><span style='background-color:#FFFFFF; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            
        # Force auto-scroll to bottom
        scrollbar = self.chat_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event):
        self.engine_process.terminate()
        self.engine_process.wait()
        super().closeEvent(event)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WaveLinkApp()
    window.show()
    sys.exit(app.exec_())
