"""Game center over the simulated air (numpy PHY), without a browser.

    python3 -m tests.test_games 3        # 3 stations: a full UNO round, then a race
    python3 -m tests.test_games 4 --ebn0 25 --uno-only

UNO: every station plays through its own GameHub (the host's hub applies every move);
the 'bot' of each station answers the views it receives.  Measured: from a move being
made until every station has the resulting view.

Race: a stand-in for each station's browser posts a car state 20 times a second plus a
few events (missile, mine, EMP, finish).  Measured: updates heard per car per second, the
age of a state when it is heard, events delivered (and duplicates passed to the browser).
"""
import argparse
import collections
import math
import os
import random
import shutil
import sys
import tempfile
import threading
import time

from cdmachat.config import PhyConfig
from cdmachat.games.uno import CARDS, WILD, WILD4
from cdmachat.node import Node
from cdmachat.radio_sim import SimAir

NAMES = ["Alice", "Bob", "Carol", "Dave"]


def make_stations(n, ebn0, key=None, seed=3):
    cfg = PhyConfig.from_profile("fast")
    air = SimAir(cfg, ebn0_db=ebn0, seed=seed)
    tmp = tempfile.mkdtemp(prefix="games-")
    nodes = []
    for i in range(1, n + 1):
        r = air.add_node(i)
        nodes.append(Node(r, i, NAMES[i - 1], os.path.join(tmp, f"n{i}"), key, mode="sim",
                          log=lambda *a: None))
    for nd in nodes:
        nd.start()
    t_end = time.time() + 30
    while time.time() < t_end:
        if all(sum(c.get("online", False) for c in nd.contacts.values()) == n - 1 for nd in nodes):
            break
        time.sleep(0.1)
    else:
        raise SystemExit("stations did not hear each other's beacons")
    return air, nodes, tmp


def wait(cond, timeout, step=0.02):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if cond():
            return True
        time.sleep(step)
    return False


def room_lobby(nodes, game, opts=None):
    host = nodes[0]
    rid = host.hub.create(game, [nd.addr for nd in nodes[1:]], opts or {})
    assert wait(lambda: all(rid in nd.hub.rooms for nd in nodes), 10), "invitations not delivered"
    for nd in nodes[1:]:
        nd.hub.answer(rid, True)
    assert wait(lambda: all(host.hub.rooms[rid]["st"][nd.addr] == "joined" for nd in nodes[1:]), 10)
    return rid


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))] if xs else float("nan")


# ============================================================================ UNO
def play_uno(nodes, rounds=1, think=0.05, timeout=300, transfer_kb=0):
    rid = room_lobby(nodes, "uno")
    host = nodes[0]
    if transfer_kb:
        # a file transfer from the host to the second station runs during the whole game
        host.send(f"u:{nodes[1].addr}", "file", os.urandom(transfer_kb * 1024), name="big.bin",
                  mime="application/octet-stream")
    shown = collections.defaultdict(dict)        # version -> {addr: time first shown}
    lock = threading.Lock()
    moves = {}                                    # version produced -> (time, addr)
    acted = {}                                    # addr -> version it last acted on
    errors = []

    def listener_for(nd):
        def fn(ev, data):
            if ev == "uno" and data.get("r") == rid:
                with lock:
                    shown[data["v"]].setdefault(nd.addr, time.time())
                    latest[nd.addr] = data
            elif ev == "unoerr":
                errors.append((nd.addr, data))
        return fn

    latest = {}
    for nd in nodes:
        nd.hub.listeners.append(listener_for(nd))
    host.hub.start_game(rid)
    rng = random.Random(1)
    t_end = time.time() + timeout
    done_rounds = 0
    while time.time() < t_end and done_rounds < rounds:
        time.sleep(think)
        for nd in nodes:
            with lock:
                v = latest.get(nd.addr)
            if not v or acted.get(nd.addr) == v["v"]:
                continue
            if v["phase"] == "over":
                if nd is host and all(latest.get(x.addr, {}).get("phase") == "over" for x in nodes):
                    done_rounds += 1
                    if done_rounds < rounds:
                        acted[nd.addr] = v["v"]
                        host.hub.uno_next(rid)
                continue
            if v["turn"] != v["seat"]:
                continue
            acted[nd.addr] = v["v"]
            t = time.time()
            with lock:
                moves[v["v"] + 1] = (t, nd.addr)
            if v["phase"] == "color":
                nd.hub.uno_action(rid, "color", col=rng.randrange(4))
            elif v["phase"] == "drawn":
                nd.hub.uno_action(rid, "play", c=v["drawn"], col=rng.randrange(4),
                                  uno=len(v["hand"]) == 2)
            elif v["legal"]:
                c = v["legal"][0]
                col = rng.randrange(4) if CARDS[c][1] in (WILD, WILD4) else None
                nd.hub.uno_action(rid, "play", c=c, col=col, uno=len(v["hand"]) == 2)
            else:
                nd.hub.uno_action(rid, "draw")
    final = latest.get(host.addr)
    ok = final is not None and final["phase"] == "over"
    lat, lat_remote = [], []
    for ver, (t0, actor) in moves.items():
        seen = shown.get(ver, {})
        if len(seen) == len(nodes):
            lat.append(max(seen.values()) - t0)
            if actor != host.addr:
                lat_remote.append(max(seen.values()) - t0)
    for nd in nodes:
        nd.hub.listeners.clear()
    turns = len(moves)
    return dict(ok=ok, rounds=done_rounds, turns=turns, winner=final and final.get("winner"),
                scores=final and final.get("scores"), lat=lat, lat_remote=lat_remote,
                errors=errors, stats=dict(host.hub.stats), rid=rid)


def uno_leaving(nodes, timeout=120):
    """A player leaves in the middle of a round: the others play on; then the host closes."""
    rid = room_lobby(nodes, "uno")
    host, quitter = nodes[0], nodes[-1]
    latest = {}
    for nd in nodes:
        nd.hub.listeners.append(lambda ev, d, a=nd.addr: ev == "uno" and d.get("r") == rid and latest.__setitem__(a, d))
    host.hub.start_game(rid)
    assert wait(lambda: all(nd.addr in latest for nd in nodes), 15), "no views"
    quitter.hub.leave(rid)
    q_seat = host.hub.rooms[rid]["players"].index(quitter.addr)
    assert wait(lambda: all(q_seat in latest[nd.addr].get("out", []) for nd in nodes[:-1]), 15), \
        "the others were not told that a player left"
    rng = random.Random(5)
    t_end = time.time() + timeout
    acted = {}
    while time.time() < t_end and latest[host.addr]["phase"] != "over":
        time.sleep(0.05)
        for nd in nodes[:-1]:
            v = latest[nd.addr]
            if v["turn"] != v["seat"] or acted.get(nd.addr) == v["v"] or v["phase"] == "over":
                continue
            acted[nd.addr] = v["v"]
            if v["phase"] == "color":
                nd.hub.uno_action(rid, "color", col=1)
            elif v["phase"] == "drawn":
                nd.hub.uno_action(rid, "keep")
            elif v["legal"]:
                c = v["legal"][0]
                nd.hub.uno_action(rid, "play", c=c, col=rng.randrange(4), uno=len(v["hand"]) == 2)
            else:
                nd.hub.uno_action(rid, "draw")
    finished = latest[host.addr]["phase"] == "over"
    winner_ok = latest[host.addr].get("winner") != q_seat
    host.hub.leave(rid)
    closed = wait(lambda: all(nd.hub.rooms[rid]["state"] == "closed" for nd in nodes[1:-1]), 15)
    for nd in nodes:
        nd.hub.listeners.clear()
    return finished and winner_ok and closed


# ============================================================================ race
def run_race(nodes, seconds=20.0, finish_at=None):
    rid = room_lobby(nodes, "race", {"laps": 2})
    host = nodes[0]
    assert wait(lambda: all(host.hub.rooms[rid]["sync"].get(nd.addr) for nd in nodes), 30), \
        "clock sync did not complete"
    clocks = {nd.addr: nd.hub.snapshot()["rooms"][-1].get("clock") for nd in nodes[1:]}
    host.hub.start_game(rid)
    assert wait(lambda: all(nd.hub.rooms[rid]["state"] == "playing" for nd in nodes), 10)
    got = collections.defaultdict(list)           # (receiver, sender seat, event id) -> count
    sent = collections.defaultdict(set)           # sender seat -> event ids
    lock = threading.Lock()
    results = {}

    def listener_for(nd):
        def fn(ev, data):
            if ev == "race" and data.get("r") == rid:
                with lock:
                    for e in data["ev"]:
                        got[(nd.addr, data["s"], e["id"])].append(time.time())
            elif ev == "raceover" and data.get("r") == rid:
                results[nd.addr] = data["res"]
        return fn

    for nd in nodes:
        nd.hub.listeners.append(listener_for(nd))
    stop = threading.Event()
    t0 = host.hub.rooms[rid]["t0"]
    finish_at = finish_at or {}

    def driver(nd):
        hub = nd.hub
        seat = hub.rooms[rid]["players"].index(nd.addr)
        eid, kinds = 0, ["missile", "mine", "emp"]
        next_ev = 3.0 + seat * 0.7
        finished = False
        while not stop.is_set():
            tg = hub.host_now(rid) - t0
            a = 0.3 * tg + seat
            st = dict(t=tg * 1000, x=1500 + 900 * math.cos(a), y=1200 + 700 * math.sin(a), h=a + math.pi / 2,
                      vx=-270 * math.sin(a), vy=210 * math.cos(a), w=0.3, d=max(0.0, 250 * tg), fl=0, it=0)
            evs = []
            if tg > next_ev and not finished:
                eid += 1
                evs.append(dict(k=kinds[eid % 3], id=eid, t=tg * 1000, x=st["x"], y=st["y"], a=0, b=255))
                with lock:
                    sent[seat].add(eid)
                next_ev = tg + 2.0
            if not finished and tg > finish_at.get(seat, 1e9):
                eid += 1
                evs.append(dict(k="finish", id=eid, t=tg * 1000, x=st["x"], y=st["y"], a=2, b=0))
                with lock:
                    sent[seat].add(eid)
                finished = True
            hub.race_post(rid, st, evs)
            time.sleep(0.05)

    threads = [threading.Thread(target=driver, args=(nd,), daemon=True) for nd in nodes]
    for th in threads:
        th.start()
    t_start = time.time()
    loads = []
    while time.time() - t_start < seconds + 40:
        if len(results) == len(nodes):
            break
        time.sleep(0.2)
        loads.append(nodes[0].radio.air.cpu_load)
    time.sleep(3.0)                    # the last events' repeats go out after the results
    stop.set()
    for th in threads:
        th.join()
    per_station = {nd.addr: nd.hub.race_stats(rid) for nd in nodes}
    missing, dups = [], 0
    for nd in nodes:
        seat_me = host.hub.rooms[rid]["players"].index(nd.addr)
        for seat, ids in sent.items():
            if seat == seat_me:
                continue
            for i in ids:
                k = len(got.get((nd.addr, seat, i), []))
                if k == 0:
                    missing.append((nd.addr, seat, i))
                dups += max(0, k - 1)
    for nd in nodes:
        nd.hub.listeners.clear()
    return dict(rid=rid, stats=per_station, missing=missing, dups=dups, events=sum(map(len, sent.values())),
                results=results, clocks=clocks, load=sum(loads) / max(1, len(loads)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int, nargs="?", default=3)
    ap.add_argument("--ebn0", type=float, default=25.0)
    ap.add_argument("--key", default=None)
    ap.add_argument("--uno-only", action="store_true")
    ap.add_argument("--race-only", action="store_true")
    ap.add_argument("--race-seconds", type=float, default=20.0)
    ap.add_argument("--transfer", type=int, default=0, help="KB sent host -> station 2 during UNO")
    a = ap.parse_args()
    air, nodes, tmp = make_stations(a.n, a.ebn0, a.key)
    print(f"{a.n} stations on the simulated air, Es/N0 {a.ebn0} dB{' , encrypted' if a.key else ''}")
    fail = False
    try:
        if not a.race_only:
            t = time.time()
            r = play_uno(nodes, transfer_kb=a.transfer)
            if a.transfer:
                print(f"  (a {a.transfer} KB file transfer host -> {NAMES[1]} runs in the background)")
            lat = r["lat"]
            print(f"UNO: round finished={r['ok']} in {time.time() - t:.1f} s, {r['turns']} moves, "
                  f"winner seat {r['winner']}, scores {r['scores']}")
            print(f"  a move until every station shows it: median {pct(lat, .5):.2f} s, "
                  f"90 % {pct(lat, .9):.2f} s, max {max(lat):.2f} s  (moves by remote players: "
                  f"median {pct(r['lat_remote'], .5):.2f} s)")
            print(f"  host: {r['stats'].get('uno_views_sent', 0)} views sent, "
                  f"{r['stats'].get('uno_views_withdrawn', 0)} withdrawn and merged, "
                  f"{r['stats'].get('uno_refused', 0)} moves refused, errors {r['errors'][:3]}")
            fail |= not r["ok"] or bool(r["errors"])
            if a.n >= 3:
                ok = uno_leaving(nodes)
                print(f"  a player leaves mid-round: the others finish the round, then the host closes the room: "
                      f"{'ok' if ok else 'FAILED'}")
                fail |= not ok
        if not a.uno_only:
            seats = list(range(a.n))
            r = run_race(nodes, a.race_seconds, finish_at={s: a.race_seconds - 3 + s for s in seats})
            print(f"Race: clock sync {r['clocks']}")
            for addr, st in r["stats"].items():
                rx = ", ".join(f"car{s}: {v['rate']}/s age {v['age_med']}s (p90 {v['age_p90']}) "
                               f"lost {v['lost']}/{v['seqs']}" for s, v in sorted(st["rx"].items()))
                print(f"  station {addr}: sent {st['tx']} (slot P={st['P'] * 1e3:.0f} ms, "
                      f"air {st['air'] * 1e3:.1f} ms, skipped {st['skipped']}) heard {rx}")
            print(f"  simulator: {r['load']:.1f} s of CPU per second of air on average (one PC computes "
                  f"every receiver; above 1 the air runs slower than real time)")
            print(f"  events: {r['events']} sent, missing {len(r['missing'])} {r['missing'][:5]}, "
                  f"duplicates passed on {r['dups']}")
            res = next(iter(r["results"].values()), None)
            print(f"  results on {len(r['results'])} stations: "
                  f"{[(x['name'], x['ft']) for x in res] if res else None}")
            fail |= bool(r["missing"]) or r["dups"] > 0 or len(r["results"]) != a.n
    finally:
        for nd in nodes:
            nd.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    print("game center:", "FAIL" if fail else "PASS")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
