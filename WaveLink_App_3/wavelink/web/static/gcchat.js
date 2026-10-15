"use strict";
/* Chats inside the game center: a Chat button in the game center's top bar opens a panel at the side
   where you can read and send messages while UNO or the race goes on behind it.  Nothing in the game
   stops: the race keeps running and posting your car (while you type, the keys go to the message box,
   so the car rolls on without steering) and UNO's turn clock keeps counting. */

const GCCHAT = { open: false, chat: null };
const GCC_ICON = '<svg viewBox="0 0 24 24"><path d="M4 4h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2zm3 5v2h10V9zm0 4v2h7v-2z" transform="translate(-1 0)"/></svg>';

GCCHAT.unread = () => Object.values(S.unread || {}).reduce((a, b) => a + (b || 0), 0);

// the button in the game center's top bar (games.js GC.bar)
GCCHAT.button = function () {
  const n = GCCHAT.unread();
  return `<button class="gc-ghost gc-chatbtn" data-act="chat" title="Chats: read and send while you play" aria-label="Chats"
    aria-expanded="${GCCHAT.open}">${GCC_ICON}<span>Chat</span><b class="n">${n || ""}</b></button>`;
};

GCCHAT.chats = function () {
  const ids = new Set([GROUP]);
  for (const a of S.contacts.keys()) ids.add(`u:${a}`);
  for (const uid of S.order) ids.add(S.msgs.get(uid).chat);
  return [...ids].sort((a, b) => (b === GROUP) - (a === GROUP) || (lastMsg(b)?.ts || 0) - (lastMsg(a)?.ts || 0));
};

GCCHAT.toggle = () => (GCCHAT.open ? GCCHAT.close() : GCCHAT.show());

GCCHAT.show = function (chat) {
  const list = GCCHAT.chats();
  GCCHAT.chat = chat || GCCHAT.chat || list.find((c) => S.unread[c]) || S.cur || list.find((c) => lastMsg(c)) || GROUP;
  GCCHAT.open = true;
  $("#gcChat").hidden = false;
  GCCHAT.render();
  GCCHAT.read();
  if (typeof RACE !== "undefined") RACE.keys.clear();       // a key held down now would stay down
  $("#gccText").focus();
};

GCCHAT.close = function () {
  GCCHAT.open = false;
  $("#gcChat").hidden = true;
  if (document.activeElement === $("#gccText")) $("#gccText").blur();   // the keys go back to the game
  GCCHAT.refresh();
};

GCCHAT.read = function () {
  if (!GCCHAT.open || !GCCHAT.chat || document.hidden) return;
  if (S.unread[GCCHAT.chat]) api("/api/read", { chat: GCCHAT.chat }).catch(() => {});
};

// the unread counts on the top-bar button and the tabs
GCCHAT.refresh = function () {
  const n = GCCHAT.unread();
  document.querySelectorAll(".gc-chatbtn").forEach((b) => {
    b.querySelector(".n").textContent = n ? String(n) : "";
    b.setAttribute("aria-expanded", String(GCCHAT.open));
  });
  if (GCCHAT.open) GCCHAT.renderTabs();
};

GCCHAT.renderTabs = function () {
  $("#gccTabs").innerHTML = GCCHAT.chats().map((id) => {
    const n = S.unread[id] || 0, addr = id === GROUP ? null : +id.split(":")[1];
    const av = id === GROUP ? `<span class="gc-av" style="background:var(--grad)">✱</span>` : avatarHtml(addr, "gc-av");
    return `<button role="tab" data-chat="${id}" aria-selected="${id === GCCHAT.chat}" title="${esc(chatName(id))}">${av}
      <span class="nm">${esc(chatName(id))}</span>${n ? `<b class="n">${n}</b>` : ""}</button>`;
  }).join("");
  $("#gccTabs").querySelectorAll("button").forEach((b) => (b.onclick = () => { GCCHAT.chat = b.dataset.chat; GCCHAT.render(); GCCHAT.read(); $("#gccText").focus(); }));
};

GCCHAT.msgHtml = function (m) {
  const mine = m.frm === S.me.addr, url = m.file ? `/media/${encodeURIComponent(m.file)}` : "";
  let body;
  if (m.kind === "text") body = `<div class="t${isJumbo(m.text) ? " jumbo" : ""}">${esc(m.text)}</div>`;
  else if (m.kind === "image") body = `<img src="${url}" alt="${esc(m.name || "photo")}" loading="lazy">`;
  else if (m.kind === "audio") body = `<audio controls preload="none" src="${url}"></audio>`;
  else if (m.kind === "file") body = `<a href="${url}" download="${esc(m.name)}">📎 ${esc(m.name)} <small>${fmtBytes(m.size)}</small></a>`;
  else if (m.kind === "game") body = `<div class="t">🎮 Tic-tac-toe</div>`;
  else body = "";
  const who = !mine && m.chat === GROUP ? `<span class="who" style="color:hsl(${hue(m.frm)} 60% 60%)">${esc(contactName(m.frm))}</span>` : "";
  return `<div class="gcm${mine ? " mine" : ""}">${who}${body}<span class="meta">${fmtTime(m.ts)}${mine ? ticks(m) : ""}</span></div>`;
};

GCCHAT.render = function () {
  if (!GCCHAT.open) return;
  GCCHAT.renderTabs();
  const box = $("#gccMsgs");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  const list = chatMsgs(GCCHAT.chat).slice(-60);
  box.innerHTML = list.length ? list.map(GCCHAT.msgHtml).join("") : `<p class="none">No messages with ${esc(chatName(GCCHAT.chat))} yet.</p>`;
  if (atBottom || GCCHAT.jump) box.scrollTop = box.scrollHeight;
  GCCHAT.jump = false;
  $("#gccText").placeholder = `Message ${chatName(GCCHAT.chat)}`;
};

// app.js calls this for every new or updated message
GCCHAT.onMsg = function (rec) {
  if (!GCCHAT.open || rec.chat !== GCCHAT.chat) return;
  GCCHAT.render();
  if (rec.frm !== S.me.addr) GCCHAT.read();
};

GCCHAT.send = async function () {
  const inp = $("#gccText"), text = inp.value.trim();
  if (!text || !GCCHAT.chat) return;
  inp.value = "";
  GCCHAT.jump = true;
  try { await api("/api/send", { to: GCCHAT.chat, text }); SFX.play("send"); }
  catch (e) { toast(e.message); inp.value = text; }
};

document.addEventListener("DOMContentLoaded", () => {
  $("#gccForm").onsubmit = (e) => { e.preventDefault(); GCCHAT.send(); };
  $("#gccClose").onclick = GCCHAT.close;
  $("#gccText").addEventListener("keydown", (e) => {
    e.stopPropagation();                                   // typing never reaches the game
    if (e.key === "Escape") { e.preventDefault(); GCCHAT.close(); }
  });
});
