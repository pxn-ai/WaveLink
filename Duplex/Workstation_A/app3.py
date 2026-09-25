import sys
import socket
import os
import threading
import time
from pathlib import Path
from PyQt5.QtWidgets import QApplication, QMainWindow, QTextEdit, QLineEdit, QVBoxLayout, QWidget, QPushButton, QFileDialog
from PyQt5.QtCore import QThread, pyqtSignal
import subprocess

class ReceiverThread(QThread):
    message_received = pyqtSignal(str)
    
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 5002))
        
        # Since we removed file headers, we dump all received raw bytes to a single binary stream
        raw_output_file = open("rx_raw_stream.bin", "wb")

        while True:
            data, _ = sock.recvfrom(2048)
            if not data: continue
            
            # Write raw binary data directly to disk
            raw_output_file.write(data)
            raw_output_file.flush()
            
            # Attempt to decode as text for the UI chat (ignore raw binary file chunks)
            try:
                text = data.decode('utf-8').replace('\x00', '').strip()
                if text:
                    self.message_received.emit(text)
            except UnicodeDecodeError:
                pass # Silent pass for pure binary file data

class WaveLinkApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WaveLink Chat")
        self.resize(400, 600)
        self.setStyleSheet("background-color: #E5DDD5;") 

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
        self.file_button = QPushButton("📎 Send Raw File")
        self.file_button.clicked.connect(self.send_file_dialog)
        self.file_button.setStyleSheet("background-color: #34B7F1; color: white; border-radius: 10px; padding: 10px; font-weight: bold;")
        layout.addWidget(self.file_button)
        
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Start GNU Radio Flowgraph
        flowgraph = Path(__file__).with_name('bpskrxtx.py')
        self.engine_process = subprocess.Popen([sys.executable, '-u', str(flowgraph)])

        # Start Receiver Thread
        self.rx_thread = ReceiverThread()
        self.rx_thread.message_received.connect(self.display_message)
        self.rx_thread.start()

    def send_message(self):
        text = self.input_field.text()
        if text:
            # Send text padded to exactly 1024 bytes
            packet = (text + '\n').encode('utf-8')
            if len(packet) < 1024:
                packet += b'\x00' * (1024 - len(packet))
                
            self.tx_socket.sendto(packet[:1024], ("127.0.0.1", 5001))
            
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            self.input_field.clear()
            
            scrollbar = self.chat_history.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def transmit_file(self, filepath):
        # Purely packetize the binary file into 1024-byte chunks. No headers, no metadata.
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(1024)
                if not chunk:
                    break
                
                # Pad the final chunk to exactly 1024 bytes to satisfy the Stream to Tagged Stream block
                if len(chunk) < 1024:
                    chunk += b'\x00' * (1024 - len(chunk))
                    
                self.tx_socket.sendto(chunk, ("127.0.0.1", 5001))
                time.sleep(0.08) # Rate limiting to prevent UDP buffer overflow
                
        # Flush the SDR hardware buffer safely at the END of the transmission
        for _ in range(10):
            self.tx_socket.sendto(b'\x00' * 1024, ("127.0.0.1", 5001))
            time.sleep(0.02)

    def display_message(self, text):
        bubble = f"<table width='100%'><tr><td align='left'><span style='background-color:#FFFFFF; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
        self.chat_history.append(bubble)
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
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;⏳ Sending {filename} (Raw Binary)...&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            threading.Thread(target=self.transmit_file, args=(filepath,), daemon=True).start()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WaveLinkApp()
    window.show()
    sys.exit(app.exec_())