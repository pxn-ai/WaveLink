#!/usr/bin/env python3
"""
WaveLink Chat over the BPSK duplex link (ANTSDR / Pluto).

Protocol v4 - both stations must run this version.
  * In-order display: every text and file carries a per-sender ordering key and
    the receiver inserts messages in the order they were sent, so a message that
    needed a retransmission no longer lands after newer ones.
  * Fast, robust file transfer: selective-repeat ARQ where ONE ack frame (FACK)
    describes the whole transfer (cumulative base + bitmap). A lost ack is
    repaired by the next one, holes are detected and resent immediately (fast
    retransmit), the retransmit timer adapts to the measured round-trip time,
    and a transfer only fails after STALL_S seconds with no progress at all.
"""
import argparse, base64, json, os, queue, random, re, struct, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from duplex_link import Link, MAX_DATA

# New type codes for the changed packet formats, so an old peer ignores them
# instead of mis-parsing them.
T_ACK, T_READ, T_PING = 2, 3, 4
T_TEXT, T_FILE, T_FACK = 11, 15, 16

PKT = struct.Struct(">BI")                # ACK / READ / PING: type, id
TEXT_PKT = struct.Struct(">BIQ")          # type, msg_id, order
FILE_PKT = struct.Struct(">BIHHQ")        # type, file_id, chunk_idx, total_chunks, order
FACK_HDR = struct.Struct(">BIHH")         # type, file_id, total_chunks, base (+ bitmap)
MAX_TEXT = MAX_DATA - TEXT_PKT.size       # 1007 bytes
MAX_FILE_DATA = MAX_DATA - FILE_PKT.size  # 1003 bytes per chunk
MAX_CHUNKS = 0xFFFF

RTO_INIT, RTO_MIN, RTO_MAX = 3.0, 0.8, 10.0   # retransmit timer bounds (adaptive in between)
TEXT_MAX_TRIES = 15
FILE_WINDOW = 32                          # chunks in flight per transfer
TXQ_TARGET = 4                            # keep the modem queue shallow so texts/acks stay snappy
STALL_S = 90.0                            # fail a file only after this long with zero progress
FACK_DELAY = 0.08                         # batch file acks (~3 frames) into one ack frame
TICK = 0.05                               # timer resolution
UI_MIN_INTERVAL = 0.2                     # max 5 GUI pushes per second
PING_S, ONLINE_S = 5.0, 16.0
SAVE_EVERY = 1.0
MAX_NAME = 100

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


class Rtt:
    """Jacobson/Karels round-trip estimator -> retransmission timeout."""
    def __init__(self):
        self.srtt = self.rttvar = None

    def sample(self, r):
        if r <= 0:
            return
        if self.srtt is None:
            self.srtt, self.rttvar = r, r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r

    def rto(self, tries=1):
        base = RTO_INIT if self.srtt is None else max(self.srtt + 4 * self.rttvar, RTO_MIN)
        return min(base * 1.5 ** max(tries - 1, 0), RTO_MAX)


class Chat:
    def __init__(self, name, history_path):
        self.name = name[:32] or "Me"
        self.history_path = Path(history_path)
        self.link = None
        self.lock = threading.RLock()
        self.msgs, self.index = [], {}
        self.rtt = Rtt()
        self._last_order = 0
        self._txn = 0                     # transmission counter (txq is FIFO, so this is air order)

        self.active_transfers = {}        # outgoing: file_id -> transfer state
        self.incoming_files = {}          # incoming: file_id -> {chunks, tot, base, max}
        self._fack_due = {}               # file_id -> total_chunks, acks waiting to be batched
        self._fack_since = 0.0

        self.peer_name = "Peer"
        self.last_rx = 0.0
        self.last_seen = None
        self.subs = set()
        self.dirty = False
        self._ui_dirty = False
        self._last_ui = 0.0
        self.read_recent = []
        self._last_online = None
        self._last_ping = 0.0
        self._last_save = 0.0
        self._load()

    # ------------------------------------------------------------------ storage
    def _add(self, m):
        self.msgs.append(m)
        self.index[(m["dir"], m["id"])] = m

    def _insert_in(self, m):
        """Insert an incoming message in sender order (before any later-sent incoming one)."""
        k, pos = m.get("order"), len(self.msgs)
        if k is not None:
            i, lim = len(self.msgs) - 1, max(0, len(self.msgs) - 1000)
            while i >= lim:
                o = self.msgs[i]
                if o["dir"] == "in":
                    ok = o.get("order")
                    if ok is None or ok <= k:
                        break
                    pos = i
                i -= 1
        self.msgs.insert(pos, m)
        self.index[(m["dir"], m["id"])] = m

    def _next_order(self):
        o = max(self._last_order + 1, int(time.time() * 1000))
        self._last_order = o
        return o

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
            if m["dir"] == "in" and m["status"] == "receiving":
                m["status"] = "failed"
                m["text"] = "Incomplete transfer (app restarted)"
            m["next_tx"] = float("inf")
            self._add(m)
            if m["dir"] == "out" and m.get("order"):
                self._last_order = max(self._last_order, m["order"])

    def _save(self):
        keep = ("id", "dir", "text", "ts", "status", "tries", "type", "path", "progress", "order")
        d = {"peer_name": self.peer_name, "last_seen": self.last_seen,
             "messages": [{k: m[k] for k in keep if k in m} for m in self.msgs[-2000:]]}
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
            if ("out", i) not in self.index:
                return i

    # ------------------------------------------------------------------ sending
    def _tx(self, m):
        if not m.get("order"):
            m["order"] = self._next_order()
        self.link.send(TEXT_PKT.pack(T_TEXT, m["id"], m["order"]) + m["text"].encode("utf-8"))
        m["tries"] += 1
        m["sent_at"] = time.time()
        # Fallback timer in case on_sent() never fires; on_sent() refines it.
        m["next_tx"] = m["sent_at"] + self.rtt.rto(m["tries"]) + 1.0

    def on_sent(self, *args):
        """Called by the link when a frame left the queue: start the timer at air time."""
        changed = False
        try:
            d = args[0]
            if not isinstance(d, (bytes, bytearray)) or not d:
                return
            now = time.time()
            with self.lock:
                if d[0] == T_TEXT and len(d) >= TEXT_PKT.size:
                    _, mid, _o = TEXT_PKT.unpack_from(d)
                    m = self.index.get(("out", mid))
                    if m and m["status"] in ("queued", "sent"):
                        if m["status"] != "sent":
                            m["status"], changed = "sent", True
                        m["next_tx"] = now + self.rtt.rto(m["tries"])
                elif d[0] == T_FILE and len(d) >= FILE_PKT.size:
                    _, fid, idx, _tot, _o = FILE_PKT.unpack_from(d)
                    tr = self.active_transfers.get(fid)
                    if tr and idx < tr["tot"]:
                        c = tr["chunks"][idx]
                        if c["status"] == "sent":
                            c["next_tx"] = now + self.rtt.rto(c["tries"])
        except Exception as e:
            print("on_sent error:", e, file=sys.stderr)
        if changed:
            self.notify(dirty=True)

    def send_text(self, text):
        with self.lock:
            for part in split_text(text):
                m = {"id": self._new_id(), "dir": "out", "type": "text", "text": part,
                     "ts": time.time(), "status": "queued", "tries": 0,
                     "order": self._next_order()}
                self._add(m)
                self._tx(m)
        self.notify(dirty=True)

    def send_file(self, name, b64):
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return
        name = safe_name(name)
        with self.lock:
            fid = self._new_id()
            now = time.time()
            m = {"id": fid, "dir": "out", "type": "file", "text": name, "ts": now,
                 "status": "queued", "tries": 0, "progress": 0, "order": self._next_order()}
            self._add(m)
            payload = name.encode("utf-8") + b"\x00" + raw
            blocks = [payload[i:i + MAX_FILE_DATA] for i in range(0, len(payload), MAX_FILE_DATA)]
            if len(blocks) > MAX_CHUNKS:
                m["status"] = "failed"
            else:
                self.active_transfers[fid] = {
                    "tot": len(blocks), "base": 0, "order": m["order"], "last_progress": now,
                    "chunks": [{"block": b, "tries": 0, "next_tx": 0.0, "status": "queued",
                                "sent_at": 0.0, "txn": 0} for b in blocks]}
                self._pump_files(now)
        self.notify(dirty=True)

    def retry(self, mid):
        with self.lock:
            m = self.index.get(("out", mid))
            if not m or m["status"] != "failed":
                return
            if m.get("type") == "file":
                tr = self.active_transfers.get(mid)
                if not tr:                # lost after restart: nothing left to resend
                    return
                m["status"] = "sent" if tr["base"] else "queued"
                tr["last_progress"] = time.time()
                for c in tr["chunks"]:
                    if c["status"] != "delivered":
                        c["status"], c["tries"], c["next_tx"] = "queued", 0, 0.0
                self._pump_files(time.time())
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

    # -------------------------------------------------------- file ARQ (sender)
    def _pump_files(self, now):
        """Fill each transfer's window with chunks that are new or whose timer expired."""
        dirty = False
        for fid, tr in list(self.active_transfers.items()):
            p = self.index.get(("out", fid))
            if p is None or p["status"] == "failed":
                continue
            if now - tr["last_progress"] > STALL_S:
                p["status"], dirty = "failed", True
                continue
            base, tot = tr["base"], tr["tot"]
            for idx in range(base, min(base + FILE_WINDOW, tot)):
                if self.link.txq.qsize() >= TXQ_TARGET:
                    return dirty
                c = tr["chunks"][idx]
                if c["status"] == "delivered" or now < c["next_tx"]:
                    continue
                self.link.send(FILE_PKT.pack(T_FILE, fid, idx, tot, tr["order"]) + c["block"])
                self._txn += 1
                c["txn"], c["sent_at"], c["status"] = self._txn, now, "sent"
                c["tries"] += 1
                c["next_tx"] = now + self.rtt.rto(c["tries"]) + 1.0   # on_sent() refines this
                if p["status"] == "queued":
                    p["status"], dirty = "sent", True
            # Tail-loss probe: when the window is all in flight and the modem is idle,
            # nothing later will arrive to reveal a lost tail chunk, so resend after
            # ~1.5 RTT instead of waiting for the full timeout.
            if self.link.txq.qsize() == 0 and self.rtt.srtt is not None:
                probe = max(1.5 * self.rtt.srtt + FACK_DELAY, 0.3)
                for idx in range(base, min(base + FILE_WINDOW, tot)):
                    c = tr["chunks"][idx]
                    if c["status"] == "sent" and now - c["sent_at"] > probe:
                        c["next_tx"] = min(c["next_tx"], now)
                    elif c["status"] != "delivered":
                        break                 # only probe the oldest outstanding chunks
        return dirty

    def _on_fack(self, data, now):
        _, fid, tot, base = FACK_HDR.unpack_from(data)
        tr = self.active_transfers.get(fid)
        if not tr or tr["tot"] != tot:
            return False
        chunks, bm, newly = tr["chunks"], data[FACK_HDR.size:], []

        def ack(i):
            c = chunks[i]
            if c["status"] != "delivered":
                c["status"] = "delivered"
                newly.append(c)

        for i in range(tr["base"], min(base, tot)):
            ack(i)
        for j in range(len(bm) * 8):
            i = base + j
            if i >= tot:
                break
            if bm[j >> 3] & (0x80 >> (j & 7)):
                ack(i)
        if not newly:
            return False

        tr["last_progress"] = now
        # RTT samples only from chunks sent exactly once (Karn's rule)
        clean = [c for c in newly if c["tries"] == 1]
        for c in clean:
            self.rtt.sample(now - c["sent_at"])
        while tr["base"] < tot and chunks[tr["base"]]["status"] == "delivered":
            tr["base"] += 1

        # Fast retransmit: the radio path is FIFO, so any chunk transmitted before a
        # chunk that has now arrived, but which the receiver still lacks, was lost.
        hi = max((c["txn"] for c in clean), default=0)
        for i in range(tr["base"], min(tr["base"] + FILE_WINDOW, tot)):
            c = chunks[i]
            if c["status"] == "sent" and c["txn"] < hi:
                c["next_tx"] = now

        p = self.index.get(("out", fid))
        done = sum(1 for c in chunks if c["status"] == "delivered")
        if p:
            p["progress"] = int(done * 100 / tot)
            if done == tot:
                p["status"], p["progress"] = "delivered", 100
            elif p["status"] in ("queued", "failed"):
                p["status"] = "sent"
        if done == tot:
            del self.active_transfers[fid]
        self._pump_files(now)
        return True

    # ------------------------------------------------------ file ARQ (receiver)
    def _build_fack(self, fid, tot):
        st = self.incoming_files.get(fid)
        if st is None:                         # complete: say so
            return FACK_HDR.pack(T_FACK, fid, tot, tot)
        base = st["base"]
        nbytes = min((max(0, st["max"] + 1 - base) + 7) // 8, MAX_DATA - FACK_HDR.size)
        bm = bytearray(nbytes)
        for i in st["chunks"]:
            j = i - base
            if 0 <= j < nbytes * 8:
                bm[j >> 3] |= 0x80 >> (j & 7)
        return FACK_HDR.pack(T_FACK, fid, tot, base) + bytes(bm)

    def _flush_facks(self):
        with self.lock:
            due, self._fack_due = self._fack_due, {}
            for fid, tot in due.items():
                self.link.send(self._build_fack(fid, tot))

    def _want_fack(self, fid, tot, now, urgent=False):
        if not self._fack_due:
            self._fack_since = now
        self._fack_due[fid] = tot
        if urgent:
            self._fack_since = 0.0

    def _rx_file_chunk(self, fid, idx, tot, order, body, now):
        """Store one received chunk. Returns True if the visible state changed."""
        inc = self.index.get(("in", fid))
        if inc is not None and inc["status"] in ("unread", "read"):
            self._want_fack(fid, tot, now, urgent=True)    # late duplicate: re-confirm completion
            return False
        if inc is not None and inc["status"] != "receiving":
            return False                       # failed after a restart: cannot resume

        st = self.incoming_files.get(fid)
        if st is None or st["tot"] != tot:
            st = self.incoming_files[fid] = {"chunks": {}, "tot": tot, "base": 0, "max": -1}
        if inc is None:
            inc = {"id": fid, "dir": "in", "type": "file", "text": "Incoming Transfer...",
                   "ts": now, "status": "receiving", "progress": 0, "order": order}
            self._insert_in(inc)

        st["chunks"][idx] = body
        st["max"] = max(st["max"], idx)
        while st["base"] in st["chunks"]:
            st["base"] += 1
        inc["progress"] = int(len(st["chunks"]) * 100 / tot)
        if len(st["chunks"]) < tot:
            self._want_fack(fid, tot, now)
            return True

        # ---- all chunks present: assemble and save
        full = b"".join(st["chunks"][i] for i in range(tot))
        del self.incoming_files[fid]
        self._want_fack(fid, tot, now, urgent=True)
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
        inc["path"] = "/downloads/" + quote(path.name)
        inc["progress"] = 100
        return True

    # ---------------------------------------------------------------- receiving
    def _rx_loop(self):
        while True:
            d = self.link.recv(0.02)
            if d:
                try:
                    self._handle(d)
                except Exception as e:
                    print("rx error:", e, file=sys.stderr)
                    try:
                        self.notify(dirty=True)
                    except Exception:
                        pass
            if self._fack_due and time.time() - self._fack_since >= FACK_DELAY:
                self._flush_facks()

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

            elif t == T_TEXT and len(data) >= TEXT_PKT.size:
                _, mid, order = TEXT_PKT.unpack_from(data)
                self.link.send(PKT.pack(T_ACK, mid))
                if ("in", mid) not in self.index:
                    self._insert_in({"id": mid, "dir": "in", "type": "text", "ts": now,
                                     "status": "unread", "order": order,
                                     "text": data[TEXT_PKT.size:].decode("utf-8", "replace")})
                    changed = True

            elif t == T_ACK and len(data) >= PKT.size:
                _, mid = PKT.unpack_from(data)
                m = self.index.get(("out", mid))
                if m and m["status"] in ("queued", "sent", "failed"):
                    if m.get("tries") == 1 and m.get("sent_at"):
                        self.rtt.sample(now - m["sent_at"])
                    m["status"], changed = "delivered", True

            elif t == T_FILE and len(data) >= FILE_PKT.size:
                _, fid, idx, tot, order = FILE_PKT.unpack_from(data)
                if 0 < tot and idx < tot:
                    changed = self._rx_file_chunk(fid, idx, tot, order, data[FILE_PKT.size:], now)

            elif t == T_FACK and len(data) >= FACK_HDR.size:
                changed = self._on_fack(data, now)

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

            # Text retransmissions, oldest first
            for m in self.msgs:
                if (m["dir"] == "out" and m.get("type", "text") == "text"
                        and m["status"] in ("queued", "sent") and now >= m["next_tx"]):
                    if m["tries"] >= TEXT_MAX_TRIES:
                        m["status"], dirty = "failed", True
                    elif self.link.txq.qsize() < 16:
                        self._tx(m)

            dirty |= self._pump_files(now)

            for e in list(self.read_recent):
                if not e[2] and now - e[0] > 4:
                    e[2] = True
                    self._send_read(e[1])
                if now - e[0] > 30:
                    self.read_recent.remove(e)
        return dirty

    def _timer_loop(self):
        ticks, every = 0, max(1, int(2.0 / TICK))
        while True:
            time.sleep(TICK)
            try:
                dirty = self._tick()
            except Exception as e:
                print("timer error:", e, file=sys.stderr)
                dirty = False
            ticks += 1
            try:
                self.notify(dirty=dirty, force=(ticks % every == 0))
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
        """Push state to the GUI (rate-limited; the timer flushes anything held back)."""
        with self.lock:
            now = time.time()
            online = self._online()
            if dirty:
                self.dirty = self._ui_dirty = True
            if online != self._last_online:
                self._ui_dirty = True
            if not (self._ui_dirty or force):
                return
            if not force and now - self._last_ui < UI_MIN_INTERVAL:
                return
            self._ui_dirty, self._last_ui, self._last_online = False, now, online
            if self.subs:
                payload = json.dumps(self.state(online))
                for q in list(self.subs):
                    q.put(payload)
            if self.dirty and (force or now - self._last_save >= SAVE_EVERY):
                try:
                    self._save()
                    self.dirty = False
                except Exception as e:
                    print("save error:", e, file=sys.stderr)
                self._last_save = now

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
