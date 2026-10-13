"""Zero-dependency web server: REST + Server-Sent Events for one station."""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_UPLOAD = 70_000_000      # 64 MB files + envelope
CTYPES = {".woff": "font/woff", ".woff2": "font/woff2", ".js": "text/javascript", ".css": "text/css",
          ".txt": "text/plain; charset=utf-8"}


def make_handler(node):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        # ------------------------------------------------------------ helpers
        def _send(self, code: int, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not (extra and "Cache-Control" in extra):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _err(self, msg, code=400):
            self._json({"error": str(msg)}, code)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ValueError(f"upload too large ({n} bytes, max {MAX_UPLOAD})")
            buf = bytearray()
            while len(buf) < n:                       # large uploads arrive in pieces
                chunk = self.rfile.read(min(1 << 20, n - len(buf)))
                if not chunk:
                    break
                buf += chunk
            return bytes(buf)

        def _file(self, path):
            if not os.path.isfile(path):
                return self._err("not found", 404)
            with open(path, "rb") as f:
                data = f.read()
            ctype = CTYPES.get(os.path.splitext(path)[1]) or mimetypes.guess_type(path)[0] \
                or "application/octet-stream"
            if path.endswith(".webm"):
                ctype = "audio/webm"
            extra = {"Cache-Control": "max-age=86400"} if path.endswith((".woff", ".woff2")) else None
            self._send(200, data, ctype, extra)

        # ------------------------------------------------------------ GET
        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            p = u.path
            if p in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"))
            if p.startswith("/static/"):
                return self._file(os.path.join(STATIC, os.path.basename(p)))
            if p.startswith("/media/"):
                return self._file(os.path.join(node.media_dir, os.path.basename(p)))
            if p == "/api/state":
                return self._json(node.state())
            if p == "/api/radio":
                return self._json(node.radio_status())
            if p == "/api/events":
                return self._sse()
            if p == "/api/gc":
                return self._json(node.hub.snapshot())
            if p == "/api/gc/clock":
                q = urllib.parse.parse_qs(u.query)
                return self._json({"t": node.hub.host_now(q.get("r", [None])[0])})
            if p == "/api/gc/stats":
                q = urllib.parse.parse_qs(u.query)
                return self._json(node.hub.race_stats(q.get("r", [""])[0]))
            self._err("not found", 404)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = node.subscribe()
            try:
                self.wfile.write(b"retry: 1500\n\n")
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=15)
                        self.wfile.write(b"data: " + msg.encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                node.unsubscribe(q)
                self.close_connection = True

        # ------------------------------------------------------------ POST
        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                body = self._body()
                if u.path == "/api/send":
                    j = json.loads(body or b"{}")
                    text = (j.get("text") or "").strip()
                    if not text:
                        return self._err("empty message")
                    rec = node.send(j["to"], "text", text.encode(), urgent=bool(j.get("urgent")), text=text)
                    return self._json(rec)
                if u.path == "/api/upload":
                    kind = qs.get("kind", "file")
                    if kind not in ("image", "audio", "file"):
                        return self._err("bad kind")
                    if len(body) > node.mac.max_message_bytes() - 4096:
                        return self._err("file too large for the link")
                    rec = node.send(qs["to"], kind, body, name=qs.get("name", ""),
                                    mime=qs.get("mime", ""), urgent=qs.get("urgent") == "1",
                                    extra={"dur": float(qs["dur"])} if qs.get("dur") else None)
                    return self._json(rec)
                if u.path == "/api/game":
                    j = json.loads(body)
                    rec = node.send(j["to"], "game", b"", extra=j["state"])
                    return self._json(rec)
                if u.path == "/api/gc/race":
                    j = json.loads(body)
                    ok = node.hub.race_post(j["r"], j["s"], j.get("ev") or ())
                    return self._json({"ok": ok, "t": node.hub.host_now(j["r"])})
                if u.path == "/api/gc":
                    return self._json(game_action(node.hub, json.loads(body)))
                if u.path == "/api/read":
                    j = json.loads(body)
                    node.send_receipts(j["chat"])
                    return self._json({"ok": True})
                if u.path == "/api/name":
                    j = json.loads(body)
                    node.set_name(j.get("name", ""))
                    return self._json({"ok": True, "name": node.name})
                if u.path == "/api/clear":
                    node.clear_history()
                    return self._json({"ok": True})
                if u.path == "/api/sim":
                    air = getattr(node.radio, "air", None)
                    if air is None:
                        return self._err("not in simulation mode")
                    j = json.loads(body)
                    air.set_ebn0(float(j["ebn0_db"]))
                    return self._json({"ok": True, "ebn0_db": air.ebn0_db})
                self._err("not found", 404)
            except (ValueError, KeyError) as e:
                self._err(e)
            except Exception as e:      # keep the server alive
                node.log("web error", repr(e))
                self._err(repr(e), 500)

    return H


def game_action(hub, j: dict) -> dict:
    """POST /api/gc  {a: action, ...}"""
    a = j.get("a")
    if a == "create":
        return {"r": hub.create(j["game"], j.get("players") or [], j.get("o") or {})}
    rid = j["r"]
    if a == "answer":
        hub.answer(rid, bool(j.get("ok")))
    elif a == "start":
        hub.start_game(rid)
    elif a == "leave":
        hub.leave(rid)
    elif a == "dismiss":
        hub.dismiss(rid)
    elif a == "uno":
        n = hub.uno_action(rid, j["m"], c=j.get("c"), col=j.get("col"), uno=j.get("uno") or None)
        return {"ok": True, "n": n}
    elif a == "next":
        hub.uno_next(rid)
    else:
        raise ValueError("unknown action")
    return {"ok": True}


def serve(node, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(node))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name=f"web{port}", daemon=True).start()
    return srv
