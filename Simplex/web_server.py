import sys
# Add GNU Radio system paths so we can import pmt
sys.path.append("/opt/homebrew/lib/python3.14/site-packages")

import asyncio
import subprocess
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import zmq
import zmq.asyncio
import pmt
import xmlrpc.client
import numpy as np
import json
import os

app = FastAPI()

# Make sure static directory exists
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

# XMLRPC Clients for controlling flowgraphs
tx_rpc = xmlrpc.client.ServerProxy("http://127.0.0.1:8081")
rx_rpc = xmlrpc.client.ServerProxy("http://127.0.0.1:8082")

# ZMQ Context
ctx = zmq.asyncio.Context()

# Global process handles
processes = []

@app.on_event("startup")
async def startup_event():
    # Start GNU Radio Headless scripts
    env = os.environ.copy()
    # We use the system python (which has gnuradio) for the subprocesses
    p_tx = subprocess.Popen(["/opt/homebrew/bin/python3.14", "BPSK_Transmission_Headless.py"], env=env)
    p_rx = subprocess.Popen(["/opt/homebrew/bin/python3.14", "BPSK_Recieve_RTL_Headless.py"], env=env)
    processes.extend([p_tx, p_rx])
    
    # Start background tasks to poll ZMQ
    asyncio.create_task(poll_rx_chat())
    asyncio.create_task(poll_tx_iq())
    asyncio.create_task(poll_rx_iq())

@app.on_event("shutdown")
async def shutdown_event():
    for p in processes:
        p.terminate()

# ZMQ PUSH for TX Chat
tx_chat_push = ctx.socket(zmq.PUSH)
tx_chat_push.bind("tcp://127.0.0.1:5001")

# WebSocket connections
active_connections = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg['type'] == 'chat':
                text = msg['text']
                # Send via ZMQ PUSH to TX flowgraph
                pmt_msg = pmt.cons(pmt.PMT_NIL, pmt.make_dict())
                pmt_msg = pmt.dict_add(pmt_msg, pmt.intern("text"), pmt.intern(text))
                await tx_chat_push.send(pmt.serialize_str(pmt_msg))
            elif msg['type'] == 'control':
                # Handle sliders via XMLRPC
                if msg['param'] == 'tx_atten':
                    try: tx_rpc.set_tx_atten(float(msg['value']))
                    except Exception as e: print("XMLRPC Error TX:", e)
                elif msg['param'] == 'rfGain':
                    try: rx_rpc.set_rfGain(int(msg['value']))
                    except Exception as e: print("XMLRPC Error RX:", e)
    except WebSocketDisconnect:
        active_connections.remove(websocket)

async def broadcast(data):
    for connection in active_connections:
        try:
            await connection.send_text(json.dumps(data))
        except Exception:
            pass

async def poll_rx_chat():
    rx_chat_pull = ctx.socket(zmq.PULL)
    rx_chat_pull.bind("tcp://127.0.0.1:5002")
    while True:
        msg = await rx_chat_pull.recv()
        try:
            pmt_msg = pmt.deserialize_str(msg)
            text_pmt = pmt.dict_ref(pmt_msg, pmt.intern("text"), pmt.PMT_NIL)
            if not pmt.is_null(text_pmt):
                text = pmt.symbol_to_string(text_pmt)
                await broadcast({'type': 'rx_chat', 'text': text})
        except Exception as e:
            print("Error parsing RX chat:", e)

def process_iq(msg):
    data = np.frombuffer(msg, dtype=np.complex64)
    if len(data) == 0: return None
    # Downsample
    if len(data) > 1024:
        data = data[-1024:] # take latest 1024 samples
    
    # Calculate FFT
    fft = np.fft.fftshift(np.fft.fft(data))
    fft_mag = 20 * np.log10(np.abs(fft) + 1e-12)
    
    return {
        'time_real': data.real.tolist(),
        'time_imag': data.imag.tolist(),
        'fft': fft_mag.tolist(),
    }

async def poll_tx_iq():
    sub = ctx.socket(zmq.SUB)
    sub.connect("tcp://127.0.0.1:5003")
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    # Use CONFLATE to keep only the latest message to avoid backlog
    sub.setsockopt(zmq.CONFLATE, 1)
    
    while True:
        msg = await sub.recv()
        processed = process_iq(msg)
        if processed:
            processed['type'] = 'tx_iq'
            await broadcast(processed)
        await asyncio.sleep(0.05) # ~20 FPS

async def poll_rx_iq():
    sub = ctx.socket(zmq.SUB)
    sub.connect("tcp://127.0.0.1:5004")
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.setsockopt(zmq.CONFLATE, 1)
    
    while True:
        msg = await sub.recv()
        processed = process_iq(msg)
        if processed:
            processed['type'] = 'rx_iq'
            await broadcast(processed)
        await asyncio.sleep(0.05)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
