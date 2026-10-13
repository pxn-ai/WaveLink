"use strict";
/* Gold Code GP: a top-down street race through a rain-soaked night city.

   Every station drives its own car here (physics at 120 steps per second) and posts the car's
   state to its station 20 times a second; the station broadcasts the newest state in its own
   time slot (games/hub.py).  The other cars arrive as 'race' events and are drawn where their
   last state says they are now (dead reckoning, blended over 0.1 s).  Hits are decided by the
   victim's station: it simulates incoming missiles against its own exact position, checks
   mines when it drives over them and EMPs against where it was when they went off.

   The track and the city are generated from the room's seed, so every station builds the
   same circuit without sending it.  Keys: arrows or WASD, Space uses the pickup. */

const R_SEAT = ["#3fe6ff", "#ff3f9e", "#b8ff3c", "#a47cff"];
const R_TW = 112;                 // half the track width (px)
const R_RUN = 44;                 // run-off between the track edge and the neon rail
const R_STEP = 8;                 // centreline sample spacing (px)
const R_DT = 1 / 120;
const R_ITEMS = ["", "missile", "mine", "emp", "shield", "boost"];
const R_ITEM_NAMES = { missile: "Seeker missile", mine: "Plasma mine", emp: "EMP", shield: "Shield", boost: "Boost" };
const R_ITEM_HELP = { missile: "locks on to the car ahead", mine: "dropped behind you", emp: "stuns cars within 280 px",
  shield: "blocks the next hit", boost: "short burst of speed" };
const R_FLAG = { boost: 1, shield: 2, stun: 4, fin: 8, brake: 16, rev: 32, off: 64, spin: 128 };
const R_CAR = { len: 46, wid: 24, rad: 20, vmax: 820, acc: 620, brake: 1250, rev: 230, drag: 0.22 };
const R_EMP_R = 280, R_MISSILE_V = 1300, R_MISSILE_LIFE = 6.0, R_MINE_LIFE = 25, R_MINE_ARM = 0.6;
const R_ICON = {
  missile: '<svg viewBox="0 0 32 32"><path d="M5 16h15l7-4v8l-7-4" fill="#ffab40"/><path d="M5 16 2 12M5 16l-3 4" stroke="#ffd08a" stroke-width="2"/></svg>',
  mine: '<svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="7" fill="#ff3f9e"/><circle cx="16" cy="16" r="12" fill="none" stroke="#ff3f9e" stroke-width="2" stroke-dasharray="3 4"/></svg>',
  emp: '<svg viewBox="0 0 32 32"><path d="M18 3 8 18h7l-2 11 11-16h-7z" fill="#3fe6ff"/></svg>',
  shield: '<svg viewBox="0 0 32 32"><path d="M16 3 27 7v8c0 7-5 12-11 14C10 27 5 22 5 15V7z" fill="none" stroke="#b8ff3c" stroke-width="2.5"/></svg>',
  boost: '<svg viewBox="0 0 32 32"><path d="M6 8l9 8-9 8M16 8l9 8-9 8" fill="none" stroke="#ffab40" stroke-width="3" stroke-linejoin="round"/></svg>',
};
const R_SIGNS = ["GOLD CODE", "SPREAD", "2.5 GHz", "CHIP RATE", "NOODLES", "HOTEL ROLL-OFF", "QPSK BAR", "LDPC", "PREAMBLE",
  "RAMEN 24H", "KARAOKE", "PARITY", "SYNC", "DESPREAD", "OPEN LATE", "PACHINKO", "CAPSULE", "TEA"];

function rMulberry(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rClamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const rWrap = (a) => { while (a > Math.PI) a -= 2 * Math.PI; while (a < -Math.PI) a += 2 * Math.PI; return a; };

const RACE = {
  rid: null, room: null, root: null, cv: null, ctx: null, T: null, me: null, cars: [], keys: new Set(),
  raf: 0, last: 0, acc: 0, postT: 0, posting: false, outEv: [], eid: 0, missiles: [], mines: [], fx: [],
  rain: [], splash: [], tiles: new Map(), hudT: 0, msgs: [], results: null, syncT: 0, laps: 3, seatN: 2,
  cam: { x: 0, y: 0, z: 1 }, hist: [], seen: new Set(), mini: null, lastPos: 0,
};

// ====================================================================== track + city
RACE.buildTrack = function (seed) {
  const rng = rMulberry((seed >>> 0) ^ 0x9e3779b9);
  const CX = 2400, CY = 1700, RX = 1650, RY = 1150;
  let pts = null;
  for (let attempt = 0; attempt < 300 && !pts; attempt++) {
    const n = 9 + Math.floor(rng() * 4), ctrl = [];
    for (let i = 0; i < n; i++) {
      const a = (i / n) * 2 * Math.PI + (rng() - 0.5) * (2 * Math.PI / n) * 0.55;
      const r = 0.5 + 0.5 * rng();
      ctrl.push([CX + Math.cos(a) * RX * r, CY + Math.sin(a) * RY * r]);
    }
    const p = RACE.spline(ctrl);
    if (RACE.trackOk(p)) pts = p;
  }
  if (!pts) {                                   // practically never: an oval
    pts = [];
    for (let i = 0; i < 1200; i++) { const a = i / 1200 * 2 * Math.PI; pts.push([CX + Math.cos(a) * RX * .8, CY + Math.sin(a) * RY * .8]); }
    pts = RACE.resample(pts);
  }
  // start in the middle of the straightest stretch
  const M = pts.length, ang = RACE.angles(pts);
  let best = 0, bestV = 1e9;
  for (let i = 0; i < M; i++) {
    let v = 0;
    for (let k = -30; k < 30; k++) v += Math.abs(rWrap(ang[(i + k + 1 + M) % M] - ang[(i + k + M) % M]));
    if (v < bestV) { bestV = v; best = i; }
  }
  pts = pts.slice(best).concat(pts.slice(0, best));
  return RACE.finishTrack(pts, rng);
};

RACE.spline = function (ctrl) {            // closed Catmull-Rom, then even 8 px spacing
  const n = ctrl.length, out = [];
  for (let i = 0; i < n; i++) {
    const p0 = ctrl[(i - 1 + n) % n], p1 = ctrl[i], p2 = ctrl[(i + 1) % n], p3 = ctrl[(i + 2) % n];
    const len = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]), k = Math.max(8, Math.ceil(len / 4));
    for (let j = 0; j < k; j++) {
      const t = j / k, t2 = t * t, t3 = t2 * t;
      out.push([0, 1].map((d) => 0.5 * (2 * p1[d] + (-p0[d] + p2[d]) * t + (2 * p0[d] - 5 * p1[d] + 4 * p2[d] - p3[d]) * t2
        + (-p0[d] + 3 * p1[d] - 3 * p2[d] + p3[d]) * t3)));
    }
  }
  return RACE.resample(out);
};

RACE.resample = function (p) {
  const out = [p[0]];
  let carry = 0;
  for (let i = 0; i < p.length; i++) {
    const a = p[i], b = p[(i + 1) % p.length];
    const seg = Math.hypot(b[0] - a[0], b[1] - a[1]);
    let d = R_STEP - carry;
    while (d <= seg) { const t = d / seg; out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]); d += R_STEP; }
    carry = seg - (d - R_STEP);
  }
  if (Math.hypot(out[out.length - 1][0] - out[0][0], out[out.length - 1][1] - out[0][1]) < R_STEP * 0.5) out.pop();
  return out;
};

RACE.angles = function (p) {
  const M = p.length;
  return p.map((a, i) => { const b = p[(i + 1) % M]; return Math.atan2(b[1] - a[1], b[0] - a[0]); });
};

RACE.trackOk = function (p) {
  const M = p.length, L = M * R_STEP;
  if (L < 7000 || L > 12500) return false;
  const ang = RACE.angles(p), W = 10;                  // radius over 2*W samples
  for (let i = 0; i < M; i++) {
    const dth = Math.abs(rWrap(ang[(i + W) % M] - ang[(i - W + M) % M]));
    if (dth > 1e-6 && (2 * W * R_STEP) / dth < 175) return false;          // corner too tight
  }
  const need = 2 * (R_TW + R_RUN) + 70, gap = Math.ceil((need * 1.8) / R_STEP);
  for (let i = 0; i < M; i += 3) {
    for (let j = i + gap; j < M; j += 3) {
      if (M - (j - i) < gap) break;
      if (Math.hypot(p[i][0] - p[j][0], p[i][1] - p[j][1]) < need) return false;   // two parts too close
    }
  }
  return true;
};

RACE.finishTrack = function (pts, rng) {
  const M = pts.length, L = M * R_STEP;
  const x = new Float32Array(M), y = new Float32Array(M), tx = new Float32Array(M), ty = new Float32Array(M), curv = new Float32Array(M);
  for (let i = 0; i < M; i++) { x[i] = pts[i][0]; y[i] = pts[i][1]; }
  for (let i = 0; i < M; i++) {
    const a = (i + 1) % M, b = (i - 1 + M) % M;
    const dx = x[a] - x[b], dy = y[a] - y[b], d = Math.hypot(dx, dy) || 1;
    tx[i] = dx / d; ty[i] = dy / d;
  }
  const ang = RACE.angles(pts);
  for (let i = 0; i < M; i++) curv[i] = rWrap(ang[(i + 6) % M] - ang[(i - 6 + M) % M]) / (12 * R_STEP);
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (let i = 0; i < M; i++) { x0 = Math.min(x0, x[i]); y0 = Math.min(y0, y[i]); x1 = Math.max(x1, x[i]); y1 = Math.max(y1, y[i]); }
  const T = { M, L, x, y, tx, ty, curv, box: [x0 - 900, y0 - 900, x1 + 900, y1 + 900], grid: new Map(), cell: 160 };
  for (let i = 0; i < M; i++) {
    const k = `${Math.floor(x[i] / T.cell)},${Math.floor(y[i] / T.cell)}`;
    (T.grid.get(k) || T.grid.set(k, []).get(k)).push(i);
  }
  // straights for boost pads, three rows of holo caches, lamps, puddles, signs, buildings
  const straight = (i) => { let v = 0; for (let k = -12; k <= 12; k++) v += Math.abs(curv[(i + k + M) % M]); return v; };
  T.pads = [];
  for (const f of [0.22, 0.52, 0.8]) {
    let bi = Math.floor(f * M), bv = 1e9;
    for (let k = -50; k <= 50; k += 5) { const i = (Math.floor(f * M) + k + M) % M, v = straight(i); if (v < bv) { bv = v; bi = i; } }
    T.pads.push({ i: bi, off: (rng() - 0.5) * R_TW * 0.9 });
  }
  T.caches = [];
  for (const f of [0.12, 0.4, 0.68]) {
    const i = Math.floor(f * M);
    for (const o of [-0.55, 0, 0.55]) T.caches.push({ i, off: o * R_TW, gone: 0 });
  }
  T.lamps = [];
  for (let i = 0, side = 1; i < M; i += Math.round(240 / R_STEP), side = -side) {
    const n = [-ty[i], tx[i]], d = R_TW + R_RUN + 26;
    T.lamps.push([x[i] + n[0] * d * side, y[i] + n[1] * d * side]);
  }
  T.puddles = [];
  for (let k = 0; k < 70; k++) {
    const i = Math.floor(rng() * M), n = [-ty[i], tx[i]], o = (rng() - 0.5) * 2 * (R_TW + 30);
    T.puddles.push([x[i] + n[0] * o, y[i] + n[1] * o, 18 + rng() * 46, 8 + rng() * 22, rng() * Math.PI]);
  }
  T.buildings = [];
  const [bx0, by0, bx1, by1] = T.box, C = 290;
  for (let gx = bx0 - 200; gx < bx1 + 200; gx += C) {
    for (let gy = by0 - 200; gy < by1 + 200; gy += C) {
      const w = 130 + rng() * 120, h = 130 + rng() * 120;
      const cx = gx + C / 2 + (rng() - 0.5) * 40, cy = gy + C / 2 + (rng() - 0.5) * 40;
      const dist = RACE.distToTrack(T, cx, cy);
      if (dist < R_TW + R_RUN + 40 + Math.hypot(w, h) / 2) continue;
      const lit = rng();
      T.buildings.push({ x0: cx - w / 2, y0: cy - h / 2, x1: cx + w / 2, y1: cy + h / 2, ht: 0.18 + rng() * 0.32,
        roof: `hsl(${230 + rng() * 40} ${10 + rng() * 14}% ${9 + rng() * 7}%)`, win: Math.floor(rng() * 3),
        sign: dist < R_TW + R_RUN + 320 && lit < 0.55 ? { text: R_SIGNS[Math.floor(rng() * R_SIGNS.length)],
          col: R_SEAT[Math.floor(rng() * 4)], side: Math.floor(rng() * 4) } : null,
        vents: Array.from({ length: 1 + Math.floor(rng() * 3) }, () => [rng(), rng(), 12 + rng() * 18]) });
    }
  }
  return T;
};

RACE.distToTrack = function (T, px, py) {
  let best = 1e9;
  for (let i = 0; i < T.M; i += 4) { const d = Math.hypot(T.x[i] - px, T.y[i] - py); if (d < best) best = d; }
  return best;
};

// nearest centreline sample: local search from a hint, grid search otherwise
RACE.nearest = function (T, px, py, hint) {
  let bi = -1, bd = 1e18;
  if (hint !== undefined && hint >= 0) {
    for (let k = -40; k <= 40; k++) {
      const i = (hint + k + T.M) % T.M, dx = T.x[i] - px, dy = T.y[i] - py, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; bi = i; }
    }
    if (bd < 300 * 300) return bi;
  }
  const cx = Math.floor(px / T.cell), cy = Math.floor(py / T.cell);
  for (let r = 0; r < 12 && bi < 0 || r < 2; r++) {
    for (let gx = cx - r; gx <= cx + r; gx++) for (let gy = cy - r; gy <= cy + r; gy++) {
      if (Math.max(Math.abs(gx - cx), Math.abs(gy - cy)) !== r) continue;
      for (const i of T.grid.get(`${gx},${gy}`) || []) {
        const dx = T.x[i] - px, dy = T.y[i] - py, d = dx * dx + dy * dy;
        if (d < bd) { bd = d; bi = i; }
      }
    }
  }
  if (bi < 0) for (let i = 0; i < T.M; i++) { const d = (T.x[i] - px) ** 2 + (T.y[i] - py) ** 2; if (d < bd) { bd = d; bi = i; } }
  return bi;
};

// signed lateral offset of a point from the centreline at sample i
RACE.lateral = (T, i, px, py) => (px - T.x[i]) * -T.ty[i] + (py - T.y[i]) * T.tx[i];
RACE.at = (T, i, off) => [T.x[i] - T.ty[i] * off, T.y[i] + T.tx[i] * off];

// ====================================================================== static world: 512 px tiles, drawn once
RACE.TILE = 512;
RACE.tile = function (tx, ty) {
  const key = tx + "," + ty;
  let c = RACE.tiles.get(key);
  if (c) { RACE.tiles.delete(key); RACE.tiles.set(key, c); return c; }
  c = document.createElement("canvas");
  const S0 = RACE.TILE, tz = RACE.tz;                 // tiles are drawn at screen scale: blits are 1:1
  c.width = c.height = Math.round(S0 * tz);
  const g = c.getContext("2d"), T = RACE.T;
  g.setTransform(tz, 0, 0, tz, -tx * S0 * tz, -ty * S0 * tz);
  const rng = rMulberry(tx * 7919 + ty * 104729 + 17);
  // ground: dark wet asphalt with grit
  g.fillStyle = "#0b0d13"; g.fillRect(tx * S0, ty * S0, S0, S0);
  g.fillStyle = "rgba(160,170,200,0.035)";
  for (let k = 0; k < 900; k++) g.fillRect(tx * S0 + rng() * S0, ty * S0 + rng() * S0, 1.5, 1.5);
  // city block pavements around the buildings
  for (const b of T.buildings) {
    if (b.x1 + 40 < tx * S0 || b.x0 - 40 > (tx + 1) * S0 || b.y1 + 40 < ty * S0 || b.y0 - 40 > (ty + 1) * S0) continue;
    g.fillStyle = "#151821"; g.fillRect(b.x0 - 26, b.y0 - 26, b.x1 - b.x0 + 52, b.y1 - b.y0 + 52);
    g.fillStyle = "#06070b"; g.fillRect(b.x0 + 10, b.y0 + 10, b.x1 - b.x0, b.y1 - b.y0);        // shadow
  }
  const path = () => { g.beginPath(); g.moveTo(T.x[0], T.y[0]); for (let i = 1; i < T.M; i++) g.lineTo(T.x[i], T.y[i]); g.closePath(); };
  const edge = (side, off) => { g.beginPath(); for (let i = 0; i <= T.M; i++) { const j = i % T.M, [px, py] = RACE.at(T, j, side * off); i ? g.lineTo(px, py) : g.moveTo(px, py); } };
  g.lineJoin = "round"; g.lineCap = "round";
  path(); g.strokeStyle = "#191c25"; g.lineWidth = 2 * (R_TW + R_RUN); g.stroke();          // run-off
  path(); g.strokeStyle = "#1d2130"; g.lineWidth = 2 * R_TW; g.stroke();                      // racing surface
  path(); g.strokeStyle = "rgba(120,140,190,0.06)"; g.lineWidth = 2 * R_TW - 40; g.stroke();  // wet sheen
  // kerbs: amber and dark stripes on both edges
  for (const side of [-1, 1]) {
    edge(side, R_TW + 5); g.lineWidth = 10; g.strokeStyle = "#2a2219"; g.setLineDash([]); g.stroke();
    edge(side, R_TW + 5); g.strokeStyle = "#c7832b"; g.setLineDash([22, 22]); g.stroke();
    edge(side, R_TW - 3); g.lineWidth = 2.5; g.strokeStyle = "rgba(235,235,245,0.55)"; g.setLineDash([]); g.stroke();
  }
  g.setLineDash([26, 34]); path(); g.lineWidth = 3; g.strokeStyle = "rgba(220,225,240,0.18)"; g.stroke(); g.setLineDash([]);
  // puddles reflect the sky glow
  for (const [px, py, rx, ry, a] of T.puddles) {
    g.save(); g.translate(px, py); g.rotate(a);
    const gr = g.createRadialGradient(0, 0, 2, 0, 0, rx);
    gr.addColorStop(0, "rgba(90,110,160,0.22)"); gr.addColorStop(1, "rgba(90,110,160,0)");
    g.fillStyle = gr; g.scale(1, ry / rx); g.beginPath(); g.arc(0, 0, rx, 0, 7); g.fill(); g.restore();
  }
  // start / finish: chequered band
  const sx = T.x[0], sy = T.y[0], ang = Math.atan2(T.ty[0], T.tx[0]);
  g.save(); g.translate(sx, sy); g.rotate(ang);
  for (let r = -R_TW; r < R_TW; r += 14) for (let c = 0; c < 3; c++) {
    g.fillStyle = ((r / 14 + c) & 1) ? "#e9edf5" : "#141720"; g.fillRect(-21 + c * 14, r, 14, 14);
  }
  g.restore();
  // grid boxes
  for (let k = 0; k < 4; k++) {
    const [gx, gy, ga] = RACE.gridSlot(k);
    g.save(); g.translate(gx, gy); g.rotate(ga);
    g.strokeStyle = "rgba(230,235,245,0.35)"; g.lineWidth = 2; g.strokeRect(-26, -16, 52, 32); g.restore();
  }
  // neon rails at the outer edges (the walls)
  for (const [side, col] of [[-1, "#ff3f9e"], [1, "#3fe6ff"]]) {
    edge(side, R_TW + R_RUN);
    g.lineWidth = 9; g.strokeStyle = col; g.globalAlpha = 0.16; g.stroke();
    g.lineWidth = 4; g.globalAlpha = 0.45; g.stroke();
    g.lineWidth = 1.6; g.globalAlpha = 1; g.strokeStyle = "#f4f7ff"; g.stroke();
  }
  // sodium light pools
  g.globalCompositeOperation = "lighter";
  for (const [lx, ly] of T.lamps) {
    if (lx + 260 < tx * S0 || lx - 260 > (tx + 1) * S0 || ly + 260 < ty * S0 || ly - 260 > (ty + 1) * S0) continue;
    const gr = g.createRadialGradient(lx, ly, 4, lx, ly, 250);
    gr.addColorStop(0, "rgba(255,170,70,0.30)"); gr.addColorStop(0.35, "rgba(255,150,50,0.12)"); gr.addColorStop(1, "rgba(255,140,40,0)");
    g.fillStyle = gr; g.fillRect(lx - 250, ly - 250, 500, 500);
  }
  g.globalCompositeOperation = "source-over";
  RACE.tiles.set(key, c);
  while (RACE.tiles.size > 60) RACE.tiles.delete(RACE.tiles.keys().next().value);
  return c;
};

RACE.gridSlot = function (k) {
  const T = RACE.T, back = 70 + Math.floor(k / 2) * 74, i = (T.M - Math.round(back / R_STEP)) % T.M;
  const off = (k % 2 ? 1 : -1) * 48;
  const [px, py] = RACE.at(T, i, off);
  return [px, py, Math.atan2(T.ty[i], T.tx[i]), i, -back];
};

// sprites: cars per seat colour, lamp heads, light cones
RACE.sprites = function () {
  const mk = (w, h, fn) => { const c = document.createElement("canvas"); c.width = w; c.height = h; fn(c.getContext("2d")); return c; };
  RACE.carSpr = R_SEAT.map((col) => mk(64, 40, (g) => {
    g.translate(32, 20);
    const L = R_CAR.len, W = R_CAR.wid;
    g.fillStyle = "#0c0e14"; g.beginPath(); g.roundRect(-L / 2, -W / 2, L, W, 7); g.fill();
    const gr = g.createLinearGradient(0, -W / 2, 0, W / 2);
    gr.addColorStop(0, col); gr.addColorStop(0.5, "#11131a"); gr.addColorStop(1, col);
    g.fillStyle = gr; g.beginPath(); g.roundRect(-L / 2 + 2, -W / 2 + 2, L - 4, W - 4, 6); g.fill();
    g.fillStyle = "rgba(20,30,45,0.95)"; g.beginPath(); g.roundRect(-4, -W / 2 + 4, 16, W - 8, 4); g.fill();   // canopy
    g.fillStyle = "rgba(160,220,255,0.35)"; g.fillRect(8, -W / 2 + 6, 3, W - 12);
    g.fillStyle = col; g.fillRect(-L / 2 + 3, -2, 14, 4);                                                      // stripe
    g.fillStyle = "#f6fbff"; g.fillRect(L / 2 - 4, -W / 2 + 3, 3, 5); g.fillRect(L / 2 - 4, W / 2 - 8, 3, 5);  // headlights
  }));
  RACE.glowSpr = R_SEAT.map((col) => mk(120, 90, (g) => {
    const gr = g.createRadialGradient(60, 45, 4, 60, 45, 44);
    gr.addColorStop(0, col); gr.addColorStop(1, "rgba(0,0,0,0)");
    g.globalAlpha = 0.55; g.fillStyle = gr; g.scale(1, 0.7); g.beginPath(); g.arc(60, 64, 44, 0, 7); g.fill();
  }));
  RACE.beamSpr = mk(260, 120, (g) => {
    const gr = g.createLinearGradient(0, 0, 260, 0);
    gr.addColorStop(0, "rgba(255,245,220,0.35)"); gr.addColorStop(1, "rgba(255,245,220,0)");
    g.fillStyle = gr; g.beginPath(); g.moveTo(0, 52); g.lineTo(260, 0); g.lineTo(260, 120); g.lineTo(0, 68); g.fill();
  });
  RACE.lampSpr = mk(40, 40, (g) => {
    const gr = g.createRadialGradient(20, 20, 1, 20, 20, 20);
    gr.addColorStop(0, "rgba(255,230,180,1)"); gr.addColorStop(0.3, "rgba(255,170,64,0.6)"); gr.addColorStop(1, "rgba(255,150,40,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 40, 40);
  });
  RACE.winPat = [0, 1, 2].map((k) => mk(48, 48, (g) => {
    g.fillStyle = "#11131b"; g.fillRect(0, 0, 48, 48);
    const r = rMulberry(k + 3);
    for (let yy = 4; yy < 48; yy += 8) for (let xx = 3; xx < 48; xx += 7) {
      const v = r();
      g.fillStyle = v < 0.25 ? (k === 1 ? "rgba(140,220,255,0.75)" : "rgba(255,196,120,0.8)") : v < 0.33 ? "rgba(255,90,180,0.55)" : "rgba(40,46,62,0.9)";
      g.fillRect(xx, yy, 4, 4);
    }
  }));
};

// ====================================================================== module interface (games.js)
RACE.show = function (room, root) {
  RACE.room = room;
  if (RACE.rid === room.r && root.querySelector(".race-room") && RACE.round === room.n) {
    RACE.updateRoom(room);
    return;
  }
  RACE.mount(room, root);
};

RACE.mount = function (room, root) {
  RACE.unmount();
  RACE.rid = room.r; RACE.round = room.n; RACE.room = room;
  RACE.seatN = room.players.length; RACE.laps = room.o?.laps || 3;
  RACE.T = RACE.buildTrack(room.seed);
  RACE.tiles = new Map(); RACE.mini = null;
  if (!RACE.carSpr) RACE.sprites();
  RACE.missiles = []; RACE.mines = []; RACE.fx = []; RACE.msgs = []; RACE.outEv = []; RACE.eid = 0; RACE.seen = new Set();
  RACE.hist = []; RACE.results = room.state === "over" ? room.results : null; RACE.keys.clear();
  RACE.cars = room.players.map((a, s) => {
    const [x, y, h, i, d] = RACE.gridSlot(s);
    return { s, a, name: GC.name(room, a), col: R_SEAT[s % 4], x, y, h, vx: 0, vy: 0, w: 0, d, i, fl: 0, it: 0,
      pkt: null, err: null, rx: [], fin: room.finish?.[s] ?? null, lapT: [], prevLap: 0 };
  });
  RACE.me = RACE.cars[room.me_seat];
  Object.assign(RACE.me, { item: "", boost: 0, shield: 0, stun: 0, spin: 0, spinDir: 1, throttle: 0, steer: 0,
    lapStart: 0, best: null, lapNow: 0, finT: null, off: false });
  root.innerHTML = `<div class="race-room"><canvas class="world"></canvas><div class="vignette"></div>
    <div class="hud">
      <div class="pos"><span class="big">–</span><span class="of"></span></div>
      <div class="lap"></div><div class="times"></div>
      <div class="board"></div>
      <canvas class="mini" width="400" height="300"></canvas>
      <div class="item"><span class="slot"></span><span class="nm"></span></div>
      <div class="speed"><span class="v">0</span><small>km/h</small></div>
      <div class="count"></div><div class="r-msg"></div><div class="keys"></div>
    </div>
    ${GC.bar(`Gold Code GP <span style="opacity:.6;font-weight:500">${RACE.laps} laps</span>`, `<button class="gc-ghost" id="raceLeave">Leave race</button>`)}
    <div class="race-results" hidden></div></div>`;
  RACE.root = root.querySelector(".race-room");
  GC.wire(RACE.root);
  RACE.root.querySelector("#raceLeave").onclick = () => GC.post({ a: "leave", r: room.r }).catch((e) => toast(e.message));
  RACE.cv = RACE.root.querySelector("canvas.world");
  RACE.ctx = RACE.cv.getContext("2d");
  RACE.hud = Object.fromEntries(["pos", "lap", "times", "board", "item", "speed", "count", "r-msg", "keys"].map((k) => [k.replace("r-", ""), RACE.root.querySelector(".hud ." + k)]));
  RACE.miniCv = RACE.root.querySelector("canvas.mini");
  RACE.initRain();
  RACE.cam = { x: RACE.me.x, y: RACE.me.y, z: 1 };
  RACE.last = performance.now(); RACE.acc = 0;
  GC.clocks[room.r] = GC.clocks[room.r] || { s: [], off: null };
  for (let k = 0; k < 5; k++) setTimeout(() => GC.syncClock(room.r), k * 120);
  RACE.syncT = setInterval(() => GC.syncClock(room.r), 2000);
  RACE.raf = requestAnimationFrame(RACE.frame);
  if (RACE.results) RACE.showResults(RACE.results);
};

RACE.unmount = function () {
  cancelAnimationFrame(RACE.raf); clearInterval(RACE.syncT);
  RACE.rid = null; RACE.root = null; RACE.tiles = new Map();
};

RACE.updateRoom = function (room) {
  for (const c of RACE.cars) {
    const st = room.st?.[c.a];
    c.gone = st && st !== "joined";
    if (room.finish?.[c.s] !== undefined && c.fin === null) c.fin = room.finish[c.s];
  }
  if (room.state === "over" && room.results && !RACE.results) { RACE.results = room.results; RACE.showResults(room.results); }
};

RACE.onEvent = function (ev, data) {
  if (!["race", "racefin", "raceover"].includes(ev)) return false;
  if (data.r !== RACE.rid) return true;
  if (ev === "race") RACE.onRemote(data);
  else if (ev === "racefin") { const c = RACE.cars[data.s]; if (c && c.fin === null) c.fin = data.ft; }
  else if (ev === "raceover") { RACE.results = data.res; RACE.showResults(data.res); }
  return true;
};

RACE.onKey = function (e) {
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const map = { ArrowUp: "up", w: "up", ArrowDown: "down", s: "down", ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right", " ": "use" };
  const act = map[k];
  if (!act) return false;
  if (e.type === "keydown" && act === "use" && !e.repeat) RACE.useItem();
  return true;
};

// key up/down tracking (games.js only forwards keydown)
document.addEventListener("keydown", (e) => {
  if (!RACE.root || !GC.open) return;                      // typing in the chat doesn't drive the car
  const tag = e.target?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const m = { ArrowUp: "up", w: "up", ArrowDown: "down", s: "down", ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right" }[k];
  if (m) RACE.keys.add(m);
});
document.addEventListener("keyup", (e) => {
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const m = { ArrowUp: "up", w: "up", ArrowDown: "down", s: "down", ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right" }[k];
  if (m) RACE.keys.delete(m);
});
window.addEventListener("blur", () => RACE.keys.clear());
document.addEventListener("visibilitychange", () => RACE.keys.clear());

// ====================================================================== time
RACE.now = () => GC.now(RACE.rid);                         // host clock (s)
RACE.tg = () => RACE.now() - RACE.room.t0;                 // seconds since the green light

// ====================================================================== my car
RACE.step = function (dt) {
  const T = RACE.T, c = RACE.me, tg = RACE.tg();
  const started = tg >= 0, done = c.finT !== null;
  let up = RACE.keys.has("up"), down = RACE.keys.has("down");
  let steer = (RACE.keys.has("right") ? 1 : 0) - (RACE.keys.has("left") ? 1 : 0);
  if (!started || done) { up = down = false; steer = done ? steer * 0.4 : 0; }
  if (c.spin > 0) { up = down = false; steer = 0; }
  if (c.stun > 0) { up = false; steer *= 0.25; }
  const ch = Math.cos(c.h), sh = Math.sin(c.h);
  let vf = c.vx * ch + c.vy * sh, vr = -c.vx * sh + c.vy * ch;
  c.i = RACE.nearest(T, c.x, c.y, c.i);
  const lat = RACE.lateral(T, c.i, c.x, c.y), alat = Math.abs(lat);
  c.off = alat > R_TW + 4;
  const boost = c.boost > 0;
  const vmax = R_CAR.vmax * (boost ? 1.42 : 1) * (c.off ? 0.5 : 1) * (c.stun > 0 ? 0.4 : 1);
  if (up) vf += R_CAR.acc * (boost ? 2 : 1) * dt * (vf < vmax ? 1 : 0);
  if (down) vf -= (vf > 0 ? R_CAR.brake : R_CAR.acc * 0.6) * dt;
  if (boost && !up && vf < vmax) vf += R_CAR.acc * 0.8 * dt;
  vf = Math.max(vf, -R_CAR.rev);
  if (vf > vmax) vf -= Math.min(vf - vmax, (c.off ? 900 : 400) * dt);
  vf -= vf * (R_CAR.drag + (c.off ? 1.3 : 0) + (up || boost ? 0 : 0.35)) * dt;
  const grip = c.off ? 4 : 9.5;
  vr *= Math.exp(-grip * dt);
  const sp = Math.abs(vf);
  const yawMax = 2.7 * rClamp(sp / 240, 0, 1) * (1 - 0.38 * rClamp(sp / R_CAR.vmax, 0, 1));
  c.steer += (steer - c.steer) * Math.min(1, dt * 12);
  let w = c.steer * yawMax * (vf < 0 ? -1 : 1);
  if (c.spin > 0) { w = c.spinDir * 13; c.spin -= dt; vf *= Math.exp(-2.2 * dt); vr *= Math.exp(-2.2 * dt); }
  if (c.stun > 0) c.stun -= dt;
  c.vx = ch * vf - sh * vr; c.vy = sh * vf + ch * vr;
  c.h = rWrap(c.h + w * dt); c.w = w;
  c.x += c.vx * dt; c.y += c.vy * dt;
  // neon rail: push back inside and lose speed
  const lim = R_TW + R_RUN - R_CAR.wid / 2;
  const lat2 = RACE.lateral(T, c.i, c.x, c.y);
  if (Math.abs(lat2) > lim) {
    const nx = -T.ty[c.i] * Math.sign(lat2), ny = T.tx[c.i] * Math.sign(lat2);
    const pen = Math.abs(lat2) - lim;
    c.x -= nx * pen; c.y -= ny * pen;
    const vn = c.vx * nx + c.vy * ny;
    if (vn > 0) { c.vx -= 1.35 * vn * nx; c.vy -= 1.35 * vn * ny; c.vx *= 0.82; c.vy *= 0.82;
      if (vn > 160) RACE.spark(c.x + nx * R_CAR.wid / 2, c.y + ny * R_CAR.wid / 2, vn); }
  }
  // the other cars: each station only moves its own car out of the way
  for (const o of RACE.cars) {
    if (o === c || o.gone || !o.pkt) continue;
    const dx = c.x - o.dx, dy = c.y - o.dy, d = Math.hypot(dx, dy);
    if (d > 0.01 && d < 2 * R_CAR.rad) {
      const nx = dx / d, ny = dy / d, pen = 2 * R_CAR.rad - d;
      c.x += nx * pen * 0.6; c.y += ny * pen * 0.6;
      const rv = (c.vx - o.dvx) * nx + (c.vy - o.dvy) * ny;
      if (rv < 0) { c.vx -= 0.85 * rv * nx; c.vy -= 0.85 * rv * ny; }
    }
  }
  // distance raced, laps
  const ni = RACE.nearest(T, c.x, c.y, c.i);
  let ds = (ni - c.i) * R_STEP;
  if (ds > T.L / 2) ds -= T.L; if (ds < -T.L / 2) ds += T.L;
  c.i = ni; c.d += ds;
  const lap = Math.floor(c.d / T.L);
  if (started && !done && lap > c.prevLap && lap >= 1) {
    const lt = tg - c.lapStart;
    c.best = c.best === null ? lt : Math.min(c.best, lt);
    c.lapStart = tg;
    if (lap >= RACE.laps) {
      c.finT = tg; c.fin = Math.round(tg * 1000);
      RACE.emit("finish", { a: RACE.laps, b: 0 });
      RACE.say(`Finished in ${RACE.fmt(tg)}`, 4);
    } else RACE.say(lap === RACE.laps - 1 ? "Final lap" : `Lap ${lap + 1}`, 1.6);
  }
  c.prevLap = Math.max(c.prevLap, lap);
  if (c.boost > 0) c.boost -= dt;
  if (c.shield > 0) c.shield -= dt;
  // boost pads and holo caches
  if (started && !done) {
    for (const p of T.pads) {
      const [px, py] = RACE.at(T, p.i, p.off);
      if (Math.hypot(c.x - px, c.y - py) < 46) c.boost = Math.max(c.boost, 0.9);
    }
    for (const b of T.caches) {
      if (b.gone > performance.now()) continue;
      const [bx, by] = RACE.at(T, b.i, b.off);
      if (Math.hypot(c.x - bx, c.y - by) < 34 && !c.item) {
        b.gone = performance.now() + 3000;
        c.item = RACE.rollItem();
        RACE.say(R_ITEM_NAMES[c.item], 1.2);
      }
    }
  }
  RACE.hist.push([tg, c.x, c.y]);
  while (RACE.hist.length && RACE.hist[0][0] < tg - 2.2) RACE.hist.shift();
  c.fl = (boost ? R_FLAG.boost : 0) | (c.shield > 0 ? R_FLAG.shield : 0) | (c.stun > 0 ? R_FLAG.stun : 0) |
    (done ? R_FLAG.fin : 0) | (down && vf > 20 ? R_FLAG.brake : 0) | (vf < -5 ? R_FLAG.rev : 0) |
    (c.off ? R_FLAG.off : 0) | (c.spin > 0 ? R_FLAG.spin : 0);
  c.it = R_ITEMS.indexOf(c.item || "");
  RACE.checkHits(tg);
};

RACE.rollItem = function () {
  const order = RACE.order(), rank = order.indexOf(RACE.me), n = order.length;
  const f = n > 1 ? rank / (n - 1) : 0;                    // 0 = leading, 1 = last
  const wts = { missile: 0.1 + 0.42 * f, mine: 0.36 - 0.32 * f, emp: 0.12 + 0.1 * f, shield: 0.3 - 0.22 * f, boost: 0.12 + 0.14 * f };
  let r = Math.random() * Object.values(wts).reduce((a, b) => a + b, 0);
  for (const [k, v] of Object.entries(wts)) { r -= v; if (r <= 0) return k; }
  return "boost";
};

RACE.useItem = function () {
  const c = RACE.me, it = c.item;
  if (!it || RACE.tg() < 0 || c.finT !== null) return;
  c.item = "";
  const ch = Math.cos(c.h), sh = Math.sin(c.h);
  if (it === "missile") {
    const order = RACE.order(), k = order.indexOf(c), tgt = k > 0 ? order[k - 1] : null;
    const x = c.x + ch * 30, y = c.y + sh * 30;
    const e = RACE.emit("missile", { x, y, a: Math.round(((c.h + 2 * Math.PI) % (2 * Math.PI)) / (2 * Math.PI) * 65536) & 0xffff, b: tgt ? tgt.s : 255 });
    RACE.addMissile(c.s, e);
    RACE.say(tgt ? `Missile locked on ${tgt.name}` : "Missile away", 1.4);
  } else if (it === "mine") {
    const e = RACE.emit("mine", { x: c.x - ch * 40, y: c.y - sh * 40 });
    RACE.addMine(c.s, e);
  } else if (it === "emp") {
    const e = RACE.emit("emp", { x: c.x, y: c.y });
    RACE.fx.push({ k: "emp", x: e.x, y: e.y, t: 0, col: c.col });
  } else if (it === "shield") { c.shield = 12; RACE.say("Shield up", 1.2); }
  else if (it === "boost") c.boost = 1.4;
};

RACE.emit = function (k, f) {
  RACE.eid = (RACE.eid + 1) & 0xff;
  const e = Object.assign({ k, id: RACE.eid, t: Math.round(RACE.tg() * 1000), x: RACE.me.x, y: RACE.me.y, a: 0, b: 0 }, f);
  RACE.outEv.push(e);
  return e;
};

// ====================================================================== projectiles (simulated on every station)
RACE.addMissile = function (seat, e) {
  const T = RACE.T, i = RACE.nearest(T, e.x, e.y);
  RACE.missiles.push({ seat, id: e.id, t0: e.t / 1000, x: e.x, y: e.y, h: (e.a / 65536) * 2 * Math.PI, i, tgt: e.b === 255 ? null : e.b,
    homing: false, simT: e.t / 1000, dead: false });
};
RACE.addMine = function (seat, e) { RACE.mines.push({ seat, id: e.id, t0: e.t / 1000, x: e.x, y: e.y, dead: false }); };

RACE.moveMissiles = function (tg) {
  const T = RACE.T;
  for (const m of RACE.missiles) {
    if (m.dead) continue;
    if (tg - m.t0 > R_MISSILE_LIFE) { m.dead = true; RACE.fx.push({ k: "puff", x: m.x, y: m.y, t: 0 }); continue; }
    while (m.simT < tg) {
      const dt = Math.min(1 / 120, tg - m.simT);
      m.simT += dt;
      const tgt = m.tgt !== null ? RACE.cars[m.tgt] : null;
      const tp = tgt ? (tgt === RACE.me ? [tgt.x, tgt.y] : [tgt.dx, tgt.dy]) : null;
      if (tp && (m.homing || Math.hypot(tp[0] - m.x, tp[1] - m.y) < 520)) {
        m.homing = true;
        const want = Math.atan2(tp[1] - m.y, tp[0] - m.x);
        m.h += rClamp(rWrap(want - m.h), -5 * dt, 5 * dt);
      } else {
        m.i = RACE.nearest(T, m.x, m.y, m.i);
        const ahead = (m.i + 10) % T.M, [ax, ay] = RACE.at(T, ahead, RACE.lateral(T, m.i, m.x, m.y) * 0.85);
        const want = Math.atan2(ay - m.y, ax - m.x);
        m.h += rClamp(rWrap(want - m.h), -7 * dt, 7 * dt);
      }
      m.x += Math.cos(m.h) * R_MISSILE_V * dt; m.y += Math.sin(m.h) * R_MISSILE_V * dt;
    }
  }
  RACE.missiles = RACE.missiles.filter((m) => !m.dead || false);
  RACE.mines = RACE.mines.filter((m) => !m.dead && tg - m.t0 < R_MINE_LIFE);
};

// victim's decision: does anything hit my car?
RACE.checkHits = function (tg) {
  const c = RACE.me;
  if (c.finT !== null || tg < 0) return;
  for (const m of RACE.missiles) {
    if (m.dead || m.seat === c.s) continue;
    if (Math.hypot(m.x - c.x, m.y - c.y) < R_CAR.rad + 8) { m.dead = true; RACE.hitMe(1, m.seat, m.id); }
  }
  for (const m of RACE.mines) {
    if (m.dead || tg - m.t0 < R_MINE_ARM) continue;
    if (Math.hypot(m.x - c.x, m.y - c.y) < R_CAR.rad + 10) { m.dead = true; RACE.hitMe(2, m.seat, m.id); }
  }
};

RACE.hitMe = function (cause, src, srcId) {
  const c = RACE.me, by = RACE.cars[src]?.name || "someone";
  const what = ["", "missile", "mine", "EMP"][cause];
  if (c.shield > 0) {
    c.shield = 0;
    RACE.emit("block", { a: (cause << 8) | src, b: srcId });
    RACE.fx.push({ k: "block", x: c.x, y: c.y, t: 0, col: c.col });
    RACE.say(`Shield stopped ${by}'s ${what}`, 1.6);
    return;
  }
  RACE.emit("hit", { a: (cause << 8) | src, b: srcId });
  if (cause === 3) { c.stun = 1.4; RACE.fx.push({ k: "zap", x: c.x, y: c.y, t: 0 }); }
  else { c.spin = 1.0; c.spinDir = Math.random() < 0.5 ? -1 : 1; c.vx *= 0.35; c.vy *= 0.35; RACE.fx.push({ k: "boom", x: c.x, y: c.y, t: 0 }); }
  RACE.say(`Hit by ${by}'s ${what}`, 1.6);
};

// ====================================================================== remote cars
RACE.onRemote = function (p) {
  const c = RACE.cars[p.s];
  if (!c || c === RACE.me) return;
  const now = RACE.tg();
  const before = c.pkt ? RACE.predict(c.pkt, now) : null;
  c.pkt = p;
  c.d = p.d; c.fl = p.fl; c.it = p.it;
  c.rx.push(performance.now());
  while (c.rx.length && performance.now() - c.rx[0] > 3000) c.rx.shift();
  c.age = p.age;
  if (before) {
    const after = RACE.predict(p, now);
    c.err = { x: before[0] - after[0], y: before[1] - after[1], h: rWrap(before[2] - after[2]), at: performance.now() };
  }
  for (const e of p.ev || []) RACE.remoteEvent(c, e);
};

RACE.predict = function (p, tg) {           // constant speed and yaw rate from the packet's time
  const dt = rClamp(tg - p.t / 1000, 0, 0.5), w = p.w;
  let x, y;
  if (Math.abs(w) < 1e-3) { x = p.x + p.vx * dt; y = p.y + p.vy * dt; }
  else {
    const s = Math.sin(w * dt), co = Math.cos(w * dt);
    x = p.x + (p.vx * s + p.vy * (co - 1)) / w;
    y = p.y + (p.vx * (1 - co) + p.vy * s) / w;
  }
  const r = Math.cos(w * dt), q = Math.sin(w * dt);
  return [x, y, p.h + w * dt, p.vx * r - p.vy * q, p.vx * q + p.vy * r];
};

RACE.placeRemotes = function (tg) {
  for (const c of RACE.cars) {
    if (c === RACE.me || !c.pkt) continue;
    const [x, y, h, vx, vy] = RACE.predict(c.pkt, tg);
    let k = 0;
    if (c.err) k = Math.max(0, 1 - (performance.now() - c.err.at) / 100);
    c.dx = x + (c.err ? c.err.x * k : 0); c.dy = y + (c.err ? c.err.y * k : 0);
    c.dh = h + (c.err ? c.err.h * k : 0); c.dvx = vx; c.dvy = vy;
  }
};

RACE.remoteEvent = function (c, e) {
  const key = `${c.s}:${e.id}:${e.k}`;
  if (RACE.seen.has(key)) return;
  RACE.seen.add(key);
  if (e.k === "missile") RACE.addMissile(c.s, e);
  else if (e.k === "mine") RACE.addMine(c.s, e);
  else if (e.k === "emp") {
    RACE.fx.push({ k: "emp", x: e.x, y: e.y, t: 0, col: c.col });
    // was my car within reach when it went off?  (my last 2 s of positions)
    const me = RACE.me, t = e.t / 1000;
    let best = null;
    for (const h of RACE.hist) if (!best || Math.abs(h[0] - t) < Math.abs(best[0] - t)) best = h;
    const pos = best && Math.abs(best[0] - t) < 0.3 ? [best[1], best[2]] : [me.x, me.y];
    if (me.finT === null && Math.hypot(pos[0] - e.x, pos[1] - e.y) < R_EMP_R) RACE.hitMe(3, c.s, e.id);
  } else if (e.k === "hit" || e.k === "block") {
    const src = e.a & 0xff, srcId = e.b;
    for (const m of RACE.missiles) if (m.seat === src && m.id === srcId) m.dead = true;
    for (const m of RACE.mines) if (m.seat === src && m.id === srcId) m.dead = true;
    RACE.fx.push({ k: e.k === "hit" ? ((e.a >> 8) === 3 ? "zap" : "boom") : "block", x: e.x, y: e.y, t: 0, col: c.col });
    if (src === RACE.me.s) RACE.say(e.k === "hit" ? `You hit ${c.name}` : `${c.name}'s shield stopped it`, 1.6);
  } else if (e.k === "finish") {
    if (c.fin === null) c.fin = e.t;
  }
};

// ====================================================================== network: post my state 20 times a second
RACE.post = function () {
  const c = RACE.me;
  if (RACE.posting || !RACE.rid) return;
  RACE.posting = true;
  const ev = RACE.outEv.splice(0);
  const s = { t: Math.round(RACE.tg() * 1000), x: c.x, y: c.y, h: c.h, vx: c.vx, vy: c.vy, w: c.w, d: c.d, fl: c.fl,
    it: (c.it & 7) | ((Math.round(c.steer) + 1) << 3) };
  fetch("/api/gc/race", { method: "POST", body: JSON.stringify({ r: RACE.rid, s, ev }) })
    .catch(() => { RACE.outEv.unshift(...ev); })
    .finally(() => { RACE.posting = false; });
};

// ====================================================================== race order
RACE.order = function () {
  return [...RACE.cars].filter((c) => !c.gone || c.fin !== null).sort((a, b) =>
    (a.fin !== null) !== (b.fin !== null) ? (a.fin !== null ? -1 : 1) : a.fin !== null ? a.fin - b.fin : b.d - a.d);
};

// ====================================================================== frame
RACE.frame = function (now) {
  if (!RACE.root || !RACE.root.isConnected) return;
  RACE.raf = requestAnimationFrame(RACE.frame);
  const dtw = rClamp((now - RACE.last) / 1000, 0, 0.1);   // rAF time can be a little before 'last'
  RACE.last = now;
  RACE.fpsN = (RACE.fpsN || 0) + 1;                       // frames drawn per second (shown in tests)
  if (!RACE.fpsT || now - RACE.fpsT > 2000) { if (RACE.fpsT) RACE.fps = Math.round(RACE.fpsN * 1000 / (now - RACE.fpsT)); RACE.fpsT = now; RACE.fpsN = 0; }
  if (GC.clocks[RACE.rid]?.off === null || GC.clocks[RACE.rid]?.off === undefined) return;
  RACE.acc += dtw;
  while (RACE.acc >= R_DT) { RACE.step(R_DT); RACE.acc -= R_DT; }
  const tg = RACE.tg();
  RACE.placeRemotes(tg);
  RACE.moveMissiles(tg);
  if (now - RACE.postT >= 50) { RACE.postT = now; RACE.post(); }
  if (!GC.open) { RACE.keys.clear(); return; }             // behind the chat: physics and posting only
  const t0d = performance.now();
  RACE.draw(dtw, tg);
  RACE.drawMs = 0.95 * (RACE.drawMs || 0) + 0.05 * (performance.now() - t0d);
  if (now - RACE.hudT > 100) { RACE.hudT = now; RACE.drawHud(tg); }
};

RACE.fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
RACE.say = function (text, secs = 1.5) { RACE.msgs.push({ text, until: performance.now() + secs * 1000 }); };

// ====================================================================== drawing
RACE.initRain = function () {
  const r = rMulberry(99);
  RACE.rain = Array.from({ length: 260 }, () => ({ x: r(), y: r(), z: 0.4 + r() * 0.6 }));
  RACE.splash = [];
};

RACE.draw = function (dt, tg) {
  const cv = RACE.cv, g = RACE.ctx, T = RACE.T, me = RACE.me;
  const dpr = Math.min(1.5, window.devicePixelRatio || 1), W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  // camera: ahead of the car; a fixed zoom per window size so the static world can be pre-scaled
  const S0 = RACE.TILE;
  const z = rClamp(Math.min(W / 1500, H / 950) * 1.12, 0.55, 1.35);
  const tz = Math.round(S0 * dpr * z) / S0;
  if (tz !== RACE.tz) { RACE.tz = tz; RACE.tiles = new Map(); }
  const cam = RACE.cam, tx = me.x + me.vx * 0.32, ty = me.y + me.vy * 0.32, k = 1 - Math.exp(-dt * 5);
  cam.x += (tx - cam.x) * k; cam.y += (ty - cam.y) * k; cam.z = tz / dpr;
  const ox = Math.round(dpr * W / 2 - cam.x * tz), oy = Math.round(dpr * H / 2 - cam.y * tz);
  const vx0 = (0 - ox) / tz, vy0 = (0 - oy) / tz, vx1 = (dpr * W - ox) / tz, vy1 = (dpr * H - oy) / tz;
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.fillStyle = "#0b0d13"; g.fillRect(0, 0, cv.width, cv.height);
  const TS = Math.round(S0 * tz);
  for (let tx2 = Math.floor(vx0 / S0); tx2 <= Math.floor(vx1 / S0); tx2++)
    for (let ty2 = Math.floor(vy0 / S0); ty2 <= Math.floor(vy1 / S0); ty2++) g.drawImage(RACE.tile(tx2, ty2), ox + tx2 * TS, oy + ty2 * TS);
  g.setTransform(tz, 0, 0, tz, ox, oy);
  const vis = (x, y, m = 80) => x > vx0 - m && x < vx1 + m && y > vy0 - m && y < vy1 + m;
  const t = performance.now() / 1000;
  // boost pads
  for (const p of T.pads) {
    const [px, py] = RACE.at(T, p.i, p.off);
    if (!vis(px, py)) continue;
    g.save(); g.translate(px, py); g.rotate(Math.atan2(T.ty[p.i], T.tx[p.i]));
    g.fillStyle = "rgba(63,230,255,0.12)"; g.fillRect(-40, -34, 80, 68);
    for (let j = 0; j < 3; j++) {
      const a = 0.35 + 0.65 * ((Math.sin(t * 6 - j * 1.3) + 1) / 2);
      g.strokeStyle = `rgba(63,230,255,${a})`; g.lineWidth = 5;
      g.beginPath(); g.moveTo(-28 + j * 20, -22); g.lineTo(-12 + j * 20, 0); g.lineTo(-28 + j * 20, 22); g.stroke();
    }
    g.restore();
  }
  // holo caches
  for (const b of T.caches) {
    const [bx, by] = RACE.at(T, b.i, b.off);
    if (!vis(bx, by) || b.gone > performance.now()) continue;
    g.save(); g.translate(bx, by); g.rotate(t * 1.6 + b.off);
    g.globalCompositeOperation = "lighter";
    g.fillStyle = "rgba(255,171,64,0.18)"; g.fillRect(-22, -22, 44, 44);
    g.strokeStyle = "rgba(255,208,138,0.9)"; g.lineWidth = 2; g.strokeRect(-14, -14, 28, 28);
    g.rotate(Math.PI / 4); g.strokeStyle = "rgba(164,124,255,0.8)"; g.strokeRect(-10, -10, 20, 20);
    g.restore();
  }
  // mines
  for (const m of RACE.mines) {
    if (m.dead || !vis(m.x, m.y)) continue;
    const armed = tg - m.t0 > R_MINE_ARM, pulse = 0.5 + 0.5 * Math.sin(t * 8);
    g.save(); g.globalCompositeOperation = "lighter";
    g.fillStyle = `rgba(255,63,158,${armed ? 0.25 + 0.25 * pulse : 0.12})`; g.beginPath(); g.arc(m.x, m.y, 22, 0, 7); g.fill();
    g.fillStyle = "#ff3f9e"; g.beginPath(); g.arc(m.x, m.y, 7, 0, 7); g.fill(); g.restore();
  }
  // cars: glow, beams, body
  const order = RACE.cars.filter((c) => c === me || c.pkt);
  for (const c of order) {
    const x = c === me ? c.x : c.dx, y = c === me ? c.y : c.dy, h = c === me ? c.h : c.dh;
    if (!vis(x, y, 300)) continue;
    g.save(); g.translate(x, y); g.rotate(h);
    g.globalCompositeOperation = "lighter";
    g.drawImage(RACE.glowSpr[c.s % 4], -60, -45);
    g.drawImage(RACE.beamSpr, 20, -60);
    if (c.fl & R_FLAG.brake) { g.fillStyle = "rgba(255,40,60,0.6)"; g.fillRect(-R_CAR.len / 2 - 14, -10, 14, 20); }
    if (c.fl & R_FLAG.boost) {
      const f = 18 + Math.random() * 16;
      g.fillStyle = "rgba(255,171,64,0.85)"; g.beginPath(); g.moveTo(-R_CAR.len / 2, -6); g.lineTo(-R_CAR.len / 2 - f, 0); g.lineTo(-R_CAR.len / 2, 6); g.fill();
    }
    g.globalCompositeOperation = "source-over";
    g.globalAlpha = c.gone ? 0.35 : 1;
    g.drawImage(RACE.carSpr[c.s % 4], -32, -20);
    g.fillStyle = c.fl & R_FLAG.brake ? "#ff3348" : "#8a1c28";
    g.fillRect(-R_CAR.len / 2 + 1, -R_CAR.wid / 2 + 3, 3, 5); g.fillRect(-R_CAR.len / 2 + 1, R_CAR.wid / 2 - 8, 3, 5);
    g.globalAlpha = 1;
    if (c.fl & R_FLAG.shield) {
      g.strokeStyle = `rgba(184,255,60,${0.55 + 0.3 * Math.sin(t * 10)})`; g.lineWidth = 3;
      g.beginPath(); g.ellipse(0, 0, 36, 26, 0, 0, 7); g.stroke();
    }
    if (c.fl & R_FLAG.stun) {
      g.strokeStyle = "rgba(63,230,255,0.9)"; g.lineWidth = 2; g.beginPath();
      for (let j = 0; j < 6; j++) { const a = Math.random() * 7, r1 = 18, r2 = 32; g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); g.lineTo(Math.cos(a + 0.3) * r2, Math.sin(a + 0.3) * r2); }
      g.stroke();
    }
    g.restore();
    if (c !== me) {
      g.font = "600 13px 'Chakra Petch', system-ui"; g.textAlign = "center"; g.fillStyle = c.col;
      g.fillText(c.name, x, y - 34);
    }
  }
  // missiles
  for (const m of RACE.missiles) {
    if (m.dead || !vis(m.x, m.y)) continue;
    g.save(); g.translate(m.x, m.y); g.rotate(m.h);
    g.globalCompositeOperation = "lighter";
    const gr = g.createLinearGradient(-70, 0, 0, 0); gr.addColorStop(0, "rgba(255,171,64,0)"); gr.addColorStop(1, "rgba(255,190,90,0.85)");
    g.fillStyle = gr; g.fillRect(-70, -3, 70, 6);
    g.fillStyle = "#fff4dc"; g.beginPath(); g.moveTo(10, 0); g.lineTo(-6, -5); g.lineTo(-6, 5); g.fill();
    g.restore();
  }
  // effects
  RACE.fx = RACE.fx.filter((f) => (f.t += dt) < (f.k === "emp" ? 0.7 : 0.8));
  g.save(); g.globalCompositeOperation = "lighter";
  for (const f of RACE.fx) {
    const u = f.t / 0.8;
    if (f.k === "boom") {
      const gr = g.createRadialGradient(f.x, f.y, 2, f.x, f.y, 30 + 90 * u);
      gr.addColorStop(0, `rgba(255,230,180,${1 - u})`); gr.addColorStop(0.4, `rgba(255,120,40,${0.8 * (1 - u)})`); gr.addColorStop(1, "rgba(255,60,20,0)");
      g.fillStyle = gr; g.beginPath(); g.arc(f.x, f.y, 30 + 90 * u, 0, 7); g.fill();
    } else if (f.k === "emp") {
      const v = f.t / 0.7;
      g.strokeStyle = `rgba(63,230,255,${1 - v})`; g.lineWidth = 6 * (1 - v) + 1;
      g.beginPath(); g.arc(f.x, f.y, R_EMP_R * v, 0, 7); g.stroke();
    } else if (f.k === "zap") {
      g.strokeStyle = `rgba(63,230,255,${1 - u})`; g.lineWidth = 2; g.beginPath();
      for (let j = 0; j < 8; j++) { const a = Math.random() * 7; g.moveTo(f.x, f.y); g.lineTo(f.x + Math.cos(a) * 50, f.y + Math.sin(a) * 50); }
      g.stroke();
    } else if (f.k === "block") {
      g.strokeStyle = `rgba(184,255,60,${1 - u})`; g.lineWidth = 4; g.beginPath(); g.arc(f.x, f.y, 30 + 40 * u, 0, 7); g.stroke();
    } else if (f.k === "spark") {
      g.fillStyle = `rgba(255,220,160,${1 - u})`;
      for (const s of f.p) g.fillRect(f.x + s[0] * f.t * 300, f.y + s[1] * f.t * 300, 2.5, 2.5);
    } else if (f.k === "puff") {
      g.fillStyle = `rgba(200,200,220,${0.3 * (1 - u)})`; g.beginPath(); g.arc(f.x, f.y, 10 + 30 * u, 0, 7); g.fill();
    }
  }
  g.restore();
  // buildings in false perspective: walls lean away from the camera, roofs on top
  for (const b of T.buildings) {
    if (b.x1 < vx0 - 220 || b.x0 > vx1 + 220 || b.y1 < vy0 - 220 || b.y0 > vy1 + 220) continue;
    RACE.drawBuilding(g, b, cam, t);
  }
  // lamp heads
  g.save(); g.globalCompositeOperation = "lighter";
  for (const [lx, ly] of T.lamps) if (vis(lx, ly)) g.drawImage(RACE.lampSpr, lx - 20, ly - 20);
  g.restore();
  // start gantry
  {
    const i = 0, [ax, ay] = RACE.at(T, i, -(R_TW + R_RUN + 10)), [bx, by] = RACE.at(T, i, R_TW + R_RUN + 10);
    if (vis(ax, ay, 400)) {
      const ox = (ax + bx) / 2 - cam.x, oy = (ay + by) / 2 - cam.y, lift = 0.22;
      g.save(); g.translate(ox * lift, oy * lift);
      g.strokeStyle = "rgba(20,22,30,0.95)"; g.lineWidth = 16; g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
      g.strokeStyle = "#ffab40"; g.lineWidth = 2.5; g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
      g.translate((ax + bx) / 2, (ay + by) / 2); g.rotate(Math.atan2(by - ay, bx - ax));
      const spr = RACE.glowText(tg < 0 ? "GRID" : "GOLD CODE GP", 15, "#ffab40", "#ffe2b0");
      g.drawImage(spr, -spr.width / 2, -spr.height / 2);
      g.restore();
    }
  }
  // rain in screen space
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!still) {
    g.strokeStyle = "rgba(170,190,230,0.28)"; g.lineWidth = 1; g.beginPath();
    for (const d of RACE.rain) {
      d.y += dt * (1.3 + d.z) ; d.x += dt * 0.12;
      if (d.y > 1) { d.y -= 1; d.x = Math.random(); }
      if (d.x > 1) d.x -= 1;
      const x = d.x * W, y = d.y * H, len = 10 + 16 * d.z;
      g.moveTo(x, y); g.lineTo(x - len * 0.18, y - len);
    }
    g.stroke();
    if (Math.random() < 0.9) RACE.splash.push({ x: Math.random() * W, y: Math.random() * H, t: 0 });
    RACE.splash = RACE.splash.filter((s) => (s.t += dt) < 0.28);
    g.strokeStyle = "rgba(170,190,230,0.22)"; g.beginPath();
    for (const s of RACE.splash) { const rx = 2 + s.t * 30; g.moveTo(s.x + rx, s.y); g.ellipse(s.x, s.y, rx, 1 + s.t * 12, 0, 0, 7); }
    g.stroke();
  }
  RACE.drawMini();
};

RACE.drawBuilding = function (g, b, cam, t) {
  const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
  const ox = (cx - cam.x) * b.ht, oy = (cy - cam.y) * b.ht;
  const r = [b.x0 + ox, b.y0 + oy, b.x1 + ox, b.y1 + oy];
  if (!RACE.pats || RACE.pats.ctx !== g) {
    RACE.pats = RACE.winPat.map((c) => g.createPattern(c, "repeat"));
    RACE.pats.ctx = g;
  }
  const pat = RACE.pats[b.win];
  const wall = (pts, shade) => {
    g.beginPath(); g.moveTo(pts[0], pts[1]); for (let i = 2; i < pts.length; i += 2) g.lineTo(pts[i], pts[i + 1]); g.closePath();
    g.fillStyle = pat; g.fill(); g.fillStyle = `rgba(5,6,10,${shade})`; g.fill();
  };
  if (ox > 0) wall([b.x0, b.y0, r[0], r[1], r[0], r[3], b.x0, b.y1], 0.35);
  if (ox < 0) wall([b.x1, b.y0, r[2], r[1], r[2], r[3], b.x1, b.y1], 0.35);
  if (oy > 0) wall([b.x0, b.y0, r[0], r[1], r[2], r[1], b.x1, b.y0], 0.2);
  if (oy < 0) wall([b.x0, b.y1, r[0], r[3], r[2], r[3], b.x1, b.y1], 0.2);
  g.fillStyle = b.roof; g.fillRect(r[0], r[1], r[2] - r[0], r[3] - r[1]);
  g.strokeStyle = "rgba(150,160,190,0.18)"; g.lineWidth = 2; g.strokeRect(r[0] + 1, r[1] + 1, r[2] - r[0] - 2, r[3] - r[1] - 2);
  for (const [vx, vy, s] of b.vents) {
    const x = r[0] + 12 + vx * (r[2] - r[0] - 24 - s), y = r[1] + 12 + vy * (r[3] - r[1] - 24 - s);
    g.fillStyle = "rgba(60,66,84,0.9)"; g.fillRect(x, y, s, s);
    g.fillStyle = "rgba(255,255,255,0.05)"; g.fillRect(x + 2, y + 2, s - 4, 3);
  }
  if (b.sign) {
    const s = b.sign, flick = Math.sin(t * 3 + cx) > -0.95 ? 1 : 0.35;
    const spr = s.spr || (s.spr = RACE.glowText(s.text, 22, s.col, null, true));
    const midX = (r[0] + r[2]) / 2, midY = (r[1] + r[3]) / 2;
    const pos = [[midX, r[1] + 16, 0], [r[2] - 16, midY, Math.PI / 2], [midX, r[3] - 16, 0], [r[0] + 16, midY, -Math.PI / 2]][s.side];
    if (spr.width - 20 < (pos[2] ? r[3] - r[1] : r[2] - r[0]) - 8) {
      g.save(); g.translate(pos[0], pos[1]); g.rotate(pos[2]); g.globalAlpha = flick;
      g.drawImage(spr, -spr.width / 2, -spr.height / 2);
      g.restore();
    }
  }
};

// neon text with its glow, rendered once (a live shadow blur is slow without a GPU)
RACE.glowCache = new Map();
RACE.glowText = function (text, size, col, core, plate) {
  const key = [text, size, col, core, plate].join("|");
  let c = RACE.glowCache.get(key);
  if (c) return c;
  c = document.createElement("canvas");
  const g = c.getContext("2d"), font = `700 ${size}px 'Chakra Petch', system-ui`;
  g.font = font;
  const tw = Math.ceil(g.measureText(text).width) + 18, pad = 14;
  c.width = tw + 2 * pad; c.height = size + 16 + 2 * pad;
  g.font = font; g.textAlign = "center"; g.textBaseline = "middle";
  const cx = c.width / 2, cy = c.height / 2;
  if (plate) { g.fillStyle = "rgba(8,9,14,0.85)"; g.fillRect(cx - tw / 2, cy - (size + 8) / 2, tw, size + 8); }
  g.shadowColor = col; g.shadowBlur = 14; g.fillStyle = col; g.fillText(text, cx, cy + 1);
  g.shadowBlur = 0; g.fillStyle = core || "rgba(255,255,255,0.75)"; g.fillText(text, cx, cy + 1);
  RACE.glowCache.set(key, c);
  return c;
};

RACE.spark = function (x, y, v) {
  RACE.fx.push({ k: "spark", x, y, t: 0, p: Array.from({ length: 10 }, () => { const a = Math.random() * 7; return [Math.cos(a), Math.sin(a)]; }) });
};

RACE.drawMini = function () {
  const T = RACE.T, cv = RACE.miniCv, g = cv.getContext("2d");
  const [x0, y0, x1, y1] = [T.box[0] + 700, T.box[1] + 700, T.box[2] - 700, T.box[3] - 700];
  const s = Math.min((cv.width - 30) / (x1 - x0), (cv.height - 30) / (y1 - y0));
  const ox = (cv.width - (x1 - x0) * s) / 2 - x0 * s, oy = (cv.height - (y1 - y0) * s) / 2 - y0 * s;
  if (!RACE.mini) {
    const c = document.createElement("canvas"); c.width = cv.width; c.height = cv.height;
    const m = c.getContext("2d");
    m.lineJoin = "round"; m.beginPath(); m.moveTo(T.x[0] * s + ox, T.y[0] * s + oy);
    for (let i = 1; i < T.M; i++) m.lineTo(T.x[i] * s + ox, T.y[i] * s + oy);
    m.closePath(); m.strokeStyle = "rgba(230,235,250,0.35)"; m.lineWidth = 7; m.stroke();
    const [ax, ay] = RACE.at(T, 0, -R_TW), [bx, by] = RACE.at(T, 0, R_TW);
    m.strokeStyle = "#ffab40"; m.lineWidth = 3; m.beginPath(); m.moveTo(ax * s + ox, ay * s + oy); m.lineTo(bx * s + ox, by * s + oy); m.stroke();
    RACE.mini = c;
  }
  g.clearRect(0, 0, cv.width, cv.height);
  g.drawImage(RACE.mini, 0, 0);
  for (const c of RACE.cars) {
    const x = c === RACE.me ? c.x : c.dx, y = c === RACE.me ? c.y : c.dy;
    if (x === undefined) continue;
    g.fillStyle = c.col; g.beginPath(); g.arc(x * s + ox, y * s + oy, c === RACE.me ? 7 : 5.5, 0, 7); g.fill();
    if (c === RACE.me) { g.strokeStyle = "#fff"; g.lineWidth = 2; g.stroke(); }
  }
};

RACE.drawHud = function (tg) {
  const me = RACE.me, h = RACE.hud, order = RACE.order(), T = RACE.T;
  const rank = order.indexOf(me) + 1;
  h.pos.innerHTML = `<span class="big">${rank || "–"}</span><span class="of">/${order.length}</span>`;
  const lap = Math.min(RACE.laps, Math.max(1, Math.floor(me.d / T.L) + 1));
  h.lap.innerHTML = me.finT !== null ? `<b>Finished</b>` : `Lap <b>${lap}</b>/${RACE.laps}`;
  const cur = tg > 0 && me.finT === null ? tg - me.lapStart : null;
  h.times.innerHTML = [cur !== null ? `This lap ${RACE.fmt(cur)}` : "", me.best !== null ? `Best lap ${RACE.fmt(me.best)}` : "",
    me.finT !== null ? `Total ${RACE.fmt(me.finT)}` : ""].filter(Boolean).join("<br>");
  h.speed.querySelector(".v").textContent = Math.round(Math.hypot(me.vx, me.vy) * 0.36);
  const it = me.item;
  h.item.className = "item" + (it ? " full" : "");
  h.item.innerHTML = `<span class="slot">${it ? R_ICON[it] : ""}</span><span class="nm">${it ? `${R_ITEM_NAMES[it]}<kbd>Space</kbd><small>${R_ITEM_HELP[it]}</small>` : "No pickup<small>drive through a holo cache</small>"}</span>`;
  const leader = order[0];
  h.board.innerHTML = order.map((c, k) => {
    let q;
    if (c.fin !== null) q = RACE.fmt(c.fin / 1000);
    else if (c === me) q = k === 0 ? "leading" : `${Math.round((leader.d - c.d) / 10)} m back`;
    else {
      const rate = c.rx.length > 1 ? (c.rx.length - 1) / ((c.rx[c.rx.length - 1] - c.rx[0]) / 1000) : 0;
      q = c.gone ? "left" : c.pkt ? `${rate.toFixed(0)}/s, ${Math.round((c.age || 0) * 1000)} ms` : "no signal";
    }
    return `<div class="${c === me ? "r-me" : ""}"><span class="sw" style="background:${c.col}"></span><span class="nm">${k + 1}. ${esc(c === me ? "You" : c.name)}</span><span class="q">${q}</span></div>`;
  }).join("");
  // countdown / messages
  let count = "", cls = "count";
  if (tg < 0 && tg > -3) count = String(Math.ceil(-tg));
  else if (tg >= 0 && tg < 0.9) { count = "GO"; cls = "count go"; }
  h.count.className = cls; h.count.textContent = count;
  h.keys.textContent = tg < -0.2 ? "Arrows or W A S D to drive, Space to use a pickup" : "";
  if (tg < -3) h.msg.textContent = "Get ready";
  else {
    RACE.msgs = RACE.msgs.filter((m) => m.until > performance.now());
    h.msg.textContent = RACE.msgs.length ? RACE.msgs[RACE.msgs.length - 1].text : "";
  }
};

RACE.showResults = function (res) {
  const box = RACE.root?.querySelector(".race-results");
  if (!box) return;
  const host = RACE.room.h === S.me.addr, T = RACE.T;
  const rows = res.map((r, k) => `<tr><td>${k + 1}.</td><td><span style="color:${R_SEAT[r.s % 4]}">■</span> ${esc(r.a === S.me.addr ? "You" : r.name)}</td>
    <td class="t">${r.ft !== null && r.ft !== undefined ? RACE.fmt(r.ft / 1000) : r.gone ? "left" : `DNF, ${Math.max(0, r.d / T.L).toFixed(1)} laps`}</td></tr>`).join("");
  box.hidden = false;
  box.innerHTML = `<div class="box"><h3>Results</h3><table>${rows}</table><div class="acts">
    ${host ? `<button class="primary" data-act="start" data-r="${RACE.rid}">Rematch on a new track</button>` : `<span style="opacity:.75">${esc(GC.name(RACE.room, RACE.room.h))} can start a rematch.</span>`}
    <button class="gc-ghost" data-act="leave" data-r="${RACE.rid}">${host ? "Close room" : "Leave"}</button></div></div>`;
  GC.wire(box);
};

// ====================================================================== poster / lobby backdrop: a wet street at night
RACE.backdropStatic = function (w, h) {
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w)); c.height = Math.max(1, Math.round(h));
  const g = c.getContext("2d"), hz = h * 0.42;
  g.fillStyle = "#0b0d13"; g.fillRect(0, 0, w, h);
  const sky = g.createLinearGradient(0, 0, 0, hz);
  sky.addColorStop(0, "#07080d"); sky.addColorStop(1, "#1b1424");
  g.fillStyle = sky; g.fillRect(0, 0, w, hz);
  const r = rMulberry(7);                               // skyline with lit windows
  for (let x = -20; x < w + 40;) {
    const bw = 40 + r() * 90, bh = hz * (0.35 + r() * 0.6);
    g.fillStyle = `hsl(${235 + r() * 30} 18% ${7 + r() * 5}%)`; g.fillRect(x, hz - bh, bw, bh);
    for (let yy = hz - bh + 8; yy < hz - 6; yy += 9) for (let xx = x + 5; xx < x + bw - 6; xx += 8)
      if (r() < 0.22) { g.fillStyle = r() < 0.75 ? "rgba(255,190,110,0.75)" : "rgba(120,220,255,0.7)"; g.fillRect(xx, yy, 3, 4); }
    x += bw + 4 + r() * 10;
  }
  g.fillStyle = "#11141c";                              // the street, receding to the horizon
  g.beginPath(); g.moveTo(w * 0.47, hz); g.lineTo(w * 0.53, hz); g.lineTo(w * 1.1, h); g.lineTo(-w * 0.1, h); g.fill();
  g.globalCompositeOperation = "lighter";               // sodium lamps down both sides
  for (let k = 0; k < 6; k++) {
    const f = k / 6, y = hz + (h - hz) * (f * f) + 6, spread = 0.03 + 0.55 * f * f;
    for (const side of [-1, 1]) {
      const x = w / 2 + side * w * spread, rad = 12 + 220 * f * f;
      const gr = g.createRadialGradient(x, y, 1, x, y, rad);
      gr.addColorStop(0, "rgba(255,200,120,0.9)"); gr.addColorStop(0.2, "rgba(255,160,60,0.35)"); gr.addColorStop(1, "rgba(255,140,40,0)");
      g.fillStyle = gr; g.beginPath(); g.arc(x, y, rad, 0, 7); g.fill();
    }
  }
  return c;
};

RACE.backdrop = function (g, w, h, t) {
  const key = `${Math.round(w)}x${Math.round(h)}`;
  if (!RACE.bd || RACE.bd.key !== key) RACE.bd = { key, img: RACE.backdropStatic(w, h) };
  g.drawImage(RACE.bd.img, 0, 0, w, h);
  const hz = h * 0.42;
  g.font = `700 ${Math.round(h * 0.035)}px 'Chakra Petch', system-ui`; g.textAlign = "center";
  [[0.16, 0.2, "#ff3f9e", "KARAOKE"], [0.8, 0.26, "#3fe6ff", "RAMEN 24H"]].forEach(([sx, sy, col, txt], k) => {
    // a neon sign (it stutters now and then) and its smeared reflection on the wet road
    const on = Math.sin(t * 7.3 + k * 2.1) + Math.sin(t * 2.9 + k * 4.4) > -1.75;
    const spr = RACE.glowText(txt, Math.round(h * 0.035), col, null, false);
    g.save(); g.globalAlpha = on ? 1 : 0.3;
    g.drawImage(spr, sx * w - spr.width / 2, sy * h - spr.height / 2);
    g.globalCompositeOperation = "lighter";
    const top = hz + 12, rows = 22;
    for (let j = 0; j < rows; j++) {
      const f = j / rows, y = top + (h - top) * f;
      const wob = Math.sin(t * 1.7 + j * 0.9 + k) * (3 + 10 * f);
      const half = (spr.width * 0.42) * (1 - 0.55 * f);
      g.globalAlpha = (on ? 0.22 : 0.06) * (1 - f) * (0.6 + 0.4 * Math.sin(j * 1.7 + t * 3));
      g.fillStyle = col;
      g.fillRect(sx * w - half + wob, y, 2 * half, 1.6 + 2.2 * f);
    }
    g.restore();
  });
  g.strokeStyle = "rgba(170,190,230,0.3)"; g.lineWidth = 1; g.beginPath();
  const rr = rMulberry(3);
  for (let k = 0; k < 160; k++) {
    const sp = 0.6 + rr();
    const x = (rr() * w + t * 40 * sp) % w, y = (rr() * h + t * 520 * sp) % h, len = 8 + 14 * sp;
    g.moveTo(x, y); g.lineTo(x - len * 0.15, y - len);
  }
  g.stroke();
  const fade = g.createLinearGradient(0, h * 0.45, 0, h);
  fade.addColorStop(0, "rgba(11,13,19,0)"); fade.addColorStop(1, "rgba(11,13,19,0.85)");
  g.fillStyle = fade; g.fillRect(0, 0, w, h);
};

// canvas text drawn before the bundled font has loaded would keep the fallback font: reset the cache
if (document.fonts) document.fonts.load("700 20px 'Chakra Petch'").then(() => RACE.glowCache.clear()).catch(() => {});

GC.mods.race = RACE;
