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
            text = data.decode('utf-8', errors='ignore').strip()
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
            # 1. Pad the real message to 1024 bytes
            padded_text = (text + '\n').ljust(1024, ' ')
            self.tx_socket.sendto(padded_text.encode('utf-8'), ("127.0.0.1", 5001))
            
            # 2. Send the dummy flush packet to push it through the DSP filters
            dummy = " ".ljust(1024, ' ')
            self.tx_socket.sendto(dummy.encode('utf-8'), ("127.0.0.1", 5001))
            
            # 3. Append right-aligned green bubble style to UI
            self.chat_history.append(f"<div style='text-align: right; color: #075E54;'><b>You:</b> {text}</div><br>")
            self.input_field.clear()

    def display_message(self, text):
        # Append left-aligned black text style to UI
        self.chat_history.append(f"<div style='text-align: left; color: #333333;'><b>RX:</b> {text}</div><br>")

    def closeEvent(self, event):
        self.engine_process.terminate()
        self.engine_process.wait()
        super().closeEvent(event)

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WaveLinkApp()
    window.show()
    sys.exit(app.exec_())
