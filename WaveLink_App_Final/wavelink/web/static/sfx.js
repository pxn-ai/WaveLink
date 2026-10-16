"use strict";
/* Sound effects for the chat and the games, synthesised with Web Audio (no sound files, works
   offline).  Settings: pref("sound") on/off, pref("vol") 0..1.  Browsers start audio only after a
   click or a key press, so the context is created on the first one. */

const SFX = { ctx: null, master: null, engine: null, last: {} };

SFX.ensure = function () {
  if (!SFX.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    SFX.ctx = new AC();
    SFX.master = SFX.ctx.createGain();
    SFX.master.connect(SFX.ctx.destination);
  }
  SFX.master.gain.value = Math.max(0, Math.min(1, pref("vol"))) * 0.8;
  if (SFX.ctx.state === "suspended") SFX.ctx.resume().catch(() => {});
  return SFX.ctx;
};

// play a named effect; 'gap' (s) stops the same sound from piling up
SFX.play = function (name, opt = {}) {
  if (!pref("sound") || !SFX.lib[name]) return;
  // before the first click or key press a browser keeps audio off (and warns about it): stay quiet
  if (!SFX.ctx && navigator.userActivation && !navigator.userActivation.hasBeenActive) return;
  const now = performance.now() / 1000;
  if (opt.gap && now - (SFX.last[name] || 0) < opt.gap) return;
  SFX.last[name] = now;
  try {
    const ctx = SFX.ensure();
    if (ctx) SFX.lib[name](ctx, SFX.master, ctx.currentTime + 0.005, opt);
  } catch (e) { /* audio is a nicety */ }
};

// ------------------------------------------------------------------ building blocks
SFX.tone = function (ctx, out, t, f0, dur, { type = "sine", gain = 0.25, f1 = null, att = 0.005, rel = null } = {}) {
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.type = type;
  o.frequency.setValueAtTime(f0, t);
  if (f1) o.frequency.exponentialRampToValueAtTime(f1, t + dur);
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(gain, t + att);
  g.gain.exponentialRampToValueAtTime(0.0001, t + (rel ?? dur));
  o.connect(g).connect(out);
  o.start(t);
  o.stop(t + (rel ?? dur) + 0.05);
};

SFX.noiseBuf = null;
SFX.noise = function (ctx, out, t, dur, { gain = 0.3, type = "bandpass", f0 = 1200, f1 = null, q = 1.2 } = {}) {
  if (!SFX.noiseBuf) {
    const n = ctx.sampleRate;
    SFX.noiseBuf = ctx.createBuffer(1, n, ctx.sampleRate);
    const d = SFX.noiseBuf.getChannelData(0);
    for (let i = 0; i < n; i++) d[i] = Math.random() * 2 - 1;
  }
  const src = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
  src.buffer = SFX.noiseBuf;
  f.type = type; f.Q.value = q;
  f.frequency.setValueAtTime(f0, t);
  if (f1) f.frequency.exponentialRampToValueAtTime(f1, t + dur);
  g.gain.setValueAtTime(gain, t);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  src.connect(f).connect(g).connect(out);
  src.start(t, Math.random() * 0.5);
  src.stop(t + dur + 0.05);
};

const N = (n) => 440 * Math.pow(2, (n - 69) / 12);       // MIDI note -> Hz

SFX.lib = {
  // chat
  send: (c, o, t) => { SFX.tone(c, o, t, 520, 0.11, { f1: 920, gain: 0.16 }); },
  receive: (c, o, t) => { SFX.tone(c, o, t, N(79), 0.16, { type: "triangle", gain: 0.2 }); SFX.tone(c, o, t + 0.1, N(84), 0.28, { type: "triangle", gain: 0.18 }); },
  urgent: (c, o, t) => { [76, 81, 88].forEach((n, i) => SFX.tone(c, o, t + i * 0.08, N(n), 0.14, { type: "square", gain: 0.07 })); },
  delivered: (c, o, t) => { SFX.tone(c, o, t, N(91), 0.07, { gain: 0.06 }); },
  error: (c, o, t) => { SFX.tone(c, o, t, 196, 0.18, { type: "square", gain: 0.06 }); SFX.tone(c, o, t + 0.12, 165, 0.22, { type: "square", gain: 0.05 }); },
  rec: (c, o, t) => { SFX.tone(c, o, t, 1200, 0.04, { type: "square", gain: 0.05 }); },
  pop: (c, o, t) => { SFX.tone(c, o, t, 660, 0.07, { f1: 990, gain: 0.08 }); },
  // UNO
  card: (c, o, t) => { SFX.noise(c, o, t, 0.09, { f0: 2400, f1: 900, gain: 0.35, q: 0.8 }); SFX.tone(c, o, t + 0.05, 140, 0.06, { gain: 0.12 }); },
  draw: (c, o, t) => { SFX.noise(c, o, t, 0.16, { f0: 900, f1: 3000, gain: 0.22, q: 0.7 }); },
  turn: (c, o, t) => { SFX.tone(c, o, t, N(81), 0.5, { gain: 0.14 }); SFX.tone(c, o, t, N(88), 0.4, { gain: 0.06 }); },
  uno: (c, o, t) => { [72, 76, 79, 84].forEach((n, i) => SFX.tone(c, o, t + i * 0.06, N(n), 0.3, { type: "triangle", gain: 0.16 })); },
  caught: (c, o, t) => { SFX.tone(c, o, t, 520, 0.4, { type: "sawtooth", f1: 130, gain: 0.08 }); },
  win: (c, o, t) => { [72, 76, 79, 84, 88].forEach((n, i) => SFX.tone(c, o, t + i * 0.09, N(n), 0.35, { type: "triangle", gain: 0.16 })); },
  lose: (c, o, t) => { SFX.tone(c, o, t, N(67), 0.3, { type: "triangle", gain: 0.14 }); SFX.tone(c, o, t + 0.22, N(62), 0.45, { type: "triangle", gain: 0.12 }); },
  nope: (c, o, t) => { SFX.tone(c, o, t, 220, 0.12, { type: "square", gain: 0.05 }); },
  // race
  beep: (c, o, t) => { SFX.tone(c, o, t, 440, 0.16, { type: "square", gain: 0.09 }); },
  go: (c, o, t) => { SFX.tone(c, o, t, 880, 0.45, { type: "square", gain: 0.1 }); },
  pickup: (c, o, t) => { [88, 93, 100].forEach((n, i) => SFX.tone(c, o, t + i * 0.045, N(n), 0.12, { gain: 0.1 })); },
  missile: (c, o, t) => { SFX.noise(c, o, t, 0.45, { f0: 400, f1: 3200, gain: 0.3, q: 2 }); },
  mine: (c, o, t) => { SFX.tone(c, o, t, 300, 0.12, { type: "square", f1: 160, gain: 0.08 }); },
  boom: (c, o, t) => { SFX.noise(c, o, t, 0.8, { type: "lowpass", f0: 1800, f1: 120, gain: 0.55, q: 0.5 }); SFX.tone(c, o, t, 110, 0.5, { f1: 40, gain: 0.3 }); },
  emp: (c, o, t) => { SFX.tone(c, o, t, 1400, 0.6, { type: "sawtooth", f1: 70, gain: 0.09 }); SFX.noise(c, o, t, 0.4, { f0: 5000, gain: 0.08, q: 4 }); },
  shield: (c, o, t) => { SFX.tone(c, o, t, N(84), 0.5, { gain: 0.08 }); SFX.tone(c, o, t, N(84) * 1.01, 0.5, { gain: 0.08 }); },
  boost: (c, o, t) => { SFX.tone(c, o, t, 180, 0.5, { type: "sawtooth", f1: 720, gain: 0.07 }); },
  wall: (c, o, t) => { SFX.tone(c, o, t, 90, 0.14, { gain: 0.25 }); SFX.noise(c, o, t, 0.12, { type: "lowpass", f0: 900, gain: 0.25 }); },
  lap: (c, o, t) => { SFX.tone(c, o, t, N(84), 0.22, { type: "triangle", gain: 0.14 }); SFX.tone(c, o, t + 0.12, N(91), 0.35, { type: "triangle", gain: 0.12 }); },
  finish: (c, o, t) => { [67, 72, 76, 79, 84].forEach((n, i) => SFX.tone(c, o, t + i * 0.11, N(n), 0.4, { type: "square", gain: 0.06 })); },
};

// ------------------------------------------------------------------ the race car's engine (continuous)
SFX.engineStart = function () {
  if (!pref("sound") || SFX.engine) return;
  const c = SFX.ensure();
  if (!c) return;
  const o1 = c.createOscillator(), o2 = c.createOscillator(), f = c.createBiquadFilter(), g = c.createGain();
  o1.type = "sawtooth"; o2.type = "sawtooth";
  o1.frequency.value = 45; o2.frequency.value = 45.7;
  f.type = "lowpass"; f.frequency.value = 380; f.Q.value = 3;
  g.gain.value = 0.0001;
  o1.connect(f); o2.connect(f); f.connect(g).connect(SFX.master);
  o1.start(); o2.start();
  SFX.engine = { o1, o2, f, g };
};
SFX.engineSet = function (speed01, throttle) {
  const e = SFX.engine;
  if (!e) return;
  const t = SFX.ctx.currentTime, f = 42 + 150 * speed01 + (throttle ? 12 : 0);
  e.o1.frequency.setTargetAtTime(f, t, 0.08);
  e.o2.frequency.setTargetAtTime(f * 1.012, t, 0.08);
  e.f.frequency.setTargetAtTime(300 + 1400 * speed01 + (throttle ? 250 : 0), t, 0.1);
  e.g.gain.setTargetAtTime(pref("sound") ? 0.035 + 0.05 * speed01 : 0.0001, t, 0.1);
};
SFX.engineStop = function () {
  const e = SFX.engine;
  if (!e) return;
  SFX.engine = null;
  const t = SFX.ctx.currentTime;
  e.g.gain.setTargetAtTime(0.0001, t, 0.05);
  e.o1.stop(t + 0.3); e.o2.stop(t + 0.3);
};

// browsers allow audio after the first gesture
["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, () => { if (pref("sound")) SFX.ensure(); }, { once: true, capture: true }));
