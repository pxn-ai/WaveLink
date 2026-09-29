#!/usr/bin/env python3
"""
WaveLink Chat - a WhatsApp-style chat on top of the BPSK radio link.

    python3 chat_gui.py --name Pasan          ->  http://localhost:8080

Needs bpsk_duplex_node.grc (generated + running) and duplex_link.py next to
this file.  Only pyzmq is required (pip install pyzmq); the web server is
the Python standard library.

Message layer (each packet fits in ONE radio frame, so a lost frame is a
lost packet, never a desynchronised stream):
    TEXT  [1][id u32][utf-8 text]     re-sent until ACKed (max 10 tries)
    ACK   [2][id u32]                 ->  double grey tick
    READ  [3][0][id u32 ...]          ->  double blue tick
    PING  [4][0][name utf-8]          ->  presence ("online") + peer name
"""
import argparse, json, os, queue, random, struct, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from duplex_link import Link, MAX_DATA

T_TEXT, T_ACK, T_READ, T_PING = 1, 2, 3, 4
PKT = struct.Struct(">BI")
MAX_TEXT = MAX_DATA - PKT.size            # bytes of text per packet
RTO0, RTO_MAX, MAX_TRIES = 3.0, 10.0, 10  # retransmit timing
PING_S, ONLINE_S = 5.0, 16.0              # presence beacon / online window
HERE = Path(__file__).resolve().parent


def rto(tries):
    return min(RTO0 * 1.5 ** max(tries - 1, 0), RTO_MAX)


def split_text(text, limit=MAX_TEXT):
    out, cur, n = [], [], 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if n + b > limit:
            out.append("".join(cur)); cur, n = [], 0
        cur.append(ch); n += b
    if cur:
        out.append("".join(cur))
    return out


class Chat:
    def __init__(self, name, history_path):
        self.name = name[:32] or "Me"
        self.history_path = Path(history_path)
        self.link = None
        self.lock = threading.RLock()
        self.msgs, self.index = [], {}
        self.peer_name = "Peer"
        self.last_rx = 0.0
        self.last_seen = None
        self.subs = set()
        self.dirty = False
        self.read_recent = []             # [ts, ids, resent]
        self._last_online = None
        self._last_ping = 0.0
        self._load()

    # ---- persistence -------------------------------------------------
    def _add(self, m):
        self.msgs.append(m)
        self.index[(m["dir"], m["id"])] = m

    def _load(self):
        try:
            d = json.loads(self.history_path.read_text())
        except Exception:
            return
        self.peer_name = d.get("peer_name", self.peer_name)
        self.last_seen = d.get("last_seen")
        for m in d.get("messages", []):
            if m["dir"] == "out" and m["status"] in ("queued", "sent"):
                m["status"] = "failed"    # unsent at shutdown: let the user retry
            m["next_tx"] = float("inf")
            self._add(m)

    def _save(self):
        keep = ("id", "dir", "text", "ts", "status", "tries")
        d = {"peer_name": self.peer_name, "last_seen": self.last_seen,
             "messages": [{k: m.get(k, 0) for k in keep} for m in self.msgs[-2000:]]}
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.history_path)

    # ---- lifecycle ---------------------------------------------------
    def attach(self, link):
        self.link = link
        for f in (self._rx_loop, self._timer_loop):
            threading.Thread(target=f, daemon=True).start()

    # ---- sending -----------------------------------------------------
    def _new_id(self):
        while True:
            i = random.getrandbits(32)
            if ("out", i) not in self.index:
                return i

    def _tx(self, m):
        self.link.send(PKT.pack(T_TEXT, m["id"]) + m["text"].encode("utf-8"))
        m["tries"] += 1
        m["next_tx"] = float("inf")       # armed by on_sent() when really on air

    def send_text(self, text):
        with self.lock:
            for part in split_text(text):
                m = {"id": self._new_id(), "dir": "out", "text": part,
                     "ts": time.time(), "status": "queued", "tries": 0}
                self._add(m)
                self._tx(m)
        self.notify(dirty=True)

    def retry(self, mid):
        with self.lock:
            m = self.index.get(("out", mid))
            if not m or m["status"] != "failed":
                return
            m["status"], m["tries"] = "queued", 0
            self._tx(m)
        self.notify(dirty=True)

    def on_sent(self, data):              # called from Link's TX thread
        if len(data) < PKT.size:
            return
        t, mid = PKT.unpack_from(data)
        if t != T_TEXT:
            return
        with self.lock:
            m = self.index.get(("out", mid))
            if not m:
                return
            if m["status"] == "queued":
                m["status"] = "sent"
            m["next_tx"] = time.time() + rto(m["tries"])
        self.notify(dirty=True)

    def mark_read(self):
        with self.lock:
            ids = [m["id"] for m in self.msgs if m["dir"] == "in" and m["status"] == "unread"]
            for i in ids:
                self.index[("in", i)]["status"] = "read"
            if ids:
                self._send_read(ids)
                self.read_recent.append([time.time(), ids, False])
        if ids:
            self.notify(dirty=True)

    def _send_read(self, ids):
        per = (MAX_DATA - PKT.size) // 4
        for i in range(0, len(ids), per):
            chunk = ids[i:i + per]
            self.link.send(PKT.pack(T_READ, 0) + struct.pack(f">{len(chunk)}I", *chunk))

    # ---- receiving ---------------------------------------------------
    def _rx_loop(self):
        while True:
            d = self.link.recv(0.5)
            if d:
                try:
                    self._handle(d)
                except Exception as e:
                    print("rx error:", e, file=sys.stderr)

    def _handle(self, data):
        if len(data) < PKT.size:
            return
        t, mid = PKT.unpack_from(data)
        body = data[PKT.size:]
        now = time.time()
        changed = False
        with self.lock:
            self.last_rx = self.last_seen = now
            if t == T_PING:
                nm = body.decode("utf-8", "replace").strip()[:32]
                if nm and nm != self.peer_name:
                    self.peer_name, changed = nm, True
            elif t == T_TEXT:
                self.link.send(PKT.pack(T_ACK, mid))          # always (ACK may have been lost)
                if ("in", mid) not in self.index:
                    self._add({"id": mid, "dir": "in", "ts": now, "status": "unread",
                               "text": body.decode("utf-8", "replace")})
                    changed = True
            elif t == T_ACK:
                m = self.index.get(("out", mid))
                if m and m["status"] in ("queued", "sent", "failed"):
                    m["status"], changed = "delivered", True
            elif t == T_READ:
                for i in struct.unpack(f">{len(body) // 4}I", body[:len(body) // 4 * 4]):
                    m = self.index.get(("out", i))
                    if m and m["status"] != "read":
                        m["status"], changed = "read", True
        self.notify(dirty=changed)

    # ---- timers: ping, retransmit ------------------------------------
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
                for m in self.msgs:
                    if m["dir"] == "out" and m["status"] == "sent" and now >= m["next_tx"]:
                        if m["tries"] >= MAX_TRIES:
                            m["status"], dirty = "failed", True
                        elif self.link.txq.qsize() < 6:
                            self._tx(m)
                for e in list(self.read_recent):
                    if not e[2] and now - e[0] > 4:
                        e[2] = True
                        self._send_read(e[1])              # READ has no ACK: send twice
                    if now - e[0] > 30:
                        self.read_recent.remove(e)
            ticks += 1
            self.notify(dirty=dirty, force=(ticks % 4 == 0))

    # ---- state for the UI --------------------------------------------
    def _online(self):
        return time.time() - self.last_rx < ONLINE_S

    def state(self, online):
        l = self.link
        return {
            "me": self.name, "peer": self.peer_name, "online": online,
            "last_seen": self.last_seen,
            "unread": sum(1 for m in self.msgs if m["dir"] == "in" and m["status"] == "unread"),
            "modem": {"tx": l.frames_tx, "rx": l.frames_rx, "lost": l.frames_lost,
                      "queue": l.txq.qsize(), "blocked_s": round(l.tx_blocked_for, 1)},
            "messages": [{"id": m["id"], "dir": m["dir"], "text": m["text"], "ts": m["ts"],
                          "status": m["status"], "tries": m.get("tries", 0)}
                         for m in self.msgs[-500:]],
        }

    def notify(self, dirty=False, force=False):
        with self.lock:
            online = self._online()
            if dirty:
                self.dirty = True
            if not (dirty or force or online != self._last_online):
                return
            self._last_online = online
            if self.dirty:
                self._save()
                self.dirty = False
            if self.subs:
                payload = json.dumps(self.state(online))
                for q in list(self.subs):
                    q.put(payload)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subs.add(q)
            q.put(json.dumps(self.state(self._online())))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


# ---- HTTP ---------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    chat = None
    loopback_only = True

    def log_message(self, *a):
        pass

    def _host_ok(self):
        if not self.loopback_only:
            return True
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, (HERE / "chat_ui.html").read_text(encoding="utf-8"), "text/html")
        if path == "/api/state":
            c = self.chat
            return self._send(200, c.state(c._online()))
        if path == "/events":
            return self._events()
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
                    while not q.empty():          # only the newest state matters
                        payload = q.get_nowait()
                    self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.chat.unsubscribe(q)

    def do_POST(self):
        # custom header => cross-site pages cannot trigger this (CORS preflight fails)
        if not self._host_ok() or self.headers.get("X-Requested-With") != "WaveLink":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        path = self.path.split("?")[0]
        if path == "/api/send":
            text = str(body.get("text", "")).strip()
            if not text:
                return self._send(400, {"error": "empty"})
            self.chat.send_text(text[:4000])
        elif path == "/api/read":
            self.chat.mark_read()
        elif path == "/api/retry":
            self.chat.retry(int(body.get("id", 0)))
        else:
            return self._send(404, {"error": "not found"})
        self._send(200, {"ok": True})


def main():
    ap = argparse.ArgumentParser(description="WaveLink Chat over the BPSK link")
    ap.add_argument("--name", default="Me", help="your display name")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to open the chat from a phone on your LAN (no login!)")
    ap.add_argument("--tx", default="tcp://127.0.0.1:5555")
    ap.add_argument("--rx", default="tcp://127.0.0.1:5556")
    ap.add_argument("--history", help="chat history file (default: chat_history_<name>.json)")
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
    if not a.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        chat.notify(dirty=True)
        print(f"frames tx={link.frames_tx} rx={link.frames_rx} lost={link.frames_lost}")


if __name__ == "__main__":
    main()
