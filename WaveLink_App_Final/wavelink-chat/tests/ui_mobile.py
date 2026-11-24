"""Mobile & Desktop regression UI checks (Playwright, radioconda env).

Starts or attaches to a simulated 3-station network and verifies:
  1. Desktop (1280x800, non-touch): mobile shell lock inactive, touch overlays
     hidden (display: none), keyboard hints visible.
  2. Mobile Portrait (390x844) & Landscape (844x390) with has_touch=True, is_mobile=True:
     - Chat header & composer alignment during scroll and simulated visualViewport
       keyboard resize, >=16px inputs, Wallpaper Editor pinch-to-zoom preserved.
     - Slot Rush active touch controls (>=44px, multi-touch steer slide + gas +
       auto-gas + pickup + camera cycle, auto-hide & input reset on Chat/Radio open).
     - Slot Rush spectator touch pan/pinch-zoom, leaderboard tap follow, >=44px bar.
     - UNO active touch hand swipe, tap-to-select + tap-again-to-play, >=44px bar,
       hidden <kbd> hints, Wild picker grid, and 3-seat spectator layout fit.

    python3 -m wavelink sim --nodes 3 &
    python3 -m tests.ui_mobile
"""
import argparse
import asyncio
import base64
import io
import os
import sys
import time

from PIL import Image
from playwright.async_api import async_playwright

NAMES = ["Alice", "Bob", "Carol"]


async def wait_for(pg, js, timeout=25.0, what=""):
    t_end = time.time() + timeout
    while time.time() < t_end:
        if await pg.evaluate(js):
            return True
        await asyncio.sleep(0.15)
    raise AssertionError(f"timed out waiting for {what or js}")


async def make_room(pages, game, laps=2, biome=0):
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
    return await host.evaluate("GC.focus")


async def cleanup_gc(pages):
    for pg in pages:
        await pg.evaluate("""(async () => {
          for (const r of (GC.snap?.rooms || [])) {
            if (r.state === 'playing' || r.state === 'lobby') {
              try { await GC.post({a: 'leave', r: r.r}); } catch (e) {}
            }
          }
          if (GC.snap?.watching) try { await GC.post({a: 'unwatch'}); } catch (e) {}
        })()""")
    await asyncio.sleep(0.5)
    for pg in pages:
        await pg.evaluate("""(async () => {
          for (const r of (GC.snap?.rooms || [])) {
            try { await GC.post({a: 'dismiss', r: r.r}); } catch (e) {}
          }
          GC.focus = null; GC.unmount();
          if (GC.open) GC.show(false);
          if (S.dashOpen) showDash(false);
          if (typeof GCCHAT !== 'undefined' && GCCHAT.open) GCCHAT.close();
        })()""")


async def check_desktop_guard(browser, port):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 800}, has_touch=False, is_mobile=False)
    p0 = await ctx.new_page()
    p1 = await ctx.new_page()
    errs = []
    p0.on("pageerror", lambda e: errs.append(str(e)))
    p1.on("pageerror", lambda e: errs.append(str(e)))
    await p0.goto(f"http://localhost:{port}/")
    await p1.goto(f"http://localhost:{port + 1}/")
    await wait_for(p0, "S.contacts.size >= 1 && [...S.contacts.values()].some((c) => c.online)", 25, "desktop online")

    desk_css = await p0.evaluate("""(() => ({
      htmlPos: getComputedStyle(document.documentElement).position,
      bodyPos: getComputedStyle(document.body).position,
      isTouch: isTouchMobile(),
      appHeightVar: document.documentElement.style.getPropertyValue('--app-height'),
    }))()""")
    assert desk_css["htmlPos"] != "fixed" and desk_css["bodyPos"] != "fixed" and not desk_css["isTouch"] \
        and desk_css["appHeightVar"] == "", f"Desktop shell polluted by mobile rules: {desk_css}"

    rid = await make_room([p0, p1], "race", laps=2, biome=0)
    await wait_for(p0, "!!document.querySelector('.lobby .primary:not([disabled])')", 25, "desktop race lobby ready")
    await p0.click(".lobby .primary")
    await wait_for(p0, "RACE.root && RACE.rid === '" + rid + "' && !RACE.spec", 20, "desktop race mounted")
    rt_style = await p0.evaluate("""(() => ({
      touchDisp: getComputedStyle(document.querySelector('.race-touch')).display,
      camDisp: getComputedStyle(document.querySelector('#rtCam')).display,
      kbdDisp: getComputedStyle(document.querySelector('#raceCam kbd')).display,
    }))()""")
    assert rt_style["touchDisp"] == "none" and rt_style["camDisp"] == "none" and rt_style["kbdDisp"] != "none", \
        f"Desktop race touch overlay leaked: {rt_style}"

    await cleanup_gc([p0, p1])
    await ctx.close()
    assert not errs, f"Desktop page errors: {errs}"
    print("  ok   desktop non-touch guard: shell unlocked, touch overlays hidden (display: none), <kbd> shown")


async def check_mobile_chat_and_walledit(pg, width, height, label):
    # Verify viewport meta + shell lock + zero horizontal overflow
    meta = await pg.evaluate("""(() => ({
      meta: document.querySelector('meta[name="viewport"]')?.content || '',
      htmlPos: getComputedStyle(document.documentElement).position,
      bodyPos: getComputedStyle(document.body).position,
      bodyOverscroll: getComputedStyle(document.body).overscrollBehavior,
      sw: document.documentElement.scrollWidth,
      cw: document.documentElement.clientWidth,
      appHVar: document.documentElement.style.getPropertyValue('--app-height'),
    }))()""")
    assert "maximum-scale=1" in meta["meta"] and "user-scalable=no" in meta["meta"] and "viewport-fit=cover" in meta["meta"], \
        f"{label} bad viewport meta: {meta}"
    assert meta["htmlPos"] == "fixed" and meta["bodyPos"] == "fixed" and meta["bodyOverscroll"] == "none", \
        f"{label} shell not locked: {meta}"
    assert meta["sw"] <= meta["cw"], f"{label} horizontal overflow on list: {meta}"

    # Open Everyone chat, pump enough messages to scroll, check >= 16px composer font + pinned header/composer
    await pg.click("#chats li")
    await wait_for(pg, "S.cur === 'g:all' && !$('#conv').hidden", 10, "chat open")
    await pg.evaluate("""(() => {
      const box = $('#msgs');
      for (let i = 0; i < 30; i++) {
        const d = document.createElement('div');
        d.className = 'msg';
        d.innerHTML = `<div class="txt">Test scroll line ${i} for mobile layout alignment</div>`;
        box.appendChild(d);
      }
      box.scrollTop = 0;
    })()""")
    await asyncio.sleep(0.08)
    await pg.evaluate("$('#msgs').scrollTop = 400")
    await asyncio.sleep(0.08)
    chat_metrics = await pg.evaluate("""(() => {
      const head = $('.convhead').getBoundingClientRect();
      const comp = $('.composer').getBoundingClientRect();
      const msgs = $('#msgs');
      const txtCs = getComputedStyle($('#text'));
      return {
        fontPx: parseFloat(txtCs.fontSize),
        headTop: head.top, headBot: head.bottom,
        compBot: comp.bottom, compLeft: comp.left, compRight: comp.right,
        winH: window.innerHeight, winW: window.innerWidth,
        scrollY: window.scrollY,
        msgsTouchAction: getComputedStyle(msgs).touchAction,
        msgsOverscrollY: getComputedStyle(msgs).overscrollBehaviorY,
        sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
      };
    })()""")
    assert chat_metrics["fontPx"] >= 16, f"{label} #text font-size < 16px: {chat_metrics}"
    assert 0 <= chat_metrics["headTop"] <= 2, f"{label} convhead drifted: {chat_metrics}"
    assert abs(chat_metrics["compBot"] - height) <= 2, f"{label} composer bottom misaligned: {chat_metrics}"
    assert chat_metrics["compRight"] <= width + 1 and chat_metrics["sw"] <= chat_metrics["cw"], \
        f"{label} horizontal overflow in chat composer: {chat_metrics}"
    assert chat_metrics["msgsTouchAction"] == "pan-y" and chat_metrics["msgsOverscrollY"] == "contain", \
        f"{label} #msgs scroll isolation missing: {chat_metrics}"

    # Simulate visualViewport resize (virtual keyboard opens) + scroll drift
    await pg.evaluate("""(() => {
      $('#msgs').scrollTop = $('#msgs').scrollHeight;
      const vv = window.visualViewport;
      const kbH = Math.max(200, Math.round(window.innerHeight * 0.52));
      Object.defineProperty(vv, 'height', { configurable: true, get: () => kbH });
      Object.defineProperty(vv, 'offsetTop', { configurable: true, get: () => 10 });
      vv.dispatchEvent(new Event('resize'));
      vv.dispatchEvent(new Event('scroll'));
    })()""")
    await asyncio.sleep(0.1)
    vv_check = await pg.evaluate("""(() => {
      const head = $('.convhead').getBoundingClientRect();
      const comp = $('.composer').getBoundingClientRect();
      return {
        appH: document.documentElement.style.getPropertyValue('--app-height'),
        appTop: document.documentElement.style.getPropertyValue('--app-offset-top'),
        headTop: head.top,
        compBot: comp.bottom,
      };
    })()""")
    kbH = max(200, round(height * 0.52))
    assert vv_check["appH"] == f"{kbH}px" and vv_check["appTop"] == "10px", f"{label} vv vars wrong: {vv_check}"
    assert abs(vv_check["headTop"] - 10) <= 2 and abs(vv_check["compBot"] - (kbH + 10)) <= 2, \
        f"{label} header/composer drifted on keyboard resize: {vv_check}"

    # Reset simulated visualViewport back to full screen & test focusout blur reset
    await pg.evaluate("""((h) => {
      const vv = window.visualViewport;
      Object.defineProperty(vv, 'height', { configurable: true, get: () => h });
      Object.defineProperty(vv, 'offsetTop', { configurable: true, get: () => 0 });
      vv.dispatchEvent(new Event('resize'));
      $('#text').dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    })""", height)
    await asyncio.sleep(0.1)

    # Wallpaper Editor 2-finger pinch-to-zoom check under mobile lock
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (30, 120, 210)).save(buf, format="PNG")
    if await pg.locator("#backBtn").is_visible():
        await pg.click("#backBtn")
    await pg.click("#setBtn")
    await wait_for(pg, "!$('#settings').hidden", 10, "settings open")
    await pg.set_input_files("#wallFile", {"name": "testwall.png", "mimeType": "image/png", "buffer": buf.getvalue()})
    await wait_for(pg, "!!document.querySelector('.we-ui')", 15, "wallpaper editor open")
    stage_box = await pg.evaluate("""(() => {
      const ui = document.querySelector('.we-ui'), r = ui.getBoundingClientRect();
      return { ta: getComputedStyle(ui).touchAction, cx: r.left + r.width / 2, cy: r.top + r.height / 2, z0: WALLED.zoomOf(WALLED.st) };
    })()""")
    assert stage_box["ta"] == "none", f"{label} .we-ui touch-action != none: {stage_box}"
    cdp = await pg.context.new_cdp_session(pg)
    cx, cy = stage_box["cx"], stage_box["cy"]
    await cdp.send("Input.dispatchTouchEvent", {
        "type": "touchStart",
        "touchPoints": [{"x": cx - 20, "y": cy, "id": 1}, {"x": cx + 20, "y": cy, "id": 2}],
    })
    for step in (35, 55, 75):
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchMove",
            "touchPoints": [{"x": cx - step, "y": cy, "id": 1}, {"x": cx + step, "y": cy, "id": 2}],
        })
        await asyncio.sleep(0.02)
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    z1 = await pg.evaluate("WALLED.zoomOf(WALLED.st)")
    assert z1 > stage_box["z0"] + 0.1, f"{label} wallpaper pinch zoom failed: z0={stage_box['z0']} z1={z1}"
    await cdp.detach()
    await pg.click(".wallsheet .acts button:not(.primary)")
    await pg.click("#setClose")
    print(f"  ok   {label} chat & wallpaper editor: scroll lock, vv resize ({kbH}px), pinch zoom ({stage_box['z0']:.2f}->{z1:.2f})")


async def check_mobile_race(p0, p1, spec, width, height, label):
    rid = await make_room([p0, p1], "race", laps=2, biome=0)
    await wait_for(p0, "!!document.querySelector('.lobby .primary:not([disabled])')", 30, "race lobby ready")
    await p0.click(".lobby .primary")
    for pg in (p0, p1):
        await wait_for(pg, f"RACE.root && RACE.rid === '{rid}' && !RACE.spec", 20, "race mounted")
    if not await spec.evaluate("GC.open"):
        await spec.click("#gcBtn")
    await wait_for(spec, f"!!document.querySelector(`.live-row .watch[data-r='{rid}']`)", 20, "Live Now race row")
    await spec.click(f".live-row .watch[data-r='{rid}']")
    await wait_for(spec, "RACE.root && RACE.spec && !!RACE.view", 20, "race spectator mounted")
    await wait_for(p0, "RACE.tg() > 0.3", 30, "race green light")

    # Active player layout & >=44px touch targets & zero overlap
    metrics = await p0.evaluate("""(({w, h}) => {
      const ids = ['#rtSteer', '#rtGas', '#rtBrake', '#rtAuto', '#rtItem', '#rtCam'];
      const boxes = {};
      for (const s of ids) {
        const b = document.querySelector(s).getBoundingClientRect();
        boxes[s] = { w: Math.round(b.width), h: Math.round(b.height), l: b.left, r: b.right, t: b.top, b: b.bottom };
      }
      const kbds = [...document.querySelectorAll('#gc kbd')].map((k) => getComputedStyle(k).display);
      return {
        touchDisp: getComputedStyle(document.querySelector('.race-touch')).display,
        boxes,
        kbdsVisible: kbds.filter((d) => d !== 'none').length,
        sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
        w, h,
      };
    })""", {"w": width, "h": height})
    assert metrics["touchDisp"] == "block" and metrics["kbdsVisible"] == 0, f"{label} race touch HUD invalid: {metrics}"
    assert metrics["sw"] <= metrics["cw"], f"{label} race horizontal overflow: {metrics}"
    for s, b in metrics["boxes"].items():
        assert b["w"] >= 44 and b["h"] >= 44, f"{label} button {s} too small: {b}"
        assert b["l"] >= 0 and b["r"] <= width + 1 and b["t"] >= 0 and b["b"] <= height + 1, \
            f"{label} button {s} clipped out of {width}x{height}: {b}"
    st_r = metrics["boxes"]["#rtSteer"]
    for rp in ("#rtGas", "#rtBrake", "#rtAuto", "#rtItem"):
        rb = metrics["boxes"][rp]
        x_ov = st_r["l"] < rb["r"] and st_r["r"] > rb["l"]
        y_ov = st_r["t"] < rb["b"] and st_r["b"] > rb["t"]
        assert not (x_ov and y_ov), f"{label} steer overlaps {rp}: {st_r} vs {rb}"

    # Multi-touch: simultaneous steer slide (left -> right without lift) + Gas + Auto-Gas + Pickup + Cam cycle
    mt_res = await p0.evaluate("""(() => {
      const pad = $('#rtSteer'), gas = $('#rtGas'), auto = $('#rtAuto'), item = $('#rtItem');
      const sr = pad.getBoundingClientRect(), gr = gas.getBoundingClientRect();
      const fire = (el, type, id, x, y) => el.dispatchEvent(new PointerEvent(type, {
        bubbles: true, cancelable: true, pointerId: id, pointerType: 'touch', clientX: x, clientY: y, button: 0,
      }));
      fire(pad, 'pointerdown', 11, sr.left + 12, sr.top + sr.height / 2);
      fire(gas, 'pointerdown', 12, gr.left + gr.width / 2, gr.top + gr.height / 2);
      const s1 = { l: RACE.touch.left, r: RACE.touch.right, g: RACE.touch.gas };
      fire(pad, 'pointermove', 11, sr.right - 12, sr.top + sr.height / 2);
      const s2 = { l: RACE.touch.left, r: RACE.touch.right, g: RACE.touch.gas };
      RACE.step(1 / 120);
      const v1 = Math.hypot(RACE.me.vx, RACE.me.vy);
      fire(pad, 'pointerup', 11, sr.right - 12, sr.top + sr.height / 2);
      fire(gas, 'pointerup', 12, gr.left + gr.width / 2, gr.top + gr.height / 2);
      const s3 = { l: RACE.touch.left, r: RACE.touch.right, g: RACE.touch.gas };
      // Auto-Gas toggle
      fire(auto, 'pointerdown', 13, 0, 0);
      const ag1 = RACE.touch.autoGas && auto.getAttribute('aria-pressed') === 'true';
      // Grant pickup, check charged state, fire with touch button
      RACE.me.item = 'boost';
      RACE.syncTouchItem();
      const charged = item.classList.contains('charged') && item.textContent.includes('Overdrive');
      fire(item, 'pointerdown', 14, 0, 0);
      const afterItem = RACE.me.item === '' && RACE.me.boost > 0 && item.classList.contains('empty');
      return { s1, s2, v1, s3, ag1, charged, afterItem };
    })()""")
    assert mt_res["s1"] == {"l": True, "r": False, "g": True}, f"{label} steer+gas down failed: {mt_res}"
    assert mt_res["s2"] == {"l": False, "r": True, "g": True}, f"{label} steer slide right failed: {mt_res}"
    assert mt_res["v1"] > 1, f"{label} car did not accelerate with touch gas: {mt_res}"
    assert mt_res["s3"] == {"l": False, "r": False, "g": False}, f"{label} touch release stuck: {mt_res}"
    assert mt_res["ag1"] and mt_res["charged"] and mt_res["afterItem"], f"{label} autoGas/pickup failed: {mt_res}"

    # Cam cycle button
    await p0.evaluate("setPref('raceCam', 'north')")
    await wait_for(p0, "pref('raceCam') === 'north'", 5, "camera reset north")
    c0 = await p0.evaluate("pref('raceCam')")
    await p0.click("#rtCam")
    c1 = await p0.evaluate("pref('raceCam')")
    await p0.click("#rtCam")
    c2 = await p0.evaluate("pref('raceCam')")
    await p0.click("#rtCam")
    c3 = await p0.evaluate("pref('raceCam')")
    assert [c0, c1, c2, c3] == ["north", "chase", "bird", "north"], f"{label} Cam cycle wrong: {[c0, c1, c2, c3]}"

    # Auto-hide & input reset when bottom Chat or Radio drawer opens
    await p0.evaluate("""(() => {
      const pad = $('#rtSteer'), sr = pad.getBoundingClientRect();
      pad.dispatchEvent(new PointerEvent('pointerdown', {
        bubbles: true, cancelable: true, pointerId: 15, pointerType: 'touch', clientX: sr.left + 12, clientY: sr.top + 20, button: 0,
      }));
    })()""")
    assert await p0.evaluate("RACE.touch.left && !$('.race-touch').hidden"), "steer hold before drawer failed"
    await p0.click(".gc-chatbtn")
    chat_hide = await p0.evaluate("({ hid: $('.race-touch').hidden, camHid: $('#rtCam').hidden, t: { ...RACE.touch } })")
    assert chat_hide["hid"] and chat_hide["camHid"] and not chat_hide["t"]["left"], f"{label} Chat did not hide/release: {chat_hide}"
    await p0.click("#gccClose")
    assert not await p0.evaluate("$('.race-touch').hidden"), f"{label} overlay did not restore after Chat close"

    await p0.click(".gc-radiobtn")
    dash_hide = await p0.evaluate("({ hid: $('.race-touch').hidden, camHid: $('#rtCam').hidden })")
    assert dash_hide["hid"] and dash_hide["camHid"], f"{label} Radio did not hide touch controls: {dash_hide}"
    await p0.click("#dashClose")

    # Spectator controls on mobile
    spec_res = await spec.evaluate("""(() => {
      const driveCnt = document.querySelectorAll('.race-touch').length;
      const ids = ['#specZoomOut', '#specFull', '#specZoomIn', '#specChase'];
      const boxes = Object.fromEntries(ids.map((s) => {
        const b = document.querySelector(s).getBoundingClientRect();
        return [s, { w: Math.round(b.width), h: Math.round(b.height), b: b.bottom }];
      }));
      const cv = document.querySelector('.race-room.is-spec canvas.world');
      const r = cv.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
      const z0 = RACE.specCam.targetZoom;
      const fire = (type, id, x, y) => cv.dispatchEvent(new PointerEvent(type, {
        bubbles: true, cancelable: true, pointerId: id, pointerType: 'touch', clientX: x, clientY: y, button: -1,
      }));
      fire('pointerdown', 21, cx - 30, cy);
      fire('pointerdown', 22, cx + 30, cy);
      fire('pointermove', 21, cx - 90, cy);
      fire('pointermove', 22, cx + 90, cy);
      const z1 = RACE.specCam.targetZoom;
      fire('pointerup', 21, cx - 90, cy);
      fire('pointerup', 22, cx + 90, cy);
      return {
        driveCnt, boxes, z0, z1,
        sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
        boardCount: document.querySelectorAll('.hud .board.spec-board button').length,
      };
    })()""")
    assert spec_res["driveCnt"] == 0 and spec_res["sw"] <= spec_res["cw"] and spec_res["boardCount"] >= 2, \
        f"{label} spectator HUD failed: {spec_res}"
    for s, b in spec_res["boxes"].items():
        assert b["w"] >= 44 and b["h"] >= 44 and b["b"] <= height + 1, f"{label} spec button {s} bad size/pos: {b}"
    assert spec_res["z1"] > spec_res["z0"] * 1.1, f"{label} spectator pinch zoom failed: {spec_res}"
    await spec.click(".hud .board.spec-board button[data-seat='0']")
    assert await spec.evaluate("RACE.specCam.follow === 0"), f"{label} tap leaderboard follow failed"
    await spec.click("#specChase")
    assert await spec.evaluate("RACE.specCam.autoChase === true"), f"{label} tap Auto-Chase failed"
    await spec.click("#specFull")
    assert await spec.evaluate("RACE.specCam.follow === null && !RACE.specCam.autoChase"), f"{label} tap Full Track failed"

    await cleanup_gc([p0, p1, spec])
    print(f"  ok   {label} Slot Rush active & spectator: touch controls, slide steer, auto-hide, pinch zoom")


async def check_mobile_uno(p0, p1, spec, width, height, label):
    # Ensure p0, p1 start, spec watches (3 players -> p0 & p1 deal UNO, plus add p2? Use 2-player game watched by spec,
    # and also inspect 3-seat spectator overflow check if needed!)
    rid = await make_room([p0, p1], "uno")
    await wait_for(p0, "!!document.querySelector('.lobby .primary:not([disabled])')", 25, "UNO lobby ready")
    await p0.click(".lobby .primary")
    for pg in (p0, p1):
        await wait_for(pg, "!!(UNO.view && UNO.el && UNO.view.hand.length >= 7)", 20, "UNO dealt")
    if not await spec.evaluate("GC.open"):
        await spec.click("#gcBtn")
    await wait_for(spec, f"!!document.querySelector(`.live-row .watch[data-r='{rid}']`)", 20, "Live Now UNO row")
    await spec.click(f".live-row .watch[data-r='{rid}']")
    await wait_for(spec, "!!document.querySelector('.uno-room.is-spec') && !!UNO.pub", 20, "UNO spectator table")

    # Find whichever active page currently has the turn
    turn = await p0.evaluate("UNO.view.turn")
    cur_pg = p0 if turn == 0 else p1
    if await cur_pg.evaluate("UNO.view.phase === 'color'"):
        await cur_pg.locator(".uno-modal .swatch").first.click()
        await wait_for(cur_pg, "UNO.view.phase !== 'color'", 10, "initial wild colour picked")
        turn = await p0.evaluate("UNO.view.turn")
        cur_pg = p0 if turn == 0 else p1
    uno_m = await cur_pg.evaluate("""(() => {
      const h = document.querySelector('.uno-hand'), hs = getComputedStyle(h);
      const btns = [...document.querySelectorAll('.uno-bar .uno-btn')].map((b) => {
        const r = b.getBoundingClientRect();
        return { k: b.dataset.k, w: Math.round(r.width), h: Math.round(r.height), r: r.right, b: r.bottom };
      });
      const kbds = [...document.querySelectorAll('#gc kbd')].filter((k) => getComputedStyle(k).display !== 'none').length;
      return {
        overflowX: hs.overflowX, touchAction: hs.touchAction,
        btns, kbds, sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
        handSw: h.scrollWidth, handCw: h.clientWidth,
      };
    })()""")
    assert uno_m["overflowX"] == "auto" and uno_m["touchAction"] == "pan-x" and uno_m["kbds"] == 0, \
        f"{label} UNO hand/kbd check failed: {uno_m}"
    assert uno_m["sw"] <= uno_m["cw"], f"{label} UNO horizontal overflow: {uno_m}"
    for b in uno_m["btns"]:
        assert b["w"] >= 44 and b["h"] >= 44 and b["r"] <= width + 1 and b["b"] <= height + 1, \
            f"{label} UNO button clipped/small: {b}"

    # Verify tap-to-select (raises card without playing) and tap-again-to-play (plays legal card)
    if await cur_pg.evaluate("!(UNO.view.legal || []).length"):
        v0 = await cur_pg.evaluate("UNO.view.v")
        await cur_pg.locator(".uno-bar .uno-btn[data-k='d']").click()
        await wait_for(cur_pg, f"UNO.view.v > {v0}", 15, "UNO draw via touch button")
        if await cur_pg.evaluate("UNO.view.phase === 'drawn' && !(UNO.view.legal || []).length"):
            v1 = await cur_pg.evaluate("UNO.view.v")
            await cur_pg.locator(".uno-bar .uno-btn[data-k='p']").click()
            await wait_for(cur_pg, f"UNO.view.v > {v1}", 15, "UNO keep via touch button")
            turn = await p0.evaluate("UNO.view.turn")
            cur_pg = p0 if turn == 0 else p1
    tap_info = await cur_pg.evaluate("""(() => {
      const v = UNO.view, legal = new Set(v.legal || []);
      const idx = v.hand.findIndex((id, i) => i !== UNO.sel && legal.has(id));
      const leg0 = v.hand.findIndex((id) => legal.has(id));
      const other = v.hand.findIndex((_, i) => i !== UNO.sel);
      return { v: v.v, idx, leg0, other, sel0: UNO.sel, legalSize: legal.size };
    })()""")
    if tap_info["other"] >= 0:
        await cur_pg.locator(".uno-hand .card").nth(tap_info["other"]).click()
        assert await cur_pg.evaluate(f"UNO.sel === {tap_info['other']}"), f"{label} tap did not select card {tap_info['other']}"
    play_idx = tap_info["idx"] if tap_info["idx"] >= 0 else tap_info["leg0"]
    if play_idx >= 0:
        if await cur_pg.evaluate(f"UNO.sel !== {play_idx}"):
            await cur_pg.locator(".uno-hand .card").nth(play_idx).click()
            assert await cur_pg.evaluate(f"UNO.sel === {play_idx}"), f"{label} tap did not select playable card {play_idx}"
        is_wild = await cur_pg.evaluate(f"UNO_CARDS[UNO.view.hand[{play_idx}]][0] < 0")
        await cur_pg.locator(".uno-hand .card").nth(play_idx).click()
        if is_wild:
            await wait_for(cur_pg, "!!document.querySelector('.uno-modal .swatches')", 5, "Wild colour grid")
            sw_box = await cur_pg.evaluate("document.querySelector('.uno-modal .swatch').getBoundingClientRect()")
            assert sw_box["width"] >= 64 and sw_box["height"] >= 64, f"{label} swatch too small: {sw_box}"
            await cur_pg.locator(".uno-modal .swatch").first.click()
        await wait_for(cur_pg, f"UNO.view.v > {tap_info['v']}", 15, "UNO card played via second tap")

    # Verify horizontal hand tray scrolling with large hand (26 cards) keeps page scrollWidth clean
    hand_scroll = await p0.evaluate("""(() => {
      const saved = UNO.view;
      UNO.setView({ ...saved, v: saved.v + 1, hand: Array.from({ length: 26 }, (_, i) => i) });
      UNO.render();
      const h = document.querySelector('.uno-hand');
      const sw0 = h.scrollWidth, cw0 = h.clientWidth;
      h.scrollLeft = 60;
      const sl = h.scrollLeft;
      const pageSw = document.documentElement.scrollWidth, pageCw = document.documentElement.clientWidth;
      UNO.view = saved;
      UNO.render();
      return { sw0, cw0, sl, pageSw, pageCw };
    })()""")
    assert hand_scroll["sw0"] > hand_scroll["cw0"] and hand_scroll["sl"] > 0 and hand_scroll["pageSw"] <= hand_scroll["pageCw"], \
        f"{label} UNO hand scroll check failed: {hand_scroll}"

    # UNO Spectator layout fit (portrait and landscape)
    spec_m = await spec.evaluate("""(({w, h}) => {
      const seats = [...document.querySelectorAll('.uno-table .seat')].map((s) => {
        const r = s.getBoundingClientRect();
        return { cls: s.className, l: r.left, r: r.right, t: r.top, b: r.bottom };
      });
      return {
        hands: document.querySelectorAll('.uno-hand').length,
        btns: document.querySelectorAll('.uno-bar .uno-btn').length,
        seats,
        sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
        w, h,
      };
    })""", {"w": width, "h": height})
    assert spec_m["hands"] == 0 and spec_m["btns"] == 0 and spec_m["sw"] <= spec_m["cw"], \
        f"{label} UNO spectator overflow/controls: {spec_m}"
    for s in spec_m["seats"]:
        assert s["l"] >= -1 and s["r"] <= width + 1 and s["t"] >= 0 and s["b"] <= height + 1, \
            f"{label} UNO spectator seat clipped: {s}"

    # Verify 4-seat UNO spectator layout fit (bottom, left, top, right with 7-10 cards)
    spec4_m = await spec.evaluate("""(() => {
      UNO.setPubView({
        r: UNO.rid, room_id: UNO.rid, host: 1, host_num: 9, v: 999, round: 99,
        turn_seat: 1, direction: 1, active_colour: 2, discard_top: 15, deck_count: 68,
        phase: 'play', paused: false, round_over: false,
        seats: [
          { id: 1, name: 'Alice', card_count: 8, score: 12, out: false, away: false, called_uno: false },
          { id: 2, name: 'Bob', card_count: 10, score: 4, out: false, away: false, called_uno: false },
          { id: 3, name: 'Carol', card_count: 7, score: 25, out: false, away: false, called_uno: false },
          { id: 4, name: 'Dave', card_count: 9, score: 0, out: false, away: false, called_uno: false },
        ],
      });
      UNO.renderSpec();
      const seats = [...document.querySelectorAll('.uno-table .seat')].map((s) => {
        const r = s.getBoundingClientRect();
        return { cls: s.className, l: r.left, r: r.right, t: r.top, b: r.bottom };
      });
      return {
        seats,
        sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
      };
    })()""")
    assert spec4_m["sw"] <= spec4_m["cw"] and len(spec4_m["seats"]) == 4, \
        f"{label} 4-seat UNO spectator overflow: {spec4_m}"
    for s in spec4_m["seats"]:
        assert s["l"] >= -1 and s["r"] <= width + 1 and s["t"] >= 0 and s["b"] <= height + 1, \
            f"{label} 4-seat UNO spectator seat clipped: {s}"

    await cleanup_gc([p0, p1, spec])
    print(f"  ok   {label} UNO active & spectator: swipe hand, >=44px bar, tap-to-play, zero overflow")


async def run_orientation(browser, port, width, height, label):
    print(f"mobile ({label}, {width}x{height}):")
    ctxs, pages, errs = [], [], []
    for k in range(3):
        ctx = await browser.new_context(viewport={"width": width, "height": height}, has_touch=True, is_mobile=True)
        pg = await ctx.new_page()
        pg.on("pageerror", lambda e, k=k: errs.append(f"{NAMES[k]}: {e}"))
        await pg.goto(f"http://localhost:{port + k}/")
        ctxs.append(ctx)
        pages.append(pg)
    for pg in pages:
        await wait_for(pg, "S.contacts.size >= 2 && [...S.contacts.values()].filter((c) => c.online).length >= 2", 30, "stations online")

    await check_mobile_chat_and_walledit(pages[0], width, height, label)
    await check_mobile_race(pages[0], pages[1], pages[2], width, height, label)
    await check_mobile_uno(pages[0], pages[1], pages[2], width, height, label)

    for ctx in ctxs:
        await ctx.close()
    assert not errs, f"{label} page errors: {errs}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    a = ap.parse_args()
    async with async_playwright() as p:
        b = await p.chromium.launch(args=[
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ])
        await check_desktop_guard(b, a.port)
        await run_orientation(b, a.port, 390, 844, "portrait")
        await run_orientation(b, a.port, 844, 390, "landscape")
        await b.close()
    print("UI mobile test: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
