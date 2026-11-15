"""TD-CDMA edge cases on GNU Radio with fake ANTSDRs.

Every station is a process of its own running the real app on the real flowgraph, exactly as on the
three PCs - `python3 -m wavelink run --id N --uri fake:...` - with only the flowgraph's two radio
blocks replaced (wavelink/fake_antsdr.py).  The air between them (FakeAir) runs in this process.

    python3 -m tests.test_fake_antsdr                 # all
    python3 -m tests.test_fake_antsdr 3 7 --speed 0.5 # just these, air at half speed
    python3 -m tests.test_fake_antsdr --quick         # shorter versions

  1  network formation: one after another (3, 1, 2), all at once, two lone stations that meet
  2  transfers on three links at once; a group message; nothing late, no slots overlapping
  3  the lead's PC dies mid-transfer (kill -9): its successor takes over; the lead comes back
  4  two radios set to the same address
  5  sample-clock error +25 / -25 / +10 ppm (50 ppm between stations) with traffic
  6  TX underruns and lost RX buffers (the stream slips) during a transfer
  7  a hidden station: 1 and 3 can't hear each other, 2 relays
  8  a station off the air for 1 s (keeps its slot) and for 3.5 s (rejoins)
  9  low SNR and bursty interference
 10  real time (air at full speed), two stations: transfer, no underruns, lead time
 11  spread ALOHA (--mac aloha) on the same flowgraph and radios
 12  game center: a 3-player race and an UNO round over the slots

Requires GNU Radio 3.10 in this Python (run it with the interpreter that has gnuradio).  The air
may run slower than real time (--speed, default 0.6) so that three stations fit on a small PC; the
stations' timers follow the air's sample clock, and slots, offsets and drift are all in samples.
"""
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from wavelink.fake_antsdr import FakeAir  # noqa: E402

QUICK = "--quick" in sys.argv
SPEED = 0.6
for i, a in enumerate(sys.argv):
    if a == "--speed" and i + 1 < len(sys.argv):
        SPEED = float(sys.argv[i + 1])
NAMES = {1: "Alice", 2: "Bob", 3: "Carol", 4: "Dave", 5: "Eve", 6: "Frank"}
SLIP_S = 0.5
RESULTS = []
METRICS = {}


def say(*a):
    print(*a, flush=True)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    say(f"    {'ok ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    return ok


def metric(key, value):
    METRICS[key] = value


# ============================================================================ harness
class Station:
    """One station: `python3 -m wavelink run` in its own process on a fake ANTSDR."""

    def __init__(self, lab, sid: int, slot: int | None = None, extra=()):
        self.lab, self.id = lab, sid
        self.slot = slot or sid
        self.port = lab.port0 + self.slot
        self.data = os.path.join(lab.tmp, f"st{self.slot}")
        self.log_path = os.path.join(lab.tmp, f"st{self.slot}.log")
        self.extra = list(extra)
        self.proc = None
        self.starts = 0

    def start(self):
        args = [sys.executable, "-m", "wavelink", "run", "--id", str(self.id), "--name", NAMES.get(self.id, f"N{self.id}"),
                "--uri", f"fake:{self.lab.air.name}/{self.slot}", "--port", str(self.port), "--data", self.data,
                "--key", "WaveLink"] + self.extra
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(args, cwd=ROOT, stdout=self.logf, stderr=subprocess.STDOUT, env=env)
        self.starts += 1

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def up(self) -> bool:
        try:
            self.get("/api/profile", timeout=2)
            return True
        except (OSError, ValueError):
            return False

    def wait_up(self, t=120) -> bool:
        t0 = time.time()
        while time.time() - t0 < t:
            if not self.alive():
                return False
            if self.up():
                return True
            time.sleep(0.5)
        return False

    def get(self, path, timeout=10):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as r:
            return json.load(r)

    def get_raw(self, path, timeout=20) -> bytes:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as r:
            return r.read()

    def post(self, path, body, raw=False, timeout=20):
        data = body if raw else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream" if raw else "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def radio(self):
        try:
            return self.get("/api/radio")
        except (OSError, ValueError):
            return None

    def td(self):
        r = self.radio()
        return (r or {}).get("tdma") or {}

    def messages(self):
        try:
            return {m["uid"]: m for m in self.get("/api/state", timeout=20)["messages"]}
        except (OSError, ValueError, KeyError):
            return {}

    def stop(self, kill=False):
        if not self.alive():
            return
        self.proc.send_signal(signal.SIGKILL if kill else signal.SIGTERM)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.logf.close()

    def log_tail(self, n=15) -> str:
        try:
            lines = [ln for ln in open(self.log_path, errors="replace").read().splitlines()
                     if "set_min_output_buffer" not in ln and "vmcircbuf" not in ln]
            return "\n".join(lines[-n:])
        except OSError:
            return ""


class Lab:
    """A fake air (in this process) and stations (processes) plugged into its slots."""

    def __init__(self, ids=(1, 2, 3), speed=None, esn0=28.0, seed=1, ppm=None, self_leak=25.0, start=True,
                 extra=None, slots=None, port0=None, stagger=0.0):
        self.tmp = tempfile.mkdtemp(prefix="wl-fake-")
        self.air = FakeAir(name=f"t{os.getpid()}x{seed}", slots=6, esn0_db=esn0, seed=seed,
                           speed=SPEED if speed is None else speed, self_leak_db=self_leak, log=self._log)
        self.logs = []
        for k, v in (ppm or {}).items():
            self.air.set_ppm(k, v)
        self.air.start()
        self.port0 = port0 or 18000 + 10 * (seed % 50)
        self.st = {}
        for i in ids:
            self.st[(slots or {}).get(i, i)] = Station(self, i, (slots or {}).get(i), (extra or {}).get(i, ()))
        self.check_from = None
        if start:
            self.start(*self.st, stagger=stagger)

    def _log(self, msg):
        self.logs.append((round(self.t(), 2), msg))

    def t(self) -> float:
        """Air time (s)."""
        return self.air.clock / self.air.fs

    def start(self, *slots, stagger=0.0, wait=True):
        for k, s in enumerate(slots):
            self.st[s].start()
            if stagger and k < len(slots) - 1:
                self.run(stagger)
        if wait:
            for s in slots:
                ok = self.st[s].wait_up()
                if not ok:
                    say(f"    station in slot {s} did not come up:\n{self.st[s].log_tail()}")
                    raise RuntimeError(f"station {s} failed to start")

    def run(self, secs):
        t0 = self.t()
        while self.t() - t0 < secs:
            time.sleep(0.1)

    def wait(self, cond, secs, poll=0.5) -> bool:
        t0 = self.t()
        while True:
            try:
                if cond():
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if self.t() - t0 > secs:
                return False
            time.sleep(poll)

    def tds(self, ids=None):
        return {i: self.st[i].td() for i in (ids or self.st) if self.st[i].alive()}

    def roles(self, ids=None):
        return {i: (t.get("role"), t.get("lead")) for i, t in self.tds(ids).items()}

    def one_network(self, ids=None) -> bool:
        r = self.tds(ids)
        if not r or (ids and len(r) < len(ids)):
            return False
        leads = {t.get("lead") for t in r.values()}
        return (all(t.get("role") in ("lead", "member") for t in r.values()) and len(leads) == 1
                and sum(t.get("role") == "lead" for t in r.values()) == 1)

    def steady(self):
        self.check_from = self.t()

    def overlaps(self, raw: bool = False):
        """Two stations on the air at once (that a third hears) since steady().  Not counted: within SLIP_S
        after a stream slip of either (an underrun or a lost RX buffer moves a station's timing until it has
        heard itself again; on this 2-core PC they come from CPU load)."""
        ov = [o for o in self.air.overlaps if self.check_from is not None and o[0] >= self.check_from]
        if raw:
            return ov
        sl = list(self.air.slips)
        return [o for o in ov if not any(x[1] in (o[1], o[2]) and 0 <= o[0] - x[0] <= SLIP_S for x in sl)]

    def faults(self):
        """Underruns / overflows the fake radios saw (slot -> counters)."""
        return {i: dict(u=s["underruns"], o=s["overflows"]) for i, s in self.air.status()["slots"].items()}

    def slips(self) -> dict:
        """Samples each station's streams slipped so far (TX zeros played, injected TX gaps, RX samples lost,
        injected RX drops): what its TX -> RX offset D must have moved by."""
        st = self.air.status()["slots"]
        return {i: v["underrun_samples"] + v["injected_gap"] - v["overflow_samples"] - v["injected_drop"]
                for i, v in st.items()}

    def late(self, ids=None) -> int:
        return sum(t.get("late", 0) or 0 for t in self.tds(ids).values())

    def cpu(self) -> dict:
        """CPU seconds used so far by each station's process, and the air's, with the air time."""
        out = {}
        for i, s in self.st.items():
            try:
                st = open(f"/proc/{s.proc.pid}/stat").read()
                f = st[st.rindex(")") + 2:].split()
                out[i] = (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
            except (OSError, AttributeError):
                pass
        out["air"] = self.air.cpu_s
        out["t"] = self.t()
        return out

    @staticmethod
    def cores_per_air_s(c0: dict, c1: dict) -> dict:
        dt = max(1e-6, c1["t"] - c0["t"])
        return {k: round((c1[k] - c0[k]) / dt, 2) for k in c1 if k != "t" and k in c0}

    def close(self):
        for s in self.st.values():
            s.stop()
        self.air.close()
        if os.environ.get("KEEP_LOGS"):
            say(f"    (logs in {self.tmp})")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)


def no_overlap(lab, what="no two stations ever on the air at once"):
    ov, raw = lab.overlaps(), lab.overlaps(raw=True)
    after = len(raw) - len(ov)
    note = f" ({after} more within {SLIP_S} s after a stream slip)" if after else ""
    return check(what, not ov, (f"{len(ov)} times, first {ov[:3]}" if ov else "none") + note)


def send_file(src: Station, dst, size: int, seed: int):
    data = random.Random(seed).randbytes(size)
    to = dst if isinstance(dst, str) else f"u:{dst}"
    rec = src.post(f"/api/upload?to={to}&kind=file&name=f{seed}.bin", data, raw=True)
    return rec["uid"], data


def send_text(src: Station, dst, text: str):
    to = dst if isinstance(dst, str) else f"u:{dst}"
    return src.post("/api/send", {"to": to, "text": text})["uid"]


def status_of(src: Station, uid: str):
    return src.messages().get(uid) or {}


def wait_done(lab, src: Station, uids, secs) -> dict:
    """Wait until every message is delivered (or failed); returns uid -> record."""
    out = {}

    def all_done():
        m = src.messages()
        for u in uids:
            out[u] = m.get(u, {})
        return all(out[u].get("status") in ("delivered", "read", "failed", "cancelled") for u in uids)
    lab.wait(all_done, secs, poll=1.0)
    return out


def received_ok(dst: Station, uid: str, data: bytes) -> bool:
    m = dst.messages().get(uid)
    if not m or not m.get("file"):
        return False
    try:
        return hashlib.sha256(dst.get_raw(f"/media/{m['file']}")).digest() == hashlib.sha256(data).digest()
    except OSError:
        return False


def kbps(rec) -> float:
    return float(rec.get("goodput_kbps") or 0.0)


def snapshot(lab, ids=None) -> dict:
    """TD-CDMA and radio facts per station for the report."""
    out = {}
    for i in ids or lab.st:
        r = lab.st[i].radio()
        if not r:
            continue
        t, ra = r.get("tdma") or {}, r.get("radio") or {}
        out[i] = dict(role=t.get("role"), lead=t.get("lead"), D=t.get("D"), d_jumps=t.get("d_jumps"),
                      drift_ppm=t.get("drift_ppm"), late=t.get("late"), recal=t.get("recal"),
                      plan=(t.get("plan") or {}).get("slots"), cycle_ms=(t.get("plan") or {}).get("cycle_ms"),
                      stats=t.get("stats"), lead_ms=ra.get("tx_lead_ms"), margin_ms=ra.get("tx_margin_ms"),
                      underruns=ra.get("tx_underruns"), rx_drop_chunks=ra.get("rx_overflow_chunks"),
                      fake=(ra.get("fake_sdr") or [{}])[0], searched=ra.get("searched"))
    return out


# ============================================================================ 1
def g1_formation():
    say("1. network formation")
    lab = Lab((1, 2, 3), seed=11, start=False)
    try:
        t0 = lab.t()
        lab.start(3)
        lab.run(3.0)
        lab.start(1)
        lab.run(2.0)
        lab.start(2)
        ok = lab.wait(lambda: lab.one_network(), 40)
        check("3, then 1, then 2: one network", ok, f"{lab.roles()} after {lab.t() - t0:.1f} s of air")
        check("the station first on the air leads (3)", lab.roles().get(3, (None,))[0] == "lead", f"{lab.roles()}")
        snap = snapshot(lab)
        metric("g1_D", {i: s["D"] for i, s in snap.items()})
        metric("g1_plan", snap.get(3, {}).get("plan"))
        uid = send_text(lab.st[1], 2, "hello over GNU Radio")
        rec = wait_done(lab, lab.st[1], [uid], 30)[uid]
        check("a message goes through", rec.get("status") in ("delivered", "read"), rec.get("status", "?"))
    finally:
        lab.close()
    lab = Lab((1, 2, 3), seed=12, start=False)
    try:
        t0 = lab.t()
        lab.start(1, 2, 3, wait=False)                # (their flowgraph compiles queue up: on air seconds apart)
        for s in lab.st.values():
            s.wait_up()
        ok = lab.wait(lambda: lab.one_network(), 40)
        leads = {i: (t.get("stats") or {}).get("became_lead", 0) for i, t in lab.tds().items()}
        check("3 processes started together: one network, a single lead from the start", ok and sum(leads.values()) == 1,
              f"{lab.roles()} after {lab.t() - t0:.1f} s of air, became lead {leads}")
    finally:
        lab.close()
    lab = Lab((1, 2, 3), seed=14, start=False)
    try:
        lab.air.pause(True)                           # radios switched on at the same moment of air time
        lab.start(1, 2, 3)
        t0 = lab.t()
        lab.air.pause(False)
        ok = lab.wait(lambda: lab.one_network(), 40)
        check("3 radios switched on at the same moment: one network", ok, f"{lab.roles()} after {lab.t() - t0:.1f} s of air")
        check("... led by station 1 (lowest address listens shortest)", lab.roles().get(1, (None,))[0] == "lead",
              f"{lab.roles()}")
        metric("g1_simultaneous_s", round(lab.t() - t0, 1))
    finally:
        lab.close()
    lab = Lab((2, 3), seed=13, start=False)
    try:
        lab.air.set_link(2, 3, cut=True)
        lab.start(2, 3)
        lab.run(6.0)
        two = lab.roles()
        lone = two.get(2, (None,))[0] == "lead" and two.get(3, (None,))[0] == "lead"
        t0 = lab.t()
        lab.air.set_link(2, 3, cut=False)
        ok = lab.wait(lambda: lab.one_network(), 40)
        check("two lone stations meet: one network led by the lower address",
              lone and ok and lab.roles().get(2, (None,))[0] == "lead",
              f"before {two}, after {lab.roles()} ({lab.t() - t0:.1f} s)")
    finally:
        lab.close()


# ============================================================================ 2
def g2_transfers():
    say("2. transfers on three links at once, a group message")
    lab = Lab((1, 2, 3), seed=21, stagger=1.0)
    try:
        ok = lab.wait(lambda: lab.one_network(), 40)
        check("one network", ok, f"{lab.roles()}")
        lab.run(2.0)
        c0 = lab.cpu()
        lab.run(6.0)
        metric("g2_cpu_idle", Lab.cores_per_air_s(c0, lab.cpu()))
        lab.steady()
        f0 = lab.faults()
        c0 = lab.cpu()
        size = 30_000 if QUICK else 60_000
        sends = [(1, 2), (2, 3), (3, 1)]
        jobs = []
        for k, (a, b) in enumerate(sends):
            uid, data = send_file(lab.st[a], b, size, 200 + k)
            jobs.append((a, b, uid, data))
        recs = {}
        for a, b, uid, data in jobs:
            recs[uid] = wait_done(lab, lab.st[a], [uid], 240)[uid]
        metric("g2_cpu_busy", Lab.cores_per_air_s(c0, lab.cpu()))
        okall = all(recs[u].get("status") in ("delivered", "read") for _, _, u, _ in jobs)
        rates = [kbps(recs[u]) for _, _, u, _ in jobs]
        check(f"three {size // 1000} KB files at once, all delivered", okall,
              f"{[recs[u].get('status') for _, _, u, _ in jobs]}, {rates} kb/s")
        intact = all(received_ok(lab.st[b], u, d) for _, b, u, d in jobs)
        check("... intact", intact)
        metric("g2_kbps", rates)
        gid = send_text(lab.st[2], "g:all", "group hello over the slots")
        g = wait_done(lab, lab.st[2], [gid], 30)[gid]
        check("a group message reaches both others", g.get("status") in ("delivered", "read"),
              f"{g.get('status')} {g.get('recip')}")
        check("no burst missed its slot", lab.late() == 0, f"late {lab.late()}")
        no_overlap(lab)
        f1 = lab.faults()
        snap = snapshot(lab)
        metric("g2_snapshot", snap)
        metric("g2_faults", dict(before=f0, after=f1))
        say(f"    (the fake radios' underruns/overflows: before {f0}, after {f1})")
    finally:
        lab.close()


# ============================================================================ 3
def g3_lead_dies():
    say("3. the lead's PC dies mid-transfer, then comes back")
    lab = Lab((1, 2, 3), seed=31, stagger=1.0)
    try:
        lab.wait(lambda: lab.one_network(), 40)
        lab.run(2.0)
        lab.steady()
        uid, data = send_file(lab.st[2], 3, 40_000 if QUICK else 80_000, 31)
        lab.run(3.0)
        t_off = lab.t()
        lab.st[1].stop(kill=True)                    # kill -9: no goodbye
        lab.air.overlaps.clear()
        lab.check_from = lab.t() + 0.2
        ok = lab.wait(lambda: lab.st[2].td().get("role") == "lead", 10, poll=0.2)
        check("its successor (station 2) takes over", ok, f"after {lab.t() - t_off:.2f} s of air, {lab.roles((2, 3))}")
        metric("g3_takeover_s", round(lab.t() - t_off, 2))
        rec = wait_done(lab, lab.st[2], [uid], 180)[uid]
        check("the transfer goes on", rec.get("status") in ("delivered", "read"), f"{rec.get('status')}, {kbps(rec)} kb/s")
        check("... intact", received_ok(lab.st[3], uid, data))
        t3 = lab.st[3].td()
        check("only the successor took the lead (station 3 followed it)",
              (t3.get("stats") or {}).get("took_over", 0) == 0 and t3.get("role") == "member", f"{lab.roles((2, 3))}")
        lab.start(1)                                  # the lead's app is started again
        ok = lab.wait(lambda: lab.one_network(), 40)
        check("station 1 comes back and the network is one again", ok, f"{lab.roles()}")
        u2 = send_text(lab.st[1], 3, "back again")
        r2 = wait_done(lab, lab.st[1], [u2], 30)[u2]
        check("and it can send", r2.get("status") in ("delivered", "read"), r2.get("status", "?"))
        no_overlap(lab)
        check("no burst missed its slot", lab.late() == 0, f"late {lab.late()}")
    finally:
        lab.close()


# ============================================================================ 4
def g4_duplicate():
    say("4. two radios set to the same address")
    lab = Lab((1, 2, 2), seed=41, start=False, slots=None)
    try:
        lab.st = {1: Station(lab, 1), 2: Station(lab, 2), 5: Station(lab, 2, slot=5)}
        lab.start(1, stagger=0)
        lab.run(2.0)
        lab.start(2, 5)
        ok = lab.wait(lambda: any(lab.st[s].td().get("dup") for s in (1, 2, 5)), 30)
        warn = [lab.st[s].td().get("dup") for s in (1, 2, 5)]
        check("the stations are told that two radios share address 2", ok, f"{warn}")
    finally:
        lab.close()


# ============================================================================ 5
def g5_drift():
    minutes = 1.5 if QUICK else 4
    say(f"5. sample-clock error +25 / -25 / +10 ppm for {minutes} minutes of air")
    lab = Lab((1, 2, 3), seed=51, ppm={1: 25.0, 2: -25.0, 3: 10.0}, stagger=1.0)
    try:
        ok = lab.wait(lambda: lab.one_network(), 40)
        lab.run(3.0)
        lab.steady()
        d0 = {i: t.get("D") for i, t in lab.tds().items()}
        s0 = lab.slips()
        sent = []
        t_end = lab.t() + 60 * minutes
        k = 0
        while lab.t() < t_end:
            a, b = random.Random(k).sample([1, 2, 3], 2)
            sent.append((a, send_text(lab.st[a], b, f"drift {k} " + "x" * 400)))
            k += 1
            lab.run(3.0)
        good = 0
        for a, u in sent:
            r = wait_done(lab, lab.st[a], [u], 40)[u]
            good += r.get("status") in ("delivered", "read")
        check("every message delivered", good == len(sent), f"{good}/{len(sent)}")
        tds = lab.tds()
        d1 = {i: t.get("D") for i, t in tds.items()}
        s1 = lab.slips()
        # D moves only by what the radios' streams slipped (PC too slow: lost RX buffers, underruns), not
        # with the clock error: TX and RX of a radio share its clock
        moved = {i: d1[i] - d0[i] for i in d0 if d0[i] is not None}
        slipped = {i: s1[i] - s0[i] for i in moved}
        check("the TX->RX offsets moved only by the streams' slips, not with the drift",
              all(abs(moved[i] - slipped[i]) <= 4 for i in moved), f"D moved {moved}, streams slipped {slipped} samples")
        metric("g5_D", dict(moved=moved, slipped=slipped))
        drift = {i: tds[i].get("drift_ppm") for i in (2, 3)}
        check("each follower knows its clock against the lead's", abs(drift[2] + 50) < 5 and abs(drift[3] + 15) < 5,
              f"{drift} ppm (expected -50, -15)")
        metric("g5_drift", drift)
        check("still one network, no burst late", lab.one_network() and lab.late() == 0,
              f"{lab.roles()}, late {lab.late()}")
        no_overlap(lab)
    finally:
        lab.close()


# ============================================================================ 6
def g6_slips():
    say("6. TX underruns and lost RX buffers during a transfer")
    lab = Lab((1, 2, 3), seed=61, stagger=1.0)
    try:
        lab.wait(lambda: lab.one_network(), 40)
        lab.run(2.0)
        u1, d1 = send_file(lab.st[2], 3, 60_000, 61)
        u2, d2 = send_file(lab.st[3], 1, 30_000, 62)
        faults = [(1.0, "tx", 2, 6000), (3.0, "rx", 3, 4000), (5.0, "tx", 1, 65536), (7.0, "rx", 2, 20000),
                  (9.0, "tx", 3, 1500)]
        j0 = {i: t.get("d_jumps", 0) for i, t in lab.tds().items()}
        dd0 = {i: t.get("D") for i, t in lab.tds().items()}
        s0 = lab.slips()
        t0 = lab.t()
        for at, kind, who, n in faults:
            while lab.t() - t0 < at:
                time.sleep(0.05)
            if kind == "tx":
                lab.air.inject_tx_gap(who, n)
            else:
                lab.air.inject_rx_drop(who, n)
        r1 = wait_done(lab, lab.st[2], [u1], 240)[u1]
        r2 = wait_done(lab, lab.st[3], [u2], 120)[u2]
        check("both transfers complete through 5 timing slips",
              r1.get("status") in ("delivered", "read") and r2.get("status") in ("delivered", "read"),
              f"{r1.get('status')} {kbps(r1)} kb/s, {r2.get('status')} {kbps(r2)} kb/s")
        check("... intact", received_ok(lab.st[3], u1, d1) and received_ok(lab.st[1], u2, d2))
        jumps = {i: t.get("d_jumps", 0) - j0.get(i, 0) for i, t in lab.tds().items()}
        check("each slip that moved a TX->RX offset was measured again", jumps[1] >= 1 and jumps[2] >= 2 and jumps[3] >= 2,
              f"D jumps {jumps} (expected >= 1, 2, 2)")
        lab.run(2.0)
        moved = {i: t.get("D") - dd0[i] for i, t in lab.tds().items() if dd0.get(i) is not None}
        s1 = lab.slips()
        slipped = {i: s1[i] - s0[i] for i in moved}
        check("... and each offset moved by exactly what its streams slipped", all(abs(moved[i] - slipped[i]) <= 4 for i in moved),
              f"D moved {moved}, streams slipped {slipped} samples")
        metric("g6_D", dict(moved=moved, slipped=slipped, jumps=jumps))
        ok = lab.wait(lambda: lab.one_network(), 20)
        check("still one network", ok, f"{lab.roles()}")
    finally:
        lab.close()


# ============================================================================ 7
def g7_hidden():
    say("7. a hidden station: 1 and 3 can't hear each other, 2 relays")
    lab = Lab((1, 2, 3), seed=71, start=False)
    try:
        lab.air.set_link(1, 3, cut=True)
        lab.start(1, 2, 3, stagger=1.0)
        ok = lab.wait(lambda: lab.one_network(), 60)
        check("one network although 1 and 3 can't hear each other", ok, f"{lab.roles()}")
        ok = lab.wait(lambda: "3" in (lab.st[1].td().get("reach_via") or {}), 30)
        check("station 1 knows 3 is reachable through 2", ok, f"{lab.st[1].td().get('reach_via')}")
        lab.steady()
        uid, data = send_file(lab.st[1], 3, 20_000 if QUICK else 40_000, 71)
        rec = wait_done(lab, lab.st[1], [uid], 240)[uid]
        check("a file from 1 to 3 arrives through the relay", rec.get("status") in ("delivered", "read"),
              f"{rec.get('status')}, {kbps(rec)} kb/s, via {rec.get('via')}")
        check("... intact", received_ok(lab.st[3], uid, data))
        metric("g7_kbps", kbps(rec))
        u2 = send_text(lab.st[3], 1, "hello back through Bob")
        r2 = wait_done(lab, lab.st[3], [u2], 40)[u2]
        check("and a message back", r2.get("status") in ("delivered", "read"), r2.get("status", "?"))
        no_overlap(lab, "no two stations on the air at once (where a third hears both)")
    finally:
        lab.close()


# ============================================================================ 8
def g8_off_air():
    say("8. a station off the air for 1 s and for 3.5 s")
    lab = Lab((1, 2, 3), seed=81, stagger=1.0)
    try:
        lab.wait(lambda: lab.one_network(), 40)
        lab.run(3.0)
        plan0 = [s["owner"] for s in (lab.st[1].td().get("plan") or {}).get("slots", [])]
        lab.air.set_off_air(3, True)
        lab.run(1.0)
        lab.air.set_off_air(3, False)
        lab.run(1.5)
        plan1 = [s["owner"] for s in (lab.st[1].td().get("plan") or {}).get("slots", [])]
        check("1 s off the air: station 3 keeps its slot", 3 in plan1 and lab.one_network(), f"plan {plan0} -> {plan1}")
        lab.air.set_off_air(3, True)
        t_off, plan2 = lab.t(), None
        while lab.t() - t_off < 3.5:                  # (evicted after 1.5 s unheard, the new plan 1 s later)
            p = [s["owner"] for s in (lab.st[1].td().get("plan") or {}).get("slots", [])]
            if p and 3 not in p and plan2 is None:
                plan2, t_drop = p, lab.t() - t_off
            time.sleep(0.1)
        t_on = lab.t()
        lab.air.set_off_air(3, False)
        ok = lab.wait(lambda: lab.one_network() and 3 in [s["owner"] for s in (lab.st[1].td().get("plan") or {}).get("slots", [])], 20,
                      poll=0.2)
        check("3.5 s off: it was dropped from the plan and rejoins", plan2 is not None and ok,
              (f"dropped after {t_drop:.2f} s of air ({plan2}), " if plan2 else "never dropped, ")
              + f"back in the plan {lab.t() - t_on:.2f} s after it came back")
        metric("g8_rejoin_s", round(lab.t() - t_on, 2))
        uid = send_text(lab.st[3], 1, "I'm back")
        r = wait_done(lab, lab.st[3], [uid], 30)[uid]
        check("and it can send", r.get("status") in ("delivered", "read"), r.get("status", "?"))
    finally:
        lab.close()


# ============================================================================ 9
def g9_low_snr():
    say("9. low SNR and bursty interference")
    lab = Lab((1, 2, 3), seed=91, esn0=10.0, stagger=1.0)
    try:
        ok = lab.wait(lambda: lab.one_network(), 60)
        check("Es/N0 10 dB: one network", ok, f"{lab.roles()}")
        lab.run(2.0)
        took = sum((t.get("stats") or {}).get("took_over", 0) for t in lab.tds().values())
        uid, data = send_file(lab.st[1], 2, 15_000 if QUICK else 30_000, 91)
        rec = wait_done(lab, lab.st[1], [uid], 240)[uid]
        check("a file at Es/N0 10 dB", rec.get("status") in ("delivered", "read") and received_ok(lab.st[2], uid, data),
              f"{rec.get('status')}, {kbps(rec)} kb/s")
        took2 = sum((t.get("stats") or {}).get("took_over", 0) for t in lab.tds().values())
        check("no false takeovers", took2 == took == 0, f"took_over {took2}")
        metric("g9_low_kbps", kbps(rec))
        lab.air.set_esn0(28.0)
        lab.air.set_interference(-3.0, duty=0.2, burst_ms=20)
        uid, data = send_file(lab.st[2], 3, 30_000 if QUICK else 60_000, 92)
        rec = wait_done(lab, lab.st[2], [uid], 240)[uid]
        check("interference bursts (20 % of the time, 3 dB below a link): file delivered",
              rec.get("status") in ("delivered", "read") and received_ok(lab.st[3], uid, data),
              f"{rec.get('status')}, {kbps(rec)} kb/s")
        metric("g9_intf_kbps", kbps(rec))
        check("still one network", lab.one_network(), f"{lab.roles()}")
    finally:
        lab.close()


# ============================================================================ 10
def g10_realtime():
    say("10. real time: the air at full speed, two stations")
    lab = Lab((1, 2), seed=101, speed=1.0, stagger=1.0)
    try:
        ok = lab.wait(lambda: lab.one_network(), 40)
        check("one network", ok, f"{lab.roles()}")
        lab.run(3.0)
        f0 = lab.faults()
        lab.steady()
        c0 = lab.cpu()
        uid, data = send_file(lab.st[1], 2, 60_000 if QUICK else 120_000, 101)
        rec = wait_done(lab, lab.st[1], [uid], 240)[uid]
        metric("g10_cpu_busy", Lab.cores_per_air_s(c0, lab.cpu()))
        check("a file at full speed", rec.get("status") in ("delivered", "read") and received_ok(lab.st[2], uid, data),
              f"{rec.get('status')}, {kbps(rec)} kb/s")
        metric("g10_kbps", kbps(rec))
        f1 = lab.faults()
        snap = snapshot(lab)
        metric("g10_snapshot", snap)
        check("no underrun, no lost RX buffer", f1 == f0, f"before {f0}, after {f1}")
        check("no burst missed its slot", lab.late() == 0, f"late {lab.late()}")
        lead = {i: (s["lead_ms"], s["margin_ms"]) for i, s in snap.items()}
        check("bursts reach the combiner in time (lead time, smallest margin ms)", all(m is not None and m > 0 for _, m in lead.values()),
              f"{lead}")
        no_overlap(lab)
    finally:
        lab.close()


# ============================================================================ 11
def g11_aloha():
    say("11. spread ALOHA on the same flowgraph and fake radios")
    lab = Lab((1, 2, 3), seed=111, extra={i: ("--mac", "aloha") for i in (1, 2, 3)}, stagger=0.5)
    try:
        lab.run(4.0)
        modes = {i: (lab.st[i].radio() or {}).get("mac_mode") for i in lab.st}
        check("all three run spread ALOHA", all(m == "aloha" for m in modes.values()), f"{modes}")
        uid, data = send_file(lab.st[1], 3, 20_000 if QUICK else 40_000, 111)
        u2 = send_text(lab.st[2], 1, "aloha!")
        rec = wait_done(lab, lab.st[1], [uid], 240)[uid]
        r2 = wait_done(lab, lab.st[2], [u2], 60)[u2]
        check("a file and a message", rec.get("status") in ("delivered", "read") and received_ok(lab.st[3], uid, data)
              and r2.get("status") in ("delivered", "read"), f"{rec.get('status')} {kbps(rec)} kb/s, {r2.get('status')}")
        metric("g11_kbps", kbps(rec))
    finally:
        lab.close()


# ============================================================================ 12
def g12_games():
    say("12. game center over the slots: race and UNO")
    lab = Lab((1, 2, 3), seed=121, stagger=1.0)
    try:
        lab.wait(lambda: lab.one_network(), 40)
        lab.run(3.0)
        import math
        import threading
        sts = {1: lab.st[1], 2: lab.st[2], 3: lab.st[3]}

        def room(s, rid):
            return next((r for r in s.get("/api/gc")["rooms"] if r["r"] == rid), None)
        # ---- race
        rid = sts[1].post("/api/gc", {"a": "create", "game": "race", "players": [2, 3], "o": {"laps": 5}})["r"]
        ok = lab.wait(lambda: all(room(s, rid) for s in sts.values()), 30)
        for a in (2, 3):
            sts[a].post("/api/gc", {"a": "answer", "r": rid, "ok": True})
        ok = ok and lab.wait(lambda: all((room(sts[1], rid)["sync"] or {}).get(str(a)) or (room(sts[1], rid)["sync"] or {}).get(a)
                                         for a in (2, 3)), 60)
        check("race: both invited players joined and clock-synced", ok, f"{(room(sts[1], rid) or {}).get('st')}")
        sts[1].post("/api/gc", {"a": "start", "r": rid})
        lab.wait(lambda: all((room(s, rid) or {}).get("state") == "playing" for s in sts.values()), 30)
        stop = threading.Event()

        def driver(a, s):
            """Like the browser: 20 posts a second (wall clock), each answer carries the host's game time."""
            r0 = room(s, rid) or {}
            seat, t0 = r0.get("me_seat") or 0, r0.get("t0") or 0
            th, tw = s.get(f"/api/gc/clock?r={rid}")["t"], time.monotonic()
            while not stop.is_set():
                try:
                    tg = th + (time.monotonic() - tw) - t0
                    ang = 0.3 * tg + seat
                    st = dict(t=tg * 1000, x=1500 + 900 * math.cos(ang), y=1200 + 700 * math.sin(ang), h=ang + math.pi / 2,
                              vx=-270 * math.sin(ang), vy=210 * math.cos(ang), w=0.3, d=max(0.0, 250 * tg), fl=0, it=0)
                    ans = s.post("/api/gc/race", {"r": rid, "s": st, "ev": []})
                    if ans.get("t") is not None:
                        th, tw = ans["t"], time.monotonic()
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                time.sleep(0.05)
        ths = [threading.Thread(target=driver, args=(a, s), daemon=True) for a, s in sts.items()]
        for th in ths:
            th.start()
        lab.run(10.0 if QUICK else 20.0)
        stop.set()
        cyc = ((sts[1].td().get("plan") or {}).get("cycle_ms") or 100) / 1000
        per_s = lab.air.speed / cyc                     # slots per wall second for each car
        stats = {a: s.get(f"/api/gc/stats?r={rid}") for a, s in sts.items()}
        share = {a: {k: round((v.get("rate") or 0) / per_s, 2) for k, v in (st.get("rx") or {}).items()} for a, st in stats.items()}
        ages = {a: {k: v.get("age_med") for k, v in (st.get("rx") or {}).items()} for a, st in stats.items()}
        lost = {a: sum(v.get("lost", 0) for v in (st.get("rx") or {}).values()) for a, st in stats.items()}
        metric("g12_race", dict(share_of_cycles=share, age_med_wall_s=ages, lost=lost, cycle_ms=cyc * 1000))
        okr = all(len(r) == 2 and all(x > 0.2 for x in r.values()) for r in share.values())
        check("race: every car's state reaches both other players (in 20 % of the cycles or more)", okr,
              f"share of cycles {share}, median age {ages} s of wall time (air at {lab.air.speed}x), lost {lost}")
        # ---- UNO
        uid = sts[2].post("/api/gc", {"a": "create", "game": "uno", "players": [1, 3]})["r"]
        ok = lab.wait(lambda: all(room(s, uid) for s in sts.values()), 30)
        for a in (1, 3):
            sts[a].post("/api/gc", {"a": "answer", "r": uid, "ok": True})
        lab.wait(lambda: all((room(sts[2], uid)["st"] or {}).get(str(a)) == "joined" for a in (1, 3)), 30)
        sts[2].post("/api/gc", {"a": "start", "r": uid})
        ok = lab.wait(lambda: all(((room(s, uid) or {}).get("uno") or {}).get("hand") is not None for s in sts.values()), 40)
        check("UNO: the host dealt and every player sees a hand", ok)
        from wavelink.games.uno import CARDS, WILD, WILD4
        rng = random.Random(5)
        moves, delays = 0, []
        t_end = lab.t() + (20 if QUICK else 40)
        while ok and lab.t() < t_end and moves < 15:
            for a, s in sts.items():
                v = (room(s, uid) or {}).get("uno") or {}
                if not v or v.get("phase") == "over" or v.get("turn") != v.get("seat") or v.get("pause"):
                    continue
                n0 = v.get("v") or 0                    # (the table's version: +1 per move)
                if v["phase"] == "color":
                    act = {"m": "color", "col": rng.randrange(4)}
                elif v["phase"] == "drawn":
                    act = {"m": "keep"}
                elif v.get("legal") and len(v.get("hand") or ()) > 2:
                    c = v["legal"][0]
                    act = {"m": "play", "c": c}
                    if CARDS[c][1] in (WILD, WILD4):
                        act["col"] = rng.randrange(4)
                else:
                    act = {"m": "draw"}
                t0 = lab.t()
                try:
                    s.post("/api/gc", dict(a="uno", r=uid, **act))
                except (OSError, ValueError):
                    continue
                others = [x for x in sts if x != a]
                if lab.wait(lambda: all((((room(sts[x], uid) or {}).get("uno") or {}).get("v") or 0) > n0 for x in others), 15,
                            poll=0.1):
                    delays.append(lab.t() - t0)
                moves += 1
            time.sleep(0.1)
        delays.sort()
        med = delays[len(delays) // 2] if delays else None
        metric("g12_uno", dict(moves=moves, median_air_s=med))
        check("UNO: every move reaches both other players", moves >= 5 and len(delays) >= moves - 1,
              f"{moves} moves, {len(delays)} seen by both, median {med:.2f} s of air time" if med else f"{moves} moves")
    finally:
        lab.close()


TESTS = {1: g1_formation, 2: g2_transfers, 3: g3_lead_dies, 4: g4_duplicate, 5: g5_drift, 6: g6_slips,
         7: g7_hidden, 8: g8_off_air, 9: g9_low_snr, 10: g10_realtime, 11: g11_aloha, 12: g12_games}


def main():
    nums = [int(a) for a in sys.argv[1:] if a.isdigit() and int(a) in TESTS] or list(TESTS)
    say(f"GNU Radio with fake ANTSDRs: tests {nums}, air speed {SPEED} (stations: {sys.executable})")
    t0 = time.time()
    for k in nums:
        t1 = time.time()
        try:
            TESTS[k]()
        except Exception as e:  # noqa: BLE001 - one broken test must not hide the others
            import traceback
            traceback.print_exc()
            check(f"test {k} ran", False, repr(e))
        say(f"    ({time.time() - t1:.0f} s)")
    n_ok = sum(ok for _, ok in RESULTS)
    say(f"\n{n_ok}/{len(RESULTS)} checks passed ({time.time() - t0:.0f} s)")
    out = os.environ.get("METRICS_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(dict(results=RESULTS, metrics=METRICS, speed=SPEED), f, indent=1, default=str)
    sys.exit(0 if n_ok == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
