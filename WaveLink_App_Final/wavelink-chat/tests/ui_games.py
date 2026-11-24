"""Plays the game center through the browser UI on every station: a UNO round (and the deal
of the next one), then a race and a rematch.  Needs Playwright with Chromium.

    python3 -m wavelink sim --nodes 3 &
    python3 -m tests.ui_games 3                # screenshots in build/ui-shots/

Every station's page is driven with the keyboard, the way a player would: in UNO a small bot
picks a legal card with the arrow keys and presses Enter (U to call UNO on the second-to-last
card, C to catch, D to draw, P to keep, 1-4 for a colour).  In the race an autopilot holds the
arrow keys (dispatched as key events) to follow the track and presses Space to use pickups.
"""
import argparse
import asyncio
import base64
import os
import random
import sys
import time

from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build", "ui-shots")
NAMES = ["Alice", "Bob", "Carol", "Dave"]
WILD = range(100, 108)

AUTOPILOT = """
(() => {
  clearInterval(window.__auto);
  const held = {};
  const key = (k, on) => { if (!!held[k] === on) return; held[k] = on;
    document.dispatchEvent(new KeyboardEvent(on ? "keydown" : "keyup", { key: k, bubbles: true })); };
  window.__auto = setInterval(() => {
    if (!RACE.root || !RACE.me || !RACE.T) return;
    const T = RACE.T, c = RACE.me, sp = Math.hypot(c.vx, c.vy);
    const look = Math.round((70 + sp * 0.3) / 8), i = (c.i + look) % T.M;
    const [tx, ty] = RACE.at(T, i, 0);
    let err = Math.atan2(ty - c.y, tx - c.x) - c.h;
    while (err > Math.PI) err -= 2 * Math.PI; while (err < -Math.PI) err += 2 * Math.PI;
    let curv = 0;
    for (let k = 4; k < 44; k += 2) curv = Math.max(curv, Math.abs(T.curv[(c.i + k) % T.M]));
    const vmax = Math.min(860, 2.15 / Math.max(curv, 1e-4));
    key("ArrowLeft", err < -0.05); key("ArrowRight", err > 0.05);
    key("ArrowUp", sp < vmax); key("ArrowDown", sp > vmax + 90);
    if (c.item && RACE.tg() > 2 && Math.random() < 0.04) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keyup", { key: " ", bubbles: true }));
    }
  }, 25);
})();
"""


async def shot(pg, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    try:
        cdp = getattr(pg, "_cdp", None)
        if cdp is None:
            cdp = await pg.context.new_cdp_session(pg)
            pg._cdp = cdp
        r = await cdp.send("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
    except Exception:
        await pg.screenshot(path=path, animations="allow")


async def wait_for(pg, js, timeout=30.0, what=""):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if await pg.evaluate(js):
            return True
        await asyncio.sleep(0.15)
    raise AssertionError(f"timed out waiting for {what or js}")


async def make_room(pages, game, laps=2, biome=255):
    host, guests = pages[0], pages[1:]
    for pg in pages:
        if not await pg.evaluate("GC.open"):
            await pg.click("#gcBtn")
    await wait_for(host, f"!!document.querySelector('.poster.p-{game} .pick input')", 20, "player list")
    for k in range(1, len(pages)):
        await host.click(f".poster.p-{game} .pick label:has-text('{NAMES[k]}')")
    if game == "race":
        await host.click(f".poster.p-race .biomes button[data-biome='{biome}']")
        await host.click(f".poster.p-race .laps button[data-laps='{laps}']")
    await host.click(f".poster.p-{game} .go")
    for pg in guests:
        await wait_for(pg, "!!document.querySelector('.invite .yes')", 20, "invitation")
        await pg.click(".invite .yes")
    rid = await host.evaluate("GC.focus")
    return rid


async def uno_bot(pg, stop, rng, stats):
    last = None
    while not stop.is_set():
        v = await pg.evaluate("""UNO.view && UNO.el ? {v: UNO.view.v, turn: UNO.view.turn, seat: UNO.view.seat,
            phase: UNO.view.phase, hand: UNO.view.hand, legal: UNO.view.legal, drawn: UNO.view.drawn,
            vuln: UNO.view.vuln, sel: UNO.sel, pending: UNO.pendingV, picker: UNO.picker} : null""")
        if not v or v["phase"] == "over":
            await asyncio.sleep(0.2)
            continue
        if v["vuln"] is not None and v["vuln"] != v["seat"] and rng.random() < 0.4:
            await pg.keyboard.press("c")
            stats["catch_keys"] += 1
            await asyncio.sleep(0.2)
            continue
        if v["turn"] != v["seat"] or v["v"] == last or v["pending"] == v["v"]:
            await asyncio.sleep(0.08)
            continue
        last = v["v"]
        if v["phase"] == "color":
            await pg.keyboard.press(str(1 + rng.randrange(4)))
        elif v["phase"] == "drawn":
            if v["legal"]:
                await pg.keyboard.press("Enter")
                if v["drawn"] in WILD:
                    await asyncio.sleep(0.1)
                    await pg.keyboard.press(rng.choice("rygb"))
            else:
                await pg.keyboard.press("p")
        elif v["legal"]:
            c = rng.choice(v["legal"])
            target, n = v["hand"].index(c), len(v["hand"])
            for _ in range((target - v["sel"]) % n):
                await pg.keyboard.press("ArrowRight")
            if n == 2 and rng.random() < 0.75:
                await pg.keyboard.press("u")              # call UNO with the second-to-last card
            await pg.keyboard.press("Enter")
            if c in WILD:
                await asyncio.sleep(0.1)
                await pg.keyboard.press(str(1 + rng.randrange(4)))
            stats["plays"] += 1
        else:
            await pg.keyboard.press("d")
            stats["draws"] += 1
        await asyncio.sleep(0.12)


async def play_uno(pages, n):
    rid = await make_room(pages, "uno")
    host = pages[0]
    await wait_for(host, "!!document.querySelector('.lobby .primary:not([disabled])')", 20, "UNO lobby ready")
    await shot(host, f"uno-{n}p-lobby.png")
    await host.click(".lobby .primary")
    for pg in pages:
        await wait_for(pg, "!!(UNO.view && UNO.el)", 20, "UNO table")
    await asyncio.sleep(0.6)
    for k, pg in enumerate(pages):
        await shot(pg, f"uno-{n}p-{NAMES[k].lower()}-start.png")
    stop = asyncio.Event()
    rng = random.Random(7)
    stats = {"plays": 0, "draws": 0, "catch_keys": 0}
    bots = [asyncio.create_task(uno_bot(pg, stop, rng, stats)) for pg in pages]
    t0 = time.time()
    mid_done = False
    while time.time() - t0 < 600:
        await asyncio.sleep(0.5)
        if not mid_done and stats["plays"] >= 12:
            for k, pg in enumerate(pages):
                await shot(pg, f"uno-{n}p-{NAMES[k].lower()}-mid.png")
            mid_done = True
        phases = [await pg.evaluate("UNO.view && UNO.view.phase") for pg in pages]
        if all(p == "over" for p in phases):
            break
    stop.set()
    await asyncio.gather(*bots)
    await asyncio.sleep(0.8)
    winner = await host.evaluate("UNO.view.winner")
    scores = await host.evaluate("UNO.view.scores")
    for k, pg in enumerate(pages):
        await shot(pg, f"uno-{n}p-{NAMES[k].lower()}-end.png")
    print(f"UNO: round over after {time.time() - t0:.0f} s, {stats['plays']} plays, {stats['draws']} draws, "
          f"winner seat {winner}, scores {scores}")
    # the host deals the next round with N
    await host.keyboard.press("n")
    for pg in pages:
        await wait_for(pg, "UNO.view && UNO.view.round === 2 && UNO.view.phase !== 'over'", 20, "round 2 dealt")
    print("UNO: round 2 dealt on every station")
    await host.click("#unoLeave")                          # the host closes the room
    for pg in pages[1:]:
        await wait_for(pg, f"(GC.room('{rid}') || {{}}).state === 'closed'", 20, "room closed on guests")
    for pg in pages:
        await pg.evaluate(f"GC.post({{a: 'dismiss', r: '{rid}'}}).catch(() => 0); GC.focus = null; GC.render()")
    return True


async def play_race(pages, n, laps=2, rematch=True, biome=1):
    rid = await make_room(pages, "race", laps, biome=biome)
    host = pages[0]
    await wait_for(
        host,
        f"(() => {{ const b = document.querySelector('.lobby .primary:not([disabled])'); return !!(b && b.textContent.includes('{len(pages)} players')); }})()",
        40,
        "race lobby synced",
    )
    title_ok = await host.evaluate("document.querySelector('.lobby.race h3')?.textContent.includes('Slot Rush')")
    assert title_ok, "Lobby title is not Slot Rush"
    await shot(host, f"race-{n}p-lobby.png")
    for rnd in range(2 if rematch else 1):
        await host.click(".lobby .primary" if rnd == 0 else ".race-results .primary")
        for pg in pages:
            await wait_for(pg, f"RACE.root && RACE.rid === '{rid}' && RACE.round === {rnd + 1}", 20, "race view")
        await asyncio.sleep(1.0)
        hud_ok = await host.evaluate("document.querySelector('.race-room .gc-title')?.textContent.includes('Slot Rush')")
        assert hud_ok, "Race HUD header does not show Slot Rush"
        if rnd == 0:
            await shot(pages[-1], f"race-{n}p-grid.png")
        if rnd == 1:                                       # the rematch with the chase camera (V)
            for pg in pages:
                await pg.keyboard.press("v")
            assert all([await pg.evaluate("pref('raceCam') === 'chase'") for pg in pages]), "V did not switch the camera"
        for pg in pages:
            await pg.evaluate(AUTOPILOT)
        await wait_for(host, "RACE.tg() > 9", 30, "race running")
        if rnd == 1:
            assert all([await pg.evaluate("RACE.chaseOn && Math.abs(RACE.view.rot) > 0") for pg in pages]), "chase camera not drawn"
        for k, pg in enumerate(pages):
            await shot(pg, f"race-{n}p-{NAMES[k].lower()}-r{rnd + 1}.png")
        if rnd == 0:                                       # chat from the race: the race goes on
            g = pages[-1]
            tg0 = await g.evaluate("RACE.tg()")
            heard0 = await host.evaluate(f"RACE.cars[{n - 1}].rx.length ? RACE.cars[{n - 1}].rx[RACE.cars[{n - 1}].rx.length - 1] : 0")
            await g.click(".gc-chatbtn")
            await g.click("#gccTabs button[data-chat='u:1']")
            await g.type("#gccText", "wasd and space go here, not to my car")
            await g.press("#gccText", "Enter")
            await wait_for(host, "[...S.msgs.values()].some((m) => m.text === 'wasd and space go here, not to my car')", 30, "chat from the race")
            await shot(g, f"race-{n}p-chat.png")
            running = await wait_for(g, f"!!RACE.root && RACE.tg() > {tg0} + 0.5", 5, "race advancing during chat")
            heard = await host.evaluate(f"RACE.cars[{n - 1}].rx[RACE.cars[{n - 1}].rx.length - 1] > {heard0}")
            await g.press("#gccText", "Escape")
            print(f"race 1: chat sent from the race; the race kept running {running}, the others kept hearing the car {heard}")
            assert running and heard, "chatting stopped the race"
            # the radio dashboard over the race: the race goes on under it
            tg1 = await g.evaluate("RACE.tg()")
            await g.click(".race-room .gc-radiobtn")
            await wait_for(g, "!$('#dash').hidden", 5, "radio dashboard in the race")
            await asyncio.sleep(2.0)
            await shot(g, f"race-{n}p-radio.png")
            radio = await g.evaluate(f"!!RACE.root && RACE.tg() > {tg1} + 1 && $('#tiles').children.length >= 10 "
                                     "&& getComputedStyle($('#dash')).visibility === 'visible'")
            await g.click(".race-room .gc-radiobtn")
            print(f"race 1: the radio dashboard opened over the race, which kept running: {radio}")
            assert radio and await g.evaluate("$('#dash').hidden"), "radio dashboard in the race"
            # a page reload mid-race: the car carries on from where it was, not from the grid
            await g.evaluate(AUTOPILOT)
            await wait_for(g, "RACE.me.d > 600", 30, "the car under way")
            d0 = await g.evaluate("RACE.me.d")
            await g.reload()
            await wait_for(g, f"typeof RACE !== 'undefined' && RACE.root && RACE.rid === '{rid}' && RACE.me", 30, "race after reload")
            d1 = await g.evaluate("RACE.me.d")
            print(f"race 1: page reloaded at {d0:.0f} px raced; it resumed at {d1:.0f} px")
            assert d1 > d0 - 50, "a reloaded page put the car back on the grid"
            await g.evaluate(AUTOPILOT)
        await asyncio.sleep(12)
        for pg in pages:
            await wait_for(pg, f"RACE.cars.filter((c) => c !== RACE.me && c.pkt).length >= {n - 1}", 20, "other cars seen")
        fps = [await pg.evaluate("RACE.fps || 0") for pg in pages]
        draw_ms = [await pg.evaluate("Math.round(RACE.drawMs * 10) / 10") for pg in pages]
        seen = [await pg.evaluate("RACE.cars.filter((c) => c !== RACE.me && c.pkt).length") for pg in pages]
        rates = [await pg.evaluate("""RACE.cars.filter((c) => c !== RACE.me && c.rx.length > 1)
              .map((c) => +((c.rx.length - 1) / ((c.rx[c.rx.length - 1] - c.rx[0]) / 1000)).toFixed(1))""") for pg in pages]
        off = [await pg.evaluate("RACE.dbg ? [RACE.dbg.off, RACE.dbg.n, Math.round(RACE.dbg.worst)] : null") for pg in pages]
        corr = [await pg.evaluate("""(() => { const c = (RACE.dbg && RACE.dbg.corr || []).slice().sort((a, b) => a - b);
            return c.length ? [Math.round(c[c.length >> 1]), Math.round(c[Math.floor(c.length * 0.95)]), RACE.dbg.snaps || 0] : null; })()""") for pg in pages]
        print(f"race {rnd + 1}: remote cars drawn beyond the rails [frames, of frames, worst px] {off}; "
              f"corrections along the track [median px, 95 % px, jumps] {corr}")
        assert all(o is None or o[0] == 0 for o in off), "a remote car was drawn beyond the rails"
        print(f"race {rnd + 1} ({'chase' if rnd else 'north-up'} camera): frames/s per page {fps} (draw {draw_ms} ms); other cars seen {seen}; "
              f"updates/s heard {rates}")
        assert all(s == n - 1 for s in seen), "a page does not see every other car"
        for pg in pages:
            await wait_for(pg, "!!document.querySelector('.race-results:not([hidden])')", 240, "results")
        res = await host.evaluate("RACE.results")
        print("race results: " + ", ".join(f"{r['name']} {r['ft'] / 1000 if r['ft'] else 'DNF'}" for r in res))
        for k, pg in enumerate(pages):
            await shot(pg, f"race-{n}p-{NAMES[k].lower()}-results{rnd + 1}.png")
        for pg in pages:
            await pg.evaluate("clearInterval(window.__auto)")
    for pg in pages:
        await pg.evaluate("setPref('raceCam', 'north')")
    await host.click(".race-results .gc-ghost")             # close the room
    for pg in pages[1:]:
        await wait_for(pg, f"(GC.room('{rid}') || {{}}).state === 'closed'", 20, "race room closed on guests")
    for pg in pages:
        await pg.evaluate(f"GC.post({{a: 'dismiss', r: '{rid}'}}).catch(() => 0).then(() => {{ GC.focus = null; GC.unmount(); GC.render(); }})")
    return True


async def verify_biomes(pages):
    host = pages[0]
    for pg in pages:
        await pg.evaluate("""(async () => {
          for (const r of (GC.snap?.rooms || [])) {
            if (r.state === 'closed') try { await GC.post({a: 'dismiss', r: r.r}); } catch (e) {}
          }
          GC.focus = null; GC.unmount(); GC.render();
        })()""")
    for bm in (0, 2, 3, 4):
        rid = await make_room(pages, "race", laps=2, biome=bm)
        await wait_for(
            host,
            f"(() => {{ const b = document.querySelector('.lobby .primary:not([disabled])'); return !!(b && b.textContent.includes('{len(pages)} players')); }})()",
            40,
            f"biome {bm} lobby ready",
        )
        await host.click(".lobby .primary")
        for pg in pages:
            await wait_for(pg, f"RACE.root && RACE.rid === '{rid}' && RACE.biome === {bm}", 40, f"biome {bm} race mounted")
        bname = await host.evaluate("RACE.B.name")
        hud_ok = await host.evaluate("document.querySelector('.race-room .gc-title')?.textContent") or ""
        assert "Slot Rush" in hud_ok and bname in hud_ok, f"HUD header missing Slot Rush / {bname}: {hud_ok}"
        for pg in pages:
            await pg.evaluate(AUTOPILOT)
        await wait_for(host, "RACE.tg() > 1.5", 30, f"biome {bm} green light")
        await host.evaluate("setPref('raceCam', 'north')")
        await asyncio.sleep(0.25)
        await shot(host, f"race-biome{bm}-north.png")
        await host.keyboard.press("v")
        await wait_for(host, "RACE.chaseOn", 5, f"chase camera active in biome {bm}")
        await asyncio.sleep(0.35)
        await shot(host, f"race-biome{bm}-chase.png")
        await host.keyboard.press("v")
        await wait_for(host, "pref('raceCam') === 'bird' && RACE.birdOn && RACE.miniCv.hidden", 5,
                       f"bird's-eye camera active in biome {bm}")
        await asyncio.sleep(0.25)
        if bm == 0:
            await shot(host, "race-biome0-bird.png")
        await host.keyboard.press("v")
        await wait_for(host, "pref('raceCam') === 'north' && !RACE.miniCv.hidden", 5,
                       f"north-up camera restored in biome {bm}")
        for pg in pages:
            await pg.evaluate("clearInterval(window.__auto); setPref('raceCam', 'north')")
        await host.evaluate(f"GC.post({{a: 'leave', r: '{rid}'}})")
        for pg in pages[1:]:
            await wait_for(pg, f"(GC.room('{rid}') || {{}}).state === 'closed'", 20, f"biome {bm} closed on guests")
        for pg in pages:
            await pg.evaluate(f"GC.post({{a: 'dismiss', r: '{rid}'}}).catch(() => 0).then(() => {{ GC.focus = null; GC.unmount(); GC.render(); }})")
    print("Slot Rush biome QA: verified biomes 0, 2, 3, 4 (plus 1 in play_race) across north-up, chase, and bird's-eye")
    return True


async def verify_spectator(pages):
    """Test Live Now discovery + read-only UNO & Slot Rush spectator UX on a non-playing station."""
    if len(pages) < 3:
        return True
    p0, p1, spec = pages[0], pages[1], pages[2]
    for pg in pages:
        await pg.evaluate("""(async () => {
          for (const r of (GC.snap?.rooms || [])) {
            if (r.state === 'closed') try { await GC.post({a: 'dismiss', r: r.r}); } catch (e) {}
          }
          GC.focus = null; GC.unmount();
          if (GC.snap?.watching) try { await GC.post({a: 'unwatch'}); } catch (e) {}
          if (!GC.open) GC.show(true); else GC.render();
        })()""")

    # UNO Spectator
    rid_u = await make_room([p0, p1], "uno")
    await wait_for(p0, "!!document.querySelector('.lobby .primary:not([disabled])')", 20, "2p UNO lobby ready")
    await p0.click(".lobby .primary")
    for pg in (p0, p1):
        await wait_for(pg, "!!(UNO.view && UNO.el)", 20, "2p UNO table")
    await wait_for(spec, f"!!document.querySelector(`.live-row .watch[data-r='{rid_u}']`)", 15, "Live Now UNO row")
    await shot(spec, "live-now-home.png")
    await spec.click(f".live-row .watch[data-r='{rid_u}']")
    await wait_for(spec, "!!document.querySelector('.uno-room.is-spec') && !!UNO.pub && UNO.view === null", 20,
                   "UNO spectator table")
    uno_dom = await spec.evaluate("""(() => ({
      badge: document.querySelector('.uno-room .spec-badge')?.textContent,
      hands: document.querySelectorAll('.uno-hand').length,
      btns: document.querySelectorAll('.uno-bar .uno-btn').length,
      backs: document.querySelectorAll('.seat .card.cback').length,
      hasPriv: 'hand' in UNO.pub || 'hands' in UNO.pub || 'deck' in UNO.pub || 'draw_pile' in UNO.pub || 'hs' in UNO.pub
    }))()""")
    assert uno_dom["badge"] == "SPECTATING" and uno_dom["hands"] == 0 and uno_dom["btns"] == 0 \
        and uno_dom["backs"] >= 14 and not uno_dom["hasPriv"], f"UNO spectator UI check failed: {uno_dom}"
    await shot(spec, "uno-spectator.png")
    await spec.click("#unoLeave")
    await wait_for(spec, "GC.watching() === null && !!document.querySelector('.gc-home')", 10, "UNO unwatch")
    await p0.evaluate(f"GC.post({{a: 'leave', r: '{rid_u}'}})")
    await wait_for(p1, f"(GC.room('{rid_u}') || {{}}).state === 'closed'", 15, "2p UNO closed")
    for pg in (p0, p1):
        await pg.evaluate(f"GC.post({{a: 'dismiss', r: '{rid_u}'}}).catch(() => 0).then(() => {{ GC.focus = null; GC.unmount(); GC.render(); }})")

    # Slot Rush Spectator
    rid_r = await make_room([p0, p1], "race", laps=2, biome=0)
    await wait_for(p0, "(() => { const b = document.querySelector('.lobby .primary:not([disabled])'); return !!(b && b.textContent.includes('2 players')); })()", 40, "2p race lobby synced")
    await p0.click(".lobby .primary")
    for pg in (p0, p1):
        await wait_for(pg, f"RACE.root && RACE.rid === '{rid_r}'", 20, "2p race view")
        await pg.evaluate(AUTOPILOT)
    await wait_for(spec, f"!!document.querySelector(`.live-row .watch[data-r='{rid_r}']`)", 15, "Live Now Race row")
    await spec.click(f".live-row .watch[data-r='{rid_r}']")
    await wait_for(spec, "!!document.querySelector('.race-room.is-spec') && RACE.spec && RACE.me === null && !!RACE.view",
                   20, "Slot Rush spectator view")
    await wait_for(p0, "RACE.tg() > 1.5", 30, "2p race started")
    await asyncio.sleep(0.6)
    await shot(spec, "race-spectator-bird.png")
    # Chat overlay open & typing stays isolated from spectator shortcuts
    await spec.click(".gc-chatbtn")
    await spec.click("#gccTabs button[data-chat='u:1']")
    await spec.type("#gccText", "watching v f 1 2 + -")
    await spec.press("#gccText", "Escape")
    assert await spec.evaluate("RACE.specCam.follow === null && !RACE.specCam.autoChase"), "Chat typing leaked to spectator camera"
    # Zoom, Follow, Auto-Chase, Full Track reset
    await spec.keyboard.press("=")
    await asyncio.sleep(0.15)
    await spec.keyboard.press("1")
    await wait_for(spec, "RACE.specCam.follow === 0", 5, "Spectator follow player 1")
    await spec.keyboard.press("v")
    await wait_for(spec, "RACE.specCam.autoChase && Math.abs(RACE.view.rot) > 0", 5, "Spectator auto-chase")
    await asyncio.sleep(0.25)
    await shot(spec, "race-spectator-chase.png")
    await spec.keyboard.press("f")
    await wait_for(spec, "RACE.specCam.follow === null && !RACE.specCam.autoChase", 5, "Spectator Full Track reset")
    await spec.click("#raceLeave")
    await wait_for(spec, "GC.watching() === null && !!document.querySelector('.gc-home')", 10, "Race unwatch")
    for pg in (p0, p1):
        await pg.evaluate("clearInterval(window.__auto)")
    await p0.evaluate(f"GC.post({{a: 'leave', r: '{rid_r}'}})")
    await wait_for(p1, f"(GC.room('{rid_r}') || {{}}).state === 'closed'", 15, "2p race closed")
    for pg in (p0, p1):
        await pg.evaluate(f"GC.post({{a: 'dismiss', r: '{rid_r}'}}).catch(() => 0).then(() => {{ GC.focus = null; GC.unmount(); GC.render(); }})")
    print("Spectator UI QA: verified Live Now discovery, UNO spectator zero-leak table, Slot Rush interactive Bird's-Eye/Follow/Auto-Chase/Chat isolation")
    return True


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int, nargs="?", default=3)
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--only", choices=["uno", "race"], default=None)
    ap.add_argument("--size", default="1440x900")
    a = ap.parse_args()
    w, h = map(int, a.size.split("x"))
    async with async_playwright() as p:
        b = await p.chromium.launch(args=[
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ])
        pages, errors = [], []
        for k in range(a.n):
            pg = await b.new_page(viewport={"width": w, "height": h})
            pg.on("pageerror", lambda e, k=k: errors.append(f"{NAMES[k]}: {e}"))
            await pg.goto(f"http://localhost:{a.port + k}/")
            pages.append(pg)
        for pg in pages:
            await wait_for(pg, f"S.contacts.size >= {a.n - 1} && [...S.contacts.values()].filter((c) => c.online).length >= {a.n - 1}",
                           40, "stations online")
        if a.only in (None, "uno"):
            await play_uno(pages, a.n)
        if a.only in (None, "race"):
            await play_race(pages, a.n)
            await verify_biomes(pages)
            if a.only is None:
                await verify_spectator(pages)
        await b.close()
    if errors:
        print("page errors:", *errors[:10], sep="\n  ")
        return 1
    print(f"UI game test: PASS (screenshots in {OUT})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
