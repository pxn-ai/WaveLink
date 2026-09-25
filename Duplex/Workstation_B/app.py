import sys
import socket
import base64
import os
import threading
import time
from pathlib import Path
from PyQt5.QtWidgets import QApplication, QMainWindow, QTextEdit, QLineEdit, QVBoxLayout, QWidget, QPushButton, QFileDialog
from PyQt5.QtCore import QThread, pyqtSignal
import subprocess

# Background thread to listen for incoming UDP packets from the GNU Radio RX chain
class ReceiverThread(QThread):
    message_received = pyqtSignal(str)
    file_received = pyqtSignal(str)
    
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 5003))
        
        receiving_file = False
        file_handle = None
        expected_seq = 0
        filename = ""

        while True:
            data, _ = sock.recvfrom(2048)
            if len(data) < 1: continue
            
            magic_byte = data[0:1]
            payload = data[1:]
            
            # 0x01: Text Message
            if magic_byte == b'\x01':  
                text = payload.decode('utf-8', errors='ignore').replace('\x00', '').strip()
                if text:
                    self.message_received.emit(text)
                    
            # 0x02: File Start
            elif magic_byte == b'\x02':  
                filename = payload.decode('utf-8', errors='ignore').replace('\x00', '').strip()
                if filename:
                    receiving_file = True
                    expected_seq = 0
                    file_handle = open(f"rx_{filename}", "wb")
                    self.message_received.emit(f"📥 Receiving file: {filename}...")
                    
            # 0x03: Binary File Chunk
            elif magic_byte == b'\x03' and receiving_file:  
                seq_num = int.from_bytes(payload[0:4], byteorder='big')
                chunk_len = int.from_bytes(payload[4:6], byteorder='big')
                chunk_data = payload[6:6+chunk_len]
                
                # Handle dropped packets by padding with structural zeros
                if seq_num > expected_seq:
                    missing_chunks = seq_num - expected_seq
                    file_handle.write(b'\x00' * (1017 * missing_chunks))
                    
                file_handle.write(chunk_data)
                expected_seq = seq_num + 1
                
            # 0x04: File End
            elif magic_byte == b'\x04' and receiving_file:  
                receiving_file = False
                if file_handle:
                    file_handle.close()
                self.file_received.emit(filename)

            # Workstation A currently sends plain padded text packets.
            elif not receiving_file:
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
        self.file_button = QPushButton("📎 Send File")
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
        self.rx_thread.file_received.connect(self.display_file_notification)
        self.rx_thread.start()

    def _tx_raw(self, magic_byte, data_bytes):
        packet = magic_byte + data_bytes
        if len(packet) < 1024:
            packet += b'\x00' * (1024 - len(packet))
        packet = packet[:1024]
        
        self.tx_socket.sendto(packet, ("127.0.0.1", 5004))
        time.sleep(0.02)
        
        dummy = b'\x00' * 1024
        self.tx_socket.sendto(dummy, ("127.0.0.1", 5004))

    def send_message(self):
        text = self.input_field.text()
        if text:
            self._tx_raw(b'\x01', (text + '\n').encode('utf-8'))
            
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            self.input_field.clear()
            
            scrollbar = self.chat_history.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def transmit_file(self, filepath):
        filename = os.path.basename(filepath)
        self._tx_raw(b'\x02', filename.encode('utf-8'))
        time.sleep(0.1) 
        
        seq_num = 0
        chunk_size = 1017 
        
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                
                seq_bytes = seq_num.to_bytes(4, byteorder='big')
                len_bytes = len(chunk).to_bytes(2, byteorder='big')
                self._tx_raw(b'\x03', seq_bytes + len_bytes + chunk)
                
                seq_num += 1
                time.sleep(0.08) 
                
        self._tx_raw(b'\x04', b'')
        
        # Flush the SDR hardware buffer
        for _ in range(10):
            self.tx_socket.sendto(b'\x00' * 1024, ("127.0.0.1", 5004))
            time.sleep(0.02)

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
