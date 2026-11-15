"use strict";
/* Photos, audio files and voice notes: pick the quality before sending (or use the default from the
   settings), with the size and the time on air for each choice.

   Photos are scaled and re-encoded on a canvas (WebP, JPEG where WebP can't be written).  Voice notes
   are recorded at 48 kb/s and re-encoded to Opus at 12 or 24 kb/s with WebCodecs, then packed into a
   small WebM file by the muxer below; browsers without WebCodecs re-record the clip through
   MediaRecorder at the lower rate instead (which takes as long as the clip). */

const MEDIA = {
  PHOTO: {
    saver: { label: "Saver", dim: 480, q: 0.5 },
    standard: { label: "Standard", dim: 640, q: 0.62 },
    hd: { label: "HD", dim: 1280, q: 0.8 },
    original: { label: "Original" },
  },
  AUDIO: {
    low: { label: "Low", kbps: 12, note: "speech, 12 kb/s" },
    standard: { label: "Standard", kbps: 24, note: "24 kb/s" },
    high: { label: "High", kbps: null, note: "as recorded" },
  },
  REC_KBPS: 48, REC_MAX: 30,
  SHOWABLE: /^image\/(jpeg|png|webp|gif|avif|bmp|svg\+xml)$/,
  noWebCodecs: false,          // tests switch this on to exercise the MediaRecorder fallback
};

// ------------------------------------------------------------------ helpers
MEDIA.loadImage = function (file) {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => rej(new Error("That picture could not be read"));
    img.src = URL.createObjectURL(file);
  });
};
MEDIA.canvasBlob = (c, type, q) => new Promise((r) => c.toBlob(r, type, q));
MEDIA.base = (name) => (name || "file").replace(/\.[^.]+$/, "");
MEDIA.fits = (size) => size <= S.maxBytes - 600;
// Where a photo, file or voice note goes, fixed when you start: the chat on screen ("main"), or the
// chat panel in the game center ("gc", with its own Priority switch).  upload() sends to ctx.to.
MEDIA.ctx = function (where = "main") {
  if (where === "gc") return { where, to: GCCHAT.chat, urgent: GCCHAT.urgent, sent: () => GCCHAT.setUrgent(false) };
  return { where: "main", to: S.cur, urgent: S.urgent, sent: () => setUrgent(false) };
};
// the composer in use: the game center's chat panel while the game center is open (null when that panel is shut)
MEDIA.active = () => (typeof GC !== "undefined" && GC.open ? (GCCHAT.open ? "gc" : null) : S.cur ? "main" : null);
MEDIA.recipient = (ctx = MEDIA.ctx()) => (ctx.to === GROUP ? "everyone" : chatName(ctx.to));
MEDIA.onAir = (size, ctx = MEDIA.ctx()) => `${fmtBytes(size)} · ${fmtDur(Math.max(1, airtime(size, peerMod(ctx.to))))}`;
MEDIA.linkLine = (ctx = MEDIA.ctx()) => {
  const mod = peerMod(ctx.to);
  return `${ctx.to === GROUP ? "Each station gets its own copy" : `Link to ${chatName(ctx.to)}`}: ${MODNAME[mod]}, ` +
    `${(S.phy.bit_rate * infoBits(mod) / 1e3).toFixed(0)} kb/s. Times are on air, without retransmissions.`;
};

MEDIA.onFile = async function (file, ctx = MEDIA.ctx()) {
  if (!file || !ctx.to) return;
  const t = file.type || "";
  try {
    if (t.startsWith("image/") && t !== "image/svg+xml") return await MEDIA.photo(file, ctx);
    if (t.startsWith("audio/")) return await MEDIA.audioFile(file, ctx);
    await upload(file, "file", file.name, t || "application/octet-stream", "", ctx);
  } catch (e) { toast(e.message || String(e)); }
};

// ------------------------------------------------------------------ photos
MEDIA.encodePhoto = async function (img, file, key) {
  if (key === "original") {
    if (!MEDIA.SHOWABLE.test(file.type)) return { error: "this format doesn't show in the chat" };
    return { blob: file, w: img.naturalWidth, h: img.naturalHeight, name: file.name, type: file.type };
  }
  const m = MEDIA.PHOTO[key];
  const s = Math.min(1, m.dim / Math.max(img.naturalWidth, img.naturalHeight));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(img.naturalWidth * s)); c.height = Math.max(1, Math.round(img.naturalHeight * s));
  const g = c.getContext("2d");
  g.imageSmoothingQuality = "high";
  g.drawImage(img, 0, 0, c.width, c.height);
  let b = await MEDIA.canvasBlob(c, "image/webp", m.q);
  if (!b || b.type !== "image/webp") b = await MEDIA.canvasBlob(c, "image/jpeg", m.q);
  const ext = b.type === "image/webp" ? ".webp" : ".jpg";
  return { blob: b, w: c.width, h: c.height, name: MEDIA.base(file.name) + ext, type: b.type };
};

MEDIA.photo = async function (file, ctx = MEDIA.ctx()) {
  const img = await MEDIA.loadImage(file);
  const animated = file.type === "image/gif";
  const keys = Object.keys(MEDIA.PHOTO);
  const send = (v) => upload(v.blob, "image", v.name, v.type, "", ctx);
  if (!pref("askQ")) {
    // the default, or the next smaller one if the file would be too big for the link
    let k = animated ? "original" : pref("photoQ");
    for (let i = keys.indexOf(k); i >= 0; i--) {
      const v = await MEDIA.encodePhoto(img, file, keys[i]);
      if (!v.error && MEDIA.fits(v.blob.size)) { URL.revokeObjectURL(img.src); return send(v); }
    }
    URL.revokeObjectURL(img.src);
    toast("This picture is too large for the link even at the smallest size");
    return;
  }
  await MEDIA.sheet({
    title: `Photo to ${MEDIA.recipient(ctx)}`,
    kind: "image", ctx,
    options: keys.map((k) => ({ key: k, label: MEDIA.PHOTO[k].label })),
    initial: animated ? "original" : pref("photoQ"),
    prefKey: "photoQ",
    variant: (k) => MEDIA.encodePhoto(img, file, k),
    describe: (k, v) => `${v.w}×${v.h}${k === "original" && animated ? " · animated" : ""}`,
    send,
    done: () => URL.revokeObjectURL(img.src),
  });
};

// ------------------------------------------------------------------ audio files and voice notes
// decode anything the browser can play to 48 kHz mono
MEDIA.decodeMono = async function (blob) {
  const OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const ab = await new OAC(1, 1, 48000).decodeAudioData(await blob.arrayBuffer());
  const n = ab.length, out = new Float32Array(n), ch = ab.numberOfChannels;
  for (let c = 0; c < ch; c++) { const d = ab.getChannelData(c); for (let i = 0; i < n; i++) out[i] += d[i] / ch; }
  return out;
};

MEDIA.canWebCodecs = async function (bitrate) {
  if (MEDIA.noWebCodecs || typeof AudioEncoder === "undefined" || typeof AudioData === "undefined") return null;
  const base = { codec: "opus", sampleRate: 48000, numberOfChannels: 1, bitrate };
  // longer frames cost less container overhead at low rates
  for (const cfg of [{ ...base, opus: { frameDuration: bitrate <= 16000 ? 60000 : 20000 } }, base]) {
    try { if ((await AudioEncoder.isConfigSupported(cfg)).supported) return cfg; } catch (e) { /* try the next */ }
  }
  return null;
};

// PCM -> Opus packets (WebCodecs) -> WebM
MEDIA.encodeOpus = async function (pcm, bitrate) {
  const cfg = await MEDIA.canWebCodecs(bitrate);
  if (!cfg) return MEDIA.encodeRealtime(pcm, bitrate);
  const packets = [];
  let head = null, err = null;
  const enc = new AudioEncoder({
    output: (chunk, meta) => {
      const d = meta?.decoderConfig?.description;
      if (d && !head) {
        const u = d instanceof ArrayBuffer ? new Uint8Array(d) : new Uint8Array(d.buffer, d.byteOffset, d.byteLength);
        if (u.length >= 19 && String.fromCharCode(...u.slice(0, 8)) === "OpusHead") head = u.slice();
      }
      const b = new Uint8Array(chunk.byteLength);
      chunk.copyTo(b);
      packets.push({ t: chunk.timestamp, b });
    },
    error: (e) => { err = e; },
  });
  enc.configure(cfg);
  const STEP = 4800;
  for (let i = 0; i < pcm.length; i += STEP) {
    const n = Math.min(STEP, pcm.length - i);
    const ad = new AudioData({ format: "f32-planar", sampleRate: 48000, numberOfFrames: n, numberOfChannels: 1,
      timestamp: Math.round((i * 1e6) / 48000), data: pcm.slice(i, i + n) });
    enc.encode(ad);
    ad.close();
  }
  await enc.flush();
  enc.close();
  if (err) throw err;
  if (!packets.length) throw new Error("the encoder produced nothing");
  return MEDIA.webm(packets, head, pcm.length / 48);
};

// without WebCodecs: play the clip into a MediaRecorder set to the lower rate (takes the clip's length)
MEDIA.encodeRealtime = async function (pcm, bitrate) {
  const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t));
  if (!type) throw new Error("this browser can't re-encode audio");
  toast(`Re-encoding at ${Math.round(bitrate / 1000)} kb/s: this browser has no WebCodecs, so it takes as long as the clip`, 4000);
  const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
  const buf = ctx.createBuffer(1, pcm.length, 48000);
  buf.copyToChannel(pcm, 0);
  const src = ctx.createBufferSource(), dst = ctx.createMediaStreamDestination();
  src.buffer = buf;
  src.connect(dst);
  const mr = new MediaRecorder(dst.stream, { mimeType: type, audioBitsPerSecond: bitrate });
  const parts = [];
  mr.ondataavailable = (e) => e.data.size && parts.push(e.data);
  const done = new Promise((res) => (mr.onstop = res));
  await ctx.resume();
  mr.start(500);
  src.start();
  src.onended = () => setTimeout(() => mr.state !== "inactive" && mr.stop(), 150);
  await done;
  ctx.close();
  return new Blob(parts, { type: type.split(";")[0] });
};

// ---- a minimal WebM (Matroska) writer for one Opus track: EBML header, Info with the duration,
//      Tracks with the OpusHead, and Clusters of SimpleBlocks (up to 30 s each).
MEDIA.webm = function (packets, head, durMs) {
  const te = new TextEncoder();
  const cat = (parts) => {
    let n = 0;
    for (const p of parts) n += p.length;
    const o = new Uint8Array(n);
    let k = 0;
    for (const p of parts) { o.set(p, k); k += p.length; }
    return o;
  };
  const idBytes = (id) => { const b = []; for (let x = id; x > 0; x = Math.floor(x / 256)) b.unshift(x % 256); return b; };
  const vint = (n) => {                                     // element size, shortest form
    let len = 1;
    while (n >= 2 ** (7 * len) - 1) len++;
    const b = new Uint8Array(len);
    for (let i = len - 1, x = n; i >= 0; i--, x = Math.floor(x / 256)) b[i] = x % 256;
    b[0] |= 1 << (8 - len);
    return b;
  };
  const el = (id, ...kids) => { const p = cat(kids); return cat([Uint8Array.from(idBytes(id)), vint(p.length), p]); };
  const uint = (id, v) => { const b = []; do { b.unshift(v % 256); v = Math.floor(v / 256); } while (v > 0); return el(id, Uint8Array.from(b)); };
  const flt = (id, v) => { const b = new Uint8Array(8); new DataView(b.buffer).setFloat64(0, v); return el(id, b); };
  const str = (id, s) => el(id, te.encode(s));
  if (!head) {                                              // OpusHead: version 1, mono, 312 samples pre-skip
    head = new Uint8Array(19);
    head.set(te.encode("OpusHead"));
    const dv = new DataView(head.buffer);
    dv.setUint8(8, 1); dv.setUint8(9, 1); dv.setUint16(10, 312, true); dv.setUint32(12, 48000, true); dv.setInt16(16, 0, true); dv.setUint8(18, 0);
  }
  const preskip = new DataView(head.buffer, head.byteOffset, head.byteLength).getUint16(10, true);
  const clusters = [];
  let c0 = null, blocks = [];
  const flush = () => { if (blocks.length) clusters.push(el(0x1F43B675, uint(0xE7, c0), ...blocks)); blocks = []; };
  for (const p of packets) {
    const t = Math.max(0, Math.round(p.t / 1000));
    if (c0 === null || t - c0 > 30000) { flush(); c0 = t; }
    const hdr = new Uint8Array(4);
    hdr[0] = 0x81;                                          // track 1
    new DataView(hdr.buffer).setInt16(1, t - c0);           // time relative to the cluster (ms)
    hdr[3] = 0x80;                                          // keyframe
    blocks.push(el(0xA3, hdr, p.b));
  }
  flush();
  const ebml = el(0x1A45DFA3, uint(0x4286, 1), uint(0x42F7, 1), uint(0x42F2, 4), uint(0x42F3, 8),
    str(0x4282, "webm"), uint(0x4287, 4), uint(0x4285, 2));
  const info = el(0x1549A966, uint(0x2AD7B1, 1000000), str(0x4D80, "WaveLink Chat"), str(0x5741, "WaveLink Chat"), flt(0x4489, durMs));
  const track = el(0xAE, uint(0xD7, 1), uint(0x73C5, 1), uint(0x83, 2), str(0x86, "A_OPUS"), el(0x63A2, head),
    uint(0x56AA, Math.round((preskip / 48000) * 1e9)), uint(0x56BB, 80000000), el(0xE1, flt(0xB5, 48000), uint(0x9F, 1)));
  const segment = el(0x18538067, info, el(0x1654AE6B, track), ...clusters);
  return new Blob([ebml, segment], { type: "audio/webm" });
};

// the three choices for a clip: low and standard are re-encoded, high is the original
MEDIA.audioVariants = function (blob, name, dur) {
  let pcm = null;
  const getPcm = async () => (pcm = pcm || await MEDIA.decodeMono(blob));
  return async (k) => {
    const m = MEDIA.AUDIO[k];
    if (!m.kbps) return { blob, name, type: blob.type || "audio/webm", dur };
    const p = await getPcm();
    const out = await MEDIA.encodeOpus(p, m.kbps * 1000);
    const ext = out.type.includes("ogg") ? ".ogg" : out.type.includes("mp4") ? ".m4a" : ".webm";
    return { blob: out, name: MEDIA.base(name) + ext, type: out.type, dur: dur ?? p.length / 48000 };
  };
};

MEDIA.audioFile = async function (file, ctx = MEDIA.ctx()) {
  const variant = MEDIA.audioVariants(file, file.name, null);
  const send = (v) => upload(v.blob, "audio", v.name, v.type, v.dur ? `&dur=${v.dur.toFixed(1)}` : "", ctx);
  if (!pref("askQ")) return MEDIA.sendDefault(variant, send);
  await MEDIA.sheet({
    title: `Audio to ${MEDIA.recipient(ctx)}`, kind: "audio", ctx,
    options: [["low", "Low"], ["standard", "Standard"], ["high", "Original"]].map(([key, label]) => ({ key, label })),
    initial: pref("voiceQ"), prefKey: "voiceQ", variant, send,
    describe: (k, v) => k === "high" ? "the file as it is" : `Opus ${MEDIA.AUDIO[k].kbps} kb/s`,
    note: file.name,
  });
};

// no sheet: the default quality, or a smaller one if it doesn't fit the link
MEDIA.sendDefault = async function (variant, send) {
  const keys = Object.keys(MEDIA.AUDIO), i = Math.max(0, keys.indexOf(pref("voiceQ")));
  const order = [keys[i], ...keys.slice(0, i).reverse(), ...keys.slice(i + 1)];   // then smaller, then larger
  for (const k of order) {
    try {
      const v = await variant(k);
      if (MEDIA.fits(v.blob.size)) return send(v);
    } catch (e) { /* this one can't be made here: try the next */ }
  }
  toast("This audio is too large for the link");
};

// ---- the recorder: its bar takes the place of the composer it was started from (the chat's, or the
//      game center chat panel's)
MEDIA.REC = { mr: null, parts: [], t0: 0, timer: 0, cancel: false, stream: null, ctx: null, an: null, raf: 0, levels: [], dest: null, ui: null };
MEDIA.recUI = (where) => where === "gc"
  ? { bar: $("#gccRec"), comp: $("#gccForm"), time: $("#gccRecTime"), level: $("#gccRecLevel"), hint: $("#gccRecHint"), send: $("#gccRecSend") }
  : { bar: $("#recBar"), comp: $(".composer"), time: $("#recTime"), level: $("#recLevel"), hint: $("#recHint"), send: $("#recSend") };

MEDIA.startRec = async function (dest = MEDIA.ctx()) {
  const R = MEDIA.REC;
  if (R.mr) { toast("A voice note is already being recorded"); return; }
  if (!dest.to) return;
  if (!navigator.mediaDevices?.getUserMedia) { toast("The microphone needs http://localhost or HTTPS. Attach an audio file instead."); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } }); }
  catch (e) { toast("No microphone (or permission denied)"); return; }
  const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  R.stream = stream;
  R.mr = new MediaRecorder(stream, { mimeType: type, audioBitsPerSecond: MEDIA.REC_KBPS * 1000 });
  R.parts = []; R.cancel = false; R.levels = []; R.dest = dest;
  const ui = R.ui = MEDIA.recUI(dest.where);
  R.mr.ondataavailable = (e) => e.data.size && R.parts.push(e.data);
  R.mr.onstop = MEDIA.recStopped;
  R.mr.start(250);
  R.t0 = performance.now();
  MUSIC.updateDuck();
  SFX.play("rec");
  ui.comp.hidden = true; ui.bar.hidden = false;
  ui.send.textContent = pref("askQ") ? "Done" : "Send";
  ui.time.textContent = "0:00";
  ui.hint.textContent = dest.where === "gc" ? `To ${MEDIA.recipient(dest)}` : `Recording, up to ${MEDIA.REC_MAX} s`;
  if (dest.where === "gc") GCCHAT.recording(true);
  // live level meter
  try {
    R.ctx = new (window.AudioContext || window.webkitAudioContext)();
    R.an = R.ctx.createAnalyser();
    R.an.fftSize = 1024;
    R.ctx.createMediaStreamSource(stream).connect(R.an);
  } catch (e) { R.an = null; }
  const buf = new Float32Array(1024);
  const draw = () => {
    if (!R.mr) return;
    R.raf = requestAnimationFrame(draw);
    let lvl = 0;
    if (R.an) { R.an.getFloatTimeDomainData(buf); let s = 0; for (const v of buf) s += v * v; lvl = Math.min(1, Math.sqrt(s / buf.length) * 5); }
    R.levels.push(lvl);
    if (R.levels.length > 60) R.levels.shift();
    const cv = ui.level, g = cv.getContext("2d"), w = cv.width, h = cv.height;
    g.clearRect(0, 0, w, h);
    g.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent");
    R.levels.forEach((v, i) => { const bh = Math.max(2, v * (h - 4)); g.fillRect(i * 4, (h - bh) / 2, 2.5, bh); });
  };
  draw();
  R.timer = setInterval(() => {
    const s = (performance.now() - R.t0) / 1000;
    ui.time.textContent = `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    if (s >= MEDIA.REC_MAX) MEDIA.stopRec(false);
  }, 200);
};

MEDIA.stopRec = function (cancel) {
  const R = MEDIA.REC;
  if (!R.mr || R.mr.state === "inactive") return;
  R.cancel = cancel;
  R.dur = (performance.now() - R.t0) / 1000;
  R.mr.stop();
};

MEDIA.recStopped = async function () {
  const R = MEDIA.REC, mr = R.mr, dest = R.dest, ui = R.ui;
  R.stream?.getTracks().forEach((t) => t.stop());
  clearInterval(R.timer); cancelAnimationFrame(R.raf);
  R.ctx?.close().catch(() => {});
  R.mr = null; R.ctx = null; R.an = null; R.dest = null; R.ui = null;
  MUSIC.updateDuck();
  ui.bar.hidden = true; ui.comp.hidden = false;
  if (dest.where === "gc") GCCHAT.recording(false);
  if (R.cancel) { SFX.play("pop"); return; }
  const type = (mr.mimeType || "audio/webm").split(";")[0];
  const blob = new Blob(R.parts, { type });
  const name = `voice-${Date.now()}.${type.split("/")[1] === "mp4" ? "m4a" : type.split("/")[1]}`;
  const dur = R.dur ?? (performance.now() - R.t0) / 1000;
  R.dur = null;
  const variant = MEDIA.audioVariants(blob, name, dur);
  const send = (v) => upload(v.blob, "audio", v.name, v.type, `&dur=${dur.toFixed(1)}`, dest);
  if (!pref("askQ")) return MEDIA.sendDefault(variant, send);
  await MEDIA.sheet({
    title: `Voice note to ${MEDIA.recipient(dest)}`, kind: "audio", ctx: dest,
    options: Object.keys(MEDIA.AUDIO).map((k) => ({ key: k, label: MEDIA.AUDIO[k].label })),
    initial: pref("voiceQ"), prefKey: "voiceQ", variant, send,
    describe: (k) => MEDIA.AUDIO[k].kbps ? `Opus ${MEDIA.AUDIO[k].kbps} kb/s` : `${MEDIA.REC_KBPS} kb/s, as recorded`,
    note: `${dur.toFixed(1)} s`,
    discard: "Discard",
  });
};

// ------------------------------------------------------------------ the quality sheet
// Every choice is made right away (one after the other) so its size and time on air can be shown;
// the preview shows the selected one.
MEDIA.sheet = function (o) {
  return new Promise((resolve) => {
    const keys = o.options.map((x) => x.key);
    let sel = keys.includes(o.initial) ? o.initial : keys[Math.min(1, keys.length - 1)];
    const got = {}, urls = [];
    const el = document.createElement("div");
    el.className = "sheet";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", o.title);
    el.innerHTML = `<div class="scard">
      <h3>${esc(o.title)}</h3>
      <div class="pv">${o.kind === "image" ? '<img alt="Preview">' : '<audio controls preload="metadata"></audio>'}</div>
      <div class="chips" role="radiogroup" aria-label="Quality">${o.options.map((x) => `<button class="chip" role="radio" data-k="${x.key}" aria-checked="false" aria-pressed="false">
        <b>${esc(x.label)}</b><small>…</small></button>`).join("")}</div>
      <div class="info"></div>
      <label class="mkdef"><input type="checkbox"> Use this quality from now on</label>
      <div class="acts"><button data-x>${o.discard || "Cancel"}</button><button class="primary" data-go disabled>Send</button></div>
    </div>`;
    document.body.appendChild(el);
    const pv = el.querySelector(o.kind === "image" ? ".pv img" : ".pv audio");
    const info = el.querySelector(".info"), go = el.querySelector("[data-go]"), mk = el.querySelector(".mkdef input");
    const prevFocus = document.activeElement;
    const close = (sent) => {
      el.remove();
      document.removeEventListener("keydown", onKey, true);
      urls.forEach((u) => URL.revokeObjectURL(u));
      o.done?.();
      prevFocus?.focus?.();
      resolve(sent);
    };
    const show = () => {
      el.querySelectorAll(".chip").forEach((c) => { const on = c.dataset.k === sel; c.setAttribute("aria-pressed", String(on)); c.setAttribute("aria-checked", String(on)); });
      const v = got[sel];
      mk.parentElement.hidden = sel === pref(o.prefKey);
      if (!v) { info.textContent = "Preparing…"; go.disabled = true; return; }
      if (v.error) { info.textContent = `Not available: ${v.error}`; go.disabled = true; return; }
      if (!v.url) { v.url = URL.createObjectURL(v.blob); urls.push(v.url); }
      if (pv.dataset.k !== sel) { pv.src = v.url; pv.dataset.k = sel; }
      const big = !MEDIA.fits(v.blob.size);
      info.textContent = big ? `Too large for the link (${fmtBytes(v.blob.size)}, at most ${fmtBytes(S.maxBytes - 600)})`
        : `${o.describe ? o.describe(sel, v) + " · " : ""}${fmtBytes(v.blob.size)}, about ${fmtDur(Math.max(1, airtime(v.blob.size, peerMod(o.ctx?.to))))} on air. ${MEDIA.linkLine(o.ctx)}`;
      go.disabled = big;
    };
    el.querySelectorAll(".chip").forEach((c) => (c.onclick = () => { sel = c.dataset.k; show(); }));
    el.querySelector("[data-x]").onclick = () => close(false);
    el.addEventListener("pointerdown", (e) => { if (e.target === el) close(false); });
    go.onclick = async () => {
      const v = got[sel];
      if (!v || v.error) return;
      go.disabled = true;
      if (mk.checked) setPref(o.prefKey, sel);
      if (await o.send(v)) close(true); else go.disabled = false;
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(false); }
      else if (e.key === "Enter" && !go.disabled && !e.target.matches?.("[data-x], input")) { e.preventDefault(); go.click(); }
      else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && e.target.closest?.(".chips")) {
        const i = keys.indexOf(sel) + (e.key === "ArrowLeft" ? -1 : 1);
        if (i >= 0 && i < keys.length) { sel = keys[i]; show(); el.querySelector(`.chip[data-k="${sel}"]`).focus(); }
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey, true);
    const note = o.note ? ` · ${o.note}` : "";
    el.querySelector("h3").insertAdjacentHTML("beforeend", note ? `<small>${esc(note)}</small>` : "");
    show();
    el.querySelector(`.chip[data-k="${sel}"]`).focus();
    // make every choice, the selected one first
    (async () => {
      for (const k of [sel, ...keys.filter((x) => x !== sel)]) {
        if (!el.isConnected) return;
        let v;
        try { v = await o.variant(k); } catch (e) { v = { error: e.message || "can't be made in this browser" }; }
        got[k] = v;
        const small = el.querySelector(`.chip[data-k="${k}"] small`);
        if (small) small.textContent = v.error ? "not available" : MEDIA.onAir(v.blob.size, o.ctx);
        el.querySelector(`.chip[data-k="${k}"]`).disabled = !!v.error;
        if (k === sel) show();
      }
      // the preferred one couldn't be made: pick the first that could
      if (got[sel]?.error) { const ok = keys.find((k) => got[k] && !got[k].error); if (ok) { sel = ok; show(); } }
    })();
  });
};

// ------------------------------------------------------------------ the camera: take a photo, then send it
// A live preview with a shutter; Retake or Use photo; then the usual quality sheet (or the default).
MEDIA.camera = async function (ctx = MEDIA.ctx()) {
  if (!ctx.to) return;
  if (!navigator.mediaDevices?.getUserMedia) { toast("The camera needs http://localhost or HTTPS. Attach a picture instead."); return; }
  const el = document.createElement("div");
  el.className = "sheet camsheet";
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  el.setAttribute("aria-label", "Take a photo");
  el.innerHTML = `<div class="scard">
    <h3>Photo for ${esc(MEDIA.recipient(ctx))}</h3>
    <div class="pv"><video autoplay playsinline muted></video><img class="shot" alt="The photo" hidden><div class="flash"></div></div>
    <div class="note">Starting the camera…</div>
    <div class="camacts">
      <button class="smallbtn" data-flip hidden title="Switch between the front and back cameras">Switch camera</button>
      <button class="shutter" data-shoot title="Take the photo (Space)" aria-label="Take the photo" disabled></button>
      <button class="smallbtn" data-retake hidden>Retake</button>
    </div>
    <div class="acts"><button data-x>Cancel</button><button class="primary" data-use disabled>Use photo</button></div>
  </div>`;
  document.body.appendChild(el);
  const video = el.querySelector("video"), shot = el.querySelector(".shot"), note = el.querySelector(".note");
  const shoot = el.querySelector("[data-shoot]"), use = el.querySelector("[data-use]"), retake = el.querySelector("[data-retake]"), flip = el.querySelector("[data-flip]");
  let stream = null, facing = "user", blob = null, url = null, closed = false;
  const stop = () => { stream?.getTracks().forEach((t) => t.stop()); stream = null; };
  const start = async () => {
    stop();
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: facing, width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false });
    } catch (e) {
      note.textContent = e.name === "NotAllowedError" ? "Camera permission was refused." : "No camera found.";
      return;
    }
    if (closed) { stop(); return; }
    video.srcObject = stream;
    const settings = stream.getVideoTracks()[0]?.getSettings() || {};
    video.classList.toggle("mirror", (settings.facingMode || facing) === "user");
    await video.play().catch(() => {});
    note.textContent = "Press the button (or Space) to take the photo.";
    shoot.disabled = false;
    const cams = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
    flip.hidden = cams.length < 2;
  };
  const close = () => {
    closed = true; stop(); el.remove();
    document.removeEventListener("keydown", onKey, true);
    if (url) URL.revokeObjectURL(url);
  };
  const take = async () => {
    if (!stream || !video.videoWidth) return;
    const c = document.createElement("canvas");
    c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0);
    blob = await MEDIA.canvasBlob(c, "image/jpeg", 0.92);
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(blob);
    shot.src = url;
    const fl = el.querySelector(".flash");
    fl.classList.remove("go"); void fl.offsetWidth; fl.classList.add("go");
    SFX.play("pop");
    video.hidden = true; shot.hidden = false;
    shoot.hidden = true; flip.hidden = true; retake.hidden = false; use.disabled = false;
    note.textContent = `${c.width}×${c.height}. Use it, or take it again.`;
  };
  const again = () => {
    video.hidden = false; shot.hidden = true; blob = null;
    shoot.hidden = false; retake.hidden = true; use.disabled = true;
    note.textContent = "Press the button (or Space) to take the photo.";
    navigator.mediaDevices.enumerateDevices().then((d) => { flip.hidden = d.filter((x) => x.kind === "videoinput").length < 2; });
  };
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if ((e.key === " " || e.key === "Enter") && e.target.tagName !== "BUTTON") {
      e.preventDefault();
      if (!shot.hidden && !use.disabled) use.click(); else if (!shoot.disabled) take();
    }
  };
  document.addEventListener("keydown", onKey, true);
  el.querySelector("[data-x]").onclick = close;
  el.addEventListener("pointerdown", (e) => { if (e.target === el) close(); });
  shoot.onclick = take;
  retake.onclick = again;
  flip.onclick = () => { facing = facing === "user" ? "environment" : "user"; shoot.disabled = true; start(); };
  use.onclick = () => {
    if (!blob) return;
    const file = new File([blob], `photo-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-")}.jpg`, { type: "image/jpeg" });
    close();
    MEDIA.onFile(file, ctx);                               // the quality sheet, or the default quality
  };
  shoot.focus();
  start();
};

document.addEventListener("DOMContentLoaded", () => {
  $("#camBtn").onclick = () => MEDIA.camera(MEDIA.ctx("main"));
  $("#recSend").onclick = () => MEDIA.stopRec(false);
  $("#recCancel").onclick = () => MEDIA.stopRec(true);
});
