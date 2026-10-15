"""Station features over the simulated air, without a browser: profile pictures travel with the
beacons, a file transfer can be cancelled by its sender or its receiver, and clock offsets are
estimated so late messages can be placed by send time.

    python3 -m tests.test_features
"""
import os
import shutil
import sys
import tempfile
import time

from wavelink.config import PhyConfig
from wavelink.node import Node
from wavelink.radio_sim import SimAir


def wait(cond, timeout, step=0.05):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(step)
    return False


def main():
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=25, seed=4)
    tmp = tempfile.mkdtemp(prefix="feat-")
    nodes = [Node(air.add_node(i), i, n, os.path.join(tmp, f"n{i}"), None, beacon_period=1.0, log=lambda *a: None)
             for i, n in ((1, "Alice"), (2, "Bob"))]
    a, b = nodes
    for n in nodes:
        n.start()
    ok = True
    try:
        assert wait(lambda: b.contacts.get(1, {}).get("online") and a.contacts.get(2, {}).get("online"), 20)

        # ---- emoji avatar: carried in the beacon
        a.update_profile(avatar={"kind": "emoji", "emoji": "🦊", "hue": 30})
        got = wait(lambda: (b.contacts[1].get("av") or {}).get("emoji") == "🦊", 10)
        print(f"  emoji avatar seen by Bob: {got}")
        ok &= got

        # ---- photo avatar: beacon carries the hash, Bob asks for the photo once
        photo = os.urandom(3000)                       # stands in for a 160 px WebP
        a.set_avatar_photo(photo, "image/webp")
        h = a.profile["avatar"]["hash"]
        got = wait(lambda: (b.contacts[1].get("av") or {}).get("hash") == h, 15)
        f = b.avatar_file("1")
        same = got and f and open(f, "rb").read() == photo
        print(f"  photo avatar fetched by Bob: {bool(same)} ({b.contacts[1]['av'].get('url')})")
        ok &= bool(same)

        # ---- emoji again, then back to the photo without uploading it again
        a.update_profile(avatar={"kind": "emoji", "emoji": "🐙", "hue": 200})
        a.update_profile(avatar={"kind": "photo"})
        back = a.avatar_tag() == f"p:{h}" and a.profile_public()["has_photo"]
        print(f"  back to the kept photo: {back}")
        ok &= back

        # ---- the sender cancels a file mid-transfer
        rec = a.send("u:2", "file", os.urandom(400_000), name="big.bin", mime="application/octet-stream")
        wait(lambda: b.mac.incoming_progress() and b.mac.incoming_progress()[0]["have"] > 20, 30)
        have = b.mac.incoming_progress()[0]["have"] if b.mac.incoming_progress() else 0
        a.cancel_send(rec["uid"])
        dropped = wait(lambda: not b.mac.incoming_progress(), 10)
        time.sleep(1.0)
        late = b.mac.stats.get("dropped_frames", 0)
        print(f"  sender cancel after {have} fragments: Alice shows '{a.messages[rec['uid']]['status']}', "
              f"Bob dropped the partial file: {dropped} (ignored {late} frames still in flight)")
        ok &= dropped and a.messages[rec["uid"]]["status"] == "cancelled"

        # ---- the receiver cancels an incoming file
        rec = a.send("u:2", "file", os.urandom(400_000), name="big2.bin", mime="application/octet-stream")
        wait(lambda: b.mac.incoming_progress() and b.mac.incoming_progress()[0]["have"] > 20, 30)
        inc = b.mac.incoming_progress()[0]
        b.cancel_incoming(inc["src"], inc["id"])
        stopped = wait(lambda: a.messages[rec["uid"]]["status"] == "cancelled", 10)
        print(f"  receiver cancel: Alice's transfer stopped: {stopped} "
              f"(recipient status {a.messages[rec['uid']]['recip']})")
        ok &= stopped and not b.mac.incoming_progress()

        # ---- after a cancel, the link still works
        r2 = a.send("u:2", "text", b"still here", text="still here")
        delivered = wait(lambda: a.messages[r2["uid"]]["status"] in ("delivered", "read"), 10)
        print(f"  a text after the cancels is delivered: {delivered}")
        ok &= delivered

        # ---- clock offsets (one process: all stations share the clock, so close to 0)
        off = b.clock_offset(1)
        print(f"  Bob's estimate of Alice's clock offset: {off * 1000:.0f} ms (true 0 ms + delivery time)")
        ok &= -0.05 < off < 1.0
    finally:
        for n in nodes:
            n.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print("station features:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
