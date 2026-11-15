"use strict";
/* Game center shell: the two posters, invitations and lobbies.  The games themselves live in
   uno.js (UNO) and race.js (Gold Code GP); they register in GC.mods.  Uses app.js globals
   ($, S, api, toast, esc, hue, contactName). */

const GC = {
  snap: null, open: false, focus: null, mods: {}, laps: 3,
  pick: { uno: new Set(), race: new Set() },
  clocks: {}, anim: 0, mounted: null,
};
const GC_STATUS = {
  invited: "invited", joined: "in", declined: "said no", left: "left", missed: "didn't answer", gone: "went off the air",
};
const GC_RADIO = '<svg viewBox="0 0 24 24"><path d="M4 18h2V9H4zm4 0h2V5H8zm4 0h2v-6h-2zm4 0h2V8h-2zm4 0h2v-3h-2z"/></svg>';
const GC_X = '<svg viewBox="0 0 24 24"><path d="M19 6.4 17.6 5 12 10.6 6.4 5 5 6.4 10.6 12 5 17.6 6.4 19l5.6-5.6 5.6 5.6 1.4-1.4-5.6-5.6z"/></svg>';

GC.post = (body) => api("/api/gc", body);

GC.load = async function () {
  try { GC.snap = await api("/api/gc"); } catch (e) { return; }
  GC.render();
};

GC.onEvent = function (ev, data) {
  if (ev === "gc") {
    const before = GC.snap;
    GC.snap = data;
    GC.autoOpen(before, data);
    GC.render();
    return true;
  }
  for (const m of Object.values(GC.mods)) if (m.onEvent && m.onEvent(ev, data)) return true;
  return false;
};

GC.room = (rid) => GC.snap?.rooms.find((r) => r.r === rid) || null;
GC.me = () => S.me?.addr;
GC.name = (room, a) => room?.names?.[a] || contactName(a);

// a game I joined has started: open it (the host pressed Start somewhere else)
GC.autoOpen = function (before, now) {
  for (const r of now.rooms) {
    const old = before?.rooms.find((x) => x.r === r.r);
    if (r.state === "playing" && r.me_seat !== null && (!old || old.state !== "playing")) {
      GC.focus = r.r;
      if (!GC.open) GC.show(true);
    }
  }
};

GC.current = function () {
  const rooms = GC.snap?.rooms || [];
  let r = GC.focus && rooms.find((x) => x.r === GC.focus);
  if (r) return r;
  r = rooms.find((x) => x.state === "playing" && x.me_seat !== null);
  return r || null;
};

GC.show = function (open) {
  GC.open = open;
  $("#gc").hidden = !open;
  document.documentElement.toggleAttribute("data-gc-open", open);   // the chat's lights rest behind a game
  if (typeof NAV !== "undefined") NAV.mark();
  if (!open && typeof GCCHAT !== "undefined") GCCHAT.hidePopups();
  // a running game stays mounted behind the chat (the race keeps your car where it is)
  if (!open) { cancelAnimationFrame(GC.anim); return; }
  GC.render();
};

GC.unmount = function () {
  if (GC.mounted && GC.mods[GC.mounted.g]?.unmount) GC.mods[GC.mounted.g].unmount();
  GC.mounted = null;
};

GC.render = function () {
  const rooms = GC.snap?.rooms || [];
  const invites = rooms.filter((r) => r.state === "invited").length;
  $("#gcBtn .dot").hidden = !invites;
  if (!GC.open || !GC.snap) return;
  const room = GC.current();
  const inGame = room && room.me_seat !== null && (room.state === "playing" || (room.state === "over" && room.g === "race"));
  if (inGame) {
    if (GC.mounted?.r !== room.r) { GC.unmount(); GC.mounted = { r: room.r, g: room.g }; cancelAnimationFrame(GC.anim); }
    GC.mods[room.g].show(room, $("#gcBody"));
    return;
  }
  if (GC.mounted) GC.unmount();
  if (room) GC.renderLobby(room); else GC.renderHome();
};

// ------------------------------------------------------------------ top bar
GC.bar = function (title, extra = "", over = false) {
  return `<header class="gc-bar${over ? " over" : ""}"><span class="gc-title">${title}</span>${extra}
    ${GC.radioButton()}${typeof GCCHAT !== "undefined" ? GCCHAT.button() : ""}<button class="gc-close" data-act="close" title="Back to the chat (Esc)" aria-label="Back to the chat">${GC_X}</button></header>`;
};
// the radio dashboard over the game (the game keeps running underneath); its light pulses with frames heard
GC.radioButton = () => `<button class="gc-ghost gc-radiobtn" data-act="radio" title="Radio dashboard: links, constellation, spectrum, queue"
  aria-label="Radio dashboard" aria-expanded="${!!S.dashOpen}">${GC_RADIO}<span>Radio</span><b class="livedot${$("#liveDot")?.classList.contains("on") ? " on" : ""}"><i></i></b></button>`;
GC.narrow = () => matchMedia("(max-width: 760px)").matches;
GC.markRadio = () => document.querySelectorAll(".gc-radiobtn").forEach((b) => b.setAttribute("aria-expanded", String(!!S.dashOpen)));

// Keys go to the game unless they are meant for something on top of it: a text field, the chat panel,
// the radio dashboard, or a photo / camera sheet.
GC.keysToGame = function (e) {
  const t = e.target;
  if (document.querySelector(".sheet")) return false;
  if (!t || !t.closest) return true;
  if (t.tagName === "TEXTAREA" || t.isContentEditable) return false;
  if (t.tagName === "INPUT" && !["checkbox", "radio", "button"].includes(t.type)) return false;
  return !t.closest("#gcChat, #dash");
};

GC.wire = function (root) {
  root.querySelectorAll("[data-act]").forEach((b) => {
    b.onclick = async () => {
      const a = b.dataset.act, rid = b.dataset.r;
      try {
        if (a === "close") GC.show(false);
        else if (a === "chat") GCCHAT.toggle();
        else if (a === "radio") {
          // on a phone the dashboard and the chat panel share the bottom of the screen: one at a time
          if (!S.dashOpen && GC.narrow() && GCCHAT.open) GCCHAT.close();
          showDash(!S.dashOpen); b.blur();
        }
        else if (a === "home") { GC.focus = null; GC.render(); }
        else if (a === "join") { await GC.post({ a: "answer", r: rid, ok: true }); GC.focus = rid; }
        else if (a === "decline") { await GC.post({ a: "answer", r: rid, ok: false }); await GC.post({ a: "dismiss", r: rid }); GC.focus = null; }
        else if (a === "open") { GC.focus = rid; GC.render(); }
        else if (a === "start") await GC.post({ a: "start", r: rid });
        else if (a === "leave") { await GC.post({ a: "leave", r: rid }); }
        else if (a === "dismiss") { await GC.post({ a: "dismiss", r: rid }); GC.focus = null; }
        else if (a === "invite") await GC.invite(b.dataset.g);
      } catch (e) { toast(e.message, 4000); }
    };
  });
};

// ------------------------------------------------------------------ home: posters
GC.online = () => [...S.contacts.values()].filter((c) => c.online && c.addr !== GC.me());

GC.pickHtml = function (g) {
  const on = GC.online();
  for (const a of [...GC.pick[g]]) if (!on.some((c) => c.addr === a)) GC.pick[g].delete(a);
  if (!on.length) return `<span class="none">No other station on the air yet. Stations appear when their beacons are heard.</span>`;
  return on.map((c) => `<label><input type="checkbox" data-g="${g}" value="${c.addr}" ${GC.pick[g].has(c.addr) ? "checked" : ""}>
    ${avatarHtml(c.addr, "gc-av")}<span>${esc(c.name)}</span></label>`).join("");
};

GC.renderHome = function () {
  const rooms = GC.snap.rooms;
  const dock = rooms.filter((r) => r.state !== "closed" || r.why).map((r) => {
    const host = GC.name(r, r.h), game = GC.snap.games[r.g];
    if (r.state === "invited") return `<div class="invite"><span class="who"><b>${esc(host)}</b> invites you to ${esc(game)}
        <small>${r.seats.length} players${r.g === "race" ? `, ${r.o.laps} laps` : ""}</small></span>
        <button class="yes" data-act="join" data-r="${r.r}">Join</button><button class="no" data-act="decline" data-r="${r.r}">Not now</button></div>`;
    if (r.state === "closed") return `<div class="invite"><span class="who">${esc(game)}: ${esc(r.why || "room closed")}</span>
        <button class="no" data-act="dismiss" data-r="${r.r}">OK</button></div>`;
    return `<div class="invite"><span class="who">${esc(game)} with ${esc(r.seats.filter((a) => a !== GC.me()).map((a) => GC.name(r, a)).join(", "))}
        <small>${r.state === "lobby" ? "waiting in the lobby" : "running"}</small></span>
        <button class="open" data-act="open" data-r="${r.r}">Open</button></div>`;
  }).join("");
  const n = (g) => GC.pick[g].size;
  $("#gcBody").innerHTML = `${GC.bar("Game center", "", true)}
    <div class="gc-dock">${dock}</div>
    <div class="gc-home">
      <section class="poster p-uno">
        <div class="fan" aria-hidden="true">${typeof UNO !== "undefined" ? UNO.fanHtml() : ""}</div>
        <h2>UNO</h2>
        <p class="tag">Standard rules for 2 to 4 players. Your hand stays on your screen: the host's station shuffles, deals and checks every move.</p>
        <div class="pick">${GC.pickHtml("uno")}</div>
        <div class="row"><button class="go" data-act="invite" data-g="uno" ${n("uno") ? "" : "disabled"}>Invite ${n("uno") ? n("uno") + " to UNO" : ""}</button>
          <span class="hint">Pick 1 to 3 stations</span></div>
      </section>
      <section class="poster p-race">
        <canvas class="bg" aria-hidden="true"></canvas>
        <h2>Gold Code <em>GP</em></h2>
        <p class="tag">A street race through a rain-soaked night city, with seeker missiles, plasma mines, EMP, shields and boost. Every car is on the air 15 times a second.</p>
        <div class="pick">${GC.pickHtml("race")}</div>
        <div class="row"><span class="laps" role="group" aria-label="Laps">${[2, 3, 5].map((l) =>
          `<button data-laps="${l}" aria-pressed="${GC.laps === l}">${l} laps</button>`).join("")}</span>
          <button class="go" data-act="invite" data-g="race" ${n("race") ? "" : "disabled"}>Invite ${n("race") ? n("race") + " to race" : ""}</button></div>
      </section>
    </div>`;
  const body = $("#gcBody");
  GC.wire(body);
  body.querySelectorAll(".pick input").forEach((i) => (i.onchange = () => {
    const set = GC.pick[i.dataset.g];
    if (i.checked) { if (set.size >= 3) { i.checked = false; toast("Up to 3 other stations: 4 players in all"); return; } set.add(+i.value); }
    else set.delete(+i.value);
    GC.renderHome();
  }));
  body.querySelectorAll(".laps button").forEach((b) => (b.onclick = () => { GC.laps = +b.dataset.laps; GC.renderHome(); }));
  GC.backdrop(body.querySelector(".poster.p-race canvas.bg"));
};

GC.invite = async function (g) {
  const players = [...GC.pick[g]];
  const j = await GC.post({ a: "create", game: g, players, o: g === "race" ? { laps: GC.laps } : {} });
  GC.pick[g].clear();
  GC.focus = j.r;
  GC.render();
};

// animated night street behind the race poster / lobby (drawn by race.js)
GC.backdrop = function (cv) {
  cancelAnimationFrame(GC.anim);
  if (!cv || typeof RACE === "undefined") return;
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const frame = (t) => {
    if (!cv.isConnected) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = cv.clientWidth, h = cv.clientHeight;
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const c = cv.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    RACE.backdrop(c, w, h, t / 1000);
    if (!still) GC.anim = requestAnimationFrame(frame);
  };
  GC.anim = requestAnimationFrame(frame);
};

// ------------------------------------------------------------------ lobby
GC.renderLobby = function (r) {
  const me = GC.me(), host = r.h === me, game = GC.snap.games[r.g];
  const synced = (a) => r.g !== "race" || a === r.h || r.sync?.[a];
  const rows = r.seats.map((a) => {
    const st = r.st[a] || "invited";
    let txt = a === r.h ? "host" : GC_STATUS[st] || st;
    if (r.g === "race" && st === "joined" && a !== r.h) txt = synced(a) ? "in, clock synced" : "in, syncing clock…";
    return `<li class="${st}"><span class="dot"></span><span class="nm">${esc(GC.name(r, a))}${a === me ? " (you)" : ""}</span><span class="st">${esc(txt)}</span></li>`;
  }).join("");
  const joined = r.seats.filter((a) => r.st[a] === "joined");
  const ready = joined.length >= 2 && joined.every(synced);
  let acts = "", note = "";
  if (r.state === "closed") {
    acts = `<button class="primary" data-act="dismiss" data-r="${r.r}">Back to the game center</button>`;
    note = r.why || "This room is closed.";
  } else if (r.state === "invited") {
    acts = `<button class="primary" data-act="join" data-r="${r.r}">Join</button><button class="gc-ghost" data-act="decline" data-r="${r.r}">Not now</button>`;
  } else if (host) {
    acts = `<button class="primary" data-act="start" data-r="${r.r}" ${ready ? "" : "disabled"}>Start with ${joined.length} players</button>
      <button class="gc-ghost" data-act="leave" data-r="${r.r}">Close room</button>`;
    note = joined.length < 2 ? "Start becomes available when someone joins. Stations that haven't answered stay out."
      : !ready ? "Waiting for the clock sync (a few pings over the air)." : "Stations that haven't answered stay out.";
  } else {
    acts = `<button class="gc-ghost" data-act="leave" data-r="${r.r}">Leave</button>`;
    note = `${GC.name(r, r.h)} starts the game.`;
  }
  const sub = r.state === "closed" ? "Room closed" : r.g === "race" ? `${r.o.laps} laps, ${GC.name(r, r.h)} is hosting` : `${GC.name(r, r.h)} is hosting and deals`;
  $("#gcBody").innerHTML = `<div class="lobby-bg ${r.g}">${r.g === "race" ? "<canvas></canvas>" : ""}</div>
    ${GC.bar(`<button class="gc-ghost" data-act="home">Game center</button>`)}
    <div class="lobby ${r.g}"><h3>${esc(game)}</h3><p class="sub">${esc(sub)}</p><ul>${rows}</ul>
      <div class="acts">${acts}</div>${note ? `<p class="note">${esc(note)}</p>` : ""}</div>`;
  GC.wire($("#gcBody"));
  if (r.g === "race") GC.backdrop($("#gcBody .lobby-bg canvas"));
};

// ------------------------------------------------------------------ game clock (host's clock), for the race
GC.syncClock = async function (rid) {
  const c = GC.clocks[rid] || (GC.clocks[rid] = { s: [], off: null });
  const t1 = performance.now();
  let j;
  try { j = await api(`/api/gc/clock?r=${encodeURIComponent(rid)}`); } catch (e) { return; }
  const t2 = performance.now();
  c.s.push({ rtt: t2 - t1, off: j.t - (t1 + t2) / 2000, at: t2 });
  c.s = c.s.filter((x) => t2 - x.at < 20000).slice(-12);
  c.off = c.s.reduce((a, b) => (b.rtt < a.rtt ? b : a)).off;
};
GC.now = (rid) => performance.now() / 1000 + (GC.clocks[rid]?.off ?? 0);

// ------------------------------------------------------------------ wiring
(function () {
  $("#gcBtn").onclick = () => GC.show(!GC.open);
  document.addEventListener("keydown", (e) => {
    if (!GC.open || !GC.keysToGame(e)) return;
    const room = GC.current();
    if (GC.mounted && room && GC.mods[room.g]?.onKey && GC.mods[room.g].onKey(e)) { e.preventDefault(); return; }
    if (e.key === "Escape") { GC.show(false); e.preventDefault(); }
  });
  GC.load();
})();

// app.js calls this when the station list changes: refresh the player pickers
GC.contactsChanged = function () {
  if (GC.open && GC.snap && !GC.mounted && !GC.current()) GC.renderHome();
};
