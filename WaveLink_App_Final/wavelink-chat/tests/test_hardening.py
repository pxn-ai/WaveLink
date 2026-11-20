"""Hardening checks (code review, Nov 2026): what arrives over the air or from a browser is
validated, --key can't be bypassed, TD-CDMA ACK sections longer than 255 bytes, partial messages
that will never complete, and stations whose app restarts.

    python3 -m tests.test_hardening          # everything (about a minute)
    python3 -m tests.test_hardening 1 2 4    # pick parts: 1 messages, 2 link layer, 3 simulated
                                             # air (TD-CDMA restarts, stale transfers), 4 web server
"""
import json
import os
import re
import socket
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

from wavelink import tdma as T
from wavelink.config import PhyConfig
from wavelink.crypto import PLAIN
from wavelink.mac import HDR, T_ACK, T_DATA, add_crc
from wavelink.node import Node
from wavelink.radio_sim import SimAir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail != "" else ""), flush=True)


def wait(cond, timeout, step=0.05):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(step)
    return False


def plain(meta, body=b""):
    mj = json.dumps(meta).encode()
    return bytes([PLAIN]) + struct.pack(">H", len(mj)) + mj + body


class Frame:
    def __init__(self, payload=b"", snr_db=20.0):
        self.payload, self.snr_db, self.esn0_db, self.cfo_hz = payload, snr_db, snr_db, 0.0
        self.sample_index, self.mod = None, "qpsk-1/2"


def stations(tmp):
    """Stations that are never started: their application layer is fed directly."""
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=25, seed=4)
    quiet = lambda *a: None                                         # noqa: E731
    bob = Node(air.add_node(2), 2, "Bob", os.path.join(tmp, "n2"), None, log=quiet, mac="aloha")
    carol = Node(air.add_node(3), 3, "Carol", os.path.join(tmp, "n3"), "our-group-secret", log=quiet, mac="aloha")
    return bob, carol


# ============================================================================ 1. messages
def part1():
    print("1. messages from the air")
    tmp = tempfile.mkdtemp(prefix="hard1-")
    bob, carol = stations(tmp)
    meta_l = {"snr_db": 20}

    # message ids name files on disk
    bob._on_message(1, plain({"k": "file", "u": "../../escaped", "t": time.time(), "n": "x.py"}, b"print(1)\n"), meta_l)
    outside = [p for p in (os.path.join(tmp, "escaped.py"), os.path.join(tmp, "n2", "escaped.py")) if os.path.exists(p)]
    check("a message id can't put a file outside media/", not outside, outside or "nothing written outside")
    files = [r.get("file") for r in bob.messages.values() if r["kind"] == "file"]
    check("... the file is still received, under an id of our own",
          len(files) == 1 and all(re.fullmatch(r"[0-9a-f]{8,32}\.py", f or "") for f in files), files)
    bob._on_message(1, plain({"k": "file", "u": "0123456789ab", "t": time.time(), "n": "x.<script>"}, b"data"), meta_l)
    f = (bob.messages.get("0123456789ab") or {}).get("file", "")
    check("file extensions from the air are letters and digits only", re.fullmatch(r"0123456789ab\.[A-Za-z0-9]{1,7}", f), f)

    # --key: unencrypted envelopes are refused, sealed ones accepted
    n0 = len(carol.messages)
    carol._on_message(1, plain({"k": "text", "u": "aaaa1111aaaa", "t": time.time()}, b"no key needed?"), meta_l)
    check("with --key an unencrypted message is refused", len(carol.messages) == n0)
    mj = json.dumps({"k": "text", "u": "bbbb2222bbbb", "t": 1.0}).encode()
    inner = struct.pack(">H", len(mj)) + mj + b"hi"
    carol._on_message(1, carol.crypto.seal(inner, 1, 3), meta_l)
    r = carol.messages.get("bbbb2222bbbb")
    check("... an encrypted one is accepted", r is not None and r["enc"], r and r.get("text"))
    got = []
    carol.hub.on_datagram = lambda src, payload, frame=None: got.append(payload)
    carol._on_datagram(1, 3, bytes([PLAIN]) + b"\x01fake race state", Frame())
    check("... and so is an unencrypted datagram (game state)", not got, carol.mac.stats.get("dgram_undecryptable", 0))

    # the sender can't choose the chat
    bob._on_message(1, plain({"k": "text", "u": "cccc3333cccc", "t": time.time(), "g": "u:3"}, b"I am Carol"), meta_l)
    bob._on_message(1, plain({"k": "text", "u": "dddd4444dddd", "t": time.time(), "g": "g:all"}, b"to everyone"), meta_l)
    check("a message is filed in its sender's chat or the group, nowhere else",
          bob.messages["cccc3333cccc"]["chat"] == "u:1" and bob.messages["dddd4444dddd"]["chat"] == "g:all",
          (bob.messages["cccc3333cccc"]["chat"], bob.messages["dddd4444dddd"]["chat"]))

    # tic-tac-toe states and names end up in the page
    bad = {"gid": "x1", "b": ["<img src=x onerror=alert(1)>"] * 9, "p": [1, 2], "t": 2}
    bob._on_message(1, plain({"k": "game", "u": "eeee5555eeee", "t": time.time(), "x": bad}), meta_l)
    good = {"gid": "x2", "b": "X--------", "p": [1, 2], "t": 2}
    bob._on_message(1, plain({"k": "game", "u": "ffff6666ffff", "t": time.time(), "x": good}), meta_l)
    check("a malformed tic-tac-toe state is refused, a real one kept",
          "eeee5555eeee" not in bob.messages and (bob.messages.get("ffff6666ffff") or {}).get("extra") == good)
    try:
        bob.send("u:1", "game", b"", extra=bad)
        sent_bad = True
    except ValueError:
        sent_bad = False
    check("... and can't be sent through /api/game either", not sent_bad)
    bob._on_beacon(1, json.dumps({"n": "<img src=x onerror=alert(1)>\n" + "x" * 500}).encode(), Frame())
    nm = bob.contacts[1]["name"]
    check("names from beacons: at most 32 printable characters", len(nm) <= 32 and nm.isprintable(), repr(nm))

    # compressed bodies are inflated with a limit
    bomb = zlib.compress(b"\0" * 20_000_000, 9)
    bob._on_message(1, plain({"k": "text", "u": "abab7777abab", "t": time.time(), "z": 1}, bomb), meta_l)
    check(f"a {len(bomb) // 1000} KB zip bomb (20 MB of text) is refused", "abab7777abab" not in bob.messages)

    # control messages carry an id; a replayed copy is dropped
    seen = []
    bob.hub.on_gc = lambda src, obj: seen.append(obj)
    blob = plain({"k": "gc", "u": "1234567890ab"}, json.dumps({"t": "ping"}).encode())
    bob._on_message(1, blob, meta_l)
    bob._on_message(1, blob, meta_l)
    check("a game message replayed is handled once", len(seen) == 1, len(seen))
    m = bob.send_gc(1, {"t": "x"})
    inner, _enc = bob.crypto.open(m.data, 2, 1)
    mlen = struct.unpack_from(">H", inner)[0]
    check("... and every control message we send has an id", isinstance(json.loads(inner[2:2 + mlen]).get("u"), str))

    # a transfer that failed is cancelled at the receiver too
    class M:
        msg_id, first_tx, frags, acked, done_time, via, data = 4321, 1.0, [b""] * 9, set(), None, None, b""
    rec = bob.send("u:1", "file", b"x" * 5000, name="a.bin")
    bob.mac.out.clear()
    bob._on_tx_event(rec["uid"], 1, "failed", M())
    ctl = []
    for mm in bob.mac.out.get(1, []):
        inner, _ = bob.crypto.open(mm.data, 2, 1)
        mlen = struct.unpack_from(">H", inner)[0]
        ctl.append(json.loads(inner[2:2 + mlen]))
    check("a failed transfer tells the receiver to drop what it has",
          any(c.get("k") == "ctl" and (c.get("x") or {}).get("a") == "cancel" and c["x"].get("m") == 4321 for c in ctl), ctl)

    # game rooms from invitations
    hub = bob.hub
    hub._handle(1, {"t": "inv", "r": '"><img src=x onerror=alert(1)>', "g": "uno", "h": 1, "p": [1, 2], "o": {}})
    hub._handle(1, {"t": "inv", "r": "a1b2c3", "g": "race", "h": 1, "p": [1, 2], "o": {"laps": "<b>9</b>"}})
    rooms = dict(hub.rooms)
    check("an invitation with a malformed room id is ignored", list(rooms) == ["a1b2c3"], list(rooms))
    laps = (rooms.get("a1b2c3") or {}).get("o", {}).get("laps")
    check("... and room options are checked", laps in (None, 2, 3, 5), repr(laps))


# ============================================================================ 2. link layer
def part2():
    print("2. link layer")
    # an ACK section longer than 255 bytes is sent in pieces; the receiver joins them
    recs, want = b"", []
    for k, (sender, mid) in enumerate([(1, 100), (2, 200), (3, 300), (1, 101)]):
        bm = bytes([0xAA]) * 62
        recs += T.REC.pack(sender, 4, mid, 400, 10 + k, 60, 40, len(bm)) + bm
        want.append((sender, mid, 10 + k, len(bm)))
    secs = [(t, d) for t, d in T._sections(T._sec(T.SEC_ACK, recs) + T._sec(T.SEC_LINK, b"\x01\x20"))]
    acks = [d for t, d in secs if t == T.SEC_ACK]
    got, i = [], 0
    data = b"".join(acks)
    while i + T.REC.size <= len(data):
        s, a, mid, cnt, base, trig, snr, nbm = T.REC.unpack_from(data, i)
        got.append((s, mid, base, len(data[i + T.REC.size:i + T.REC.size + nbm])))
        i += T.REC.size + nbm
    check(f"a {len(recs)}-byte ACK section comes back whole", len(acks) == 1 and got == want, f"{len(acks)} section(s)")
    check("... and the sections after it are untouched", [t for t, _ in secs] == [T.SEC_ACK, T.SEC_LINK])
    room_fn = getattr(T, "_sec_room", None)
    worst = None if room_fn is None else max(len(T._sec(T.SEC_ACK, bytes(max(0, room_fn(r))))) - r for r in range(16, 1200))
    check("the ACK section fits the header's room, piece headers included", worst is not None and worst <= 0, worst)

    # implausible slot plans are refused (a cycle of length 0 would hang the receiver)
    try:
        T.Plan.decode(T.PLAN_HEAD.pack(1, 7, 0, 1) + T.SLOT.pack(1, 0, 0))
        refused = False
    except struct.error:
        refused = True
    ok = T.Plan.decode(T.Plan.build(1, [(1, 60)], 46).encode(7))[1].T == 106
    check("a slot plan with a zero-length cycle is refused, a real one decoded", refused and ok)

    # malformed frames and errors while handling a frame never reach the PHY's thread
    tmp = tempfile.mkdtemp(prefix="hard2-")
    bob, _ = stations(tmp)
    mac = bob.mac
    frames = [HDR.pack(T_DATA, 1, 2, 1, 77) + struct.pack(">HH", 5, 1) + b"x",       # fragment 5 of 1
              HDR.pack(T_DATA, 1, 2, 1, 78) + struct.pack(">HH", 0, 0) + b"x",       # of 0
              HDR.pack(T_DATA, 1, 2, 1, 79) + b"\x00",                               # too short
              HDR.pack(T_ACK, 3, 2, 1, 80) + b"\x00\x01"]                            # too short
    raised = []
    for body in frames:
        try:
            mac._on_phy_frame(Frame(add_crc(body)))
        except Exception as e:
            raised.append(repr(e))
    check("malformed frames are dropped, not raised", not raised and mac.stats.get("bad_frames", 0) >= 4,
          raised or dict(bad_frames=mac.stats.get("bad_frames", 0)))
    orig = mac._on_data
    mac._on_data = lambda *a, **k: 1 / 0
    try:
        mac._on_phy_frame(Frame(add_crc(HDR.pack(T_DATA, 1, 2, 1, 81) + struct.pack(">HH", 0, 1) + b"x")))
        guarded = mac.stats.get("rx_frame_errors", 0) >= 1
    except ZeroDivisionError:
        guarded = False
    mac._on_data = orig
    check("an error while handling a frame is counted, not raised", guarded)
    from wavelink.grc_phy import LdpcDecoderCore
    try:
        data, ok_, it = LdpcDecoderCore().decode([0.1] * 100, 20, "bogus", 9)
        safe = not ok_ and len(data) == 20
    except Exception:
        safe = False
    check("the flowgraph's LDPC decoder core never raises", safe)

    # the message-id counter carries on across a restart
    path = os.path.join(tmp, "ids.json")
    ids1, ids2 = None, None
    if hasattr(mac, "set_id_store"):
        mac.set_id_store(path)
        ids1 = [mac._alloc_id() for _ in range(300)]
        bob2, _ = stations(tempfile.mkdtemp(prefix="hard2b-"))
        bob2.mac.set_id_store(path)
        ids2 = [bob2.mac._alloc_id() for _ in range(300)]
    check("a restarted station doesn't reuse message ids", ids1 is not None and not set(ids1) & set(ids2),
          None if ids1 is None else f"first ids {ids1[0]}.., after restart {ids2[0]}..")


# ============================================================================ 3. simulated air
def part3():
    print("3. TD-CDMA on the simulated air: restarts, transfers that stop")
    import random
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=25, seed=5)
    tmp = tempfile.mkdtemp(prefix="hard3-")
    a, b = [Node(air.add_node(i), i, n, os.path.join(tmp, f"n{i}"), None, beacon_period=1.0, log=lambda *x: None)
            for i, n in ((1, "Alice"), (2, "Bob"))]
    for n in (a, b):
        n.start()
    try:
        if not wait(lambda: b.contacts.get(1, {}).get("online") and a.contacts.get(2, {}).get("online"), 40):
            check("the two stations hear each other", False)
            return
        # Alice's app restarts and its new id counter lands on ids Bob still remembers
        first = a.mac._next_id + 1
        old = [a.send("u:2", "text", f"before {i}".encode(), text=f"before {i}")["uid"] for i in range(3)]
        wait(lambda: all(a.messages[u]["status"] == "delivered" for u in old), 30)
        new_nonce = random.randrange(1, 1 << 16)
        with a.mac.lock:
            a.mac._next_id = (first - 1) & 0xFFFF
            a.mac.tdma.nonce = new_nonce
        wait(lambda: (b.mac.tdma.heard.get(1) or {}).get("nonce") == new_nonce, 10)
        new = [a.send("u:2", "text", f"after {i}".encode(), text=f"after {i}")["uid"] for i in range(3)]
        wait(lambda: all(a.messages[u]["status"] in ("delivered", "failed") for u in new), 30)
        check("a sender restart: its messages still arrive even if their ids were used before",
              all(u in b.messages for u in new), [u in b.messages for u in new])

        # Bob's app restarts in the middle of a file from Alice
        data = os.urandom(60_000)
        rec = a.send("u:2", "file", data, name="f.bin", mime="application/octet-stream")
        wait(lambda: any(x["have"] > 15 for x in b.mac.incoming_progress()), 30)
        with b.mac.lock:
            b.mac.inbox.clear()
            b.mac.done_in.clear()
            b.mac.tdma.ack_due.clear()
            b.mac.tdma.nonce = random.randrange(1, 1 << 16)
        wait(lambda: a.messages[rec["uid"]]["status"] in ("delivered", "failed"), 60)
        got = [r for r in b.messages.values() if r["frm"] == 1 and r["kind"] == "file"]
        same = bool(got) and open(os.path.join(b.media_dir, got[0]["file"]), "rb").read() == data
        check("a receiver restart mid-transfer: the file is sent again and arrives intact",
              same and a.messages[rec["uid"]]["status"] == "delivered", a.messages[rec["uid"]]["status"])

        # Alice goes out of range for good in the middle of a file
        b.INCOMING_STALE_S = 3.0
        a.send("u:2", "file", os.urandom(120_000), name="g.bin", mime="application/octet-stream")
        wait(lambda: any(x["have"] > 10 for x in b.mac.incoming_progress()), 30)
        air.set_link(1, 2, cut=True)
        t0 = b.radio.now()
        gone = wait(lambda: not b.mac.incoming_progress(), 40)
        check("a transfer that stopped arriving is dropped at the receiver", gone,
              f"after {b.radio.now() - t0:.1f} s of radio time" if gone else b.mac.incoming_progress())
    finally:
        for n in (a, b):
            n.stop()


# ============================================================================ 4. web server
def part4():
    print("4. web server")
    from wavelink.web.server import serve
    tmp = tempfile.mkdtemp(prefix="hard4-")
    bob, _ = stations(tmp)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = serve(bob, "127.0.0.1", port)
    base = f"http://127.0.0.1:{port}"

    def req(path, data=None, headers=None):
        r = urllib.request.Request(base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    try:
        st, h, body = req("/")
        inline = re.findall(rb"<script(?![^>]*\bsrc=)[^>]*>", body)
        check("the page sends a Content-Security-Policy without inline scripts",
              "script-src 'self'" in h.get("Content-Security-Policy", "") and not inline,
              h.get("Content-Security-Policy") or f"{len(inline)} inline script(s)")
        boot = re.findall(rb'<script src="(/static/[a-z]+\.js)"', body)
        check("... and every script it loads is there", boot and all(req(p.decode())[0] == 200 for p in boot),
              len(boot))
        with open(os.path.join(bob.media_dir, "0123456789ab.html"), "wb") as f:
            f.write(b"<script>alert(1)</script>")
        st, h, _ = req("/media/0123456789ab.html")
        check("received files can't run scripts in the page's origin",
              st == 200 and "sandbox" in h.get("Content-Security-Policy", "")
              and h.get("Content-Disposition", "").startswith("attachment") and h.get("X-Content-Type-Options") == "nosniff",
              {k: h.get(k) for k in ("Content-Security-Policy", "Content-Disposition")})
        body = json.dumps({"chat": "u:1"}).encode()
        st_evil = req("/api/read", body, {"Origin": "http://evil.example"})[0]
        st_same = req("/api/read", body, {"Origin": base})[0]
        st_none = req("/api/read", body)[0]
        check("POSTs from other web pages are refused, the station's own page and scripts work",
              st_evil == 403 and st_same == 200 and st_none == 200, (st_evil, st_same, st_none))
        st_rebind = req("/api/state", headers={"Host": f"evil.example:{port}"})[0]
        st_local = req("/api/state", headers={"Host": f"localhost:{port}"})[0]
        check("requests for another host name are refused (DNS rebinding)", st_rebind == 403 and st_local == 200,
              (st_rebind, st_local))
    finally:
        srv.shutdown()
    js = open(os.path.join(ROOT, "wavelink", "web", "static", "app.js"), encoding="utf-8").read()
    gj = open(os.path.join(ROOT, "wavelink", "web", "static", "games.js"), encoding="utf-8").read()
    check("the page escapes names and room ids it puts into HTML",
          "cancelled by ${esc(contactName" in js and 'data-r="${r.r}"' not in gj and "${r.o.laps} laps`" not in gj)


PARTS = {1: part1, 2: part2, 3: part3, 4: part4}


def main():
    pick = [int(x) for x in sys.argv[1:] if x.isdigit()] or list(PARTS)
    for k in pick:
        try:
            PARTS[k]()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(f"part {k} ran to the end", False, repr(e))
    n = sum(RESULTS)
    print(f"hardening: {'PASS' if all(RESULTS) else 'FAIL'} ({n}/{len(RESULTS)} checks)")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
