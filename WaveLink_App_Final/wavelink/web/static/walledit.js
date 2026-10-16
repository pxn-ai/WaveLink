"use strict";
/* The wallpaper editor.  Uploading a picture for the chat's background (or pressing Edit… later)
   opens it with a preview of the chat as it will look:

   Crop    the frame starts in the shape of your chat window, as large as the picture allows.  Drag
           it to move it, drag a corner to resize it, scroll or pinch to zoom (arrow keys and + / −
           work too).  "Free" lets it take any shape, "Whole picture" keeps all of it.
   Rotate  a quarter turn either way, and a mirror flip.
   Adjust  brightness, contrast, saturation, blur, and how much the chat colour dims it.
   Fit     what happens when the chat window isn't the frame's shape (it changes with the window):
           crop to fit, fit inside, stretch to fit, or normal (actual size).

   The picture shown in the chat is made here (at most 2560 px).  The station keeps the original
   and these settings (pref "wallEdit"), so Edit… starts again from the original. */

const WALLED = { MAX: 2560, WORK: 1400, st: null };
WALLED.FITS = [
  ["cover", "Crop to fit", "Fills the chat; what doesn't fit is cut off at the edges."],
  ["contain", "Fit inside", "All of the picture, with bands of the chat colour beside it."],
  ["stretch", "Stretch to fit", "Fills the chat exactly: the picture is squeezed into its shape."],
  ["normal", "Normal", "Actual size, in the middle, not scaled."],
];
WALLED.ADJ = [  // key, label, min, max, step, default
  ["b", "Brightness", 50, 150, 1, 100],
  ["c", "Contrast", 50, 150, 1, 100],
  ["s", "Saturation", 0, 200, 1, 100],
  ["blur", "Blur", 0, 24, 0.5, 0],
];
WALLED.ADJ0 = Object.fromEntries(WALLED.ADJ.map((a) => [a[0], a[5]]));
WALLED.canFilter = (() => {
  try { const g = document.createElement("canvas").getContext("2d"); g.filter = "blur(2px)"; return g.filter === "blur(2px)"; }
  catch (e) { return false; }
})();
// the blur is measured on a picture 1600 px across, so it looks the same whatever the picture's size
WALLED.filter = (a, blurPx = 0) =>
  `brightness(${a.b}%) contrast(${a.c}%) saturate(${a.s}%)${blurPx > 0.05 ? ` blur(${blurPx.toFixed(2)}px)` : ""}`;
WALLED.defaultDim = () => (THEME.mode() === "dark" ? 0.55 : 0.35);

// the chat's message area as it will be once the settings drawer is closed
WALLED.chatSize = function () {
  if (matchMedia("(max-width: 760px)").matches) return { w: innerWidth, h: Math.max(200, innerHeight - 122) };
  let w, h;
  const mw = $(".msgwrap"), r = mw && !$("#conv").hidden ? mw.getBoundingClientRect() : null;
  if (r && r.width > 80 && r.height > 80) { w = r.width; h = r.height; }
  else { const m = $("#main").getBoundingClientRect(); w = m.width; h = m.height - 122; }
  if (!$("#settings").hidden) w += $("#settings").getBoundingClientRect().width;
  return { w: Math.round(w), h: Math.max(200, Math.round(h)) };
};

// ------------------------------------------------------------------ geometry (in the turned picture's pixels)
WALLED.turned = (st) => (st.rot % 180 ? [st.sh, st.sw] : [st.sw, st.sh]);
// draw the source turned and flipped, in the coordinates of the turned picture (RW x RH)
WALLED.orient = function (g, st) {
  if (st.flip) { g.translate(st.RW, 0); g.scale(-1, 1); }
  if (st.rot === 90) { g.translate(st.RW, 0); g.rotate(Math.PI / 2); }
  else if (st.rot === 180) { g.translate(st.RW, st.RH); g.rotate(Math.PI); }
  else if (st.rot === 270) { g.translate(0, st.RH); g.rotate(-Math.PI / 2); }
  g.drawImage(st.img, 0, 0);
};
// the largest frame of shape ar (w / h) inside the picture
WALLED.maxFrame = (st, ar) => { const w = Math.min(st.RW, st.RH * ar); return { w, h: w / ar }; };
WALLED.minSide = (st) => Math.max(24, Math.min(st.RW, st.RH) * 0.04);
WALLED.clampPos = function (st, c) {
  c.w = Math.min(c.w, st.RW); c.h = Math.min(c.h, st.RH);
  c.x = Math.max(0, Math.min(st.RW - c.w, c.x)); c.y = Math.max(0, Math.min(st.RH - c.h, c.y));
  return c;
};
WALLED.centred = function (st, ar, zoom = 1, cx = st.RW / 2, cy = st.RH / 2) {
  const m = WALLED.maxFrame(st, ar), w = m.w / zoom, h = m.h / zoom;
  return WALLED.clampPos(st, { x: cx - w / 2, y: cy - h / 2, w, h });
};
WALLED.ar = (st) => (st.mode === "chat" ? st.chat.w / st.chat.h : st.mode === "whole" ? st.RW / st.RH : st.crop.w / st.crop.h);
WALLED.zoomOf = (st) => WALLED.maxFrame(st, st.crop.w / st.crop.h).w / st.crop.w;
// zoom about the frame's centre, keeping its shape
WALLED.setZoom = function (st, z) {
  const ar = st.crop.w / st.crop.h, m = WALLED.maxFrame(st, ar), min = WALLED.minSide(st);
  z = Math.max(1, Math.min(z, m.w / min, m.h / min));
  st.crop = WALLED.centred(st, ar, z, st.crop.x + st.crop.w / 2, st.crop.y + st.crop.h / 2);
};
WALLED.outSize = (st) => {
  const os = Math.min(1, WALLED.MAX / Math.max(st.crop.w, st.crop.h));
  return { os, w: Math.max(1, Math.round(st.crop.w * os)), h: Math.max(1, Math.round(st.crop.h * os)) };
};

// a quarter turn (dir +1 clockwise) or a flip: the frame keeps the same part of the picture
WALLED.turn = function (st, dir) {
  const c = st.crop, RW = st.RW, RH = st.RH;
  // (the mirror is applied after the turn, so a mirrored picture turns the other way in the source)
  st.rot = (st.rot + ((dir > 0) !== st.flip ? 90 : 270)) % 360;
  [st.RW, st.RH] = WALLED.turned(st);
  st.crop = dir > 0 ? { x: RH - (c.y + c.h), y: c.x, w: c.h, h: c.w } : { x: c.y, y: RW - (c.x + c.w), w: c.h, h: c.w };
  WALLED.reshape(st);
  WALLED.buildWork(st);
};
WALLED.flip = function (st) {
  st.flip = !st.flip;
  st.crop.x = st.RW - (st.crop.x + st.crop.w);
  WALLED.buildWork(st);
};
// the frame back in its mode's shape (after a turn, or a change of mode), at the same zoom and centre
WALLED.reshape = function (st) {
  if (st.mode === "whole") { st.crop = { x: 0, y: 0, w: st.RW, h: st.RH }; return; }
  if (st.mode === "free") { WALLED.clampPos(st, st.crop); return; }
  const z = Math.min(WALLED.zoomOf(st), 8);
  st.crop = WALLED.centred(st, WALLED.ar(st), z, st.crop.x + st.crop.w / 2, st.crop.y + st.crop.h / 2);
};

// the picture turned and flipped, at most 1400 px, for drawing while editing
WALLED.buildWork = function (st) {
  const ws = Math.min(1, WALLED.WORK / Math.max(st.RW, st.RH));
  const c = st.work || document.createElement("canvas");
  c.width = Math.max(1, Math.round(st.RW * ws)); c.height = Math.max(1, Math.round(st.RH * ws));
  const g = c.getContext("2d");
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.imageSmoothingQuality = "high";
  g.scale(c.width / st.RW, c.height / st.RH);
  WALLED.orient(g, st);
  st.work = c; st.ws = c.width / st.RW;
  st.dirtyImg = true;
};

// ------------------------------------------------------------------ the finished picture
WALLED.render = async function (st) {
  const { os, w, h } = WALLED.outSize(st);
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  const g = c.getContext("2d");
  g.imageSmoothingQuality = "high";
  const draw = () => { g.save(); g.scale(os, os); g.translate(-st.crop.x, -st.crop.y); WALLED.orient(g, st); g.restore(); };
  const a = st.adj, blur = a.blur * Math.max(w, h) / 1600, plain = a.b === 100 && a.c === 100 && a.s === 100;
  if (WALLED.canFilter) {
    g.filter = WALLED.filter(a);
    draw();
    // the blurred picture over the sharp one, so its edges don't fade out
    if (blur > 0.05) { g.filter = WALLED.filter(a, blur); draw(); }
    g.filter = "none";
  } else {
    draw();
    if (!plain) WALLED.adjustPixels(g, w, h, a);
    if (blur > 0.5) {                          // a blur by scaling down and up again
      const f = Math.min(16, 1 + blur / 2), t = document.createElement("canvas");
      t.width = Math.max(1, Math.round(w / f)); t.height = Math.max(1, Math.round(h / f));
      t.getContext("2d").drawImage(c, 0, 0, t.width, t.height);
      g.imageSmoothingQuality = "high";
      g.drawImage(t, 0, 0, w, h);
    }
  }
  let blob = await MEDIA.canvasBlob(c, "image/webp", 0.86);
  if (!blob || blob.type !== "image/webp") blob = await MEDIA.canvasBlob(c, "image/jpeg", 0.88);
  return { blob, w, h };
};
// brightness, contrast and saturation as CSS does them (for browsers without canvas filters)
WALLED.adjustPixels = function (g, w, h, a) {
  const d = g.getImageData(0, 0, w, h), p = d.data, B = a.b / 100, C = a.c / 100, s = a.s / 100;
  const m = [0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s, 0.213 - 0.213 * s, 0.715 + 0.285 * s,
    0.072 - 0.072 * s, 0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s];
  const cl = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  for (let i = 0; i < p.length; i += 4) {
    const r = cl((cl(p[i] / 255 * B) - 0.5) * C + 0.5), gg = cl((cl(p[i + 1] / 255 * B) - 0.5) * C + 0.5), b = cl((cl(p[i + 2] / 255 * B) - 0.5) * C + 0.5);
    p[i] = 255 * cl(m[0] * r + m[1] * gg + m[2] * b);
    p[i + 1] = 255 * cl(m[3] * r + m[4] * gg + m[5] * b);
    p[i + 2] = 255 * cl(m[6] * r + m[7] * gg + m[8] * b);
  }
  g.putImageData(d, 0, 0);
};

// ------------------------------------------------------------------ opening the editor
// WALLED.open(file): a new picture.  WALLED.open(): the wallpaper you have, from its original.
WALLED.open = async function (file) {
  if (WALLED.st || WALLED.loading) return;
  WALLED.loading = true;
  let blob = file, fresh = true, saved = null;
  try {
    if (!file) {
      const p = S.profile || {}, src = p.wallpaper_src_url;
      if (!src && !p.wallpaper_url) { $("#wallFile").click(); return; }
      blob = await fetch(src || p.wallpaper_url).then((r) => { if (!r.ok) throw new Error("the wallpaper could not be loaded"); return r.blob(); });
      fresh = !src;                              // (an older wallpaper without its original: it becomes the original)
      saved = src ? pref("wallEdit") : null;
    }
    const img = await MEDIA.loadImage(blob);
    const st = WALLED.st = {
      img, blob, fresh, name: file?.name || "wallpaper", sw: img.naturalWidth, sh: img.naturalHeight,
      rot: 0, flip: false, mode: file ? "chat" : "whole", adj: { ...WALLED.ADJ0 }, fit: pref("wallFit") || "cover",
      dim: pref("wallDim") ?? WALLED.defaultDim(), chat: WALLED.chatSize(), tab: "crop",
    };
    if (saved && typeof saved === "object") {
      if ([0, 90, 180, 270].includes(saved.rot)) st.rot = saved.rot;
      st.flip = !!saved.flip;
      if (["chat", "free", "whole"].includes(saved.mode)) st.mode = saved.mode;
      for (const [k, , lo, hi] of WALLED.ADJ) if (typeof saved.adj?.[k] === "number") st.adj[k] = Math.max(lo, Math.min(hi, saved.adj[k]));
    }
    [st.RW, st.RH] = WALLED.turned(st);
    st.crop = { x: 0, y: 0, w: st.RW, h: st.RH };
    if (saved && Array.isArray(saved.crop) && saved.crop.length === 4 && saved.crop.every((v) => typeof v === "number")) {
      const [x, y, w, h] = saved.crop;
      st.crop = WALLED.clampPos(st, { x: x * st.RW, y: y * st.RH, w: Math.max(0.02, w) * st.RW, h: Math.max(0.02, h) * st.RH });
    }
    WALLED.reshape(st);                          // ("Chat window": to the window's shape now, same middle and zoom)
    WALLED.buildWork(st);
    WALLED.sheet(st);
  } catch (e) {
    WALLED.st = null;
    toast(e.message || "That picture could not be read");
  } finally { WALLED.loading = false; }
};

WALLED.sheet = function (st) {
  const el = st.el = document.createElement("div");
  el.className = "sheet wallsheet";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  el.setAttribute("aria-label", "Wallpaper");
  const tab = (id, label, icon) => `<button role="tab" data-tab="${id}" aria-selected="${id === st.tab}"><svg viewBox="0 0 24 24">${icon}</svg>${label}</button>`;
  const ratio = `${st.chat.w}×${st.chat.h}`;
  el.innerHTML = `<div class="scard">
    <h3>Chat wallpaper<small> · ${esc(st.name)}, ${st.sw}×${st.sh}</small></h3>
    <div class="we-main">
      <div class="we-stage"><canvas class="we-img"></canvas><canvas class="we-ui" tabindex="0"
        aria-label="Crop frame: drag to move, drag a corner to resize, scroll to zoom; arrow keys move it, plus and minus zoom"></canvas></div>
      <div class="we-side">
        <div class="we-plabel">In the chat <span class="muted"></span></div>
        <div class="we-prev"><canvas></canvas><div class="dim"></div>
          <div class="bubs"><span class="we-bub">Are you on the air?</span><span class="we-bub mine">Loud and clear 📡</span><span class="we-bub">New wallpaper?</span></div></div>
      </div>
    </div>
    <div class="we-tabs" role="tablist">
      ${tab("crop", "Crop", '<path d="M7 2v15h15v2h-4v3h-2v-3H5V7H2V5h3V2zm2 3h8v8h-2V7H9z"/>')}
      ${tab("rotate", "Rotate", '<path d="M12 4a8 8 0 1 1-7.4 5h2.2A6 6 0 1 0 12 6v3L7.5 5 12 1z"/>')}
      ${tab("adjust", "Adjust", '<path d="M4 7h9.2a3 3 0 0 1 5.6 0H20v2h-1.2a3 3 0 0 1-5.6 0H4zm12 1a1 1 0 1 0 0 0zM4 15h1.2a3 3 0 0 1 5.6 0H20v2h-9.2a3 3 0 0 1-5.6 0H4z"/>')}
      ${tab("fit", "Fit", '<path d="M3 5h6v2H5v4H3zm12 0h6v6h-2V7h-4zM3 13h2v4h4v2H3zm16 0h2v6h-6v-2h4z"/>')}
    </div>
    <div class="we-panel" data-p="crop">
      <div class="we-row"><span class="lbl">Shape</span><div class="seg we-mode" role="group" aria-label="Frame shape">
        <button data-m="chat" title="The shape of your chat window (${ratio})">Chat window</button><button data-m="free">Free</button><button data-m="whole">Whole picture</button></div></div>
      <div class="we-row"><label for="weZoom" class="lbl">Zoom</label><input type="range" id="weZoom" min="0" max="100" step="1"><output></output>
        <button class="smallbtn" data-reset-crop>Reset</button></div>
      <p class="we-hint">Drag the frame to move it, drag a corner to resize it; scroll or pinch to zoom.</p>
    </div>
    <div class="we-panel" data-p="rotate" hidden>
      <div class="we-row we-turns">
        <button class="smallbtn" data-turn="-1"><svg viewBox="0 0 24 24"><path d="M12 4a8 8 0 1 1-7.4 5h2.2A6 6 0 1 0 12 6v3L7.5 5 12 1z" transform="matrix(-1 0 0 1 24 0)"/></svg>Turn left</button>
        <button class="smallbtn" data-turn="1"><svg viewBox="0 0 24 24"><path d="M12 4a8 8 0 1 1-7.4 5h2.2A6 6 0 1 0 12 6v3L7.5 5 12 1z"/></svg>Turn right</button>
        <button class="smallbtn" data-flip aria-pressed="false"><svg viewBox="0 0 24 24"><path d="M11 2h2v20h-2zM3 18 9 6v12zm18 0h-6V6zm-2-2V11l-2.5 5z"/></svg>Mirror</button>
      </div>
      <p class="we-hint we-rotinfo"></p>
    </div>
    <div class="we-panel" data-p="adjust" hidden>
      ${WALLED.ADJ.map(([k, l, lo, hi, step]) => `<div class="we-row"><label class="lbl" for="weA${k}">${l}</label><input type="range" id="weA${k}" data-a="${k}" min="${lo}" max="${hi}" step="${step}"><output></output></div>`).join("")}
      <div class="we-row"><label class="lbl" for="weDim">Dim</label><input type="range" id="weDim" min="0" max="0.85" step="0.05"><output></output></div>
      <div class="we-row"><span class="we-hint">Dim lays the chat colour over the picture so the messages stay easy to read.</span><button class="smallbtn" data-reset-adj>Reset</button></div>
    </div>
    <div class="we-panel" data-p="fit" hidden>
      <div class="we-fits" role="radiogroup" aria-label="How the picture fits the chat">${WALLED.FITS.map(([id, l]) =>
        `<button role="radio" data-fit="${id}" aria-checked="false">${WALLED.fitIcon(id)}<span>${l}</span></button>`).join("")}</div>
      <p class="we-hint we-fithint"></p>
    </div>
    <div class="acts"><span class="we-out muted"></span><button data-x>Cancel</button><button class="primary" data-go>Set wallpaper</button></div>
  </div>`;
  document.body.appendChild(el);
  st.prevFocus = document.activeElement;
  WALLED.wire(st);
  WALLED.layout(st);
  WALLED.sync(st);
  el.querySelector(".we-ui").focus({ preventScroll: true });
};

// little pictures of the four fits: the chat window (outline) and the picture (filled)
WALLED.fitIcon = function (id) {
  const pic = { cover: '<rect x="-6" y="-4" width="68" height="46"/>', contain: '<rect x="13" y="0" width="30" height="38"/>',
    stretch: '<rect x="0" y="0" width="56" height="38"/><path d="M8 19h40M8 19l5-4M8 19l5 4M48 19l-5-4M48 19l-5 4" stroke="currentColor" stroke-width="2" fill="none" opacity=".9"/>',
    normal: '<rect x="18" y="11" width="20" height="16"/>' }[id];
  return `<svg class="fiticon" viewBox="-2 -2 60 42"><defs><clipPath id="fc-${id}"><rect x="0" y="0" width="56" height="38" rx="4"/></clipPath></defs>
    <g clip-path="url(#fc-${id})" class="pic">${pic}</g><rect x="0" y="0" width="56" height="38" rx="4" class="frame"/></svg>`;
};

// ------------------------------------------------------------------ sizes and drawing
WALLED.layout = function (st) {
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  // the preview in the chat window's shape, at most 240 x 300 (first: its width decides the stage's)
  const prev = st.el.querySelector(".we-prev"), narrow = matchMedia("(max-width: 680px)").matches;
  const maxW = narrow ? 150 : 240, maxH = narrow ? 160 : 300;
  let pw = maxW, ph = pw * st.chat.h / st.chat.w;
  if (ph > maxH) { ph = maxH; pw = ph * st.chat.w / st.chat.h; }
  prev.style.width = `${Math.round(pw)}px`; prev.style.height = `${Math.round(ph)}px`;
  const pc = prev.querySelector("canvas");
  pc.width = Math.round(pw * dpr); pc.height = Math.round(ph * dpr);
  for (const cv of st.el.querySelectorAll(".we-stage canvas")) {      // (layout size: the sheet may still be rising in)
    cv.width = Math.max(1, Math.round(cv.clientWidth * dpr)); cv.height = Math.max(1, Math.round(cv.clientHeight * dpr));
  }
  st.dpr = dpr;
  st.dirtyImg = true;
};

WALLED.paint = function (st) {
  if (st.raf) return;
  st.raf = requestAnimationFrame(() => { st.raf = 0; if (WALLED.st === st) WALLED.draw(st); });
};

WALLED.draw = function (st) {
  const img = st.el.querySelector(".we-img"), ui = st.el.querySelector(".we-ui"), dpr = st.dpr;
  const W = img.width / dpr, H = img.height / dpr, pad = 14;
  const ds = Math.min((W - 2 * pad) / st.RW, (H - 2 * pad) / st.RH);
  st.view = { ds, ox: (W - st.RW * ds) / 2, oy: (H - st.RH * ds) / 2 };
  const v = st.view;
  if (st.dirtyImg) {
    const g = img.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.imageSmoothingQuality = "high";
    g.drawImage(st.work, v.ox, v.oy, st.RW * v.ds, st.RH * v.ds);
    st.dirtyImg = false;
  }
  img.style.filter = WALLED.filter(st.adj, st.adj.blur * Math.max(st.crop.w, st.crop.h) * v.ds / 1600);
  // the frame: everything outside it darker, thirds, corner handles
  const g = ui.getContext("2d"), c = st.crop;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const x = v.ox + c.x * v.ds, y = v.oy + c.y * v.ds, w = c.w * v.ds, h = c.h * v.ds;
  g.fillStyle = "rgba(4, 8, 18, .58)";
  g.beginPath(); g.rect(0, 0, W, H); g.rect(x, y, w, h); g.fill("evenodd");
  g.strokeStyle = "rgba(255, 255, 255, .35)"; g.lineWidth = 1;
  g.beginPath();
  for (const k of [1, 2]) { g.moveTo(x + (w * k) / 3, y); g.lineTo(x + (w * k) / 3, y + h); g.moveTo(x, y + (h * k) / 3); g.lineTo(x + w, y + (h * k) / 3); }
  g.stroke();
  g.strokeStyle = "#fff"; g.lineWidth = 1.5; g.strokeRect(x, y, w, h);
  const acc = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#1f6fe5", L = Math.min(18, w / 3, h / 3);
  g.lineWidth = 4; g.lineCap = "round";
  for (const [cx, cy, sx, sy] of [[x, y, 1, 1], [x + w, y, -1, 1], [x, y + h, 1, -1], [x + w, y + h, -1, -1]]) {
    g.strokeStyle = "#fff"; g.beginPath(); g.moveTo(cx + sx * L, cy); g.lineTo(cx, cy); g.lineTo(cx, cy + sy * L); g.stroke();
    g.strokeStyle = acc; g.lineWidth = 2; g.beginPath(); g.moveTo(cx + sx * L, cy); g.lineTo(cx, cy); g.lineTo(cx, cy + sy * L); g.stroke();
    g.lineWidth = 4;
  }
  WALLED.drawPreview(st);
};

// the chat window in miniature: the picture as the chosen fit lays it out, dimmed, with a few bubbles
WALLED.drawPreview = function (st) {
  const prev = st.el.querySelector(".we-prev"), cv = prev.querySelector("canvas"), dpr = st.dpr;
  const PW = cv.width / dpr, PH = cv.height / dpr, out = WALLED.outSize(st), k = PW / st.chat.w;
  let dw, dh;
  if (st.fit === "stretch") { dw = PW; dh = PH; }
  else {
    const s = st.fit === "cover" ? Math.max(PW / out.w, PH / out.h) : st.fit === "contain" ? Math.min(PW / out.w, PH / out.h) : k;
    dw = out.w * s; dh = out.h * s;
  }
  const g = cv.getContext("2d"), c = st.crop, ws = st.ws;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, PW, PH);
  g.imageSmoothingQuality = "high";
  g.drawImage(st.work, c.x * ws, c.y * ws, c.w * ws, c.h * ws, (PW - dw) / 2, (PH - dh) / 2, dw, dh);
  cv.style.filter = WALLED.filter(st.adj, st.adj.blur * Math.max(out.w, out.h) / 1600 * (dw / out.w));
  prev.querySelector(".dim").style.opacity = String(st.dim);
};

// controls follow the state
WALLED.sync = function (st) {
  const el = st.el;
  el.querySelectorAll(".we-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === st.tab)));
  el.querySelectorAll(".we-panel").forEach((p) => (p.hidden = p.dataset.p !== st.tab));
  el.querySelectorAll(".we-mode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.m === st.mode)));
  const z = WALLED.zoomOf(st), zi = el.querySelector("#weZoom");
  if (document.activeElement !== zi) zi.value = String(Math.round(100 * Math.log(z) / Math.log(8)));
  zi.disabled = st.mode === "whole";
  zi.nextElementSibling.textContent = `${z.toFixed(z < 10 ? 2 : 1)}×`;
  for (const [k] of WALLED.ADJ) {
    const i = el.querySelector(`#weA${k}`);
    if (document.activeElement !== i) i.value = String(st.adj[k]);
    i.nextElementSibling.textContent = k === "blur" ? String(st.adj[k]) : `${st.adj[k]}%`;
  }
  const di = el.querySelector("#weDim");
  if (document.activeElement !== di) di.value = String(st.dim);
  di.nextElementSibling.textContent = `${Math.round(st.dim * 100)}%`;
  el.querySelector("[data-flip]").setAttribute("aria-pressed", String(st.flip));
  el.querySelector(".we-rotinfo").textContent = `${st.rot ? `Turned ${st.rot}° clockwise` : "Not turned"}${st.flip ? ", mirrored" : ""}. The frame keeps the same part of the picture.`;
  el.querySelectorAll(".we-fits button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.fit === st.fit)));
  el.querySelector(".we-fithint").textContent = WALLED.FITS.find((f) => f[0] === st.fit)[2] +
    (st.fit === "cover" && Math.abs(st.crop.w / st.crop.h - st.chat.w / st.chat.h) < 0.02 ? " (The frame has the chat's shape, so nothing more is cut off.)" : "");
  const out = WALLED.outSize(st);
  el.querySelector(".we-out").textContent = `Saved at ${out.w}×${out.h}`;
  el.querySelector(".we-plabel .muted").textContent = WALLED.FITS.find((f) => f[0] === st.fit)[1].toLowerCase();
  WALLED.paint(st);
};

// ------------------------------------------------------------------ mouse, touch and keys
WALLED.wire = function (st) {
  const el = st.el, ui = el.querySelector(".we-ui");
  const toImg = (e) => {
    const r = ui.getBoundingClientRect(), v = st.view, k = ui.clientWidth / Math.max(1, r.width);
    const sx = (e.clientX - r.left) * k, sy = (e.clientY - r.top) * k;
    return { x: (sx - v.ox) / v.ds, y: (sy - v.oy) / v.ds, sx, sy };
  };
  // which corner (0 tl, 1 tr, 2 bl, 3 br) is under the pointer, or "in" for inside the frame
  const hit = (p, touch) => {
    const v = st.view, c = st.crop, R = touch ? 24 : 15;
    const pts = [[c.x, c.y], [c.x + c.w, c.y], [c.x, c.y + c.h], [c.x + c.w, c.y + c.h]];
    for (let i = 0; i < 4; i++) if (Math.hypot(v.ox + pts[i][0] * v.ds - p.sx, v.oy + pts[i][1] * v.ds - p.sy) < R) return i;
    return p.x >= c.x && p.x <= c.x + c.w && p.y >= c.y && p.y <= c.y + c.h ? "in" : null;
  };
  const ptrs = new Map();
  let drag = null, pinch = null;
  ui.addEventListener("pointerdown", (e) => {
    ui.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (ptrs.size === 2) {
      const [a, b] = [...ptrs.values()];
      pinch = { d0: Math.hypot(a.x - b.x, a.y - b.y), z0: WALLED.zoomOf(st) }; drag = null; return;
    }
    if (st.mode === "whole") return;
    const p = toImg(e), h = hit(p, e.pointerType === "touch"), c = st.crop;
    if (typeof h === "number") {
      const ax = h % 2 ? c.x : c.x + c.w, ay = h < 2 ? c.y + c.h : c.y;       // the opposite corner stays put
      drag = { k: "corner", ax, ay, sx: h % 2 ? 1 : -1, sy: h < 2 ? -1 : 1 };
    } else {
      // inside: move the frame; outside: the frame jumps there first
      if (h !== "in") { st.crop = WALLED.clampPos(st, { ...c, x: p.x - c.w / 2, y: p.y - c.h / 2 }); WALLED.sync(st); }
      drag = { k: "move", p0: p, c0: { ...st.crop } };
    }
  });
  ui.addEventListener("pointermove", (e) => {
    if (ptrs.has(e.pointerId)) ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && ptrs.size === 2) {
      const [a, b] = [...ptrs.values()];
      WALLED.setZoom(st, pinch.z0 * Math.hypot(a.x - b.x, a.y - b.y) / Math.max(1, pinch.d0));
      if (st.mode === "whole") st.mode = "free";
      WALLED.sync(st); return;
    }
    const p = toImg(e);
    if (!drag) {
      const h = st.mode === "whole" ? null : hit(p, e.pointerType === "touch");
      ui.style.cursor = h === 0 || h === 3 ? "nwse-resize" : h === 1 || h === 2 ? "nesw-resize" : h === "in" ? "move" : st.mode === "whole" ? "default" : "crosshair";
      return;
    }
    if (drag.k === "move") {
      st.crop = WALLED.clampPos(st, { ...drag.c0, x: drag.c0.x + p.x - drag.p0.x, y: drag.c0.y + p.y - drag.p0.y });
    } else {
      const maxW = drag.sx > 0 ? st.RW - drag.ax : drag.ax, maxH = drag.sy > 0 ? st.RH - drag.ay : drag.ay, min = WALLED.minSide(st);
      let dx = Math.max(0, (p.x - drag.ax) * drag.sx), dy = Math.max(0, (p.y - drag.ay) * drag.sy), w, h;
      if (st.mode === "free") { w = Math.max(min, Math.min(maxW, dx)); h = Math.max(min, Math.min(maxH, dy)); }
      else {
        const ar = WALLED.ar(st);
        w = Math.max(dx, dy * ar);
        w = Math.max(Math.max(min, min * ar), Math.min(w, maxW, maxH * ar)); h = w / ar;
      }
      st.crop = { x: drag.sx > 0 ? drag.ax : drag.ax - w, y: drag.sy > 0 ? drag.ay : drag.ay - h, w, h };
    }
    WALLED.sync(st);
  });
  const up = (e) => { ptrs.delete(e.pointerId); if (ptrs.size < 2) pinch = null; if (!ptrs.size) drag = null; };
  ui.addEventListener("pointerup", up);
  ui.addEventListener("pointercancel", up);
  ui.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (st.mode === "whole") st.mode = "free";
    WALLED.setZoom(st, WALLED.zoomOf(st) * Math.exp(-e.deltaY * 0.0015));
    WALLED.sync(st);
  }, { passive: false });
  ui.addEventListener("dblclick", () => { WALLED.reshape(st); st.crop = WALLED.centred(st, WALLED.ar(st)); WALLED.sync(st); });
  ui.addEventListener("keydown", (e) => {
    const step = (e.shiftKey ? 0.05 : 0.01) * Math.max(st.RW, st.RH);
    const mv = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (mv && st.mode !== "whole") { st.crop = WALLED.clampPos(st, { ...st.crop, x: st.crop.x + mv[0], y: st.crop.y + mv[1] }); }
    else if (e.key === "+" || e.key === "=") { if (st.mode === "whole") st.mode = "free"; WALLED.setZoom(st, WALLED.zoomOf(st) * 1.1); }
    else if (e.key === "-" || e.key === "_") WALLED.setZoom(st, WALLED.zoomOf(st) / 1.1);
    else return;
    e.preventDefault();
    WALLED.sync(st);
  });
  // tabs and panels
  el.querySelectorAll(".we-tabs button").forEach((b) => (b.onclick = () => { st.tab = b.dataset.tab; WALLED.sync(st); }));
  el.querySelector(".we-tabs").addEventListener("keydown", (e) => {
    const all = [...el.querySelectorAll(".we-tabs button")], i = all.findIndex((b) => b.dataset.tab === st.tab);
    const j = e.key === "ArrowRight" ? (i + 1) % all.length : e.key === "ArrowLeft" ? (i + all.length - 1) % all.length : -1;
    if (j >= 0) { e.preventDefault(); st.tab = all[j].dataset.tab; WALLED.sync(st); all[j].focus(); }
  });
  el.querySelectorAll(".we-mode button").forEach((b) => (b.onclick = () => {
    st.mode = b.dataset.m;
    if (st.mode === "chat") st.crop = WALLED.centred(st, WALLED.ar(st)); else WALLED.reshape(st);
    WALLED.sync(st);
  }));
  el.querySelector("#weZoom").oninput = (e) => {
    if (st.mode === "whole") return;
    WALLED.setZoom(st, 8 ** (+e.target.value / 100)); WALLED.sync(st);
  };
  el.querySelector("[data-reset-crop]").onclick = () => { if (st.mode === "free") st.mode = "chat"; st.crop = WALLED.centred(st, WALLED.ar(st)); WALLED.sync(st); };
  el.querySelectorAll("[data-turn]").forEach((b) => (b.onclick = () => { WALLED.turn(st, +b.dataset.turn); WALLED.sync(st); }));
  el.querySelector("[data-flip]").onclick = () => { WALLED.flip(st); WALLED.sync(st); };
  el.querySelectorAll("[data-a]").forEach((i) => (i.oninput = () => { st.adj[i.dataset.a] = +i.value; WALLED.sync(st); }));
  el.querySelector("#weDim").oninput = (e) => { st.dim = +e.target.value; st.dimSet = true; WALLED.sync(st); };
  el.querySelector("[data-reset-adj]").onclick = () => { st.adj = { ...WALLED.ADJ0 }; st.dim = WALLED.defaultDim(); st.dimSet = true; WALLED.sync(st); };
  el.querySelectorAll(".we-fits button").forEach((b) => (b.onclick = () => { st.fit = b.dataset.fit; WALLED.sync(st); }));
  // closing
  el.querySelector("[data-x]").onclick = () => WALLED.close(st);
  el.addEventListener("pointerdown", (e) => { if (e.target === el) WALLED.close(st); });
  el.querySelector("[data-go]").onclick = () => WALLED.apply(st);
  st.onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); WALLED.close(st); }
    else if (e.key === "Enter" && !e.target.matches?.("button, input")) { e.preventDefault(); WALLED.apply(st); }
  };
  document.addEventListener("keydown", st.onKey, true);
  st.onResize = () => { WALLED.layout(st); WALLED.paint(st); };
  window.addEventListener("resize", st.onResize);
  // (and whenever the stage itself changes size)
  if (window.ResizeObserver) {
    let last = "";
    st.ro = new ResizeObserver(() => {
      const s = el.querySelector(".we-stage"), k = `${s.clientWidth}x${s.clientHeight}`;
      if (k !== last) { last = k; st.onResize(); }
    });
    st.ro.observe(el.querySelector(".we-stage"));
  }
};

WALLED.close = function (st) {
  cancelAnimationFrame(st.raf);
  document.removeEventListener("keydown", st.onKey, true);
  window.removeEventListener("resize", st.onResize);
  st.ro?.disconnect();
  st.el.remove();
  URL.revokeObjectURL(st.img.src);
  st.prevFocus?.focus?.();
  if (WALLED.st === st) WALLED.st = null;
};

// ------------------------------------------------------------------ saving
WALLED.apply = async function (st) {
  const go = st.el.querySelector("[data-go]");
  if (go.disabled) return;
  go.disabled = true; go.textContent = "Saving…";
  try {
    const out = await WALLED.render(st);
    // the picture for the chat (a new one drops the old original, unless this is an edit of it)
    S.profile = await api(`/api/wallpaper?mime=${encodeURIComponent(out.blob.type)}${st.fresh ? "" : "&keep_src=1"}`, out.blob, true);
    if (st.fresh) {
      // and the original, so Edit… can start from it (re-encoded if it is very large or an odd format)
      let src = st.blob, mime = st.blob.type || "image/jpeg";
      if (src.size > 11.5e6 || !/^image\/(jpeg|png|webp|gif|avif)$/.test(mime) || Math.max(st.sw, st.sh) > 6000) {
        const s = Math.min(1, 4096 / Math.max(st.sw, st.sh)), c = document.createElement("canvas");
        c.width = Math.round(st.sw * s); c.height = Math.round(st.sh * s);
        c.getContext("2d").drawImage(st.img, 0, 0, c.width, c.height);
        src = await MEDIA.canvasBlob(c, "image/webp", 0.9);
        if (!src || src.type !== "image/webp") src = await MEDIA.canvasBlob(c, "image/jpeg", 0.9);
        mime = src.type;
      }
      try { S.profile = await api(`/api/wallpaper?src=1&mime=${encodeURIComponent(mime)}`, src, true); }
      catch (e) { /* the wallpaper is set; Edit… will start from it instead */ }
    }
    const n = (v) => Math.round(v * 1e4) / 1e4;
    setPref("wallEdit", { rot: st.rot, flip: st.flip, mode: st.mode, adj: { ...st.adj },
      crop: [n(st.crop.x / st.RW), n(st.crop.y / st.RH), n(st.crop.w / st.RW), n(st.crop.h / st.RH)] });
    setPref("wallFit", st.fit);
    if (st.dimSet) setPref("wallDim", st.dim);
    setPref("wall", "custom");                     // (overrides a choice still waiting to be posted)
    WALLED.close(st);
    THEME.wall();
    SETTINGS.refresh();
    toast(`Wallpaper set: ${out.w}×${out.h}, ${fmtBytes(out.blob.size)}`);
    SFX.play("pop");
  } catch (e) {
    toast(e.message || "The wallpaper could not be saved");
    go.disabled = false; go.textContent = "Set wallpaper";
  }
};
