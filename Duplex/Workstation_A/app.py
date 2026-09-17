import sys
import socket
import base64
import os
import threading
import time
from PyQt5.QtWidgets import QApplication, QMainWindow, QTextEdit, QLineEdit, QVBoxLayout, QWidget, QPushButton, QFileDialog
from PyQt5.QtCore import QThread, pyqtSignal
import subprocess

# Background thread to listen for incoming UDP packets from the GNU Radio RX chain
class ReceiverThread(QThread):
    message_received = pyqtSignal(str)
    file_received = pyqtSignal(str)
    
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 5002))
        
        receiving_file = False
        file_data = ""
        filename = "received_file"

        while True:
            data, _ = sock.recvfrom(2048)
            text = data.decode('utf-8', errors='ignore').replace('\x00', '').strip()
            if not text: continue
                
            if text.startswith("FILE_START:"):
                receiving_file = True
                filename = text.split(":", 1)[1]
                file_data = ""
            elif text.startswith("FILE_CHUNK:") and receiving_file:
                file_data += text.replace("FILE_CHUNK:", "")
            elif text == "FILE_END" and receiving_file:
                receiving_file = False
                try:
                    with open(f"rx_{filename}", "wb") as f:
                        f.write(base64.b64decode(file_data))
                    self.file_received.emit(filename)
                except:
                    self.message_received.emit("❌ File transfer failed.")
            elif not receiving_file:
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

        # Add file send button
        self.file_button = QPushButton("📎 Send File")
        self.file_button.clicked.connect(self.send_file_dialog)
        self.file_button.setStyleSheet("background-color: #34B7F1; color: white; border-radius: 10px; padding: 10px; font-weight: bold;")
        layout.addWidget(self.file_button)
    
        self.rx_thread.file_received.connect(self.display_file_notification)
        
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

    def display_message(self, text):
        bubble = f"<table width='100%'><tr><td align='left'><span style='background-color:#FFFFFF; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
        self.chat_history.append(bubble)
        
        # Force auto-scroll to bottom
        scrollbar = self.chat_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event):
        self.engine_process.terminate()
        self.engine_process.wait()
        super().closeEvent(event)

    def send_file_dialog(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Select File")
        if filepath:
            filename = os.path.basename(filepath)
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;⏳ Sending {filename}...&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            threading.Thread(target=self.transmit_file, args=(filepath,), daemon=True).start()

    def transmit_file(self, filepath):
        filename = os.path.basename(filepath)
        self._tx_padded(f"FILE_START:{filename}")
        time.sleep(0.1) 
        
        with open(filepath, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode('utf-8')
            
        # Chunk into 900 bytes to leave room for headers
        chunk_size = 900
        for i in range(0, len(b64_data), chunk_size):
            self._tx_padded(f"FILE_CHUNK:{b64_data[i:i+chunk_size]}")
            time.sleep(0.08) # Pace the UDP buffer so the SDR doesn't drop packets
            
        self._tx_padded("FILE_END")

    def _tx_padded(self, text):
        padded_text = (text + '\n').ljust(1024, ' ')
        self.tx_socket.sendto(padded_text.encode('utf-8'), ("127.0.0.1", 5001))
        time.sleep(0.02)
        dummy = " ".ljust(1024, ' ')
        self.tx_socket.sendto(dummy.encode('utf-8'), ("127.0.0.1", 5001))

    def display_file_notification(self, filename):
        bubble = f"<table width='100%'><tr><td align='left'><span style='background-color:#FFFFFF; color:black; font-size:16px;'>&nbsp;&nbsp;📁 Received: rx_{filename}&nbsp;&nbsp;</span></td></tr></table>"
        self.chat_history.append(bubble)
        scrollbar = self.chat_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WaveLinkApp()
    window.show()
    sys.exit(app.exec_())
