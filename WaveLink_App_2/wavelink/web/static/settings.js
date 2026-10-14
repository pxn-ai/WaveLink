"use strict";
/* Settings: profile (name and picture), colour palette, light/dark, breathing lights, chat wallpaper,
   sounds, the quality used for photos and audio, and the race camera.  Everything is stored in the
   station's profile (data/node<N>/profile.json via POST /api/profile), so every station keeps its own
   look; the name and the picture also go out over the air with the station's beacons. */

const PALETTES = [  // id, name, light: accent, 2nd accent, bubble; dark: accent, 2nd accent, bubble (as in style.css)
  ["wavelink", "WaveLink", "#1f6fe5", "#10b9c6", "#dbe9fd", "#4a8dff", "#2fdcd3", "#11305e"],
  ["lagoon", "Lagoon", "#0f8f7a", "#1b8fc4", "#d6f2e9", "#2bb79d", "#39b8e8", "#10463c"],
  ["saffron", "Saffron", "#b9600b", "#c9405f", "#fae3c6", "#f2a03d", "#ff7a8a", "#4a3214"],
  ["orchid", "Orchid", "#8b3fb2", "#c43f8f", "#ecdcf6", "#c58aea", "#f08ac8", "#3e2052"],
  ["cobalt", "Cobalt", "#2a5fd1", "#5b3fd6", "#dae5fc", "#79a4ff", "#a08bff", "#1c3360"],
  ["coral", "Coral", "#c93a52", "#d9822b", "#fadde2", "#ff7f92", "#ffb062", "#4f1f29"],
  ["graphite", "Graphite", "#3c434e", "#64748b", "#e3e6ea", "#c8cfd9", "#8fa3bf", "#2e343d"],
  ["neon", "Neon", "#c8166f", "#0fa3c4", "#fbd9eb", "#ff3f9e", "#3fe6ff", "#3d1033"],
];
const WALLS = [["plain", "Plain"], ["chips", "Chips"], ["qam", "QAM"], ["waves", "Waves"], ["rain", "Rain"],
  ["dusk", "Dusk"], ["velvet", "Velvet"], ["custom", "Your photo"]];
const AVATAR_EMOJI = [..."🦊🐼🐨🐯🦁🐸🐙🦉🐧🐳🦄🐝🦋🐢🦖🐶🐱🐰🐻🐵🌵🌻🍄🌈🔥⚡🌙⭐🌊🍀🍕🍩☕🎧🎮🎲🚀🤖👾👻😎🤓🥳😺🎯🃏💎🎸"]
  .concat(["🛰️", "📡", "📻", "🏎️"]);

// ------------------------------------------------------------------ colours
const THEME = {
  mq: window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : { matches: false },
  CUSTOM_KEYS: ["--accent", "--accent2", "--accent-ink", "--mine", "--chatbg"],
};
THEME.mode = () => (pref("mode") === "auto" ? (THEME.mq.matches ? "dark" : "light") : pref("mode"));
THEME.rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
THEME.hex = (c) => "#" + c.map((v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, "0")).join("");
THEME.mix = (a, b, t) => a.map((v, i) => v * t + b[i] * (1 - t));          // t of a, 1 - t of b
THEME.lum = (c) => {
  const l = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
};
// a palette from one colour: keep the accent readable on the background, derive the bubble colour
THEME.customVars = function (hex, mode) {
  const c = THEME.rgb(hex), W = [255, 255, 255], K = [16, 22, 26];
  let acc = c;
  if (mode === "dark") { while (THEME.lum(acc) < 0.16) acc = THEME.mix(acc, W, 0.85); }
  else { while (THEME.lum(acc) > 0.32) acc = THEME.mix(acc, [0, 0, 0], 0.85); }
  // the second accent: the same colour turned 40 degrees round the colour wheel
  const [r, g, b] = acc.map((v) => v / 255), mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  let h = 0;
  if (mx > mn) h = mx === r ? ((g - b) / (mx - mn)) % 6 : mx === g ? (b - r) / (mx - mn) + 2 : (r - g) / (mx - mn) + 4;
  const h2 = ((h * 60 + 40) % 360 + 360) % 360, Lh = (mx + mn) / 2, Ch = mx - mn;
  const X = Ch * (1 - Math.abs(((h2 / 60) % 2) - 1)), m0 = Lh - Ch / 2;
  const sector = [[Ch, X, 0], [X, Ch, 0], [0, Ch, X], [0, X, Ch], [X, 0, Ch], [Ch, 0, X]][Math.floor(h2 / 60) % 6];
  const acc2 = sector.map((v) => (v + m0) * 255);
  return {
    "--accent": THEME.hex(acc),
    "--accent2": THEME.hex(acc2),
    "--accent-ink": THEME.lum(acc) > 0.36 ? "#101418" : "#ffffff",
    "--mine": THEME.hex(mode === "dark" ? THEME.mix(c, K, 0.32) : THEME.mix(c, W, 0.2)),
    "--chatbg": THEME.hex(mode === "dark" ? THEME.mix(c, [11, 16, 16], 0.06) : THEME.mix(c, [231, 236, 234], 0.07)),
  };
};
THEME.apply = function () {
  const r = document.documentElement, mode = THEME.mode(), pal = pref("pal"), acc = pref("accent");
  r.dataset.mode = mode;
  r.dataset.pal = pal;
  r.dataset.anim = pref("anim") ? "on" : "off";
  let vars = null;
  if (pal === "custom" && /^#[0-9a-f]{6}$/i.test(acc || "")) vars = THEME.customVars(acc, mode);
  for (const k of THEME.CUSTOM_KEYS) vars ? r.style.setProperty(k, vars[k]) : r.style.removeProperty(k);
  try { localStorage.setItem("wavelink-theme", JSON.stringify({ pal, mode: pref("mode"), anim: !!pref("anim"), accent: acc || null, vars })); } catch (e) { /* private window */ }
  THEME.wall();
};
THEME.wall = function () {
  const w = $("#wall");
  if (!w) return;
  const id = pref("wall"), url = S.profile?.wallpaper;
  const shown = id === "custom" ? (url ? "custom" : "plain") : WALLS.some((x) => x[0] === id) ? id : "plain";
  w.className = "wall-" + shown;
  w.style.backgroundImage = shown === "custom" ? `url("${url}")` : "";
  const dim = pref("wallDim");
  if (dim === null || dim === undefined) w.style.removeProperty("--wall-dim"); else w.style.setProperty("--wall-dim", String(dim));
};
if (THEME.mq.addEventListener) THEME.mq.addEventListener("change", () => { if (pref("mode") === "auto") THEME.apply(); });

// ------------------------------------------------------------------ the drawer
const SETTINGS = { built: false };

SETTINGS.open = function () {
  if (S.dashOpen) { S.dashOpen = false; $("#dash").hidden = true; }
  SETTINGS.build();
  SETTINGS.refresh();
  $("#settings").hidden = false;
  $("#settings .dashbody").scrollTop = 0;
};
SETTINGS.close = function () { $("#settings").hidden = true; };
SETTINGS.toggle = function () { $("#settings").hidden ? SETTINGS.open() : SETTINGS.close(); };

SETTINGS.build = function () {
  if (SETTINGS.built) return;
  SETTINGS.built = true;
  // palettes: the seven presets and one from any colour
  $("#palGrid").innerHTML = PALETTES.map(([id, nm]) => `<button class="pal" data-v="${id}"><i></i>${nm}</button>`).join("") +
    `<label class="pal" data-v="custom" title="Any colour"><i></i>Custom<input type="color" id="palPick" aria-label="Custom colour"></label>`;
  $("#palGrid").querySelectorAll("button.pal").forEach((b) => (b.onclick = () => { setPref("pal", b.dataset.v); SETTINGS.refresh(); SFX.play("pop"); }));
  const pick = $("#palPick");
  pick.oninput = () => { S.prefs.accent = pick.value; S.prefs.pal = "custom"; THEME.apply(); SETTINGS.paintPalettes(); };
  pick.onchange = () => { setPref("accent", pick.value); setPref("pal", "custom"); SETTINGS.refresh(); };
  $("#palGrid").querySelector("label.pal").onclick = (e) => {
    if (e.target === pick) return;
    if (pref("accent")) { setPref("pal", "custom"); SETTINGS.refresh(); }
  };
  SETTINGS.seg("#modeSeg", "mode");
  $("#animSw").onclick = () => { setPref("anim", !pref("anim")); SETTINGS.refresh(); };
  // wallpapers
  $("#wallGrid").innerHTML = WALLS.map(([id, nm]) => `<button class="wtile" data-v="${id}" title="${nm}"><span class="w wall-${id}"></span><b>${nm}</b></button>`).join("");
  $("#wallGrid").querySelectorAll(".wtile").forEach((b) => (b.onclick = () => {
    if (b.dataset.v === "custom" && !S.profile?.wallpaper) { $("#wallFile").click(); return; }
    setPref("wall", b.dataset.v); SETTINGS.refresh();
  }));
  $("#wallUpload").onclick = () => $("#wallFile").click();
  $("#wallFile").onchange = (e) => { SETTINGS.uploadWallpaper(e.target.files[0]); e.target.value = ""; };
  $("#wallDim").oninput = (e) => { S.prefs.wallDim = +e.target.value; THEME.wall(); };
  $("#wallDim").onchange = (e) => setPref("wallDim", +e.target.value);
  // sounds
  $("#sndSw").onclick = () => { setPref("sound", !pref("sound")); if (!pref("sound")) SFX.engineStop(); SETTINGS.refresh(); SFX.play("pop"); };
  $("#vol").oninput = (e) => { S.prefs.vol = +e.target.value; SFX.ensure(); };
  $("#vol").onchange = (e) => { setPref("vol", +e.target.value); SFX.play("receive"); };
  $("#sndTest").onclick = () => {
    const seq = ["receive", "send", "card", "pickup", "lap"];
    seq.forEach((n, k) => setTimeout(() => SFX.play(n), k * 420));
  };
  // media
  $("#askSw").onclick = () => { setPref("askQ", !pref("askQ")); SETTINGS.refresh(); };
  SETTINGS.seg("#photoSeg", "photoQ");
  SETTINGS.seg("#voiceSeg", "voiceQ");
  SETTINGS.seg("#camSeg", "raceCam");
  // profile
  const nm = $("#setName");
  nm.onchange = async () => {
    const v = nm.value.trim();
    if (!v || v === S.me.name) { nm.value = S.me.name; return; }
    try { await api("/api/profile", { name: v }); toast("Name changed: the other stations see it with your next beacon"); }
    catch (e) { toast(e.message); nm.value = S.me.name; }
  };
  nm.onkeydown = (e) => { if (e.key === "Enter") nm.blur(); if (e.key === "Escape") { nm.value = S.me.name; nm.blur(); } };
  $("#avKind").querySelectorAll("button").forEach((b) => (b.onclick = () => SETTINGS.setKind(b.dataset.v)));
  $("#avPick").innerHTML = AVATAR_EMOJI.map((e) => `<button data-e="${e}" aria-label="${e}">${e}</button>`).join("");
  $("#avPick").querySelectorAll("button").forEach((b) => (b.onclick = () => SETTINGS.saveAvatar({ kind: "emoji", emoji: b.dataset.e })));
  const hueIn = $("#avHue");
  hueIn.oninput = () => SETTINGS.previewHue(+hueIn.value);
  hueIn.onchange = () => {
    const a = S.profile?.avatar || {};
    SETTINGS.saveAvatar(a.kind === "emoji" ? { kind: "emoji", emoji: a.emoji, hue: +hueIn.value } : { kind: "initial", hue: +hueIn.value });
  };
  $("#avUpload").onclick = () => $("#avFile").click();
  $("#avFile").onchange = (e) => { SETTINGS.uploadAvatar(e.target.files[0]); e.target.value = ""; };
  $("#setClose").onclick = SETTINGS.close;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#settings").hidden && !document.querySelector(".sheet") && document.activeElement !== nm) SETTINGS.close();
  });
};

// a segmented control bound to a preference
SETTINGS.seg = function (sel, key) {
  $(sel).querySelectorAll("button").forEach((b) => (b.onclick = () => { setPref(key, b.dataset.v); SETTINGS.refresh(); }));
};
const pressSeg = (sel, v) => $(sel)?.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v)));

SETTINGS.paintPalettes = function () {
  const dark = THEME.mode() === "dark";
  $("#palGrid").querySelectorAll(".pal").forEach((b) => {
    const id = b.dataset.v, i = b.querySelector("i");
    let a, a2, m;
    if (id === "custom") {
      const acc = pref("accent");
      if (acc) { const v = THEME.customVars(acc, dark ? "dark" : "light"); a = v["--accent"]; a2 = v["--accent2"]; m = v["--mine"]; }
      else { i.style.background = "conic-gradient(#e84a5f, #f2a03d, #f7dc6f, #2bb79d, #2a5fd1, #8b3fb2, #e84a5f)"; }
    } else {
      const p = PALETTES.find((x) => x[0] === id);
      [a, a2, m] = dark ? [p[5], p[6], p[7]] : [p[2], p[3], p[4]];
    }
    // the accent gradient over the bubble colour
    if (a) i.style.background = `linear-gradient(135deg, ${a} 0%, ${a2} 54%, ${m} 54%, ${m} 100%)`;
    b.setAttribute("aria-pressed", String(pref("pal") === id));
  });
};

SETTINGS.refresh = function () {
  if (!SETTINGS.built || !S.me) return;
  const p = S.profile || {}, av = p.avatar || {};
  // profile
  const nm = $("#setName");
  if (document.activeElement !== nm) nm.value = S.me.name;
  avatar($("#setAvatar"), `u:${S.me.addr}`);
  $("#setAvatar").classList.remove("on");
  const kind = SETTINGS.kind || av.kind || "initial";
  pressSeg("#avKind", kind);
  $("#avPhotoRow").hidden = kind !== "photo";
  $("#avPick").hidden = kind !== "emoji";
  $("#avHue").hidden = kind === "photo";
  $("#avHue").value = av.hue ?? hue(S.me.addr);
  $("#avPhotoInfo").textContent = av.kind === "photo" ? "shared with the other stations" : "";
  $("#avUpload").textContent = av.kind === "photo" ? "Change photo…" : "Choose a photo…";
  $("#avPick").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(av.kind === "emoji" && av.emoji === b.dataset.e)));
  // theme
  SETTINGS.paintPalettes();
  if (pref("accent")) $("#palPick").value = pref("accent");
  pressSeg("#modeSeg", pref("mode"));
  $("#animSw").setAttribute("aria-checked", String(!!pref("anim")));
  // wallpaper
  $("#wallGrid").querySelectorAll(".wtile").forEach((b) => {
    const custom = b.dataset.v === "custom";
    if (custom) {
      const w = b.querySelector(".w");
      w.style.backgroundImage = p.wallpaper ? `url("${p.wallpaper}")` : "";
      w.style.backgroundSize = "cover"; w.style.backgroundPosition = "center";
      b.querySelector("b").textContent = p.wallpaper ? "Your photo" : "Upload…";
    }
    b.setAttribute("aria-pressed", String(pref("wall") === b.dataset.v && (!custom || !!p.wallpaper)));
  });
  $("#wallUpload").textContent = p.wallpaper ? "Replace photo…" : "Upload a wallpaper…";
  const dimV = pref("wallDim");
  $("#wallDim").value = dimV ?? (THEME.mode() === "dark" ? 0.55 : 0.35);
  $("#dimRow").hidden = !(pref("wall") === "custom" && p.wallpaper);
  // sounds and media
  $("#sndSw").setAttribute("aria-checked", String(!!pref("sound")));
  $("#vol").value = pref("vol");
  $("#vol").disabled = !pref("sound");
  $("#askSw").setAttribute("aria-checked", String(!!pref("askQ")));
  pressSeg("#photoSeg", pref("photoQ"));
  pressSeg("#voiceSeg", pref("voiceQ"));
  pressSeg("#camSeg", pref("raceCam"));
  const mod = peerMod(), t = (kb) => fmtDur(Math.max(1, airtime(kb * 1024, mod)));
  $("#mediaNote").textContent = `On ${MODNAME[mod]}${S.cur && S.cur !== GROUP ? ` (your link to ${chatName(S.cur)})` : ""}, a typical photo takes about ` +
    `${t(22)} / ${t(45)} / ${t(140)} on air (Saver / Standard / HD), and a 10-second voice note ${t(16)} / ${t(31)} / ${t(61)} (Low / Standard / High).`;
};

// ------------------------------------------------------------------ profile picture
SETTINGS.setKind = function (kind) {
  const av = S.profile?.avatar || {};
  if (kind === "photo") {
    SETTINGS.kind = "photo";
    SETTINGS.refresh();
    if (S.profile?.has_photo) SETTINGS.saveAvatar({ kind: "photo" });
    else $("#avFile").click();
    return;
  }
  SETTINGS.kind = null;
  if (kind === "emoji") SETTINGS.saveAvatar({ kind: "emoji", emoji: av.kind === "emoji" ? av.emoji : AVATAR_EMOJI[0], hue: av.hue ?? hue(S.me.addr) });
  else SETTINGS.saveAvatar({ kind: "initial", hue: av.hue ?? hue(S.me.addr) });
};
SETTINGS.previewHue = function (h) {
  const el = $("#setAvatar");
  el.style.background = `hsl(${h} 45% 42%)`;
};
SETTINGS.saveAvatar = async function (a) {
  if (a.hue === undefined && a.kind !== "photo") a.hue = +$("#avHue").value;
  try {
    S.profile = await api("/api/profile", { avatar: a });
    SETTINGS.kind = null;
    renderMe(); SETTINGS.refresh();
  } catch (e) { toast(e.message); }
};
// a square 160 px picture (a portrait is cut a little above the middle, where faces usually are)
SETTINGS.uploadAvatar = async function (file) {
  if (!file) { SETTINGS.kind = null; SETTINGS.refresh(); return; }
  try {
    const img = await MEDIA.loadImage(file);
    const w = img.naturalWidth, h = img.naturalHeight, s = Math.min(w, h);
    const c = document.createElement("canvas");
    c.width = c.height = 160;
    const g = c.getContext("2d");
    g.imageSmoothingQuality = "high";
    g.drawImage(img, (w - s) / 2, (h - s) * (h > w ? 0.3 : 0.5), s, s, 0, 0, 160, 160);
    let blob = await MEDIA.canvasBlob(c, "image/webp", 0.8), mime = "image/webp";
    if (!blob || blob.type !== "image/webp") { blob = await MEDIA.canvasBlob(c, "image/jpeg", 0.85); mime = "image/jpeg"; }
    S.profile = await api(`/api/avatar?mime=${encodeURIComponent(mime)}`, blob, true);
    SETTINGS.kind = null;
    renderMe(); SETTINGS.refresh();
    toast(`Profile photo set (${fmtBytes(blob.size)}): stations fetch it once when they next hear your beacon`, 4000);
  } catch (e) { toast(e.message || "That picture could not be read"); SETTINGS.kind = null; SETTINGS.refresh(); }
};

// ------------------------------------------------------------------ wallpaper (stays on this station)
SETTINGS.uploadWallpaper = async function (file) {
  if (!file) return;
  try {
    let blob = file, mime = file.type || "image/jpeg";
    const img = await MEDIA.loadImage(file);
    const big = Math.max(img.naturalWidth, img.naturalHeight);
    if (big > 2560 || file.size > 4e6 || !/^image\/(jpeg|png|webp|gif|avif)$/.test(mime)) {
      const s = Math.min(1, 2560 / big), c = document.createElement("canvas");
      c.width = Math.round(img.naturalWidth * s); c.height = Math.round(img.naturalHeight * s);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      blob = await MEDIA.canvasBlob(c, "image/webp", 0.86);
      if (!blob || blob.type !== "image/webp") blob = await MEDIA.canvasBlob(c, "image/jpeg", 0.88);
      mime = blob.type;
    }
    S.profile = await api(`/api/wallpaper?mime=${encodeURIComponent(mime)}`, blob, true);
    setPref("wall", "custom");                             // (overrides a choice still waiting to be posted)
    SETTINGS.refresh();
  } catch (e) { toast(e.message || "That picture could not be read"); }
};

// ------------------------------------------------------------------ wiring
document.addEventListener("DOMContentLoaded", () => {
  $("#setBtn").onclick = SETTINGS.toggle;
  THEME.apply();
});
