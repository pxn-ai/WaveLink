import sys
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

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("static/rx_index.html")

# XMLRPC Client
rx_rpc = xmlrpc.client.ServerProxy("http://127.0.0.1:8082")

ctx = zmq.asyncio.Context()
processes = []

@app.on_event("startup")
async def startup_event():
    env = os.environ.copy()
    p_rx = subprocess.Popen(["/opt/homebrew/bin/python3.14", "BPSK_Recieve_RTL_Headless.py"], env=env)
    processes.append(p_rx)
    asyncio.create_task(poll_rx_chat())
    asyncio.create_task(poll_rx_iq())

@app.on_event("shutdown")
async def shutdown_event():
    for p in processes:
        p.terminate()

active_connections = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg['type'] == 'control':
                if msg['param'] == 'rfGain':
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
            # Safely extract text from PMT message
            pmt_msg = pmt.deserialize_str(msg)
            # Depending on how the msg is constructed natively, it might be a dict or a pair
            if pmt.is_pair(pmt_msg):
                pmt_dict = pmt.car(pmt_msg)
                text_pmt = pmt.dict_ref(pmt_dict, pmt.intern("text"), pmt.PMT_NIL)
            else:
                text_pmt = pmt.dict_ref(pmt_msg, pmt.intern("text"), pmt.PMT_NIL)
                
            if not pmt.is_null(text_pmt):
                text = pmt.symbol_to_string(text_pmt)
                await broadcast({'type': 'rx_chat', 'text': text})
        except Exception as e:
            print("Error parsing RX chat:", e)

def process_iq(msg):
    data = np.frombuffer(msg, dtype=np.complex64)
    if len(data) == 0: return None
    if len(data) > 1024:
        data = data[-1024:]
    fft = np.fft.fftshift(np.fft.fft(data))
    fft_mag = 20 * np.log10(np.abs(fft) + 1e-12)
    return {
        'type': 'rx_iq',
        'time_real': data.real.tolist(),
        'time_imag': data.imag.tolist(),
        'fft': fft_mag.tolist(),
    }

async def poll_rx_iq():
    sub = ctx.socket(zmq.SUB)
    sub.connect("tcp://127.0.0.1:5004")
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.setsockopt(zmq.CONFLATE, 1)
    
    while True:
        msg = await sub.recv()
        processed = process_iq(msg)
        if processed:
            await broadcast(processed)
        await asyncio.sleep(0.05)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port)
