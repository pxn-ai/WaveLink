"""Game center: rooms, invitations, clock sync, the UNO host and the race's time slots.

One GameHub runs on every station, next to the chat (Node).  It talks to the other
stations in two ways:

* `gc` messages: the chat's reliable messages (selective-repeat ARQ), sent at high
  priority and kept out of the chat history.  Everything that must arrive: invitations
  and answers, the lobby, the start, UNO moves and views, race finish times, results.
* datagrams: one unacknowledged frame, sent at once at the most robust modulation and
  coding.  Clock-sync pings (unicast) and race car states (broadcast code, in time slots).

Rooms.  The station that invites is the room's *host*.  Invitees answer; the host starts
with everyone who has joined, sending each player a `start` with the seed (the race
track is generated from it) and the seat order.  If the host leaves or goes off the air
the room closes; any other player who leaves is announced and the game goes on.

UNO.  The host is the authority: it holds the deck and every hand (games/uno.py).  A
player sends an intent ("play card 37 as blue", "draw"); the host checks it, applies it
and sends every player a view with only their own hand.  When a view is ready while the
previous one to that player is still queued in the MAC, the old one is withdrawn and the
new one carries the events of both.

Race.  Each station drives its own car in the browser and posts its state to this hub 20
times a second.  The hub broadcasts the newest state in its own time slot:
seat k of N transmits at S0 + k*P/N + m*P (host's radio clock), so the cars' bursts never
overlap on the broadcast code and no station is deaf (transmitting) while another car's
update is on the air.

Clocks.  Every other player measures its offset to the host with datagram pings and keeps
the sample with the shortest round trip.  Two clocks are synchronised by the same pings:
  * the game clock (time.monotonic): countdown, state and event timestamps, finish times;
    the browser runs its physics in real time against it;
  * the radio clock (radio.now()): the time slots.  On the ANTSDRs this is the same
    monotonic clock; in the simulator it is the simulated air's sample clock, which falls
    behind real time when the PC can't keep up, so slots in real time would pile up there.
"""
from __future__ import annotations

import collections
import math
import os
import queue
import random
import struct
import threading
import time

from ..config import BROADCAST_ADDR, BROADCAST_CODE
from .uno import Uno, UnoError
from . import race as R

GAMES = {"uno": "UNO", "race": "Gold Code GP"}
MAX_PLAYERS = 4
UNO_TURN = 30.0                  # s per turn, then the host plays the timeout
RACE_LAPS = (2, 3, 5)
RACE_COUNTDOWN = 6.0             # s from the start message to the green light
FINISH_GRACE = 25.0              # s after the first finisher before results are final
OFFLINE_AFTER = 17.5             # s without hearing a station = off the air

D_PING, D_PONG = 0x01, 0x02
PING = struct.Struct(">BHdd")            # kind, id, t1, r1 (sender's game and radio clocks)
PONG = struct.Struct(">BHdddddd")        # kind, id, t1, r1, t2, r2 (host rx), t3, r3 (host tx)
PING_FAST, PING_SLOW = 0.25, 2.0         # s between pings: until synchronised / afterwards
SYNC_SAMPLES = 3


class Clock:
    """Offsets of the room host's clocks to ours (host = ours + offset), from ping/pong, for
    the game clock and the radio clock.  For each, the sample with the shortest round trip
    has the least room for asymmetric delay."""

    def __init__(self):
        self.samples = collections.deque(maxlen=40)       # (rtt, off, rrtt, roff, when)
        self.n = 0
        self.next_ping = 0.0

    def add(self, t1, r1, t2, r2, t3, r3, t4, r4):
        rtt = (t4 - t1) - (t3 - t2)
        rrtt = (r4 - r1) - (r3 - r2)
        if not (0 <= rtt < 5 and 0 <= rrtt < 5):
            return
        self.samples.append((rtt, ((t2 - t1) + (t3 - t4)) / 2, rrtt, ((r2 - r1) + (r3 - r4)) / 2, t4))
        self.n += 1

    def _fresh(self):
        now = time.monotonic()
        return [s for s in self.samples if now - s[4] < 120] or list(self.samples)

    @property
    def best(self):
        f = self._fresh()
        return min(f, key=lambda s: s[0]) if f else None

    @property
    def offset(self) -> float:
        b = self.best
        return b[1] if b else 0.0

    @property
    def radio_offset(self) -> float:
        f = self._fresh()
        return min(f, key=lambda s: s[2])[3] if f else 0.0

    @property
    def rtt(self) -> float | None:
        b = self.best
        return b[0] if b else None

    @property
    def synced(self) -> bool:
        return self.n >= SYNC_SAMPLES


class GameHub:
    def __init__(self, node, log=print):
        self.node = node
        self.addr = node.addr
        self.log = log
        self.lock = threading.RLock()
        self.rooms: dict[str, dict] = {}          # rid -> room (what the UI shows)
        self.rt: dict[str, dict] = {}             # rid -> runtime state (not shown)
        self.clocks: dict[int, Clock] = {}        # host addr -> Clock
        self._inbox: queue.Queue = queue.Queue()
        self._pings: dict[int, tuple] = {}        # ping id -> (host, t1)
        self._ping_id = random.randrange(1 << 16)
        self.stats = collections.Counter()
        self.listeners = []                        # test hooks: fn(event, data)
        self._running = False
        self._t_slow = 0.0

    # ================================================================ lifecycle
    def start(self):
        self._running = True
        threading.Thread(target=self._loop, name=f"games{self.addr}", daemon=True).start()

    def stop(self):
        self._running = False

    @staticmethod
    def mono() -> float:
        return time.monotonic()

    def rnow(self) -> float:
        """Radio clock (time slots)."""
        return self.node.radio.now()

    # ================================================================ helpers
    def _name(self, a: int) -> str:
        if a == self.addr:
            return self.node.name
        c = self.node.contacts.get(a)
        return (c or {}).get("name") or f"Node {a}"

    def _online(self, a: int) -> bool:
        if a == self.addr:
            return True
        c = self.node.contacts.get(a) or {}
        heard = max(c.get("last_seen", 0), self.node.mac.last_heard.get(a, 0))
        return time.time() - heard < OFFLINE_AFTER

    def _send(self, dst: int, obj: dict):
        try:
            return self.node.send_gc(dst, obj)
        except Exception as e:                  # e.g. not reachable; presence will tell
            self.log(f"games: send to {dst} failed: {e}")
            return None

    def _emit(self, ev: str, data):
        self.node.publish(ev, data)
        for fn in self.listeners:
            try:
                fn(ev, data)
            except Exception as e:
                self.log("games listener error", repr(e))

    def _publish(self):
        self._emit("gc", self.snapshot())

    def host_offset(self, room: dict) -> float:
        if room["h"] == self.addr:
            return 0.0
        c = self.clocks.get(room["h"])
        return c.offset if c else 0.0

    def host_radio_offset(self, room: dict) -> float:
        if room["h"] == self.addr:
            return 0.0
        c = self.clocks.get(room["h"])
        return c.radio_offset if c else 0.0

    def host_now(self, rid: str | None = None) -> float:
        """The game clock: the room host's monotonic clock."""
        room = self.rooms.get(rid) if rid else None
        return self.mono() + (self.host_offset(room) if room else 0.0)

    def _members(self, room: dict) -> list[int]:
        """Stations to keep informed about the lobby (not those who declined or left)."""
        return [a for a in room["seats"] if room["st"].get(a) in ("invited", "joined")]

    # ================================================================ snapshot for the UI
    def snapshot(self) -> dict:
        with self.lock:
            rooms = [self._room_public(r) for r in sorted(self.rooms.values(), key=lambda r: r["created"])]
        return dict(me=self.addr, rooms=rooms, games=GAMES, laps=list(RACE_LAPS), turn_s=UNO_TURN)

    def _room_public(self, room: dict) -> dict:
        d = {k: room.get(k) for k in ("r", "g", "h", "seats", "st", "o", "state", "players", "seed",
                                      "t0", "why", "results", "sync", "n")}
        d["names"] = {str(a): self._name(a) for a in room["seats"]}
        d["online"] = {str(a): self._online(a) for a in room["seats"]}
        d["me_seat"] = room["players"].index(self.addr) if self.addr in (room.get("players") or []) else None
        rt = self.rt.get(room["r"], {})
        if room["g"] == "uno" and rt.get("view"):
            d["uno"] = rt["view"]
        if room["g"] == "race":
            c = self.clocks.get(room["h"])
            d["clock"] = dict(synced=room["h"] == self.addr or bool(c and c.synced),
                              rtt_ms=round(c.rtt * 1e3, 1) if c and c.rtt is not None else None,
                              off_ms=round(self.host_offset(room) * 1e3, 2))
            d["P"] = rt.get("P")
            d["finish"] = {str(k): v for k, v in rt.get("finish", {}).items()}
        return d

    # ================================================================ API (web server / tests)
    def create(self, game: str, invitees, opts=None) -> str:
        if game not in GAMES:
            raise ValueError("unknown game")
        inv = []
        for a in invitees:
            a = int(a)
            if a != self.addr and a not in inv:
                inv.append(a)
        if not 1 <= len(inv) <= MAX_PLAYERS - 1:
            raise ValueError(f"invite 1 to {MAX_PLAYERS - 1} stations")
        offline = [a for a in inv if not self._online(a)]
        if offline:
            raise ValueError(f"{', '.join(self._name(a) for a in offline)} is not on the air")
        o = {}
        if game == "race":
            laps = int((opts or {}).get("laps", 3))
            o["laps"] = laps if laps in RACE_LAPS else 3
        rid = os.urandom(3).hex()
        room = dict(r=rid, g=game, h=self.addr, seats=[self.addr] + inv,
                    st={self.addr: "joined", **{a: "invited" for a in inv}}, o=o, state="lobby",
                    players=[], seed=None, t0=None, why="", results=None, sync={self.addr: True},
                    created=time.time(), n=0)
        with self.lock:
            self.rooms[rid] = room
            self.rt[rid] = {}
        msg = dict(t="inv", r=rid, g=game, h=self.addr, p=room["seats"], o=o)
        for a in inv:
            self._send(a, msg)
        self.stats["rooms_created"] += 1
        self._publish()
        return rid

    def answer(self, rid: str, ok: bool):
        with self.lock:
            room = self._room(rid)
            if room["state"] != "invited":
                raise ValueError("this invitation is no longer open")
            room["st"][self.addr] = "joined" if ok else "declined"
            room["state"] = "lobby" if ok else "closed"
            if not ok:
                room["why"] = "You declined"
        self._send(room["h"], dict(t="ans", r=rid, ok=bool(ok)))
        self._publish()

    def start_game(self, rid: str):
        with self.lock:
            room = self._room(rid)
            if room["h"] != self.addr:
                raise ValueError("only the host can start")
            if room["state"] not in ("lobby", "over"):
                raise ValueError("already running")
            players = [a for a in room["seats"] if room["st"].get(a) == "joined" and self._online(a)]
            if len(players) < 2:
                raise ValueError("nobody has joined yet")
            if room["g"] == "race":
                unsynced = [a for a in players if not room["sync"].get(a)]
                if unsynced:
                    raise ValueError(f"waiting for clock sync: {', '.join(self._name(a) for a in unsynced)}")
            # stations that never answered stay out
            dropped = [a for a in room["seats"] if room["st"].get(a) == "invited"]
            for a in dropped:
                room["st"][a] = "missed"
            room.update(state="playing", players=players, seed=random.randrange(1 << 31), results=None,
                        n=room.get("n", 0) + 1)
            room["t0"] = self.mono() + RACE_COUNTDOWN if room["g"] == "race" else None
            room["s0"] = self.rnow() if room["g"] == "race" else None
            msg = dict(t="start", r=rid, g=room["g"], seed=room["seed"], p=players, t0=room["t0"],
                       s0=room["s0"], o=room["o"], n=room["n"])
        for a in dropped:
            self._send(a, dict(t="close", r=rid, why="The game started without you"))
        for a in players:
            if a != self.addr:
                self._send(a, msg)
        self._begin(rid)
        self.stats["games_started"] += 1
        self._publish()

    def leave(self, rid: str):
        with self.lock:
            room = self._room(rid)
            was = room["state"]
            others = [a for a in self._members(room) if a != self.addr]
            room["st"][self.addr] = "left"
            room["state"] = "closed"
            room["why"] = "You left"
            if room["h"] == self.addr:
                for a in others:
                    self._send(a, dict(t="close", r=rid, why=f"{self.node.name} (host) closed the room"))
            elif was not in ("closed", "invited"):
                self._send(room["h"], dict(t="leave", r=rid))
            elif was == "invited":
                self._send(room["h"], dict(t="ans", r=rid, ok=False))
        self._publish()

    def dismiss(self, rid: str):
        with self.lock:
            room = self.rooms.get(rid)
            if room and room["state"] == "closed":
                self.rooms.pop(rid, None)
                self.rt.pop(rid, None)
        self._publish()

    def _room(self, rid: str) -> dict:
        room = self.rooms.get(rid)
        if room is None:
            raise ValueError("no such room")
        return room

    # ================================================================ incoming (MAC threads)
    def on_gc(self, src: int, obj: dict):
        """Called from the MAC receive path: hand over to the hub thread (no locks here)."""
        self._inbox.put((src, obj))

    def on_datagram(self, src: int, payload: bytes, frame=None):
        if not payload:
            return
        k = payload[0]
        if k == D_PING and len(payload) >= PING.size:
            t2, r2 = self.mono(), self.rnow()
            _, pid, t1, r1 = PING.unpack_from(payload)
            self.stats["pings_rx"] += 1
            self._dgram(src, PONG.pack(D_PONG, pid, t1, r1, t2, r2, self.mono(), self.rnow()))
        elif k == D_PONG and len(payload) >= PONG.size:
            t4, r4 = self.mono(), self.rnow()
            _, pid, t1, r1, t2, r2, t3, r3 = PONG.unpack_from(payload)
            p = self._pings.pop(pid, None)
            if p and p[0] == src:
                with self.lock:
                    self.clocks.setdefault(src, Clock()).add(t1, r1, t2, r2, t3, r3, t4, r4)
                self.stats["pongs_rx"] += 1
        elif k == R.KIND_STATE:
            self._on_race_state(src, payload)

    # ================================================================ hub thread
    def _poll_interval(self) -> float:
        """How long the hub thread may sleep: messages wake it at once; race slots need it
        every 2 ms on a real-time radio (5 ms when the radio starts bursts on exact samples)."""
        racing = any(r["g"] == "race" and r["state"] == "playing" for r in list(self.rooms.values()))
        if not racing:
            return 0.02
        return 0.005 if getattr(self.node.radio, "timed_tx", False) else 0.002

    def _loop(self):
        while self._running:
            try:
                src, obj = self._inbox.get(timeout=self._poll_interval())
                try:
                    self._handle(src, obj)
                except Exception as e:
                    self.log("games: bad message", src, repr(e))
                continue
            except queue.Empty:
                pass
            try:
                self._tick()
            except Exception as e:
                self.log("games tick error", repr(e))

    def _handle(self, src: int, o: dict):
        t = o.get("t")
        fn = getattr(self, "_on_" + str(t), None)
        if fn is None:
            self.log(f"games: unknown message {t!r} from {src}")
            return
        self.stats["gc_rx_" + str(t)] += 1
        fn(src, o)

    def _tick(self):
        now = self.mono()
        self._race_slots(now)
        if now - self._t_slow >= 0.1:
            self._t_slow = now
            self._pings_due(now)
            self._uno_timers(now)
            self._race_timers(now)
            self._presence(now)

    def _dgram(self, dst: int, payload: bytes, urgent: bool = True, at: float | None = None,
               latest: float | None = None):
        try:
            return self.node.send_datagram(dst, payload, urgent, at, latest)
        except Exception as e:
            self.log("games: datagram failed", repr(e))
            return None

    # ================================================================ lobby messages
    def _on_inv(self, src, o):
        rid = o["r"]
        with self.lock:
            if rid in self.rooms:
                return
            seats = [int(a) for a in o["p"]]
            if self.addr not in seats or o["g"] not in GAMES or src != o["h"]:
                return
            self.rooms[rid] = dict(r=rid, g=o["g"], h=src, seats=seats,
                                   st={a: ("joined" if a == src else "invited") for a in seats},
                                   o=o.get("o") or {}, state="invited", players=[], seed=None, t0=None,
                                   why="", results=None, sync={}, created=time.time(), n=0)
            self.rt[rid] = {}
        self._emit("toast", f"🎮 {self._name(src)} invites you to {GAMES[o['g']]}. Open the game center to join.")
        self._publish()

    def _on_ans(self, src, o):
        with self.lock:
            room = self.rooms.get(o["r"])
            if not room or room["h"] != self.addr or src not in room["seats"]:
                return
            if room["st"].get(src) not in ("invited", "joined", "declined"):
                return
            room["st"][src] = "joined" if o.get("ok") else "declined"
        self._send_lobby(room)
        self._publish()

    def _send_lobby(self, room: dict):
        msg = dict(t="lobby", r=room["r"], g=room["g"], h=room["h"], seats=room["seats"],
                   st={str(k): v for k, v in room["st"].items()}, o=room["o"],
                   sync={str(k): v for k, v in room["sync"].items()}, state=room["state"])
        for a in self._members(room):
            if a != self.addr:
                self._send(a, msg)

    def _on_lobby(self, src, o):
        with self.lock:
            room = self.rooms.get(o["r"])
            if not room or room["h"] != src:
                return
            room["st"] = {int(k): v for k, v in o["st"].items()}
            room["sync"] = {int(k): v for k, v in (o.get("sync") or {}).items()}
            room["o"] = o.get("o") or room["o"]
            if room["state"] == "invited" and room["st"].get(self.addr) == "joined":
                room["state"] = "lobby"
        self._publish()

    def _on_sync(self, src, o):
        with self.lock:
            room = self.rooms.get(o["r"])
            if not room or room["h"] != self.addr or src not in room["seats"]:
                return
            if room["sync"].get(src):
                return
            room["sync"][src] = True
        self._send_lobby(room)
        self._publish()

    def _on_start(self, src, o):
        with self.lock:
            room = self.rooms.get(o["r"])
            if not room or room["h"] != src or self.addr not in o["p"]:
                return
            room.update(state="playing", players=[int(a) for a in o["p"]], seed=o["seed"], t0=o.get("t0"),
                        s0=o.get("s0"), o=o.get("o") or room["o"], results=None, n=o.get("n", 1))
            room["st"][self.addr] = "joined"
        self._begin(o["r"])
        self._publish()

    def _on_leave(self, src, o):
        rid = o["r"]
        with self.lock:
            room = self.rooms.get(rid)
            if not room or room["h"] != self.addr or src not in room["seats"]:
                return
            if room["st"].get(src) == "left":
                return
            room["st"][src] = "left"
        self._player_gone(rid, src, f"{self._name(src)} left")

    def _on_close(self, src, o):
        with self.lock:
            room = self.rooms.get(o["r"])
            if not room or room["h"] != src:
                return
            room["state"] = "closed"
            room["why"] = o.get("why") or "The host closed the room"
        self._emit("toast", room["why"])
        self._publish()

    def _player_gone(self, rid: str, a: int, why: str):
        """Host: a player left or went off the air."""
        with self.lock:
            room = self.rooms[rid]
            if room["st"].get(a) not in ("left", "gone"):
                room["st"][a] = "gone"
            playing = room["state"] == "playing" and a in room["players"]
            rt = self.rt.get(rid, {})
            if playing and room["g"] == "uno" and rt.get("uno"):
                rt["uno"].remove_seat(room["players"].index(a))
        self._emit("toast", why)
        self._broadcast_note(rid, why)
        if playing and room["g"] == "uno":
            self._uno_push(rid)
        self._send_lobby(room)
        self._publish()

    def _broadcast_note(self, rid: str, text: str):
        room = self.rooms[rid]
        for a in self._members(room):
            if a != self.addr:
                self._send(a, dict(t="note", r=rid, m=text))

    def _on_note(self, src, o):
        room = self.rooms.get(o["r"])
        if room and room["h"] == src:
            self._emit("toast", o.get("m", ""))

    def _presence(self, now: float):
        with self.lock:
            rooms = list(self.rooms.values())
        for room in rooms:
            if room["state"] == "closed":
                continue
            if room["h"] != self.addr:
                if not self._online(room["h"]):
                    with self.lock:
                        room["state"] = "closed"
                        room["why"] = f"{self._name(room['h'])} (host) went off the air"
                    self._emit("toast", room["why"])
                    self._publish()
                continue
            for a in list(room["seats"]):
                if a != self.addr and room["st"].get(a) in ("invited", "joined") and not self._online(a):
                    self._player_gone(room["r"], a, f"{self._name(a)} went off the air")

    # ================================================================ game start
    def _begin(self, rid: str):
        room = self.rooms[rid]
        rt = self.rt.setdefault(rid, {})
        if room["g"] == "uno":
            if room["h"] == self.addr:
                rt.clear()
                rt["uno"] = Uno(len(room["players"]), room["seed"], dealer=random.randrange(len(room["players"])))
                rt.update(ver=0, sent_upto={}, pending={}, deadline=None, turn_key=None)
                self._uno_push(rid)
        else:
            self._race_begin(rid)

    # ================================================================ UNO (host)
    def uno_action(self, rid: str, a: str, **kw):
        """The local player's intent (from the browser)."""
        room = self._room(rid)
        if room["g"] != "uno" or room["state"] != "playing":
            raise ValueError("no UNO game running")
        seat = room["players"].index(self.addr)
        rt = self.rt[rid]
        rt["seq"] = rt.get("seq", 0) + 1
        intent = dict(t="ui", r=rid, n=rt["seq"], a=a, **{k: v for k, v in kw.items() if v is not None})
        rt["sent_at"] = self.mono()
        if room["h"] == self.addr:
            self._on_ui(self.addr, intent)
        else:
            self._send(room["h"], intent)
        self.stats["uno_intents"] += 1
        return intent["n"]

    def uno_next(self, rid: str):
        with self.lock:
            room = self._room(rid)
            rt = self.rt[rid]
            if room["h"] != self.addr or not rt.get("uno") or rt["uno"].phase != "over":
                raise ValueError("only the host deals the next round, after a round has ended")
            g = rt["uno"]
            if len(g.active) < 2:
                raise ValueError("not enough players left")
            rt["uno"] = g.next_round(random.randrange(1 << 31))
            rt["sent_upto"] = {}
            rt["pending"] = {}
        self._uno_push(rid)

    def _on_ui(self, src, o):
        rid = o["r"]
        with self.lock:
            room = self.rooms.get(rid)
            rt = self.rt.get(rid, {})
            g = rt.get("uno")
            if not room or room["h"] != self.addr or g is None or src not in room["players"]:
                return
            seat = room["players"].index(src)
            a = o.get("a")
            try:
                if a == "play":
                    g.play(seat, int(o["c"]), o.get("col"), bool(o.get("uno")))
                elif a == "draw":
                    g.draw(seat)
                elif a == "keep":
                    g.keep(seat)
                elif a == "color":
                    g.choose_color(seat, int(o["col"]))
                elif a == "catch":
                    g.catch(seat)
                else:
                    raise UnoError("Unknown move")
                err = None
            except (UnoError, KeyError, ValueError, TypeError) as e:
                err = str(e) if isinstance(e, UnoError) else "Bad move"
        if err:
            self.stats["uno_refused"] += 1
            if src == self.addr:
                self._emit("unoerr", dict(r=rid, n=o.get("n"), m=err))
            else:
                self._send(src, dict(t="ue", r=rid, n=o.get("n"), m=err))
            return
        self._uno_push(rid)

    def _uno_push(self, rid: str):
        """Send every player their view of the table (host)."""
        with self.lock:
            room = self.rooms[rid]
            rt = self.rt[rid]
            g = rt["uno"]
            rt["ver"] += 1
            now = self.mono()
            key = (g.turn, g.phase, g.round_no)
            if g.phase == "over":
                rt["deadline"] = None
            elif key != rt.get("turn_key") or rt.get("deadline") is None:
                rt["deadline"] = now + UNO_TURN
            rt["turn_key"] = key
            out = []
            for seat, a in enumerate(room["players"]):
                if seat in g.out or room["st"].get(a) in ("left", "gone"):
                    continue
                start = rt["sent_upto"].get(seat, 0)
                pend = rt["pending"].get(seat)
                if pend is not None and self.node.mac.withdraw(pend[0]):
                    start = pend[1]                     # the replaced view's events ride along
                    self.stats["uno_views_withdrawn"] += 1
                evs = [g.public_event(e, seat) for e in g.events[start:]][-40:]
                view = dict(g.view(seat), v=rt["ver"], ev=evs, r=rid, p=room["players"],
                            dl=round(rt["deadline"] - now, 2) if rt["deadline"] else None)
                rt["sent_upto"][seat] = len(g.events)
                if a == self.addr:
                    out.append(view)
                else:
                    m = self._send(a, dict(view, t="uv"))
                    if m is not None:
                        rt["pending"][seat] = (m, start)
                    self.stats["uno_views_sent"] += 1
        for view in out:
            self._on_uv(self.addr, dict(view, t="uv"))

    def _uno_timers(self, now: float):
        for rid, rt in list(self.rt.items()):
            g = rt.get("uno")
            if g is None or rt.get("deadline") is None or now < rt["deadline"]:
                continue
            with self.lock:
                if self.rooms.get(rid, {}).get("state") != "playing":
                    continue
                g.timeout()
                rt["deadline"] = None
            self.stats["uno_timeouts"] += 1
            self._uno_push(rid)

    # ================================================================ UNO (every player)
    def _on_uv(self, src, o):
        rid = o["r"]
        with self.lock:
            room = self.rooms.get(rid)
            if not room or room["h"] != src:
                return
            rt = self.rt.setdefault(rid, {})
            old = rt.get("view")
            if old and old["v"] >= o["v"] and old.get("round") == o.get("round"):
                self.stats["uno_views_stale"] += 1
                return
            view = {k: v for k, v in o.items() if k != "t"}
            if view.get("dl") is not None:
                view["dl_at"] = time.time() + view["dl"]       # wall clock for the browser
            rt["view"] = view
            started = room["state"] in ("invited", "lobby")
            if started:                    # the first view overtook the start message
                room.update(state="playing", players=[int(a) for a in o.get("p", [])])
        if started:
            self._publish()
        self._emit("uno", view)

    def _on_ue(self, src, o):
        room = self.rooms.get(o["r"])
        if room and room["h"] == src:
            self._emit("unoerr", dict(r=o["r"], n=o.get("n"), m=o.get("m", "Refused")))

    # ================================================================ race
    def _race_begin(self, rid: str):
        room = self.rooms[rid]
        rt = self.rt[rid]
        n = len(room["players"])
        me = room["players"].index(self.addr) if self.addr in room["players"] else None
        enc = 29 if self.node.crypto.enabled else 1
        air = self.node.mac.datagram_airtime(R.HEAD.size + R.EVENT.size + enc)
        rt.clear()
        rt.update(seat=me, n=n, P=R.slot_period(n, air), air=air, seq=0, state=None, state_at=0.0,
                  events=collections.deque(), next_slot=None, seen={}, finish={}, first_finish=None,
                  last=dict(), rx=collections.defaultdict(lambda: dict(n=0, first=None, last=None, ages=[],
                                                                       seqs=0, lost=0, lastseq=None)),
                  tx=0, skipped=0, eid=0)

    def race_post(self, rid: str, state: dict, events=()):
        """The browser's newest car state (20 per second) and any new events."""
        with self.lock:
            room = self.rooms.get(rid)
            rt = self.rt.get(rid)
            if not room or room["g"] != "race" or room["state"] not in ("playing", "over") or rt is None \
                    or rt.get("seat") is None:
                return False
            rt["state"] = dict(state)
            rt["state_at"] = self.mono()
            fin = None
            for e in events or ():
                rt["events"].append([dict(e), R.REPEATS + 1])
                if e.get("k") == "finish" and rt["seat"] not in rt["finish"]:
                    fin = int(e["t"])
            if fin is not None:
                self._record_finish(rid, rt["seat"], fin)
        if fin is not None:
            msg = dict(t="rf", r=rid, s=rt["seat"], ft=fin)
            for a in room["players"]:
                if a != self.addr and room["st"].get(a) == "joined":
                    self._send(a, msg)
        return True

    def _record_finish(self, rid: str, seat: int, ft: int):
        rt = self.rt[rid]
        if seat in rt["finish"]:
            return
        rt["finish"][seat] = ft
        if rt["first_finish"] is None:
            rt["first_finish"] = self.mono()
        self._emit("racefin", dict(r=rid, s=seat, ft=ft))

    def _on_rf(self, src, o):
        rid = o["r"]
        with self.lock:
            room = self.rooms.get(rid)
            if not room or src not in room.get("players", []) or rid not in self.rt:
                return
            if room["players"].index(src) != int(o["s"]):
                return
            if "finish" in self.rt[rid]:
                self._record_finish(rid, int(o["s"]), int(o["ft"]))

    def _race_slots(self, now: float):
        """Put the newest car state on the air in this station's slot.  A radio with timed
        transmission (the simulator) gets the burst a little ahead with its exact start time;
        the others get it at the slot time.  A slot that can no longer be met is skipped:
        a late burst would land in the next car's slot."""
        radio = self.node.radio
        timed = getattr(radio, "timed_tx", False)
        for rid, rt in list(self.rt.items()):
            if rt.get("seat") is None or "P" not in rt:
                continue
            room = self.rooms.get(rid)
            if not room or room["g"] != "race" or room.get("s0") is None:
                continue
            # after the results, keep sending for 2 s so the last events (finish) get their repeats
            if room["state"] != "playing" and not (room["state"] == "over" and rt.get("over_at")
                                                   and now - rt["over_at"] < 2.0):
                continue
            P, n, k = rt["P"], rt["n"], rt["seat"]
            lead = min(0.02, 0.3 * P) if timed else 0.0
            roff = self.host_radio_offset(room)
            rnow = self.rnow()
            hnow = rnow + roff                                # host's radio clock
            epoch = room["s0"] + k * P / n
            if rt["next_slot"] is None:
                rt["next_slot"] = epoch + math.ceil((hnow + lead - epoch) / P) * P
            if hnow < rt["next_slot"] - lead:
                continue
            slot = rt["next_slot"]
            rt["next_slot"] += P
            late = hnow - slot
            # a burst may start late as long as it still ends before the next car's slot
            tol = max(0.0, P / n - rt["air"] - 0.003)
            if late > tol:
                missed = math.floor(late / P) + 1             # this slot and any others that passed
                rt["next_slot"] = slot + missed * P
                if rt["next_slot"] - lead <= hnow:
                    rt["next_slot"] += P
                rt["skipped"] += missed
                continue
            st = rt["state"]
            if st is None or now - rt["state_at"] > 1.5:
                continue                                     # browser not posting (tab closed?)
            if not timed and radio.tx_backlog(BROADCAST_CODE) > 0.5 * P / n:
                rt["skipped"] += 1                           # something else still on the air
                continue
            with self.lock:
                evs, keep = [], collections.deque()
                for item in rt["events"]:
                    if len(evs) < R.MAX_EVENTS:
                        evs.append(item[0])
                        item[1] -= 1
                    if item[1] > 0:
                        keep.append(item)
                rt["events"] = keep
                rt["seq"] = (rt["seq"] + 1) & 0xFF
                pkt = R.pack_state(bytes.fromhex(rid), k, rt["seq"], st, evs)
            done = self._dgram(BROADCAST_ADDR, pkt, urgent=False, at=(slot - roff) if timed else None,
                               latest=(slot - roff + tol) if timed else None)
            if done is None:
                rt["skipped"] += 1
                continue
            rt["tx"] += 1
            self.stats["race_tx"] += 1

    def _on_race_state(self, src: int, payload: bytes):
        p = R.unpack_state(payload)
        if p is None:
            return
        rid = p["rid"].hex()
        with self.lock:
            room = self.rooms.get(rid)
            rt = self.rt.get(rid)
            if not room or rt is None or "seen" not in rt or room["state"] not in ("playing", "over"):
                return
            seat = p["seat"]
            if seat >= len(room["players"]) or room["players"][seat] != src:
                return
            now_ms = (self.host_now(rid) - room["t0"]) * 1000.0
            age = (now_ms - p["t"]) / 1000.0
            st = rt["rx"][seat]
            st["n"] += 1
            if st["first"] is None:
                st["first"] = self.mono()
            st["last"] = self.mono()
            st["ages"].append(age)
            if len(st["ages"]) > 2000:
                del st["ages"][:1000]
            if st["lastseq"] is not None:
                gap = (p["seq"] - st["lastseq"]) & 0xFF
                if 0 < gap < 128:
                    st["seqs"] += gap
                    st["lost"] += gap - 1
            st["lastseq"] = p["seq"]
            seen = rt["seen"].setdefault(seat, {})
            fresh = []
            for e in p["ev"]:
                t_seen = seen.get(e["id"])
                if t_seen is not None and self.mono() - t_seen < 30:
                    self.stats["race_ev_dups"] += 1
                    continue
                seen[e["id"]] = self.mono()
                fresh.append(e)
                self.stats["race_ev_rx"] += 1
                if e["k"] == "finish":
                    self._record_finish(rid, seat, int(e["t"]))
            rt["last"][seat] = p
        out = {k: v for k, v in p.items() if k not in ("rid", "seat")}
        out.update(r=rid, s=seat, ev=fresh, age=round(age, 3))
        self._emit("race", out)

    def _race_timers(self, now: float):
        for rid, rt in list(self.rt.items()):
            room = self.rooms.get(rid)
            if not room or room["g"] != "race" or room["state"] != "playing" or "finish" not in rt:
                continue
            if room["h"] != self.addr:
                continue
            active = [s for s, a in enumerate(room["players"]) if room["st"].get(a) == "joined"]
            done = all(s in rt["finish"] for s in active)
            late = rt["first_finish"] is not None and now - rt["first_finish"] > FINISH_GRACE
            alone = len(active) < 2 and now > (room["t0"] or 0) + 1
            if done or late or alone:
                self._race_results(rid)

    def _race_results(self, rid: str):
        with self.lock:
            room = self.rooms[rid]
            rt = self.rt[rid]
            res = []
            for s, a in enumerate(room["players"]):
                last = rt["last"].get(s) if a != self.addr else rt.get("state")
                d = (last or {}).get("d", 0)
                res.append(dict(s=s, a=a, name=self._name(a), ft=rt["finish"].get(s), d=d,
                                gone=room["st"].get(a) != "joined"))
            res.sort(key=lambda r: (r["ft"] is None, r["ft"] if r["ft"] is not None else -r["d"]))
            room["results"] = res
            room["state"] = "over"
            rt["over_at"] = self.mono()
        for a in room["players"]:
            if a != self.addr and room["st"].get(a) == "joined":
                self._send(a, dict(t="rr", r=rid, res=res))
        self._emit("raceover", dict(r=rid, res=res))
        self._publish()

    def _on_rr(self, src, o):
        with self.lock:
            room = self.rooms.get(o["r"])
            if not room or room["h"] != src:
                return
            room["results"] = o["res"]
            room["state"] = "over"
            if o["r"] in self.rt:
                self.rt[o["r"]]["over_at"] = self.mono()
        self._emit("raceover", dict(r=o["r"], res=o["res"]))
        self._publish()

    def race_stats(self, rid: str) -> dict:
        """Per-car reception statistics (tests, dashboard)."""
        with self.lock:
            rt = self.rt.get(rid, {})
            out = {}
            for seat, st in rt.get("rx", {}).items():
                dur = (st["last"] - st["first"]) if st["first"] is not None and st["last"] else 0
                ages = sorted(st["ages"])
                out[seat] = dict(n=st["n"], rate=round((st["n"] - 1) / dur, 2) if dur > 0 else None,
                                 age_med=round(ages[len(ages) // 2], 3) if ages else None,
                                 age_p90=round(ages[int(0.9 * (len(ages) - 1))], 3) if ages else None,
                                 lost=st["lost"], seqs=st["seqs"])
            return dict(rx=out, tx=rt.get("tx", 0), skipped=rt.get("skipped", 0), P=rt.get("P"),
                        air=rt.get("air"))

    # ================================================================ clock sync
    def _pings_due(self, now: float):
        hosts = {}
        with self.lock:
            for room in self.rooms.values():
                if room["g"] != "race" or room["h"] == self.addr:
                    continue
                if room["state"] in ("lobby", "playing") and room["st"].get(self.addr) == "joined":
                    hosts[room["h"]] = room
        for h, room in hosts.items():
            c = self.clocks.setdefault(h, Clock())
            due = getattr(c, "next_ping", 0.0)
            if now < due:
                continue
            c.next_ping = now + (PING_FAST if not c.synced else PING_SLOW) * random.uniform(0.8, 1.2)
            self._ping_id = (self._ping_id + 1) & 0xFFFF
            t1 = self.mono()
            self._pings[self._ping_id] = (h, t1)
            if len(self._pings) > 64:
                for k in list(self._pings)[:-32]:
                    del self._pings[k]
            self._dgram(h, PING.pack(D_PING, self._ping_id, t1, self.rnow()))
            self.stats["pings_tx"] += 1
            if c.synced and not room["sync"].get(self.addr):
                room["sync"][self.addr] = True
                self._send(h, dict(t="sync", r=room["r"], rtt=round(c.rtt or 0, 4)))
                self._publish()
