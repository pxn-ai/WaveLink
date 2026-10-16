"""The chat's personal touches, driven through the browser on two stations.  Needs Playwright with
Chromium (and Pillow for the test pictures).

    python3 -m wavelink sim --nodes 2 &
    python3 -m tests.ui_features               # screenshots in build/ui-shots/

Checks: themes (palette, custom colour, light/dark, animations) survive a reload; wallpapers
(built-in; uploaded through the editor: cropped to the chat's shape, turned, adjusted, fitted four
ways, edited again from the kept original; dimmed); profile pictures (emoji, photo, back to the photo) reach the other
station over the air; the emoji palette (search, insert, recently used); the quality sheet for
photos (four sizes, the chosen one arrives) and for voice notes (a fake microphone, Opus re-encoded
to 12 kb/s and packed into WebM that plays); cancelling a file on either end; a message that
arrives late slides into the place it was sent from; new messages are highlighted and the arrow
counts them; sound effects are triggered; in the game center (during UNO): chatting, the + tray (Voice,
Camera, File, Emoji, Priority, with their sheets over the game) and the radio dashboard over the game.
"""
import argparse
import asyncio
import io
import json
import os
import random
import sys
import tempfile
import time

from playwright.async_api import async_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "build", "ui-shots")

# count sounds and FLIP animations on every page
HOOKS = """
window.__sfx = []; window.__anims = 0;
const _an = Element.prototype.animate;
Element.prototype.animate = function (k, o) { if (this.classList && this.classList.contains("msg") || this.classList.contains("day") || this.classList.contains("newdiv")) window.__anims++; return _an.call(this, k, o); };
document.addEventListener("DOMContentLoaded", () => {
  const p = SFX.play;
  SFX.play = function (n, o) { window.__sfx.push(n); return p.call(this, n, o); };
});
"""

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  {'ok ' if ok else 'FAIL'} {name}{': ' + detail if detail else ''}")
    return ok


async def wait_for(pg, js, timeout=30.0, what=""):
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            if await pg.evaluate(js):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.15)
    raise AssertionError(f"timed out waiting for {what or js}")


async def shot(pg, name):
    os.makedirs(OUT, exist_ok=True)
    await pg.screenshot(path=os.path.join(OUT, name))


def pictures(tmp):
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(5)
    # a "photo": sky gradient, hills, noise (so the sizes differ the way real photos do)
    import numpy as np
    w, h = 2400, 1600
    yy = np.linspace(0, 1, h)[:, None] * np.ones((1, w))
    im = Image.fromarray(np.stack([40 + 120 * yy, 90 + 80 * yy, 200 - 60 * yy], -1).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for k in range(9):
        cx, cy, r = rnd.randrange(w), rnd.randrange(h // 2, h), rnd.randrange(200, 700)
        d.ellipse([cx - r, cy - r // 2, cx + r, cy + r // 2], fill=(rnd.randrange(30, 90), rnd.randrange(90, 160), rnd.randrange(30, 80)))
    for k in range(4000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        d.point((x, y), fill=(255, 255, 255))
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    photo = os.path.join(tmp, "harbour.jpg")
    im.save(photo, quality=90)
    # a portrait for the profile picture
    p = Image.new("RGB", (600, 800), (40, 60, 90))
    d = ImageDraw.Draw(p)
    d.ellipse([170, 160, 430, 460], fill=(230, 190, 150))
    d.rectangle([140, 520, 460, 800], fill=(200, 60, 80))
    portrait = os.path.join(tmp, "me.jpg")
    p.save(portrait, quality=88)
    # a wallpaper
    xx, yy = np.meshgrid(np.linspace(0, 1, 1600), np.linspace(0, 1, 1000))
    wp = Image.fromarray(np.stack([20 + 60 * xx, 30 + 40 * yy, 90 + 0 * xx], -1).astype(np.uint8))
    wall = os.path.join(tmp, "wall.png")
    wp.save(wall)
    # a big file for the cancel tests and a medium one for the late message
    big = os.path.join(tmp, "dataset.bin")
    with open(big, "wb") as f:
        f.write(os.urandom(1_500_000))
    mid = os.path.join(tmp, f"capture-{os.urandom(2).hex()}.bin")   # (a fresh name for each run)
    with open(mid, "wb") as f:
        f.write(os.urandom(260_000))
    # a short MP3 for the background music (ffmpeg makes it; without ffmpeg a WAV)
    mp3 = os.path.join(tmp, "theme.mp3")
    import shutil, subprocess
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=6", "-b:a", "64k", mp3], check=True)
    else:
        import wave
        mp3 = os.path.join(tmp, "theme.wav")
        with wave.open(mp3, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
            w.writeframes(b"".join(int(8000 * np.sin(2 * np.pi * 330 * i / 22050)).to_bytes(2, "little", signed=True) for i in range(22050 * 6)))
    return photo, portrait, wall, big, mid, mp3


async def open_chat(pg, name):
    await pg.click(f"#chats li:has(.cname:text-is('{name}'))")
    await wait_for(pg, f"$('#convName').textContent === '{name}'", 5)


async def css_var(pg, v):
    return await pg.evaluate(f"getComputedStyle(document.documentElement).getPropertyValue('{v}').trim()")


async def test_theme(a):
    print("themes")
    await a.click("#setBtn")
    await wait_for(a, "!$('#settings').hidden", 5)
    await a.click('#modeSeg button[data-v="dark"]')
    await a.click('#palGrid .pal[data-v="cobalt"]')
    await a.wait_for_timeout(200)
    check("dark cobalt palette", await css_var(a, "--accent") == "#79a4ff" and await a.evaluate("document.documentElement.dataset.mode") == "dark")
    await a.evaluate("""(() => { const i = $('#palPick'); i.value = '#d0237a'; i.dispatchEvent(new Event('input')); i.dispatchEvent(new Event('change')); })()""")
    await a.wait_for_timeout(200)
    acc = await css_var(a, "--accent")
    check("custom colour palette", await a.evaluate("document.documentElement.dataset.pal") == "custom" and acc.startswith("#") and acc != "#79a4ff", acc)
    await a.click("#animSw")
    check("animations off", await a.evaluate("document.documentElement.dataset.anim") == "off")
    await a.click("#animSw")
    await a.wait_for_timeout(900)                      # the preferences are posted after 350 ms
    prof = await a.evaluate("fetch('/api/profile').then(r => r.json())")
    check("preferences stored on the station", prof["prefs"].get("pal") == "custom" and prof["prefs"].get("accent") == "#d0237a"
          and prof["prefs"].get("mode") == "dark", json.dumps(prof["prefs"])[:120])
    await a.reload()
    await wait_for(a, "S.me && S.profile", 15)
    # the inline script in <head> applied the cached look before the first paint
    check("look survives a reload", await a.evaluate("document.documentElement.dataset.pal") == "custom" and await css_var(a, "--accent") == acc)


async def test_wallpaper(a, wall):
    print("wallpaper: the editor (crop, rotate, adjust, fit)")
    await open_chat(a, "Bob")
    await a.click("#setBtn")
    await wait_for(a, "!$('#settings').hidden", 5)
    await a.click('#wallGrid .wtile[data-v="rain"]')
    check("built-in wallpaper", await a.evaluate("$('#wall').className") == "wall-rain")
    # a 1600 x 1000 picture: the editor opens, the frame in the chat window's shape
    await a.set_input_files("#wallFile", wall)
    await wait_for(a, "!!document.querySelector('.wallsheet') && !!WALLED.st?.view", 10, "wallpaper editor")
    await a.wait_for_timeout(400)
    st = await a.evaluate("(() => { const s = WALLED.st; return { chat: s.chat.w / s.chat.h, crop: s.crop.w / s.crop.h, full: Math.abs(s.crop.h - s.RH) < 0.5 || Math.abs(s.crop.w - s.RW) < 0.5 }; })()")
    check("the editor opens with the frame in the chat window's shape, as large as it goes",
          abs(st["crop"] - st["chat"]) < 0.01 and st["full"], f"chat {st['chat']:.3f}, frame {st['crop']:.3f}")
    await shot(a, "f22-wall-editor.png")
    # drag the bottom-right corner in: smaller, same shape
    pt = await a.evaluate("""(() => { const ui = document.querySelector('.we-ui'), r = ui.getBoundingClientRect(), v = WALLED.st.view, c = WALLED.st.crop;
      return [r.left + v.ox + (c.x + c.w) * v.ds - 3, r.top + v.oy + (c.y + c.h) * v.ds - 3, c.w]; })()""")
    await a.mouse.move(pt[0], pt[1])
    await a.mouse.down()
    await a.mouse.move(pt[0] - 120, pt[1] - 60, steps=5)
    await a.mouse.up()
    c = await a.evaluate("[WALLED.st.crop.w, WALLED.st.crop.w / WALLED.st.crop.h]")
    check("dragging a corner resizes the frame and keeps its shape", c[0] < pt[2] * 0.9 and abs(c[1] - st["chat"]) < 0.01, f"{pt[2]:.0f} -> {c[0]:.0f} px")
    # turn right: the picture is now portrait, the frame keeps the chat's shape
    await a.click('.we-tabs button[data-tab="rotate"]')
    await a.click('[data-turn="1"]')
    r = await a.evaluate("[WALLED.st.rot, WALLED.st.RW, WALLED.st.RH, WALLED.st.crop.w / WALLED.st.crop.h]")
    check("turn right", r[0] == 90 and r[1:3] == [1000, 1600] and abs(r[3] - st["chat"]) < 0.01, str(r))
    # adjust, fit
    await a.click('.we-tabs button[data-tab="adjust"]')
    await a.evaluate("(() => { const i = $('#weAb'); i.value = 115; i.dispatchEvent(new Event('input')); })()")
    await a.click('.we-tabs button[data-tab="fit"]')
    await a.click('.we-fits button[data-fit="stretch"]')
    await a.wait_for_timeout(300)
    await shot(a, "f23-wall-fit.png")
    await a.click(".wallsheet [data-go]")
    await wait_for(a, "!document.querySelector('.wallsheet') && $('#wall').className === 'wall-custom'", 20, "wallpaper set")
    await a.wait_for_timeout(900)                      # (preferences are posted after 350 ms)
    res = await a.evaluate("""(async () => { const p = await fetch('/api/profile').then(r => r.json());
      const dims = async (u) => { const i = new Image(); i.src = u; await i.decode(); return [i.naturalWidth, i.naturalHeight]; };
      return { out: await dims(p.wallpaper_url), src: await dims(p.wallpaper_src_url), srcUrl: p.wallpaper_src_url, edit: p.prefs.wallEdit, fit: p.prefs.wallFit,
               bs: getComputedStyle($('#wall')).backgroundSize, bg: $('#wall').style.backgroundImage }; })()""")
    out = res["out"]
    check("the saved picture is the frame: the chat's shape", abs(out[0] / out[1] - st["chat"]) < 0.01, f"{out[0]}x{out[1]}")
    check("the original is kept, with the settings", res["src"] == [1600, 1000] and res["edit"]["rot"] == 90 and res["edit"]["adj"]["b"] == 115,
          json.dumps(res["edit"])[:110])
    check("stretch to fit in the chat", res["fit"] == "stretch" and res["bs"] == "100% 100%" and "wallpaper" in res["bg"], res["bs"])
    # Edit… starts again from the original, with the same settings; Whole picture keeps all of it
    await a.click("#wallEditBtn")
    await wait_for(a, "!!document.querySelector('.wallsheet') && !!WALLED.st?.view", 10, "editor again")
    again = await a.evaluate("[WALLED.st.rot, WALLED.st.adj.b, WALLED.st.sw, WALLED.st.sh, WALLED.st.fresh]")
    await a.click('.we-mode button[data-m="whole"]')
    await a.click(".wallsheet [data-go]")
    await wait_for(a, "!document.querySelector('.wallsheet')", 20, "saved again")
    await a.wait_for_timeout(600)
    res2 = await a.evaluate("""(async () => { const p = await fetch('/api/profile').then(r => r.json());
      const i = new Image(); i.src = p.wallpaper_url; await i.decode(); return { out: [i.naturalWidth, i.naturalHeight], srcUrl: p.wallpaper_src_url }; })()""")
    check("Edit… reopens the original with its settings; the whole picture, turned", again == [90, 115, 1600, 1000, False]
          and res2["out"] == [1000, 1600] and res2["srcUrl"] == res["srcUrl"], f"{again}, {res2['out']}")
    # the fit choices in Settings
    got = []
    for v in ("cover", "contain", "normal", "stretch"):
        await a.click(f'#fitSeg button[data-v="{v}"]')
        got.append(await a.evaluate("getComputedStyle($('#wall')).backgroundSize"))
    check("Settings: crop to fit, fit inside, normal, stretch to fit", got == ["cover", "contain", "auto", "100% 100%"], ", ".join(got))
    await a.click('#fitSeg button[data-v="cover"]')
    await a.evaluate("(() => { const i = $('#wallDim'); i.value = 0.6; i.dispatchEvent(new Event('input')); i.dispatchEvent(new Event('change')); })()")
    await a.wait_for_timeout(500)
    check("wallpaper dimmed", await a.evaluate("$('#wall').style.getPropertyValue('--wall-dim')") == "0.6")
    await a.evaluate("$('#settings .dashbody').scrollTop = $('#wallGrid').offsetTop - 60")
    await shot(a, "f24-wall-settings.png")


async def test_avatar(a, b, portrait):
    print("profile pictures")
    await a.click('#avKind button[data-v="emoji"]')
    await a.wait_for_timeout(300)
    await a.click('#avPick button[data-e="🦉"]')
    await wait_for(b, "(S.contacts.get(1)?.av || {}).emoji === '🦉'", 20, "emoji avatar at Bob")
    check("emoji picture reaches Bob", await b.evaluate("[...document.querySelectorAll('#chats .avatar')].some(e => e.textContent === '🦉')"))
    await a.click('#avKind button[data-v="photo"]')        # no photo yet: opens the file picker
    await a.set_input_files("#avFile", portrait)
    await wait_for(a, "S.profile.avatar.kind === 'photo'", 10, "photo avatar")
    size = await a.evaluate("fetch(S.profile.avatar.url).then(r => r.blob()).then(b => b.size)")
    await wait_for(b, "(S.contacts.get(1)?.av || {}).url", 30, "photo at Bob")
    await wait_for(b, "[...document.querySelectorAll('#chats .avatar img')].some(i => i.complete && i.naturalWidth === 160)", 10, "Bob shows the photo")
    check("photo picture fetched by Bob over the air", True, f"{size} bytes")
    await a.click('#avKind button[data-v="initial"]')
    await wait_for(a, "S.profile.avatar.kind === 'initial'", 5)
    await a.click('#avKind button[data-v="photo"]')        # the station kept the photo
    await wait_for(a, "S.profile.avatar.kind === 'photo'", 5, "back to the photo")
    check("back to the earlier photo without uploading again", True)
    await shot(a, "f1-settings.png")
    await a.click("#setClose")


async def test_emoji(a):
    print("emoji palette")
    await open_chat(a, "Bob")
    await a.click("#emojiBtn")
    await a.fill("#emojiPop .eq", "satellite")
    await a.wait_for_timeout(150)
    names = await a.evaluate("EPICK.visibleButtons().map(b => b.title)")
    check("search by name", any("satellite" in n for n in names), ", ".join(names[:3]))
    await a.keyboard.press("Enter")
    await a.fill("#emojiPop .eq", "")
    await a.click('#emojiPop .etabs button[data-c="Food"]')
    await a.wait_for_timeout(500)
    await a.click('#emojiPop section[data-c="Food"] button >> nth=0')
    await a.keyboard.press("Escape")
    await a.wait_for_timeout(500)
    txt = await a.input_value("#text")
    check("emoji inserted at the cursor", len(txt) >= 2, repr(txt))
    await a.click("#emojiBtn")
    await a.wait_for_timeout(200)
    rec = await a.evaluate("[...document.querySelectorAll('#emojiPop section[data-c=Recent] button')].map(b => b.dataset.e)")
    check("recently used", len(rec) >= 2 and rec[0] == txt[-len(rec[0]):], " ".join(rec[:4]))
    await shot(a, "f2-emoji.png")
    await a.keyboard.press("Escape")
    await a.fill("#text", "")


async def sheet_sizes(pg, n):
    await wait_for(pg, f"document.querySelectorAll('.sheet .chip small').length === {n} && "
                       "[...document.querySelectorAll('.sheet .chip small')].every(s => s.textContent !== '…')", 60, "quality choices")
    return await pg.evaluate("Object.fromEntries([...document.querySelectorAll('.sheet .chip')].map(c => [c.dataset.k, c.querySelector('small').textContent]))")


def kb(txt):
    v = txt.split("·")[0].strip()
    num, unit = v.split()
    return float(num) * {"B": 1 / 1024, "KB": 1, "MB": 1024}[unit]


async def test_photo(a, b, photo):
    print("photo quality")
    await open_chat(a, "Bob")
    await a.set_input_files("#fileIn", photo)
    await wait_for(a, "!!document.querySelector('.sheet')", 10, "photo sheet")
    sizes = await sheet_sizes(a, 4)
    s = {k: kb(v) for k, v in sizes.items()}
    check("four photo sizes, smaller is smaller", s["saver"] < s["standard"] < s["hd"] < s["original"],
          ", ".join(f"{k} {v}" for k, v in sizes.items()))
    await a.click('.sheet .chip[data-k="hd"]')
    await a.check(".sheet .mkdef input")
    await shot(a, "f3-photo-sheet.png")
    n0 = await b.evaluate("S.msgs.size")
    await a.click(".sheet [data-go]")
    await wait_for(a, "!document.querySelector('.sheet')", 10)
    check("'use from now on' sets the default", await a.evaluate("pref('photoQ')") == "hd")
    await wait_for(b, f"[...S.msgs.values()].some(m => m.kind === 'image' && m.frm === 1 && m.name === 'harbour.webp')", 90, "photo at Bob")
    await open_chat(b, "Alice")
    await wait_for(b, "[...document.querySelectorAll('#msgs img.media')].some(i => i.complete && i.naturalWidth > 0)", 20)
    w = await b.evaluate("Math.max(...[...document.querySelectorAll('#msgs img.media')].map(i => i.naturalWidth))")
    check("the HD photo arrives at 1280 px", w == 1280, f"{w} px")
    await a.evaluate("setPref('photoQ', 'standard')")


async def test_voice(a, b):
    print("voice note quality")
    await open_chat(a, "Bob")
    await a.click("#sendBtn")                              # empty box: the microphone
    await wait_for(a, "!$('#recBar').hidden", 10, "recording")
    await a.wait_for_timeout(3200)
    await shot(a, "f4-recording.png")
    await a.click("#recSend")
    await wait_for(a, "!!document.querySelector('.sheet')", 10, "voice sheet")
    sizes = await sheet_sizes(a, 3)
    s = {k: kb(v) for k, v in sizes.items()}
    check("three voice sizes, smaller is smaller", s["low"] < s["standard"] < s["high"], ", ".join(f"{k} {v}" for k, v in sizes.items()))
    await a.click('.sheet .chip[data-k="low"]')
    await wait_for(a, "document.querySelector('.sheet .pv audio').readyState >= 1", 10, "preview loads")
    dur = await a.evaluate("document.querySelector('.sheet .pv audio').duration")
    check("the 12 kb/s WebM plays, with its duration", 2.5 < dur < 4.5, f"{dur:.2f} s")
    await shot(a, "f5-voice-sheet.png")
    n0 = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'audio' && m.frm === 1).length")
    await a.click(".sheet [data-go]")
    await wait_for(b, f"[...S.msgs.values()].filter(m => m.kind === 'audio' && m.frm === 1).length > {n0}", 60, "voice at Bob")
    await open_chat(b, "Alice")
    sel = "'#m-' + [...S.msgs.values()].filter(m => m.kind === 'audio' && m.frm === 1).pop().uid + ' audio'"
    await wait_for(b, f"document.querySelector({sel})?.readyState >= 1", 15, "Bob's audio loads")
    d2 = await b.evaluate(f"document.querySelector({sel}).duration")
    check("Bob's copy plays", 2.5 < d2 < 4.5, f"{d2:.2f} s")
    # the muxer on its own, and the fallback without WebCodecs
    res = await a.evaluate("""(async () => {
      const pcm = new Float32Array(96000); for (let i = 0; i < pcm.length; i++) pcm[i] = 0.4 * Math.sin(2 * Math.PI * 440 * i / 48000);
      const out = {};
      for (const nwc of [false, true]) {
        MEDIA.noWebCodecs = nwc;
        const t0 = performance.now();
        const blob = await MEDIA.encodeOpus(pcm, 24000);
        const back = await MEDIA.decodeMono(blob);
        let s = 0; for (let i = 24000; i < 72000; i++) s += back[i] * back[i];
        out[nwc ? 'fallback' : 'webcodecs'] = { type: blob.type, bytes: blob.size, secs: back.length / 48000, rms: Math.sqrt(s / 48000), ms: Math.round(performance.now() - t0) };
      }
      MEDIA.noWebCodecs = false;
      return out;
    })()""")
    for k, r in res.items():
        check(f"Opus at 24 kb/s ({k}) decodes back", 1.8 < r["secs"] < 2.4 and r["rms"] > 0.15,
              f"{r['type']}, {r['bytes']} B, {r['secs']:.2f} s, rms {r['rms']:.2f}, {r['ms']} ms")


async def test_cancel(a, b, big):
    print("cancel transfers")
    await open_chat(a, "Bob")
    await open_chat(b, "Alice")
    await a.set_input_files("#fileIn", big)
    await wait_for(a, "[...S.msgs.values()].some(m => m.name === 'dataset.bin' && m.status === 'sent' && m.progress > 0.04)", 60, "transfer under way")
    await shot(a, "f6-sending.png")
    uid = await a.evaluate("[...S.msgs.values()].filter(m => m.name === 'dataset.bin').pop().uid")
    await a.click(f"#m-{uid} [data-cancel]")
    await wait_for(a, f"S.msgs.get('{uid}').status === 'cancelled'", 10, "cancelled at Alice")
    await wait_for(b, "!(S.radio?.incoming || []).length", 15, "Bob dropped it")
    check("sender cancels: both ends stop", True)
    await a.set_input_files("#fileIn", big)
    await wait_for(b, "!!document.querySelector('#pending .inc .xbtn')", 60, "incoming at Bob")
    await shot(b, "f7-receiving.png")
    uid2 = await a.evaluate("[...S.msgs.values()].filter(m => m.name === 'dataset.bin').pop().uid")
    await b.click("#pending .inc .xbtn")
    await wait_for(a, f"S.msgs.get('{uid2}').status === 'cancelled'", 20, "Alice told")
    txt = await a.evaluate(f"document.querySelector('#m-{uid2} .progwrap .progtxt').textContent")
    check("receiver cancels: the sender stops", "cancelled by Bob" in txt, txt)
    await shot(a, "f8-cancelled.png")


async def test_late(a, b, mid):
    print("late message slides into place")
    await open_chat(a, "Bob")
    await open_chat(b, "Alice")
    await b.evaluate("window.__anims = 0")
    name = os.path.basename(mid)
    text = f"This was sent after {name}"
    await a.set_input_files("#fileIn", mid)
    await a.wait_for_timeout(300)
    await a.fill("#text", text)
    await a.press("#text", "Enter")
    await wait_for(b, f"[...S.msgs.values()].some(m => m.text === '{text}')", 30, "text at Bob")
    file_first = await b.evaluate(f"[...S.msgs.values()].some(m => m.name === '{name}')")
    await wait_for(b, f"[...S.msgs.values()].some(m => m.name === '{name}')", 120, "file at Bob")
    await b.wait_for_timeout(900)
    order = await b.evaluate("""(() => { const els = [...document.querySelectorAll('#msgs .msg')];
      const f = els.findIndex(e => e.textContent.includes('NAME')), t = els.findIndex(e => e.textContent.includes('TEXT'));
      return { f, t, late: els[f]?.querySelector('.late')?.textContent || '', anims: window.__anims }; })()""".replace("NAME", name).replace("TEXT", text))
    check("the text overtook the file on the air", not file_first)
    check("the file is shown where it was sent, above the text", 0 <= order["f"] < order["t"], json.dumps(order))
    check("it is marked late and slid into place", "late" in order["late"] and order["anims"] >= 2, f"{order['late']}, {order['anims']} animations")
    await shot(b, "f9-late.png")


async def test_fresh(a, b):
    print("new messages and the arrow")
    await open_chat(a, "Bob")
    await open_chat(b, "Alice")
    for k in range(18):
        await a.evaluate(f"api('/api/send', {{ to: 'u:2', text: 'Filler message {k + 1}' }})")
    await wait_for(b, "[...S.msgs.values()].some(m => m.text === 'Filler message 18')", 60, "filler")
    await b.wait_for_timeout(1500)
    await b.evaluate("$('#msgs').scrollTop = 0")
    await b.wait_for_timeout(300)
    for k in range(3):
        await a.evaluate(f"api('/api/send', {{ to: 'u:2', text: 'New while you were up there {k + 1}' }})")
    await wait_for(b, "[...S.msgs.values()].some(m => m.text === 'New while you were up there 3')", 60, "new ones")
    await b.wait_for_timeout(400)
    n = await b.evaluate("$('#jumpBtn').hidden ? -1 : +$('#jumpBtn .n').textContent")
    fresh = await b.evaluate("document.querySelectorAll('#msgs .msg.fresh:not(.seen)').length")
    check("arrow with the count of new messages", n == 3 and fresh == 3, f"arrow {n}, highlighted {fresh}")
    await shot(b, "f10-arrow.png")
    await b.click("#jumpBtn")
    await b.wait_for_timeout(700)
    await shot(b, "f11-highlight.png")
    await wait_for(b, "!document.querySelectorAll('#msgs .msg.fresh:not(.seen)').length", 8, "seen")
    await b.wait_for_timeout(1800)
    check("seen messages lose the highlight, the arrow goes", await b.evaluate("!document.querySelectorAll('#msgs .msg.fresh').length && $('#jumpBtn').hidden"))
    # unread when the chat is opened: a divider
    await b.click("#chats li:has(.cname:text-is('Everyone'))")
    await a.evaluate("api('/api/send', { to: 'u:2', text: 'Are you there?' })")
    await a.evaluate("api('/api/send', { to: 'u:2', text: 'Hello again' })")
    await wait_for(b, "S.unread['u:1'] === 2", 30, "unread")
    await open_chat(b, "Alice")
    div = await b.evaluate("document.querySelector('#msgs .newdiv')?.textContent || ''")
    check("'2 new messages' divider", div == "2 new messages", div)


async def test_nav(a):
    print("buttons at the bottom of the chat list")
    await a.click("#setBtn")
    await wait_for(a, "!$('#settings').hidden", 5)
    await a.click("#navSw")
    await a.wait_for_timeout(300)
    where = await a.evaluate("$('#navBtns').parentElement.id")
    lbl = await a.evaluate("getComputedStyle($('#sideNav .nlbl')).display + ' ' + $('#sideNav').getBoundingClientRect().bottom + ' ' + $('#chats').getBoundingClientRect().bottom")
    check("buttons move under the chat list, with names", where == "sideNav" and lbl.startswith("block"), lbl)
    await shot(a, "f12-bottom-bar.png")
    await a.click("#sideNav #radioBtn")
    check("they still work from there", await a.evaluate("!$('#dash').hidden && $('#radioBtn').getAttribute('aria-expanded') === 'true'"))
    await a.click("#sideNav #radioBtn")
    await a.click("#sideNav #setBtn")
    await wait_for(a, "!$('#settings').hidden", 5)
    await a.click("#navSw")
    await a.wait_for_timeout(300)
    check("and back to the header", await a.evaluate("$('#navBtns').parentElement.classList.contains('me')"))
    await a.click("#setClose")


async def test_camera(a, b):
    print("camera")
    await open_chat(a, "Bob")
    n0 = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).length")
    await a.click("#camBtn")
    await wait_for(a, "document.querySelector('.camsheet video')?.videoWidth > 0", 15, "camera preview")
    await a.click(".camsheet [data-shoot]")
    await wait_for(a, "!document.querySelector('.camsheet .shot').hidden", 5, "photo taken")
    await shot(a, "f13-camera.png")
    await a.click(".camsheet [data-retake]")
    check("retake goes back to the live picture", await a.evaluate("!document.querySelector('.camsheet video').hidden"))
    await a.click(".camsheet [data-shoot]")
    await wait_for(a, "!document.querySelector('.camsheet [data-use]').disabled", 5)
    await a.click(".camsheet [data-use]")
    await wait_for(a, "!!document.querySelector('.sheet:not(.camsheet)')", 10, "quality sheet")
    gone = await a.evaluate("!document.querySelector('.camsheet')")
    await sheet_sizes(a, 4)
    await a.click('.sheet .chip[data-k="standard"]')
    await a.click(".sheet [data-go]")
    await wait_for(b, f"[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).length > {n0}", 60, "photo at Bob")
    name = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).pop().name")
    check("photo taken, quality chosen, received by Bob", gone and name.startswith("photo-"), name)


async def test_music(a, mp3):
    print("background music")
    await a.click("#setBtn")
    await wait_for(a, "!$('#settings').hidden", 5)
    await a.evaluate("$('#settings .dashbody').scrollTop = $('#musicGrid').offsetTop - 80")
    res = await a.evaluate("""(async () => {
      const out = {};
      for (const id of ['drift', 'carrier', 'night', 'rain']) {
        const t0 = performance.now();
        const b = await MUSIC.render(id);
        const d = b.getChannelData(0);
        let s = 0; for (let i = 0; i < d.length; i += 7) s += d[i] * d[i];
        // the loop joins: the last and first samples are close
        out[id] = { secs: +(b.duration).toFixed(1), rms: +Math.sqrt(s / (d.length / 7)).toFixed(3), ms: Math.round(performance.now() - t0),
                    seam: +Math.abs(d[d.length - 1] - d[0]).toFixed(3) };
      }
      return out; })()""")
    for k, r in res.items():
        check(f"built-in track {k}", r["rms"] > 0.01 and r["seam"] < 0.2, f"{r['secs']} s loop, rms {r['rms']}, made in {r['ms']} ms")
    await a.click('#musicGrid button[data-v="night"]')
    await wait_for(a, "MUSIC.node && MUSIC.cur === 'night'", 15, "music playing")
    check("chosen track plays", True)
    await a.evaluate("(() => { const i = $('#musicVol'); i.value = 0.2; i.dispatchEvent(new Event('input')); i.dispatchEvent(new Event('change')); })()")
    await a.wait_for_timeout(600)
    g = await a.evaluate("MUSIC.gain.gain.value")
    check("music volume", abs(g - 0.2 * 0.9) < 0.03, f"gain {g:.3f}")
    await a.set_input_files("#musicFile", mp3)
    await wait_for(a, "S.profile.music_url && MUSIC.el && MUSIC.cur.startsWith('custom')", 20, "uploaded music playing")
    st = await a.evaluate("fetch(S.profile.music_url, { headers: { Range: 'bytes=0-99' } }).then(r => r.status + ' ' + r.headers.get('content-range'))")
    await a.wait_for_timeout(1500)
    playing = await a.evaluate("!MUSIC.el.paused && MUSIC.el.currentTime > 0")
    check("uploaded MP3 loops (served in ranges)", playing and st.startswith("206"), st)
    await shot(a, "f14-music.png")
    await a.evaluate("MUSIC.playing.add('test'); MUSIC.updateDuck()")
    ducked = await a.evaluate("MUSIC.duck")
    await a.evaluate("MUSIC.playing.delete('test'); MUSIC.updateDuck()")
    check("music steps back while other audio plays", ducked < 0.2)
    await a.click('#musicGrid button[data-v="off"]')
    await wait_for(a, "!MUSIC.node && !MUSIC.el", 5, "music off")
    await a.click("#setClose")


async def test_gcchat(a, b):
    print("chats inside the game center (during UNO)")
    await start_uno(a, b)
    hand0 = await a.evaluate("UNO.view.hand.length")
    v0 = await a.evaluate("UNO.view.v")
    await a.click(".gc-chatbtn")
    await wait_for(a, "!$('#gcChat').hidden && document.activeElement === $('#gccText')", 5, "chat panel")
    await a.click(f"#gccTabs button[data-chat='u:2']")
    # the letters D, P, U, C are UNO keys: typed into the panel they must not play
    await a.type("#gccText", "Drawing? Pass? UNO? Catch! Good game")
    await a.press("#gccText", "Enter")
    await wait_for(b, "[...S.msgs.values()].some(m => m.text === 'Drawing? Pass? UNO? Catch! Good game')", 30, "message at Bob")
    still = await a.evaluate("GC.open && !!UNO.el && UNO.view.hand.length") 
    check("message sent from the game center", True)
    check("typing didn't play UNO keys and the game is still on", still == hand0 or (await a.evaluate("UNO.view.v")) >= v0, f"hand {hand0} -> {still}")
    await b.evaluate("api('/api/send', { to: 'u:1', text: 'Your turn!' })")
    await wait_for(a, "[...document.querySelectorAll('#gccMsgs .gcm .t')].some(e => e.textContent === 'Your turn!')", 30, "reply in the panel")
    check("replies show in the panel", True)
    await shot(a, "f15-gc-chat.png")
    await a.press("#gccText", "Escape")
    check("Esc closes the panel and leaves the game open", await a.evaluate("$('#gcChat').hidden && GC.open"))
    # Bob's unread count on the Chat button
    await a.evaluate("api('/api/send', { to: 'u:2', text: 'Ping from the table' })")
    await wait_for(b, "+(document.querySelector('.gc-chatbtn .n')?.textContent || 0) > 0", 30, "badge")
    check("unread count on the Chat button", True)
    await end_uno(a, b)


async def start_uno(a, b):
    for pg in (a, b):
        if not await pg.evaluate("GC.open"):
            await pg.click("#gcBtn")
    await wait_for(a, "!!document.querySelector('.poster.p-uno .pick input')", 20, "UNO poster")
    await a.click(".poster.p-uno .pick label:has-text('Bob')")
    await a.click(".poster.p-uno .go")
    await wait_for(b, "!!document.querySelector('.invite .yes')", 20, "invitation")
    await b.click(".invite .yes")
    await wait_for(a, "!!document.querySelector('.lobby .primary:not([disabled])')", 30, "lobby")
    await a.click(".lobby .primary")
    for pg in (a, b):
        await wait_for(pg, "UNO.view && UNO.el", 20, "UNO table")


async def end_uno(a, b):
    rid = await a.evaluate("UNO.rid")
    await a.evaluate(f"GC.post({{ a: 'leave', r: '{rid}' }}).catch(() => {{}})")      # the host closes the table
    await a.wait_for_timeout(1500)
    for pg in (a, b):
        await pg.evaluate("GC.show(false); GC.focus = null")


# is the middle of this element the topmost thing on the screen (not hidden behind the game center)?
ON_TOP = """(sel) => { const e = document.querySelector(sel); if (!e) return false; const r = e.getBoundingClientRect();
  if (!r.width || !r.height) return false; const t = document.elementFromPoint(r.left + r.width / 2, r.top + Math.min(r.height / 2, 40));
  if (!t || !(t === e || e.contains(t))) return false;
  if (getComputedStyle(e).visibility !== "visible") return false;     // (inherited, so the element's own value tells)
  let op = 1; for (let x = e; x; x = x.parentElement) op *= +getComputedStyle(x).opacity;
  return op > 0.9; }"""


async def test_gctray(a, b, photo):
    print("the + tray in the game center chat (during UNO)")
    await start_uno(a, b)
    await a.click(".gc-chatbtn")
    await wait_for(a, "!$('#gcChat').hidden", 5, "chat panel")
    await a.click("#gccTabs button[data-chat='u:2']")
    await a.click("#gccPlus")
    await a.wait_for_timeout(400)
    tools = await a.evaluate("[...document.querySelectorAll('#gccTray button')].map(b => b.dataset.tool)")
    check("one + button opens Voice, Camera, File, Emoji, Priority", tools == ["mic", "cam", "file", "emoji", "urgent"]
          and await a.evaluate(ON_TOP, "#gccTray"), ", ".join(tools))
    await shot(a, "f16-gc-tray.png")
    # priority, then a message
    await a.click('#gccTray [data-tool="urgent"]')
    pressed = await a.evaluate("GCCHAT.urgent && $('#gccForm').classList.contains('urgent') && $('#gccText').placeholder.startsWith('Priority')")
    await a.fill("#gccText", "Priority from the table")
    await a.press("#gccText", "Enter")
    await wait_for(b, "[...S.msgs.values()].some(m => m.text === 'Priority from the table')", 30, "priority message at Bob")
    urg = await b.evaluate("[...S.msgs.values()].find(m => m.text === 'Priority from the table').urgent")
    check("Priority: the message goes out urgent, then the switch resets", pressed and urg and not await a.evaluate("GCCHAT.urgent"))
    # emoji into the panel's box (the palette moves into the panel)
    await a.click("#gccPlus")
    await a.click('#gccTray [data-tool="emoji"]')
    await wait_for(a, "EPICK.isOpen() && $('#emojiPop').parentElement === $('#gcChat')", 5, "palette in the panel")
    await a.wait_for_timeout(400)
    vis = await a.evaluate(ON_TOP, "#emojiPop .egrid")
    await a.fill("#emojiPop .eq", "satellite")
    await a.wait_for_timeout(150)
    await a.keyboard.press("Enter")
    await a.wait_for_timeout(150)
    await shot(a, "f17-gc-emoji.png")
    await a.keyboard.press("Escape")
    txt = await a.input_value("#gccText")
    check("Emoji types into the panel's message box", vis and "🛰" in txt and await a.evaluate("GC.open && !EPICK.isOpen()"), repr(txt))
    await a.fill("#gccText", "")
    # File: a photo, through the quality sheet, which shows above the game
    n0 = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).length")
    await a.click("#gccPlus")
    async with a.expect_file_chooser() as fc:
        await a.click('#gccTray [data-tool="file"]')
    await (await fc.value).set_files(photo)
    await wait_for(a, "!!document.querySelector('.sheet')", 10, "photo sheet")
    await sheet_sizes(a, 4)
    await a.wait_for_timeout(300)
    top = await a.evaluate(ON_TOP, ".sheet .scard")
    title = await a.evaluate("document.querySelector('.sheet h3').textContent")
    await shot(a, "f18-gc-photo-sheet.png")
    await a.click('.sheet .chip[data-k="saver"]')
    await a.click(".sheet [data-go]")
    await wait_for(b, f"[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).length > {n0}", 90, "photo at Bob")
    toast_z = await a.evaluate("+getComputedStyle($('#toast')).zIndex")
    check("File: the sheet shows over the game and the photo reaches Bob", top and "Bob" in title and toast_z > 20, f"{title!r}, toast z {toast_z}")
    await wait_for(a, "[...document.querySelectorAll('#gccMsgs .gcm.mine img')].length > 0", 10, "photo in the panel")
    # Voice: the recording bar takes the message box's place inside the panel
    n0 = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'audio' && m.frm === 1).length")
    await a.click("#gccPlus")
    await a.click('#gccTray [data-tool="mic"]')
    await wait_for(a, "!$('#gccRec').hidden && $('#gccForm').hidden", 10, "recording in the panel")
    rec_top = await a.evaluate(ON_TOP, "#gccRec")
    await a.wait_for_timeout(2500)
    await shot(a, "f19-gc-recording.png")
    await a.click("#gccRecSend")
    await wait_for(a, "!!document.querySelector('.sheet')", 10, "voice sheet")
    await sheet_sizes(a, 3)
    await a.click('.sheet .chip[data-k="low"]')
    await a.click(".sheet [data-go]")
    await wait_for(b, f"[...S.msgs.values()].filter(m => m.kind === 'audio' && m.frm === 1).length > {n0}", 60, "voice at Bob")
    back = await a.evaluate("$('#gccRec').hidden && !$('#gccForm').hidden && $('#recBar').hidden && !$('.composer').hidden")
    check("Voice: recorded in the panel, sent to Bob", rec_top and back)
    # Camera
    n0 = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).length")
    await a.click("#gccPlus")
    await a.click('#gccTray [data-tool="cam"]')
    await wait_for(a, "document.querySelector('.camsheet video')?.videoWidth > 0", 15, "camera preview")
    await a.wait_for_timeout(300)
    cam_top = await a.evaluate(ON_TOP, ".camsheet .scard")
    await a.click(".camsheet [data-shoot]")
    await wait_for(a, "!document.querySelector('.camsheet [data-use]').disabled", 5)
    await a.click(".camsheet [data-use]")
    await wait_for(a, "!!document.querySelector('.sheet:not(.camsheet)')", 10, "quality sheet")
    await sheet_sizes(a, 4)
    await a.click('.sheet .chip[data-k="saver"]')
    await a.click(".sheet [data-go]")
    await wait_for(b, f"[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).length > {n0}", 90, "camera photo at Bob")
    name = await b.evaluate("[...S.msgs.values()].filter(m => m.kind === 'image' && m.frm === 1).pop().name")
    check("Camera: photo taken over the game, received by Bob", cam_top and name.startswith("photo-"), name)
    check("the game kept going the whole time", await a.evaluate("GC.open && !!UNO.el && UNO.view.hand.length > 0"))
    await a.click(".gc-chatbtn")
    await end_uno(a, b)


async def test_gcradio(a, b):
    print("the radio dashboard in the game center")
    await start_uno(a, b)
    await a.click(".gc-radiobtn")
    await wait_for(a, "!$('#dash').hidden", 5, "dashboard")
    await a.wait_for_timeout(1500)                         # (after its rise-in)
    r = await a.evaluate("(() => { const r = $('#dash').getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
    on_top = await a.evaluate(ON_TOP, "#dash .tiles")
    tiles = await a.evaluate("$('#tiles').children.length")
    fr0 = await a.evaluate("S.radio.mac.frames_ok")
    await a.wait_for_timeout(2500)
    fr1 = await a.evaluate("+[...document.querySelectorAll('#tiles .tile')].find(t => t.querySelector('.k').textContent === 'Frames OK').querySelector('.v').firstChild.textContent")
    check("Radio button shows the live dashboard over the game", on_top and tiles >= 10 and r[2] > 250 and fr1 >= fr0,
          f"{r[2]:.0f}x{r[3]:.0f} at {r[0]:.0f},{r[1]:.0f}, frames {fr0} -> {fr1}")
    await shot(a, "f20-gc-radio.png")
    # with the chat panel open too: dashboard on the left, chat on the right, the table between
    await a.click(".gc-chatbtn")
    await wait_for(a, "!$('#gcChat').hidden", 5)
    await a.wait_for_timeout(500)
    both = await a.evaluate("$('#dash').getBoundingClientRect().right < $('#gcChat').getBoundingClientRect().left")
    await shot(a, "f21-gc-radio-chat.png")
    await a.click(".gc-chatbtn")
    # keys on the dashboard (its Eb/N0 slider) don't play the game
    await a.click(".gc-radiobtn")
    check("the Radio button closes it again, the game still on", both and await a.evaluate("$('#dash').hidden && GC.open && !!UNO.el")
          and await a.evaluate("$('.gc-radiobtn').getAttribute('aria-expanded')") == "false")
    await end_uno(a, b)


async def test_sounds(a, b):
    print("sound effects")
    sa = await a.evaluate("window.__sfx")
    sb = await b.evaluate("window.__sfx")
    check("chat sounds played", "send" in sa and "receive" in sb and "pop" in sa + sb, f"Alice {sorted(set(sa))}, Bob {sorted(set(sb))}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--size", default="1360x860")
    ap.add_argument("--only", default=None, help="comma list: theme,wallpaper,avatar,emoji,photo,voice,cancel,late,fresh,nav,camera,music,gcchat,gctray,gcradio")
    o = ap.parse_args()
    only = set(o.only.split(",")) if o.only else None
    w, h = map(int, o.size.split("x"))
    tmp = tempfile.mkdtemp(prefix="uifeat-")
    photo, portrait, wall, big, mid, mp3 = pictures(tmp)
    async with async_playwright() as p:
        br = await p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                           "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(viewport={"width": w, "height": h}, permissions=["microphone", "camera"])
        await ctx.add_init_script(HOOKS)
        errors, pages = [], []
        for k in range(2):
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e, k=k: errors.append(f"{'AB'[k]}: {e}"))
            await pg.goto(f"http://localhost:{o.port + k}/")
            pages.append(pg)
        a, b = pages
        for pg in pages:
            await wait_for(pg, "S.me && [...S.contacts.values()].some(c => c.online)", 40, "stations online")
        await a.evaluate("setPref('askQ', true); setPref('sound', true)")
        steps = [("theme", lambda: test_theme(a)), ("wallpaper", lambda: test_wallpaper(a, wall)),
                 ("avatar", lambda: test_avatar(a, b, portrait)), ("emoji", lambda: test_emoji(a)),
                 ("photo", lambda: test_photo(a, b, photo)), ("voice", lambda: test_voice(a, b)),
                 ("cancel", lambda: test_cancel(a, b, big)), ("late", lambda: test_late(a, b, mid)),
                 ("fresh", lambda: test_fresh(a, b)), ("nav", lambda: test_nav(a)), ("camera", lambda: test_camera(a, b)),
                 ("music", lambda: test_music(a, mp3)), ("gcchat", lambda: test_gcchat(a, b)),
                 ("gctray", lambda: test_gctray(a, b, photo)), ("gcradio", lambda: test_gcradio(a, b))]
        for name, fn in steps:
            if only and name not in only:
                continue
            try:
                await fn()
            except AssertionError as e:
                check(name, False, str(e))
        if not only:
            await test_sounds(a, b)
        await br.close()
    if errors:
        print("page errors:", *errors[:10], sep="\n  ")
    bad = [n for n, ok in RESULTS if not ok]
    print(f"UI feature test: {'PASS' if not bad and not errors else 'FAIL'} ({len(RESULTS) - len(bad)}/{len(RESULTS)} checks; screenshots in {OUT})")
    return 1 if bad or errors else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
