"use strict";
/* UNO table.  The host's station holds the deck and every hand; this page only ever sees its
   own hand (the view) and sends intents.  Keys: ←/→ choose a card, Enter play, D draw,
   P keep the drawn card, U call UNO, C catch, 1-4 or R Y G B pick a colour, N next round (host). */

const UNO_COLORS = ["red", "yellow", "green", "blue"];
const UNO_COLOR_NAMES = ["Red", "Yellow", "Green", "Blue"];
const UNO_CARDS = (() => {            // same numbering as wavelink/games/uno.py
  const a = [];
  for (let c = 0; c < 4; c++) {
    a.push([c, 0]);
    for (const v of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) a.push([c, v], [c, v]);
  }
  for (let i = 0; i < 4; i++) a.push([-1, 13]);
  for (let i = 0; i < 4; i++) a.push([-1, 14]);
  return a;
})();
const UNO_SKIP = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.4" fill="none" stroke="currentColor" stroke-width="3.2"/><path d="M6.2 17.8 17.8 6.2" stroke="currentColor" stroke-width="3.2"/></svg>';
const UNO_REV = '<svg viewBox="0 0 24 24"><path d="M4.5 9.5h13l-3.6-3.6M19.5 14.5h-13l3.6 3.6" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

const UNO_RING = (() => {            // three clockwise arcs with arrowheads (mirrored for counter-clockwise)
  const R = 46, pt = (a) => [50 + R * Math.cos(a), 50 + R * Math.sin(a)];
  let d = "";
  for (let k = 0; k < 3; k++) {
    const a0 = ((k * 120 + 20) * Math.PI) / 180, a1 = a0 + (70 * Math.PI) / 180;
    const [x0, y0] = pt(a0), [x1, y1] = pt(a1);
    const tx = -Math.sin(a1), ty = Math.cos(a1), nx = Math.cos(a1), ny = Math.sin(a1);
    d += `M${x0.toFixed(1)} ${y0.toFixed(1)}A${R} ${R} 0 0 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
    d += `M${(x1 - 5 * tx + 3.5 * nx).toFixed(1)} ${(y1 - 5 * ty + 3.5 * ny).toFixed(1)}L${x1.toFixed(1)} ${y1.toFixed(1)}L${(x1 - 5 * tx - 3.5 * nx).toFixed(1)} ${(y1 - 5 * ty - 3.5 * ny).toFixed(1)}`;
  }
  return `<svg viewBox="0 0 100 100" aria-hidden="true"><path d="${d}"/></svg>`;
})();

const UNO = {
  rid: null, room: null, view: null, sel: 0, armed: false, picker: null, log: [], seenEv: new Set(),
  pendingV: null, pendingAt: 0, prevHand: new Set(), prevTop: null, el: null, tick: 0,
};

UNO.cardName = function (id) {
  const [c, v] = UNO_CARDS[id];
  const nm = v <= 9 ? String(v) : { 10: "Skip", 11: "Reverse", 12: "Draw Two", 13: "Wild", 14: "Wild Draw Four" }[v];
  return c < 0 ? nm : `${UNO_COLOR_NAMES[c]} ${nm}`;
};

UNO.cardHtml = function (id, cls = "") {
  const [c, v] = UNO_CARDS[id];
  const col = c < 0 ? "wild" : UNO_COLORS[c];
  let big, ix;
  if (v <= 9) big = ix = String(v);
  else if (v === 10) big = ix = UNO_SKIP;
  else if (v === 11) big = ix = UNO_REV;
  else if (v === 12) big = ix = "+2";
  else if (v === 13) { big = ""; ix = "W"; }
  else { big = "+4"; ix = "+4"; }
  return `<div class="card c-${col} ${cls}" data-id="${id}" role="img" aria-label="${UNO.cardName(id)}">` +
    `<span class="ix tl">${ix}</span>${c < 0 ? '<span class="quad"></span>' : ""}<span class="big">${big}</span>` +
    `<span class="ix br">${ix}</span>${c >= 0 ? '<span class="pip"></span>' : ""}</div>`;
};

UNO.fanHtml = function () {           // decoration for the poster
  const ids = [16, 31, 71, 98, 104];
  return ids.map((id, i) => UNO.cardHtml(id).replace('class="card', `style="transform: rotate(${(i - 2) * 11}deg)" class="card`)).join("");
};

// ------------------------------------------------------------------ module interface (games.js)
UNO.show = function (room, root) {
  UNO.room = room;
  if (UNO.rid !== room.r || !root.querySelector(".uno-room")) {
    UNO.rid = room.r;
    UNO.log = []; UNO.seenEv = new Set(); UNO.sel = 0; UNO.armed = false; UNO.picker = null;
    if (UNO.view && UNO.view.r !== room.r) UNO.view = null;  // another table's view
    UNO.prevHand = new Set(); UNO.prevTop = null;
    root.innerHTML = `<div class="uno-room">${GC.bar("UNO", `<span class="uno-scores"></span><button class="gc-ghost" id="unoLeave">Leave table</button>`)}
      <div class="uno-table"></div></div>`;
    GC.wire(root);
    root.querySelector("#unoLeave").onclick = () => GC.post({ a: "leave", r: room.r }).catch((e) => toast(e.message));
    UNO.el = root.querySelector(".uno-table");
    UNO.scoreEl = root.querySelector(".uno-scores");
    clearInterval(UNO.tick);
    UNO.tick = setInterval(UNO.timerTick, 200);
    UNO.quiet = true;                                        // no sounds for what happened before
    if (room.uno) UNO.setView(room.uno);
    UNO.quiet = false;
  }
  UNO.render();
};

UNO.unmount = function () { clearInterval(UNO.tick); UNO.el = null; UNO.rid = null; };

UNO.onEvent = function (ev, data) {
  if (ev === "uno") {
    if (data.r === UNO.rid) { UNO.setView(data); UNO.render(); }
    return true;
  }
  if (ev === "unoerr") {
    if (data.r === UNO.rid) { UNO.pendingV = null; toast(data.m, 3200); SFX.play("nope"); UNO.render(); }
    return true;
  }
  return false;
};

UNO.setView = function (v) {
  const old = UNO.view;
  if (old && old.v >= v.v && old.round === v.round) return;
  if (old && old.round !== v.round) { UNO.seenEv = new Set(); }
  UNO.view = v;
  UNO.pendingV = null;
  const fresh = [];
  for (const e of v.ev || []) {
    const key = `${v.round}:${e.i}`;
    if (UNO.seenEv.has(key)) continue;
    UNO.seenEv.add(key);
    fresh.push(e);
    const line = UNO.describe(e, v);
    if (line) UNO.log.push(line);
  }
  if (old && !UNO.quiet) UNO.sounds(old, v, fresh);
  UNO.log = UNO.log.slice(-6);
  if (UNO.sel >= v.hand.length) UNO.sel = Math.max(0, v.hand.length - 1);
  if (v.hand.length !== 2) UNO.armed = false;
  if (v.phase === "drawn" && v.turn === v.seat && v.drawn !== null) UNO.sel = Math.max(0, v.hand.indexOf(v.drawn));
};

// one sound for what just happened (the most important event), then a chime if it's now my turn
UNO.sounds = function (old, v, fresh) {
  const PRI = { win: 6, caught: 5, uno: 4, play: 3, skip: 2, reverse: 2, draw: 1, deal: 1 };
  let top = null;
  for (const e of fresh) if (PRI[e.e] && (!top || PRI[e.e] >= PRI[top.e])) top = e;
  if (top) {
    const name = top.e === "win" ? (top.s === v.seat ? "win" : "lose") : top.e === "play" || top.e === "skip" || top.e === "reverse" ? "card"
      : top.e === "draw" || top.e === "deal" ? "draw" : top.e;
    SFX.play(name);
  }
  const mine = (x) => x.turn === x.seat && x.phase !== "over";
  if (mine(v) && !(mine(old) && old.round === v.round)) setTimeout(() => SFX.play("turn"), top ? 280 : 0);
};

UNO.who = function (seat, v = UNO.view) {
  if (seat === v.seat) return "You";
  const a = (v.p || UNO.room?.players || [])[seat];
  return GC.name(UNO.room, a);
};

UNO.describe = function (e, v) {
  const w = (s) => UNO.who(s, v);
  switch (e.e) {
    case "deal": return `Round ${e.round}: ${w(e.dealer)} dealt. First card ${UNO.cardName(e.top)}.`;
    case "play": return `${w(e.s)} played ${UNO.cardName(e.c)}${UNO_CARDS[e.c][0] < 0 ? `, colour ${UNO_COLOR_NAMES[e.col].toLowerCase()}` : ""}.`;
    case "draw":
      if (e.why === "draw") return e.s === v.seat && e.cards?.length ? `You drew ${UNO.cardName(e.cards[0])}.` : `${w(e.s)} drew a card.`;
      if (e.why === "caught") return null;
      return `${w(e.s)} ${e.s === v.seat ? "draw" : "draws"} ${e.k}.`;
    case "skip": return `${w(e.s)} ${e.s === v.seat ? "are" : "is"} skipped.`;
    case "reverse": return "Direction reversed.";
    case "uno": return `${w(e.s)}: UNO!`;
    case "caught": return `${w(e.by)} caught ${e.s === v.seat ? "you" : w(e.s)} without UNO: 2 cards.`;
    case "pass": return `${w(e.s)} passed.`;
    case "timeout": return `${w(e.s)} ran out of time.`;
    case "color": return `${w(e.s)} chose ${UNO_COLOR_NAMES[e.col].toLowerCase()}.`;
    case "shuffle": return "Discards shuffled into a new draw pile.";
    case "left": return `${w(e.s)} left the table.`;
    case "win": return `${w(e.s)} won the round: ${e.pts} points.`;
  }
  return null;
};

// ------------------------------------------------------------------ rendering
UNO.layout = function (n, rel) {
  if (n === 2) return "top";
  if (n === 3) return rel === 1 ? "left" : "right";
  return ["", "left", "top", "right"][rel];
};

UNO.render = function () {
  const v = UNO.view, el = UNO.el;
  if (!el) return;
  if (!v) { el.innerHTML = `<div class="uno-center"><div class="uno-status">Dealing…</div></div>`; return; }
  const me = v.seat, myTurn = v.turn === me && v.phase !== "over";
  const busy = UNO.pendingV === v.v && performance.now() - UNO.pendingAt < 4000;
  const out = new Set(v.out || []);
  let seats = "";
  for (let s = 0; s < v.n; s++) {
    if (s === me) continue;
    const rel = (s - me + v.n) % v.n, pos = UNO.layout(v.n, rel), a = v.p[s];
    const k = v.counts[s];
    const backs = Array.from({ length: Math.min(k, 12) }, () => '<div class="card cback sm"></div>').join("");
    const flag = k === 1 ? (v.vuln === s ? '<span class="flag bad">1 card, no UNO!</span>' : '<span class="flag">UNO</span>') : "";
    seats += `<div class="seat ${pos} ${v.turn === s && v.phase !== "over" ? "turn" : ""} ${out.has(s) ? "out" : ""}">
      <div class="who">${avatarHtml(a, "av", UNO.who(s))}
      <span class="nm">${esc(UNO.who(s))}</span><span class="n">${out.has(s) ? "left" : k}</span></div>
      <div class="backs">${backs}</div>${flag}</div>`;
  }
  const curColor = `var(--c-${UNO_COLORS[v.color]})`;
  const topIsNew = UNO.prevTop !== v.top;
  const drawable = myTurn && v.phase === "play" && !busy;
  const center = `<div class="uno-center">
      <div class="pile draw ${drawable ? "can" : ""}" title="Draw a card (D)">
        <div class="card cback" style="transform:translate(3px,3px)"></div><div class="card cback"></div>
        <div class="count">${v.draw_n} left</div></div>
      <div class="pile disc"><div class="ring ${v.dir < 0 ? "ccw" : ""}" style="--cur:${curColor}"
          title="Play goes ${v.dir < 0 ? "counter-clockwise" : "clockwise"}"><div class="glow"></div>${UNO_RING}</div>
        ${UNO.cardHtml(v.top, topIsNew ? "land" : "")}
        <div class="color-name">Colour in play: ${UNO_COLOR_NAMES[v.color].toLowerCase()}</div></div>
    </div>`;
  UNO.prevTop = v.top;
  const legal = new Set(v.legal || []);
  const ov = Math.min(-8, Math.max(-62, -((v.hand.length * 96 - Math.min(window.innerWidth - 80, 1100)) / Math.max(1, v.hand.length - 1))));
  const hand = v.hand.map((id, i) => UNO.cardHtml(id, `${legal.has(id) ? "legal" : ""} ${i === UNO.sel ? "sel" : ""} ${UNO.prevHand.size && !UNO.prevHand.has(id) ? "fresh" : ""}`)).join("");
  UNO.prevHand = new Set(v.hand);
  const canCatch = v.vuln !== null && v.vuln !== undefined && v.vuln !== me && v.phase !== "over";
  let status;
  if (v.phase === "over") status = `<b>${esc(UNO.who(v.winner))}</b> ${v.winner === me ? "win" : "wins"} the round.`;
  else if (myTurn && v.phase === "color") status = "<b>Your turn.</b> The first card is a Wild: pick the colour.";
  else if (myTurn && v.phase === "drawn") status = `<b>You drew ${esc(UNO.cardName(v.drawn))}.</b> Play it (Enter) or keep it (P).`;
  else if (myTurn) status = legal.size ? `<b>Your turn.</b> Match ${UNO_COLOR_NAMES[v.color].toLowerCase()} or ${esc(UNO.cardName(v.top).split(" ").slice(UNO_CARDS[v.top][0] < 0 ? 0 : 1).join(" "))}, or draw.`
    : "<b>Your turn.</b> Nothing matches: draw a card (D).";
  else status = `${esc(UNO.who(v.turn))}${v.phase === "color" ? " is picking the colour" : "'s turn"}.`;
  if (busy) status += " Sending…";
  if (UNO.scoreEl) UNO.scoreEl.innerHTML = v.p.map((a, s) => `<span>${esc(UNO.who(s))} <b>${v.scores[s]}</b></span>`).join("");
  el.innerHTML = `${seats}${center}
    <div class="uno-log" aria-live="polite">${UNO.log.slice(-3).map((l) => `<div>${esc(l)}</div>`).join("")}</div>
    <div class="uno-hand ${myTurn ? "myturn" : ""}" style="--ov:${ov}px">${hand}</div>
    <div class="uno-bar">
      <span class="me-timer ${myTurn ? "me-turn" : ""}" title="Time left for your turn"></span>
      <span class="uno-status">${status}</span>
      <button class="uno-btn" data-k="Enter" ${myTurn && v.phase !== "color" && legal.size && !busy ? "" : "disabled"}>Play<kbd>Enter</kbd></button>
      <button class="uno-btn" data-k="d" ${drawable ? "" : "disabled"}>Draw<kbd>D</kbd></button>
      <button class="uno-btn" data-k="p" ${myTurn && v.phase === "drawn" && !busy ? "" : "disabled"}>Keep<kbd>P</kbd></button>
      <button class="uno-btn call" data-k="u" aria-pressed="${UNO.armed}" ${v.hand.length === 2 && myTurn ? "" : "disabled"} title="Call UNO with your second-to-last card">UNO!<kbd>U</kbd></button>
      <button class="uno-btn catch" data-k="c" ${canCatch ? "" : "disabled"} title="Catch a player who did not call UNO">Catch<kbd>C</kbd></button>
    </div>${UNO.modalHtml(v)}`;
  el.querySelectorAll(".uno-btn[data-k]").forEach((b) => (b.onclick = () => UNO.key(b.dataset.k)));
  el.querySelectorAll(".uno-hand .card").forEach((c, i) => {
    c.onclick = () => { if (UNO.sel === i) UNO.key("Enter"); else { UNO.sel = i; UNO.render(); } };
  });
  el.querySelector(".pile.draw").onclick = () => UNO.key("d");
  el.querySelectorAll(".swatch").forEach((b) => (b.onclick = () => UNO.pickColor(+b.dataset.c)));
  el.querySelector(".uno-modal [data-next]")?.addEventListener("click", () => UNO.key("n"));
  el.querySelector(".uno-modal [data-cancel]")?.addEventListener("click", () => { UNO.picker = null; UNO.render(); });
  UNO.timerTick();
};

UNO.modalHtml = function (v) {
  const sw = (title, sub) => `<div class="uno-modal"><div class="box"><h3>${title}</h3><p>${sub}</p><div class="swatches">${UNO_COLOR_NAMES.map((c, i) =>
    `<button class="swatch s${i}" data-c="${i}">${c}<kbd>${i + 1}</kbd></button>`).join("")}</div>
    ${UNO.picker !== null ? '<p style="margin:14px 0 0"><button class="uno-btn" data-cancel>Cancel<kbd>Esc</kbd></button></p>' : ""}</div></div>`;
  if (UNO.picker !== null) return sw("Pick a colour", `for your ${esc(UNO.cardName(UNO.picker))}`);
  if (v.phase === "color" && v.turn === v.seat) return sw("Pick a colour", "The first card is a Wild: you choose the colour in play.");
  if (v.phase === "over") {
    const host = UNO.room?.h === S.me.addr;
    const rows = v.p.map((a, s) => ({ s, sc: v.scores[s], left: v.counts[s] })).sort((x, y) => y.sc - x.sc)
      .map((r) => `<tr class="${r.s === v.winner ? "win" : ""}"><td>${esc(UNO.who(r.s))}</td><td>${r.s === v.winner ? `+${v.pts}` : `${r.left} cards left`}</td><td>${r.sc}</td></tr>`).join("");
    return `<div class="uno-modal"><div class="box"><h3>${esc(UNO.who(v.winner))} ${v.winner === v.seat ? "win" : "wins"}</h3>
      <p>Round ${v.round}: ${v.pts} points from the cards left in the other hands.</p>
      <table class="standings">${rows}</table>
      ${host ? '<button class="uno-btn call" data-next>Deal next round<kbd>N</kbd></button>' : `<p>${esc(GC.name(UNO.room, UNO.room?.h))} deals the next round.</p>`}</div></div>`;
  }
  return "";
};

UNO.timerTick = function () {
  const v = UNO.view, el = UNO.el;
  if (!v || !el) return;
  const left = v.dl_at ? Math.max(0, Math.min(1, (v.dl_at - Date.now() / 1000) / (GC.snap?.turn_s || 30))) : 0;
  el.querySelectorAll(".turn .av, .me-timer.me-turn").forEach((x) => x.style.setProperty("--left", left.toFixed(3)));
};

// ------------------------------------------------------------------ actions
UNO.send = async function (m, extra = {}) {
  const v = UNO.view;
  UNO.pendingV = v.v; UNO.pendingAt = performance.now();
  UNO.render();
  try { await GC.post({ a: "uno", r: UNO.rid, m, ...extra }); }
  catch (e) { UNO.pendingV = null; toast(e.message, 3000); UNO.render(); }
};

UNO.play = function () {
  const v = UNO.view, id = v.hand[UNO.sel];
  if (id === undefined) return;
  if (v.phase === "drawn" && id !== v.drawn) { SFX.play("nope"); toast("After drawing you can only play the card you drew (or keep it with P)"); return; }
  if (!(v.legal || []).includes(id)) {
    SFX.play("nope");
    const [c, val] = UNO_CARDS[id];
    toast(val === 14 ? `Wild Draw Four only when you have no ${UNO_COLOR_NAMES[v.color].toLowerCase()} card`
      : `${UNO.cardName(id)} doesn't match ${UNO_COLOR_NAMES[v.color].toLowerCase()} or ${UNO.cardName(v.top)}`);
    return;
  }
  if (UNO_CARDS[id][0] < 0) { UNO.picker = id; UNO.render(); return; }
  UNO.send("play", { c: id, uno: UNO.armed && v.hand.length === 2 });
  UNO.armed = false;
};

UNO.pickColor = function (col) {
  const v = UNO.view;
  if (UNO.picker !== null) {
    const id = UNO.picker;
    UNO.picker = null;
    UNO.send("play", { c: id, col, uno: UNO.armed && v.hand.length === 2 });
    UNO.armed = false;
  } else if (v.phase === "color" && v.turn === v.seat) UNO.send("color", { col });
};

UNO.key = function (k) {
  const v = UNO.view;
  if (!v) return false;
  const myTurn = v.turn === v.seat && v.phase !== "over";
  const busy = UNO.pendingV === v.v && performance.now() - UNO.pendingAt < 4000;
  const lk = k.length === 1 ? k.toLowerCase() : k;
  const colorKey = { 1: 0, 2: 1, 3: 2, 4: 3, r: 0, y: 1, g: 2, b: 3 }[lk];
  if (UNO.picker !== null || (myTurn && v.phase === "color")) {
    if (colorKey !== undefined && !busy) { UNO.pickColor(colorKey); return true; }
    if (k === "Escape" && UNO.picker !== null) { UNO.picker = null; UNO.render(); return true; }
    return k !== "Escape";
  }
  if (k === "ArrowLeft" || k === "ArrowRight") {
    if (!v.hand.length) return true;
    UNO.sel = (UNO.sel + (k === "ArrowLeft" ? -1 : 1) + v.hand.length) % v.hand.length;
    UNO.render();
    return true;
  }
  if (lk === "c") {
    if (v.vuln !== null && v.vuln !== undefined && v.vuln !== v.seat && v.phase !== "over") UNO.send("catch");
    return true;
  }
  if (lk === "n") {
    if (v.phase === "over" && UNO.room?.h === S.me.addr) GC.post({ a: "next", r: UNO.rid }).catch((e) => toast(e.message));
    return true;
  }
  if (!myTurn || busy) return ["Enter", "d", "p", "u"].includes(lk);
  if (k === "Enter") { UNO.play(); return true; }
  if (lk === "d" && v.phase === "play") { UNO.send("draw"); return true; }
  if (lk === "p" && v.phase === "drawn") { UNO.send("keep"); return true; }
  if (lk === "u") { if (v.hand.length === 2) { UNO.armed = !UNO.armed; UNO.render(); } return true; }
  return false;
};

UNO.onKey = function (e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  return UNO.key(e.key);
};

GC.mods.uno = UNO;
