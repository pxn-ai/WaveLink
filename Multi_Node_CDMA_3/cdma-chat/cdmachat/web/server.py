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
MAX_UPLOAD = 600_000


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
            return self.rfile.read(n) if n else b""

        def _file(self, path):
            if not os.path.isfile(path):
                return self._err("not found", 404)
            with open(path, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith(".webm"):
                ctype = "audio/webm"
            self._send(200, data, ctype)

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
                    if len(body) > node.mac.max_message_bytes() - 512:
                        return self._err("file too large for the link")
                    rec = node.send(qs["to"], kind, body, name=qs.get("name", ""),
                                    mime=qs.get("mime", ""), urgent=qs.get("urgent") == "1",
                                    extra={"dur": float(qs["dur"])} if qs.get("dur") else None)
                    return self._json(rec)
                if u.path == "/api/game":
                    j = json.loads(body)
                    rec = node.send(j["to"], "game", b"", extra=j["state"])
                    return self._json(rec)
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


def serve(node, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(node))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name=f"web{port}", daemon=True).start()
    return srv
