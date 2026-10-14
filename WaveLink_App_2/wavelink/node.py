"""Application layer: a WhatsApp-like station built on top of the link layer.

Message envelope (inside the reliable link-layer message)::

    crypto byte (0x01 plain / 0x02 AES-GCM) | meta_len (2 B) | meta JSON | body bytes

meta keys: k (kind), u (uid), t (timestamp), g (group id), n (file name),
m (mime type), x (extra, kind specific), z (body is zlib-compressed).
Kinds: text, image, audio, file, game (tic-tac-toe), rcpt (read receipt), gc (game center:
JSON body, handed to games.hub.GameHub), prof (profile picture request / reply) and ctl
(transfer control: a cancelled transfer).  gc, prof and ctl are not kept in the history.

Presence beacons carry the name, the encryption key id, an avatar tag ('e:<hue>:<emoji>' or
'p:<hash>' for a photo, fetched with a 'prof' request when it changes) and the sender's clock,
from which each station estimates the others' clock offsets: a message is placed in the chat by
when it was sent, so one that arrives late (behind a big file, after retransmissions) still
lands where it belongs.

Game center datagrams (clock sync, race car states) are sealed the same way (crypto byte,
AES-GCM when --key is set) but carry no meta: they are binary and tiny.
"""
from __future__ import annotations

import hashlib
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
from .games.hub import GameHub

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
        self.avatar_dir = os.path.join(data_dir, "avatars")
        os.makedirs(self.avatar_dir, exist_ok=True)
        self.profile_path = os.path.join(data_dir, "profile.json")
        self.profile: dict = {}
        self._load_profile()
        self.out_by_uid: dict[str, list] = {}          # uid -> [(dst, OutMsg)] (cancel)
        self._cancelled: set = set()                   # uids cancelled by us
        self.skew: dict[int, list] = {}                # addr -> recent (our clock - their clock)
        self._av_req: dict[int, float] = {}            # addr -> when we last asked for its photo
        self.mac = LinkLayer(radio, addr, on_message=self._on_message, on_beacon=self._on_beacon,
                             on_datagram=self._on_datagram, log=log)
        self.hub = GameHub(self, log=log)
        self._load_history()
        self._running = False

    # ================================================================ lifecycle
    def start(self):
        self._running = True
        self.radio.start()
        self.mac.start()
        self.hub.start()
        threading.Thread(target=self._beacon_loop, name=f"beacon{self.addr}", daemon=True).start()

    def stop(self):
        self._running = False
        self.hub.stop()
        self.mac.stop()
        self.radio.stop()

    def _beacon_loop(self):
        import random
        # a few quick beacons at start-up so peers appear immediately
        delays = [0.2, 1.0, 2.0]
        while self._running:
            info = json.dumps({"n": self.name, "e": self.crypto.key_id, "a": self.avatar_tag(),
                               "t": round(time.time(), 2)}, separators=(",", ":"), ensure_ascii=False).encode()
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
        if isinstance(d.get("t"), (int, float)):
            self._note_clock(src, float(d["t"]))
        self._note_avatar(src, str(d.get("a") or ""))
        self.publish("contacts", self.contact_list())

    def _refresh_presence(self):
        changed = False
        with self.lock:
            for c in self.contacts.values():
                # any frame counts, not only beacons: a beacon can collide with game traffic
                heard = max(c.get("last_seen", 0), self.mac.last_heard.get(c["addr"], 0))
                on = time.time() - heard < 3.5 * self.beacon_period
                if on != c.get("online"):
                    c["online"] = on
                    changed = True
        if changed:
            self.publish("contacts", self.contact_list())

    def contact_list(self) -> list[dict]:
        with self.lock:
            out = []
            for c in sorted(self.contacts.values(), key=lambda c: c["addr"]):
                c = dict(c)
                c["tx_mod"] = self.mac.mod_for(c["addr"])       # what we'd send to them now
                out.append(c)
            return out

    def set_name(self, name: str):
        self.name = name.strip()[:32] or self.name
        self.profile["name"] = self.name
        self._save_profile()
        self.publish("profile", self.profile_public())

    # ================================================================ profile, avatars, wallpaper
    def _load_profile(self):
        try:
            with open(self.profile_path, encoding="utf-8") as f:
                self.profile = json.load(f)
        except (OSError, ValueError):
            self.profile = {}
        if not isinstance(self.profile.get("prefs"), dict):
            self.profile["prefs"] = {}
        if not isinstance(self.profile.get("avatar"), dict):
            self.profile["avatar"] = {"kind": "initial"}
        if self.profile["avatar"].get("kind") == "photo" and not self.profile.get("photo"):
            self.profile["photo"] = dict(self.profile["avatar"])
        if self.profile.get("name"):
            self.name = str(self.profile["name"])[:32]       # renamed in the UI: wins over --name

    def _save_profile(self):
        tmp = self.profile_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.profile, f, ensure_ascii=False)
            os.replace(tmp, self.profile_path)
        except OSError as e:
            self.log("profile save failed", e)

    def avatar_tag(self) -> str:
        a = self.profile.get("avatar") or {}
        if a.get("kind") == "emoji" and a.get("emoji"):
            return f"e:{int(a.get('hue', 200)) % 360}:{a['emoji']}"
        if a.get("kind") == "photo" and a.get("hash"):
            return f"p:{a['hash']}"
        if a.get("kind") == "initial" and a.get("hue") is not None:
            return f"i:{int(a['hue']) % 360}"
        return ""

    def _avatar_public(self, a: dict, who: str) -> dict:
        out = {k: v for k, v in a.items() if k in ("kind", "emoji", "hue", "hash")}
        if a.get("kind") == "photo" and a.get("file"):
            out["url"] = f"/avatar/{who}?h={a.get('hash', '')}"
        return out

    def profile_public(self) -> dict:
        p = self.profile
        wall = p["prefs"].get("wall")
        photo = p.get("photo") or {}
        return dict(name=self.name, avatar=self._avatar_public(p.get("avatar") or {}, "me"), prefs=dict(p["prefs"]),
                    wallpaper=f"/wallpaper?v={p.get('wall_v', 0)}" if wall == "custom" and p.get("wall_file") else None,
                    has_photo=bool(photo.get("file")), has_wallpaper=bool(p.get("wall_file")),
                    wallpaper_url=f"/wallpaper?v={p.get('wall_v', 0)}" if p.get("wall_file") else None)

    def update_profile(self, name: str | None = None, avatar: dict | None = None, prefs: dict | None = None):
        with self.lock:
            if name is not None and name.strip():
                self.name = name.strip()[:32]
                self.profile["name"] = self.name
            if prefs:
                for k, v in prefs.items():
                    if not isinstance(k, str) or len(k) > 24:
                        continue
                    if v is None:
                        self.profile["prefs"].pop(k, None)
                    else:
                        self.profile["prefs"][k] = v
            if avatar is not None:
                kind = avatar.get("kind")
                if kind == "emoji":
                    emoji = str(avatar.get("emoji", ""))[:16]
                    if not emoji:
                        raise ValueError("pick an emoji")
                    self.profile["avatar"] = {"kind": "emoji", "emoji": emoji, "hue": int(avatar.get("hue", 200)) % 360}
                elif kind == "initial":
                    self.profile["avatar"] = {"kind": "initial", "hue": int(avatar.get("hue", 0)) % 360} \
                        if avatar.get("hue") is not None else {"kind": "initial"}
                elif kind == "photo":                       # back to the last photo uploaded
                    ph = self.profile.get("photo") or {}
                    if not ph.get("file") or not os.path.exists(os.path.join(self.avatar_dir, ph["file"])):
                        raise ValueError("upload a photo first")
                    self.profile["avatar"] = {"kind": "photo", "hash": ph["hash"], "mime": ph["mime"], "file": ph["file"]}
                else:
                    raise ValueError("unknown avatar kind")
            self._save_profile()
        self.publish("profile", self.profile_public())
        return self.profile_public()

    def set_avatar_photo(self, data: bytes, mime: str):
        if len(data) > 96_000:
            raise ValueError("profile photo too large (the page shrinks it to 160 px first)")
        if mime not in ("image/webp", "image/jpeg", "image/png"):
            raise ValueError("profile photo must be WebP, JPEG or PNG")
        h = hashlib.sha1(data).hexdigest()[:10]
        fn = f"me-{h}{EXT.get(mime, '.img')}"
        with open(os.path.join(self.avatar_dir, fn), "wb") as f:
            f.write(data)
        for old in os.listdir(self.avatar_dir):              # earlier photos of mine
            if old.startswith("me-") and old != fn:
                try:
                    os.remove(os.path.join(self.avatar_dir, old))
                except OSError:
                    pass
        with self.lock:
            self.profile["avatar"] = {"kind": "photo", "hash": h, "mime": mime, "file": fn}
            self.profile["photo"] = dict(self.profile["avatar"])
            self._save_profile()
        self.publish("profile", self.profile_public())
        return self.profile_public()

    def avatar_file(self, who: str) -> str | None:
        if who == "me":
            fn = (self.profile.get("avatar") or {}).get("file")
            return os.path.join(self.avatar_dir, fn) if fn else None
        try:
            c = self.contacts.get(int(who)) or {}
        except ValueError:
            return None
        fn = (c.get("av") or {}).get("file")
        return os.path.join(self.avatar_dir, fn) if fn else None

    def set_wallpaper(self, data: bytes, mime: str):
        if len(data) > 12_000_000:
            raise ValueError("wallpaper too large (max 12 MB)")
        if not mime.startswith("image/"):
            raise ValueError("wallpaper must be an image")
        ext = EXT.get(mime) or mimetypes.guess_extension(mime) or ".img"
        for old in os.listdir(self.data_dir):
            if old.startswith("wallpaper."):
                try:
                    os.remove(os.path.join(self.data_dir, old))
                except OSError:
                    pass
        fn = "wallpaper" + ext
        with open(os.path.join(self.data_dir, fn), "wb") as f:
            f.write(data)
        with self.lock:
            self.profile["wall_file"] = fn
            self.profile["wall_v"] = int(time.time())
            self.profile["prefs"]["wall"] = "custom"
            self._save_profile()
        self.publish("profile", self.profile_public())
        return self.profile_public()

    def wallpaper_file(self) -> str | None:
        fn = self.profile.get("wall_file")
        return os.path.join(self.data_dir, fn) if fn else None

    def _note_avatar(self, src: int, tag: str):
        """A contact's beacon advertises its avatar; fetch a new photo once."""
        with self.lock:
            c = self.contacts.setdefault(src, {"addr": src})
            av = c.get("av") or {}
            if tag.startswith("e:"):
                _, hue, emoji = (tag.split(":", 2) + ["", ""])[:3]
                c["av"] = {"kind": "emoji", "emoji": emoji[:16], "hue": int(hue) if hue.isdigit() else 200}
                return
            if tag.startswith("i:") and tag[2:].isdigit():
                c["av"] = {"kind": "initial", "hue": int(tag[2:])}
                return
            if not tag.startswith("p:"):
                c["av"] = {"kind": "initial"}
                return
            h = tag[2:14]
            if av.get("kind") == "photo" and av.get("hash") == h and av.get("file"):
                return
            for ext in (".webp", ".jpg", ".png"):                 # cached from an earlier session?
                fn = f"{src}-{h}{ext}"
                if os.path.exists(os.path.join(self.avatar_dir, fn)):
                    c["av"] = {"kind": "photo", "hash": h, "file": fn, "url": f"/avatar/{src}?h={h}"}
                    return
            if time.time() - self._av_req.get(src, 0) < 45:
                return
            self._av_req[src] = time.time()
        meta = {"k": "prof", "x": {"q": "photo", "h": h}}
        try:
            self.mac.send(src, self._envelope(meta, b"", src), PRIO_NORMAL)
        except Exception as e:
            self.log("avatar request failed", e)

    def _on_prof(self, src: int, x: dict, body: bytes):
        if x.get("q") == "photo":                                   # someone wants our photo
            a = self.profile.get("avatar") or {}
            path = self.avatar_file("me")
            if a.get("kind") != "photo" or not path or not os.path.exists(path):
                return
            with open(path, "rb") as f:
                data = f.read()
            meta = {"k": "prof", "x": {"r": "photo", "h": a.get("hash"), "m": a.get("mime", "image/webp")}}
            self.mac.send(src, self._envelope(meta, data, src), PRIO_NORMAL)
        elif x.get("r") == "photo" and body:                        # a contact's photo
            h = str(x.get("h", ""))[:12]
            if hashlib.sha1(body).hexdigest()[:10] != h[:10]:
                return
            fn = f"{src}-{h}{EXT.get(x.get('m'), '.img')}"
            with open(os.path.join(self.avatar_dir, fn), "wb") as f:
                f.write(body)
            with self.lock:
                c = self.contacts.setdefault(src, {"addr": src})
                c["av"] = {"kind": "photo", "hash": h, "file": fn, "url": f"/avatar/{src}?h={h}"}
            self.publish("contacts", self.contact_list())

    # ================================================================ clocks of the other stations
    def _note_clock(self, src: int, their_t: float):
        """Their clock vs ours: the smallest (arrival - their send time) over the last minutes is the
        offset plus the shortest delivery delay."""
        now = time.time()
        with self.lock:
            lst = self.skew.setdefault(src, [])
            lst.append((now, now - their_t))
            cut = now - 300
            while lst and lst[0][0] < cut:
                lst.pop(0)

    def clock_offset(self, src: int) -> float:
        lst = self.skew.get(src) or []
        return min(d for _, d in lst) if lst else 0.0

    # ================================================================ cancelling transfers
    def cancel_send(self, uid: str):
        """Stop sending a message (a file) to every recipient that hasn't got it yet."""
        with self.lock:
            r = self.messages.get(uid)
            outs = list(self.out_by_uid.get(uid, []))
            if not r or r.get("frm") != self.addr:
                raise ValueError("no such outgoing message")
            if r.get("status") in ("delivered", "read", "cancelled"):
                raise ValueError("already delivered")
            self._cancelled.add(uid)
        for dst, m in outs:
            started = m.first_tx is not None
            if m.state.value in ("delivered", "failed", "withdrawn"):
                continue
            self.mac.cancel(dst, m.msg_id)
            if started:                       # the receiver holds part of it: tell it to drop that
                self._send_ctl(dst, {"a": "cancel", "m": m.msg_id})
        with self.lock:
            for d, st in r["recip"].items():
                if st in ("queued", "sent"):
                    r["recip"][d] = "cancelled"
            r["status"] = self._agg_status(r)
            self._persist(r)
        self.publish("msg", r)
        return r

    def cancel_incoming(self, src: int, msg_id: int):
        """Stop receiving a message: drop what has arrived and ask the sender to stop."""
        self.mac.drop_incoming(src, msg_id)
        self._send_ctl(src, {"a": "stop", "m": msg_id})
        self.publish("incoming", None)

    def _send_ctl(self, dst: int, x: dict):
        meta = {"k": "ctl", "x": x}
        try:
            self.mac.send(dst, self._envelope(meta, b"", dst), PRIO_HIGH)
        except Exception as e:
            self.log("control message failed", e)

    def _on_ctl(self, src: int, x: dict):
        a, mid = x.get("a"), x.get("m")
        if not isinstance(mid, int):
            return
        if a == "cancel":                     # the sender gave up: drop the partial message
            if self.mac.drop_incoming(src, mid):
                self.publish("toast", f"{(self.contacts.get(src) or {}).get('name', f'Node {src}')} cancelled a transfer")
            self.publish("incoming", None)
        elif a == "stop":                     # the receiver doesn't want it any more
            m = self.mac.find_out(src, mid)
            if m is None:
                return
            uid = None
            with self.lock:
                for u, outs in self.out_by_uid.items():
                    if any(d == src and om is m for d, om in outs):
                        uid = u
                        break
                if uid:
                    self._cancelled.add(uid)
            self.mac.cancel(src, mid)
            if uid:
                with self.lock:
                    r = self.messages.get(uid)
                    if r:
                        r["recip"][str(src)] = "cancelled"
                        r["status"] = self._agg_status(r)
                        r["stopped_by"] = src
                        self._persist(r)
                if r:
                    self.publish("msg", r)
                    self.publish("toast", f"{(self.contacts.get(src) or {}).get('name', f'Node {src}')} declined the transfer")

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
        elif kind == "file" and len(body) > 2048:
            # documents, logs, CSV ... often shrink a lot; skip already-compressed data
            sample = body[:262144]
            if len(zlib.compress(sample, 1)) < 0.9 * len(sample):
                z = zlib.compress(body, 6)
                if len(z) < 0.95 * len(body):
                    meta["z"] = 1
                    body = z
                    rec["wire_size"] = len(z)
        self._store(rec)
        outs = []
        for d in dsts:
            payload = self._envelope(meta, body, d)
            m = self.mac.send(d, payload, prio, on_event=lambda ev, m, d=d, uid=uid: self._on_tx_event(uid, d, ev, m))
            outs.append((d, m))
        with self.lock:
            self.out_by_uid[uid] = outs
            if len(self.out_by_uid) > 500:
                for k in list(self.out_by_uid)[:250]:
                    self.out_by_uid.pop(k, None)
        return rec

    def send_gc(self, dst: int, obj: dict, prio: int = PRIO_HIGH):
        """Game center message: reliable, high priority (overtakes chat and file traffic),
        not stored in the history."""
        body = json.dumps(obj, separators=(",", ":")).encode()
        meta = {"k": "gc"}
        z = zlib.compress(body, 6)
        if len(z) < len(body) - 8:
            meta["z"] = 1
            body = z
        return self.mac.send(dst, self._envelope(meta, body, dst), prio)

    def send_datagram(self, dst: int, payload: bytes, urgent: bool = True, at: float | None = None,
                      latest: float | None = None):
        return self.mac.send_datagram(dst, self.crypto.seal(payload, self.addr, dst), urgent, at, latest)

    def _on_datagram(self, src: int, dst: int, blob: bytes, frame):
        try:
            payload, _ = self.crypto.open(blob, src, dst)
        except ValueError:
            self.mac.stats["dgram_undecryptable"] += 1
            return
        self.hub.on_datagram(src, payload, frame)

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
                if ev == "failed" and uid in self._cancelled:
                    ev = "cancelled"
                if r["recip"].get(str(dst)) not in ("read", "cancelled"):
                    r["recip"][str(dst)] = ev
            if ev not in ("failed", "cancelled"):
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
        if vals and all(v == "cancelled" for v in vals):
            return "cancelled"
        for s in ("failed", "queued", "sent", "delivered", "read"):
            if s in vals:
                return s
        return "cancelled" if "cancelled" in vals else "queued"

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
        if kind == "gc":
            try:
                obj = json.loads(body.decode())
            except ValueError:
                return
            self.hub.on_gc(src, obj)
            return
        if kind == "prof":
            self._on_prof(src, meta.get("x") or {}, body)
            return
        if kind == "ctl":
            self._on_ctl(src, meta.get("x") or {})
            return
        uid = meta.get("u") or uuid.uuid4().hex[:12]
        chat = meta.get("g") or chat_id_for(src)
        now = time.time()
        sent = float(meta.get("t", now))
        if kind == "text":
            self._note_clock(src, sent)         # a short message: a good clock sample too
        # when it was sent, on our clock (the contact's clock may be off by seconds)
        sent_here = min(now, sent + self.clock_offset(src)) if self.skew.get(src) else min(now, sent)
        rec = dict(uid=uid, chat=chat, frm=src, kind=kind, ts=sent_here, ts_sender=sent,
                   rx_ts=now, name=meta.get("n", ""), mime=meta.get("m", ""), size=len(body),
                   extra=meta.get("x"), urgent=bool(meta.get("!")), status="received", enc=enc,
                   snr_db=round(meta_l.get("snr_db", 0), 1), recip={})
        if now - sent_here > 4.0:
            rec["delayed"] = round(now - sent_here, 1)
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
                    for d, st in (r.get("recip") or {}).items():
                        if st in ("queued", "sent"):
                            r["recip"][d] = "failed"
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
                        profile=self.profile_public(),
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
