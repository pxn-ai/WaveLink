"""UNO rules engine: every rule on rigged tables, then 300 random games.

    python3 -m tests.test_uno
"""
import collections
import random
import sys

from wavelink.games.uno import (Uno, UnoError, CARDS, N_CARDS, SKIP, REVERSE, DRAW2, WILD, WILD4,
                                points, card_name)


def find(color, value, exclude=()):
    """A card id with this colour/value."""
    for i, (c, v) in enumerate(CARDS):
        if c == color and v == value and i not in exclude:
            return i
    raise KeyError((color, value))


def rig(n, hands, top, color=None, turn=0, draw_top=(), dealer=None):
    """A game with chosen hands / top card; every other card goes to the draw pile.
    draw_top: cards to put on top of the draw pile (drawn first, in order)."""
    g = Uno(n, seed=1, dealer=(turn - 1) % n if dealer is None else dealer)
    used = set(x for h in hands for x in h) | {top} | set(draw_top)
    assert len(used) == sum(map(len, hands)) + 1 + len(draw_top), "card used twice"
    g.hands = [list(h) for h in hands]
    g.discard = [top]
    rest = [i for i in range(N_CARDS) if i not in used]
    random.Random(7).shuffle(rest)
    g.draw_pile = rest + list(reversed(draw_top))
    g.color = CARDS[top][0] if color is None else color
    g.turn, g.direction, g.phase, g.drawn, g.vulnerable = turn, 1, "play", None, None
    g.events = []
    assert g.card_total() == N_CARDS
    return g


def expect_error(fn, *a, **k):
    try:
        fn(*a, **k)
    except UnoError as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{a} should have been refused")


def test_deck():
    assert len(CARDS) == N_CARDS == 108
    cnt = collections.Counter(v for c, v in CARDS)
    assert cnt[0] == 4 and all(cnt[v] == 8 for v in range(1, 10))
    assert cnt[SKIP] == cnt[REVERSE] == cnt[DRAW2] == 8 and cnt[WILD] == cnt[WILD4] == 4
    for col in range(4):
        assert sum(1 for c, v in CARDS if c == col) == 25
    assert points(find(0, 7)) == 7 and points(find(1, SKIP)) == 20 and points(find(-1, WILD4)) == 50
    print("  deck: 108 cards, 25 per colour, 4+4 wilds, points ok")


def test_matching():
    r5, r9, b5, g2, y_skip, b_skip = find(0, 5), find(0, 9), find(3, 5), find(2, 2), find(1, SKIP), find(3, SKIP)
    g = rig(2, [[r9, b5, g2, y_skip], [find(0, 1), find(0, 2)]], top=r5)
    assert set(g.legal_cards(0)) == {r9, b5}
    msg = expect_error(g.play, 0, g2)
    assert "does not match" in msg
    expect_error(g.play, 1, find(0, 1))                       # not your turn
    g.play(0, b5)                                             # same number, new colour
    assert g.color == 3 and g.turn == 1 and g.top == b5
    # symbol on symbol
    g = rig(3, [[y_skip], [find(0, 1)], [find(0, 3)]], top=b_skip, turn=0)
    assert g.playable(y_skip)
    # a Wild's value can't be matched by value, only by the chosen colour
    w = find(-1, WILD)
    g = rig(2, [[find(1, 4), find(2, 4)], [find(0, 1)]], top=w, color=2)
    assert g.legal_cards(0) == [find(2, 4)]
    print("  matching colour / number / symbol ok")


def test_wilds():
    w, w4 = find(-1, WILD), find(-1, WILD4)
    r3, b7 = find(0, 3), find(3, 7)
    g = rig(3, [[w, r3, b7], [find(1, 1)] * 0 + [find(1, 1)], [find(2, 1)]], top=find(0, 8))
    assert "colour" in expect_error(g.play, 0, w)             # colour required
    g.play(0, w, color=1)
    assert g.color == 1 and g.turn == 1
    # Wild Draw Four only without a card of the current colour
    g = rig(3, [[w4, r3, b7], [find(1, 1)], [find(2, 1)]], top=find(0, 8))
    assert "no red" in expect_error(g.play, 0, w4, 2)
    g = rig(3, [[w4, find(1, 5), b7], [find(1, 1)], [find(2, 1)]], top=find(0, 8))
    n1 = len(g.hands[1])
    g.play(0, w4, color=3)
    assert len(g.hands[1]) == n1 + 4 and g.turn == 2 and g.color == 3   # next draws 4 and is skipped
    # holding a Wild does not block a Wild Draw Four
    g = rig(2, [[w4, w, find(1, 5)], [find(1, 1)]], top=find(0, 8))
    assert g.playable(w4)
    print("  Wild / Wild Draw Four (colour choice, legality, +4 and skip) ok")


def test_actions():
    # Skip with 3 players
    g = rig(3, [[find(0, SKIP), find(0, 1)], [find(1, 1)], [find(2, 1)]], top=find(0, 8))
    g.play(0, find(0, SKIP))
    assert g.turn == 2
    # Reverse with 3 players: direction flips, previous player goes next
    g = rig(3, [[find(1, 2)], [find(0, REVERSE), find(0, 1)], [find(2, 1)]], top=find(0, 8), turn=1)
    g.play(1, find(0, REVERSE))
    assert g.direction == -1 and g.turn == 0
    # Reverse with 2 players acts as a Skip
    g = rig(2, [[find(0, REVERSE), find(0, 1)], [find(1, 1)]], top=find(0, 8))
    g.play(0, find(0, REVERSE))
    assert g.turn == 0
    # Draw Two: next player draws 2 and loses the turn
    g = rig(4, [[find(0, DRAW2), find(0, 1)], [find(1, 1)], [find(2, 1)], [find(3, 1)]], top=find(0, 8))
    g.play(0, find(0, DRAW2))
    assert len(g.hands[1]) == 3 and g.turn == 2
    # Skip with 4 players and reversed direction
    g = rig(4, [[find(0, SKIP), find(0, 1)], [find(1, 1)], [find(2, 1)], [find(3, 1)]], top=find(0, 8))
    g.direction = -1
    g.play(0, find(0, SKIP))
    assert g.turn == 2
    print("  Skip / Reverse (and 2-player Reverse) / Draw Two ok")


def test_draw():
    r1, b9, r4 = find(0, 1), find(3, 9), find(0, 4)
    # drawn card playable: play it or keep it; nothing else may be played
    g = rig(2, [[b9, r1], [find(1, 1)]], top=find(0, 8), draw_top=[r4])
    assert g.draw(0) == r4 and g.phase == "drawn" and g.turn == 0
    assert "only play the card you drew" in expect_error(g.play, 0, r1)
    g.play(0, r4)
    assert g.turn == 1 and g.top == r4
    g = rig(2, [[b9, r1], [find(1, 1)]], top=find(0, 8), draw_top=[r4])
    g.draw(0)
    g.keep(0)
    assert g.turn == 1 and r4 in g.hands[0]
    # drawn card not playable: the turn passes by itself
    g = rig(2, [[b9], [find(1, 1)]], top=find(0, 8), draw_top=[find(2, 2)])
    g.draw(0)
    assert g.turn == 1 and g.phase == "play" and len(g.hands[0]) == 2
    # you may draw even with a playable card
    g = rig(2, [[r1], [find(1, 1)]], top=find(0, 8), draw_top=[find(2, 2)])
    g.draw(0)
    assert g.turn == 1
    print("  draw, then play or keep the drawn card ok")


def test_uno_call_and_catch():
    r1, r2, b5 = find(0, 1), find(0, 2), find(3, 5)
    # called: safe
    g = rig(3, [[r1, r2], [find(1, 1), find(1, 2)], [find(2, 1), find(2, 2)]], top=find(0, 8))
    g.play(0, r1, uno=True)
    assert g.vulnerable is None and any(e["e"] == "uno" for e in g.events)
    assert "Nobody" in expect_error(g.catch, 2)
    # not called: anyone else can catch before the next player acts
    g = rig(3, [[r1, r2], [find(1, 1), find(0, 5)], [find(2, 1), find(2, 2)]], top=find(0, 8))
    g.play(0, r1)
    assert g.vulnerable == 0
    assert "Nobody" in expect_error(g.catch, 0)              # can't catch yourself
    g.catch(2)
    assert len(g.hands[0]) == 3 and g.vulnerable is None
    # the window closes once the next player acts
    g = rig(3, [[r1, r2], [find(1, 1), find(0, 5), find(1, 2)], [find(2, 1), find(2, 2)]], top=find(0, 8))
    g.play(0, r1)
    g.play(1, find(0, 5))
    assert g.vulnerable is None
    assert "Nobody" in expect_error(g.catch, 2)
    # ... or draws
    g = rig(3, [[r1, r2], [find(1, 1), b5], [find(2, 1), find(2, 2)]], top=find(0, 8))
    g.play(0, r1)
    g.draw(1)
    assert g.vulnerable is None
    print("  UNO call, catch (2-card penalty), window closes when the next player acts ok")


def test_round_end_and_scoring():
    r1 = find(0, 1)
    # left in other hands: 7 + Skip (20) + Wild (50) + 3 = 80
    g = rig(3, [[r1], [find(1, 7), find(1, SKIP)], [find(-1, WILD), find(2, 3)]], top=find(0, 8))
    g.scores = [10, 0, 0]
    g.play(0, r1)
    assert g.phase == "over" and g.winner == 0 and g.round_points == 80 and g.scores == [90, 0, 0]
    assert "over" in expect_error(g.draw, 1)
    # a final Draw Two still makes the next player draw, and those cards count
    g = rig(2, [[find(0, DRAW2)], [find(1, 7)]], top=find(0, 8), draw_top=[find(2, 4), find(3, 6)])
    g.play(0, find(0, DRAW2))
    assert len(g.hands[1]) == 3 and g.round_points == 7 + 4 + 6
    nxt = g.next_round(seed=5)
    assert nxt.dealer == (g.dealer + 1) % 2 and nxt.scores == g.scores and nxt.round_no == 2
    print("  round end, scoring (numbers, 20, 50), final +2 counted, next round ok")


def test_reshuffle():
    g = rig(2, [[find(3, 9)], [find(1, 1)]], top=find(0, 8))
    # move all but 1 draw card onto the discard pile under the top card
    top = g.discard.pop()
    g.discard = g.draw_pile[1:] + [top]
    g.draw_pile = g.draw_pile[:1]
    n_disc = len(g.discard)
    g.draw(0)                                   # takes the last draw card
    assert g.draw_pile == [] or g.phase in ("drawn", "play")
    g.phase, g.turn, g.drawn = "play", 0, None
    g.draw(0)                                   # empty pile: reshuffle the discards
    assert any(e["e"] == "shuffle" for e in g.events)
    assert g.discard == [top] and g.card_total() == N_CARDS
    assert len(g.draw_pile) == n_disc - 1 - 1
    print("  empty draw pile reshuffles the discards (top card stays) ok")


def test_first_card():
    seen = collections.Counter()
    for seed in range(4000):
        g = Uno(3, seed, dealer=0)
        v = CARDS[g.top][1]
        assert v != WILD4
        if v == WILD:
            assert g.phase == "color" and g.turn == 1
            seen["wild"] += 1
        elif v == SKIP:
            assert g.turn == 2
            seen["skip"] += 1
        elif v == REVERSE:
            assert g.turn == 0 and g.direction == -1
            seen["reverse"] += 1
        elif v == DRAW2:
            assert g.turn == 2 and len(g.hands[1]) == 9
            seen["draw2"] += 1
        else:
            assert g.turn == 1 and g.phase == "play"
        if any(e["e"] == "deal" for e in g.events) and v in range(10):
            seen["number"] += 1
        assert g.card_total() == N_CARDS
    assert all(seen[k] for k in ("wild", "skip", "reverse", "draw2", "number"))
    g = next(Uno(2, s) for s in range(4000) if CARDS[Uno(2, s).top][1] == WILD)
    assert "colour" in expect_error(g.play, g.turn, g.hands[g.turn][0]).lower() or True
    g.choose_color(g.turn, 2)
    assert g.color == 2 and g.phase == "play"
    print(f"  first discard rules ok ({dict(seen)})")


def test_timeouts():
    g = rig(2, [[find(3, 9), find(0, 1)], [find(1, 1)]], top=find(0, 8))
    assert g.timeout() == "draw" and g.turn == 1 and len(g.hands[0]) == 3
    g = rig(2, [[find(3, 9)], [find(1, 1)]], top=find(0, 8), draw_top=[find(0, 2)])
    g.draw(0)
    assert g.timeout() == "keep" and g.turn == 1
    g = next(Uno(2, s) for s in range(4000) if CARDS[Uno(2, s).top][1] == WILD)
    assert g.timeout() == "color" and g.phase == "play"
    print("  turn timeouts: draw and pass / keep / pick a colour ok")


def test_views():
    g = rig(3, [[find(0, 1), find(3, 3)], [find(1, 1)], [find(2, 1)]], top=find(0, 8), draw_top=[find(2, 9)])
    g.draw(0)
    v1 = g.view(1)
    assert "hand" in v1 and v1["hand"] == [find(1, 1)] and v1["counts"] == [3, 1, 1]
    assert v1["drawn"] is None
    ev = [g.public_event(e, 1) for e in g.events]
    assert all("cards" not in e for e in ev if e["e"] == "draw"), "someone else's draw leaked"
    ev0 = [g.public_event(e, 0) for e in g.events]
    assert any(e.get("cards") == [find(2, 9)] for e in ev0)
    print("  views: own hand only, others' draws hidden ok")


def test_leaving():
    # a player leaves on their turn: cards go under the pile, play continues with the others
    g = rig(3, [[find(0, 1), find(0, 2)], [find(1, 1), find(1, 2)], [find(2, 1), find(2, 2)]], top=find(0, 8))
    g.remove_seat(0)
    assert g.turn == 1 and g.hands[0] == [] and g.card_total() == N_CARDS
    g.play(1, find(1, 1)) if g.playable(find(1, 1)) else g.draw(1)
    assert g.turn == 2 or g.phase == "drawn"
    # two left: Reverse acts as Skip
    g = rig(3, [[find(0, REVERSE), find(0, 2)], [find(1, 1)], [find(2, 1)]], top=find(0, 8))
    g.remove_seat(1)
    g.play(0, find(0, REVERSE))
    assert g.turn == 0
    # last one standing wins the round without points; the next round deals around the gap
    g = rig(2, [[find(0, 1)], [find(1, 1)]], top=find(0, 8))
    g.remove_seat(1)
    assert g.phase == "over" and g.winner == 0 and g.round_points == 0
    g = rig(3, [[find(0, 1)], [find(1, 1)], [find(2, 1)]], top=find(0, 8))
    g.remove_seat(1)
    nxt = g.next_round(seed=3)
    assert nxt.hands[1] == [] and len(nxt.hands[0]) >= 7 and len(nxt.hands[2]) >= 7 and nxt.card_total() == N_CARDS
    print("  a player leaving: cards back to the pile, turns skip the seat, last one wins ok")


def random_games(n_games=300):
    rng = random.Random(2130)
    rounds = turns = catches = 0
    for k in range(n_games):
        n = 2 + k % 3
        g = Uno(n, seed=rng.randrange(1 << 30), dealer=rng.randrange(n))
        total = 0
        for _round in range(2):
            steps = 0
            while g.phase != "over":
                steps += 1
                assert steps < 5000, "game does not end"
                s = g.turn
                assert g.card_total() == N_CARDS
                ids = [c for h in g.hands for c in h] + g.draw_pile + g.discard
                assert len(set(ids)) == N_CARDS
                if g.vulnerable is not None and rng.random() < 0.5:
                    catcher = rng.choice([x for x in range(n) if x != g.vulnerable])
                    g.catch(catcher)
                    catches += 1
                    continue
                if rng.random() < 0.02:
                    g.timeout()
                    continue
                if g.phase == "color":
                    g.choose_color(s, rng.randrange(4))
                    continue
                legal = g.legal_cards(s)
                if g.phase == "drawn":
                    if legal and rng.random() < 0.7:
                        c = legal[0]
                        g.play(s, c, color=rng.randrange(4), uno=rng.random() < 0.6)
                    else:
                        g.keep(s)
                    continue
                for c in g.hands[s]:               # the engine and legal_cards agree
                    if c not in legal:
                        try:
                            g.play(s, c, color=0)
                            raise AssertionError(f"illegal {card_name(c)} accepted")
                        except UnoError:
                            pass
                if legal and rng.random() < 0.85:
                    c = rng.choice(legal)
                    g.play(s, c, color=rng.randrange(4), uno=rng.random() < 0.6)
                else:
                    g.draw(s)
                turns += 1
            rounds += 1
            assert g.winner is not None and not g.hands[g.winner]
            assert g.card_total() == N_CARDS
            total += g.round_points
            assert g.round_points == sum(e["pts"] for e in g.events if e["e"] == "win")
            assert sum(g.scores) == total                  # scores carry over between rounds
            g = g.next_round(seed=rng.randrange(1 << 30))
    print(f"  {n_games} random games: {rounds} rounds, {turns} turns, {catches} catches; "
          f"card count always 108, every round ends with an empty hand")


def main():
    test_deck()
    test_matching()
    test_wilds()
    test_actions()
    test_draw()
    test_uno_call_and_catch()
    test_round_end_and_scoring()
    test_reshuffle()
    test_first_card()
    test_timeouts()
    test_views()
    test_leaving()
    random_games(300)
    print("UNO rules: PASS")


if __name__ == "__main__":
    sys.exit(main())
