"use strict";
/* WaveLink Chat front end — vanilla JS, no external dependencies (works offline in the lab).
   Scripts: app.js (chat), sfx.js (sounds), settings.js (profile, themes, wallpapers), emojipick.js,
   media.js (photo / voice quality, recorder), games.js + uno.js + race.js (game center). */

const $ = (s, el = document) => el.querySelector(s);
const S = {
  me: null, phy: null, contacts: new Map(), msgs: new Map(), order: [], unread: {},
  cur: null, urgent: false, maxBytes: 400000, radio: null, dashOpen: false,
  profile: null, prefs: {}, fresh: new Set(), lastFrames: 0,
};
const GROUP = "g:all";

// ------------------------------------------------------------------ preferences (kept on the station)
const PREF_DEFAULTS = {
  pal: "wavelink", mode: "auto", anim: true, wall: "plain", wallDim: null, sound: true, vol: 0.6,
  askQ: true, photoQ: "standard", voiceQ: "standard", raceCam: "north", emojiRecent: [],
  navBottom: false, music: "off", musicVol: 0.35, wallFit: "cover", wallEdit: null,
};
function pref(k) {
  const v = S.prefs ? S.prefs[k] : undefined;
  return v === undefined || v === null ? PREF_DEFAULTS[k] : v;
}
const prefQueue = {};
let prefTimer = null;
function setPref(k, v) {
  S.prefs[k] = v;
  prefQueue[k] = v;
  if (typeof THEME !== "undefined") THEME.apply();
  clearTimeout(prefTimer);
  prefTimer = setTimeout(() => {
    const body = { prefs: { ...prefQueue } };
    for (const key of Object.keys(prefQueue)) delete prefQueue[key];
    api("/api/profile", body).catch((e) => toast(e.message));
  }, 350);
}

// ------------------------------------------------------------------ light that follows the radio
// The logo sends out a ring when the radio hears frames or a message comes or goes (at most every 1.2 s).
const BRAND = { last: 0 };
BRAND.ping = function () {
  const now = performance.now();
  if (now - BRAND.last < 1200 || document.hidden) return;
  BRAND.last = now;
  document.querySelectorAll(".wl-mark, .livedot").forEach((el) => {
    el.classList.remove("pinging");
    void el.offsetWidth;                                   // restart the animation
    el.classList.add("pinging");
  });
};
// one short glow along the conversation header (a message just came in on this chat)
function headerPulse() {
  const h = $(".convhead");
  if (!h || h.classList.contains("busy")) return;
  h.classList.remove("pulse"); void h.offsetWidth; h.classList.add("pulse");
  setTimeout(() => h.classList.remove("pulse"), 1000);
}

// ------------------------------------------------------------------ utils
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hue = (n) => (n * 67 + 160) % 360;
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (t) => new Date(t * 1000).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(2)} MB`;
const fmtDur = (s) => s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
const animOn = () => pref("anim") && !matchMedia("(prefers-reduced-motion: reduce)").matches;
function toast(t, ms = 2600) {
  const el = $("#toast"); el.textContent = t; el.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("show"), ms);
}
async function api(path, body, raw) {
  const opt = body === undefined ? {} : { method: "POST", body: raw ? body : JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const contactName = (addr) => (+addr === S.me?.addr ? S.me.name : S.contacts.get(+addr)?.name) || `Node ${addr}`;
const chatName = (id) => id === GROUP ? "Everyone" : contactName(id.split(":")[1]);
function avatarSpec(addr) {
  return addr === S.me?.addr ? (S.profile?.avatar || {}) : (S.contacts.get(addr)?.av || {});
}
function avatar(el, id) {
  el.classList.remove("emo");
  if (id === GROUP) { el.textContent = "✱"; el.style.background = "var(--accent)"; el.classList.remove("on"); return; }
  const a = +id.split(":")[1], av = avatarSpec(a), nm = contactName(a);
  const h = av.hue ?? hue(a);
  if (av.kind === "photo" && av.url) {
    if (el.dataset.src !== av.url) { el.innerHTML = `<img src="${esc(av.url)}" alt="">`; el.dataset.src = av.url; }
    el.style.background = "var(--line)";
  } else {
    el.dataset.src = "";
    if (av.kind === "emoji" && av.emoji) { el.textContent = av.emoji; el.classList.add("emo"); }
    else el.textContent = nm.trim().slice(0, 1).toUpperCase();
    el.style.background = `hsl(${h} 45% 42%)`;
  }
  el.classList.toggle("on", a !== S.me?.addr && !!S.contacts.get(a)?.online);
}
// the same picture as an HTML string (game center, UNO table)
function avatarHtml(addr, cls = "", name = null) {
  const av = avatarSpec(+addr), h = av.hue ?? hue(+addr), nm = name || contactName(addr);
  if (av.kind === "photo" && av.url) return `<span class="${cls}" style="background:#333"><img src="${esc(av.url)}" alt=""></span>`;
  if (av.kind === "emoji" && av.emoji) return `<span class="${cls} emo" style="background:hsl(${h} 45% 40%)">${esc(av.emoji)}</span>`;
  return `<span class="${cls}" style="background:hsl(${h} 45% 40%)">${esc(nm.trim().slice(0, 1).toUpperCase())}</span>`;
}
const BPS = { bpsk: 1, bpskc: 1, qpsk: 2, "16qam": 4 };
const BASENAME = { bpsk: "BPSK", bpskc: "BPSK", qpsk: "QPSK", "16qam": "16-QAM" };
const RATEFRAC = { "1/2": 0.5, "2/3": 2 / 3, "3/4": 0.75, "5/6": 5 / 6 };
const RATEGLYPH = { "1/2": "½", "2/3": "⅔", "3/4": "¾", "5/6": "⅚" };
// modulation-and-coding names from the link layer: "16qam-3/4", "qpsk-1/2", "16qam" (uncoded)
const mcsBase = (m) => (m || "bpsk").split("-")[0];
const mcsRate = (m) => RATEFRAC[(m || "").split("-")[1]] || 1;
const MODNAME = new Proxy({}, { get: (_, m) => {
  if (typeof m !== "string" || !m) return undefined;
  const [b, r] = m.split("-");
  return BASENAME[b] ? BASENAME[b] + (r ? " " + (RATEGLYPH[r] || r) : " uncoded") : m;
} });
const infoBits = (m) => BPS[mcsBase(m)] * mcsRate(m);       // information bits per symbol
function peerMod(chat = S.cur) {
  if (!chat || chat === "g:all") return "qpsk-1/2";
  return S.contacts.get(+chat.split(":")[1])?.tx_mod || "qpsk-1/2";
}
const airtime = (bytes, mod = peerMod()) => bytes * 8 / ((S.phy?.bit_rate || 32000) * infoBits(mod) * 0.85);

// ------------------------------------------------------------------ icons
const TICK1 = '<svg viewBox="0 0 18 12"><path d="M5.6 9.2 2 5.6.9 6.7l4.7 4.7L15.4 1.6 14.3.5z"/></svg>';
const TICK2 = '<svg viewBox="0 0 18 12"><path d="M5.6 9.2 2 5.6.9 6.7l4.7 4.7L15.4 1.6 14.3.5z"/><path d="m9.6 9.2-.9-.9-1.1 1.1 2 2L19.4 1.6 18.3.5z" transform="translate(-2 0)"/></svg>';
const CLOCK = '<svg viewBox="0 0 18 12"><circle cx="9" cy="6" r="4.6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8.4 3.5h1.2v2.8l1.8 1-.6 1-2.4-1.4z"/></svg>';
const BANG = '<svg viewBox="0 0 18 12"><circle cx="9" cy="6" r="5.5"/><path d="M8.3 2.8h1.4v4H8.3zm0 4.9h1.4v1.4H8.3z" fill="#fff"/></svg>';
const STOP = '<svg viewBox="0 0 18 12"><circle cx="9" cy="6" r="4.8" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="m5.8 9.2 6.4-6.4" stroke="currentColor" stroke-width="1.3"/></svg>';
const XSVG = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18.3 7.1 16.9 5.7 12 10.6 7.1 5.7 5.7 7.1l4.9 4.9-4.9 4.9 1.4 1.4 4.9-4.9 4.9 4.9 1.4-1.4-4.9-4.9z"/></svg>';
const LOCK = '<svg class="lock" viewBox="0 0 24 24"><path d="M17 9h-1V7a4 4 0 0 0-8 0v2H7a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2zm-7-2a2 2 0 0 1 4 0v2h-4z"/></svg>';
function ticks(rec) {
  const st = rec.status;
  const tip = Object.entries(rec.recip || {}).map(([a, s]) => `${contactName(a)}: ${s}`).join("\n");
  if (st === "queued") return `<span class="ticks" title="${esc(tip || "queued")}">${CLOCK}</span>`;
  if (st === "sent") return `<span class="ticks" title="${esc(tip)}">${TICK1}</span>`;
  if (st === "delivered") return `<span class="ticks" title="${esc(tip)}">${TICK2}</span>`;
  if (st === "read") return `<span class="ticks read" title="${esc(tip)}">${TICK2}</span>`;
  if (st === "failed") return `<span class="ticks failed" title="${esc(tip || "failed")}">${BANG}</span>`;
  if (st === "cancelled") return `<span class="ticks cancelled" title="${esc(tip || "cancelled")}">${STOP}</span>`;
  return "";
}

// ------------------------------------------------------------------ state load + SSE
async function load() {
  const st = await api("/api/state");
  S.me = st.me; S.phy = st.phy; S.maxBytes = st.max_bytes; S.unread = st.unread || {};
  S.profile = st.profile; S.prefs = { ...(st.profile?.prefs || {}), ...prefQueue };
  if (typeof THEME !== "undefined") THEME.apply();
  S.contacts = new Map(st.contacts.map((c) => [c.addr, c]));
  S.msgs = new Map(); S.order = [];
  for (const m of st.messages) { S.msgs.set(m.uid, m); S.order.push(m.uid); }
  renderMe(); renderChats();
  if (S.cur) renderConv(true);
}
function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    const { ev, data } = JSON.parse(e.data);
    if (ev === "msg") onMsg(data);
    else if (ev === "contacts") {
      S.contacts = new Map(data.map((c) => [c.addr, c])); renderChats(); renderConvHead();
      if (typeof GC !== "undefined") GC.contactsChanged();
    }
    else if (ev === "unread") { S.unread = data; renderChats(); }
    else if (ev === "toast") toast(data, 4000);
    else if (ev === "reset") load();
    else if (ev === "profile") {
      // the station's copy, plus any change still waiting to be posted
      S.profile = data; S.prefs = { ...data.prefs, ...prefQueue };
      if (typeof THEME !== "undefined") THEME.apply();
      if (!S.me) return;                                  // load() is still on its way
      if (data.name) S.me.name = data.name;
      renderMe();
      if (S.cur) renderConvHead();
      if (typeof SETTINGS !== "undefined") SETTINGS.refresh();
    }
    else if (ev === "incoming") pollRadio();
    else if (typeof GC !== "undefined") GC.onEvent(ev, data);       // game center (games.js)
  };
  es.onerror = () => { $("#sideFoot").textContent = "Reconnecting to station…"; };
  es.onopen = () => { load(); if (typeof GC !== "undefined") GC.load(); };
}
function onMsg(rec) {
  const old = S.msgs.get(rec.uid), isNew = !old;
  S.msgs.set(rec.uid, rec);
  if (isNew) S.order.push(rec.uid);
  const fromOther = rec.frm !== S.me.addr;
  if (isNew) BRAND.ping();
  if (isNew && fromOther && rec.chat === S.cur) headerPulse();
  if (isNew && fromOther && rec.kind !== "game") SFX.play(rec.urgent ? "urgent" : "receive", { gap: 0.4 });
  if (isNew && fromOther && rec.kind === "game" && rec.extra?.t === S.me.addr) SFX.play("turn");
  if (!isNew && !fromOther && old.status !== rec.status && rec.status === "failed") SFX.play("error");
  if (typeof GCCHAT !== "undefined") GCCHAT.onMsg(rec);
  if (rec.chat === S.cur) {
    if (rec.kind === "game") renderConv(false);
    else if (isNew) placeMsgEl(rec, fromOther);
    else updateMsgEl(rec);
    if (isNew && fromOther) markRead();
  }
  renderChats();
}

// ------------------------------------------------------------------ sidebar
function renderMe() {
  $("#meName").textContent = S.me.name;
  $("#meSub").textContent = `node ${S.me.addr} · ${({ sim: "simulation (numpy PHY)", "grc-sim": "GNU Radio PHY · simulated channel", grc: "GNU Radio PHY · ANTSDR" })[S.me.mode] || "ANTSDR"}${S.me.enc ? " · encrypted (key " + S.me.key_id + ")" : ""}`;
  avatar($("#meAvatar"), `u:${S.me.addr}`); $("#meAvatar").classList.remove("on");
  const p = S.phy;
  $("#phyLine").textContent = `${(p.center_freq / 1e9).toFixed(3)} GHz · DS-CDMA · ${p.sf}-chip Gold · BPSK/QPSK/16-QAM + LDPC ${(p.bit_rate / 2e3).toFixed(0)}–${(4 * p.bit_rate / 1e3).toFixed(0)} kb/s · my code #${S.me.addr}`;
  document.title = `${S.me.name} · WaveLink Chat`;
  const ab = $("#aboutStation");
  if (ab) ab.textContent = `${S.me.name} is node ${S.me.addr} on Gold code #${S.me.addr}: DS-CDMA over ANTSDR E200 and GNU Radio at ${(p.center_freq / 1e9).toFixed(1)} GHz.`;
}
const sortKey = (m) => m.ts ?? 0;
function lastMsg(chat) {
  let best = null;
  for (const uid of S.order) { const m = S.msgs.get(uid); if (m.chat === chat && (!best || sortKey(m) >= sortKey(best))) best = m; }
  return best;
}
function preview(m) {
  if (!m) return "";
  const who = m.frm === S.me.addr ? "You: " : (m.chat === GROUP ? contactName(m.frm) + ": " : "");
  const body = m.kind === "text" ? m.text : m.kind === "image" ? "📷 Photo" : m.kind === "audio" ? "🎤 Voice note"
    : m.kind === "game" ? "🎮 Tic-tac-toe" : "📎 " + (m.name || "File");
  return who + body;
}
function renderChats() {
  const f = $("#filter").value.trim().toLowerCase();
  const ids = new Set([GROUP]);
  for (const a of S.contacts.keys()) ids.add(`u:${a}`);
  for (const uid of S.order) ids.add(S.msgs.get(uid).chat);
  const rows = [...ids].map((id) => ({ id, last: lastMsg(id) }))
    .filter((r) => !f || chatName(r.id).toLowerCase().includes(f))
    .sort((a, b) => (b.id === GROUP) - (a.id === GROUP) || (b.last?.ts || 0) - (a.last?.ts || 0) || a.id.localeCompare(b.id));
  const ul = $("#chats"); ul.innerHTML = "";
  for (const r of rows) {
    const li = document.createElement("li");
    li.className = r.id === S.cur ? "active" : "";
    const av = document.createElement("div"); av.className = "avatar"; avatar(av, r.id);
    const n = S.unread[r.id] || 0;
    let sub = preview(r.last);
    if (!r.last) {
      if (r.id === GROUP) { const n = [...S.contacts.values()].filter((c) => c.online).length; sub = `${n} station${n === 1 ? "" : "s"} online`; }
      else { const c = S.contacts.get(+r.id.split(":")[1]); sub = c?.online ? `online · ${c.snr_db} dB` : "offline"; }
    }
    li.innerHTML = `<div class="cmain"><div class="ctop"><span class="cname">${esc(chatName(r.id))}</span>
      <span class="ctime">${r.last ? fmtTime(r.last.ts) : ""}</span></div>
      <div class="cbot"><span class="cprev">${esc(sub)}</span>${n ? `<span class="badge">${n}</span>` : ""}</div></div>`;
    li.prepend(av);
    li.onclick = () => openChat(r.id);
    ul.appendChild(li);
  }
  const on = [...S.contacts.values()].filter((c) => c.online).length;
  $("#sideFoot").textContent = `${on} of ${S.contacts.size} known stations online`;
  renderOnAir();
  if (typeof GCCHAT !== "undefined") GCCHAT.refresh();
}

// ------------------------------------------------------------------ conversation
function openChat(id) {
  const unread = S.unread[id] || 0;
  S.cur = id;
  $("#empty").hidden = true; $("#conv").hidden = false;
  $("#app").classList.add("inchat");
  $("#gameBtn").hidden = id === GROUP;
  renderChats(); renderConv(true, unread); markRead();
  $("#text").focus();
}
function renderConvHead() {
  if (!S.cur) return;
  avatar($("#convAvatar"), S.cur);
  $("#convName").textContent = chatName(S.cur);
  let sub;
  if (S.cur === GROUP) {
    const on = [...S.contacts.values()].filter((c) => c.online).map((c) => c.name);
    sub = on.length ? `Delivered to each online station on its own code: ${on.join(", ")}` : "No stations online yet";
  } else {
    const a = +S.cur.split(":")[1], c = S.contacts.get(a);
    const lq = S.radio?.links?.[a];
    sub = c?.online ? `online · code #${a}` : `offline · code #${a}`;
    if (lq || c) sub += ` · Eb/N0 ${(lq || c).snr_db} dB · CFO ${((lq || c).cfo_hz / 1000).toFixed(1)} kHz`;
    if (c?.tx_mod) sub += ` · sending ${MODNAME[c.tx_mod]} (${(S.phy.bit_rate * infoBits(c.tx_mod) / 1e3).toFixed(0)} kb/s)`;
    if (c && c.key_ok === false && S.me.enc) sub += " · ⚠ different encryption key";
  }
  $("#convSub").textContent = sub;
}
// the header's signal line flows while a file goes to or comes from this chat
function updateBusy() {
  const h = $(".convhead");
  if (!h || !S.cur || !S.me) return;
  const peer = S.cur === GROUP ? null : +S.cur.split(":")[1];
  let busy = (S.radio?.incoming || []).some((x) => peer === null || x.src === peer);
  if (!busy) for (const m of S.msgs.values()) {
    if (m.chat === S.cur && m.frm === S.me.addr && m.size > 230 && (m.status === "queued" || m.status === "sent")) { busy = true; break; }
  }
  h.classList.toggle("busy", busy);
}
// the empty screen says who is on the air
function renderOnAir() {
  const el = $("#onAir");
  if (!el) return;
  const on = [...S.contacts.values()].filter((c) => c.online).map((c) => c.name);
  el.classList.toggle("live", on.length > 0);
  el.textContent = !on.length ? "Listening for other stations. They appear on the left when their beacons are heard."
    : `${on.length === 1 ? on[0] + " is" : on.slice(0, -1).join(", ") + " and " + on[on.length - 1] + " are"} on the air. Pick a chat on the left.`;
}

// the chat's messages in the order they were sent (a late one goes where it belongs)
function chatMsgs(chat) {
  const latestGame = new Map();
  const list = [];
  for (const uid of S.order) {
    const m = S.msgs.get(uid);
    if (m.chat !== chat) continue;
    if (m.kind === "game") latestGame.set(m.extra?.gid, uid);
    list.push(m);
  }
  return list.filter((m) => m.kind !== "game" || latestGame.get(m.extra?.gid) === m.uid)
    .map((m, i) => [m, i]).sort((a, b) => sortKey(a[0]) - sortKey(b[0]) || a[1] - b[1]).map((x) => x[0]);
}
function dayEl(d) { const el = document.createElement("div"); el.className = "day"; el.textContent = d; el.dataset.day = d; return el; }
function renderConv(scroll, unreadCount = 0) {
  renderConvHead();
  const box = $("#msgs");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60, top0 = box.scrollTop;
  box.innerHTML = "";
  S.fresh = new Set();
  const list = chatMsgs(S.cur);
  // the last unreadCount messages from others are new: highlight them until they've been seen
  if (unreadCount) {
    let k = unreadCount;
    for (let i = list.length - 1; i >= 0 && k > 0; i--) if (list[i].frm !== S.me.addr) { S.fresh.add(list[i].uid); k--; }
  }
  let day = "", divDone = false, divEl = null;
  for (const m of list) {
    const d = fmtDay(m.ts);
    if (d !== day) { day = d; box.appendChild(dayEl(d)); }
    if (!divDone && S.fresh.has(m.uid)) {
      divDone = true;
      divEl = document.createElement("div"); divEl.className = "newdiv";
      divEl.textContent = `${S.fresh.size} new message${S.fresh.size > 1 ? "s" : ""}`;
      box.appendChild(divEl);
    }
    box.appendChild(msgEl(m));
  }
  if (divEl) box.scrollTop = Math.max(0, divEl.offsetTop - box.clientHeight * 0.3);
  else if (scroll || atBottom) box.scrollTop = box.scrollHeight;
  else box.scrollTop = top0;                               // (a redraw keeps the reader's place)
  observeFresh();
  updateJump();
}
function nearBottom(box = $("#msgs")) { return box.scrollHeight - box.scrollTop - box.clientHeight < 80; }

// a new message: put it where it belongs by send time; a late one slides into its place
function placeMsgEl(m, fromOther) {
  const box = $("#msgs");
  const wasBottom = nearBottom(box);
  const el = msgEl(m);
  if (fromOther) { S.fresh.add(m.uid); el.classList.add("fresh"); }
  const msgs = [...box.querySelectorAll(".msg")];
  const before = msgs.find((x) => +x.dataset.ts > sortKey(m));
  const d = fmtDay(m.ts);
  if (!before) {
    const lastDay = [...box.querySelectorAll(".day")].pop()?.dataset.day;
    if (lastDay !== d) box.appendChild(dayEl(d));
    if (animOn()) el.classList.add("arrive");
    box.appendChild(el);
    if (wasBottom || !fromOther) box.scrollTop = box.scrollHeight;
  } else {
    let anchor = before;
    const prev = before.previousElementSibling;
    if (prev && prev.classList.contains("day") && prev.dataset.day !== d) anchor = prev;
    const prevMsg = msgs[msgs.indexOf(before) - 1];
    if (fmtDay(+before.dataset.ts) !== d && (!prevMsg || fmtDay(+prevMsg.dataset.ts) !== d)) { renderConv(false); return; }
    // FLIP: everything from the anchor down moves to make room, the late message flies up from the end
    const moving = [];
    for (let x = anchor; x; x = x.nextElementSibling) moving.push([x, x.getBoundingClientRect().top]);
    const endY = box.lastElementChild ? box.lastElementChild.getBoundingClientRect().bottom : 0;
    box.insertBefore(el, anchor);
    if (animOn()) {
      for (const [x, y0] of moving) {
        const dy = y0 - x.getBoundingClientRect().top;
        if (Math.abs(dy) > 0.5) x.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }], { duration: 460, easing: "cubic-bezier(.2,.8,.2,1)" });
      }
      const top = el.getBoundingClientRect().top;
      const from = Math.max(0, Math.min(endY - top, box.clientHeight));
      el.animate([{ transform: `translateY(${from}px)`, opacity: 0.35 }, { transform: "none", opacity: 1 }],
        { duration: 760, easing: "cubic-bezier(.2,.8,.2,1)" });
    }
    if (!fromOther) el.scrollIntoView({ block: "nearest" });
  }
  observeFresh();
  updateJump();
}
function updateMsgEl(m) {
  const old = document.getElementById("m-" + m.uid);
  if (!old) return;
  // keep media elements (avoid re-loading audio/images) — only refresh meta + progress
  const fresh = msgEl(m);
  if (old.classList.contains("fresh")) fresh.classList.add("fresh");
  if (old.classList.contains("seen")) fresh.classList.add("seen");
  const oldMedia = old.querySelector("img.media, audio");
  if (oldMedia && fresh.querySelector("img.media, audio")) {
    old.querySelector(".meta").replaceWith(fresh.querySelector(".meta"));
    old.classList.toggle("transit", fresh.classList.contains("transit"));
    const op = old.querySelector(".progwrap"), np = fresh.querySelector(".progwrap");
    if (op && np) op.replaceWith(np); else if (op) op.remove();
    wireMsgEl(old, m);
  } else {
    if (old._seenT) clearTimeout(old._seenT);
    freshObs?.unobserve(old);
    old.replaceWith(fresh);
    if (fresh.classList.contains("fresh") && !fresh.classList.contains("seen")) observeFresh();
  }
}
function msgEl(m) {
  const mine = m.frm === S.me.addr;
  const el = document.createElement("div");
  el.id = "m-" + m.uid;
  el.dataset.ts = sortKey(m);
  el.className = "msg" + (mine ? " mine" : "") + (m.urgent ? " urgent" : "") + (S.fresh.has(m.uid) ? " fresh" : "")
    + (mine && (m.status === "queued" || m.status === "sent") ? " transit" : "");
  let h = "";
  if (!mine && m.chat === GROUP) h += `<div class="from" style="color:hsl(${hue(m.frm)} 55% 52%)">${esc(contactName(m.frm))}</div>`;
  const url = m.file ? `/media/${encodeURIComponent(m.file)}` : "";
  if (m.kind === "text") h += `<div class="txt${isJumbo(m.text) ? " jumbo" : ""}">${esc(m.text)}</div>`;
  else if (m.kind === "image") h += `<img class="media" src="${url}" alt="${esc(m.name || "image")}" loading="lazy">`;
  else if (m.kind === "audio") h += `<audio controls preload="metadata" src="${url}"></audio>`;
  else if (m.kind === "file") {
    const ext = (m.name.split(".").pop() || "file").slice(0, 4).toUpperCase();
    h += `<a class="file" href="${url}" download="${esc(m.name)}"><span class="fi">${esc(ext)}</span>
      <span><div>${esc(m.name)}</div><div class="progtxt">${fmtBytes(m.size)}</div></span></a>`;
  } else if (m.kind === "game") h += gameHtml(m);
  const sending = m.status === "queued" || m.status === "sent";
  if (mine && m.size > 230 && m.status !== "delivered" && m.status !== "read") {
    const pct = Math.round((m.progress || 0) * 100);
    const what = m.status === "failed" ? "failed" : m.status === "cancelled"
      ? (m.stopped_by ? `cancelled by ${contactName(m.stopped_by)}` : "cancelled") : `${pct}% of ${fmtBytes(m.size)}`;
    h += `<div class="progwrap"><div class="progrow"><div class="prog"><i style="width:${pct}%"></i></div>
      ${sending ? `<button class="xbtn" data-cancel title="Cancel sending" aria-label="Cancel sending">${XSVG}</button>` : ""}</div>
      <div class="progtxt">${what}${m.retries && sending ? ` · ${m.retries} retries` : ""}</div></div>`;
  }
  let info = "";
  if (mine && m.goodput_kbps && m.size > 230) info = `${m.airtime_s}s · ${m.goodput_kbps} kb/s`;
  if (!mine && m.snr_db !== undefined && m.kind !== "game") info = `${m.snr_db} dB`;
  const late = !mine && m.delayed ? `<span class="late" title="Sent ${fmtTime(m.ts)}, arrived ${fmtTime(m.rx_ts)}">⏱ ${fmtDur(m.delayed)} late</span>` : "";
  h += `<div class="meta">${late}${m.enc ? LOCK : ""}${m.urgent ? "⚡" : ""}${info ? `<span class="info">${esc(info)}</span>` : ""}
    <span>${fmtTime(m.ts)}</span>${mine ? ticks(m) : ""}</div>`;
  el.innerHTML = h;
  wireMsgEl(el, m);
  return el;
}
function wireMsgEl(el, m) {
  const img = el.querySelector("img.media");
  if (img) img.onclick = () => { $("#lightbox img").src = img.src; $("#lightbox").hidden = false; };
  el.querySelectorAll(".game button[data-i]").forEach((b) => (b.onclick = () => gameMove(m, +b.dataset.i)));
  const x = el.querySelector("[data-cancel]");
  if (x) x.onclick = async () => {
    x.disabled = true;
    try { await api("/api/cancel", { uid: m.uid }); SFX.play("pop"); } catch (e) { toast(e.message); x.disabled = false; }
  };
}
// one to three emoji and nothing else: show them big
function isJumbo(t) {
  if (!t || t.length > 24) return false;
  const s = t.replace(/\s+/g, "");
  if (!s || /[\p{L}\p{N}]/u.test(s.replace(/[\u{1F1E6}-\u{1F1FF}]/gu, ""))) return false;
  const parts = [...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(s)];
  return parts.length <= 3 && parts.every((p) => /\p{Extended_Pictographic}|[\u{1F1E6}-\u{1F1FF}]/u.test(p.segment));
}

// ------------------------------------------------------------------ new-message highlight and the jump arrow
let freshObs = null;
function observeFresh() {
  if (!("IntersectionObserver" in window)) return;
  if (!freshObs) {
    // seen = mostly on screen for most of a second (a tall one: half the screen; one cut off at the top:
    // the reader is already past its start), or scrolled past: one that leaves through the top
    freshObs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const el = e.target, rb = e.rootBounds;
        const shown = e.isIntersecting && (e.intersectionRatio >= 0.6 || (rb && (e.intersectionRect.height >= 0.5 * rb.height
          || e.boundingClientRect.top < rb.top)));
        if (shown && document.visibilityState === "visible") {
          if (!el._seenT) el._seenT = setTimeout(() => markSeen(el), 900);
        } else {
          if (el._seenT) { clearTimeout(el._seenT); el._seenT = null; }
          if (!e.isIntersecting && rb && e.boundingClientRect.bottom <= rb.top + 1) markSeen(el);
        }
      }
    }, { root: $("#msgs"), threshold: [0, 0.25, 0.5, 0.6, 1] });
  }
  document.querySelectorAll("#msgs .msg.fresh:not(.seen)").forEach((el) => freshObs.observe(el));
}
function markSeen(el) {
  if (el.classList.contains("seen") || !el.isConnected) return;
  el._seenT = null;
  el.classList.add("seen");
  freshObs?.unobserve(el);
  S.fresh.delete(el.id.slice(2));
  setTimeout(() => { el.classList.remove("fresh", "seen"); }, 1500);
  if (!S.fresh.size) {
    const div = $("#msgs .newdiv");
    if (div) setTimeout(() => div.remove(), 1500);
  }
  updateJump();
}
function updateJump() {
  const box = $("#msgs"), btn = $("#jumpBtn");
  if (!box || !btn) return;
  const pending = [...box.querySelectorAll(".msg.fresh:not(.seen)")];
  const below = pending.filter((el) => el.offsetTop > box.scrollTop + box.clientHeight - 20);
  const far = box.scrollHeight - box.scrollTop - box.clientHeight > 320;
  btn.hidden = !(below.length || far);
  btn.classList.toggle("has-new", pending.length > 0);
  btn.querySelector(".n").textContent = pending.length ? String(pending.length) : "";
  btn.title = pending.length ? `${pending.length} new message${pending.length > 1 ? "s" : ""}` : "Latest messages";
}
function jumpToNew() {
  const box = $("#msgs");
  const first = [...box.querySelectorAll(".msg.fresh:not(.seen)")].find((el) => el.offsetTop + el.offsetHeight > box.scrollTop + box.clientHeight - 10);
  const behavior = animOn() ? "smooth" : "auto";
  if (first) box.scrollTo({ top: Math.max(0, first.offsetTop - box.clientHeight * 0.3), behavior });
  else box.scrollTo({ top: box.scrollHeight, behavior });
}

// ------------------------------------------------------------------ tic-tac-toe
const LINES = [[0,1,2],[3,4,5],[6,7,8],[0,3,6],[1,4,7],[2,5,8],[0,4,8],[2,4,6]];
function winner(b) {
  for (const [a, c, d] of LINES) if (b[a] !== "-" && b[a] === b[c] && b[a] === b[d]) return b[a];
  return b.includes("-") ? null : "draw";
}
function gameHtml(m) {
  const g = m.extra || {};
  const b = g.b || "---------";
  const w = winner(b);
  const myTurn = !w && g.t === S.me.addr;
  const sym = (a) => (a === g.p[0] ? "X" : "O");
  let st;
  if (w === "draw") st = "Draw.";
  else if (w) st = (w === sym(S.me.addr) ? "You won 🎉" : `${contactName(w === "X" ? g.p[0] : g.p[1])} won`);
  else st = myTurn ? `Your move (you are ${sym(S.me.addr)})` : `Waiting for ${contactName(g.t)}…`;
  let cells = "";
  for (let i = 0; i < 9; i++) {
    const c = b[i] === "-" ? "" : b[i];
    cells += `<button data-i="${i}" class="${c.toLowerCase()}" ${myTurn && !c ? "" : "disabled"}>${c}</button>`;
  }
  return `<div class="gstat">🎮 Tic-tac-toe</div><div class="game">${cells}</div><div class="gstat">${esc(st)}</div>`;
}
async function gameMove(m, i) {
  const g = { ...m.extra };
  if (g.t !== S.me.addr || g.b[i] !== "-") return;
  const sym = g.p[0] === S.me.addr ? "X" : "O";
  g.b = g.b.slice(0, i) + sym + g.b.slice(i + 1);
  g.t = g.p[0] === S.me.addr ? g.p[1] : g.p[0];
  try { await api("/api/game", { to: m.chat, state: g }); SFX.play("card"); } catch (e) { toast(e.message); }
}
async function newGame() {
  if (!S.cur || S.cur === GROUP) return;
  const peer = +S.cur.split(":")[1];
  const g = { gid: Math.random().toString(36).slice(2, 8), b: "---------", p: [S.me.addr, peer], t: S.me.addr };
  try { await api("/api/game", { to: S.cur, state: g }); } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ sending
async function sendText() {
  const ta = $("#text");
  const text = ta.value.trim();
  if (!text || !S.cur) return;
  ta.value = ""; autosize(); updateSendBtn();
  try { await api("/api/send", { to: S.cur, text, urgent: S.urgent }); SFX.play("send"); }
  catch (e) { toast(e.message); SFX.play("error"); ta.value = text; updateSendBtn(); }
  setUrgent(false);
}
// upload a prepared blob (media.js decides the quality first).  ctx says where it goes: the chat on
// screen, or the chat panel in the game center (MEDIA.ctx)
async function upload(blob, kind, name, mime, extraQs = "", ctx = MEDIA.ctx()) {
  if (blob.size > S.maxBytes - 600) { toast(`Too large for the link (${fmtBytes(blob.size)}, max ${fmtBytes(S.maxBytes - 600)})`); return false; }
  if (!ctx.to) return false;
  const qs = new URLSearchParams({ to: ctx.to, kind, name, mime, urgent: ctx.urgent ? "1" : "0" });
  try {
    await api(`/api/upload?${qs}${extraQs}`, blob, true);
    SFX.play("send");
    const mod = peerMod(ctx.to), t = airtime(blob.size, mod);
    toast(`Sending ${fmtBytes(blob.size)}: about ${fmtDur(Math.max(1, t))} on air at ${MODNAME[mod]} (less if it compresses)`, 5000);
  } catch (e) { toast(e.message); SFX.play("error"); return false; }
  ctx.sent?.();
  if (ctx.where === "gc") GCCHAT.jump = true;
  return true;
}

// ------------------------------------------------------------------ read receipts
let readT = null;
function markRead() {
  if (!S.cur || document.hidden) return;
  clearTimeout(readT);
  readT = setTimeout(() => api("/api/read", { chat: S.cur }).catch(() => {}), 300);
}

// ------------------------------------------------------------------ radio dashboard
async function pollRadio() {
  try {
    S.radio = await api("/api/radio");
    renderConvHead();
    renderIncoming();
    // my avatar breathes, the logo and the dashboard's light pulse while frames are being heard
    const fr = S.radio.mac?.frames_ok || 0;
    const heard = fr > S.lastFrames;
    $("#meAvatar").classList.toggle("live", heard);
    $("#liveDot")?.classList.toggle("on", heard);
    document.querySelectorAll(".gc-radiobtn .livedot").forEach((d) => d.classList.toggle("on", heard));
    if (heard && S.lastFrames) BRAND.ping();
    S.lastFrames = fr;
    updateBusy();
    if (S.dashOpen) renderDash();
  } catch (e) { /* station restarting */ }
}
// the radio dashboard: beside the chat, or over the game center (the Radio button there)
function showDash(open) {
  S.dashOpen = open;
  $("#dash").hidden = !open;
  if (open) { SETTINGS.close(); renderDash(); }
  NAV.mark();
  if (typeof GC !== "undefined") GC.markRadio();
}
function renderIncoming() {
  const el = $("#pending");
  const inc = (S.radio?.incoming || []).filter((x) => S.cur === GROUP || S.cur === `u:${x.src}`);
  if (!inc.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = inc.map((x) => `<div class="inc"><span>↓ receiving from ${esc(contactName(x.src))}: ${x.have}/${x.count} fragments</span>
    <div class="prog"><i style="width:${Math.round(100 * x.have / x.count)}%"></i></div>
    <button class="xbtn" data-src="${x.src}" data-id="${x.id}" title="Stop receiving this" aria-label="Stop receiving">${XSVG}</button></div>`).join("");
  el.querySelectorAll(".xbtn").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api("/api/cancel_in", { src: +b.dataset.src, id: +b.dataset.id }); SFX.play("pop"); pollRadio(); }
    catch (e) { toast(e.message); b.disabled = false; }
  }));
}
function tile(k, v, unit = "") { return `<div class="tile"><div class="k">${k}</div><div class="v">${v}<small> ${unit}</small></div></div>`; }
function renderDash() {
  const R = S.radio; if (!R) return;
  const r = R.radio, m = R.mac;
  $("#tiles").innerHTML =
    tile("Last frame", r.last_mod ? MODNAME[r.last_mod] : "–") + tile("Symbol SNR", r.last_esn0_db ?? "–", "dB") +
    tile("Eb/N0", r.last_snr_db ?? "–", "dB") + tile("Chip SNR", r.last_chip_snr_db ?? "–", "dB") +
    tile("Last CFO", r.last_cfo_hz !== undefined ? (r.last_cfo_hz / 1000).toFixed(2) : "–", "kHz") +
    tile("Preamble metric", r.last_metric ?? "–") +
    tile("Frames OK", m.frames_ok || 0) + tile("CRC-32 drops", (m.crc_fail || 0) + (r.crc_drops || 0)) +
    tile("Retransmissions", m.retransmissions || 0) + tile("Bursts sent", r.tx_bursts || 0) +
    tile("LDPC frames", r.fec_frames || 0) + tile("LDPC iterations", r.fec_frames ? (r.fec_iters / r.fec_frames).toFixed(1) : "–", "avg");
  drawConst(r.constellation || [], mcsBase(r.last_mod));
  drawSpec(R.spectrum);
  const links = Object.entries(R.links || {});
  $("#linkTbl").innerHTML = "<tr><th>station</th><th>Es/N0</th><th>their SNR of us</th><th>CFO</th><th>TX mod</th></tr>" + (links.length ? links.map(([a, q]) =>
    `<tr><td>${esc(contactName(a))} #${a}</td><td>${q.esn0_db ?? q.snr_db} dB</td><td>${q.fb_esn0 !== undefined ? q.fb_esn0.toFixed(1) + " dB" : "–"}</td><td>${(q.cfo_hz / 1000).toFixed(2)} kHz</td><td>${MODNAME[S.contacts.get(+a)?.tx_mod] || "–"}</td></tr>`).join("")
    : `<tr><td colspan=5>nothing heard yet</td></tr>`);
  const q = R.queue || [];
  const PR = ["bulk", "normal", "high", "ctrl"];
  $("#queueTbl").innerHTML = "<tr><th>to</th><th>prio</th><th>frags</th><th>mod</th><th>retries</th></tr>" + (q.length ? q.map((x) =>
    `<tr><td>${esc(contactName(x.dst))}</td><td>${PR[x.prio]}</td><td>${x.acked}/${x.frags}</td><td>${MODNAME[x.mod] || ""}</td><td>${x.retries}</td></tr>`).join("")
    : `<tr><td colspan=5>idle</td></tr>`);
  const ctr = { "detections": r.detections, "header CRC-8 fails": r.header_fail || 0, "PHY frames": r.frames, "data frames sent": m.data_tx || 0,
    "ACKs sent": m.acks_tx || 0, "ACK timeouts": m.timeouts || 0, "duplicates": m.dup_frames || 0, "not for me": m.not_for_me || 0,
    "messages rx": m.msgs_rx || 0, "messages delivered": m.msgs_delivered || 0, "messages failed": m.msgs_failed || 0,
    "datagrams sent / heard": `${m.dgram_tx || 0} / ${m.dgram_rx || 0}`,
    "beacons sent": m.beacons_tx || 0, "LDPC decode failures": r.fec_fail || 0, "TX underruns": r.tx_underruns || 0, "RX overflows": r.rx_overflow_chunks || 0, "active TX codes": (r.tx_active_codes || []).join(", ") || "–" };
  $("#ctrTbl").innerHTML = Object.entries(ctr).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
  if (R.sim) {
    $("#simPanel").hidden = false;
    if (document.activeElement !== $("#ebSlider")) $("#ebSlider").value = R.sim.ebn0_db;
    $("#ebVal").textContent = `${R.sim.ebn0_db} dB`;
    $("#simCpu").textContent = `simulator load ${Math.round(R.sim.cpu_load * 100)}% of real time · ` +
      R.sim.links.filter((l) => l.rx === S.me.addr).map((l) => `#${l.tx}→me ${l.ebn0_db} dB`).join(", ");
  }
  const p = S.phy;
  const rows = { "carrier": `${(p.center_freq / 1e9).toFixed(4)} GHz`, "sample rate": `${p.samp_rate / 1e6} MS/s`, "chip rate": `${p.chip_rate / 1e6} Mchip/s`,
    "profile": p.profile, "spreading": `${p.sf}-chip Gold (deg ${p.gold_degree})`, "processing gain": `${p.processing_gain_db} dB`,
    "BPSK / QPSK / 16-QAM": `${(p.bit_rate / 1e3).toFixed(1)} / ${(2 * p.bit_rate / 1e3).toFixed(1)} / ${(4 * p.bit_rate / 1e3).toFixed(1)} kb/s uncoded`,
    "FEC": "LDPC, 5G NR base graph 1, rate ½ ⅔ ¾ ⅚ (adaptive)",
    "pulse": `RRC α=${p.rolloff}`, "preamble": `${p.preamble_bits} bits`, "max burst": `${p.max_phy_payload} B` };
  $("#phyTbl").innerHTML = Object.entries(rows).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
}
function cvCtx(cv) {
  const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  const c = cv.getContext("2d"); c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
  return [c, w, h];
}
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
// "#rrggbb" with an alpha, for canvas gradients
function hexA(hex, a) {
  const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})` : hex;
}
function drawConst(pts, mod) {
  const [c, w, h] = cvCtx($("#constCv"));
  const sc = w / 3.2, cx = w / 2, cy = h / 2;
  c.strokeStyle = css("--line"); c.lineWidth = 1;
  c.beginPath(); c.moveTo(0, cy); c.lineTo(w, cy); c.moveTo(cx, 0); c.lineTo(cx, h); c.stroke();
  c.fillStyle = css("--muted"); c.font = "11px system-ui"; c.fillText("I", w - 12, cy - 5); c.fillText("Q", cx + 5, 12);
  let ref = [[-1, 0], [1, 0]];
  mod = mcsBase(mod);
  if (mod === "qpsk") ref = [[1, 1], [1, -1], [-1, 1], [-1, -1]].map(([i, q]) => [i / Math.SQRT2, q / Math.SQRT2]);
  if (mod === "16qam") { ref = []; for (const i of [-3, -1, 1, 3]) for (const q of [-3, -1, 1, 3]) ref.push([i / Math.sqrt(10), q / Math.sqrt(10)]); }
  c.strokeStyle = css("--ink2"); c.setLineDash([3, 3]);
  for (const [i, q] of ref) { c.beginPath(); c.arc(cx + i * sc, cy - q * sc, 4, 0, 7); c.stroke(); }
  c.setLineDash([]);
  const pg = c.createLinearGradient(0, 0, w, h);
  pg.addColorStop(0, css("--accent")); pg.addColorStop(1, css("--accent2"));
  c.fillStyle = pg; c.globalAlpha = 0.8; c.shadowColor = css("--accent"); c.shadowBlur = 6;
  for (const [i, q] of pts) { c.beginPath(); c.arc(cx + i * sc, cy - q * sc, 2.2, 0, 7); c.fill(); }
  c.globalAlpha = 1; c.shadowBlur = 0;
  $("#constInfo").textContent = pts.length ? `(${BASENAME[mod] || mod}, ${pts.length} symbols, despread)` : "";
}
function drawSpec(sp) {
  const [c, w, h] = cvCtx($("#specCv"));
  if (!sp || !sp.psd.length) { c.fillStyle = css("--muted"); c.fillText("no samples yet", 10, 20); return; }
  const lo = Math.min(...sp.psd), hi = Math.max(...sp.psd) + 3;
  const y = (v) => h - 14 - ((v - lo) / (hi - lo || 1)) * (h - 22);
  c.strokeStyle = css("--line");
  c.beginPath(); c.moveTo(w / 2, 0); c.lineTo(w / 2, h - 14); c.stroke();
  const line = c.createLinearGradient(0, 0, w, 0);
  line.addColorStop(0, css("--accent")); line.addColorStop(0.5, css("--accent2")); line.addColorStop(1, css("--accent"));
  const fill = c.createLinearGradient(0, 0, 0, h - 14);
  fill.addColorStop(0, hexA(css("--accent2"), 0.35)); fill.addColorStop(1, hexA(css("--accent2"), 0));
  c.beginPath();
  sp.psd.forEach((v, i) => { const x = (i / (sp.psd.length - 1)) * w; i ? c.lineTo(x, y(v)) : c.moveTo(x, y(v)); });
  c.lineTo(w, h - 14); c.lineTo(0, h - 14); c.closePath();
  c.fillStyle = fill; c.fill();
  c.strokeStyle = line; c.lineWidth = 1.6; c.shadowColor = css("--accent2"); c.shadowBlur = 8; c.beginPath();
  sp.psd.forEach((v, i) => { const x = (i / (sp.psd.length - 1)) * w; i ? c.lineTo(x, y(v)) : c.moveTo(x, y(v)); });
  c.stroke(); c.shadowBlur = 0;
  c.fillStyle = css("--muted"); c.font = "10px system-ui";
  c.fillText(`${sp.freqs[0]} kHz`, 2, h - 2); c.fillText("0", w / 2 - 3, h - 2);
  c.fillText(`+${sp.freqs[sp.freqs.length - 1]} kHz`, w - 58, h - 2);
  c.fillText(`${Math.round(hi)} dB`, 2, 10);
}

// ------------------------------------------------------------------ wiring
function autosize() { const t = $("#text"); t.style.height = "auto"; t.style.height = Math.min(140, t.scrollHeight) + "px"; }
function updateSendBtn() { $("#sendBtn").classList.toggle("hastext", !!$("#text").value.trim()); }
function setUrgent(v) { S.urgent = v; $("#urgentBtn").setAttribute("aria-pressed", String(v)); }
function wire() {
  $("#text").addEventListener("input", () => { autosize(); updateSendBtn(); });
  $("#text").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); } });
  $("#sendBtn").onclick = () => ($("#text").value.trim() ? sendText() : MEDIA.startRec(MEDIA.ctx("main")));
  $("#attachBtn").onclick = () => $("#fileIn").click();
  $("#fileIn").onchange = (e) => { MEDIA.onFile(e.target.files[0], MEDIA.ctx("main")); e.target.value = ""; };
  $("#urgentBtn").onclick = () => setUrgent(!S.urgent);
  $("#gameBtn").onclick = newGame;
  $("#backBtn").onclick = () => { S.cur = null; $("#app").classList.remove("inchat"); $("#conv").hidden = true; $("#empty").hidden = false; renderChats(); };
  $("#filter").oninput = renderChats;
  $("#radioBtn").onclick = () => showDash(!S.dashOpen);
  $("#dashClose").onclick = () => showDash(false);
  $("#lightbox").onclick = () => ($("#lightbox").hidden = true);
  $("#meName").onclick = () => SETTINGS.open();
  $("#meAvatar").onclick = () => SETTINGS.open();
  $("#ebSlider").onchange = (e) => api("/api/sim", { ebn0_db: +e.target.value }).then(pollRadio).catch((x) => toast(x.message));
  $("#clearBtn").onclick = () => confirm("Delete this station's chat history?") && api("/api/clear", {});
  $("#jumpBtn").onclick = jumpToNew;
  $("#msgs").addEventListener("scroll", updateJump, { passive: true });
  document.addEventListener("visibilitychange", () => { markRead(); observeFresh(); });
  // drag & drop
  $("#main").addEventListener("dragover", (e) => e.preventDefault());
  $("#main").addEventListener("drop", (e) => { e.preventDefault(); MEDIA.onFile(e.dataTransfer.files[0], MEDIA.ctx("main")); });
  // a pasted picture goes to the chat being typed in (in the game center: the chat panel, when it is open)
  document.addEventListener("paste", (e) => {
    const f = [...(e.clipboardData?.files || [])][0], where = MEDIA.active();
    if (f && where) MEDIA.onFile(f, MEDIA.ctx(where));
  });
}
// start once every script has loaded (settings.js, media.js ... define what wire() uses)
document.addEventListener("DOMContentLoaded", () => {
  wire();
  connect();
  setInterval(pollRadio, 1000);
  setInterval(() => S.cur && renderConvHead(), 5000);
});
