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
    return FileResponse("static/tx_index.html")

# XMLRPC Client
tx_rpc = xmlrpc.client.ServerProxy("http://127.0.0.1:8081")

ctx = zmq.asyncio.Context()
processes = []

@app.on_event("startup")
async def startup_event():
    env = os.environ.copy()
    p_tx = subprocess.Popen(["/opt/homebrew/bin/python3.14", "BPSK_Transmission_Headless.py"], env=env)
    processes.append(p_tx)
    asyncio.create_task(poll_tx_iq())

@app.on_event("shutdown")
async def shutdown_event():
    for p in processes:
        p.terminate()

# ZMQ PUSH for TX Chat
tx_chat_push = ctx.socket(zmq.PUSH)
tx_chat_push.bind("tcp://127.0.0.1:5001")

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
                # Correctly form PMT dictionary
                pmt_dict = pmt.make_dict()
                pmt_dict = pmt.dict_add(pmt_dict, pmt.intern("text"), pmt.intern(text))
                pmt_msg = pmt.cons(pmt_dict, pmt.make_u8vector(0, 0)) # Standard PMT message format
                await tx_chat_push.send(pmt.serialize_str(pmt_msg))
            elif msg['type'] == 'control':
                if msg['param'] == 'tx_atten':
                    try: tx_rpc.set_tx_atten(float(msg['value']))
                    except Exception as e: print("XMLRPC Error TX:", e)
    except WebSocketDisconnect:
        active_connections.remove(websocket)

async def broadcast(data):
    for connection in active_connections:
        try:
            await connection.send_text(json.dumps(data))
        except Exception:
            pass

def process_iq(msg):
    data = np.frombuffer(msg, dtype=np.complex64)
    if len(data) == 0: return None
    if len(data) > 1024:
        data = data[-1024:]
    fft = np.fft.fftshift(np.fft.fft(data))
    fft_mag = 20 * np.log10(np.abs(fft) + 1e-12)
    return {
        'type': 'tx_iq',
        'time_real': data.real.tolist(),
        'time_imag': data.imag.tolist(),
        'fft': fft_mag.tolist(),
    }

async def poll_tx_iq():
    sub = ctx.socket(zmq.SUB)
    sub.connect("tcp://127.0.0.1:5003")
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
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host="0.0.0.0", port=args.port)
