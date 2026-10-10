"use strict";
/* CDMA Chat front end — vanilla JS, no external dependencies (works offline in the lab). */

const $ = (s, el = document) => el.querySelector(s);
const S = {
  me: null, phy: null, contacts: new Map(), msgs: new Map(), order: [], unread: {},
  cur: null, urgent: false, maxBytes: 400000, radio: null, dashOpen: false,
};
const GROUP = "g:all";

// ------------------------------------------------------------------ utils
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hue = (n) => (n * 67 + 160) % 360;
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDay = (t) => new Date(t * 1000).toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" });
const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(2)} MB`;
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
const contactName = (addr) => (S.contacts.get(+addr)?.name) || `Node ${addr}`;
const chatName = (id) => id === GROUP ? "Everyone" : contactName(id.split(":")[1]);
function avatar(el, id) {
  if (id === GROUP) { el.textContent = "✱"; el.style.background = "var(--accent)"; el.classList.remove("on"); return; }
  const a = +id.split(":")[1];
  const nm = a === S.me?.addr ? S.me.name : contactName(a);
  el.textContent = nm.trim().slice(0, 1).toUpperCase();
  el.style.background = `hsl(${hue(a)} 45% 42%)`;
  el.classList.toggle("on", !!S.contacts.get(a)?.online);
}
const airtime = (bytes) => bytes * 8 / ((S.phy?.bit_rate || 32000) * 0.85);

// ------------------------------------------------------------------ icons
const TICK1 = '<svg viewBox="0 0 18 12"><path d="M5.6 9.2 2 5.6.9 6.7l4.7 4.7L15.4 1.6 14.3.5z"/></svg>';
const TICK2 = '<svg viewBox="0 0 18 12"><path d="M5.6 9.2 2 5.6.9 6.7l4.7 4.7L15.4 1.6 14.3.5z"/><path d="m9.6 9.2-.9-.9-1.1 1.1 2 2L19.4 1.6 18.3.5z" transform="translate(-2 0)"/></svg>';
const CLOCK = '<svg viewBox="0 0 18 12"><circle cx="9" cy="6" r="4.6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8.4 3.5h1.2v2.8l1.8 1-.6 1-2.4-1.4z"/></svg>';
const BANG = '<svg viewBox="0 0 18 12"><circle cx="9" cy="6" r="5.5"/><path d="M8.3 2.8h1.4v4H8.3zm0 4.9h1.4v1.4H8.3z" fill="#fff"/></svg>';
const LOCK = '<svg class="lock" viewBox="0 0 24 24"><path d="M17 9h-1V7a4 4 0 0 0-8 0v2H7a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-9a2 2 0 0 0-2-2zm-7-2a2 2 0 0 1 4 0v2h-4z"/></svg>';
function ticks(rec) {
  const st = rec.status;
  const tip = Object.entries(rec.recip || {}).map(([a, s]) => `${contactName(a)}: ${s}`).join("\n");
  if (st === "queued") return `<span class="ticks" title="${esc(tip || "queued")}">${CLOCK}</span>`;
  if (st === "sent") return `<span class="ticks" title="${esc(tip)}">${TICK1}</span>`;
  if (st === "delivered") return `<span class="ticks" title="${esc(tip)}">${TICK2}</span>`;
  if (st === "read") return `<span class="ticks read" title="${esc(tip)}">${TICK2}</span>`;
  if (st === "failed") return `<span class="ticks failed" title="${esc(tip || "failed")}">${BANG}</span>`;
  return "";
}

// ------------------------------------------------------------------ state load + SSE
async function load() {
  const st = await api("/api/state");
  S.me = st.me; S.phy = st.phy; S.maxBytes = st.max_bytes; S.unread = st.unread || {};
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
    else if (ev === "contacts") { S.contacts = new Map(data.map((c) => [c.addr, c])); renderChats(); renderConvHead(); }
    else if (ev === "unread") { S.unread = data; renderChats(); }
    else if (ev === "toast") toast(data, 4000);
    else if (ev === "reset") load();
  };
  es.onerror = () => { $("#sideFoot").textContent = "Reconnecting to station…"; };
  es.onopen = () => { load(); };
}
function onMsg(rec) {
  const isNew = !S.msgs.has(rec.uid);
  S.msgs.set(rec.uid, rec);
  if (isNew) S.order.push(rec.uid);
  if (rec.chat === S.cur) {
    if (rec.kind === "game") renderConv(false);
    else upsertMsgEl(rec, isNew);
    if (isNew && rec.frm !== S.me.addr) markRead();
  }
  renderChats();
}

// ------------------------------------------------------------------ sidebar
function renderMe() {
  $("#meName").textContent = S.me.name;
  $("#meSub").textContent = `node ${S.me.addr} · ${S.me.mode === "sim" ? "simulation" : "ANTSDR"}${S.me.enc ? " · encrypted (key " + S.me.key_id + ")" : ""}`;
  avatar($("#meAvatar"), `u:${S.me.addr}`); $("#meAvatar").classList.remove("on");
  const p = S.phy;
  $("#phyLine").textContent = `${(p.center_freq / 1e9).toFixed(3)} GHz · DS-CDMA BPSK · ${p.sf}-chip Gold · ${(p.bit_rate / 1e3).toFixed(1)} kb/s · my code #${S.me.addr}`;
  document.title = `${S.me.name} · CDMA Chat`;
}
function lastMsg(chat) {
  for (let i = S.order.length - 1; i >= 0; i--) { const m = S.msgs.get(S.order[i]); if (m.chat === chat) return m; }
  return null;
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
      if (r.id === GROUP) sub = `${[...S.contacts.values()].filter((c) => c.online).length} stations online`;
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
}

// ------------------------------------------------------------------ conversation
function openChat(id) {
  S.cur = id;
  $("#empty").hidden = true; $("#conv").hidden = false;
  $("#app").classList.add("inchat");
  $("#gameBtn").hidden = id === GROUP;
  renderChats(); renderConv(true); markRead();
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
    if (c && c.key_ok === false && S.me.enc) sub += " · ⚠ different encryption key";
  }
  $("#convSub").textContent = sub;
}
function renderConv(scroll) {
  renderConvHead();
  const box = $("#msgs");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = "";
  let day = "";
  const latestGame = new Map();
  for (const uid of S.order) { const m = S.msgs.get(uid); if (m.chat === S.cur && m.kind === "game") latestGame.set(m.extra?.gid, uid); }
  for (const uid of S.order) {
    const m = S.msgs.get(uid);
    if (m.chat !== S.cur) continue;
    if (m.kind === "game" && latestGame.get(m.extra?.gid) !== uid) continue;
    const d = fmtDay(m.ts);
    if (d !== day) { day = d; const el = document.createElement("div"); el.className = "day"; el.textContent = d; box.appendChild(el); }
    box.appendChild(msgEl(m));
  }
  if (scroll || atBottom) box.scrollTop = box.scrollHeight;
}
function upsertMsgEl(m, isNew) {
  const old = document.getElementById("m-" + m.uid);
  if (old) {
    // keep media elements (avoid re-loading audio/images) — only refresh meta + progress
    const fresh = msgEl(m);
    const oldMedia = old.querySelector("img.media, audio");
    if (oldMedia && fresh.querySelector("img.media, audio")) {
      old.querySelector(".meta").replaceWith(fresh.querySelector(".meta"));
      const op = old.querySelector(".progwrap"), np = fresh.querySelector(".progwrap");
      if (op && np) op.replaceWith(np); else if (op) op.remove();
    } else old.replaceWith(fresh);
  } else if (isNew) {
    const box = $("#msgs");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    box.appendChild(msgEl(m));
    if (atBottom || m.frm === S.me.addr) box.scrollTop = box.scrollHeight;
  }
}
function msgEl(m) {
  const mine = m.frm === S.me.addr;
  const el = document.createElement("div");
  el.id = "m-" + m.uid;
  el.className = "msg" + (mine ? " mine" : "") + (m.urgent ? " urgent" : "");
  let h = "";
  if (!mine && m.chat === GROUP) h += `<div class="from" style="color:hsl(${hue(m.frm)} 55% 52%)">${esc(contactName(m.frm))}</div>`;
  const url = m.file ? `/media/${encodeURIComponent(m.file)}` : "";
  if (m.kind === "text") h += `<div class="txt">${esc(m.text)}</div>`;
  else if (m.kind === "image") h += `<img class="media" src="${url}" alt="${esc(m.name || "image")}" loading="lazy">`;
  else if (m.kind === "audio") h += `<audio controls preload="metadata" src="${url}"></audio>`;
  else if (m.kind === "file") {
    const ext = (m.name.split(".").pop() || "file").slice(0, 4).toUpperCase();
    h += `<a class="file" href="${url}" download="${esc(m.name)}"><span class="fi">${esc(ext)}</span>
      <span><div>${esc(m.name)}</div><div class="progtxt">${fmtBytes(m.size)}</div></span></a>`;
  } else if (m.kind === "game") h += gameHtml(m);
  if (mine && m.size > 230 && m.status !== "delivered" && m.status !== "read") {
    const pct = Math.round((m.progress || 0) * 100);
    h += `<div class="progwrap"><div class="prog"><i style="width:${pct}%"></i></div>
      <div class="progtxt">${m.status === "failed" ? "failed" : `${pct}% of ${fmtBytes(m.size)}`}${m.retries ? ` · ${m.retries} retries` : ""}</div></div>`;
  }
  let info = "";
  if (mine && m.goodput_kbps && m.size > 230) info = `${m.airtime_s}s · ${m.goodput_kbps} kb/s`;
  if (!mine && m.snr_db !== undefined && m.kind !== "game") info = `${m.snr_db} dB`;
  h += `<div class="meta">${m.enc ? LOCK : ""}${m.urgent ? "⚡" : ""}${info ? `<span class="info">${esc(info)}</span>` : ""}
    <span>${fmtTime(m.ts)}</span>${mine ? ticks(m) : ""}</div>`;
  el.innerHTML = h;
  const img = el.querySelector("img.media");
  if (img) img.onclick = () => { $("#lightbox img").src = img.src; $("#lightbox").hidden = false; };
  el.querySelectorAll(".game button[data-i]").forEach((b) => (b.onclick = () => gameMove(m, +b.dataset.i)));
  return el;
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
  try { await api("/api/game", { to: m.chat, state: g }); } catch (e) { toast(e.message); }
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
  try { await api("/api/send", { to: S.cur, text, urgent: S.urgent }); }
  catch (e) { toast(e.message); ta.value = text; updateSendBtn(); }
  setUrgent(false);
}
async function upload(blob, kind, name, mime, extraQs = "") {
  if (blob.size > S.maxBytes - 600) { toast(`Too large for the link (${fmtBytes(blob.size)}, max ${fmtBytes(S.maxBytes - 600)})`); return; }
  const qs = new URLSearchParams({ to: S.cur, kind, name, mime, urgent: S.urgent ? "1" : "0" });
  try {
    await api(`/api/upload?${qs}${extraQs}`, blob, true);
    toast(`Sending ${fmtBytes(blob.size)} — about ${Math.max(1, Math.round(airtime(blob.size)))} s on air`);
  } catch (e) { toast(e.message); }
  setUrgent(false);
}
function compressImage(file, maxDim = 640, q = 0.6) {
  return new Promise((res, rej) => {
    const img = new Image();
    img.onload = () => {
      const s = Math.min(1, maxDim / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * s); c.height = Math.round(img.height * s);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      const tryType = (type) => new Promise((r) => c.toBlob(r, type, q));
      tryType("image/webp").then((b) => (b && b.type === "image/webp" ? b : tryType("image/jpeg"))).then(res);
      URL.revokeObjectURL(img.src);
    };
    img.onerror = rej;
    img.src = URL.createObjectURL(file);
  });
}
async function onFile(file) {
  if (!file || !S.cur) return;
  if (file.type.startsWith("image/") && file.type !== "image/gif") {
    const b = await compressImage(file);
    const ext = b.type === "image/webp" ? ".webp" : ".jpg";
    await upload(b, "image", file.name.replace(/\.[^.]+$/, "") + ext, b.type);
  } else if (file.type.startsWith("audio/")) await upload(file, "audio", file.name, file.type);
  else await upload(file, "file", file.name, file.type || "application/octet-stream");
}

// ------------------------------------------------------------------ voice notes
const REC = { mr: null, chunks: [], t0: 0, timer: null, cancel: false };
async function startRec() {
  if (!navigator.mediaDevices?.getUserMedia) { toast("Microphone needs http://localhost (or HTTPS). Attach an audio file instead."); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true } }); }
  catch (e) { toast("Microphone permission denied"); return; }
  const types = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"];
  const mimeType = types.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
  REC.mr = new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 16000 });
  REC.chunks = []; REC.cancel = false;
  REC.mr.ondataavailable = (e) => e.data.size && REC.chunks.push(e.data);
  REC.mr.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(REC.timer);
    $("#recBar").hidden = true; $(".composer").hidden = false;
    if (REC.cancel) return;
    const dur = (performance.now() - REC.t0) / 1000;
    const type = (REC.mr.mimeType || "audio/webm").split(";")[0];
    const blob = new Blob(REC.chunks, { type });
    await upload(blob, "audio", `voice-${Date.now()}.${type.split("/")[1]}`, type, `&dur=${dur.toFixed(1)}`);
  };
  REC.mr.start(250); REC.t0 = performance.now();
  $(".composer").hidden = true; $("#recBar").hidden = false;
  REC.timer = setInterval(() => {
    const s = (performance.now() - REC.t0) / 1000;
    $("#recTime").textContent = `0:${String(Math.floor(s)).padStart(2, "0")}`;
    if (s >= 15) REC.mr.state === "recording" && REC.mr.stop();
  }, 200);
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
    if (S.dashOpen) renderDash();
  } catch (e) { /* station restarting */ }
}
function renderIncoming() {
  const el = $("#pending");
  const inc = (S.radio?.incoming || []).filter((x) => S.cur === GROUP || S.cur === `u:${x.src}`);
  if (!inc.length) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = inc.map((x) => `↓ receiving from ${contactName(x.src)}: ${x.have}/${x.count} fragments`).join("   ");
}
function tile(k, v, unit = "") { return `<div class="tile"><div class="k">${k}</div><div class="v">${v}<small> ${unit}</small></div></div>`; }
function renderDash() {
  const R = S.radio; if (!R) return;
  const r = R.radio, m = R.mac;
  $("#tiles").innerHTML =
    tile("Last Eb/N0", r.last_snr_db ?? "–", "dB") + tile("Chip SNR", r.last_chip_snr_db ?? "–", "dB") +
    tile("Last CFO", r.last_cfo_hz !== undefined ? (r.last_cfo_hz / 1000).toFixed(2) : "–", "kHz") +
    tile("Preamble metric", r.last_metric ?? "–") +
    tile("Frames OK", m.frames_ok || 0) + tile("CRC-32 drops", m.crc_fail || 0) +
    tile("Retransmissions", m.retransmissions || 0) + tile("Bursts sent", r.tx_bursts || 0);
  drawConst(r.constellation || []);
  drawSpec(R.spectrum);
  const links = Object.entries(R.links || {});
  $("#linkTbl").innerHTML = "<tr><th>station</th><th>Eb/N0</th><th>CFO</th><th>seen</th></tr>" + (links.length ? links.map(([a, q]) =>
    `<tr><td>${esc(contactName(a))} #${a}</td><td>${q.snr_db} dB</td><td>${(q.cfo_hz / 1000).toFixed(2)} kHz</td><td>${Math.round(Date.now() / 1000 - q.t)} s</td></tr>`).join("")
    : `<tr><td colspan=4>nothing heard yet</td></tr>`);
  const q = R.queue || [];
  const PR = ["bulk", "normal", "high", "ctrl"];
  $("#queueTbl").innerHTML = "<tr><th>to</th><th>prio</th><th>frags</th><th>retries</th></tr>" + (q.length ? q.map((x) =>
    `<tr><td>${esc(contactName(x.dst))}</td><td>${PR[x.prio]}</td><td>${x.acked}/${x.frags}</td><td>${x.retries}</td></tr>`).join("")
    : `<tr><td colspan=4>idle</td></tr>`);
  const ctr = { "detections": r.detections, "header CRC-8 fails": r.header_fail || 0, "PHY frames": r.frames, "data frames sent": m.data_tx || 0,
    "ACKs sent": m.acks_tx || 0, "ACK timeouts": m.timeouts || 0, "duplicates": m.dup_frames || 0, "not for me": m.not_for_me || 0,
    "messages rx": m.msgs_rx || 0, "messages delivered": m.msgs_delivered || 0, "messages failed": m.msgs_failed || 0,
    "beacons sent": m.beacons_tx || 0, "active TX codes": (r.tx_active_codes || []).join(", ") || "–" };
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
    "spreading": `${p.sf}-chip Gold (deg ${p.gold_degree})`, "processing gain": `${p.processing_gain_db} dB`, "bit rate": `${(p.bit_rate / 1e3).toFixed(2)} kb/s`,
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
function drawConst(pts) {
  const [c, w, h] = cvCtx($("#constCv"));
  const sc = w / 4.4, cx = w / 2, cy = h / 2;
  c.strokeStyle = css("--line"); c.lineWidth = 1;
  c.beginPath(); c.moveTo(0, cy); c.lineTo(w, cy); c.moveTo(cx, 0); c.lineTo(cx, h); c.stroke();
  c.fillStyle = css("--muted"); c.font = "11px system-ui"; c.fillText("I", w - 12, cy - 5); c.fillText("Q", cx + 5, 12);
  c.strokeStyle = css("--ink2"); c.setLineDash([3, 3]);
  for (const x of [-1, 1]) { c.beginPath(); c.arc(cx + x * sc, cy, 4, 0, 7); c.stroke(); }
  c.setLineDash([]);
  c.fillStyle = css("--accent"); c.globalAlpha = 0.7;
  for (const [i, q] of pts) { c.beginPath(); c.arc(cx + i * sc, cy - q * sc, 2.2, 0, 7); c.fill(); }
  c.globalAlpha = 1;
  $("#constInfo").textContent = pts.length ? `(${pts.length} symbols, despread)` : "";
}
function drawSpec(sp) {
  const [c, w, h] = cvCtx($("#specCv"));
  if (!sp || !sp.psd.length) { c.fillStyle = css("--muted"); c.fillText("no samples yet", 10, 20); return; }
  const lo = Math.min(...sp.psd), hi = Math.max(...sp.psd) + 3;
  const y = (v) => h - 14 - ((v - lo) / (hi - lo || 1)) * (h - 22);
  c.strokeStyle = css("--line");
  c.beginPath(); c.moveTo(w / 2, 0); c.lineTo(w / 2, h - 14); c.stroke();
  c.strokeStyle = css("--accent"); c.lineWidth = 1.5; c.beginPath();
  sp.psd.forEach((v, i) => { const x = (i / (sp.psd.length - 1)) * w; i ? c.lineTo(x, y(v)) : c.moveTo(x, y(v)); });
  c.stroke();
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
  $("#sendBtn").onclick = () => ($("#text").value.trim() ? sendText() : startRec());
  $("#recSend").onclick = () => REC.mr?.state === "recording" && REC.mr.stop();
  $("#recCancel").onclick = () => { REC.cancel = true; REC.mr?.state === "recording" && REC.mr.stop(); };
  $("#attachBtn").onclick = () => $("#fileIn").click();
  $("#fileIn").onchange = (e) => { onFile(e.target.files[0]); e.target.value = ""; };
  $("#urgentBtn").onclick = () => setUrgent(!S.urgent);
  $("#gameBtn").onclick = newGame;
  $("#backBtn").onclick = () => { S.cur = null; $("#app").classList.remove("inchat"); $("#conv").hidden = true; $("#empty").hidden = false; renderChats(); };
  $("#filter").oninput = renderChats;
  $("#radioBtn").onclick = () => { S.dashOpen = !S.dashOpen; $("#dash").hidden = !S.dashOpen; if (S.dashOpen) renderDash(); };
  $("#dashClose").onclick = () => { S.dashOpen = false; $("#dash").hidden = true; };
  $("#lightbox").onclick = () => ($("#lightbox").hidden = true);
  $("#meName").onclick = async () => {
    const n = prompt("Station name", S.me.name);
    if (n && n.trim()) { await api("/api/name", { name: n.trim() }); S.me.name = n.trim(); renderMe(); }
  };
  $("#ebSlider").onchange = (e) => api("/api/sim", { ebn0_db: +e.target.value }).then(pollRadio).catch((x) => toast(x.message));
  $("#clearBtn").onclick = () => confirm("Delete this station's chat history?") && api("/api/clear", {});
  document.addEventListener("visibilitychange", markRead);
  // drag & drop
  $("#main").addEventListener("dragover", (e) => e.preventDefault());
  $("#main").addEventListener("drop", (e) => { e.preventDefault(); onFile(e.dataTransfer.files[0]); });
  document.addEventListener("paste", (e) => { const f = [...(e.clipboardData?.files || [])][0]; if (f && S.cur) onFile(f); });
}
wire();
connect();
setInterval(pollRadio, 1000);
setInterval(() => S.cur && renderConvHead(), 5000);
