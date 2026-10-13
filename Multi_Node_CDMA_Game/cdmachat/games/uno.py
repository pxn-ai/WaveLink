"""UNO rules engine: the standard rules, no house rules.

Pure game logic, no I/O.  The game center's host station owns one `Uno` object per room:
it shuffles, deals, keeps every hand and checks every move.  Players only ever see a
`view(seat)` (their own hand plus the others' card counts) and send intents, which the
host applies with `play`, `draw`, `keep`, `choose_color`, `catch` and `timeout`.

Cards are numbered 0..107 so a hand travels as a list of small integers; `CARDS[id]` is
(colour, value) and the browser rebuilds the same table (web/static/uno.js).

    ids   0- 24  red:    0, 1,1, 2,2 .. 9,9, Skip x2, Reverse x2, Draw Two x2
         25- 49  yellow  (same)
         50- 74  green   (same)
         75- 99  blue    (same)
        100-103  Wild
        104-107  Wild Draw Four

Rules implemented (Mattel's official rules):
  * 7 cards each.  The first discard: a Wild Draw Four goes back and another card is
    turned; a Wild lets the first player name the colour; Skip skips the first player;
    Reverse lets the dealer start and turns the direction; Draw Two makes the first player
    draw two and lose the turn.
  * Match the colour or the number/symbol of the top card, or play a Wild.  Wild Draw
    Four only when you hold no card of the current colour.
  * Instead of playing you may draw one card; if it can be played you may play it at
    once, otherwise (or if you keep it) the turn passes.
  * Skip, Reverse (a Skip with two players), Draw Two and Wild Draw Four (the next player
    draws and loses the turn; no stacking).
  * UNO: call it when you play your second-to-last card.  Until the next player acts,
    anyone can catch an uncalled UNO: the caught player draws two.
  * The round ends when a hand is empty (a final Draw Two / Wild Draw Four still makes the
    next player draw).  The winner scores the cards left in the other hands: numbers at
    face value, Skip/Reverse/Draw Two 20, Wild/Wild Draw Four 50.
  * An empty draw pile is refilled by shuffling the discards under the top card.
"""
from __future__ import annotations

import random

COLORS = ("red", "yellow", "green", "blue")
SKIP, REVERSE, DRAW2, WILD, WILD4 = 10, 11, 12, 13, 14
VALUE_NAMES = {SKIP: "Skip", REVERSE: "Reverse", DRAW2: "Draw Two", WILD: "Wild", WILD4: "Wild Draw Four"}
HAND_SIZE = 7
N_CARDS = 108


def _build() -> tuple:
    cards = []
    for c in range(4):
        cards.append((c, 0))
        for v in list(range(1, 10)) + [SKIP, REVERSE, DRAW2]:
            cards += [(c, v), (c, v)]
    cards += [(-1, WILD)] * 4 + [(-1, WILD4)] * 4
    assert len(cards) == N_CARDS
    return tuple(cards)


CARDS = _build()                       # id -> (colour 0..3 or -1, value 0..14)


def card_name(cid: int) -> str:
    c, v = CARDS[cid]
    return (VALUE_NAMES[v] if c < 0 else f"{COLORS[c].capitalize()} {VALUE_NAMES.get(v, v)}")


def points(cid: int) -> int:
    v = CARDS[cid][1]
    return v if v <= 9 else (20 if v <= DRAW2 else 50)


class UnoError(ValueError):
    """An illegal move; the message is shown to the player who tried it."""


class Uno:
    """One round of UNO for 2-4 seats.  Seat order is the order of play (direction +1)."""

    def __init__(self, n_players: int, seed: int, dealer: int = 0, scores=None, round_no: int = 1,
                 out=()):
        if not 2 <= n_players <= 4:
            raise ValueError("UNO here is for 2-4 players")
        self.n = n_players
        self.out = set(out)             # seats whose player left the room (skipped, no cards)
        if n_players - len(self.out) < 2:
            raise ValueError("UNO needs at least two players")
        self.rng = random.Random(seed)
        self.dealer = dealer % n_players
        while self.dealer in self.out:
            self.dealer = (self.dealer + 1) % n_players
        self.round_no = round_no
        self.scores = list(scores) if scores else [0] * n_players
        self.hands: list[list[int]] = [[] for _ in range(n_players)]
        self.draw_pile = list(range(N_CARDS))
        self.rng.shuffle(self.draw_pile)
        self.discard: list[int] = []
        self.color = 0                  # colour in force (0..3); set by Wilds
        self.direction = 1
        self.turn = 0
        self.phase = "play"             # play | drawn | color | over
        self.drawn: int | None = None   # card just drawn by the player on turn (phase 'drawn')
        self.vulnerable: int | None = None  # seat with one card that did not call UNO
        self.winner: int | None = None
        self.round_points = 0
        self.events: list[dict] = []    # every event of the round, numbered by 'i'
        self._deal()

    # ------------------------------------------------------------------ helpers
    def _ev(self, **e):
        e["i"] = len(self.events)
        self.events.append(e)
        return e

    def _next(self, seat: int, steps: int = 1) -> int:
        for _ in range(steps):
            seat = (seat + self.direction) % self.n
            while seat in self.out:
                seat = (seat + self.direction) % self.n
        return seat

    @property
    def active(self) -> list[int]:
        return [s for s in range(self.n) if s not in self.out]

    def _take(self) -> int | None:
        if not self.draw_pile:
            if len(self.discard) <= 1:
                return None            # every other card is in a hand
            top = self.discard.pop()
            self.draw_pile = self.discard
            self.rng.shuffle(self.draw_pile)
            self.discard = [top]
            self._ev(e="shuffle", k=len(self.draw_pile))
        return self.draw_pile.pop()

    def _give(self, seat: int, k: int, why: str) -> list[int]:
        got = []
        for _ in range(k):
            c = self._take()
            if c is None:
                break
            self.hands[seat].append(c)
            got.append(c)
        # the cards themselves are private: views show them to the drawer only
        self._ev(e="draw", s=seat, k=len(got), why=why, cards=got)
        return got

    def _deal(self):
        for _ in range(HAND_SIZE):
            for s in self.active:
                self.hands[s].append(self.draw_pile.pop())
        first = self.draw_pile.pop()
        while CARDS[first][1] == WILD4:          # goes back into the deck
            self.draw_pile.insert(self.rng.randrange(len(self.draw_pile) + 1), first)
            first = self.draw_pile.pop()
        self.discard.append(first)
        c, v = CARDS[first]
        self.turn = self._next(self.dealer)
        self._ev(e="deal", dealer=self.dealer, top=first, round=self.round_no)
        if v == WILD:
            self.phase = "color"                 # first player names the colour
            return
        self.color = c
        if v == SKIP:
            self._ev(e="skip", s=self.turn)
            self.turn = self._next(self.turn)
        elif v == REVERSE:
            self.direction = -1
            self._ev(e="reverse", d=-1)
            self.turn = self.dealer
        elif v == DRAW2:
            self._give(self.turn, 2, "draw2")
            self._ev(e="skip", s=self.turn)
            self.turn = self._next(self.turn)

    # ------------------------------------------------------------------ queries
    @property
    def top(self) -> int:
        return self.discard[-1]

    def playable(self, cid: int, seat: int | None = None) -> bool:
        c, v = CARDS[cid]
        if v == WILD:
            return True
        if v == WILD4:
            seat = self.turn if seat is None else seat
            return not any(CARDS[h][0] == self.color for h in self.hands[seat])
        tv = CARDS[self.top][1]
        return c == self.color or (v == tv and tv <= DRAW2)

    def legal_cards(self, seat: int) -> list[int]:
        if self.phase == "drawn":
            return [self.drawn] if seat == self.turn and self.playable(self.drawn, seat) else []
        if self.phase != "play" or seat != self.turn:
            return []
        return [h for h in self.hands[seat] if self.playable(h, seat)]

    def card_total(self) -> int:
        return sum(len(h) for h in self.hands) + len(self.draw_pile) + len(self.discard)

    # ------------------------------------------------------------------ moves
    def _check_turn(self, seat: int, phases):
        if self.phase == "over":
            raise UnoError("The round is over")
        if seat != self.turn:
            raise UnoError("It is not your turn")
        if self.phase not in phases:
            raise UnoError({"color": "Pick a colour first", "drawn": "Play the card you drew or keep it",
                            "play": "Draw a card first"}.get(self.phase, "Not now"))

    def _close_uno_window(self, actor: int):
        """The next player acted: an uncalled UNO can no longer be caught."""
        if self.vulnerable is not None and actor != self.vulnerable:
            self.vulnerable = None

    def play(self, seat: int, cid: int, color: int | None = None, uno: bool = False) -> dict:
        self._check_turn(seat, ("play", "drawn"))
        if cid not in self.hands[seat]:
            raise UnoError("You don't have that card")
        if self.phase == "drawn" and cid != self.drawn:
            raise UnoError("After drawing you can only play the card you drew")
        c, v = CARDS[cid]
        if not self.playable(cid, seat):
            if v == WILD4:
                raise UnoError(f"Wild Draw Four only when you have no {COLORS[self.color]} card")
            raise UnoError(f"{card_name(cid)} does not match {COLORS[self.color]} / {card_name(self.top)}")
        if v in (WILD, WILD4):
            if color is None or not 0 <= int(color) <= 3:
                raise UnoError("Pick a colour for the Wild")
            color = int(color)
        self._close_uno_window(seat)
        self.hands[seat].remove(cid)
        self.discard.append(cid)
        self.drawn = None
        self.phase = "play"
        self.color = color if v in (WILD, WILD4) else c
        ev = self._ev(e="play", s=seat, c=cid, col=self.color, left=len(self.hands[seat]))
        if len(self.hands[seat]) == 1:
            if uno:
                self._ev(e="uno", s=seat)
                if self.vulnerable == seat:
                    self.vulnerable = None
            else:
                self.vulnerable = seat           # catchable until the next player acts
        nxt = self._next(seat)
        if v == SKIP or (v == REVERSE and len(self.active) == 2):
            if v == REVERSE:
                self.direction *= -1
                self._ev(e="reverse", d=self.direction)
            self._ev(e="skip", s=nxt)
            nxt = self._next(nxt)
        elif v == REVERSE:
            self.direction *= -1
            self._ev(e="reverse", d=self.direction)
            nxt = self._next(seat)
        elif v in (DRAW2, WILD4):
            self._give(nxt, 2 if v == DRAW2 else 4, "draw2" if v == DRAW2 else "wild4")
            self._ev(e="skip", s=nxt)
            nxt = self._next(nxt)
        if not self.hands[seat]:
            self._end_round(seat)
            return ev
        self.turn = nxt
        return ev

    def draw(self, seat: int) -> int | None:
        self._check_turn(seat, ("play",))
        self._close_uno_window(seat)
        got = self._give(seat, 1, "draw")
        if got and self.playable(got[0], seat):
            self.phase, self.drawn = "drawn", got[0]
            return got[0]
        self._ev(e="pass", s=seat)
        self.turn = self._next(seat)
        return got[0] if got else None

    def keep(self, seat: int):
        """Pass after drawing a playable card."""
        self._check_turn(seat, ("drawn",))
        self.phase, self.drawn = "play", None
        self._ev(e="pass", s=seat)
        self.turn = self._next(seat)

    def choose_color(self, seat: int, color: int):
        """Only at the start of a round whose first discard is a Wild."""
        self._check_turn(seat, ("color",))
        if not 0 <= int(color) <= 3:
            raise UnoError("Unknown colour")
        self.color = int(color)
        self.phase = "play"
        self._ev(e="color", s=seat, col=self.color)

    def catch(self, seat: int) -> bool:
        """Anyone but the vulnerable player catches an uncalled UNO: two cards for them."""
        if self.phase == "over":
            raise UnoError("The round is over")
        v = self.vulnerable
        if v is None or v == seat or seat in self.out:
            raise UnoError("Nobody to catch")
        self.vulnerable = None
        self._ev(e="caught", s=v, by=seat)
        self._give(v, 2, "caught")
        return True

    def timeout(self) -> str:
        """The turn clock ran out: draw and pass, keep the drawn card, or pick a colour."""
        s = self.turn
        if self.phase == "over":
            return "over"
        self._ev(e="timeout", s=s)
        if self.phase == "color":
            counts = [0] * 4
            for h in self.hands[s]:
                if CARDS[h][0] >= 0:
                    counts[CARDS[h][0]] += 1
            self.choose_color(s, max(range(4), key=lambda c: (counts[c], -c)))
            return "color"
        if self.phase == "drawn":
            self.keep(s)
            return "keep"
        self._close_uno_window(s)
        self._give(s, 1, "draw")
        self._ev(e="pass", s=s)
        self.turn = self._next(s)
        return "draw"

    def _end_round(self, winner: int):
        pts = sum(points(c) for s, h in enumerate(self.hands) if s != winner for c in h)
        self.scores[winner] += pts
        self.winner, self.round_points, self.phase = winner, pts, "over"
        self.vulnerable = None
        self._ev(e="win", s=winner, pts=pts, scores=list(self.scores))

    def next_round(self, seed: int) -> "Uno":
        return Uno(self.n, seed, dealer=self.dealer + 1, scores=self.scores, round_no=self.round_no + 1,
                   out=self.out)

    def remove_seat(self, seat: int):
        """The player left the room: their cards go under the draw pile and their turns are
        skipped.  With one player left the round ends (no points)."""
        if seat in self.out:
            return
        was_turn = seat == self.turn
        self.out.add(seat)
        self.draw_pile[0:0] = self.hands[seat]
        self.hands[seat] = []
        if self.vulnerable == seat:
            self.vulnerable = None
        self._ev(e="left", s=seat)
        if self.phase == "over":
            return
        if len(self.active) < 2:
            self.winner, self.round_points, self.phase = self.active[0], 0, "over"
            self._ev(e="win", s=self.winner, pts=0, scores=list(self.scores))
            return
        if was_turn:
            self.phase, self.drawn = ("color" if self.phase == "color" else "play"), None
            self.turn = self._next(seat)

    # ------------------------------------------------------------------ views
    def public_event(self, e: dict, seat: int) -> dict:
        """An event as seat may see it (other players' drawn cards stay hidden)."""
        if e.get("e") == "draw" and e.get("s") != seat:
            e = {k: v for k, v in e.items() if k != "cards"}
        return e

    def view(self, seat: int) -> dict:
        """Everything seat may know: its own hand, the others' card counts, the table."""
        return dict(seat=seat, n=self.n, hand=sorted(self.hands[seat]), counts=[len(h) for h in self.hands],
                    top=self.top, color=self.color, dir=self.direction, turn=self.turn, phase=self.phase,
                    drawn=self.drawn if seat == self.turn else None, draw_n=len(self.draw_pile),
                    vuln=self.vulnerable, scores=list(self.scores), winner=self.winner,
                    pts=self.round_points, round=self.round_no, dealer=self.dealer,
                    legal=self.legal_cards(seat), out=sorted(self.out))
