"""End-to-end scenario against a running simulation:
    python -m cdmachat sim --nodes 3      (in another terminal)
    python -m tests.scenario_api
Sends text, a group message, an image, a game move and read receipts concurrently.
"""
import io
import json
import os
import time
import urllib.request


def post(port, path, body, raw=False):
    data = body if raw else json.dumps(body).encode()
    req = urllib.request.Request(f"http://localhost:{port}{path}", data=data, method="POST")
    return json.load(urllib.request.urlopen(req))


def get(port, path):
    return json.load(urllib.request.urlopen(f"http://localhost:{port}{path}"))


def test_image() -> bytes:
    try:
        import numpy as np
        from PIL import Image
        yy, xx = np.mgrid[0:240, 0:320]
        a = np.stack([(xx * .8) % 255, yy % 255, ((xx + yy) * .5) % 255], -1).astype("uint8")
        b = io.BytesIO()
        Image.fromarray(a).save(b, "JPEG", quality=60)
        return b.getvalue()
    except ImportError:
        return os.urandom(15000)


def main():
    img = test_image()
    t0 = time.time()
    r = dict(
        txt=(8081, post(8081, "/api/send", {"to": "u:2", "text": "Hi Bob! over DS-CDMA"})),
        grp=(8081, post(8081, "/api/send", {"to": "g:all", "text": "Hello everyone", "urgent": True})),
        img=(8082, post(8082, "/api/upload?to=u:1&kind=image&name=test.jpg&mime=image/jpeg", img, raw=True)),
        game=(8083, post(8083, "/api/game", {"to": "u:1", "state": {"gid": "abc", "b": "X--------", "p": [3, 1], "t": 1}})),
    )
    done = {}
    while len(done) < len(r) and time.time() - t0 < 60:
        time.sleep(0.5)
        S = {p: {m["uid"]: m for m in get(p, "/api/state")["messages"]} for p in (8081, 8082, 8083)}
        for name, (p, rec) in r.items():
            m = S[p][rec["uid"]]
            if name not in done and m["status"] in ("delivered", "failed", "read"):
                done[name] = (m["status"], round(time.time() - t0, 1), m["recip"], m.get("goodput_kbps"))
    print(f"image {len(img)} bytes")
    for k, v in done.items():
        print(f"  {k:<5} {v}")
    s1 = get(8081, "/api/state")
    print("  Alice received:", [(m["kind"], m["frm"]) for m in s1["messages"] if m["frm"] != 1])
    post(8082, "/api/read", {"chat": "u:1"})
    post(8083, "/api/read", {"chat": "g:all"})
    time.sleep(2.5)
    S1 = {m["uid"]: m for m in get(8081, "/api/state")["messages"]}
    print("  after read receipts:", S1[r["txt"][1]["uid"]]["status"], S1[r["grp"][1]["uid"]]["recip"])
    print("  Alice MAC:", get(8081, "/api/radio")["mac"])
    ok = all(v[0] in ("delivered", "read") for v in done.values()) and len(done) == len(r)
    print("SCENARIO", "PASSED" if ok else "FAILED")


if __name__ == "__main__":
    main()
