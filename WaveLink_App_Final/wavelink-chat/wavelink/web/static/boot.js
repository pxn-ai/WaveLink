/* This station's look, cached from its profile so the first paint has the right colours.  (Loaded from
   <head>: the page's Content-Security-Policy allows no inline scripts.) */
(function () {
  var r = document.documentElement, t = {};
  try { t = JSON.parse(localStorage.getItem("wavelink-theme") || "{}") || {}; } catch (e) {}
  var m = t.mode || "auto";
  if (m === "auto") m = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  r.setAttribute("data-mode", m);
  r.setAttribute("data-pal", t.pal || "wavelink");
  r.setAttribute("data-anim", t.anim === false ? "off" : "on");
  if (t.pal === "custom" && /^#[0-9a-f]{6}$/i.test(t.accent || "") && t.vars) for (var k in t.vars) r.style.setProperty(k, t.vars[k]);
})();
