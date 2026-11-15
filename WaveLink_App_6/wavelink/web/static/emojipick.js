"use strict";
/* Emoji palette for the composer: search by name, categories, recently used (kept in the station's
   preferences).  The data (EMOJI_DATA) is built by tools/gen_emoji.py from Unicode names.
   The same palette serves the chat panel in the game center: EPICK.open("gc") moves it there and
   types into that panel's message box. */

const EPICK = { built: false, where: "main" };
EPICK.ICON = { Recent: "🕘", Smileys: "😀", People: "👋", Animals: "🐶", Food: "🍕", Activity: "⚽", Travel: "🚗",
  Objects: "💡", Symbols: "❤️", Radio: "📡" };
EPICK.COLS = 8;

EPICK.isOpen = () => !$("#emojiPop").hidden;
EPICK.toggle = (where = "main") => (EPICK.isOpen() && EPICK.where === where ? EPICK.close() : EPICK.open(where));
EPICK.input = () => (EPICK.where === "gc" ? $("#gccText") : $("#text"));
EPICK.button = () => (EPICK.where === "gc" ? $('#gccTray [data-tool="emoji"]') : $("#emojiBtn"));

EPICK.open = function (where = "main") {
  if (EPICK.isOpen()) EPICK.close(false);
  EPICK.where = where;
  EPICK.build();
  const pop = $("#emojiPop");
  // the palette sits above the message box it types into
  const home = where === "gc" ? $("#gcChat") : $(".composer");
  if (pop.parentElement !== home) home.appendChild(pop);
  pop.classList.toggle("ingc", where === "gc");
  pop.style.bottom = where === "gc" ? `${$("#gccForm").offsetHeight + 8}px` : "";
  pop.hidden = false;
  EPICK.button()?.setAttribute("aria-expanded", "true");
  EPICK.renderRecent();
  const q = pop.querySelector(".eq");
  if (q.value) { q.value = ""; EPICK.search(""); }
  EPICK.spy();
  // on a touch screen, don't pop the keyboard up over the palette
  if (!matchMedia("(pointer: coarse)").matches) q.focus({ preventScroll: true });
};
EPICK.close = function (refocus) {
  $("#emojiPop").hidden = true;
  $("#emojiBtn").setAttribute("aria-expanded", "false");
  $('#gccTray [data-tool="emoji"]')?.setAttribute("aria-expanded", "false");
  if (refocus) EPICK.input().focus();
};

EPICK.btn = (e, nm) => `<button type="button" data-e="${e}" title="${esc(nm)}" aria-label="${esc(nm)}">${e}</button>`;

EPICK.build = function () {
  if (EPICK.built) return;
  EPICK.built = true;
  const pop = $("#emojiPop");
  const cats = typeof EMOJI_DATA !== "undefined" ? EMOJI_DATA : [];
  EPICK.names = new Map();
  for (const [, items] of cats) for (const [e, nm] of items) if (!EPICK.names.has(e)) EPICK.names.set(e, nm);
  const tabs = [["Recent", "Recently used"], ...cats.map(([c]) => [c, c])];
  pop.innerHTML = `<input class="eq" type="search" placeholder="Search emoji (heart, fire, antenna…)" aria-label="Search emoji">
    <div class="etabs" role="tablist">${tabs.map(([c, t]) => `<button type="button" role="tab" data-c="${c}" title="${t}" aria-label="${t}">${EPICK.ICON[c] || "•"}</button>`).join("")}</div>
    <div class="egrid">
      <section data-c="Recent"><h5>Recently used</h5><div class="g" role="group" aria-label="Recently used"></div></section>
      ${cats.map(([c, items]) => `<section data-c="${c}"><h5>${c}</h5><div class="g" role="group" aria-label="${c}">${items.map(([e, nm]) => EPICK.btn(e, nm)).join("")}</div></section>`).join("")}
      <section data-c="Results" hidden><h5>Results</h5><div class="g"></div><p class="none" hidden>No emoji with that name</p></section>
    </div>`;
  const grid = pop.querySelector(".egrid"), q = pop.querySelector(".eq");
  grid.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-e]");
    if (b) EPICK.insert(b.dataset.e, !e.shiftKey);
  });
  pop.querySelectorAll(".etabs button").forEach((b) => (b.onclick = () => {
    if (q.value) { q.value = ""; EPICK.search(""); }
    const sec = grid.querySelector(`section[data-c="${b.dataset.c}"]`);
    if (sec && !sec.hidden) grid.scrollTo({ top: sec.offsetTop - grid.offsetTop, behavior: animOn() ? "smooth" : "auto" });
  }));
  grid.addEventListener("scroll", EPICK.spy, { passive: true });
  q.addEventListener("input", () => EPICK.search(q.value));
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); const b = EPICK.visibleButtons()[0]; if (b) EPICK.insert(b.dataset.e, true); }
    else if (e.key === "ArrowDown") { e.preventDefault(); EPICK.visibleButtons()[0]?.focus(); }
  });
  grid.addEventListener("keydown", EPICK.nav);
  pop.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); EPICK.close(true); } });
  EPICK.spy();
};

EPICK.visibleButtons = () => [...$("#emojiPop .egrid").querySelectorAll(":scope > section:not([hidden]) button[data-e]")];

// arrow keys move around the grid, Enter inserts
EPICK.nav = function (e) {
  const b = e.target.closest("button[data-e]");
  if (!b) return;
  const all = EPICK.visibleButtons(), i = all.indexOf(b);
  const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: EPICK.COLS, ArrowUp: -EPICK.COLS }[e.key];
  if (step) {
    e.preventDefault();
    const j = i + step;
    if (j < 0) $("#emojiPop .eq").focus();
    else all[Math.min(all.length - 1, j)]?.focus();
  }
};

// which category is on screen
EPICK.spy = function () {
  const grid = $("#emojiPop .egrid");
  let cur = "Recent";
  for (const s of grid.querySelectorAll(":scope > section:not([hidden])")) {
    if (s.offsetTop - grid.offsetTop <= grid.scrollTop + 12) cur = s.dataset.c;
  }
  $("#emojiPop").querySelectorAll(".etabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.c === cur)));
};

EPICK.renderRecent = function () {
  const sec = $('#emojiPop section[data-c="Recent"]');
  const rec = (pref("emojiRecent") || []).filter((e) => typeof e === "string").slice(0, 24);
  sec.hidden = !rec.length;
  sec.querySelector(".g").innerHTML = rec.map((e) => EPICK.btn(e, EPICK.names.get(e) || e)).join("");
};

EPICK.search = function (text) {
  const pop = $("#emojiPop"), terms = text.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const res = pop.querySelector('section[data-c="Results"]');
  pop.querySelectorAll('.egrid section:not([data-c="Results"])').forEach((s) => (s.hidden = !!terms.length || (s.dataset.c === "Recent" && !(pref("emojiRecent") || []).length)));
  res.hidden = !terms.length;
  if (terms.length) {
    const hits = [];
    for (const [e, nm] of EPICK.names) {
      const words = nm.split(" ");
      if (terms.every((t) => words.some((w) => w.startsWith(t)) || (t.length > 2 && nm.includes(t)))) hits.push([e, nm]);
      if (hits.length >= 160) break;
    }
    res.querySelector(".g").innerHTML = hits.map(([e, nm]) => EPICK.btn(e, nm)).join("");
    res.querySelector(".none").hidden = !!hits.length;
  }
  pop.querySelector(".egrid").scrollTop = 0;
  EPICK.spy();
};

// put the emoji where the cursor is in the message box
EPICK.insert = function (e, keepOpen = true) {
  const ta = EPICK.input();
  const s = ta.selectionStart ?? ta.value.length, en = ta.selectionEnd ?? s;
  ta.setRangeText(e, s, en, "end");
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  SFX.play("pop", { gap: 0.05 });
  const rec = [e, ...(pref("emojiRecent") || []).filter((x) => x !== e)].slice(0, 24);
  setPref("emojiRecent", rec);
  if (!keepOpen) EPICK.close(true);
};

document.addEventListener("DOMContentLoaded", () => {
  $("#emojiBtn").onclick = (e) => { e.stopPropagation(); EPICK.toggle("main"); };
  // a click anywhere else closes the palette
  document.addEventListener("pointerdown", (e) => {
    if (!EPICK.isOpen()) return;
    if (e.target.closest('#emojiPop, #emojiBtn, #gccTray [data-tool="emoji"]')) return;
    EPICK.close(false);
  });
  $("#gccText").addEventListener("keydown", (e) => { if (e.key === "Escape" && EPICK.isOpen() && EPICK.where === "gc") { e.stopImmediatePropagation(); e.preventDefault(); EPICK.close(true); } });
  $("#text").addEventListener("keydown", (e) => { if (e.key === "Escape" && EPICK.isOpen()) EPICK.close(true); });
});
