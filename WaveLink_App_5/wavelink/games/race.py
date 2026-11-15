"""Gold Code GP: the race's datagram format.

Every station drives its own car (physics runs in the browser at 120 steps/s) and, in
its own time slot, broadcasts the car's newest state as one unacknowledged datagram on
the broadcast code.  Events (missile fired, mine dropped, EMP, hit, finish) ride in the
same packets and are repeated in the next 4 packets, so one lost packet loses nothing.

State packet (28 bytes + 13 bytes per event, big-endian)::

    0  kind 0x10        1-3  room id        4  seat          5  sequence number
    6  t: ms since the green light (host clock, int32)
    10 x, 12 y (int16, 0.5 px)   14 heading (uint16, 2*pi/65536)
    16 vx, 18 vy (int16, px/s)   20 yaw rate (int16, mrad/s)
    22 distance raced (uint24, px + 4096 so the grid, just behind the line, is positive)
    25 flags   26 item | steer | throttle   27 number of events

    event: type, id, t (int32 ms), x, y (int16, 0.5 px), a (uint16), b (uint8)

flags: 1 boost, 2 shield, 4 stunned (EMP), 8 finished, 16 braking, 32 reversing,
       64 off the track, 128 spinning (hit)
"""
from __future__ import annotations

import math
import struct

KIND_STATE = 0x10
HEAD = struct.Struct(">B3sBBihhHhhh3sBBB")
EVENT = struct.Struct(">BBihhHB")
assert HEAD.size == 28 and EVENT.size == 13
D_BIAS = 4096
REPEATS = 4                       # each event is sent in 1 + REPEATS packets
MAX_EVENTS = 6                    # per packet

EV_MISSILE, EV_MINE, EV_EMP, EV_HIT, EV_FINISH, EV_BLOCK = 1, 2, 3, 4, 5, 6
EV_NAMES = {EV_MISSILE: "missile", EV_MINE: "mine", EV_EMP: "emp", EV_HIT: "hit",
            EV_FINISH: "finish", EV_BLOCK: "block"}
EV_CODES = {v: k for k, v in EV_NAMES.items()}

FIELDS = ("t", "x", "y", "h", "vx", "vy", "w", "d", "fl", "it")


def _i16(v: float) -> int:
    return max(-32768, min(32767, int(round(v))))


def pack_state(rid: bytes, seat: int, seq: int, s: dict, events=()) -> bytes:
    """s: dict with t (ms), x, y (px), h (rad), vx, vy (px/s), w (rad/s), d (px), fl, it."""
    d = max(0, min((1 << 24) - 1, int(round(s.get("d", 0))) + D_BIAS))
    events = list(events)[:MAX_EVENTS]
    head = HEAD.pack(KIND_STATE, rid, seat & 0xFF, seq & 0xFF, int(s.get("t", 0)),
                     _i16(2 * s.get("x", 0)), _i16(2 * s.get("y", 0)),
                     int(round(s.get("h", 0) / (2 * math.pi) * 65536)) & 0xFFFF,
                     _i16(s.get("vx", 0)), _i16(s.get("vy", 0)), _i16(1000 * s.get("w", 0)),
                     d.to_bytes(3, "big"), int(s.get("fl", 0)) & 0xFF, int(s.get("it", 0)) & 0xFF,
                     len(events))
    body = b"".join(EVENT.pack(EV_CODES.get(e["k"], 0) if isinstance(e["k"], str) else int(e["k"]),
                               int(e["id"]) & 0xFF, int(e["t"]), _i16(2 * e.get("x", 0)),
                               _i16(2 * e.get("y", 0)), int(e.get("a", 0)) & 0xFFFF,
                               int(e.get("b", 0)) & 0xFF)
                    for e in events)
    return head + body


def unpack_state(b: bytes) -> dict | None:
    if len(b) < HEAD.size or b[0] != KIND_STATE:
        return None
    (_, rid, seat, seq, t, x, y, h, vx, vy, w, d3, fl, it, nev) = HEAD.unpack_from(b)
    out = dict(rid=rid, seat=seat, seq=seq, t=t, x=x / 2, y=y / 2, h=h / 65536 * 2 * math.pi,
               vx=vx, vy=vy, w=w / 1000, d=int.from_bytes(d3, "big") - D_BIAS, fl=fl, it=it, ev=[])
    off = HEAD.size
    for _ in range(nev):
        if off + EVENT.size > len(b):
            return None
        k, eid, et, ex, ey, a, bb = EVENT.unpack_from(b, off)
        out["ev"].append(dict(k=EV_NAMES.get(k, str(k)), id=eid, t=et, x=ex / 2, y=ey / 2, a=a, b=bb))
        off += EVENT.size
    return out


def slot_period(n_cars: int, airtime: float, max_rate: float = 15.0) -> float:
    """Every car gets one slot per period P; P is long enough that the n_cars bursts (plus
    guard time for clock error and TX latency jitter) never overlap."""
    return max(1.0 / max_rate, n_cars * airtime * 1.7)
