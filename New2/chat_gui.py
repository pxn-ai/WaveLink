#!/usr/bin/env python3
"""
WaveLink Chat - a WhatsApp-style chat on top of the BPSK radio link.
"""
import argparse, base64, json, os, queue, random, struct, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from duplex_link import Link, MAX_DATA

T_TEXT, T_ACK, T_READ, T_PING, T_FILE = 1, 2, 3, 4, 5
PKT = struct.Struct(">BI")
FILE_PKT = struct.Struct(">BIIHH")        
MAX_TEXT = MAX_DATA - PKT.size            
MAX_FILE_DATA = MAX_DATA - FILE_PKT.size  
RTO0, RTO_MAX, MAX_TRIES = 3.0, 10.0, 10  
PING_S, ONLINE_S = 5.0, 16.0              
MAX_IN_FLIGHT = 20 # NEW: Sliding window limit to prevent radio buffer bloat

HERE = Path(__file__).resolve().parent
DOWNLOADS = HERE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

def rto(tries):
    return min(RTO0 * 1.5 ** max(tries - 1, 0), RTO_MAX)

def split_text(text, limit=MAX_TEXT):
    out, cur, n = [], [], 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if n + b > limit:
            out.append("".join(cur)); cur, n = [], 0
        cur.append(ch); n += b
    if cur: out.append("".join(cur))
    return out

class Chat:
    def __init__(self, name, history_path):
        self.name = name[:32] or "Me"
        self.history_path = Path(history_path)
        self.link = None
        self.lock = threading.RLock()
        self.msgs, self.index = [], {}
        self.file_chunks = {}             
        self.incoming_files = {}          
        self.peer_name = "Peer"
        self.last_rx = 0.0
        self.last_seen = None
        self.subs = set()
        self.dirty = False
        self.read_recent = []
        self._last_online = None
        self._last_ping = 0.0
        self._load()

    def _add(self, m):
        self.msgs.append(m)
        self.index[(m["dir"], m["id"])] = m

    def _load(self):
        try: d = json.loads(self.history_path.read_text())
        except Exception: return
        self.peer_name = d.get("peer_name", self.peer_name)
        self.last_seen = d.get("last_seen")
        for m in d.get("messages", []):
            if m["dir"] == "out" and m["status"] in ("queued", "sent"): m["status"] = "failed"
            m["next_tx"] = float("inf")
            self._add(m)

    def _save(self):
        keep = ("id", "dir", "text", "ts", "status", "tries", "type", "path")
        d = {"peer_name": self.peer_name, "last_seen": self.last_seen,
             "messages": [{k: m.get(k, 0 if k == "tries" else None) for k in keep if k in m} for m in self.msgs[-2000:]]}
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.history_path)

    def attach(self, link):
        self.link = link
        for f in (self._rx_loop, self._timer_loop): threading.Thread(target=f, daemon=True).start()

    def _new_id(self):
        while True:
            i = random.getrandbits(32)
            if ("out", i) not in self.index and i not in self.file_chunks: return i

    def _tx(self, m):
        self.link.send(PKT.pack(T_TEXT, m["id"]) + m["text"].encode("utf-8"))
        m["tries"] += 1
        m["next_tx"] = float("inf")

    def send_text(self, text):
        with self.lock:
            for part in split_text(text):
                m = {"id": self._new_id(), "dir": "out", "type": "text", "text": part, "ts": time.time(), "status": "queued", "tries": 0}
                self._add(m)
                self._tx(m)
        self.notify(dirty=True)

    def send_file(self, name, b64):
        try: raw = base64.b64decode(b64)
        except Exception: return
        fid = self._new_id()
        m = {"id": fid, "dir": "out", "type": "file", "text": name, "ts": time.time(), "status": "queued", "tries": 0, "progress": 0}
        with self.lock:
            self._add(m)
            payload = name.encode("utf-8") + b'\x00' + raw
            chunks = [payload[i:i + MAX_FILE_DATA] for i in range(0, len(payload), MAX_FILE_DATA)]
            tot = len(chunks)
            for idx, block in enumerate(chunks):
                cid = self._new_id()
                self.file_chunks[cid] = {"file_id": fid, "idx": idx, "total": tot, "block": block, "tries": 0, "next_tx": 0, "status": "queued"}
        self.notify(dirty=True)

    def retry(self, mid):
        with self.lock:
            m = self.index.get(("out", mid))
            if not m or m["status"] != "failed": return
            m["status"], m["tries"] = "queued", 0
            if m.get("type") == "file":
                for cid, c in self.file_chunks.items():
                    if c["file_id"] == mid and c["status"] == "failed": c["status"], c["tries"], c["next_tx"] = "queued", 0, 0
            else: self._tx(m)
        self.notify(dirty=True)

    def on_sent(self, data):
        if len(data) < 1: return
        t = data[0]
        if t == T_TEXT and len(data) >= PKT.size:
            _, mid = PKT.unpack_from(data)
            with self.lock:
                m = self.index.get(("out", mid))
                if m and m["status"] == "queued":
                    m["status"] = "sent"
                    m["next_tx"] = time.time() + rto(m["tries"])
            self.notify(dirty=True)
        elif t == T_FILE and len(data) >= FILE_PKT.size:
            _, cid, fid, idx, tot = FILE_PKT.unpack_from(data)
            with self.lock:
                c = self.file_chunks.get(cid)
                if c and c["status"] == "queued":
                    c["status"] = "sent"
                    c["next_tx"] = time.time() + rto(c["tries"])
            self.notify(dirty=True)

    def mark_read(self):
        with self.lock:
            ids = [m["id"] for m in self.msgs if m["dir"] == "in" and m["status"] == "unread"]
            for i in ids: self.index[("in", i)]["status"] = "read"
            if ids:
                self._send_read(ids)
                self.read_recent.append([time.time(), ids, False])
        if ids: self.notify(dirty=True)

    def _send_read(self, ids):
        per = (MAX_DATA - PKT.size) // 4
        for i in range(0, len(ids), per):
            chunk = ids[i:i + per]
            self.link.send(PKT.pack(T_READ, 0) + struct.pack(f">{len(chunk)}I", *chunk))

    def _rx_loop(self):
        while True:
            d = self.link.recv(0.5)
            if d:
                try: self._handle(d)
                except Exception as e: print("rx error:", e, file=sys.stderr)

    def _handle(self, data):
        if len(data) < 1: return
        t = data[0]
        now = time.time()
        changed = False
        with self.lock:
            self.last_rx = self.last_seen = now
            if t == T_PING and len(data) >= PKT.size:
                _, _ = PKT.unpack_from(data)
                nm = data[PKT.size:].decode("utf-8", "replace").strip()[:32]
                if nm and nm != self.peer_name: self.peer_name, changed = nm, True
            
            elif t == T_TEXT and len(data) >= PKT.size:
                _, mid = PKT.unpack_from(data)
                self.link.send(PKT.pack(T_ACK, mid))
                if ("in", mid) not in self.index:
                    self._add({"id": mid, "dir": "in", "type": "text", "ts": now, "status": "unread", "text": data[PKT.size:].decode("utf-8", "replace")})
                    changed = True
            
            elif t == T_ACK and len(data) >= PKT.size:
                _, mid = PKT.unpack_from(data)
                m = self.index.get(("out", mid))
                if m:
                    if m["status"] in ("queued", "sent", "failed"): m["status"], changed = "delivered", True
                elif mid in self.file_chunks:
                    c = self.file_chunks[mid]
                    if c["status"] != "delivered":
                        c["status"], changed = "delivered", True
                        fid = c["file_id"]
                        
                        # NEW: Update outgoing file progress percentage
                        all_chunks = [ch for ch in self.file_chunks.values() if ch["file_id"] == fid]
                        delivered = sum(1 for ch in all_chunks if ch["status"] == "delivered")
                        p = self.index.get(("out", fid))
                        if p:
                            p["progress"] = int((delivered / c["total"]) * 100)
                            if delivered == c["total"]: p["status"] = "delivered"
                            elif p["status"] == "queued": p["status"] = "sent"
            
            elif t == T_FILE and len(data) >= FILE_PKT.size:
                _, cid, fid, idx, tot = FILE_PKT.unpack_from(data)
                body = data[FILE_PKT.size:]
                self.link.send(PKT.pack(T_ACK, cid))
                
                if fid not in self.incoming_files:
                    self.incoming_files[fid] = {"chunks": {}, "tot": tot}
                    # NEW: Create an immediate visual bubble for incoming file progress
                    if ("in", fid) not in self.index:
                        self._add({"id": fid, "dir": "in", "type": "file", "text": "Incoming Transfer...", "ts": now, "status": "receiving", "progress": 0})
                        changed = True

                self.incoming_files[fid]["chunks"][idx] = body
                
                # Update incoming progress
                inc_msg = self.index.get(("in", fid))
                if inc_msg and inc_msg["status"] == "receiving":
                    inc_msg["progress"] = int((len(self.incoming_files[fid]["chunks"]) / tot) * 100)
                    changed = True

                if len(self.incoming_files[fid]["chunks"]) == tot:
                    full = b"".join(self.incoming_files[fid]["chunks"][i] for i in range(tot))
                    parts = full.split(b'\x00', 1)
                    if len(parts) == 2:
                        name, raw = parts[0].decode("utf-8", "replace"), parts[1]
                        path = DOWNLOADS / f"{fid}_{name}"
                        path.write_bytes(raw)
                        if inc_msg:
                            inc_msg["status"] = "unread"
                            inc_msg["text"] = name
                            inc_msg["path"] = f"/downloads/{path.name}"
                            changed = True
                    del self.incoming_files[fid]
                    
            elif t == T_READ and len(data) >= PKT.size:
                for i in struct.unpack(f">{len(data[PKT.size:]) // 4}I", data[PKT.size:len(data[PKT.size:]) // 4 * 4 + PKT.size]):
                    m = self.index.get(("out", i))
                    if m and m["status"] != "read": m["status"], changed = "read", True
        self.notify(dirty=changed)

    def _timer_loop(self):
        ticks = 0
        while True:
            time.sleep(0.5)
            now = time.time()
            dirty = False
            with self.lock:
                if now - self._last_ping >= PING_S:
                    self._last_ping = now
                    self.link.send(PKT.pack(T_PING, 0) + self.name.encode("utf-8"))
                
                # Text Messages
                for m in self.msgs:
                    if m["dir"] == "out" and m["status"] == "sent" and m.get("type", "text") == "text" and now >= m["next_tx"]:
                        if m["tries"] >= MAX_TRIES: m["status"], dirty = "failed", True
                        elif self.link.txq.qsize() < 6: self._tx(m)
                            
                # NEW: File Chunks controlled by a Sliding Window (MAX_IN_FLIGHT)
                in_flight = sum(1 for c in self.file_chunks.values() if c["status"] == "sent")
                
                for cid, c in list(self.file_chunks.items()):
                    if c["status"] in ("queued", "sent") and now >= c["next_tx"]:
                        if c["tries"] >= MAX_TRIES:
                            c["status"], dirty = "failed", True
                            p = self.index.get(("out", c["file_id"]))
                            if p and p["status"] != "failed": p["status"] = "failed"
                        elif c["status"] == "sent" or (c["status"] == "queued" and in_flight < MAX_IN_FLIGHT):
                            if self.link.txq.qsize() < 6:
                                self.link.send(FILE_PKT.pack(T_FILE, cid, c["file_id"], c["idx"], c["total"]) + c["block"])
                                c["tries"] += 1
                                c["next_tx"] = now + rto(c["tries"])
                                if c["status"] == "queued":
                                    c["status"] = "sent"
                                    in_flight += 1
                                dirty = True

                for e in list(self.read_recent):
                    if not e[2] and now - e[0] > 4:
                        e[2] = True
                        self._send_read(e[1])
                    if now - e[0] > 30: self.read_recent.remove(e)
            ticks += 1
            self.notify(dirty=dirty, force=(ticks % 4 == 0))

    def _online(self): return time.time() - self.last_rx < ONLINE_S

    def state(self, online):
        l = self.link
        return {
            "me": self.name, "peer": self.peer_name, "online": online,
            "last_seen": self.last_seen,
            "unread": sum(1 for m in self.msgs if m["dir"] == "in" and m["status"] == "unread"),
            "modem": {"tx": l.frames_tx, "rx": l.frames_rx, "lost": l.frames_lost, "queue": l.txq.qsize(), "blocked_s": round(l.tx_blocked_for, 1)},
            "messages": [{"id": m["id"], "dir": m["dir"], "text": m["text"], "ts": m["ts"],
                          "status": m["status"], "type": m.get("type", "text"), "path": m.get("path"),
                          "tries": m.get("tries", 0), "progress": m.get("progress", 0)} for m in self.msgs[-500:]],
        }

    def notify(self, dirty=False, force=False):
        with self.lock:
            online = self._online()
            if dirty: self.dirty = True
            if not (dirty or force or online != self._last_online): return
            self._last_online = online
            if self.dirty:
                self._save()
                self.dirty = False
            if self.subs:
                payload = json.dumps(self.state(online))
                for q in list(self.subs): q.put(payload)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subs.add(q)
            q.put(json.dumps(self.state(self._online())))
        return q

    def unsubscribe(self, q):
        with self.lock: self.subs.discard(q)


class Handler(BaseHTTPRequestHandler):
    chat = None
    loopback_only = True
    def log_message(self, *a): pass

    def _host_ok(self):
        if not self.loopback_only: return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body)
        if isinstance(body, str): body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok(): return self._send(403, {"error": "bad host"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"): return self._send(200, (HERE / "chat_ui.html").read_text(encoding="utf-8"), "text/html")
        if path == "/api/state": return self._send(200, self.chat.state(self.chat._online()))
        if path == "/events": return self._events()
        if path.startswith("/downloads/"):
            fpath = DOWNLOADS / path.split("/")[-1]
            if fpath.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(fpath.stat().st_size))
                self.end_headers()
                with open(fpath, "rb") as f: self.wfile.write(f.read())
                return
        self._send(404, {"error": "not found"})

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.chat.subscribe()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while True:
                try:
                    payload = q.get(timeout=15)
                    while not q.empty(): payload = q.get_nowait()
                    self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                except queue.Empty: self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError: pass
        finally: self.chat.unsubscribe(q)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Requested-With") != "WaveLink": return self._send(403, {"error": "forbidden"})
        try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception: return self._send(400, {"error": "bad json"})
        
        path = self.path.split("?")[0]
        if path == "/api/send":
            if text := str(body.get("text", "")).strip(): self.chat.send_text(text[:4000])
        elif path == "/api/upload":
            if (name := str(body.get("name", "")).strip()) and (b64 := str(body.get("b64", "")).strip()): self.chat.send_file(name, b64)
        elif path == "/api/read": self.chat.mark_read()
        elif path == "/api/retry": self.chat.retry(int(body.get("id", 0)))
        else: return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True})

def main():
    ap = argparse.ArgumentParser(description="WaveLink Chat over the BPSK link")
    ap.add_argument("--name", default="Me", help="your display name")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tx", default="tcp://127.0.0.1:5001")
    ap.add_argument("--rx", default="tcp://127.0.0.1:5002")
    ap.add_argument("--history", help="chat history file")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    safe = "".join(c for c in a.name if c.isalnum() or c in "-_") or "me"
    chat = Chat(a.name, a.history or f"chat_history_{safe}.json")
    link = Link(a.tx, a.rx, coalesce=False, on_sent=lambda d: chat.on_sent(d))
    chat.attach(link)

    Handler.chat = chat
    Handler.loopback_only = a.host in ("127.0.0.1", "localhost", "::1")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://localhost:{a.port}"
    print(f"WaveLink Chat as '{chat.name}'  ->  {url}   (Ctrl+C to quit)")
    if not a.no_browser: threading.Timer(0.8, webbrowser.open, [url]).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        chat.notify(dirty=True)
        print(f"frames tx={link.frames_tx} rx={link.frames_rx} lost={link.frames_lost}")

if __name__ == "__main__": main()