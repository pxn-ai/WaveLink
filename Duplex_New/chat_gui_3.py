#!/usr/bin/env python3
"""
WaveLink Chat - Optimized for ANTSDR with Selective Repeat ARQ
(fixed: reliable "file received" display + safe download links)
"""
import argparse, base64, json, os, queue, random, re, struct, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from duplex_link import Link, MAX_DATA

T_TEXT, T_ACK, T_READ, T_PING, T_FILE = 1, 2, 3, 4, 5
PKT = struct.Struct(">BI")
FILE_PKT = struct.Struct(">BIIHH")        # type, chunk_id, file_id, chunk_idx, total_chunks
MAX_TEXT = MAX_DATA - PKT.size            # 1020 bytes
MAX_FILE_DATA = MAX_DATA - FILE_PKT.size  # 1011 bytes per chunk
MAX_CHUNKS = 0xFFFF                       # chunk_idx / total_chunks are 16-bit
RTO0, RTO_MAX, MAX_TRIES = 3.0, 10.0, 10  # retransmit timing
PING_S, ONLINE_S = 5.0, 16.0              # presence beacon / online window
WINDOW_SIZE = 16                          # ARQ sliding window limit for ANTSDR buffers
SAVE_EVERY = 1.0                          # max one history write per second
MAX_NAME = 100                            # max characters kept from a file name

HERE = Path(__file__).resolve().parent

DOWNLOADS = HERE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def safe_name(name):
    """Strip any path, replace characters that are illegal on Windows/Linux, cap length."""
    name = str(name).replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        return "file"
    if len(name) > MAX_NAME:
        stem, ext = os.path.splitext(name)
        ext = ext[:16]
        name = stem[:MAX_NAME - len(ext)] + ext
    return name


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

        # Selective Repeat ARQ data structures
        self.active_transfers = {}        # file_id -> {base, tot, chunks[]}
        self.chunk_map = {}               # chunk_id -> (file_id, chunk_idx)
        self.incoming_files = {}          # file_id -> {chunks: {idx: data}, tot: total_chunks}

        self.peer_name = "Peer"
        self.last_rx = 0.0
        self.last_seen = None
        self.subs = set()
        self.dirty = False
        self.read_recent = []
        self._last_online = None
        self._last_ping = 0.0
        self._last_save = 0.0
        self._load()

    # ------------------------------------------------------------------ storage
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
                m["status"] = "failed"
            # A half-received file can't resume after a restart (chunks were in RAM only)
            if m["dir"] == "in" and m["status"] == "receiving":
                m["status"] = "failed"
                m["text"] = "Incomplete transfer (app restarted)"
            m["next_tx"] = float("inf")
            self._add(m)

    def _save(self):
        keep = ("id", "dir", "text", "ts", "status", "tries", "type", "path", "progress")
        d = {"peer_name": self.peer_name, "last_seen": self.last_seen,
             "messages": [{k: m.get(k, 0 if k == "tries" else None) for k in keep if k in m}
                          for m in self.msgs[-2000:]]}
        tmp = self.history_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, self.history_path)

    def flush(self):
        with self.lock:
            try:
                self._save()
                self.dirty = False
            except Exception as e:
                print("save error:", e, file=sys.stderr)

    def attach(self, link):
        self.link = link
        for f in (self._rx_loop, self._timer_loop):
            threading.Thread(target=f, daemon=True).start()

    def _new_id(self):
        while True:
            i = random.getrandbits(32)
            if ("out", i) not in self.index and i not in self.chunk_map:
                return i

    # ------------------------------------------------------------------ sending
    def _tx(self, m):
        self.link.send(PKT.pack(T_TEXT, m["id"]) + m["text"].encode("utf-8"))
        m["tries"] += 1
        # Fallback timer in case the link never calls on_sent(); on_sent() refines it.
        m["next_tx"] = time.time() + rto(m["tries"]) + 2.0

    def on_sent(self, *args):
        """Optional callback from the link when a frame really left the modem queue.
        Starts the retransmit timer at air time instead of enqueue time."""
        changed = False
        try:
            d = args[0]
            if not isinstance(d, (bytes, bytearray)) or not d:
                return
            now = time.time()
            with self.lock:
                if d[0] == T_TEXT and len(d) >= PKT.size:
                    _, mid = PKT.unpack_from(d)
                    m = self.index.get(("out", mid))
                    if m and m["status"] in ("queued", "sent"):
                        if m["status"] != "sent":
                            m["status"], changed = "sent", True
                        m["next_tx"] = now + rto(m["tries"])
                elif d[0] == T_FILE and len(d) >= FILE_PKT.size:
                    _, _cid, fid, idx, _tot = FILE_PKT.unpack_from(d)
                    tr = self.active_transfers.get(fid)
                    if tr and idx < len(tr["chunks"]):
                        c = tr["chunks"][idx]
                        if c["status"] in ("queued", "sent"):
                            c["next_tx"] = now + rto(c["tries"])
        except Exception as e:
            print("on_sent error:", e, file=sys.stderr)
        if changed:
            self.notify(dirty=True)

    def send_text(self, text):
        with self.lock:
            for part in split_text(text):
                m = {"id": self._new_id(), "dir": "out", "type": "text", "text": part,
                     "ts": time.time(), "status": "queued", "tries": 0}
                self._add(m)
                self._tx(m)
        self.notify(dirty=True)

    def send_file(self, name, b64):
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return
        name = safe_name(name)
        fid = self._new_id()
        m = {"id": fid, "dir": "out", "type": "file", "text": name,
             "ts": time.time(), "status": "queued", "tries": 0, "progress": 0}
        with self.lock:
            self._add(m)
            payload = name.encode("utf-8") + b"\x00" + raw
            chunks = [payload[i:i + MAX_FILE_DATA] for i in range(0, len(payload), MAX_FILE_DATA)]
            tot = len(chunks)
            if tot > MAX_CHUNKS:                      # would overflow the 16-bit header fields
                m["status"] = "failed"
            else:
                transfer = {"base": 0, "tot": tot, "chunks": []}
                for idx, block in enumerate(chunks):
                    cid = self._new_id()
                    self.chunk_map[cid] = (fid, idx)
                    transfer["chunks"].append({"cid": cid, "block": block, "tries": 0,
                                               "next_tx": 0, "status": "queued"})
                self.active_transfers[fid] = transfer
        self.notify(dirty=True)

    def retry(self, mid):
        with self.lock:
            m = self.index.get(("out", mid))
            if not m or m["status"] != "failed":
                return
            if m.get("type") == "file":
                transfer = self.active_transfers.get(mid)
                if not transfer:          # lost after restart: nothing left to resend
                    return
                m["status"], m["tries"] = "queued", 0
                for c in transfer["chunks"]:
                    if c["status"] == "failed":
                        c["status"], c["tries"], c["next_tx"] = "queued", 0, 0
            else:
                m["status"], m["tries"] = "queued", 0
                self._tx(m)
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

    # ---------------------------------------------------------------- receiving
    def _rx_loop(self):
        while True:
            d = self.link.recv(0.5)
            if d:
                try:
                    self._handle(d)
                except Exception as e:
                    print("rx error:", e, file=sys.stderr)
                    # Never leave the GUI stale because of an exception mid-handler
                    try:
                        self.notify(dirty=True)
                    except Exception:
                        pass

    def _rx_file_chunk(self, fid, idx, tot, body, now):
        """Store one received chunk. Returns True if the visible state changed."""
        inc = self.index.get(("in", fid))
        if inc is not None and inc["status"] != "receiving":
            return False                       # already finished/failed: late duplicate

        st = self.incoming_files.get(fid)
        if st is None or st["tot"] != tot:
            st = self.incoming_files[fid] = {"chunks": {}, "tot": tot}
        if inc is None:
            inc = {"id": fid, "dir": "in", "type": "file", "text": "Incoming Transfer...",
                   "ts": now, "status": "receiving", "progress": 0}
            self._add(inc)

        st["chunks"][idx] = body               # duplicates just overwrite
        inc["progress"] = int(len(st["chunks"]) * 100 / tot)
        if len(st["chunks"]) < tot:
            return True

        # ---- all chunks present: assemble and save
        full = b"".join(st["chunks"][i] for i in range(tot))
        del self.incoming_files[fid]
        name_b, sep, raw = full.partition(b"\x00")
        if not sep:
            inc["status"], inc["text"] = "failed", "Corrupt transfer"
            return True
        name = safe_name(name_b.decode("utf-8", "replace"))
        try:
            path = DOWNLOADS / f"{fid}_{name}"
            tmp = path.with_name(path.name + ".part")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        except OSError as e:
            inc["status"] = "failed"
            inc["text"] = f"{name} (save failed: {e.strerror or e})"
            return True
        inc["status"] = "unread"
        inc["text"] = name
        inc["path"] = "/downloads/" + quote(path.name)    # URL-safe link
        inc["progress"] = 100
        return True

    def _handle(self, data):
        if len(data) < 1:
            return
        t = data[0]
        now = time.time()
        changed = False
        with self.lock:
            self.last_rx = self.last_seen = now
            if t == T_PING and len(data) >= PKT.size:
                nm = data[PKT.size:].decode("utf-8", "replace").strip()[:32]
                if nm and nm != self.peer_name:
                    self.peer_name, changed = nm, True

            elif t == T_TEXT and len(data) >= PKT.size:
                _, mid = PKT.unpack_from(data)
                self.link.send(PKT.pack(T_ACK, mid))
                if ("in", mid) not in self.index:
                    self._add({"id": mid, "dir": "in", "type": "text", "ts": now, "status": "unread",
                               "text": data[PKT.size:].decode("utf-8", "replace")})
                    changed = True

            elif t == T_ACK and len(data) >= PKT.size:
                _, mid = PKT.unpack_from(data)
                m = self.index.get(("out", mid))
                if m:
                    if m["status"] in ("queued", "sent", "failed"):
                        m["status"], changed = "delivered", True
                elif mid in self.chunk_map:
                    fid, idx = self.chunk_map[mid]
                    transfer = self.active_transfers.get(fid)
                    if transfer:
                        c = transfer["chunks"][idx]
                        if c["status"] != "delivered":
                            c["status"] = "delivered"
                            changed = True
                            while (transfer["base"] < transfer["tot"] and
                                   transfer["chunks"][transfer["base"]]["status"] == "delivered"):
                                transfer["base"] += 1

                            p = self.index.get(("out", fid))
                            if p:
                                done = sum(1 for ch in transfer["chunks"] if ch["status"] == "delivered")
                                p["progress"] = int(done * 100 / transfer["tot"])
                                if done == transfer["tot"]:
                                    p["status"], p["progress"] = "delivered", 100
                                    for ch in transfer["chunks"]:
                                        self.chunk_map.pop(ch["cid"], None)
                                    del self.active_transfers[fid]
                                elif p["status"] in ("queued", "failed"):
                                    p["status"] = "sent"

            elif t == T_FILE and len(data) >= FILE_PKT.size:
                _, cid, fid, idx, tot = FILE_PKT.unpack_from(data)
                if tot > 0 and idx < tot:
                    # ALWAYS ack, even duplicates, so the sender's window keeps moving
                    self.link.send(PKT.pack(T_ACK, cid))
                    changed = self._rx_file_chunk(fid, idx, tot, data[FILE_PKT.size:], now)

            elif t == T_READ and len(data) >= PKT.size:
                body = data[PKT.size:]
                n = len(body) // 4
                for i in struct.unpack(f">{n}I", body[:n * 4]):
                    m = self.index.get(("out", i))
                    if m and m["status"] != "read":
                        m["status"], changed = "read", True
        self.notify(dirty=changed)

    # ------------------------------------------------------------------- timers
    def _tick(self):
        dirty = False
        now = time.time()
        with self.lock:
            if now - self._last_ping >= PING_S:
                self._last_ping = now
                self.link.send(PKT.pack(T_PING, 0) + self.name.encode("utf-8"))

            # Text messages first
            for m in self.msgs:
                if (m["dir"] == "out" and m.get("type", "text") == "text"
                        and m["status"] in ("queued", "sent") and now >= m["next_tx"]):
                    if m["tries"] >= MAX_TRIES:
                        m["status"], dirty = "failed", True
                    elif self.link.txq.qsize() < WINDOW_SIZE:
                        self._tx(m)

            # Selective Repeat ARQ sliding window for files
            for fid, tr in list(self.active_transfers.items()):
                p = self.index.get(("out", fid))
                base, tot = tr["base"], tr["tot"]
                for idx in range(base, min(base + WINDOW_SIZE, tot)):
                    c = tr["chunks"][idx]
                    if c["status"] not in ("queued", "sent") or now < c["next_tx"]:
                        continue
                    if c["tries"] >= MAX_TRIES:
                        c["status"], dirty = "failed", True
                        if p and p["status"] != "failed":
                            p["status"] = "failed"
                    elif self.link.txq.qsize() < WINDOW_SIZE:
                        self.link.send(FILE_PKT.pack(T_FILE, c["cid"], fid, idx, tot) + c["block"])
                        c["tries"] += 1
                        c["next_tx"] = now + rto(c["tries"]) + 2.0   # on_sent() refines this
                        c["status"], dirty = "sent", True
                        if p and p["status"] == "queued":
                            p["status"] = "sent"

            # Read receipts
            for e in list(self.read_recent):
                if not e[2] and now - e[0] > 4:
                    e[2] = True
                    self._send_read(e[1])
                if now - e[0] > 30:
                    self.read_recent.remove(e)
        return dirty

    def _timer_loop(self):
        ticks = 0
        while True:
            time.sleep(0.5)
            try:
                dirty = self._tick()
            except Exception as e:                 # keep the timer thread alive no matter what
                print("timer error:", e, file=sys.stderr)
                dirty = False
            ticks += 1
            try:
                self.notify(dirty=dirty, force=(ticks % 4 == 0))
            except Exception as e:
                print("notify error:", e, file=sys.stderr)

    # ---------------------------------------------------------------- GUI state
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
                          "status": m["status"], "type": m.get("type", "text"), "path": m.get("path"),
                          "tries": m.get("tries", 0), "progress": m.get("progress", 0)}
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
            # Broadcast to the GUI immediately; write history to disk at most once per SAVE_EVERY
            if self.subs:
                payload = json.dumps(self.state(online))
                for q in list(self.subs):
                    q.put(payload)
            if self.dirty and (force or time.time() - self._last_save >= SAVE_EVERY):
                try:
                    self._save()
                    self.dirty = False
                except Exception as e:
                    print("save error:", e, file=sys.stderr)
                self._last_save = time.time()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subs.add(q)
            q.put(json.dumps(self.state(self._online())))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


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

    def _download(self, path):
        raw_name = path[len("/downloads/"):]
        fpath = None
        # browsers send the name percent-encoded; also accept old un-encoded history entries
        for cand in (unquote(raw_name), raw_name):
            p = DOWNLOADS / Path(cand).name
            if p.parent == DOWNLOADS and p.is_file():
                fpath = p
                break
        if not fpath:
            return self._send(404, {"error": "not found"})
        data = fpath.read_bytes()
        head, sep, tail = fpath.name.partition("_")
        shown = tail if (sep and head.isdigit() and tail) else fpath.name
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(shown))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok(): return self._send(403, {"error": "bad host"})
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(200, (HERE / "chat_ui.html").read_text(encoding="utf-8"), "text/html")
            if path == "/api/state":
                c = self.chat
                return self._send(200, c.state(c._online()))
            if path == "/events":
                return self._events()
            if path.startswith("/downloads/"):
                return self._download(path)
            self._send(404, {"error": "not found"})
        except OSError:
            pass

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
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError: pass
        finally: self.chat.unsubscribe(q)

    def do_POST(self):
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
            if text: self.chat.send_text(text[:4000])
        elif path == "/api/upload":
            name = str(body.get("name", "")).strip()
            b64 = str(body.get("b64", "")).strip()
            if name and b64: self.chat.send_file(name, b64)
        elif path == "/api/read":
            self.chat.mark_read()
        elif path == "/api/retry":
            try:
                self.chat.retry(int(body.get("id", 0)))
            except (TypeError, ValueError):
                return self._send(400, {"error": "bad id"})
        else:
            return self._send(404, {"error": "not found"})
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
    link = Link(a.tx, a.rx, coalesce=False, on_sent=chat.on_sent)
    chat.attach(link)

    Handler.chat = chat
    Handler.loopback_only = a.host in ("127.0.0.1", "localhost", "::1")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://localhost:{a.port}"
    print(f"WaveLink Chat as '{chat.name}'  ->  {url}   (Ctrl+C to quit)")
    if not a.no_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        chat.flush()
        print(f"frames tx={link.frames_tx} rx={link.frames_rx} lost={link.frames_lost}")


if __name__ == "__main__":
    main()
