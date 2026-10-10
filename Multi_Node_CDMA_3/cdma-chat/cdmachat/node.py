"""Application layer: a WhatsApp-like station built on top of the link layer.

Message envelope (inside the reliable link-layer message)::

    crypto byte (0x01 plain / 0x02 AES-GCM) | meta_len (2 B) | meta JSON | body bytes

meta keys: k (kind), u (uid), t (timestamp), g (group id), n (file name),
m (mime type), x (extra, kind specific).
Kinds: text, image, audio, file, game, rcpt (read receipt).
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import struct
import threading
import time
import uuid
import zlib

from .config import BROADCAST_ADDR
from .crypto import Crypto
from .mac import LinkLayer, PRIO_BULK, PRIO_NORMAL, PRIO_HIGH

GROUP_ALL = "g:all"
MEDIA_KINDS = ("image", "audio", "file")
EXT = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png", "audio/webm": ".webm",
       "audio/ogg": ".ogg", "audio/mp4": ".m4a", "audio/wav": ".wav", "audio/mpeg": ".mp3"}


def chat_id_for(addr: int) -> str:
    return f"u:{addr}"


class Node:
    def __init__(self, radio, addr: int, name: str, data_dir: str, passphrase: str | None = None,
                 beacon_period: float = 5.0, mode: str = "sim", log=print):
        self.radio = radio
        self.addr = addr
        self.name = name
        self.mode = mode
        self.log = log
        self.crypto = Crypto(passphrase)
        self.data_dir = data_dir
        self.media_dir = os.path.join(data_dir, "media")
        os.makedirs(self.media_dir, exist_ok=True)
        self.hist_path = os.path.join(data_dir, "history.jsonl")
        self.lock = threading.RLock()
        self.contacts: dict[int, dict] = {}
        self.messages: dict[str, dict] = {}          # uid -> record
        self.order: list[str] = []
        self.unread: dict[str, int] = {}
        self.subscribers: list[queue.Queue] = []
        self.beacon_period = beacon_period
        self.mac = LinkLayer(radio, addr, on_message=self._on_message, on_beacon=self._on_beacon,
                             log=log)
        self._load_history()
        self._running = False

    # ================================================================ lifecycle
    def start(self):
        self._running = True
        self.radio.start()
        self.mac.start()
        threading.Thread(target=self._beacon_loop, name=f"beacon{self.addr}", daemon=True).start()

    def stop(self):
        self._running = False
        self.mac.stop()
        self.radio.stop()

    def _beacon_loop(self):
        import random
        # a few quick beacons at start-up so peers appear immediately
        delays = [0.2, 1.0, 2.0]
        while self._running:
            info = json.dumps({"n": self.name, "e": self.crypto.key_id}, separators=(",", ":")).encode()
            try:
                self.mac.send_beacon(info[:200])
            except Exception as e:
                self.log("beacon error", e)
            d = delays.pop(0) if delays else self.beacon_period * random.uniform(0.8, 1.2)
            t_end = self.radio.now() + d
            while self._running and self.radio.now() < t_end:
                time.sleep(0.05)
            self._refresh_presence()

    # ================================================================ events (SSE)
    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, ev: str, data):
        msg = json.dumps({"ev": ev, "data": data}, separators=(",", ":"))
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    # ================================================================ presence
    def _on_beacon(self, src: int, info: bytes, frame):
        try:
            d = json.loads(info.decode())
        except Exception:
            d = {}
        with self.lock:
            c = self.contacts.setdefault(src, {"addr": src})
            c.update(name=d.get("n") or f"Node {src}", last_seen=time.time(), online=True,
                     snr_db=round(frame.snr_db, 1), cfo_hz=round(frame.cfo_hz),
                     key_ok=(d.get("e", "") == self.crypto.key_id))
        self.publish("contacts", self.contact_list())

    def _refresh_presence(self):
        changed = False
        with self.lock:
            for c in self.contacts.values():
                on = time.time() - c.get("last_seen", 0) < 3.5 * self.beacon_period
                if on != c.get("online"):
                    c["online"] = on
                    changed = True
        if changed:
            self.publish("contacts", self.contact_list())

    def contact_list(self) -> list[dict]:
        with self.lock:
            return sorted((dict(c) for c in self.contacts.values()), key=lambda c: c["addr"])

    def set_name(self, name: str):
        self.name = name[:32] or self.name

    # ================================================================ sending
    def _envelope(self, meta: dict, body: bytes, dst: int) -> bytes:
        mj = json.dumps(meta, separators=(",", ":")).encode()
        inner = struct.pack(">H", len(mj)) + mj + body
        return self.crypto.seal(inner, self.addr, dst)

    def send(self, to: str, kind: str, body: bytes, *, name: str = "", mime: str = "",
             urgent: bool = False, extra=None, text: str = "") -> dict:
        """to: 'u:<addr>' or 'g:all'."""
        uid = uuid.uuid4().hex[:12]
        now = time.time()
        if to == GROUP_ALL:
            with self.lock:
                dsts = [a for a, c in self.contacts.items() if c.get("online")]
        else:
            dsts = [int(to.split(":")[1])]
        if not dsts:
            raise ValueError("no recipients online yet - wait for their beacons")
        prio = PRIO_HIGH if urgent else (PRIO_BULK if kind in MEDIA_KINDS else PRIO_NORMAL)
        rec = dict(uid=uid, chat=to, frm=self.addr, kind=kind, ts=now, text=text, name=name,
                   mime=mime, size=len(body), prio=prio, urgent=urgent, extra=extra,
                   status="queued", progress=0.0, recip={str(d): "queued" for d in dsts},
                   enc=self.crypto.enabled)
        if kind in MEDIA_KINDS:
            rec["file"] = self._save_media(uid, body, mime, name)
        meta = {"k": kind, "u": uid, "t": round(now, 3)}
        if to == GROUP_ALL:
            meta["g"] = GROUP_ALL
        if name:
            meta["n"] = name[:80]
        if mime:
            meta["m"] = mime
        if extra is not None:
            meta["x"] = extra
        if urgent:
            meta["!"] = 1
        if kind == "text":
            z = zlib.compress(body, 9)
            if len(z) < len(body):
                meta["z"] = 1
                body = z
        self._store(rec)
        for d in dsts:
            payload = self._envelope(meta, body, d)
            self.mac.send(d, payload, prio, on_event=lambda ev, m, d=d, uid=uid: self._on_tx_event(uid, d, ev, m))
        return rec

    def send_receipts(self, chat: str):
        """Mark a chat read and tell the senders (blue ticks)."""
        with self.lock:
            self.unread[chat] = 0
            by_sender: dict[int, list] = {}
            for uid in self.order:
                r = self.messages[uid]
                if r["chat"] == chat and r["frm"] != self.addr and not r.get("read_sent") and r["kind"] != "rcpt":
                    r["read_sent"] = True
                    by_sender.setdefault(r["frm"], []).append(uid)
        for src, uids in by_sender.items():
            meta = {"k": "rcpt", "u": uuid.uuid4().hex[:12], "t": round(time.time(), 3), "x": uids[-40:]}
            try:
                self.mac.send(src, self._envelope(meta, b"", src), PRIO_NORMAL)
            except Exception as e:
                self.log("receipt error", e)
        self.publish("unread", dict(self.unread))

    def _on_tx_event(self, uid: str, dst: int, ev: str, m):
        with self.lock:
            r = self.messages.get(uid)
            if not r:
                return
            if ev in ("sent", "delivered", "failed"):
                if r["recip"].get(str(dst)) != "read":
                    r["recip"][str(dst)] = ev
            if ev != "failed":
                r["progress"] = round(len(m.acked) / len(m.frags), 3)
            r["frags"] = len(m.frags)
            r["retries"] = r.get("retries", 0) + (1 if ev == "retry" else 0)
            if ev == "delivered" and m.first_tx is not None and m.done_time:
                r["airtime_s"] = round(m.done_time - m.first_tx, 2)
                r["goodput_kbps"] = round(len(m.data) * 8 / max(m.done_time - m.first_tx, 1e-3) / 1e3, 1)
            r["status"] = self._agg_status(r)
            self._persist(r)
        self.publish("msg", r)

    @staticmethod
    def _agg_status(r):
        vals = list(r["recip"].values())
        for s in ("failed", "queued", "sent", "delivered", "read"):
            if s in vals:
                return s
        return "queued"

    # ================================================================ receiving
    def _on_message(self, src: int, blob: bytes, meta_l: dict):
        dst = BROADCAST_ADDR if meta_l.get("broadcast") else self.addr
        try:
            inner, enc = self.crypto.open(blob, src, dst)
            mlen = struct.unpack_from(">H", inner)[0]
            meta = json.loads(inner[2:2 + mlen].decode())
            body = inner[2 + mlen:]
        except Exception as e:
            self.log(f"node{self.addr}: dropped message from {src}: {e}")
            self.publish("toast", f"Message from node {src} could not be opened: {e}")
            return
        kind = meta.get("k", "text")
        with self.lock:
            c = self.contacts.setdefault(src, {"addr": src, "name": f"Node {src}"})
            c.update(last_seen=time.time(), online=True, snr_db=round(meta_l.get("snr_db", 0), 1))
        if kind == "rcpt":
            self._on_receipt(src, meta.get("x") or [])
            return
        if meta.get("z"):
            body = zlib.decompress(body)
        uid = meta.get("u") or uuid.uuid4().hex[:12]
        chat = meta.get("g") or chat_id_for(src)
        rec = dict(uid=uid, chat=chat, frm=src, kind=kind, ts=meta.get("t", time.time()),
                   rx_ts=time.time(), name=meta.get("n", ""), mime=meta.get("m", ""), size=len(body),
                   extra=meta.get("x"), urgent=bool(meta.get("!")), status="received", enc=enc,
                   snr_db=round(meta_l.get("snr_db", 0), 1), recip={})
        if kind == "text":
            rec["text"] = body.decode("utf-8", "replace")
        elif kind in MEDIA_KINDS:
            rec["file"] = self._save_media(uid, body, rec["mime"], rec["name"])
        with self.lock:
            if uid in self.messages:       # duplicate (e.g. re-sent after lost ACK)
                return
            self.unread[chat] = self.unread.get(chat, 0) + 1
        self._store(rec)
        self.publish("unread", dict(self.unread))

    def _on_receipt(self, src: int, uids: list):
        for uid in uids:
            with self.lock:
                r = self.messages.get(uid)
                if not r or str(src) not in r["recip"]:
                    continue
                r["recip"][str(src)] = "read"
                r["status"] = self._agg_status(r)
                self._persist(r)
            self.publish("msg", r)

    # ================================================================ storage
    def _save_media(self, uid: str, body: bytes, mime: str, name: str) -> str:
        ext = EXT.get(mime) or os.path.splitext(name)[1][:8] or mimetypes.guess_extension(mime or "") or ".bin"
        fn = f"{uid}{ext}"
        with open(os.path.join(self.media_dir, fn), "wb") as f:
            f.write(body)
        return fn

    def _store(self, rec: dict):
        with self.lock:
            if rec["uid"] not in self.messages:
                self.order.append(rec["uid"])
            self.messages[rec["uid"]] = rec
            self._persist(rec)
        self.publish("msg", rec)

    def _persist(self, rec: dict):
        try:
            with open(self.hist_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def _load_history(self):
        if not os.path.exists(self.hist_path):
            return
        with open(self.hist_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["uid"] not in self.messages:
                    self.order.append(r["uid"])
                if r.get("status") in ("queued", "sent"):
                    r["status"] = "failed"          # lost on restart
                self.messages[r["uid"]] = r
        # compact the log
        with open(self.hist_path, "w", encoding="utf-8") as f:
            for uid in self.order:
                f.write(json.dumps(self.messages[uid], separators=(",", ":")) + "\n")

    def clear_history(self):
        with self.lock:
            self.messages.clear()
            self.order.clear()
            self.unread.clear()
            open(self.hist_path, "w").close()
        self.publish("reset", None)

    # ================================================================ snapshots for UI
    def state(self) -> dict:
        with self.lock:
            return dict(me=dict(addr=self.addr, name=self.name, enc=self.crypto.enabled,
                                key_id=self.crypto.key_id, mode=self.mode),
                        contacts=self.contact_list(),
                        messages=[self.messages[u] for u in self.order[-2000:]],
                        unread=dict(self.unread),
                        phy=self.radio.cfg.summary(),
                        max_bytes=self.mac.max_message_bytes())

    def radio_status(self) -> dict:
        d = dict(radio=self.radio.status(), mac=dict(self.mac.stats),
                 links={str(k): v for k, v in self.mac.link_quality.items()},
                 queue=self.mac.pending_summary(), incoming=self.mac.incoming_progress(),
                 spectrum=self.radio.spectrum(), now=self.radio.now())
        air = getattr(self.radio, "air", None)
        if air is not None:
            d["sim"] = dict(ebn0_db=air.ebn0_db, cpu_load=round(air.cpu_load, 2),
                            links=[l for l in air.links() if l["tx"] == self.addr or l["rx"] == self.addr])
        return d
