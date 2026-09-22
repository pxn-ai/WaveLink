import sys
import socket
import time
import os
import subprocess
import shutil
import threading
from PyQt5.QtWidgets import (QApplication, QMainWindow, QTextEdit, QLineEdit, 
                             QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QFileDialog)
from PyQt5.QtCore import QThread, pyqtSignal

class ReceiverThread(QThread):
    message_received = pyqtSignal(str)
    sys_command = pyqtSignal(str)
    
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 5002))
        while True:
            data, _ = sock.recvfrom(2048)
            text = data.decode('utf-8', errors='ignore').replace('\x00', '').strip()
            
            if text.startswith("__SYS:FILE_RX_MODE"):
                self.sys_command.emit(text)
            elif text == "__SYS:CHAT_MODE":
                self.sys_command.emit("CHAT_MODE")
            elif text:
                self.message_received.emit(text)


class WaveLinkApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("WaveLink Chat")
        self.resize(450, 650)
        self.setStyleSheet("background-color: #E5DDD5;") 

        self.tx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.engine_process = None
        self.expected_rx_filename = "received_file.bin"

        # Build UI
        layout = QVBoxLayout()
        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)
        self.chat_history.setStyleSheet("border: none; font-size: 15px; padding: 10px;")

        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("Type a message...")
        self.input_field.returnPressed.connect(self.send_message)
        self.input_field.setStyleSheet("background-color: white; border-radius: 20px; padding: 10px; font-size: 15px;")

        # Control Buttons
        btn_layout = QHBoxLayout()
        
        self.file_button = QPushButton("📎 Send File")
        self.file_button.clicked.connect(self.send_file_dialog)
        self.file_button.setStyleSheet("background-color: #34B7F1; color: white; border-radius: 10px; padding: 10px; font-weight: bold;")
        
        self.return_button = QPushButton("🔄 Return to Chat")
        self.return_button.clicked.connect(self.revert_to_chat)
        self.return_button.setStyleSheet("background-color: #25D366; color: white; border-radius: 10px; padding: 10px; font-weight: bold;")
        
        btn_layout.addWidget(self.file_button)
        btn_layout.addWidget(self.return_button)

        layout.addWidget(self.chat_history)
        layout.addWidget(self.input_field)
        layout.addLayout(btn_layout)
        
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Start Receiver Thread
        self.rx_thread = ReceiverThread()
        self.rx_thread.message_received.connect(self.display_message)
        self.rx_thread.sys_command.connect(self.handle_sys_command)
        self.rx_thread.start()
        
        # Start initial duplex chat flowgraph
        self.switch_flowgraph('bpskrxtx.py')

    def switch_flowgraph(self, script_name):
        if self.engine_process:
            self.engine_process.terminate()
            self.engine_process.wait()
            
        self.chat_history.append(f"<br><div align='center'><span style='background-color:#FFF3CD; color:#856404; padding:5px; border-radius:10px;'>⚙️ System: Switching SDR to {script_name}...</span></div><br>")
        self._scroll_to_bottom()
        
        self.engine_process = subprocess.Popen([sys.executable, '-u', script_name])

    def handle_sys_command(self, cmd):
        if cmd.startswith("__SYS:FILE_RX_MODE"):
            parts = cmd.split(":", 2)
            if len(parts) == 3:
                self.expected_rx_filename = f"rx_{parts[2]}"
            else:
                self.expected_rx_filename = f"rx_file_{int(time.time())}.bin"
                
            self.chat_history.append(f"<br><div align='center'><span style='background-color:#D1ECF1; color:#0C5460; padding:5px; border-radius:10px;'>📥 Incoming file detected. SDR locked to RX Mode.</span></div><br>")
            self._scroll_to_bottom()
            self.switch_flowgraph('BPSK_File_Recieve.py')
            
        elif cmd == "CHAT_MODE":
            self.rename_received_file()
            self.switch_flowgraph('bpskrxtx.py')

    def rename_received_file(self):
        hardcoded_file = "bpsk_receive.jpg"
        if os.path.exists(hardcoded_file):
            new_name = self.expected_rx_filename
            counter = 1
            
            # Prevent overwriting if a file with the same name already exists in the directory
            while os.path.exists(new_name):
                name, ext = os.path.splitext(self.expected_rx_filename)
                new_name = f"{name}_{counter}{ext}"
                counter += 1
                
            try:
                shutil.move(hardcoded_file, new_name)
                self.chat_history.append(f"<br><div align='center'><span style='background-color:#D4EDDA; color:#155724; padding:5px; border-radius:10px;'>✅ File successfully saved as {new_name}</span></div><br>")
            except Exception as e:
                self.chat_history.append(f"<br><div align='center'><span style='background-color:#F8D7DA; color:#721C24; padding:5px; border-radius:10px;'>❌ Error saving file: {e}</span></div><br>")
            self._scroll_to_bottom()

    def send_file_dialog(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Select File")
        if filepath:
            # Copy chosen file to a static buffer file that GNU Radio expects
            shutil.copy(filepath, "transmit_buffer.bin")
            
            filename = os.path.basename(filepath)
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;⏳ Handshake Sent. Sending {filename}...&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            self._scroll_to_bottom()
            
            threading.Thread(target=self.initiate_file_transfer, args=(filename,), daemon=True).start()

    def initiate_file_transfer(self, filename):
        # Command remote machine to switch to RX mode, sending the filename
        padded_cmd = f"__SYS:FILE_RX_MODE:{filename}".ljust(1024, ' ')
        self.tx_socket.sendto(padded_cmd.encode('utf-8'), ("127.0.0.1", 5001))
        
        # Wait 1 second to ensure the command transmits over the air
        time.sleep(1.0)
        self.switch_flowgraph('BPSK_File_Transfer.py')

    def revert_to_chat(self):
        self.switch_flowgraph('bpskrxtx.py')
        
        def delayed_revert_trigger():
            # Give the SDR 3.5 seconds to fully initialize and lock the hardware
            time.sleep(3.5) 
            padded_cmd = "__SYS:CHAT_MODE".ljust(1024, ' ')
            self.tx_socket.sendto(padded_cmd.encode('utf-8'), ("127.0.0.1", 5001))
            
            time.sleep(0.05)
            dummy = " ".ljust(1024, ' ')
            self.tx_socket.sendto(dummy.encode('utf-8'), ("127.0.0.1", 5001))
            
        threading.Thread(target=delayed_revert_trigger, daemon=True).start()

    def send_message(self):
        text = self.input_field.text()
        if text:
            padded_text = (text + '\n').ljust(1024, ' ')
            self.tx_socket.sendto(padded_text.encode('utf-8'), ("127.0.0.1", 5001))
            
            time.sleep(0.05)
            
            dummy = " ".ljust(1024, ' ')
            self.tx_socket.sendto(dummy.encode('utf-8'), ("127.0.0.1", 5001))
            
            bubble = f"<table width='100%'><tr><td align='right'><span style='background-color:#DCF8C6; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
            self.chat_history.append(bubble)
            self.input_field.clear()
            self._scroll_to_bottom()

    def display_message(self, text):
        bubble = f"<table width='100%'><tr><td align='left'><span style='background-color:#FFFFFF; color:black; font-size:16px;'>&nbsp;&nbsp;{text}&nbsp;&nbsp;</span></td></tr></table>"
        self.chat_history.append(bubble)
        self._scroll_to_bottom()
        
    def _scroll_to_bottom(self):
        scrollbar = self.chat_history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def closeEvent(self, event):
        if self.engine_process:
            self.engine_process.terminate()
            self.engine_process.wait()
        super().closeEvent(event)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = WaveLinkApp()
    window.show()
    sys.exit(app.exec_())
