"use strict";
/* Slot Rush: top-down combat racing across 5 RF-themed biomes.

   Every station drives its own car here (physics at 120 steps per second) and posts the car's
   state to its station 20 times a second; the station broadcasts the newest state in its own
   time slot (games/hub.py).  The other cars arrive as 'race' events and are drawn where their
   last state says they are now (dead reckoning, blended over 0.1 s).  Hits are decided by the
   victim's station: it simulates incoming missiles against its own exact position, checks
   mines when it drives over them and EMPs against where it was when they went off.

   The track and the biome props are generated deterministically from the room's seed + biome,
   so every station builds the same circuit without sending it.  Keys: arrows or WASD, Space
   uses the pickup, V switches between the north-up camera and the chase camera. */

const R_SEAT = ["#3fe6ff", "#ff3f9e", "#b8ff3c", "#a47cff"];
const R_TW = 112;                 // half the track width (px)
const R_RUN = 44;                 // run-off between the track edge and the rail
const R_STEP = 8;                 // centreline sample spacing (px)
const R_DT = 1 / 120;
const R_ITEMS = ["", "missile", "mine", "emp", "shield", "boost"];
const R_ITEM_NAMES = {
  missile: "Carrier Seeker",
  mine: "ISI Mine",
  emp: "RF Jammer",
  shield: "Walsh Shield",
  boost: "64-QAM Overdrive",
};
const R_ITEM_HELP = {
  missile: "locks on to the car ahead",
  mine: "dropped behind you",
  emp: "stuns cars within 280 px",
  shield: "blocks the next hit",
  boost: "short burst of speed",
};
const R_FLAG = { boost: 1, shield: 2, stun: 4, fin: 8, brake: 16, rev: 32, off: 64, spin: 128 };
const R_CAR = { len: 46, wid: 24, rad: 20, vmax: 820, acc: 620, brake: 1250, rev: 230, drag: 0.22 };
const R_EMP_R = 280, R_MISSILE_V = 1300, R_MISSILE_LIFE = 6.0, R_MINE_LIFE = 25, R_MINE_ARM = 0.6;
const R_ICON = {
  missile: '<svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="10" fill="none" stroke="#ffab40" stroke-width="2" stroke-dasharray="4 3"/><path d="M8 16h12l5-3v6l-5-3" fill="#ffab40"/><circle cx="16" cy="16" r="2" fill="#fff"/></svg>',
  mine: '<svg viewBox="0 0 32 32"><path d="M4 16q3-8 6 0t6 0 6 0 6 0" fill="none" stroke="#ffab40" stroke-width="2"/><circle cx="16" cy="16" r="6" fill="#ff3b30"/><circle cx="16" cy="16" r="11" fill="none" stroke="#ff3b30" stroke-width="1.8" stroke-dasharray="3 3"/></svg>',
  emp: '<svg viewBox="0 0 32 32"><path d="M18 3 8 18h7l-2 11 11-16h-7z" fill="#3fe6ff"/><circle cx="16" cy="16" r="13" fill="none" stroke="#3fe6ff" stroke-width="1.5" stroke-dasharray="2 3"/></svg>',
  shield: '<svg viewBox="0 0 32 32"><polygon points="16,3 27,9.5 27,22.5 16,29 5,22.5 5,9.5" fill="rgba(0,229,255,0.14)" stroke="#00e5ff" stroke-width="2.4"/></svg>',
  boost: '<svg viewBox="0 0 32 32"><g fill="#ffab40"><circle cx="9" cy="9" r="2"/><circle cx="16" cy="9" r="2"/><circle cx="23" cy="9" r="2"/><circle cx="9" cy="16" r="2"/><circle cx="16" cy="16" r="2"/><circle cx="23" cy="16" r="2"/><circle cx="9" cy="23" r="2"/><circle cx="16" cy="23" r="2"/><circle cx="23" cy="23" r="2"/></g></svg>',
};
const R_SIGNS = ["WAVELINK", "SLOT RUSH", "SPREAD", "2.5 GHz", "CHIP RATE", "NOODLES", "HOTEL ROLL-OFF", "QPSK BAR", "LDPC", "PREAMBLE",
  "RAMEN 24H", "KARAOKE", "PARITY", "SYNC", "DESPREAD", "OPEN LATE", "PACHINKO", "CAPSULE", "TEA"];
const R_ROOF_TAGS = ["WALSH", "LDPC", "2.5G"];
const R_IC_TAGS = ["AD9361", "ZYNQ-7020", "LDPC", "BG1"];

// Centralized 5-biome configuration: palettes, particles, music hints, and 120 Hz handling multipliers.
const BIOMES = [
  {
    id: 0,
    name: "Neon Rain City",
    bg: "#0B0F1A",
    surface: "#181F2E",
    runoff: "#101522",
    markings: "#2C364D",
    railL: "#FF2E88",
    railR: "#00E5FF",
    accent: "#FFB347",
    weather: "rain",
    particleCol: "rgba(180,220,255,0.28)",
    music: "night",
    phys: { topSpeedMul: 1.00, accelMul: 1.00, lateralGrip: 1.00, driftDamp: 1.00, railBounce: 0.55, boostMul: 1.00 },
  },
  {
    id: 1,
    name: "PCB Speedway",
    bg: "#072A24",
    surface: "#11161C",
    runoff: "#0B1E1A",
    markings: "#D4AF37",
    railL: "#E6B84C",
    railR: "#E6B84C",
    accent: "#00FFC8",
    weather: "sparks",
    particleCol: "rgba(0,255,200,0.35)",
    music: "carrier",
    phys: { topSpeedMul: 1.06, accelMul: 1.10, lateralGrip: 1.18, driftDamp: 1.15, railBounce: 0.70, boostMul: 1.12 },
  },
  {
    id: 2,
    name: "AWGN Desert",
    bg: "#C87D4B",
    surface: "#4A3F3A",
    runoff: "#8A5638",
    markings: "#F4D39E",
    railL: "#8C3B2B",
    railR: "#E07A5F",
    accent: "#FF6B35",
    weather: "dust",
    particleCol: "rgba(244,211,158,0.35)",
    music: "night",
    phys: { topSpeedMul: 1.08, accelMul: 0.95, lateralGrip: 0.84, driftDamp: 0.86, railBounce: 0.45, boostMul: 1.05 },
  },
  {
    id: 3,
    name: "Carrier Peak",
    bg: "#DCE9F5",
    surface: "#2E3B4E",
    runoff: "#9BB3CC",
    markings: "#A5C9E8",
    railL: "#FF6D00",
    railR: "#FF6D00",
    accent: "#7DD3FC",
    weather: "snow",
    particleCol: "rgba(255,255,255,0.75)",
    music: "rain",
    phys: { topSpeedMul: 0.95, accelMul: 0.90, lateralGrip: 0.66, driftDamp: 0.70, railBounce: 0.40, boostMul: 1.08 },
  },
  {
    id: 4,
    name: "Sunset Coast",
    bg: "#0F4C5C",
    bgSea: "#0F4C5C",
    bgSand: "#E6B88A",
    surface: "#2B2A38",
    runoff: "#5C4D46",
    markings: "#FFD166",
    railL: "#FF7F50",
    railR: "#48CAE4",
    accent: "#F72585",
    weather: "mist",
    particleCol: "rgba(255,209,102,0.22)",
    music: "drift",
    phys: { topSpeedMul: 1.03, accelMul: 1.02, lateralGrip: 0.95, driftDamp: 0.96, railBounce: 0.55, boostMul: 1.14 },
  },
];

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
  touch: {
    left: false, right: false, gas: false, brake: false, autoGas: false,
    steerPid: null, gasPids: new Set(), brakePids: new Set(),
  },
  raf: 0, last: 0, acc: 0, postT: 0, posting: false, outEv: [], eid: 0, missiles: [], mines: [], fx: [],
  rain: [], splash: [], tiles: new Map(), hudT: 0, msgs: [], results: null, syncT: 0, laps: 3, seatN: 2,
  biome: 0, B: BIOMES[0], BIOMES, spec: false, specCam: null, quiet: false,
  cam: { x: 0, y: 0, z: 1 }, hist: [], seen: new Map(), mini: null, lastPos: 0,
};

// ====================================================================== track + biome props
RACE.buildTrack = function (seed, biomeId = 0) {
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
    for (let i = 0; i < 1200; i++) { const a = i / 1200 * 2 * Math.PI; pts.push([CX + Math.cos(a) * RX * 0.8, CY + Math.sin(a) * RY * 0.8]); }
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
  return RACE.finishTrack(pts, rng, seed, biomeId);
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

RACE.finishTrack = function (pts, rng, seed = 0, biomeId = 0) {
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
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, area = 0;
  for (let i = 0; i < M; i++) {
    const j = (i + 1) % M;
    area += x[i] * y[j] - x[j] * y[i];
    x0 = Math.min(x0, x[i]); y0 = Math.min(y0, y[i]); x1 = Math.max(x1, x[i]); y1 = Math.max(y1, y[i]);
  }
  // Signed lateral offset (+off is (-ty, tx)): in Y-down screen coords, area > 0 means clockwise loop, so -1 points outward.
  const outSide = area > 0 ? -1 : 1;
  const fitPad = R_TW + R_RUN + 320;
  const T = {
    M, L, x, y, tx, ty, curv, outSide,
    box: [x0 - 900, y0 - 900, x1 + 900, y1 + 900],
    fitBox: [x0 - fitPad, y0 - fitPad, x1 + fitPad, y1 + fitPad],
    grid: new Map(), cell: 160,
  };
  for (let i = 0; i < M; i++) {
    const k = `${Math.floor(x[i] / T.cell)},${Math.floor(y[i] / T.cell)}`;
    (T.grid.get(k) || T.grid.set(k, []).get(k)).push(i);
  }
  // Shared RNG order for boost pads and holo caches (identical across all biomes)
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
  RACE.buildBiomeProps(T, seed, biomeId);
  return T;
};

RACE.buildBiomeProps = function (T, seed, biomeId) {
  const M = T.M, [bx0, by0, bx1, by1] = T.box;
  const prng = rMulberry(((seed >>> 0) ^ Math.imul((biomeId + 1), 0x85ebca6b)) >>> 0);
  T.biomeId = biomeId;
  T.lamps = []; T.puddles = []; T.buildings = [];
  T.buses = []; T.vias = []; T.ics = []; T.smds = [];
  T.mesas = []; T.scrubs = []; T.cracks = []; T.dishes = [];
  T.cliffs = []; T.ice = []; T.pines = []; T.poles = [];
  T.coast = null; T.foam = []; T.palms = []; T.piers = []; T.stacks = [];

  if (biomeId === 0) {
    for (let i = 0, side = 1; i < M; i += Math.round(240 / R_STEP), side = -side) {
      const n = [-T.ty[i], T.tx[i]], d = R_TW + R_RUN + 26;
      T.lamps.push([T.x[i] + n[0] * d * side, T.y[i] + n[1] * d * side]);
    }
    for (let k = 0; k < 70; k++) {
      const i = Math.floor(prng() * M), n = [-T.ty[i], T.tx[i]], o = (prng() - 0.5) * 2 * (R_TW + 30);
      T.puddles.push([T.x[i] + n[0] * o, T.y[i] + n[1] * o, 18 + prng() * 46, 8 + prng() * 22, prng() * Math.PI]);
    }
    const C = 290;
    let tagIdx = 0;
    for (let gx = bx0 - 200; gx < bx1 + 200; gx += C) {
      for (let gy = by0 - 200; gy < by1 + 200; gy += C) {
        const w = 130 + prng() * 120, h = 130 + prng() * 120;
        const cx = gx + C / 2 + (prng() - 0.5) * 40, cy = gy + C / 2 + (prng() - 0.5) * 40;
        const dist = RACE.distToTrack(T, cx, cy);
        if (dist < R_TW + R_RUN + 40 + Math.hypot(w, h) / 2) continue;
        const lit = prng();
        T.buildings.push({
          x0: cx - w / 2, y0: cy - h / 2, x1: cx + w / 2, y1: cy + h / 2, ht: 0.18 + prng() * 0.32,
          roof: `hsl(${230 + prng() * 40} ${10 + prng() * 14}% ${9 + prng() * 7}%)`, win: Math.floor(prng() * 3),
          roofTag: prng() < 0.72 ? R_ROOF_TAGS[(tagIdx++) % R_ROOF_TAGS.length] : null,
          sign: dist < R_TW + R_RUN + 320 && lit < 0.55 ? {
            text: R_SIGNS[Math.floor(prng() * R_SIGNS.length)],
            col: R_SEAT[Math.floor(prng() * 4)], side: Math.floor(prng() * 4),
          } : null,
          vents: Array.from({ length: 1 + Math.floor(prng() * 3) }, () => [prng(), prng(), 12 + prng() * 18]),
        });
      }
    }
  } else if (biomeId === 1) {
    // 45-degree copper trace buses + concentric ENIG gold vias
    for (let k = 0; k < 44; k++) {
      const sx = bx0 + prng() * (bx1 - bx0), sy = by0 + prng() * (by1 - by0);
      const diag = 140 + prng() * 240, l1 = 120 + prng() * 280, l2 = 120 + prng() * 280;
      const sx1 = prng() < 0.5 ? 1 : -1, sy1 = prng() < 0.5 ? 1 : -1;
      const horizFirst = prng() < 0.5;
      const p0 = [sx, sy];
      const p1 = horizFirst ? [sx + sx1 * l1, sy] : [sx, sy + sy1 * l1];
      const p2 = [p1[0] + sx1 * diag, p1[1] + sy1 * diag];
      const p3 = horizFirst ? [p2[0] + sx1 * l2, p2[1]] : [p2[0], p2[1] + sy1 * l2];
      const pts = [p0, p1, p2, p3];
      const lines = 3 + Math.floor(prng() * 4), step = 10;
      const bxMin = Math.min(...pts.map((p) => p[0])) - 50, bxMax = Math.max(...pts.map((p) => p[0])) + 50;
      const byMin = Math.min(...pts.map((p) => p[1])) - 50, byMax = Math.max(...pts.map((p) => p[1])) + 50;
      T.buses.push({ pts, lines, step, horizFirst, box: [bxMin, byMin, bxMax, byMax] });
      for (let ln = 0; ln < lines; ln++) {
        const ox = horizFirst ? 0 : (ln - (lines - 1) / 2) * step;
        const oy = horizFirst ? (ln - (lines - 1) / 2) * step : 0;
        if (RACE.distToTrack(T, p0[0] + ox, p0[1] + oy) > R_TW + R_RUN + 14) T.vias.push([p0[0] + ox, p0[1] + oy, 6.5]);
        if (RACE.distToTrack(T, p3[0] + ox, p3[1] + oy) > R_TW + R_RUN + 14) T.vias.push([p3[0] + ox, p3[1] + oy, 6.5]);
      }
    }
    // Scattered stitched ground vias
    for (let k = 0; k < 220; k++) {
      const vx = bx0 + prng() * (bx1 - bx0), vy = by0 + prng() * (by1 - by0);
      if (RACE.distToTrack(T, vx, vy) > R_TW + R_RUN + 18) T.vias.push([vx, vy, 5.5 + prng() * 2.5]);
    }
    // Black QFP/BGA IC packages with silver gull-wing pins
    const C = 320;
    let icIdx = 0;
    for (let gx = bx0 - 150; gx < bx1 + 150; gx += C) {
      for (let gy = by0 - 150; gy < by1 + 150; gy += C) {
        if (prng() < 0.34) continue;
        const sz = 92 + prng() * 96, h = prng() < 0.35 ? sz : sz * (0.72 + prng() * 0.45);
        const cx = gx + C / 2 + (prng() - 0.5) * 50, cy = gy + C / 2 + (prng() - 0.5) * 50;
        const rad = Math.hypot(sz, h) / 2 + 24;
        if (RACE.distToTrack(T, cx, cy) < R_TW + R_RUN + 36 + rad) continue;
        T.ics.push({
          cx, cy, w: sz, h, rot: prng() < 0.5 ? 0 : -Math.PI / 2,
          qfp: prng() < 0.72, tag: R_IC_TAGS[(icIdx++) % R_IC_TAGS.length], rad,
        });
      }
    }
    // 0603 SMD passives along both track shoulders
    for (let i = 0; i < M; i += Math.round(96 / R_STEP)) {
      for (const side of [-1, 1]) {
        if (prng() < 0.25) continue;
        const off = side * (R_TW + R_RUN * (0.35 + prng() * 0.32));
        const [sx, sy] = RACE.at(T, i, off);
        T.smds.push({ x: sx, y: sy, a: Math.atan2(T.ty[i], T.tx[i]) + (prng() < 0.5 ? 0 : Math.PI / 2), cap: prng() < 0.45 });
      }
    }
  } else if (biomeId === 2) {
    // Stepped canyon mesas
    const C = 360;
    for (let gx = bx0 - 200; gx < bx1 + 200; gx += C) {
      for (let gy = by0 - 200; gy < by1 + 200; gy += C) {
        if (prng() < 0.38) continue;
        const cx = gx + C / 2 + (prng() - 0.5) * 60, cy = gy + C / 2 + (prng() - 0.5) * 60;
        const r = 68 + prng() * 72;
        if (RACE.distToTrack(T, cx, cy) < R_TW + R_RUN + 46 + r) continue;
        const n = 7 + Math.floor(prng() * 4), pts = [];
        for (let k = 0; k < n; k++) {
          const a = (k / n) * Math.PI * 2 + (prng() - 0.5) * 0.28;
          const rr = r * (0.72 + prng() * 0.35);
          pts.push([cx + Math.cos(a) * rr, cy + Math.sin(a) * rr]);
        }
        T.mesas.push({ cx, cy, r: r + 24, pts });
      }
    }
    // Dark-olive scrub clusters
    for (let k = 0; k < 220; k++) {
      const sx = bx0 + prng() * (bx1 - bx0), sy = by0 + prng() * (by1 - by0);
      if (RACE.distToTrack(T, sx, sy) < R_TW + R_RUN + 18) continue;
      T.scrubs.push({ x: sx, y: sy, r: 6 + prng() * 11, n: 3 + Math.floor(prng() * 4), seed: prng() * 100 });
    }
    // Cracked-earth tarmac edge polylines propagating inward onto the asphalt
    for (let i = 0; i < M; i += Math.round(130 / R_STEP)) {
      const side = prng() < 0.5 ? -1 : 1, inSide = -side;
      const [ex, ey] = RACE.at(T, i, side * (R_TW - 1));
      const a0 = Math.atan2(T.tx[i] * inSide, -T.ty[i] * inSide) + (prng() - 0.5) * 0.7;
      const segs = [[ex, ey]];
      let px = ex, py = ey, a = a0;
      for (let s = 0; s < 4; s++) {
        a += (prng() - 0.5) * 0.9;
        px += Math.cos(a) * (7 + prng() * 9);
        py += Math.sin(a) * (7 + prng() * 9);
        segs.push([px, py]);
      }
      let twig = null;
      if (prng() < 0.55) {
        let bx = segs[2][0], by = segs[2][1], ba = a0 + (prng() < 0.5 ? 0.65 : -0.65);
        twig = [[bx, by]];
        for (let s = 0; s < 2; s++) {
          ba += (prng() - 0.5) * 0.6;
          bx += Math.cos(ba) * (6 + prng() * 7);
          by += Math.sin(ba) * (6 + prng() * 7);
          twig.push([bx, by]);
        }
      }
      T.cracks.push({ segs, twig });
    }
    // Circular white parabolic RF ground-station clusters
    for (let k = 0; k < 14; k++) {
      const cx = bx0 + 180 + prng() * (bx1 - bx0 - 360), cy = by0 + 180 + prng() * (by1 - by0 - 360);
      if (RACE.distToTrack(T, cx, cy) < R_TW + R_RUN + 130) continue;
      const count = 2 + Math.floor(prng() * 3), arr = [];
      for (let j = 0; j < count; j++) {
        const a = (j / count) * Math.PI * 2 + prng() * 0.6, d = 38 + prng() * 34;
        arr.push({ x: cx + Math.cos(a) * d, y: cy + Math.sin(a) * d, r: 22 + prng() * 14, aim: prng() * Math.PI * 2 });
      }
      T.dishes.push({ cx, cy, r: 95, arr });
    }
  } else if (biomeId === 3) {
    // Soft slate cliff shadows
    for (let k = 0; k < 34; k++) {
      const cx = bx0 + prng() * (bx1 - bx0), cy = by0 + prng() * (by1 - by0), r = 90 + prng() * 120;
      if (RACE.distToTrack(T, cx, cy) < R_TW + R_RUN + 55 + r) continue;
      const n = 6 + Math.floor(prng() * 3), pts = [];
      for (let j = 0; j < n; j++) {
        const a = (j / n) * Math.PI * 2;
        const rr = r * (0.65 + prng() * 0.45);
        pts.push([cx + Math.cos(a) * rr, cy + Math.sin(a) * rr]);
      }
      T.cliffs.push({ cx, cy, r: r + 30, pts });
    }
    // Translucent ice patches with jagged white fractures (both off-track and light patches on track)
    for (let k = 0; k < 85; k++) {
      const onTrack = k < 28;
      const rx = onTrack ? 22 + prng() * 30 : 35 + prng() * 65, ry = rx * (0.5 + prng() * 0.45);
      let px, py;
      if (onTrack) {
        const i = Math.floor(prng() * M);
        [px, py] = RACE.at(T, i, (prng() - 0.5) * 2 * (R_TW - 12));
      } else {
        px = bx0 + prng() * (bx1 - bx0); py = by0 + prng() * (by1 - by0);
        if (RACE.distToTrack(T, px, py) < R_TW + R_RUN + rx + 14) continue;
      }
      const a = prng() * Math.PI;
      const fr = Array.from({ length: 3 }, () => {
        const ang0 = prng() * Math.PI * 2;
        return [
          [Math.cos(ang0) * rx * 0.2, Math.sin(ang0) * ry * 0.2],
          [Math.cos(ang0 + 0.35) * rx * 0.55, Math.sin(ang0 + 0.35) * ry * 0.55],
          [Math.cos(ang0 - 0.2) * rx * 0.85, Math.sin(ang0 - 0.2) * ry * 0.85],
        ];
      });
      T.ice.push({ x: px, y: py, rx, ry, a, onTrack, fr });
    }
    // Star-shaped dark pine clusters with snow caps
    const C = 230;
    for (let gx = bx0 - 150; gx < bx1 + 150; gx += C) {
      for (let gy = by0 - 150; gy < by1 + 150; gy += C) {
        if (prng() < 0.25) continue;
        const cx = gx + C / 2 + (prng() - 0.5) * 70, cy = gy + C / 2 + (prng() - 0.5) * 70;
        if (RACE.distToTrack(T, cx, cy) < R_TW + R_RUN + 46) continue;
        const n = 2 + Math.floor(prng() * 4), trees = [];
        for (let j = 0; j < n; j++) {
          const a = prng() * Math.PI * 2, d = j === 0 ? 0 : 18 + prng() * 26;
          trees.push([cx + Math.cos(a) * d, cy + Math.sin(a) * d, 14 + prng() * 14, prng() * Math.PI]);
        }
        T.pines.push({ cx, cy, r: 58, trees });
      }
    }
    // Bright orange snow-pole chevrons outside tight apexes
    for (let i = 0; i < M; i += Math.round(72 / R_STEP)) {
      if (Math.abs(T.curv[i]) > 0.0022) {
        const outBend = -Math.sign(T.curv[i]);
        const [px, py] = RACE.at(T, i, outBend * (R_TW + R_RUN + 18));
        T.poles.push({ x: px, y: py, a: Math.atan2(T.ty[i], T.tx[i]) + (outBend < 0 ? Math.PI : 0) });
      }
    }
  } else if (biomeId === 4) {
    // Split ocean (outer normal) vs warm sand (inner normal) along spline's outer normal
    const coast = [];
    for (let i = 0; i < M; i += 2) {
      const wave = 14 * Math.sin(i * 0.09) + 6 * Math.cos(i * 0.21);
      coast.push(RACE.at(T, i, T.outSide * (R_TW + R_RUN + 28 + wave)));
    }
    T.coast = coast;
    for (let i = 0; i < M; i += Math.round(140 / R_STEP)) {
      const wave = 14 * Math.sin(i * 0.09) + 6 * Math.cos(i * 0.21);
      const [fx, fy] = RACE.at(T, i, T.outSide * (R_TW + R_RUN + 54 + wave));
      const nx = -T.ty[i] * T.outSide, ny = T.tx[i] * T.outSide;
      T.foam.push({ x: fx, y: fy, a: Math.atan2(ny, nx), len: 42 + prng() * 44 });
    }
    // Wooden boardwalk piers extending into the ocean
    for (let i = 0; i < M; i += Math.round(780 / R_STEP)) {
      if (Math.abs(T.curv[i]) > 0.0024) continue;
      const [px, py] = RACE.at(T, i, T.outSide * (R_TW + R_RUN + 24));
      const nx = -T.ty[i] * T.outSide, ny = T.tx[i] * T.outSide;
      const len = 110 + prng() * 55;
      // Only add if pier tip stays clear of other track segments
      if (RACE.distToTrack(T, px + nx * len, py + ny * len) > R_TW + R_RUN + 45) {
        T.piers.push({ x: px, y: py, a: Math.atan2(ny, nx), len, w: 38 });
      }
    }
    // 5-frond palm silhouettes and twilight harbor container stacks on the warm sand side
    const C = 250;
    for (let gx = bx0 + 250; gx < bx1 - 250; gx += C) {
      for (let gy = by0 + 250; gy < by1 - 250; gy += C) {
        const cx = gx + C / 2 + (prng() - 0.5) * 50, cy = gy + C / 2 + (prng() - 0.5) * 50;
        const ni = RACE.nearest(T, cx, cy);
        const lat = RACE.lateral(T, ni, cx, cy);
        if (lat * T.outSide > 0) continue; // ocean side
        if (RACE.distToTrack(T, cx, cy) < R_TW + R_RUN + 46) continue;
        if (prng() < 0.22) {
          const cols = ["#E07A5F", "#2A9D8F", "#FFD166", "#457B9D", "#F72585"];
          const boxes = Array.from({ length: 4 + Math.floor(prng() * 3) }, (_, j) => ({
            ox: (j % 2) * 32 - 30, oy: Math.floor(j / 2) * 16 - 22,
            col: cols[Math.floor(prng() * cols.length)],
          }));
          T.stacks.push({ x: cx, y: cy, a: prng() * Math.PI, r: 64, boxes });
        } else {
          T.palms.push({ x: cx, y: cy, r: 26 + prng() * 14, rot: prng() * Math.PI * 2 });
        }
      }
    }
  }
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
RACE.BLEED = 2;

function rTraceStar(g, cx, cy, pts, rOuter, rInner, rot) {
  g.beginPath();
  for (let i = 0; i < pts * 2; i++) {
    const r = i % 2 === 0 ? rOuter : rInner;
    const a = rot + (i * Math.PI) / pts;
    const px = cx + Math.cos(a) * r, py = cy + Math.sin(a) * r;
    i ? g.lineTo(px, py) : g.moveTo(px, py);
  }
  g.closePath();
}

function rTraceSmoothPoly(g, pts, scale = 1, cx = 0, cy = 0) {
  const sp = pts.map(([x, y]) => [cx + (x - cx) * scale, cy + (y - cy) * scale]);
  const n = sp.length;
  g.beginPath();
  g.moveTo((sp[n - 1][0] + sp[0][0]) / 2, (sp[n - 1][1] + sp[0][1]) / 2);
  for (let i = 0; i < n; i++) {
    const p = sp[i], q = sp[(i + 1) % n];
    g.quadraticCurveTo(p[0], p[1], (p[0] + q[0]) / 2, (p[1] + q[1]) / 2);
  }
  g.closePath();
}

RACE.tile = function (tx, ty) {
  const key = tx + "," + ty;
  let c = RACE.tiles.get(key);
  if (c) { RACE.tiles.delete(key); RACE.tiles.set(key, c); return c; }
  c = document.createElement("canvas");
  const S0 = RACE.TILE, tz = RACE.tz, B = RACE.BLEED;  // tiles are drawn at screen scale: blits are 1:1
  c.width = c.height = Math.round(S0 * tz) + 2 * B;    // plus a margin: rotated tiles overlap (no seams)
  const g = c.getContext("2d"), T = RACE.T, bm = RACE.biome, pal = BIOMES[bm];
  g.setTransform(tz, 0, 0, tz, -tx * S0 * tz + B, -ty * S0 * tz + B);
  const wx0 = tx * S0 - B / tz, wy0 = ty * S0 - B / tz, wx1 = (tx + 1) * S0 + B / tz, wy1 = (ty + 1) * S0 + B / tz;
  const inBox = (x0, y0, x1, y1, m = 0) => x1 + m >= wx0 && x0 - m <= wx1 && y1 + m >= wy0 && y0 - m <= wy1;
  const rng = rMulberry(((tx * 7919) ^ (ty * 104729) ^ ((bm + 1) * 31337)) | 0);

  // 1. Biome Ground Background + static off-track terrain props
  g.fillStyle = bm === 4 ? pal.bgSea : pal.bg;
  g.fillRect(wx0, wy0, wx1 - wx0, wy1 - wy0);

  if (bm === 0) {
    g.fillStyle = "rgba(160,170,200,0.035)";
    for (let k = 0; k < 700; k++) g.fillRect(tx * S0 + rng() * S0, ty * S0 + rng() * S0, 1.5, 1.5);
    for (const b of T.buildings) {
      if (!inBox(b.x0, b.y0, b.x1, b.y1, 40)) continue;
      g.fillStyle = "#151821"; g.fillRect(b.x0 - 26, b.y0 - 26, b.x1 - b.x0 + 52, b.y1 - b.y0 + 52);
      g.fillStyle = "#06070b"; g.fillRect(b.x0 + 10, b.y0 + 10, b.x1 - b.x0, b.y1 - b.y0);
    }
  } else if (bm === 1) {
    // Solder mask micro-grid + 45-degree copper trace buses + ENIG gold vias + IC packages
    g.strokeStyle = "rgba(14, 75, 62, 0.35)"; g.lineWidth = 1;
    g.beginPath();
    for (let gx = Math.floor(wx0 / 64) * 64; gx <= wx1; gx += 64) { g.moveTo(gx, wy0); g.lineTo(gx, wy1); }
    for (let gy = Math.floor(wy0 / 64) * 64; gy <= wy1; gy += 64) { g.moveTo(wx0, gy); g.lineTo(wx1, gy); }
    g.stroke();
    g.strokeStyle = "#0E4B3E"; g.lineWidth = 3; g.lineCap = "round"; g.lineJoin = "round";
    for (const bus of T.buses) {
      if (!inBox(bus.box[0], bus.box[1], bus.box[2], bus.box[3], 20)) continue;
      for (let ln = 0; ln < bus.lines; ln++) {
        const ox = bus.horizFirst ? 0 : (ln - (bus.lines - 1) / 2) * bus.step;
        const oy = bus.horizFirst ? (ln - (bus.lines - 1) / 2) * bus.step : 0;
        g.beginPath();
        bus.pts.forEach((p, idx) => (idx ? g.lineTo(p[0] + ox, p[1] + oy) : g.moveTo(p[0] + ox, p[1] + oy)));
        g.stroke();
      }
    }
    for (const [vx, vy, vr] of T.vias) {
      if (!inBox(vx - vr, vy - vr, vx + vr, vy + vr, 4)) continue;
      g.fillStyle = "#D4AF37"; g.beginPath(); g.arc(vx, vy, vr, 0, Math.PI * 2); g.fill();
      g.fillStyle = "#051A16"; g.beginPath(); g.arc(vx, vy, vr * 0.46, 0, Math.PI * 2); g.fill();
    }
    for (const ic of T.ics) {
      if (!inBox(ic.cx - ic.rad, ic.cy - ic.rad, ic.cx + ic.rad, ic.cy + ic.rad, 10)) continue;
      g.save(); g.translate(ic.cx, ic.cy); g.rotate(ic.rot);
      g.fillStyle = "rgba(0,0,0,0.45)"; g.fillRect(-ic.w / 2 + 6, -ic.h / 2 + 6, ic.w, ic.h);
      if (ic.qfp) {
        g.fillStyle = "#CBD5E1";
        const nx = Math.max(4, Math.floor((ic.w - 16) / 10)), ny = Math.max(4, Math.floor((ic.h - 16) / 10));
        for (let k = 0; k < nx; k++) {
          const px = -ic.w / 2 + 10 + k * ((ic.w - 20) / Math.max(1, nx - 1));
          g.fillRect(px - 2, -ic.h / 2 - 8, 4, 9); g.fillRect(px - 2, ic.h / 2 - 1, 4, 9);
        }
        for (let k = 0; k < ny; k++) {
          const py = -ic.h / 2 + 10 + k * ((ic.h - 20) / Math.max(1, ny - 1));
          g.fillRect(-ic.w / 2 - 8, py - 2, 9, 4); g.fillRect(ic.w / 2 - 1, py - 2, 9, 4);
        }
      } else {
        g.fillStyle = "#0D3B32"; g.fillRect(-ic.w / 2 - 6, -ic.h / 2 - 6, ic.w + 12, ic.h + 12);
        g.strokeStyle = "#D4AF37"; g.lineWidth = 1.5; g.strokeRect(-ic.w / 2 - 6, -ic.h / 2 - 6, ic.w + 12, ic.h + 12);
      }
      g.fillStyle = "#11161C"; g.fillRect(-ic.w / 2, -ic.h / 2, ic.w, ic.h);
      g.strokeStyle = "#2E3A48"; g.lineWidth = 2; g.strokeRect(-ic.w / 2 + 2, -ic.h / 2 + 2, ic.w - 4, ic.h - 4);
      g.fillStyle = "#D4AF37"; g.beginPath(); g.arc(-ic.w / 2 + 11, -ic.h / 2 + 11, 3, 0, Math.PI * 2); g.fill();
      g.fillStyle = "rgba(226,232,240,0.75)"; g.font = "700 13px 'Chakra Petch', monospace";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(ic.tag, 0, 0);
      g.restore();
    }
  } else if (bm === 2) {
    // Desert sand ripple texture + stepped canyon mesas + scrub dots + RF parabolic ground stations
    g.fillStyle = "rgba(244,211,158,0.09)";
    for (let k = 0; k < 420; k++) g.fillRect(tx * S0 + rng() * S0, ty * S0 + rng() * S0, 5 + rng() * 8, 1.5);
    const mesaCols = ["#A0522D", "#BF7144", "#D99B66"];
    for (const m of T.mesas) {
      if (!inBox(m.cx - m.r, m.cy - m.r, m.cx + m.r, m.cy + m.r, 12)) continue;
      g.fillStyle = "rgba(70,30,15,0.28)";
      rTraceSmoothPoly(g, m.pts, 1.03, m.cx - 8, m.cy - 8); g.fill();
      [1.0, 0.72, 0.44].forEach((sc, idx) => {
        g.fillStyle = mesaCols[idx];
        rTraceSmoothPoly(g, m.pts, sc, m.cx, m.cy); g.fill();
      });
    }
    for (const sc of T.scrubs) {
      if (!inBox(sc.x - 24, sc.y - 24, sc.x + 24, sc.y + 24, 4)) continue;
      const sr = rMulberry(Math.floor(sc.seed * 997) + 1);
      for (let j = 0; j < sc.n; j++) {
        const dx = (sr() - 0.5) * sc.r * 1.4, dy = (sr() - 0.5) * sc.r * 1.4, rr = 3.5 + sr() * 5.5;
        g.fillStyle = "rgba(45,35,20,0.25)"; g.beginPath(); g.arc(sc.x + dx + 2, sc.y + dy + 2, rr, 0, Math.PI * 2); g.fill();
        g.fillStyle = "#5B633B"; g.beginPath(); g.arc(sc.x + dx, sc.y + dy, rr, 0, Math.PI * 2); g.fill();
      }
    }
    for (const st of T.dishes) {
      if (!inBox(st.cx - st.r, st.cy - st.r, st.cx + st.r, st.cy + st.r, 24)) continue;
      for (const d of st.arr) {
        g.save(); g.translate(d.x, d.y);
        g.fillStyle = "rgba(50,25,12,0.28)"; g.beginPath(); g.arc(5, 5, d.r, 0, Math.PI * 2); g.fill();
        g.fillStyle = "#F1F5F9"; g.beginPath(); g.arc(0, 0, d.r, 0, Math.PI * 2); g.fill();
        g.strokeStyle = "#CBD5E1"; g.lineWidth = 2.5; g.beginPath(); g.arc(0, 0, d.r - 3, 0, Math.PI * 2); g.stroke();
        g.fillStyle = "rgba(148,163,184,0.32)"; g.beginPath(); g.arc(0, 0, d.r * 0.68, d.aim - 0.8, d.aim + 1.1); g.lineTo(0, 0); g.closePath(); g.fill();
        const fx = Math.cos(d.aim) * d.r * 0.25, fy = Math.sin(d.aim) * d.r * 0.25;
        g.strokeStyle = "#64748B"; g.lineWidth = 1.6;
        for (let k = 0; k < 3; k++) {
          const sa = d.aim + (k * Math.PI * 2) / 3;
          g.beginPath(); g.moveTo(Math.cos(sa) * d.r * 0.88, Math.sin(sa) * d.r * 0.88); g.lineTo(fx, fy); g.stroke();
        }
        g.fillStyle = "#FF6B35"; g.beginPath(); g.arc(fx, fy, 3.6, 0, Math.PI * 2); g.fill();
        g.restore();
      }
    }
  } else if (bm === 3) {
    // Stepped alpine slate cliff shadows & snow crests + off-track ice patches + star-shaped pine clusters
    for (const cl of T.cliffs) {
      if (!inBox(cl.cx - cl.r, cl.cy - cl.r, cl.cx + cl.r, cl.cy + cl.r, 14)) continue;
      g.save(); g.translate(6, 6);
      g.fillStyle = "rgba(75,100,130,0.22)";
      rTraceSmoothPoly(g, cl.pts, 1.0, cl.cx, cl.cy); g.fill();
      g.restore();
      g.fillStyle = "rgba(110,138,168,0.26)";
      rTraceSmoothPoly(g, cl.pts, 0.86, cl.cx, cl.cy); g.fill();
      g.save(); g.translate(-5, -5);
      g.fillStyle = "rgba(248,250,252,0.65)";
      rTraceSmoothPoly(g, cl.pts, 0.68, cl.cx, cl.cy); g.fill();
      g.restore();
    }
    for (const p of T.ice) {
      if (p.onTrack || !inBox(p.x - p.rx, p.y - p.rx, p.x + p.rx, p.y + p.rx, 8)) continue;
      g.save(); g.translate(p.x, p.y); g.rotate(p.a);
      g.save(); g.scale(1, p.ry / p.rx);
      g.fillStyle = "rgba(147,197,253,0.42)"; g.beginPath(); g.arc(0, 0, p.rx, 0, Math.PI * 2); g.fill();
      g.restore();
      g.strokeStyle = "rgba(255,255,255,0.75)"; g.lineWidth = 1.4;
      for (const f of p.fr) {
        g.beginPath(); g.moveTo(f[0][0], f[0][1]); g.lineTo(f[1][0], f[1][1]); g.lineTo(f[2][0], f[2][1]); g.stroke();
      }
      g.restore();
    }
    for (const cl of T.pines) {
      if (!inBox(cl.cx - cl.r, cl.cy - cl.r, cl.cx + cl.r, cl.cy + cl.r, 12)) continue;
      for (const [px, py, pr, rot] of cl.trees) {
        g.fillStyle = "rgba(30,50,70,0.18)";
        rTraceStar(g, px + 4, py + 5, 7, pr, pr * 0.44, rot); g.fill();
        g.fillStyle = "#1B3B36";
        rTraceStar(g, px, py, 7, pr, pr * 0.44, rot); g.fill();
        g.fillStyle = "#F8FAFC";
        rTraceStar(g, px - 1, py - 1, 5, pr * 0.56, pr * 0.25, rot + 0.2); g.fill();
      }
    }
  } else if (bm === 4) {
    // Inner warm sand beach bounded by T.coast, then ocean foam arcs and boardwalk piers on top
    g.beginPath();
    T.coast.forEach((p, idx) => (idx ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])));
    g.closePath();
    g.strokeStyle = "rgba(255,255,255,0.35)"; g.lineWidth = 16; g.stroke();
    g.strokeStyle = "#D4A373"; g.lineWidth = 8; g.stroke();
    g.fillStyle = pal.bgSand; g.fill();
    g.fillStyle = "rgba(255,245,220,0.12)";
    for (let k = 0; k < 240; k++) g.fillRect(tx * S0 + rng() * S0, ty * S0 + rng() * S0, 2, 2);
    for (const fm of T.foam) {
      if (!inBox(fm.x - fm.len, fm.y - fm.len, fm.x + fm.len, fm.y + fm.len, 30)) continue;
      g.save(); g.translate(fm.x, fm.y); g.rotate(fm.a);
      g.strokeStyle = "rgba(255,255,255,0.35)"; g.lineWidth = 3.2; g.lineCap = "round";
      g.beginPath(); g.arc(-fm.len * 0.85, 0, fm.len, -0.45, 0.45); g.stroke();
      g.lineWidth = 1.8; g.beginPath(); g.arc(-fm.len * 0.85, 0, fm.len + 16, -0.35, 0.35); g.stroke();
      g.restore();
    }
    for (const pr of T.piers) {
      if (!inBox(pr.x - pr.len, pr.y - pr.len, pr.x + pr.len, pr.y + pr.len, 24)) continue;
      g.save(); g.translate(pr.x, pr.y); g.rotate(pr.a);
      g.fillStyle = "rgba(0,20,30,0.35)"; g.fillRect(4, -pr.w / 2 + 5, pr.len, pr.w);
      g.fillStyle = "#8D5B3E"; g.fillRect(0, -pr.w / 2, pr.len, pr.w);
      g.strokeStyle = "#5C3A24"; g.lineWidth = 1.5;
      for (let x = 10; x < pr.len; x += 12) { g.beginPath(); g.moveTo(x, -pr.w / 2); g.lineTo(x, pr.w / 2); g.stroke(); }
      g.fillStyle = "#3F2616";
      for (let x = 22; x < pr.len; x += 36) {
        g.beginPath(); g.arc(x, -pr.w / 2 - 2, 3.5, 0, Math.PI * 2); g.arc(x, pr.w / 2 + 2, 3.5, 0, Math.PI * 2); g.fill();
      }
      g.restore();
    }
    for (const st of T.stacks) {
      if (!inBox(st.x - st.r, st.y - st.r, st.x + st.r, st.y + st.r, 12)) continue;
      g.save(); g.translate(st.x, st.y); g.rotate(st.a);
      g.fillStyle = "#64748B"; g.fillRect(-40, -30, 80, 60);
      for (const b of st.boxes) {
        g.fillStyle = "rgba(0,0,0,0.28)"; g.fillRect(b.ox + 2, b.oy + 2, 28, 12);
        g.fillStyle = b.col; g.fillRect(b.ox, b.oy, 28, 12);
        g.strokeStyle = "rgba(0,0,0,0.25)"; g.lineWidth = 1; g.strokeRect(b.ox, b.oy, 28, 12);
      }
      g.restore();
    }
    for (const pm of T.palms) {
      if (!inBox(pm.x - pm.r, pm.y - pm.r, pm.x + pm.r, pm.y + pm.r, 10)) continue;
      g.save(); g.translate(pm.x, pm.y);
      g.fillStyle = "rgba(20,40,30,0.18)"; g.beginPath(); g.arc(5, 6, pm.r * 0.65, 0, Math.PI * 2); g.fill();
      g.fillStyle = "#14362D";
      for (let f = 0; f < 5; f++) {
        const a = pm.rot + (f * Math.PI * 2) / 5;
        g.save(); g.rotate(a);
        g.beginPath(); g.moveTo(0, 0); g.quadraticCurveTo(pm.r * 0.55, -pm.r * 0.25, pm.r, 0);
        g.quadraticCurveTo(pm.r * 0.55, pm.r * 0.22, 0, 0); g.fill();
        g.restore();
      }
      g.fillStyle = "#4A3520"; g.beginPath(); g.arc(0, 0, 4, 0, Math.PI * 2); g.fill();
      g.restore();
    }
  }

  // 2. Track Run-off, Asphalt/Surface, Kerbs & Markings (identical collision bounds)
  const path = () => { g.beginPath(); g.moveTo(T.x[0], T.y[0]); for (let i = 1; i < T.M; i++) g.lineTo(T.x[i], T.y[i]); g.closePath(); };
  const edge = (side, off) => { g.beginPath(); for (let i = 0; i <= T.M; i++) { const j = i % T.M, [px, py] = RACE.at(T, j, side * off); i ? g.lineTo(px, py) : g.moveTo(px, py); } };
  g.lineJoin = "round"; g.lineCap = "round";
  path(); g.strokeStyle = pal.runoff; g.lineWidth = 2 * (R_TW + R_RUN); g.stroke();
  path(); g.strokeStyle = pal.surface; g.lineWidth = 2 * R_TW; g.stroke();
  if (bm === 0) { path(); g.strokeStyle = "rgba(120,140,190,0.06)"; g.lineWidth = 2 * R_TW - 40; g.stroke(); }

  // PCB SMD passives on shoulder
  if (bm === 1) {
    for (const s of T.smds) {
      if (!inBox(s.x - 16, s.y - 16, s.x + 16, s.y + 16, 4)) continue;
      g.save(); g.translate(s.x, s.y); g.rotate(s.a);
      g.fillStyle = "#DCE4EE"; g.fillRect(-9, -4, 4, 8); g.fillRect(5, -4, 4, 8);
      g.fillStyle = s.cap ? "#B98B56" : "#1D2430"; g.fillRect(-5, -3.5, 10, 7);
      g.restore();
    }
  }
  // Desert cracked-earth tarmac edges
  if (bm === 2) {
    g.strokeStyle = "rgba(42, 30, 24, 0.72)"; g.lineWidth = 2.0; g.lineCap = "round"; g.lineJoin = "round";
    for (const c of T.cracks) {
      const segs = c.segs;
      if (!inBox(segs[0][0] - 60, segs[0][1] - 60, segs[0][0] + 60, segs[0][1] + 60, 12)) continue;
      g.beginPath();
      segs.forEach((p, idx) => (idx ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])));
      g.stroke();
      if (c.twig) {
        g.lineWidth = 1.3;
        g.beginPath();
        c.twig.forEach((p, idx) => (idx ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1])));
        g.stroke();
        g.lineWidth = 2.0;
      }
    }
  }
  // Carrier Peak translucent ice patches on track with jagged fracture lines
  if (bm === 3) {
    for (const p of T.ice) {
      if (!p.onTrack || !inBox(p.x - p.rx, p.y - p.rx, p.x + p.rx, p.y + p.rx, 8)) continue;
      g.save(); g.translate(p.x, p.y); g.rotate(p.a);
      g.save(); g.scale(1, p.ry / p.rx);
      g.fillStyle = "rgba(147,197,253,0.22)"; g.beginPath(); g.arc(0, 0, p.rx, 0, Math.PI * 2); g.fill();
      g.restore();
      g.strokeStyle = "rgba(255,255,255,0.75)"; g.lineWidth = 1.4;
      for (const f of p.fr) {
        g.beginPath(); g.moveTo(f[0][0], f[0][1]); g.lineTo(f[1][0], f[1][1]); g.lineTo(f[2][0], f[2][1]); g.stroke();
      }
      g.restore();
    }
  }

  // Kerbs & edge stripes
  for (const side of [-1, 1]) {
    edge(side, R_TW + 5); g.lineWidth = 10; g.strokeStyle = pal.runoff; g.setLineDash([]); g.stroke();
    edge(side, R_TW + 5); g.strokeStyle = pal.markings; g.setLineDash([22, 22]); g.stroke();
    edge(side, R_TW - 3); g.lineWidth = 2.5; g.strokeStyle = bm === 1 ? "rgba(212,175,55,0.65)" : "rgba(235,235,245,0.5)"; g.setLineDash([]); g.stroke();
  }
  g.setLineDash([26, 34]); path(); g.lineWidth = 3; g.strokeStyle = pal.markings; g.globalAlpha = bm === 0 ? 0.55 : 0.38; g.stroke(); g.globalAlpha = 1; g.setLineDash([]);

  if (bm === 0) {
    for (const [px, py, rx, ry, a] of T.puddles) {
      if (!inBox(px - rx, py - rx, px + rx, py + rx, 6)) continue;
      g.save(); g.translate(px, py); g.rotate(a);
      const gr = g.createRadialGradient(0, 0, 2, 0, 0, rx);
      gr.addColorStop(0, "rgba(90,110,160,0.22)"); gr.addColorStop(1, "rgba(90,110,160,0)");
      g.fillStyle = gr; g.scale(1, ry / rx); g.beginPath(); g.arc(0, 0, rx, 0, Math.PI * 2); g.fill(); g.restore();
    }
  }

  // Start / finish: chequered band
  const sx = T.x[0], sy = T.y[0], ang = Math.atan2(T.ty[0], T.tx[0]);
  if (inBox(sx - R_TW, sy - R_TW, sx + R_TW, sy + R_TW, 40)) {
    g.save(); g.translate(sx, sy); g.rotate(ang);
    for (let r = -R_TW; r < R_TW; r += 14) for (let col = 0; col < 3; col++) {
      g.fillStyle = ((r / 14 + col) & 1) ? "#e9edf5" : "#141720"; g.fillRect(-21 + col * 14, r, 14, 14);
    }
    g.restore();
  }
  // Grid boxes
  for (let k = 0; k < 4; k++) {
    const [gx, gy, ga] = RACE.gridSlot(k);
    if (!inBox(gx - 36, gy - 36, gx + 36, gy + 36, 4)) continue;
    g.save(); g.translate(gx, gy); g.rotate(ga);
    g.strokeStyle = "rgba(230,235,245,0.4)"; g.lineWidth = 2; g.strokeRect(-26, -16, 52, 32); g.restore();
  }

  // Outer rails (exact wall collision boundary)
  for (const [side, col] of [[-1, pal.railL], [1, pal.railR]]) {
    edge(side, R_TW + R_RUN);
    g.lineWidth = 9; g.strokeStyle = col; g.globalAlpha = 0.20; g.stroke();
    g.lineWidth = 4.2; g.globalAlpha = 0.65; g.stroke();
    g.lineWidth = 1.6; g.globalAlpha = 0.95; g.strokeStyle = bm === 2 ? "#F4D39E" : "#f4f7ff"; g.stroke();
  }
  g.globalAlpha = 1;

  // Carrier Peak bright orange snow-pole chevrons outside tight apexes
  if (bm === 3) {
    for (const pl of T.poles) {
      if (!inBox(pl.x - 18, pl.y - 18, pl.x + 18, pl.y + 18, 6)) continue;
      g.save(); g.translate(pl.x, pl.y); g.rotate(pl.a);
      g.fillStyle = "#1E293B"; g.fillRect(-9, -7, 18, 14);
      g.strokeStyle = "#FF6D00"; g.lineWidth = 3;
      g.beginPath(); g.moveTo(-4, -4); g.lineTo(3, 0); g.lineTo(-4, 4); g.stroke();
      g.restore();
    }
  }

  // Neon Rain City sodium light pools
  if (bm === 0) {
    g.globalCompositeOperation = "lighter";
    for (const [lx, ly] of T.lamps) {
      if (!inBox(lx - 260, ly - 260, lx + 260, ly + 260, 0)) continue;
      const gr = g.createRadialGradient(lx, ly, 4, lx, ly, 250);
      gr.addColorStop(0, "rgba(255,179,71,0.30)"); gr.addColorStop(0.35, "rgba(255,150,50,0.12)"); gr.addColorStop(1, "rgba(255,140,40,0)");
      g.fillStyle = gr; g.fillRect(lx - 250, ly - 250, 500, 500);
    }
    g.globalCompositeOperation = "source-over";
  }

  RACE.tiles.set(key, c);
  while (RACE.tiles.size > 256) RACE.tiles.delete(RACE.tiles.keys().next().value);
  return c;
};

RACE.gridSlot = function (k) {
  const T = RACE.T, back = 70 + Math.floor(k / 2) * 74, i = (T.M - Math.round(back / R_STEP)) % T.M;
  const off = (k % 2 ? 1 : -1) * 48;
  const [px, py] = RACE.at(T, i, off);
  return [px, py, Math.atan2(T.ty[i], T.tx[i]), i, -back];
};

// ====================================================================== sprites + custom per-biome car renderer
RACE.sprites = function () {
  const mk = (w, h, fn) => { const c = document.createElement("canvas"); c.width = w; c.height = h; fn(c.getContext("2d")); return c; };
  RACE.glowSpr = R_SEAT.map((col) => mk(120, 90, (g) => {
    const gr = g.createRadialGradient(60, 45, 4, 60, 45, 44);
    gr.addColorStop(0, col); gr.addColorStop(1, "rgba(0,0,0,0)");
    g.globalAlpha = 0.55; g.fillStyle = gr; g.scale(1, 0.7); g.beginPath(); g.arc(60, 64, 44, 0, Math.PI * 2); g.fill();
  }));
  RACE.beamSpr = mk(260, 120, (g) => {
    const gr = g.createLinearGradient(0, 0, 260, 0);
    gr.addColorStop(0, "rgba(255,245,220,0.34)"); gr.addColorStop(1, "rgba(255,245,220,0)");
    g.fillStyle = gr; g.beginPath(); g.moveTo(0, 52); g.lineTo(260, 0); g.lineTo(260, 120); g.lineTo(0, 68); g.fill();
  });
  RACE.dualBeamSpr = mk(260, 120, (g) => {
    const gr = g.createLinearGradient(0, 0, 260, 0);
    gr.addColorStop(0, "rgba(255,248,235,0.36)"); gr.addColorStop(1, "rgba(255,248,235,0)");
    g.fillStyle = gr;
    const y1 = 60 - (R_CAR.wid / 2 - 5), y2 = 60 + (R_CAR.wid / 2 - 5);
    g.beginPath(); g.moveTo(0, y1 - 2); g.lineTo(260, 2); g.lineTo(260, 54); g.lineTo(0, y1 + 2); g.closePath(); g.fill();
    g.beginPath(); g.moveTo(0, y2 - 2); g.lineTo(260, 66); g.lineTo(260, 118); g.lineTo(0, y2 + 2); g.closePath(); g.fill();
  });
  RACE.mistSpr = mk(64, 64, (g) => {
    const gr = g.createRadialGradient(32, 32, 2, 32, 32, 32);
    gr.addColorStop(0, "rgba(255,209,102,0.22)"); gr.addColorStop(1, "rgba(255,209,102,0)");
    g.fillStyle = gr; g.fillRect(0, 0, 64, 64);
  });
  RACE.warmBeamSpr = mk(270, 140, (g) => {
    const gr = g.createLinearGradient(0, 0, 270, 0);
    gr.addColorStop(0, "rgba(255,235,150,0.28)"); gr.addColorStop(1, "rgba(255,235,150,0)");
    g.fillStyle = gr; g.beginPath(); g.moveTo(0, 56); g.lineTo(270, 0); g.lineTo(270, 140); g.lineTo(0, 84); g.fill();
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

function drawCar(ctx, car, biomeId) {
  const L = R_CAR.len, W = R_CAR.wid, col = car.col || R_SEAT[(car.s || 0) % 4];
  const t = performance.now() / 1000, braking = !!(car.fl & R_FLAG.brake), boosting = !!(car.fl & R_FLAG.boost);
  const moving = boosting || Math.hypot(car.vx || car.dvx || 0, car.vy || car.dvy || 0) > 40;
  ctx.save();
  ctx.globalAlpha = car.gone ? 0.35 : 1;

  if (biomeId === 0) {
    // 0 — Cyber Street Coupe
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(RACE.glowSpr[car.s % 4], -60, -45);
    ctx.drawImage(RACE.dualBeamSpr, 20, -60);
    if (boosting || moving) {
      const f = boosting ? 14 + Math.random() * 14 : 6 + 2 * Math.sin(t * 24);
      ctx.save();
      ctx.globalAlpha *= boosting ? 1 : 0.75;
      ctx.fillStyle = col;
      ctx.beginPath(); ctx.moveTo(-L / 2, -6); ctx.lineTo(-L / 2 - f, -4); ctx.lineTo(-L / 2, -2); ctx.fill();
      ctx.beginPath(); ctx.moveTo(-L / 2, 2); ctx.lineTo(-L / 2 - f, 4); ctx.lineTo(-L / 2, 6); ctx.fill();
      ctx.restore();
    }
    ctx.globalCompositeOperation = "source-over";
    // Sleek wedge body
    ctx.fillStyle = "#0b0e16";
    ctx.beginPath();
    ctx.moveTo(L / 2, -W / 2 + 4); ctx.lineTo(L / 2 + 1, 0); ctx.lineTo(L / 2, W / 2 - 4);
    ctx.lineTo(-L / 2 + 2, W / 2); ctx.lineTo(-L / 2, 0); ctx.lineTo(-L / 2 + 2, -W / 2);
    ctx.closePath(); ctx.fill();
    // Accent sideblades & hood trim
    ctx.fillStyle = col;
    ctx.fillRect(-L / 2 + 5, -W / 2 + 1, L - 14, 2.6);
    ctx.fillRect(-L / 2 + 5, W / 2 - 3.6, L - 14, 2.6);
    ctx.fillRect(0, -1.5, 16, 3);
    // Smoked wrap-around windshield
    ctx.fillStyle = "#1c283d";
    ctx.beginPath(); ctx.roundRect(-5, -W / 2 + 4.5, 17, W - 9, 4); ctx.fill();
    ctx.fillStyle = "rgba(180,235,255,0.42)";
    ctx.beginPath(); ctx.moveTo(6, -W / 2 + 6); ctx.lineTo(11, -W / 2 + 7); ctx.lineTo(11, W / 2 - 7); ctx.lineTo(6, W / 2 - 6); ctx.fill();
    // Rear GT spoiler
    ctx.fillStyle = "#080a10"; ctx.fillRect(-L / 2 - 1, -W / 2, 5, W);
    ctx.fillStyle = col; ctx.fillRect(-L / 2 - 1, -W / 2, 5, 2.5); ctx.fillRect(-L / 2 - 1, W / 2 - 2.5, 5, 2.5);
    // Twin neon exhaust jets & bright white headlights
    ctx.fillStyle = "#f8fcff"; ctx.fillRect(L / 2 - 3, -W / 2 + 4, 3, 4); ctx.fillRect(L / 2 - 3, W / 2 - 8, 3, 4);
    ctx.fillStyle = braking ? "#ff2a44" : "#8a1c28";
    ctx.fillRect(-L / 2 + 2, -W / 2 + 4, 2.5, 4); ctx.fillRect(-L / 2 + 2, W / 2 - 8, 2.5, 4);
  } else if (biomeId === 1) {
    // 1 — Signal Hover-Pod (IC Carrier)
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(RACE.glowSpr[car.s % 4], -56, -42, 112, 84);
    ctx.drawImage(RACE.beamSpr, 18, -50, 210, 100);
    ctx.globalCompositeOperation = "source-over";
    // 4 gold copper edge pins per side
    ctx.fillStyle = "#D4AF37";
    for (let k = 0; k < 4; k++) {
      const px = -12 + k * 8;
      ctx.fillRect(px - 2, -W / 2, 4, 3.5);
      ctx.fillRect(px - 2, W / 2 - 3.5, 4, 3.5);
    }
    // Hexagonal matte-black IC chassis
    ctx.fillStyle = "#10151B";
    ctx.beginPath();
    ctx.moveTo(L / 2, 0); ctx.lineTo(L / 2 - 9, -W / 2 + 2.5); ctx.lineTo(-L / 2 + 8, -W / 2 + 2.5);
    ctx.lineTo(-L / 2, 0); ctx.lineTo(-L / 2 + 8, W / 2 - 2.5); ctx.lineTo(L / 2 - 9, W / 2 - 2.5);
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.stroke();
    // Internal PCB silkscreen trace
    ctx.strokeStyle = "rgba(212,175,55,0.65)"; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(-14, -4); ctx.lineTo(-4, -4); ctx.lineTo(4, 0); ctx.lineTo(14, 0); ctx.stroke();
    // Pulsing central LED telemetry dot
    const pulse = 0.55 + 0.45 * Math.sin(t * 9 + car.s);
    ctx.fillStyle = col; ctx.globalAlpha *= pulse;
    ctx.beginPath(); ctx.arc(0, 0, 4.2, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = car.gone ? 0.35 : 1;
    ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(0, 0, 1.6, 0, Math.PI * 2); ctx.fill();
    if (braking) { ctx.fillStyle = "#ff3348"; ctx.fillRect(-L / 2 + 2, -4, 3, 8); }
  } else if (biomeId === 2) {
    // 2 — Baja Dune Buggy
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(RACE.warmBeamSpr, 7, -70, 250, 140);
    ctx.globalCompositeOperation = "source-over";
    // Suspension struts & 4 oversized exposed wheel arches
    ctx.strokeStyle = "#5C534E"; ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(13, -W / 2 + 1); ctx.lineTo(13, W / 2 - 1);
    ctx.moveTo(-13, -W / 2 + 1); ctx.lineTo(-13, W / 2 - 1);
    ctx.stroke();
    for (const wx of [-15, 12]) for (const wy of [-W / 2, W / 2 - 5]) {
      ctx.fillStyle = "#181514";
      ctx.beginPath(); ctx.roundRect(wx - 4, wy, 11, 5, 2); ctx.fill();
      ctx.fillStyle = "#2b2725";
      for (let tr = -2; tr <= 6; tr += 4) ctx.fillRect(wx + tr, wy + 0.5, 1.5, 4);
    }
    // Narrow central fuselage
    ctx.fillStyle = "#231F1D";
    ctx.beginPath(); ctx.roundRect(-L / 2 + 3, -7, L - 6, 14, 5); ctx.fill();
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.roundRect(-L / 2 + 6, -5.5, L - 13, 11, 4); ctx.fill();
    // Cross-braced tubular roll cage
    ctx.strokeStyle = "#F1F5F9"; ctx.lineWidth = 1.6;
    ctx.strokeRect(-10, -5, 18, 10);
    ctx.beginPath(); ctx.moveTo(-10, -5); ctx.lineTo(8, 5); ctx.moveTo(-10, 5); ctx.lineTo(8, -5); ctx.stroke();
    // Twin warm-amber roof light bar
    ctx.fillStyle = "#111"; ctx.fillRect(6, -6, 3, 12);
    ctx.fillStyle = "#FFB703"; ctx.fillRect(7, -5, 2.5, 4); ctx.fillRect(7, 1, 2.5, 4);
    if (braking) { ctx.fillStyle = "#ff3348"; ctx.fillRect(-L / 2 + 3, -4, 2.5, 8); }
  } else if (biomeId === 3) {
    // 3 — Group-B Snow Rally Hatch
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(RACE.warmBeamSpr, 20, -70);
    ctx.globalCompositeOperation = "source-over";
    // Tire edges peeking outside wheel arches & red mudflaps at rear corners
    ctx.fillStyle = "#18181b";
    ctx.fillRect(9, -W / 2 - 1.2, 9, 3); ctx.fillRect(9, W / 2 - 1.8, 9, 3);
    ctx.fillRect(-16, -W / 2 - 1.2, 9, 3); ctx.fillRect(-16, W / 2 - 1.8, 9, 3);
    ctx.fillStyle = "#EF233C";
    ctx.fillRect(-L / 2 - 2, -W / 2 + 2, 4, 5); ctx.fillRect(-L / 2 - 2, W / 2 - 7, 4, 5);
    // Boxy flared-fender rally hatch
    ctx.fillStyle = "#F1F5F9";
    ctx.beginPath(); ctx.roundRect(-L / 2 + 1, -W / 2 + 1, L - 2, W - 2, 4); ctx.fill();
    // Rear hatch spoiler lip & flared wheel arches
    ctx.fillStyle = "#1E293B"; ctx.fillRect(-L / 2 + 1, -W / 2 + 2, 3, W - 4);
    ctx.fillStyle = col;
    ctx.fillRect(8, -W / 2, 11, 3); ctx.fillRect(8, W / 2 - 3, 11, 3);
    ctx.fillRect(-17, -W / 2, 11, 3); ctx.fillRect(-17, W / 2 - 3, 11, 3);
    // Rally stripe
    ctx.fillRect(-L / 2 + 4, -3, L - 7, 6);
    // Dark windshield + roof intake scoop
    ctx.fillStyle = "#1E293B"; ctx.fillRect(2, -W / 2 + 4, 10, W - 8);
    ctx.fillStyle = "#334155"; ctx.fillRect(-8, -4, 7, 8);
    ctx.strokeStyle = "#0f172a"; ctx.lineWidth = 1; ctx.strokeRect(-8, -4, 7, 8);
    // Front 4-lamp rally pod
    ctx.fillStyle = "#FFEB96";
    for (let k = 0; k < 4; k++) ctx.fillRect(L / 2 - 2, -7 + k * 4, 3, 2.6);
    ctx.fillStyle = braking ? "#ff2a44" : "#991b1b";
    ctx.fillRect(-L / 2 + 1, -W / 2 + 3, 2.5, 4); ctx.fillRect(-L / 2 + 1, W / 2 - 7, 2.5, 4);
  } else {
    // 4 — Open-Top GT Roadster
    ctx.globalCompositeOperation = "lighter";
    ctx.drawImage(RACE.warmBeamSpr, 18, -66, 220, 132);
    ctx.globalCompositeOperation = "source-over";
    // Rounded retro-modern body & fenders
    ctx.fillStyle = col;
    ctx.beginPath(); ctx.roundRect(-L / 2 + 1, -W / 2 + 1, L - 2, W - 2, 10); ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,0.35)"; ctx.fillRect(-L / 2 + 4, -2, L - 8, 4);
    // Open cockpit
    ctx.fillStyle = "#181822";
    ctx.beginPath(); ctx.roundRect(-12, -W / 2 + 4, 19, W - 8, 6); ctx.fill();
    // Curved dark windscreen
    ctx.strokeStyle = "rgba(180,235,255,0.7)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(4, 0, 8, -Math.PI * 0.42, Math.PI * 0.42); ctx.stroke();
    // Small player-colored driver helmet circle
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(-4, 0, 4.2, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#111"; ctx.fillRect(-2, -2.5, 2.5, 5);
    // Warm golden headlights
    ctx.fillStyle = "#FFD166";
    ctx.beginPath(); ctx.arc(L / 2 - 3, -W / 2 + 5, 2.4, 0, Math.PI * 2); ctx.arc(L / 2 - 3, W / 2 - 5, 2.4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = braking ? "#ff2a44" : "#8a1c28";
    ctx.fillRect(-L / 2 + 2, -W / 2 + 4, 2.5, 4); ctx.fillRect(-L / 2 + 2, W / 2 - 8, 2.5, 4);
  }

  // Shared brake-glow halo
  if (braking) {
    ctx.globalCompositeOperation = "lighter";
    ctx.fillStyle = "rgba(255,40,60,0.45)";
    ctx.fillRect(-L / 2 - 12, -9, 12, 18);
    ctx.globalCompositeOperation = "source-over";
  }

  // 64-QAM Overdrive: brief 64-dot constellation spark trail behind car
  if (boosting) {
    ctx.globalCompositeOperation = "lighter";
    for (let row = 0; row < 8; row++) {
      for (let colIdx = 0; colIdx < 8; colIdx++) {
        const jx = (Math.sin(t * 28 + row * 3 + colIdx) * 0.9);
        const jy = (Math.cos(t * 24 + colIdx * 5 + row) * 0.9);
        const px = -L / 2 - 8 - colIdx * 5.2 + jx;
        const py = (row - 3.5) * 3.1 + jy;
        ctx.fillStyle = (row + colIdx) & 1 ? "rgba(63,230,255,0.85)" : "rgba(255,179,71,0.9)";
        ctx.fillRect(px - 1, py - 1, 2, 2);
      }
    }
    ctx.globalCompositeOperation = "source-over";
  }

  // Walsh Shield: crisp cyan hexagonal orthogonal barrier
  if (car.fl & R_FLAG.shield) {
    const a = 0.65 + 0.3 * Math.sin(t * 10);
    ctx.strokeStyle = `rgba(0,229,255,${a})`;
    ctx.fillStyle = "rgba(0,229,255,0.10)";
    ctx.lineWidth = 2.6;
    ctx.beginPath();
    for (let k = 0; k < 6; k++) {
      const ang = (k * Math.PI) / 3;
      const hx = Math.cos(ang) * 34, hy = Math.sin(ang) * 26;
      k ? ctx.lineTo(hx, hy) : ctx.moveTo(hx, hy);
    }
    ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    for (let k = 0; k < 6; k++) {
      const ang = (k * Math.PI) / 3;
      ctx.moveTo(Math.cos(ang) * 30, Math.sin(ang) * 23);
      ctx.lineTo(Math.cos(ang) * 38, Math.sin(ang) * 29);
    }
    ctx.stroke();
  }

  if (car.fl & R_FLAG.stun) {
    ctx.strokeStyle = "rgba(63,230,255,0.9)"; ctx.lineWidth = 2; ctx.beginPath();
    for (let j = 0; j < 6; j++) {
      const a = Math.random() * Math.PI * 2, r1 = 18, r2 = 32;
      ctx.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); ctx.lineTo(Math.cos(a + 0.3) * r2, Math.sin(a + 0.3) * r2);
    }
    ctx.stroke();
  }
  ctx.restore();
}
RACE.drawCar = drawCar;

// ====================================================================== module interface (games.js)
const R_CAMS = ["north", "chase", "bird"];
const R_CAM_NAMES = { north: "north up", chase: "chase", bird: "bird's-eye" };
const R_CAM_SAY = { north: "North-up camera", chase: "Chase camera", bird: "Bird's-eye view" };
const R_SPEC_TIERS = [0.28, 0.46, 0.72, 1.05, 1.45];

RACE.calcFit = function (W, H) {
  const b = RACE.T?.fitBox || RACE.T?.box || [0, 0, 4800, 3400];
  const bw = Math.max(400, b[2] - b[0]), bh = Math.max(400, b[3] - b[1]);
  const vw = Math.max(120, W - 48), vh = Math.max(120, H - 104);
  return {
    scale: rClamp(Math.min(vw / bw, vh / bh), 0.14, 1.5),
    cx: (b[0] + b[2]) / 2,
    cy: (b[1] + b[3]) / 2,
  };
};

RACE.pickTileZ = function (dpr, z) {
  if (!RACE.spec) return Math.max(0.16, Math.round(RACE.TILE * dpr * z) / RACE.TILE);
  let tier = R_SPEC_TIERS[R_SPEC_TIERS.length - 1];
  for (const t of R_SPEC_TIERS) if (t >= z * 0.94) { tier = t; break; }
  return Math.round(RACE.TILE * dpr * tier) / RACE.TILE;
};

RACE.clampSpecCam = function (x, y, zoom, W, H) {
  const b = RACE.T.fitBox || RACE.T.box, fit = RACE.calcFit(W, H);
  if (zoom <= fit.scale * 1.02) return [fit.cx, fit.cy];
  const halfW = (W / 2) / zoom, halfH = (H / 2) / zoom;
  const minX = b[0] + Math.min(halfW * 0.65, (b[2] - b[0]) * 0.45);
  const maxX = b[2] - Math.min(halfW * 0.65, (b[2] - b[0]) * 0.45);
  const minY = b[1] + Math.min(halfH * 0.65, (b[3] - b[1]) * 0.45);
  const maxY = b[3] - Math.min(halfH * 0.65, (b[3] - b[1]) * 0.45);
  return [minX <= maxX ? rClamp(x, minX, maxX) : fit.cx, minY <= maxY ? rClamp(y, minY, maxY) : fit.cy];
};

RACE.specZoomAt = function (factorOrAbs, sx, sy, isAbs = false) {
  const sc = RACE.specCam, cv = RACE.cv;
  if (!sc || !cv || !RACE.T) return;
  const W = cv.clientWidth || 1200, H = cv.clientHeight || 800, fit = RACE.calcFit(W, H);
  const minZ = fit.scale, maxZ = 1.5;
  const oldZ = sc.targetZoom ?? minZ;
  const newZ = rClamp(isAbs ? factorOrAbs : oldZ * factorOrAbs, minZ, maxZ);
  if (sc.follow !== null) {
    sc.targetZoom = newZ;
    return;
  }
  const wx = sc.tx + (sx - W / 2) / oldZ, wy = sc.ty + (sy - (H / 2 + 12)) / oldZ;
  const [cx, cy] = RACE.clampSpecCam(wx - (sx - W / 2) / newZ, wy - (sy - (H / 2 + 12)) / newZ, newZ, W, H);
  sc.targetZoom = newZ; sc.tx = cx; sc.ty = cy;
};

RACE.followSeat = function (seat) {
  const sc = RACE.specCam, c = RACE.cars[seat];
  if (!sc || !c || !RACE.cv) return;
  const W = RACE.cv.clientWidth || 1200, H = RACE.cv.clientHeight || 800, fit = RACE.calcFit(W, H);
  sc.follow = seat;
  if ((sc.targetZoom ?? fit.scale) < Math.max(0.72, fit.scale * 1.45)) sc.targetZoom = rClamp(Math.max(0.82, fit.scale * 1.7), fit.scale, 1.5);
  sc.a = c.dh ?? c.h ?? 0;
  RACE.say(`Following ${c.name}`, 1.1);
};

RACE.toggleSpecChase = function () {
  const sc = RACE.specCam;
  if (!sc) return;
  if (sc.follow === null) {
    const first = RACE.order(true)[0] || RACE.cars[0];
    if (!first) return;
    RACE.followSeat(first.s);
    sc.autoChase = true;
  } else {
    sc.autoChase = !sc.autoChase;
  }
  const c = RACE.cars[sc.follow];
  RACE.say(sc.autoChase ? `Auto-Chase: ${c?.name || "car"}` : `Follow (North-Up): ${c?.name || "car"}`, 1.1);
};

RACE.resetSpecCam = function () {
  const sc = RACE.specCam, cv = RACE.cv;
  if (!sc || !cv || !RACE.T) return;
  const fit = RACE.calcFit(cv.clientWidth || 1200, cv.clientHeight || 800);
  sc.follow = null; sc.autoChase = false;
  sc.targetZoom = fit.scale; sc.tx = fit.cx; sc.ty = fit.cy;
  RACE.say("Full track · Bird's-eye", 1.0);
};

RACE.show = function (room, root) {
  RACE.room = room;
  const isSpec = Boolean(room.spectator || room.me_seat === null);
  if (RACE.rid === room.r && RACE.spec === isSpec && root.querySelector(".race-room") && RACE.round === room.n) {
    RACE.updateRoom(room);
    return;
  }
  RACE.mount(room, root);
};

RACE.resolveBiome = function (room) {
  if (Number.isInteger(room.biome) && room.biome >= 0 && room.biome < BIOMES.length) return room.biome;
  const ob = room.o?.biome;
  if (Number.isInteger(ob) && ob >= 0 && ob < BIOMES.length) return ob;
  return (((room.seed >>> 0) * 2654435761) >>> 16) % BIOMES.length;
};

RACE.mount = function (room, root) {
  RACE.unmount();
  RACE.rid = room.r; RACE.round = room.n || 1; RACE.room = room;
  RACE.spec = Boolean(room.spectator || room.me_seat === null);
  RACE.seatN = room.players.length; RACE.laps = room.o?.laps || 3;
  RACE.biome = RACE.resolveBiome(room);
  RACE.B = BIOMES[RACE.biome];
  RACE.T = RACE.buildTrack(room.seed, RACE.biome);
  RACE.tiles = new Map(); RACE.mini = null;
  if (!RACE.glowSpr) RACE.sprites();
  RACE.missiles = []; RACE.mines = []; RACE.fx = []; RACE.msgs = []; RACE.outEv = []; RACE.eid = 0; RACE.seen = new Map();
  RACE.hist = []; RACE.results = room.state === "over" ? room.results : null; RACE.keys.clear();
  RACE.cars = room.players.map((a, s) => {
    const [x, y, h, i, d] = RACE.gridSlot(s);
    return { s, a, name: GC.name(room, a), col: R_SEAT[s % 4], x, y, h, dx: x, dy: y, dh: h, dvx: 0, dvy: 0,
      vx: 0, vy: 0, w: 0, d, i, fl: 0, it: 0,
      pkt: null, err: null, rx: [], fin: room.finish?.[s] ?? null, lapT: [], prevLap: 0, wake: [], slip: 0 };
  });
  if (RACE.spec) {
    RACE.me = null;
    RACE.quiet = true;
    for (const p of room.last_cars || []) RACE.onRemote({ ...p, r: room.r, s: p.seat ?? p.s, ev: [] });
    RACE.quiet = false;
    root.innerHTML = `<div class="race-room is-spec"><canvas class="world"></canvas><div class="vignette"></div>
      <div class="hud">
        <div class="board spec-board" aria-label="Leaderboard: click a racer to follow"></div>
        <canvas class="mini" width="400" height="300"></canvas>
        <div class="speed spec-tele" hidden><span class="v">0</span><small>km/h</small></div>
        <div class="spec-bar" role="toolbar" aria-label="Spectator camera controls">
          <span class="spec-follow-lbl">Free Bird's-Eye</span>
          <button type="button" class="spec-btn" id="specZoomOut" title="Zoom out (-)">−</button>
          <button type="button" class="spec-btn" id="specFull" title="Full track (0 / F)">Full Track<kbd>F</kbd></button>
          <button type="button" class="spec-btn" id="specZoomIn" title="Zoom in (+)">+</button>
          <button type="button" class="spec-btn" id="specChase" aria-pressed="false" title="Toggle Auto-Chase / Follow (V)">Auto-Chase<kbd>V</kbd></button>
        </div>
        <div class="count"></div><div class="r-msg"></div><div class="keys"></div>
      </div>
      ${GC.bar(`<span class="spec-badge">SPECTATING</span>Slot Rush <span style="opacity:.72;font-weight:500">· ${esc(RACE.B.name)} · ${RACE.laps} laps</span>`,
        `<button class="gc-ghost" id="raceLeave">Leave</button>`)}
      <div class="race-results" hidden></div></div>`;
  } else {
    RACE.me = RACE.cars[room.me_seat];
    Object.assign(RACE.me, { item: "", boost: 0, shield: 0, stun: 0, spin: 0, spinDir: 1, throttle: 0, steer: 0,
      lapStart: 0, best: null, lapNow: 0, finT: null, off: false });
    RACE.resume(room.state === "playing" ? room.mine : null);
    root.innerHTML = `<div class="race-room"><canvas class="world"></canvas><div class="vignette"></div>
      <div class="hud">
        <div class="pos"><span class="big">–</span><span class="of"></span></div>
        <div class="lap"></div><div class="times"></div>
        <div class="board"></div>
        <canvas class="mini" width="400" height="300"></canvas>
        <div class="item"><span class="slot"></span><span class="nm"></span></div>
        <div class="speed"><span class="v">0</span><small>km/h</small></div>
        <div class="count"></div><div class="r-msg"></div><div class="keys"></div>
        <button type="button" class="rt-cam" id="rtCam" aria-label="Switch camera" title="Switch camera"></button>
        <div class="race-touch" role="region" aria-label="Race touch controls">
          <div class="rt-steer" id="rtSteer" role="group" aria-label="Steering">
            <span class="rt-steer-z rt-steer-l" aria-hidden="true">◀</span>
            <span class="rt-steer-z rt-steer-r" aria-hidden="true">▶</span>
          </div>
          <div class="rt-pedals">
            <div class="rt-aux">
              <button type="button" class="rt-auto" id="rtAuto" aria-pressed="false">Auto-Gas</button>
              <button type="button" class="rt-item empty" id="rtItem" aria-label="No pickup held" disabled><span class="rt-slot"></span><span class="rt-inm">No pickup</span></button>
            </div>
            <div class="rt-main">
              <button type="button" class="rt-pedal rt-brake" id="rtBrake" aria-label="Brake or reverse">Brake</button>
              <button type="button" class="rt-pedal rt-gas" id="rtGas" aria-label="Gas">Gas</button>
            </div>
          </div>
        </div>
      </div>
      ${GC.bar(`Slot Rush <span style="opacity:.72;font-weight:500">· ${esc(RACE.B.name)} · ${RACE.laps} laps</span>`,
        `<button class="gc-ghost" id="raceCam" title="Switch the camera (V)"></button><button class="gc-ghost" id="raceLeave">Leave race</button>`)}
      <div class="race-results" hidden></div></div>`;
  }
  RACE.root = root.querySelector(".race-room");
  RACE.clearTouchInputs(true);
  GC.wire(RACE.root);
  RACE.root.querySelector("#raceLeave").onclick = () => GC.post(
    RACE.spec ? { a: "unwatch", r: room.r } : { a: "leave", r: room.r }
  ).catch((e) => toast(e.message));
  RACE.cv = RACE.root.querySelector("canvas.world");
  RACE.ctx = RACE.cv.getContext("2d");
  RACE.miniCv = RACE.root.querySelector("canvas.mini");
  RACE.initWeather();
  if (RACE.spec) {
    RACE.hud = Object.fromEntries(["board", "speed", "count", "r-msg", "keys", "spec-follow-lbl"].map((k) => [
      k === "r-msg" ? "msg" : k === "spec-follow-lbl" ? "followLbl" : k,
      RACE.root.querySelector(".hud ." + k),
    ]));
    const W = RACE.cv.clientWidth || window.innerWidth || 1200, H = RACE.cv.clientHeight || window.innerHeight || 800;
    const fit = RACE.calcFit(W, H);
    RACE.specCam = { zoom: fit.scale, targetZoom: fit.scale, x: fit.cx, y: fit.cy, tx: fit.cx, ty: fit.cy,
      follow: null, autoChase: false, a: 0, ptrs: new Map(), drag: null, pinchD: 0 };
    RACE.cam = { x: fit.cx, y: fit.cy, z: fit.scale, a: 0 };
    RACE.wireSpecControls();
  } else {
    RACE.root.querySelector("#raceCam").onclick = (e) => { RACE.toggleCam(); e.currentTarget.blur(); };
    RACE.camLabel();
    RACE.hud = Object.fromEntries(["pos", "lap", "times", "board", "item", "speed", "count", "r-msg", "keys"].map((k) => [k.replace("r-", ""), RACE.root.querySelector(".hud ." + k)]));
    RACE.cam = { x: RACE.me.x, y: RACE.me.y, z: 1, a: RACE.me.h };
    RACE.wireTouchControls();
  }
  RACE.countSaid = null;
  RACE.last = performance.now(); RACE.acc = 0;
  GC.clocks[room.r] = GC.clocks[room.r] || { s: [], off: null };
  GC.syncClock(room.r);
  for (let k = 1; k < 5; k++) setTimeout(() => GC.syncClock(room.r), k * 120);
  RACE.syncT = setInterval(() => GC.syncClock(room.r), 2000);
  if (typeof MUSIC !== "undefined") MUSIC.setHint(RACE.B.music);
  RACE.raf = requestAnimationFrame(RACE.frame);
  if (RACE.results) RACE.showResults(RACE.results);
};

RACE.wireSpecControls = function () {
  const cv = RACE.cv, root = RACE.root;
  if (!cv || !root) return;
  root.querySelector("#specZoomOut").onclick = (e) => { e.currentTarget.blur(); RACE.specZoomAt(1 / 1.25, cv.clientWidth / 2, cv.clientHeight / 2); };
  root.querySelector("#specZoomIn").onclick = (e) => { e.currentTarget.blur(); RACE.specZoomAt(1.25, cv.clientWidth / 2, cv.clientHeight / 2); };
  root.querySelector("#specFull").onclick = (e) => { e.currentTarget.blur(); RACE.resetSpecCam(); };
  root.querySelector("#specChase").onclick = (e) => { e.currentTarget.blur(); RACE.toggleSpecChase(); };

  cv.addEventListener("wheel", (e) => {
    if (!RACE.spec || !RACE.specCam) return;
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    RACE.specZoomAt(Math.pow(1.0016, -e.deltaY), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  cv.addEventListener("pointerdown", (e) => {
    const sc = RACE.specCam;
    if (!RACE.spec || !sc || (e.pointerType !== "touch" && e.button !== 0)) return;
    sc.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try { cv.setPointerCapture(e.pointerId); } catch (_) {}
    if (sc.ptrs.size === 1) {
      sc.drag = { id: e.pointerId, startX: e.clientX, startY: e.clientY, lastX: e.clientX, lastY: e.clientY, moved: false };
    } else if (sc.ptrs.size === 2) {
      const [p0, p1] = [...sc.ptrs.values()];
      sc.pinchD = Math.hypot(p1.x - p0.x, p1.y - p0.y) || 1;
      sc.drag = null;
      cv.classList.remove("dragging");
    }
  });

  cv.addEventListener("pointermove", (e) => {
    const sc = RACE.specCam;
    if (!RACE.spec || !sc || !sc.ptrs.has(e.pointerId)) return;
    sc.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const W = cv.clientWidth || 1200, H = cv.clientHeight || 800;
    if (sc.ptrs.size === 2) {
      const [p0, p1] = [...sc.ptrs.values()];
      const d = Math.hypot(p1.x - p0.x, p1.y - p0.y) || 1;
      const r = cv.getBoundingClientRect();
      const mx = (p0.x + p1.x) / 2 - r.left, my = (p0.y + p1.y) / 2 - r.top;
      if (sc.pinchD > 0) RACE.specZoomAt(d / sc.pinchD, mx, my);
      sc.pinchD = d;
      return;
    }
    if (sc.drag && sc.drag.id === e.pointerId) {
      if (!sc.drag.moved) {
        if (Math.hypot(e.clientX - sc.drag.startX, e.clientY - sc.drag.startY) <= 4) return;
        sc.drag.moved = true;
        sc.follow = null;
        sc.autoChase = false;
        cv.classList.add("dragging");
      }
      const dx = e.clientX - sc.drag.lastX, dy = e.clientY - sc.drag.lastY;
      sc.drag.lastX = e.clientX; sc.drag.lastY = e.clientY;
      const z = sc.zoom || 1;
      const [cx, cy] = RACE.clampSpecCam(sc.tx - dx / z, sc.ty - dy / z, z, W, H);
      sc.tx = sc.x = cx; sc.ty = sc.y = cy;
    }
  });

  const endPtr = (e, allowClick) => {
    const sc = RACE.specCam;
    if (!sc) return;
    if (allowClick && sc.drag && sc.drag.id === e.pointerId && !sc.drag.moved && RACE.view?.M) {
      const r = cv.getBoundingClientRect(), dpr = RACE.dpr || 1;
      const sx = (e.clientX - r.left) * dpr, sy = (e.clientY - r.top) * dpr;
      const M = RACE.view.M, det = M[0] * M[3] - M[1] * M[2];
      if (Math.abs(det) > 1e-6) {
        const u = sx - M[4], v = sy - M[5];
        const wx = (M[3] * u - M[2] * v) / det, wy = (-M[1] * u + M[0] * v) / det;
        const rad = Math.max(54, 34 / Math.max(sc.zoom || 0.4, 0.14));
        let best = null, bd = rad;
        for (const c of RACE.cars) {
          const cx = c.dx ?? c.x, cy = c.dy ?? c.y;
          const d = Math.hypot(wx - cx, wy - cy);
          if (d < bd) { bd = d; best = c; }
        }
        if (best) RACE.followSeat(best.s);
      }
    }
    sc.ptrs.delete(e.pointerId);
    if (sc.drag && sc.drag.id === e.pointerId) { sc.drag = null; cv.classList.remove("dragging"); }
    if (sc.ptrs.size < 2) sc.pinchD = 0;
  };
  cv.addEventListener("pointerup", (e) => endPtr(e, true));
  cv.addEventListener("pointercancel", (e) => endPtr(e, false));
  cv.addEventListener("lostpointercapture", (e) => endPtr(e, false));
};

RACE.isTouch = () => Boolean(window.matchMedia && matchMedia("(hover: none) and (pointer: coarse)").matches);

RACE.overlayBlocked = () => Boolean(
  !RACE.root || !GC.open || RACE.spec || !RACE.me || RACE.me.finT !== null || RACE.results
  || (typeof GCCHAT !== "undefined" && (GCCHAT.open || ($("#gcChat") && !$("#gcChat").hidden)))
  || S.dashOpen || ($("#dash") && !$("#dash").hidden) || ($("#settings") && !$("#settings").hidden)
  || document.querySelector(".sheet")
  || ($("#lightbox") && !$("#lightbox").hidden)
  || (RACE.root.querySelector(".race-results") && !RACE.root.querySelector(".race-results").hidden)
);

RACE.clearTouchInputs = function (resetAuto = false) {
  const t = RACE.touch;
  t.left = false; t.right = false; t.gas = false; t.brake = false;
  t.steerPid = null;
  t.gasPids.clear(); t.brakePids.clear();
  if (resetAuto) t.autoGas = false;
  const root = RACE.root;
  if (!root) return;
  root.querySelector(".rt-steer-l")?.classList.remove("active");
  root.querySelector(".rt-steer-r")?.classList.remove("active");
  root.querySelector("#rtGas")?.classList.remove("active");
  root.querySelector("#rtBrake")?.classList.remove("active");
  const ab = root.querySelector("#rtAuto");
  if (ab && resetAuto) ab.setAttribute("aria-pressed", "false");
};

RACE.syncTouchOverlay = function () {
  const root = RACE.root;
  if (!root || RACE.spec) return;
  const blocked = RACE.overlayBlocked();
  const rt = root.querySelector(".race-touch");
  if (rt) rt.hidden = blocked;
  const cam = root.querySelector("#rtCam");
  if (cam) cam.hidden = blocked;
  if (blocked) RACE.clearTouchInputs(Boolean(!RACE.me || RACE.me.finT !== null || RACE.results));
};

RACE.syncTouchItem = function () {
  const root = RACE.root;
  if (!root || RACE.spec || !RACE.me) return;
  const btn = root.querySelector("#rtItem");
  if (!btn) return;
  const it = RACE.me.item || "";
  const key = `${it}:${RACE.me.finT !== null}`;
  if (btn.dataset.key === key) return;
  btn.dataset.key = key;
  btn.classList.toggle("charged", Boolean(it));
  btn.classList.toggle("empty", !it);
  btn.disabled = !it || RACE.me.finT !== null;
  btn.setAttribute("aria-label", it ? `Use pickup: ${R_ITEM_NAMES[it]}` : "No pickup held");
  const nm = it ? ({ missile: "Seeker", mine: "Mine", emp: "Jammer", shield: "Shield", boost: "Overdrive" }[it] || R_ITEM_NAMES[it]) : "No pickup";
  btn.innerHTML = `<span class="rt-slot">${it ? R_ICON[it] : ""}</span><span class="rt-inm">${esc(nm)}</span>`;
};

RACE.wireTouchControls = function () {
  const root = RACE.root;
  if (!root || RACE.spec) return;
  const steerPad = root.querySelector("#rtSteer");
  const zLeft = root.querySelector(".rt-steer-l");
  const zRight = root.querySelector(".rt-steer-r");
  const gasBtn = root.querySelector("#rtGas");
  const brakeBtn = root.querySelector("#rtBrake");
  const autoBtn = root.querySelector("#rtAuto");
  const itemBtn = root.querySelector("#rtItem");
  const camBtn = root.querySelector("#rtCam");
  const t = RACE.touch;

  const applySteer = (clientX) => {
    const r = steerPad.getBoundingClientRect();
    const left = (clientX - r.left) < r.width / 2;
    t.left = left;
    t.right = !left;
    zLeft?.classList.toggle("active", left);
    zRight?.classList.toggle("active", !left);
  };
  const releaseSteer = (pid) => {
    if (t.steerPid !== pid) return;
    t.steerPid = null;
    t.left = false;
    t.right = false;
    zLeft?.classList.remove("active");
    zRight?.classList.remove("active");
  };
  if (steerPad) {
    steerPad.addEventListener("pointerdown", (e) => {
      if (RACE.overlayBlocked()) return;
      e.preventDefault();
      try { steerPad.setPointerCapture(e.pointerId); } catch (_) {}
      t.steerPid = e.pointerId;
      applySteer(e.clientX);
    });
    steerPad.addEventListener("pointermove", (e) => {
      if (t.steerPid !== e.pointerId || RACE.overlayBlocked()) return;
      e.preventDefault();
      applySteer(e.clientX);
    });
    const endSteer = (e) => releaseSteer(e.pointerId);
    steerPad.addEventListener("pointerup", endSteer);
    steerPad.addEventListener("pointercancel", endSteer);
    steerPad.addEventListener("lostpointercapture", endSteer);
  }

  const wireHold = (btn, pids, prop) => {
    if (!btn) return;
    const sync = () => {
      const on = pids.size > 0 && !RACE.overlayBlocked();
      t[prop] = on;
      btn.classList.toggle("active", on);
    };
    btn.addEventListener("pointerdown", (e) => {
      if (RACE.overlayBlocked()) return;
      e.preventDefault();
      try { btn.setPointerCapture(e.pointerId); } catch (_) {}
      pids.add(e.pointerId);
      sync();
    });
    const up = (e) => { if (pids.delete(e.pointerId)) sync(); };
    btn.addEventListener("pointerup", up);
    btn.addEventListener("pointercancel", up);
    btn.addEventListener("lostpointercapture", up);
  };
  wireHold(gasBtn, t.gasPids, "gas");
  wireHold(brakeBtn, t.brakePids, "brake");

  if (autoBtn) {
    autoBtn.addEventListener("pointerdown", (e) => {
      if (RACE.overlayBlocked()) return;
      e.preventDefault();
      t.autoGas = !t.autoGas;
      autoBtn.setAttribute("aria-pressed", String(t.autoGas));
    });
  }
  if (itemBtn) {
    itemBtn.addEventListener("pointerdown", (e) => {
      if (RACE.overlayBlocked()) return;
      e.preventDefault();
      RACE.useItem();
      RACE.syncTouchItem();
    });
  }
  if (camBtn) {
    camBtn.addEventListener("click", (e) => {
      e.currentTarget.blur();
      RACE.toggleCam();
    });
  }
  RACE.syncTouchItem();
  RACE.syncTouchOverlay();
};

// back after a page reload mid-race: carry on from the state this station last posted (the hub keeps
// it), instead of a car back on the grid that has to drive its laps again
RACE.resume = function (m) {
  const c = RACE.me, T = RACE.T, ok = (v) => typeof v === "number" && Number.isFinite(v);
  if (m && ["x", "y", "h", "vx", "vy", "d"].every((k) => ok(m[k]))) {
    Object.assign(c, { x: m.x, y: m.y, h: m.h, vx: m.vx, vy: m.vy, ws: ok(m.w) ? m.w : 0, d: m.d,
      fl: ok(m.fl) ? m.fl : 0, it: ok(m.it) ? m.it & 7 : 0 });
    c.item = R_ITEMS[c.it] || "";
    c.i = RACE.nearest(T, c.x, c.y, Math.round((((c.d % T.L) + T.L) % T.L) / R_STEP) % T.M);
    c.prevLap = Math.max(0, Math.floor(c.d / T.L));
    if (ok(m.ls)) c.lapStart = m.ls / 1000;
    if (ok(m.bl) && m.bl >= 0) c.best = m.bl / 1000;
    if (ok(m.ei)) RACE.eid = m.ei & 0xff;           // (the others drop event ids they have just seen)
  }
  if (c.fin !== null) c.finT = c.fin / 1000;
};

RACE.unmount = function () {
  cancelAnimationFrame(RACE.raf); clearInterval(RACE.syncT);
  RACE.clearTouchInputs(true);
  SFX.engineStop();
  if (typeof MUSIC !== "undefined") MUSIC.setHint(null);
  RACE.rid = null; RACE.root = null; RACE.tiles = new Map(); RACE.spec = false; RACE.specCam = null;
};

RACE.updateRoom = function (room) {
  RACE.room = room;
  const nb = RACE.resolveBiome(room);
  if (nb !== RACE.biome && RACE.T) {
    RACE.biome = nb; RACE.B = BIOMES[nb];
    RACE.buildBiomeProps(RACE.T, room.seed, nb);
    RACE.tiles = new Map(); RACE.mini = null;
    const tEl = RACE.root?.querySelector(".gc-bar .gc-title");
    if (tEl) tEl.innerHTML = `${RACE.spec ? '<span class="spec-badge">SPECTATING</span>' : ""}Slot Rush <span style="opacity:.72;font-weight:500">· ${esc(RACE.B.name)} · ${RACE.laps} laps</span>`;
    if (typeof MUSIC !== "undefined") MUSIC.setHint(RACE.B.music);
  }
  for (const c of RACE.cars) {
    const st = room.st?.[c.a];
    c.gone = st && st !== "joined";
    c.away = st === "away";                     // off the air, may come back within 60 s: a frozen ghost
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
  if (RACE.spec) {
    const sc = RACE.specCam, cv = RACE.cv;
    if (!sc || !cv) return false;
    const W = cv.clientWidth || 1200, H = cv.clientHeight || 800;
    if (k === "+" || k === "=") { RACE.specZoomAt(1.22, W / 2, H / 2); return true; }
    if (k === "-" || k === "_") { RACE.specZoomAt(1 / 1.22, W / 2, H / 2); return true; }
    if ((k === "0" || k === "f") && !e.repeat) { RACE.resetSpecCam(); return true; }
    if (k === "v" && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey) { RACE.toggleSpecChase(); return true; }
    if (["1", "2", "3", "4"].includes(k) && !e.repeat) {
      const s = Number(k) - 1;
      if (s < RACE.cars.length) RACE.followSeat(s);
      return true;
    }
    return ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "w", "a", "s", "d"].includes(k);
  }
  const map = { ArrowUp: "up", w: "up", ArrowDown: "down", s: "down", ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right", " ": "use" };
  if (k === "v" && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey) { RACE.toggleCam(); return true; }
  const act = map[k];
  if (!act) return false;
  if (e.type === "keydown" && act === "use" && !e.repeat) RACE.useItem();
  return true;
};

// camera: north up -> chase -> bird's-eye
RACE.toggleCam = function () {
  if (RACE.spec) { RACE.toggleSpecChase(); return; }
  const cur = pref("raceCam"), i = R_CAMS.indexOf(cur);
  const next = R_CAMS[(i + 1) % R_CAMS.length];
  setPref("raceCam", next);
  RACE.camLabel();
  if (typeof SETTINGS !== "undefined" && SETTINGS.built) SETTINGS.refresh();
  RACE.say(R_CAM_SAY[next], 1.1);
};
RACE.camLabel = function () {
  RACE._camPref = pref("raceCam");
  const b = RACE.root?.querySelector("#raceCam");
  if (b) b.innerHTML = `Camera: ${R_CAM_NAMES[RACE._camPref] || "north up"} <kbd>V</kbd>`;
  const rt = RACE.root?.querySelector("#rtCam");
  const shortNm = { north: "North", chase: "Chase", bird: "Bird" }[RACE._camPref] || "North";
  if (rt) rt.textContent = `Cam: ${shortNm}`;
};
// sounds only while the race is on screen
RACE.sfx = (name, opt) => { if (GC.open) SFX.play(name, opt); };

// key up/down tracking (games.js only forwards keydown)
document.addEventListener("keydown", (e) => {
  if (!RACE.root || !GC.open || !GC.keysToGame(e)) return; // typing in the chat doesn't drive the car
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const m = { ArrowUp: "up", w: "up", ArrowDown: "down", s: "down", ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right" }[k];
  if (m) RACE.keys.add(m);
});
document.addEventListener("keyup", (e) => {
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const m = { ArrowUp: "up", w: "up", ArrowDown: "down", s: "down", ArrowLeft: "left", a: "left", ArrowRight: "right", d: "right" }[k];
  if (m) RACE.keys.delete(m);
});
window.addEventListener("blur", () => { RACE.keys.clear(); RACE.clearTouchInputs(false); });
document.addEventListener("visibilitychange", () => { RACE.keys.clear(); RACE.clearTouchInputs(false); });

// ====================================================================== time
RACE.now = () => GC.now(RACE.rid);                         // host clock (s)
RACE.tg = () => RACE.now() - RACE.room.t0;                 // seconds since the green light

// ====================================================================== my car
RACE.step = function (dt) {
  const T = RACE.T, c = RACE.me, tg = RACE.tg(), P = RACE.B.phys;
  const started = tg >= 0, done = c.finT !== null;
  const blocked = RACE.overlayBlocked();
  const tBrake = !blocked && RACE.touch.brake;
  const tGas = !blocked && (RACE.touch.gas || (RACE.touch.autoGas && !tBrake));
  let up = RACE.keys.has("up") || tGas, down = RACE.keys.has("down") || tBrake;
  let steer = ((RACE.keys.has("right") || (!blocked && RACE.touch.right)) ? 1 : 0) - ((RACE.keys.has("left") || (!blocked && RACE.touch.left)) ? 1 : 0);
  if (!started || done) { up = down = false; steer = done ? steer * 0.4 : 0; }
  if (c.spin > 0) { up = down = false; steer = 0; }
  if (c.stun > 0) { up = false; steer *= 0.25; }
  const ch = Math.cos(c.h), sh = Math.sin(c.h);
  let vf = c.vx * ch + c.vy * sh, vr = -c.vx * sh + c.vy * ch;
  c.i = RACE.nearest(T, c.x, c.y, c.i);
  const lat = RACE.lateral(T, c.i, c.x, c.y), alat = Math.abs(lat);
  c.off = alat > R_TW + 4;
  const boost = c.boost > 0;
  const vmax = R_CAR.vmax * P.topSpeedMul * (boost ? 1 + 0.42 * P.boostMul : 1) * (c.off ? 0.5 : 1) * (c.stun > 0 ? 0.4 : 1);
  const acc = R_CAR.acc * P.accelMul;
  if (up) vf += acc * (boost ? 2 * P.boostMul : 1) * dt * (vf < vmax ? 1 : 0);
  if (down) vf -= (vf > 0 ? R_CAR.brake : acc * 0.6) * dt;
  if (boost && !up && vf < vmax) vf += acc * 0.8 * P.boostMul * dt;
  vf = Math.max(vf, -R_CAR.rev);
  if (vf > vmax) vf -= Math.min(vf - vmax, (c.off ? 900 : 400) * dt);
  vf -= vf * (R_CAR.drag + (c.off ? 1.3 : 0) + (up || boost ? 0 : 0.35)) * dt;
  const grip = (c.off ? 4 : 9.5) * P.lateralGrip;
  vr *= Math.exp(-grip * dt);
  c.slip = Math.abs(vr);
  const sp = Math.abs(vf);
  const yawMax = 2.7 * rClamp(sp / 240, 0, 1) * (1 - 0.38 * rClamp(sp / (R_CAR.vmax * P.topSpeedMul), 0, 1));
  const steerRate = steer === 0 ? 12 * P.driftDamp : 12;
  c.steer += (steer - c.steer) * Math.min(1, dt * steerRate);
  let w = c.steer * yawMax * (vf < 0 ? -1 : 1);
  if (c.spin > 0) { w = c.spinDir * 13; c.spin -= dt; vf *= Math.exp(-2.2 * dt); vr *= Math.exp(-2.2 * dt); }
  if (c.stun > 0) c.stun -= dt;
  c.vx = ch * vf - sh * vr; c.vy = sh * vf + ch * vr;
  c.h = rWrap(c.h + w * dt); c.w = w;
  c.ws = (c.ws || 0) + (w - (c.ws || 0)) * Math.min(1, dt / 0.15);   // smoothed yaw rate for the others' prediction
  c.x += c.vx * dt; c.y += c.vy * dt;
  // rail: push back inside and apply biome railBounce restitution (identical bounds across biomes)
  const lim = R_TW + R_RUN - R_CAR.wid / 2;
  const lat2 = RACE.lateral(T, c.i, c.x, c.y);
  if (Math.abs(lat2) > lim) {
    const nx = -T.ty[c.i] * Math.sign(lat2), ny = T.tx[c.i] * Math.sign(lat2);
    const pen = Math.abs(lat2) - lim;
    c.x -= nx * pen; c.y -= ny * pen;
    const vn = c.vx * nx + c.vy * ny;
    if (vn > 0) {
      const vtX = c.vx - vn * nx, vtY = c.vy - vn * ny;
      c.vx = vtX * 0.82 - P.railBounce * vn * nx;
      c.vy = vtY * 0.82 - P.railBounce * vn * ny;
      if (vn > 160) { RACE.spark(c.x + nx * R_CAR.wid / 2, c.y + ny * R_CAR.wid / 2, vn); RACE.sfx("wall", { gap: 0.25 }); }
    }
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
      RACE.sfx("finish"); SFX.engineStop();
    } else { RACE.say(lap === RACE.laps - 1 ? "Final lap" : `Lap ${lap + 1}`, 1.6); RACE.sfx("lap"); }
  }
  c.prevLap = Math.max(c.prevLap, lap);
  if (c.boost > 0) c.boost -= dt;
  if (c.shield > 0) c.shield -= dt;
  // boost pads and holo caches
  if (started && !done) {
    for (const p of T.pads) {
      const [px, py] = RACE.at(T, p.i, p.off);
      if (Math.hypot(c.x - px, c.y - py) < 46) { if (c.boost <= 0) RACE.sfx("boost", { gap: 0.8 }); c.boost = Math.max(c.boost, 0.9); }
    }
    for (const b of T.caches) {
      if (b.gone > performance.now()) continue;
      const [bx, by] = RACE.at(T, b.i, b.off);
      if (Math.hypot(c.x - bx, c.y - by) < 34 && !c.item) {
        b.gone = performance.now() + 3000;
        c.item = RACE.rollItem();
        RACE.say(R_ITEM_NAMES[c.item], 1.2);
        RACE.sfx("pickup");
        RACE.syncTouchItem();
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
  RACE.sfx(it);                                            // missile, mine, emp, shield, boost
  const ch = Math.cos(c.h), sh = Math.sin(c.h);
  if (it === "missile") {
    const order = RACE.order(), k = order.indexOf(c), tgt = k > 0 ? order[k - 1] : null;
    const x = c.x + ch * 30, y = c.y + sh * 30;
    const e = RACE.emit("missile", { x, y, a: Math.round(((c.h + 2 * Math.PI) % (2 * Math.PI)) / (2 * Math.PI) * 65536) & 0xffff, b: tgt ? tgt.s : 255 });
    RACE.addMissile(c.s, e);
    RACE.say(tgt ? `Carrier Seeker locked on ${tgt.name}` : "Carrier Seeker away", 1.4);
  } else if (it === "mine") {
    const e = RACE.emit("mine", { x: c.x - ch * 40, y: c.y - sh * 40 });
    RACE.addMine(c.s, e);
  } else if (it === "emp") {
    const e = RACE.emit("emp", { x: c.x, y: c.y });
    RACE.fx.push({ k: "emp", x: e.x, y: e.y, t: 0, col: c.col });
  } else if (it === "shield") {
    c.shield = 12; RACE.say("Walsh Shield up", 1.2);
  } else if (it === "boost") {
    c.boost = 1.4;
    RACE.fx.push({ k: "qam", x: c.x - ch * 26, y: c.y - sh * 26, h: c.h, t: 0 });
    RACE.say("64-QAM Overdrive", 1.0);
  }
  RACE.syncTouchItem();
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
      const tp = tgt ? (RACE.me && tgt === RACE.me ? [tgt.x, tgt.y] : [tgt.dx, tgt.dy]) : null;
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
  RACE.missiles = RACE.missiles.filter((m) => !m.dead);
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
  const what = ["", "Carrier Seeker", "ISI Mine", "RF Jammer"][cause];
  if (c.shield > 0) {
    c.shield = 0;
    RACE.emit("block", { a: (cause << 8) | src, b: srcId });
    RACE.fx.push({ k: "block", x: c.x, y: c.y, t: 0, col: c.col });
    RACE.say(`Walsh Shield stopped ${by}'s ${what}`, 1.6);
    RACE.sfx("shield");
    return;
  }
  RACE.sfx(cause === 3 ? "emp" : "boom");
  RACE.emit("hit", { a: (cause << 8) | src, b: srcId });
  if (cause === 3) { c.stun = 1.4; RACE.fx.push({ k: "zap", x: c.x, y: c.y, t: 0 }); }
  else { c.spin = 1.0; c.spinDir = Math.random() < 0.5 ? -1 : 1; c.vx *= 0.35; c.vy *= 0.35; RACE.fx.push({ k: "boom", x: c.x, y: c.y, t: 0 }); }
  RACE.say(`Hit by ${by}'s ${what}`, 1.6);
};

// ====================================================================== remote cars
// Remote cars are predicted in track coordinates (distance along the centreline, lateral offset): a
// racing car follows the track, so extrapolating along it stays right for much longer than a straight
// or constant-turn line, and the car can never be drawn through a wall.  Corrections when a new state
// disagrees with the prediction are blended along the track too.

RACE.trackPos = function (T, x, y, hint) {
  const i = RACE.nearest(T, x, y, hint);
  const dx = x - T.x[i], dy = y - T.y[i];
  const along = dx * T.tx[i] + dy * T.ty[i], lat = -dx * T.ty[i] + dy * T.tx[i];
  return { i, s: (((i * R_STEP + along) % T.L) + T.L) % T.L, lat };
};

RACE.posAt = function (T, s, lat) {
  const f = ((((s % T.L) + T.L) % T.L) / R_STEP), i = Math.floor(f) % T.M, j = (i + 1) % T.M, u = f - Math.floor(f);
  const tx = T.tx[i] * (1 - u) + T.tx[j] * u, ty = T.ty[i] * (1 - u) + T.ty[j] * u, n = Math.hypot(tx, ty) || 1;
  const x = T.x[i] * (1 - u) + T.x[j] * u, y = T.y[i] * (1 - u) + T.y[j] * u;
  return [x - (ty / n) * lat, y + (tx / n) * lat, Math.atan2(ty, tx), i];
};

RACE.LIM = () => R_TW + R_RUN - R_CAR.wid / 2;        // the car's centre can't go further out than this

RACE.prep = function (p, hint) {
  const T = RACE.T, tp = RACE.trackPos(T, p.x, p.y, hint);
  const th = Math.atan2(T.ty[tp.i], T.tx[tp.i]);
  const vAl = p.vx * T.tx[tp.i] + p.vy * T.ty[tp.i], vCr = -p.vx * T.ty[tp.i] + p.vy * T.tx[tp.i];
  const sp = Math.hypot(p.vx, p.vy), rel = rWrap(p.h - th);
  const along = !(p.fl & (R_FLAG.spin | R_FLAG.stun)) && sp > 60 && vAl > 0 && Math.abs(rel) < 1.1;
  return Object.assign(p, { tp, vAl, vCr, rel, along });
};

RACE.predictTrack = function (p, tg) {
  const T = RACE.T, lim = RACE.LIM();
  if (p.along) {
    const dt = rClamp(tg - p.t / 1000, 0, 1.5), tau = 0.35;
    const s = p.tp.s + p.vAl * dt;
    const lat = rClamp(p.tp.lat + p.vCr * tau * (1 - Math.exp(-dt / tau)), -lim, lim);
    const th = RACE.posAt(T, s, 0)[2];
    return [s, lat, th + p.rel * Math.exp(-dt / 0.5)];
  }
  const dt = rClamp(tg - p.t / 1000, 0, 0.35);
  const x = p.x + p.vx * dt, y = p.y + p.vy * dt;
  const tp = RACE.trackPos(T, x, y, p.tp.i);
  return [tp.s, rClamp(tp.lat, -lim, lim), p.h + p.w * dt];
};

RACE.onRemote = function (p) {
  const c = RACE.cars[p.s];
  if (!c || (RACE.me && c === RACE.me)) return;
  const now = RACE.tg();
  const before = c.pkt ? RACE.predictTrack(c.pkt, now) : null;
  RACE.prep(p, c.pkt ? c.pkt.tp.i : undefined);
  c.pkt = p;
  c.d = p.d; c.fl = p.fl; c.it = p.it;
  c.rx.push(performance.now());
  while (c.rx.length && performance.now() - c.rx[0] > 3000) c.rx.shift();
  c.age = p.age;
  c.err = null;
  if (before) {
    const after = RACE.predictTrack(p, now), L = RACE.T.L;
    let ds = before[0] - after[0];
    if (ds > L / 2) ds -= L; if (ds < -L / 2) ds += L;
    const st = RACE.dbg || (RACE.dbg = { n: 0, off: 0, worst: 0 });
    (st.corr || (st.corr = [])).push(Math.abs(ds));
    if (st.corr.length > 4000) st.corr.splice(0, 2000);
    if (Math.abs(ds) < 450) c.err = { s: ds, lat: before[1] - after[1], h: rWrap(before[2] - after[2]), at: performance.now() };
    else st.snaps = (st.snaps || 0) + 1;
  }
  for (const e of p.ev || []) RACE.remoteEvent(c, e);
};

RACE.placeRemotes = function (tg) {
  const T = RACE.T, lim = RACE.LIM();
  for (const c of RACE.cars) {
    if ((RACE.me && c === RACE.me) || !c.pkt) continue;
    let [s, lat, h] = RACE.predictTrack(c.pkt, tg);
    if (c.err) {
      const u = rClamp((performance.now() - c.err.at) / 160, 0, 1), k = 1 - u * u * (3 - 2 * u);
      s += c.err.s * k; lat += c.err.lat * k; h += c.err.h * k;
      if (u >= 1) c.err = null;
    }
    const [x, y, , i] = RACE.posAt(T, s, rClamp(lat, -lim, lim));
    const sp = Math.hypot(c.pkt.vx, c.pkt.vy);
    c.dx = x; c.dy = y; c.dh = h; c.di = i;
    c.dvx = Math.cos(h) * sp; c.dvy = Math.sin(h) * sp;
    const st = RACE.dbg || (RACE.dbg = { n: 0, off: 0, worst: 0 });
    const over = Math.abs(RACE.lateral(T, RACE.nearest(T, x, y, i), x, y)) - (R_TW + R_RUN);
    st.n++;
    if (over > 0) { st.off++; st.worst = Math.max(st.worst, over); }
  }
};

RACE.remoteEvent = function (c, e) {
  const key = `${c.s}:${e.id}:${e.k}`, now = performance.now();
  if (now - (RACE.seen.get(key) ?? -1e9) < 10000) return;
  RACE.seen.set(key, now);
  if (RACE.seen.size > 512) for (const [k, t] of RACE.seen) if (now - t > 10000) RACE.seen.delete(k);
  const fx = RACE.me ? RACE.me.x : (RACE.cam?.x || 0), fy = RACE.me ? RACE.me.y : (RACE.cam?.y || 0);
  const near = !RACE.quiet && Math.hypot(e.x - fx, e.y - fy) < (RACE.me ? 1100 : 1800);
  if (near && (e.k === "missile" || e.k === "mine")) RACE.sfx(e.k, { gap: 0.2 });
  if (near && e.k === "hit" && (!RACE.me || (e.a & 0xff) !== RACE.me.s)) RACE.sfx((e.a >> 8) === 3 ? "emp" : "boom", { gap: 0.2 });
  if (!RACE.quiet && e.k === "hit" && RACE.me && (e.a & 0xff) === RACE.me.s) RACE.sfx("boom", { gap: 0.2 });
  if (e.k === "missile") RACE.addMissile(c.s, e);
  else if (e.k === "mine") RACE.addMine(c.s, e);
  else if (e.k === "emp") {
    RACE.fx.push({ k: "emp", x: e.x, y: e.y, t: 0, col: c.col });
    if (RACE.me) {
      const me = RACE.me, t = e.t / 1000;
      let best = null;
      for (const h of RACE.hist) if (!best || Math.abs(h[0] - t) < Math.abs(best[0] - t)) best = h;
      const pos = best && Math.abs(best[0] - t) < 0.3 ? [best[1], best[2]] : [me.x, me.y];
      if (me.finT === null && Math.hypot(pos[0] - e.x, pos[1] - e.y) < R_EMP_R) RACE.hitMe(3, c.s, e.id);
    }
  } else if (e.k === "hit" || e.k === "block") {
    const src = e.a & 0xff, srcId = e.b;
    for (const m of RACE.missiles) if (m.seat === src && m.id === srcId) m.dead = true;
    for (const m of RACE.mines) if (m.seat === src && m.id === srcId) m.dead = true;
    RACE.fx.push({ k: e.k === "hit" ? ((e.a >> 8) === 3 ? "zap" : "boom") : "block", x: e.x, y: e.y, t: 0, col: c.col });
    if (RACE.me && src === RACE.me.s) RACE.say(e.k === "hit" ? `You hit ${c.name}` : `${c.name}'s Walsh Shield stopped it`, 1.6);
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
  const s = { t: Math.round(RACE.tg() * 1000), x: c.x, y: c.y, h: c.h, vx: c.vx, vy: c.vy, w: c.ws || 0, d: c.d, fl: c.fl,
    it: (c.it & 7) | ((Math.round(c.steer) + 1) << 3),
    ls: Math.round(c.lapStart * 1000), bl: c.best === null ? -1 : Math.round(c.best * 1000), ei: RACE.eid };
  fetch("/api/gc/race", { method: "POST", body: JSON.stringify({ r: RACE.rid, s, ev }) })
    .catch(() => { RACE.outEv.unshift(...ev); })
    .finally(() => { RACE.posting = false; });
};

// ====================================================================== race order
RACE.order = function (withAway = false) {
  return [...RACE.cars].filter((c) => !c.gone || (withAway && c.away) || c.fin !== null).sort((a, b) =>
    (a.fin !== null) !== (b.fin !== null) ? (a.fin !== null ? -1 : 1) : a.fin !== null ? a.fin - b.fin : b.d - a.d);
};

// ====================================================================== frame
RACE.frame = function (now) {
  if (!RACE.root || !RACE.root.isConnected) return;
  RACE.raf = requestAnimationFrame(RACE.frame);
  const dtw = rClamp((now - RACE.last) / 1000, 0, 0.1);
  RACE.last = now;
  RACE.fpsN = (RACE.fpsN || 0) + 1;
  if (!RACE.fpsT || now - RACE.fpsT > 2000) {
    if (RACE.fpsT) { RACE.fps = Math.round(RACE.fpsN * 1000 / (now - RACE.fpsT)); if (GC.open) RACE.adapt(now); }
    RACE.fpsT = now; RACE.fpsN = 0;
  }
  if (GC.clocks[RACE.rid]?.off === null || GC.clocks[RACE.rid]?.off === undefined) return;
  if (RACE.spec) {
    const tg = RACE.tg();
    RACE.placeRemotes(tg);
    RACE.moveMissiles(tg);
    RACE.stepSpecCam(dtw);
    if (!GC.open) { RACE.keys.clear(); SFX.engineStop(); return; }
    const n = tg < 0 && tg > -3 ? Math.ceil(-tg) : tg >= 0 && tg < 1 ? 0 : null;
    if (n !== RACE.countSaid && n !== null) SFX.play(n ? "beep" : "go");
    RACE.countSaid = n;
    const t0d = performance.now();
    RACE.draw(dtw, tg);
    RACE.drawMs = 0.95 * (RACE.drawMs || 0) + 0.05 * (performance.now() - t0d);
    if (now - RACE.hudT > 100) { RACE.hudT = now; RACE.drawSpecHud(tg); }
    return;
  }
  RACE.acc += dtw;
  while (RACE.acc >= R_DT) { RACE.step(R_DT); RACE.acc -= R_DT; }
  const tg = RACE.tg();
  RACE.placeRemotes(tg);
  RACE.moveMissiles(tg);
  if (now - RACE.postT >= 50) { RACE.postT = now; RACE.post(); }
  if (!GC.open) { RACE.keys.clear(); RACE.syncTouchOverlay(); SFX.engineStop(); return; }
  RACE.syncTouchOverlay();
  RACE.sounds(tg);
  const t0d = performance.now();
  RACE.draw(dtw, tg);
  RACE.drawMs = 0.95 * (RACE.drawMs || 0) + 0.05 * (performance.now() - t0d);
  if (now - RACE.hudT > 100) { RACE.hudT = now; RACE.drawHud(tg); }
};

RACE.stepSpecCam = function (dt) {
  const sc = RACE.specCam, cv = RACE.cv;
  if (!sc || !cv || !RACE.T) return;
  const W = cv.clientWidth || 1200, H = cv.clientHeight || 800, fit = RACE.calcFit(W, H);
  if (sc.targetZoom === null) { sc.zoom = sc.targetZoom = fit.scale; sc.x = sc.tx = fit.cx; sc.y = sc.ty = fit.cy; }
  sc.targetZoom = rClamp(sc.targetZoom, fit.scale, 1.5);
  const kx = (RACE.keys.has("right") ? 1 : 0) - (RACE.keys.has("left") ? 1 : 0);
  const ky = (RACE.keys.has("down") ? 1 : 0) - (RACE.keys.has("up") ? 1 : 0);
  if ((kx || ky) && !sc.drag?.moved) {
    sc.follow = null; sc.autoChase = false;
    const sp = (780 / Math.max(sc.zoom || fit.scale, 0.14)) * dt;
    const [nx, ny] = RACE.clampSpecCam(sc.tx + kx * sp, sc.ty + ky * sp, sc.targetZoom, W, H);
    sc.tx = sc.x = nx; sc.ty = sc.y = ny;
  }
  if (sc.follow !== null) {
    const fc = RACE.cars[sc.follow];
    if (fc) {
      const fx = fc.dx ?? fc.x, fy = fc.dy ?? fc.y, fh = fc.dh ?? fc.h ?? 0;
      sc.tx = fx; sc.ty = fy;
      if (sc.autoChase) {
        const err = rWrap(fh - sc.a);
        sc.a = rWrap(sc.a + err * (1 - Math.exp(-dt / (0.05 + 0.25 * Math.abs(err) / Math.PI))));
      }
    }
  }
  const k = 1 - Math.exp(-dt * 10);
  sc.zoom += (sc.targetZoom - sc.zoom) * k;
  const [tx, ty] = RACE.clampSpecCam(sc.tx, sc.ty, sc.targetZoom, W, H);
  sc.tx = tx; sc.ty = ty;
  sc.x += (sc.tx - sc.x) * k;
  sc.y += (sc.ty - sc.y) * k;
};

RACE.sounds = function (tg) {
  const me = RACE.me;
  const n = tg < 0 && tg > -3 ? Math.ceil(-tg) : tg >= 0 && tg < 1 ? 0 : null;
  if (n !== RACE.countSaid && n !== null) { SFX.play(n ? "beep" : "go"); }
  RACE.countSaid = n;
  if (pref("sound") && me.finT === null && tg > -4 && !RACE.results) {
    SFX.engineStart();
    const gasOn = !RACE.overlayBlocked() && (RACE.touch.gas || (RACE.touch.autoGas && !RACE.touch.brake));
    SFX.engineSet(rClamp(Math.hypot(me.vx, me.vy) / (R_CAR.vmax * RACE.B.phys.topSpeedMul * 1.2), 0, 1), RACE.keys.has("up") || gasOn || me.boost > 0);
  } else SFX.engineStop();
};

RACE.res = 1; RACE.resHold = 0;
RACE.adapt = function (now) {
  if (RACE.fps < 45 && RACE.res > 0.7) { RACE.res = Math.round((RACE.res - 0.1) * 10) / 10; RACE.resHold = now + 20000; }
  else if (RACE.fps >= 57 && RACE.res < 1 && now > RACE.resHold) RACE.res = Math.round((RACE.res + 0.1) * 10) / 10;
};

RACE.fmt = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
RACE.say = function (text, secs = 1.5) { RACE.msgs.push({ text, until: performance.now() + secs * 1000 }); };

// ====================================================================== drawing & biome weather
RACE.initWeather = function () {
  const r = rMulberry(99);
  RACE.rain = Array.from({ length: 240 }, () => ({ x: r(), y: r(), z: 0.35 + r() * 0.65, p: r() * Math.PI * 2 }));
  RACE.splash = [];
};
RACE.initRain = RACE.initWeather;

RACE.drawWeather = function (g, dt, W, H) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const pal = RACE.B, kind = pal.weather;
  if (kind === "rain") {
    g.strokeStyle = pal.particleCol; g.lineWidth = 1; g.beginPath();
    for (const d of RACE.rain) {
      d.y += dt * (1.3 + d.z); d.x += dt * 0.12;
      if (d.y > 1) { d.y -= 1; d.x = Math.random(); }
      if (d.x > 1) d.x -= 1;
      const x = d.x * W, y = d.y * H, len = 10 + 16 * d.z;
      g.moveTo(x, y); g.lineTo(x - len * 0.18, y - len);
    }
    g.stroke();
    if (Math.random() < 0.9) RACE.splash.push({ x: Math.random() * W, y: Math.random() * H, t: 0 });
    RACE.splash = RACE.splash.filter((s) => (s.t += dt) < 0.28);
    g.strokeStyle = "rgba(180,220,255,0.22)"; g.beginPath();
    for (const s of RACE.splash) { const rx = 2 + s.t * 30; g.moveTo(s.x + rx, s.y); g.ellipse(s.x, s.y, rx, 1 + s.t * 12, 0, 0, Math.PI * 2); }
    g.stroke();
  } else if (kind === "sparks") {
    g.fillStyle = pal.particleCol; g.beginPath();
    for (let i = 0; i < 120; i++) {
      const d = RACE.rain[i];
      d.x += dt * (0.14 + 0.15 * d.z); d.y += dt * (0.09 + 0.1 * d.z);
      if (d.x > 1) d.x -= 1; if (d.y > 1) d.y -= 1;
      const x = d.x * W, y = d.y * H, sz = 1.6 + 1.8 * d.z;
      g.rect(x, y, sz, sz);
    }
    g.fill();
  } else if (kind === "dust") {
    g.strokeStyle = pal.particleCol; g.lineWidth = 1.5; g.beginPath();
    for (let i = 0; i < 150; i++) {
      const d = RACE.rain[i];
      d.x += dt * (0.45 + 0.35 * d.z); d.y += dt * 0.08 * Math.sin(d.p + performance.now() / 600);
      if (d.x > 1) d.x -= 1; if (d.y > 1) d.y -= 1; if (d.y < 0) d.y += 1;
      const x = d.x * W, y = d.y * H, len = 10 + 18 * d.z;
      g.moveTo(x, y); g.lineTo(x - len, y - len * 0.12);
    }
    g.stroke();
  } else if (kind === "snow") {
    g.fillStyle = pal.particleCol; g.beginPath();
    for (let i = 0; i < 190; i++) {
      const d = RACE.rain[i];
      d.y += dt * (0.22 + 0.18 * d.z); d.x += dt * (0.05 + 0.06 * Math.sin(d.p + performance.now() / 700));
      if (d.y > 1) { d.y -= 1; d.x = Math.random(); }
      if (d.x > 1) d.x -= 1; if (d.x < 0) d.x += 1;
      const x = d.x * W, y = d.y * H, r = 1.4 + 2.1 * d.z;
      g.moveTo(x + r, y); g.arc(x, y, r, 0, Math.PI * 2);
    }
    g.fill();
  } else if (kind === "mist") {
    if (!RACE.mistSpr) RACE.sprites();
    for (let i = 0; i < 42; i++) {
      const d = RACE.rain[i];
      d.x += dt * (0.04 + 0.05 * d.z); d.y += dt * 0.008;
      if (d.x > 1) d.x -= 1; if (d.y > 1) d.y -= 1;
      const x = d.x * W, y = d.y * H, rx = 28 + 38 * d.z, ry = rx * 0.55;
      g.drawImage(RACE.mistSpr, x - rx, y - ry, rx * 2, ry * 2);
    }
  }
};

RACE.draw = function (dt, tg) {
  const cv = RACE.cv, g = RACE.ctx, T = RACE.T, me = RACE.me, bm = RACE.biome, pal = RACE.B;
  const dpr = Math.min(1.5, window.devicePixelRatio || 1) * RACE.res, W = cv.clientWidth, H = cv.clientHeight;
  RACE.dpr = dpr;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
  const S0 = RACE.TILE, cam = RACE.cam, B = RACE.BLEED;
  let z, chase = false, bird = false, M, vx0, vy0, vx1, vy1, eye, rot = 0;
  g.setTransform(1, 0, 0, 1, 0, 0);
  g.fillStyle = bm === 4 ? pal.bgSea : pal.bg; g.fillRect(0, 0, cv.width, cv.height);

  if (RACE.spec) {
    const sc = RACE.specCam, fit = RACE.calcFit(W, H);
    z = rClamp(sc?.zoom || fit.scale, fit.scale, 1.5);
    cam.x = sc ? sc.x : fit.cx; cam.y = sc ? sc.y : fit.cy; cam.z = z;
    chase = Boolean(sc && sc.follow !== null && sc.autoChase);
    bird = z <= fit.scale * 1.15 && (!sc || sc.follow === null);
    if (RACE.miniCv) RACE.miniCv.hidden = z <= fit.scale * 1.15;
    const tz = RACE.pickTileZ(dpr, z), sd = z * dpr;
    if (tz !== RACE.tz) { RACE.tz = tz; RACE.tiles = new Map(); }
    if (chase) {
      rot = -(sc.a || 0) - Math.PI / 2;
      const co = Math.cos(rot) * sd, si = Math.sin(rot) * sd, Ax = dpr * W / 2, Ay = dpr * H * 0.62;
      M = [co, si, -si, co, Ax - (co * cam.x - si * cam.y), Ay - (si * cam.x + co * cam.y)];
      const inv = (sx, sy) => { const u = sx - M[4], v = sy - M[5], d = sd * sd; return [(co * u + si * v) / d, (-si * u + co * v) / d]; };
      const cs = [inv(0, 0), inv(dpr * W, 0), inv(0, dpr * H), inv(dpr * W, dpr * H)];
      vx0 = Math.min(...cs.map((p) => p[0])); vx1 = Math.max(...cs.map((p) => p[0]));
      vy0 = Math.min(...cs.map((p) => p[1])); vy1 = Math.max(...cs.map((p) => p[1]));
      const ec = inv(dpr * W / 2, dpr * H / 2);
      eye = { x: ec[0], y: ec[1] };
    } else {
      const cyOff = bird ? dpr * (H / 2 + 12) : dpr * H / 2;
      const ox = dpr * W / 2 - cam.x * sd, oy = cyOff - cam.y * sd;
      M = [sd, 0, 0, sd, ox, oy];
      vx0 = (0 - ox) / sd; vy0 = (0 - oy) / sd; vx1 = (dpr * W - ox) / sd; vy1 = (dpr * H - oy) / sd;
      eye = { x: cam.x, y: cam.y };
    }
    g.setTransform(...M);
    const TS = S0 * sd, rad = TS * 0.72 + B;
    for (let tx2 = Math.floor(vx0 / S0); tx2 <= Math.floor(vx1 / S0); tx2++)
      for (let ty2 = Math.floor(vy0 / S0); ty2 <= Math.floor(vy1 / S0); ty2++) {
        if (chase) {
          const cx = (tx2 + 0.5) * S0, cy = (ty2 + 0.5) * S0;
          const sx = M[0] * cx + M[2] * cy + M[4], sy = M[1] * cx + M[3] * cy + M[5];
          if (sx < -rad || sy < -rad || sx > dpr * W + rad || sy > dpr * H + rad) continue;
        }
        g.drawImage(RACE.tile(tx2, ty2), tx2 * S0 - B / tz, ty2 * S0 - B / tz, S0 + 2 * B / tz, S0 + 2 * B / tz);
      }
  } else {
    const mode = pref("raceCam");
    chase = mode === "chase";
    bird = mode === "bird";
    if (RACE.miniCv) RACE.miniCv.hidden = bird;
    const fit = bird ? RACE.calcFit(W, H) : null;
    z = bird ? fit.scale : rClamp(Math.min(W / 1500, H / 950) * 1.12, 0.55, 1.35);
    const tz = RACE.pickTileZ(dpr, z), sd = z * dpr;
    if (tz !== RACE.tz) { RACE.tz = tz; RACE.tiles = new Map(); }
    cam.z = bird ? z : tz / dpr;
    const TS = Math.round(S0 * tz);
    if (chase) {
      if (!RACE.chaseOn) cam.a = me.h;
      const err = rWrap(me.h - cam.a);
      if (!(me.spin > 0)) cam.a = rWrap(cam.a + err * (1 - Math.exp(-dt / (0.05 + 0.25 * Math.abs(err) / Math.PI))));
      cam.x = me.x; cam.y = me.y;
      rot = -cam.a - Math.PI / 2;
      const co = Math.cos(rot) * tz, si = Math.sin(rot) * tz, Ax = dpr * W / 2, Ay = dpr * H * 0.66;
      M = [co, si, -si, co, Ax - (co * cam.x - si * cam.y), Ay - (si * cam.x + co * cam.y)];
      const inv = (sx, sy) => { const u = sx - M[4], v = sy - M[5], d = tz * tz; return [(co * u + si * v) / d, (-si * u + co * v) / d]; };
      const cs = [inv(0, 0), inv(dpr * W, 0), inv(0, dpr * H), inv(dpr * W, dpr * H)];
      vx0 = Math.min(...cs.map((p) => p[0])); vx1 = Math.max(...cs.map((p) => p[0]));
      vy0 = Math.min(...cs.map((p) => p[1])); vy1 = Math.max(...cs.map((p) => p[1]));
      const ec = inv(dpr * W / 2, dpr * H / 2);
      eye = { x: ec[0], y: ec[1] };
      g.setTransform(...M);
      const rad = TS * 0.72 + B;
      for (let tx2 = Math.floor(vx0 / S0); tx2 <= Math.floor(vx1 / S0); tx2++)
        for (let ty2 = Math.floor(vy0 / S0); ty2 <= Math.floor(vy1 / S0); ty2++) {
          const cx = (tx2 + 0.5) * S0, cy = (ty2 + 0.5) * S0;
          const sx = M[0] * cx + M[2] * cy + M[4], sy = M[1] * cx + M[3] * cy + M[5];
          if (sx < -rad || sy < -rad || sx > dpr * W + rad || sy > dpr * H + rad) continue;
          g.drawImage(RACE.tile(tx2, ty2), tx2 * S0 - B / tz, ty2 * S0 - B / tz, S0 + 2 * B / tz, S0 + 2 * B / tz);
        }
    } else if (bird) {
      cam.x = fit.cx; cam.y = fit.cy;
      const ox = dpr * W / 2 - cam.x * sd, oy = dpr * (H / 2 + 12) - cam.y * sd;
      M = [sd, 0, 0, sd, ox, oy];
      vx0 = (0 - ox) / sd; vy0 = (0 - oy) / sd; vx1 = (dpr * W - ox) / sd; vy1 = (dpr * H - oy) / sd;
      eye = { x: cam.x, y: cam.y };
      g.setTransform(...M);
      for (let tx2 = Math.floor(vx0 / S0); tx2 <= Math.floor(vx1 / S0); tx2++)
        for (let ty2 = Math.floor(vy0 / S0); ty2 <= Math.floor(vy1 / S0); ty2++)
          g.drawImage(RACE.tile(tx2, ty2), tx2 * S0 - B / tz, ty2 * S0 - B / tz, S0 + 2 * B / tz, S0 + 2 * B / tz);
    } else {
      const tx = me.x + me.vx * 0.32, ty = me.y + me.vy * 0.32, k = 1 - Math.exp(-dt * 5);
      cam.x += (tx - cam.x) * k; cam.y += (ty - cam.y) * k;
      const ox = Math.round(dpr * W / 2 - cam.x * tz), oy = Math.round(dpr * H / 2 - cam.y * tz);
      vx0 = (0 - ox) / tz; vy0 = (0 - oy) / tz; vx1 = (dpr * W - ox) / tz; vy1 = (dpr * H - oy) / tz;
      for (let tx2 = Math.floor(vx0 / S0); tx2 <= Math.floor(vx1 / S0); tx2++)
        for (let ty2 = Math.floor(vy0 / S0); ty2 <= Math.floor(vy1 / S0); ty2++) g.drawImage(RACE.tile(tx2, ty2), ox + tx2 * TS - B, oy + ty2 * TS - B);
      M = [tz, 0, 0, tz, ox, oy];
      eye = { x: cam.x, y: cam.y };
      g.setTransform(...M);
    }
  }
  RACE.chaseOn = chase;
  RACE.birdOn = bird;
  RACE.view = { M, rot };
  const entScale = z < 0.72 ? rClamp(Math.pow(0.72 / Math.max(z, 0.14), 0.55), 1.0, 2.2) : 1.0;
  const lblScale = z < 0.85 ? rClamp(0.85 / Math.max(z, 0.14), 1.0, 3.6) : 1.0;
  const vis = (x, y, m = 80) => x > vx0 - m && x < vx1 + m && y > vy0 - m && y < vy1 + m;
  const t = performance.now() / 1000;

  // Boost pads (PCB Speedway renders glowing cyan chevron traces with via pads)
  for (const p of T.pads) {
    const [px, py] = RACE.at(T, p.i, p.off);
    if (!vis(px, py)) continue;
    g.save(); g.translate(px, py); g.rotate(Math.atan2(T.ty[p.i], T.tx[p.i]));
    if (bm === 1) {
      g.fillStyle = "rgba(0,255,200,0.12)"; g.fillRect(-42, -34, 84, 68);
      for (let j = 0; j < 3; j++) {
        const a = 0.4 + 0.6 * ((Math.sin(t * 7 - j * 1.2) + 1) / 2);
        g.strokeStyle = `rgba(0,255,200,${a})`; g.lineWidth = 4;
        g.beginPath(); g.moveTo(-28 + j * 20, -22); g.lineTo(-12 + j * 20, 0); g.lineTo(-28 + j * 20, 22); g.stroke();
        g.fillStyle = `rgba(0,255,200,${a})`;
        g.beginPath(); g.arc(-28 + j * 20, -22, 2.8, 0, Math.PI * 2); g.arc(-28 + j * 20, 22, 2.8, 0, Math.PI * 2); g.fill();
      }
    } else {
      g.fillStyle = "rgba(63,230,255,0.12)"; g.fillRect(-40, -34, 80, 68);
      for (let j = 0; j < 3; j++) {
        const a = 0.35 + 0.65 * ((Math.sin(t * 6 - j * 1.3) + 1) / 2);
        g.strokeStyle = `rgba(63,230,255,${a})`; g.lineWidth = 5;
        g.beginPath(); g.moveTo(-28 + j * 20, -22); g.lineTo(-12 + j * 20, 0); g.lineTo(-28 + j * 20, 22); g.stroke();
      }
    }
    g.restore();
  }

  // Holo caches
  for (const b of T.caches) {
    const [bx, by] = RACE.at(T, b.i, b.off);
    if (!vis(bx, by) || b.gone > performance.now()) continue;
    g.save(); g.translate(bx, by); if (entScale > 1.02) g.scale(entScale, entScale); g.rotate(t * 1.6 + b.off);
    g.globalCompositeOperation = "lighter";
    g.fillStyle = "rgba(255,171,64,0.18)"; g.fillRect(-22, -22, 44, 44);
    g.strokeStyle = "rgba(255,208,138,0.9)"; g.lineWidth = 2; g.strokeRect(-14, -14, 28, 28);
    g.rotate(Math.PI / 4); g.strokeStyle = "rgba(164,124,255,0.8)"; g.strokeRect(-10, -10, 20, 20);
    g.restore();
  }

  // ISI Mines: pulsing red/amber sine-wave ring on track
  for (const m of RACE.mines) {
    if (m.dead || !vis(m.x, m.y)) continue;
    const armed = tg - m.t0 > R_MINE_ARM, pulse = 0.5 + 0.5 * Math.sin(t * 8);
    g.save(); g.translate(m.x, m.y); if (entScale > 1.02) g.scale(entScale, entScale); g.translate(-m.x, -m.y);
    g.globalCompositeOperation = "lighter";
    g.strokeStyle = armed ? `rgba(255,59,48,${0.6 + 0.35 * pulse})` : "rgba(255,171,64,0.4)";
    g.lineWidth = 2.4; g.beginPath();
    for (let s = 0; s <= 36; s++) {
      const a = (s / 36) * Math.PI * 2;
      const r = 17 + (armed ? 3.5 : 1.5) * Math.sin(a * 8 + t * 10);
      const px = m.x + Math.cos(a) * r, py = m.y + Math.sin(a) * r;
      s ? g.lineTo(px, py) : g.moveTo(px, py);
    }
    g.closePath(); g.stroke();
    g.strokeStyle = `rgba(255,171,64,${0.4 + 0.35 * pulse})`; g.lineWidth = 1.6;
    g.beginPath(); g.arc(m.x, m.y, 11 + 2 * pulse, 0, Math.PI * 2); g.stroke();
    g.fillStyle = armed ? "#ff3b30" : "#ffab40"; g.beginPath(); g.arc(m.x, m.y, 6, 0, Math.PI * 2); g.fill();
    g.restore();
  }

  // Per-car biome trails, drift particles & car bodies
  const order = RACE.cars.filter((c) => (me && c === me) || c.pkt || (RACE.spec && RACE.tg() < 2));
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  for (const c of order) {
    const isMe = me && c === me;
    const x = isMe ? c.x : (c.dx ?? c.x), y = isMe ? c.y : (c.dy ?? c.y), h = isMe ? c.h : (c.dh ?? c.h);
    const vx = isMe ? c.vx : c.dvx, vy = isMe ? c.vy : c.dvy;
    const sp = Math.hypot(vx || 0, vy || 0);
    const slip = isMe ? c.slip : Math.abs(-(vx || 0) * Math.sin(h) + (vy || 0) * Math.cos(h));
    if (!c.wake) c.wake = [];
    if (bm === 1 || bm === 4) {
      c.wake.push({ x, y, h, sp, t });
      while (c.wake.length > 22 || (c.wake.length && t - c.wake[0].t > 0.38)) c.wake.shift();
    }
    if (!still && vis(x, y, 300)) {
      if (bm === 2 && (slip > 55 || (c.fl & R_FLAG.spin)) && Math.random() < 0.7) {
        const bx = x - Math.cos(h) * 18, by = y - Math.sin(h) * 18;
        RACE.fx.push({ k: "dustpuff", x: bx + (Math.random() - 0.5) * 14, y: by + (Math.random() - 0.5) * 14, t: 0 });
      } else if (bm === 3 && (slip > 45 || (c.fl & R_FLAG.spin)) && Math.random() < 0.8) {
        const bx = x - Math.cos(h) * 20, by = y - Math.sin(h) * 20;
        RACE.fx.push({ k: "roost", x: bx + (Math.random() - 0.5) * 14, y: by + (Math.random() - 0.5) * 14, vx: -Math.cos(h) * 90, vy: -Math.sin(h) * 90, t: 0 });
      }
    }
    // Oscilloscope persistence wake (Biome 1) & GT Roadster slipstream ribbons (Biome 4)
    if (bm === 1 && c.wake.length > 2) {
      g.save(); g.globalCompositeOperation = "lighter";
      g.strokeStyle = c.col; g.lineWidth = 2.2; g.globalAlpha = 0.65;
      g.beginPath();
      for (let k = 0; k < c.wake.length; k++) {
        const w = c.wake[k], amp = (1 - k / c.wake.length) * 6 * Math.sin(k * 1.4 - t * 22);
        const wx = w.x - Math.sin(w.h) * amp, wy = w.y + Math.cos(w.h) * amp;
        k ? g.lineTo(wx, wy) : g.moveTo(wx, wy);
      }
      g.stroke(); g.restore();
    } else if (bm === 4 && sp > 420 && c.wake.length > 2) {
      g.save(); g.strokeStyle = "rgba(255,255,255,0.26)"; g.lineWidth = 1.8;
      for (const side of [-9.5, 9.5]) {
        g.beginPath();
        for (let k = 0; k < c.wake.length; k++) {
          const w = c.wake[k];
          const wx = w.x - Math.cos(w.h) * 16 - Math.sin(w.h) * side;
          const wy = w.y - Math.sin(w.h) * 16 + Math.cos(w.h) * side;
          k ? g.lineTo(wx, wy) : g.moveTo(wx, wy);
        }
        g.stroke();
      }
      g.restore();
    }
    if (!vis(x, y, 300)) continue;
    const isFocal = isMe || (RACE.spec && RACE.specCam?.follow === c.s);
    if (z < 0.72 || isFocal) {
      g.save(); g.translate(x, y);
      if (z < 0.72) {
        g.fillStyle = "rgba(6,8,14,0.68)";
        g.beginPath(); g.arc(0, 0, 32 * entScale, 0, Math.PI * 2); g.fill();
        g.strokeStyle = c.col; g.lineWidth = isFocal ? 3.5 * entScale : 2.2 * entScale;
        g.beginPath(); g.arc(0, 0, 32 * entScale, 0, Math.PI * 2); g.stroke();
      }
      if (isFocal && (bird || z < 0.72)) {
        g.rotate(h);
        g.fillStyle = isMe ? "#fff" : c.col;
        g.strokeStyle = "#080a10"; g.lineWidth = 2;
        g.beginPath();
        const r = 46 * entScale;
        g.moveTo(r + 14 * entScale, 0); g.lineTo(r - 2 * entScale, -10 * entScale); g.lineTo(r - 2 * entScale, 10 * entScale);
        g.closePath(); g.fill(); g.stroke();
      }
      g.restore();
    }
    g.save(); g.translate(x, y); g.rotate(h);
    if (entScale > 1.02) g.scale(entScale, entScale);
    drawCar(g, c, bm);
    g.restore();
    if (!isMe || bird) {
      g.save(); g.translate(x, y); g.rotate(-rot);
      if (lblScale > 1.02) g.scale(lblScale, lblScale);
      g.font = "700 13px 'Chakra Petch', system-ui"; g.textAlign = "center";
      g.lineWidth = 3.5; g.strokeStyle = "rgba(8,10,16,0.9)";
      const nmTxt = isMe ? "YOU" : (c.away ? `${c.name} · reconnecting` : c.name);
      const yOff = -(24 + (entScale - 1) * 14) / lblScale * entScale;
      g.strokeText(nmTxt, 0, Math.min(-32, yOff));
      g.fillStyle = isMe ? "#ffffff" : c.col;
      g.fillText(nmTxt, 0, Math.min(-32, yOff));
      g.restore();
    }
  }

  // Carrier Seekers + colored phase-lock reticles on targeted cars
  for (const m of RACE.missiles) {
    if (m.dead) continue;
    const lockCol = R_SEAT[m.seat % 4];
    const tgt = m.tgt !== null ? RACE.cars[m.tgt] : null;
    const tx = tgt ? ((me && tgt === me) ? tgt.x : tgt.dx) : undefined;
    const ty = tgt ? ((me && tgt === me) ? tgt.y : tgt.dy) : undefined;
    if (tx !== undefined && vis(tx, ty, 60)) {
      g.save(); g.translate(tx, ty); g.rotate(t * 3.5);
      g.strokeStyle = lockCol; g.lineWidth = 2; g.setLineDash([7, 5]);
      g.beginPath(); g.arc(0, 0, 29 * entScale, 0, Math.PI * 2); g.stroke();
      g.setLineDash([]);
      for (let q = 0; q < 4; q++) {
        g.rotate(Math.PI / 2);
        g.beginPath(); g.moveTo(22 * entScale, -8 * entScale); g.lineTo(29 * entScale, -8 * entScale); g.lineTo(29 * entScale, -2 * entScale); g.stroke();
      }
      g.restore();
    }
    if (!vis(m.x, m.y)) continue;
    g.save(); g.translate(m.x, m.y); g.rotate(m.h);
    if (entScale > 1.02) g.scale(entScale, entScale);
    g.globalCompositeOperation = "lighter";
    const gr = g.createLinearGradient(-70, 0, 0, 0); gr.addColorStop(0, "rgba(255,171,64,0)"); gr.addColorStop(1, "rgba(255,190,90,0.85)");
    g.fillStyle = gr; g.fillRect(-70, -3, 70, 6);
    g.fillStyle = lockCol; g.beginPath(); g.moveTo(11, 0); g.lineTo(-6, -5); g.lineTo(-6, 5); g.fill();
    g.fillStyle = "#fff4dc"; g.beginPath(); g.moveTo(9, 0); g.lineTo(-3, -2.5); g.lineTo(-3, 2.5); g.fill();
    g.restore();
  }

  // Effects
  RACE.fx = RACE.fx.filter((f) => (f.t += dt) < (f.k === "emp" ? 0.7 : f.k === "dustpuff" || f.k === "roost" ? 0.48 : 0.8));
  for (const f of RACE.fx) {
    if (f.k === "dustpuff") {
      const u = f.t / 0.48;
      g.fillStyle = `rgba(210,145,92,${0.48 * (1 - u)})`;
      g.beginPath(); g.arc(f.x, f.y, 7 + 16 * u, 0, Math.PI * 2); g.fill();
    } else if (f.k === "roost") {
      const u = f.t / 0.48;
      g.fillStyle = `rgba(255,255,255,${0.72 * (1 - u)})`;
      g.beginPath(); g.arc(f.x + (f.vx || 0) * f.t * 0.25, f.y + (f.vy || 0) * f.t * 0.25, 4 + 12 * u, 0, Math.PI * 2); g.fill();
    }
  }
  g.save(); g.globalCompositeOperation = "lighter";
  for (const f of RACE.fx) {
    const u = f.t / 0.8;
    if (f.k === "boom") {
      const gr = g.createRadialGradient(f.x, f.y, 2, f.x, f.y, 30 + 90 * u);
      gr.addColorStop(0, `rgba(255,230,180,${1 - u})`); gr.addColorStop(0.4, `rgba(255,120,40,${0.8 * (1 - u)})`); gr.addColorStop(1, "rgba(255,60,20,0)");
      g.fillStyle = gr; g.beginPath(); g.arc(f.x, f.y, 30 + 90 * u, 0, Math.PI * 2); g.fill();
    } else if (f.k === "emp") {
      // RF Jammer: expanding static-noise ring (280 px radius)
      const v = f.t / 0.7, R = R_EMP_R * v;
      g.strokeStyle = `rgba(63,230,255,${1 - v})`; g.lineWidth = 4 * (1 - v) + 1.4;
      g.beginPath();
      for (let s = 0; s <= 48; s++) {
        const a = (s / 48) * Math.PI * 2;
        const jit = (Math.random() - 0.5) * 14 * (1 - v * 0.4);
        const rx = f.x + Math.cos(a) * (R + jit), ry = f.y + Math.sin(a) * (R + jit);
        s ? g.lineTo(rx, ry) : g.moveTo(rx, ry);
      }
      g.closePath(); g.stroke();
      g.strokeStyle = `rgba(255,255,255,${0.55 * (1 - v)})`; g.lineWidth = 1.5;
      g.beginPath(); g.arc(f.x, f.y, R * 0.85, 0, Math.PI * 2); g.stroke();
    } else if (f.k === "zap") {
      g.strokeStyle = `rgba(63,230,255,${1 - u})`; g.lineWidth = 2; g.beginPath();
      for (let j = 0; j < 8; j++) { const a = Math.random() * Math.PI * 2; g.moveTo(f.x, f.y); g.lineTo(f.x + Math.cos(a) * 50, f.y + Math.sin(a) * 50); }
      g.stroke();
    } else if (f.k === "block") {
      g.strokeStyle = `rgba(0,229,255,${1 - u})`; g.lineWidth = 3.5; g.beginPath();
      const R = 30 + 40 * u;
      for (let k = 0; k < 6; k++) {
        const ang = (k * Math.PI) / 3;
        const hx = f.x + Math.cos(ang) * R, hy = f.y + Math.sin(ang) * R;
        k ? g.lineTo(hx, hy) : g.moveTo(hx, hy);
      }
      g.closePath(); g.stroke();
    } else if (f.k === "qam") {
      g.save(); g.translate(f.x, f.y); g.rotate(f.h);
      for (let r = 0; r < 8; r++) for (let c0 = 0; c0 < 8; c0++) {
        g.fillStyle = (r + c0) & 1 ? `rgba(63,230,255,${1 - u})` : `rgba(255,179,71,${1 - u})`;
        g.fillRect(-c0 * (6 + 18 * u), (r - 3.5) * (3.2 + 5 * u), 2.5, 2.5);
      }
      g.restore();
    } else if (f.k === "spark") {
      g.fillStyle = `rgba(255,220,160,${1 - u})`;
      for (const s of f.p) g.fillRect(f.x + s[0] * f.t * 300, f.y + s[1] * f.t * 300, 2.5, 2.5);
    } else if (f.k === "puff") {
      g.fillStyle = `rgba(200,200,220,${0.3 * (1 - u)})`; g.beginPath(); g.arc(f.x, f.y, 10 + 30 * u, 0, Math.PI * 2); g.fill();
    }
  }
  g.restore();

  // Buildings and lamps (Neon Rain City only)
  if (bm === 0) {
    for (const b of T.buildings) {
      if (b.x1 < vx0 - 220 || b.x0 > vx1 + 220 || b.y1 < vy0 - 220 || b.y0 > vy1 + 220) continue;
      RACE.drawBuilding(g, b, eye, t);
    }
    g.save(); g.globalCompositeOperation = "lighter";
    for (const [lx, ly] of T.lamps) if (vis(lx, ly)) g.drawImage(RACE.lampSpr, lx - 20, ly - 20);
    g.restore();
  }

  // Start gantry
  {
    const i = 0, [ax, ay] = RACE.at(T, i, -(R_TW + R_RUN + 10)), [bx, by] = RACE.at(T, i, R_TW + R_RUN + 10);
    if (vis(ax, ay, 400)) {
      const ox = (ax + bx) / 2 - eye.x, oy = (ay + by) / 2 - eye.y, lift = 0.22;
      g.save(); g.translate(ox * lift, oy * lift);
      g.strokeStyle = "rgba(20,22,30,0.95)"; g.lineWidth = 16; g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
      g.strokeStyle = pal.accent; g.lineWidth = 2.5; g.beginPath(); g.moveTo(ax, ay); g.lineTo(bx, by); g.stroke();
      g.translate((ax + bx) / 2, (ay + by) / 2); g.rotate(Math.atan2(by - ay, bx - ax));
      const spr = RACE.glowText(tg < 0 ? "GRID" : "SLOT RUSH", 15, pal.accent, "#ffffff");
      g.drawImage(spr, -spr.width / 2, -spr.height / 2);
      g.restore();
    }
  }

  // Weather in screen space
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  RACE.drawWeather(g, dt, W, H);
  if (RACE.miniCv && !RACE.miniCv.hidden) RACE.drawMini();
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
  if (RACE.chaseOn) g.imageSmoothingEnabled = false;
  if (ox > 0) wall([b.x0, b.y0, r[0], r[1], r[0], r[3], b.x0, b.y1], 0.35);
  if (ox < 0) wall([b.x1, b.y0, r[2], r[1], r[2], r[3], b.x1, b.y1], 0.35);
  if (oy > 0) wall([b.x0, b.y0, r[0], r[1], r[2], r[1], b.x1, b.y0], 0.2);
  if (oy < 0) wall([b.x0, b.y1, r[0], r[3], r[2], r[3], b.x1, b.y1], 0.2);
  g.imageSmoothingEnabled = true;
  g.fillStyle = b.roof; g.fillRect(r[0], r[1], r[2] - r[0], r[3] - r[1]);
  g.strokeStyle = "rgba(150,160,190,0.18)"; g.lineWidth = 2; g.strokeRect(r[0] + 1, r[1] + 1, r[2] - r[0] - 2, r[3] - r[1] - 2);
  for (const [vx, vy, s] of b.vents) {
    const x = r[0] + 12 + vx * (r[2] - r[0] - 24 - s), y = r[1] + 12 + vy * (r[3] - r[1] - 24 - s);
    g.fillStyle = "rgba(60,66,84,0.9)"; g.fillRect(x, y, s, s);
    g.fillStyle = "rgba(255,255,255,0.05)"; g.fillRect(x + 2, y + 2, s - 4, 3);
  }
  if (b.roofTag) {
    g.fillStyle = "rgba(255,179,71,0.22)";
    g.font = "700 14px 'Chakra Petch', system-ui"; g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(b.roofTag, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2);
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
  RACE.fx.push({ k: "spark", x, y, t: 0, p: Array.from({ length: 10 }, () => { const a = Math.random() * Math.PI * 2; return [Math.cos(a), Math.sin(a)]; }) });
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
    m.strokeStyle = RACE.B.accent; m.lineWidth = 3; m.beginPath(); m.moveTo(ax * s + ox, ay * s + oy); m.lineTo(bx * s + ox, by * s + oy); m.stroke();
    RACE.mini = c;
  }
  g.clearRect(0, 0, cv.width, cv.height);
  g.drawImage(RACE.mini, 0, 0);
  for (const c of RACE.cars) {
    const x = RACE.me && c === RACE.me ? c.x : c.dx, y = RACE.me && c === RACE.me ? c.y : c.dy;
    if (x === undefined) continue;
    g.fillStyle = c.col; g.beginPath();
    if ((RACE.me && c === RACE.me) || (RACE.spec && RACE.specCam?.follow === c.s)) {
      const px = x * s + ox, py = y * s + oy, a = RACE.me && c === RACE.me ? c.h : (c.dh ?? c.h);
      g.moveTo(px + Math.cos(a) * 11, py + Math.sin(a) * 11);
      g.lineTo(px + Math.cos(a + 2.5) * 8, py + Math.sin(a + 2.5) * 8);
      g.lineTo(px + Math.cos(a - 2.5) * 8, py + Math.sin(a - 2.5) * 8);
      g.closePath(); g.fill(); g.strokeStyle = "#fff"; g.lineWidth = 2; g.stroke();
    } else { g.arc(x * s + ox, y * s + oy, 5.5, 0, Math.PI * 2); g.fill(); }
  }
};

RACE.drawSpecHud = function (tg) {
  const h = RACE.hud, sc = RACE.specCam, T = RACE.T;
  if (!h || !sc) return;
  const order = RACE.order(true);
  const fc = sc.follow !== null ? RACE.cars[sc.follow] : null;
  if (h.speed) {
    if (fc) {
      h.speed.hidden = false;
      h.speed.querySelector(".v").textContent = Math.round(Math.hypot(fc.dvx || 0, fc.dvy || 0) * 0.36);
    } else {
      h.speed.hidden = true;
    }
  }
  if (h.followLbl) {
    h.followLbl.textContent = fc
      ? `${sc.autoChase ? "Chase" : "Follow"}: ${fc.name} · Lap ${Math.min(RACE.laps, Math.max(1, Math.floor((fc.d || 0) / T.L) + 1))}/${RACE.laps}`
      : "Free Bird's-Eye";
  }
  const chaseBtn = RACE.root?.querySelector("#specChase");
  if (chaseBtn) {
    chaseBtn.setAttribute("aria-pressed", String(Boolean(fc && sc.autoChase)));
    chaseBtn.innerHTML = `${fc && sc.autoChase ? "Auto-Chase" : fc ? "North-Up" : "Auto-Chase"}<kbd>V</kbd>`;
  }
  h.board.innerHTML = order.map((c, k) => {
    let q;
    if (c.fin !== null) q = RACE.fmt(c.fin / 1000);
    else {
      const rate = c.rx.length > 1 ? (c.rx.length - 1) / ((c.rx[c.rx.length - 1] - c.rx[0]) / 1000) : 0;
      q = c.away ? "reconnecting" : c.gone ? "left" : c.pkt ? `${rate.toFixed(0)}/s` : "grid";
    }
    return `<button type="button" data-seat="${c.s}" class="${sc.follow === c.s ? "r-follow" : ""}" title="Follow ${esc(c.name)} (${c.s + 1})">`
      + `<span class="sw" style="background:${c.col}"></span><span class="nm">${k + 1}. ${esc(c.name)}</span><span class="q">${esc(q)}</span></button>`;
  }).join("");
  h.board.querySelectorAll("button[data-seat]").forEach((b) => {
    b.onclick = () => { RACE.followSeat(+b.dataset.seat); b.blur(); };
  });
  let count = "", cls = "count";
  if (tg < 0 && tg > -3) count = String(Math.ceil(-tg));
  else if (tg >= 0 && tg < 0.9) { count = "GO"; cls = "count go"; }
  h.count.className = cls; h.count.textContent = count;
  h.keys.textContent = tg < -0.2
    ? (RACE.isTouch()
      ? "Drag to pan, pinch to zoom, tap a car or leaderboard row to follow"
      : "Drag to pan, scroll to zoom, click a car or 1–4 to follow, V for Auto-Chase, F for Full Track")
    : "";
  if (tg < -3) h.msg.textContent = `Get ready · ${RACE.B.name}`;
  else {
    RACE.msgs = RACE.msgs.filter((m) => m.until > performance.now());
    h.msg.textContent = RACE.msgs.length ? RACE.msgs[RACE.msgs.length - 1].text : "";
  }
};

RACE.drawHud = function (tg) {
  if (RACE._camPref !== pref("raceCam")) RACE.camLabel();
  const me = RACE.me, h = RACE.hud, order = RACE.order(true), T = RACE.T;
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
  RACE.syncTouchItem();
  const leader = order[0];
  h.board.innerHTML = order.map((c, k) => {
    let q;
    if (c.fin !== null) q = RACE.fmt(c.fin / 1000);
    else if (c === me) q = k === 0 ? "leading" : `${Math.round((leader.d - c.d) / 10)} m back`;
    else {
      const rate = c.rx.length > 1 ? (c.rx.length - 1) / ((c.rx[c.rx.length - 1] - c.rx[0]) / 1000) : 0;
      q = c.away ? "reconnecting" : c.gone ? "left" : c.pkt ? `${rate.toFixed(0)}/s, ${Math.round((c.age || 0) * 1000)} ms` : "no signal";
    }
    return `<div class="${c === me ? "r-me" : ""}"><span class="sw" style="background:${c.col}"></span><span class="nm">${k + 1}. ${esc(c === me ? "You" : c.name)}</span><span class="q">${q}</span></div>`;
  }).join("");
  let count = "", cls = "count";
  if (tg < 0 && tg > -3) count = String(Math.ceil(-tg));
  else if (tg >= 0 && tg < 0.9) { count = "GO"; cls = "count go"; }
  h.count.className = cls; h.count.textContent = count;
  h.keys.textContent = tg < -0.2
    ? (RACE.isTouch()
      ? "Left thumb to steer, right thumb for Gas, Brake and Pickup"
      : "Arrows or W A S D to drive, Space to use a pickup, V to switch the camera")
    : "";
  if (tg < -3) h.msg.textContent = `Get ready · ${RACE.B.name}`;
  else {
    RACE.msgs = RACE.msgs.filter((m) => m.until > performance.now());
    h.msg.textContent = RACE.msgs.length ? RACE.msgs[RACE.msgs.length - 1].text : "";
  }
};

RACE.showResults = function (res) {
  const box = RACE.root?.querySelector(".race-results");
  if (!box) return;
  const host = !RACE.spec && RACE.room.h === S.me.addr, T = RACE.T;
  const rows = res.map((r, k) => `<tr><td>${k + 1}.</td><td><span style="color:${R_SEAT[r.s % 4]}">■</span> ${esc(!RACE.spec && r.a === S.me.addr ? "You" : r.name)}</td>
    <td class="t">${r.ft !== null && r.ft !== undefined ? RACE.fmt(r.ft / 1000) : r.gone ? "left" : `DNF, ${Math.max(0, r.d / T.L).toFixed(1)} laps`}</td></tr>`).join("");
  box.hidden = false;
  RACE.syncTouchOverlay();
  box.innerHTML = `<div class="box"><h3>Results</h3><table>${rows}</table><div class="acts">
    ${RACE.spec
      ? `<button class="gc-ghost" data-act="unwatch" data-r="${esc(RACE.rid)}">Leave</button>`
      : `${host ? `<button class="primary" data-act="start" data-r="${esc(RACE.rid)}">Rematch on a new track</button>` : `<span style="opacity:.75">${esc(GC.name(RACE.room, RACE.room.h))} can start a rematch.</span>`}
         <button class="gc-ghost" data-act="leave" data-r="${esc(RACE.rid)}">${host ? "Close room" : "Leave"}</button>`}
    </div></div>`;
  GC.wire(box);
};

// ====================================================================== poster / lobby backdrop
RACE.backdropStatic = function (w, h, bm = 0) {
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w)); c.height = Math.max(1, Math.round(h));
  const compact = h < 540 || w < 520;
  const g = c.getContext("2d"), hz = h * (compact ? 0.30 : 0.42), pal = BIOMES[bm] || BIOMES[0];
  const skies = [
    ["#07080d", "#1b1424"],
    ["#031411", "#09372e"],
    ["#5e2c1c", "#d9824c"],
    ["#2c4866", "#bfd6ea"],
    ["#2a183b", "#f07b5f"],
  ];
  const [skyTop, skyBot] = skies[bm] || skies[0];
  g.fillStyle = bm === 4 ? pal.bgSea : pal.bg; g.fillRect(0, 0, w, h);
  const sky = g.createLinearGradient(0, 0, 0, hz);
  sky.addColorStop(0, skyTop); sky.addColorStop(1, skyBot);
  g.fillStyle = sky; g.fillRect(0, 0, w, hz);
  const r = rMulberry(7 + bm * 13);
  if (bm === 0) {
    for (let x = -20; x < w + 40;) {
      const bw = 40 + r() * 90, bh = hz * (0.35 + r() * 0.6);
      g.fillStyle = `hsl(${235 + r() * 30} 18% ${7 + r() * 5}%)`; g.fillRect(x, hz - bh, bw, bh);
      for (let yy = hz - bh + 8; yy < hz - 6; yy += 9) for (let xx = x + 5; xx < x + bw - 6; xx += 8)
        if (r() < 0.22) { g.fillStyle = r() < 0.75 ? "rgba(255,190,110,0.75)" : "rgba(120,220,255,0.7)"; g.fillRect(xx, yy, 3, 4); }
      x += bw + 4 + r() * 10;
    }
  } else if (bm === 1) {
    // PCB copper trace grid and distant IC silhouettes on the horizon
    g.strokeStyle = "rgba(0,255,200,0.22)"; g.lineWidth = 1.5;
    for (let k = 0; k < 18; k++) {
      const x = r() * w, y1 = hz * (0.2 + 0.7 * r());
      g.beginPath(); g.moveTo(x, hz); g.lineTo(x, y1); g.lineTo(x + 30, y1 - 30); g.stroke();
      g.fillStyle = "#D4AF37"; g.beginPath(); g.arc(x + 30, y1 - 30, 3, 0, Math.PI * 2); g.fill();
    }
    for (let x = 20; x < w; x += 120 + r() * 80) {
      const bw = 60 + r() * 45, bh = hz * (0.22 + r() * 0.28);
      g.fillStyle = "#0b1318"; g.fillRect(x, hz - bh, bw, bh);
      g.strokeStyle = "#D4AF37"; g.lineWidth = 1; g.strokeRect(x, hz - bh, bw, bh);
    }
  } else if (bm === 2) {
    // Stepped desert mesas + ground-station dish silhouettes
    g.fillStyle = "#8C3B2B";
    for (let x = -40; x < w + 60;) {
      const mw = 90 + r() * 120, mh = hz * (0.22 + r() * 0.38);
      g.beginPath(); g.moveTo(x, hz); g.lineTo(x + mw * 0.2, hz - mh); g.lineTo(x + mw * 0.8, hz - mh); g.lineTo(x + mw, hz); g.fill();
      x += mw * (0.75 + r() * 0.4);
    }
  } else if (bm === 3) {
    // Alpine jagged snowy peaks
    for (let layer = 0; layer < 2; layer++) {
      g.fillStyle = layer ? "#2E3B4E" : "#5B7491";
      g.beginPath(); g.moveTo(0, hz);
      for (let x = 0; x <= w + 80; x += 65 + r() * 55) g.lineTo(x, hz - hz * (layer ? 0.28 + r() * 0.35 : 0.45 + r() * 0.45));
      g.lineTo(w, hz); g.closePath(); g.fill();
    }
  } else {
    // Sunset Coast setting sun + coastal ocean horizon + curved palm silhouettes
    const sg = g.createRadialGradient(w * 0.5, hz * 0.78, 8, w * 0.5, hz * 0.78, h * 0.24);
    sg.addColorStop(0, "rgba(255,209,102,0.95)"); sg.addColorStop(0.45, "rgba(247,37,133,0.45)"); sg.addColorStop(1, "rgba(247,37,133,0)");
    g.fillStyle = sg; g.beginPath(); g.arc(w * 0.5, hz * 0.78, h * 0.24, 0, Math.PI * 2); g.fill();
    g.fillStyle = "#14362D"; g.strokeStyle = "#14362D"; g.lineWidth = 4.5; g.lineCap = "round";
    for (const px of [w * 0.14, w * 0.86]) {
      const dir = px < w * 0.5 ? 1 : -1, th = Math.min(54, hz * 0.52);
      const tx0 = px + dir * 4, ty0 = hz - th;
      g.beginPath(); g.moveTo(px, hz); g.quadraticCurveTo(px + dir * 8, hz - th * 0.52, tx0, ty0); g.stroke();
      for (let f = 0; f < 5; f++) {
        const a = -Math.PI * 0.92 + (f * Math.PI * 0.84) / 4;
        const fx = tx0 + Math.cos(a) * 32, fy = ty0 + Math.sin(a) * 14 + 10;
        g.beginPath(); g.moveTo(tx0, ty0);
        g.quadraticCurveTo(tx0 + Math.cos(a) * 18, ty0 + Math.sin(a) * 18 - 8, fx, fy);
        g.quadraticCurveTo(tx0 + Math.cos(a) * 14, ty0 + Math.sin(a) * 12, tx0, ty0);
        g.fill();
      }
    }
  }
  if (bm === 4) {
    g.fillStyle = pal.bgSand; g.beginPath(); g.moveTo(0, hz); g.lineTo(w * 0.47, hz); g.lineTo(-w * 0.1, h); g.lineTo(0, h); g.fill();
  }
  g.fillStyle = pal.surface;
  g.beginPath(); g.moveTo(w * 0.47, hz); g.lineTo(w * 0.53, hz); g.lineTo(w * 1.1, h); g.lineTo(-w * 0.1, h); g.fill();
  g.strokeStyle = pal.railL; g.lineWidth = 2;
  g.beginPath(); g.moveTo(w * 0.47, hz); g.lineTo(-w * 0.1, h); g.stroke();
  g.strokeStyle = pal.railR;
  g.beginPath(); g.moveTo(w * 0.53, hz); g.lineTo(w * 1.1, h); g.stroke();
  g.globalCompositeOperation = "lighter";
  for (let k = 0; k < 6; k++) {
    const f = k / 6, y = hz + (h - hz) * (f * f) + 6, spread = 0.03 + 0.55 * f * f;
    for (const side of [-1, 1]) {
      const x = w / 2 + side * w * spread, rad = 12 + 220 * f * f;
      const gr = g.createRadialGradient(x, y, 1, x, y, rad);
      const glow = bm === 1 ? "0,255,200" : bm === 3 ? "125,211,252" : bm === 4 ? "255,209,102" : "255,179,71";
      gr.addColorStop(0, `rgba(${glow},0.85)`); gr.addColorStop(0.22, `rgba(${glow},0.32)`); gr.addColorStop(1, `rgba(${glow},0)`);
      g.fillStyle = gr; g.beginPath(); g.arc(x, y, rad, 0, Math.PI * 2); g.fill();
    }
  }
  return c;
};

RACE.backdrop = function (g, w, h, t, biomeOpt = 0) {
  const bm = Number.isInteger(biomeOpt) && biomeOpt >= 0 && biomeOpt < BIOMES.length ? biomeOpt : 0;
  const pal = BIOMES[bm];
  const compact = h < 540 || w < 520;
  const key = `${Math.round(w)}x${Math.round(h)}:${bm}`;
  if (!RACE.bd || RACE.bd.key !== key) RACE.bd = { key, img: RACE.backdropStatic(w, h, bm) };
  g.drawImage(RACE.bd.img, 0, 0, w, h);
  const hz = h * (compact ? 0.30 : 0.42);
  const sz = Math.round(h * 0.035);
  const signSets = [
    [[0.16, 0.2, "#FF2E88", "WALSH"], [0.8, 0.26, "#00E5FF", "SLOT RUSH"]],
    [[0.18, 0.22, "#D4AF37", "AD9361"], [0.8, 0.24, "#00FFC8", "ZYNQ-7020"]],
    [[0.18, 0.22, "#FF6B35", "AWGN"], [0.8, 0.24, "#F4D39E", "RF ARRAY"]],
    [[0.18, 0.22, "#FF6D00", "CARRIER"], [0.8, 0.24, "#7DD3FC", "PEAK"]],
    [[0.18, 0.22, "#FF7F50", "SUNSET"], [0.8, 0.24, "#48CAE4", "COAST"]],
  ];
  if (!compact) (signSets[bm] || signSets[0]).forEach(([sx, sy, col, txt], k) => {
    const on = Math.sin(t * 7.3 + k * 2.1) + Math.sin(t * 2.9 + k * 4.4) > -1.75;
    const spr = RACE.glowText(txt, sz, col, null, false);
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
  const rr = rMulberry(3 + bm);
  if (bm === 3) {
    g.fillStyle = "rgba(255,255,255,0.75)"; g.beginPath();
    for (let k = 0; k < 140; k++) {
      const sp = 0.5 + rr();
      const x = (rr() * w + Math.sin(t + k) * 18) % w, y = (rr() * h + t * 140 * sp) % h;
      g.moveTo(x + 2, y); g.arc(x, y, 1.5 + sp, 0, Math.PI * 2);
    }
    g.fill();
  } else if (bm === 1) {
    g.fillStyle = pal.particleCol; g.beginPath();
    for (let k = 0; k < 80; k++) {
      const sp = 0.5 + rr();
      const x = (rr() * w + t * 110 * sp) % w, y = (rr() * h + t * 75 * sp) % h;
      g.rect(x, y, 2.2, 2.2);
    }
    g.fill();
  } else if (bm === 4) {
    if (!RACE.mistSpr) RACE.sprites();
    for (let k = 0; k < 28; k++) {
      const sp = 0.4 + rr();
      const x = (rr() * w + t * 36 * sp) % (w + 120) - 60, y = (rr() * h + t * 10 * sp) % (h + 60) - 30;
      const rx = 26 + 34 * sp, ry = rx * 0.55;
      g.drawImage(RACE.mistSpr, x - rx, y - ry, rx * 2, ry * 2);
    }
  } else {
    g.strokeStyle = pal.particleCol; g.lineWidth = bm === 2 ? 1.4 : 1; g.beginPath();
    for (let k = 0; k < 150; k++) {
      const sp = 0.6 + rr();
      const x = (rr() * w + t * (bm === 2 ? 260 : 40) * sp) % w;
      const y = (rr() * h + t * (bm === 2 ? 35 : 420) * sp) % h;
      const len = 8 + 14 * sp;
      if (bm === 2) { g.moveTo(x, y); g.lineTo(x - len, y - len * 0.12); }
      else { g.moveTo(x, y); g.lineTo(x - len * 0.15, y - len); }
    }
    g.stroke();
  }
  const fade = g.createLinearGradient(0, h * (compact ? 0.24 : 0.36), 0, h);
  fade.addColorStop(0, compact ? "rgba(11,15,26,0.18)" : "rgba(11,15,26,0.08)");
  fade.addColorStop(1, compact ? "rgba(11,15,26,0.94)" : "rgba(11,15,26,0.92)");
  g.fillStyle = fade; g.fillRect(0, 0, w, h);
};

if (document.fonts) document.fonts.load("700 20px 'Chakra Petch'").then(() => RACE.glowCache.clear()).catch(() => {});

GC.mods.race = RACE;
if (GC.open && GC.snap) GC.render();
