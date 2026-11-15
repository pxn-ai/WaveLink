"use strict";
/* Background music: four built-in loops composed here in the browser (Web Audio, no music files, so
   they work offline and carry no licences) or an audio file the station's user uploaded (an MP3,
   kept on the station only).  Each built-in track is rendered once into a buffer with an offline
   audio context; the reverb tail is folded back onto the start so the loop has no seam.  Music has
   its own volume, separate from the sound effects, and goes quiet while a voice note is recorded
   or played.  Settings: pref("music") = off | drift | carrier | night | rain | custom,
   pref("musicVol") 0..1. */

const MUSIC = {
  TRACKS: [["off", "Off", "silence"], ["drift", "Drift", "ambient pads"], ["carrier", "Carrier", "pulsing arpeggios"],
    ["night", "Night drive", "synthwave"], ["rain", "Rain lab", "rain and bells"], ["custom", "Your music", "upload…"]],
  cur: "off", node: null, el: null, elUrl: null, gain: null, buffers: {}, pending: null, duck: 1, playing: new Set(),
  SR: 44100,
};

const M_HZ = (n) => 440 * Math.pow(2, (n - 69) / 12);

// ------------------------------------------------------------------ what should be playing
MUSIC.want = function () {
  const id = pref("music");
  if (id === "custom") return S.profile?.music_url ? "custom" : "off";
  return MUSIC.TRACKS.some((t) => t[0] === id) ? id : "off";
};
MUSIC.level = () => Math.max(0, Math.min(1, pref("musicVol"))) * 0.9 * MUSIC.duck;

MUSIC.sync = async function () {
  const want = MUSIC.want();
  // browsers start audio only after a click or a key press on the page
  if (want !== "off" && navigator.userActivation && !navigator.userActivation.hasBeenActive) return;
  const ctx = want === "off" && !SFX.ctx ? null : SFX.ensure();
  if (ctx && !MUSIC.gain) {
    MUSIC.gain = ctx.createGain();
    MUSIC.gain.gain.value = 0;
    MUSIC.gain.connect(ctx.destination);
  }
  if (MUSIC.gain) MUSIC.gain.gain.setTargetAtTime(MUSIC.level(), ctx.currentTime, 0.25);
  const key = want === "custom" ? "custom:" + S.profile.music_url : want;
  if (key === MUSIC.cur) return;
  MUSIC.cur = key;
  MUSIC.stop();
  if (want === "off" || !ctx) return;
  if (want === "custom") {
    const el = new Audio(S.profile.music_url);
    el.loop = true;
    el.preload = "auto";
    el.dataset.music = "1";
    ctx.createMediaElementSource(el).connect(MUSIC.gain);
    MUSIC.el = el;
    el.play().catch((e) => toast("The music file could not be played: " + e.message));
    return;
  }
  let buf = MUSIC.buffers[want];
  if (!buf) {
    MUSIC.pending = want;
    try { buf = MUSIC.buffers[want] = await MUSIC.render(want); }
    catch (e) { toast("Music: " + e.message); return; }
    if (MUSIC.cur !== key) return;                         // the choice changed while it was being made
  }
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.loop = true;
  const fade = ctx.createGain();
  fade.gain.setValueAtTime(0.0001, ctx.currentTime);
  fade.gain.exponentialRampToValueAtTime(1, ctx.currentTime + 2.5);
  src.connect(fade).connect(MUSIC.gain);
  src.start();
  MUSIC.node = { src, fade };
};

MUSIC.stop = function () {
  const ctx = SFX.ctx;
  if (MUSIC.node && ctx) {
    const { src, fade } = MUSIC.node;
    fade.gain.cancelScheduledValues(ctx.currentTime);
    fade.gain.setTargetAtTime(0.0001, ctx.currentTime, 0.25);
    src.stop(ctx.currentTime + 1.2);
  }
  MUSIC.node = null;
  if (MUSIC.el) { const el = MUSIC.el; el.pause(); el.removeAttribute("src"); el.load(); }
  MUSIC.el = null;
};

// quieter while a voice note is recorded or any other audio on the page plays
MUSIC.setDuck = function (v) {
  MUSIC.duck = v;
  if (MUSIC.gain && SFX.ctx) MUSIC.gain.gain.setTargetAtTime(MUSIC.level(), SFX.ctx.currentTime, 0.2);
};
MUSIC.updateDuck = () => MUSIC.setDuck(MUSIC.playing.size || (typeof MEDIA !== "undefined" && MEDIA.REC.mr) ? 0.12 : 1);

// ------------------------------------------------------------------ the built-in tracks
MUSIC.render = async function (id) {
  const T = MUSIC.SONGS[id];
  const sr = MUSIC.SR, tail = 5, n = Math.round(T.len * sr);
  const o = new OfflineAudioContext(2, Math.round((T.len + tail) * sr), sr);
  // the room: two cross-fed echoes, filtered, panned left and right
  const dry = o.createGain(), wet = o.createGain();
  dry.connect(o.destination);
  wet.gain.value = T.wet ?? 0.32;
  for (const [dt, pan] of [[0.37, -0.6], [0.53, 0.6]]) {
    const d = o.createDelay(2), fb = o.createGain(), lp = o.createBiquadFilter(), p = o.createStereoPanner();
    d.delayTime.value = dt * (T.echo ?? 1); fb.gain.value = 0.42; lp.type = "lowpass"; lp.frequency.value = 2600; p.pan.value = pan;
    wet.connect(d); d.connect(lp).connect(fb).connect(d); lp.connect(p).connect(o.destination);
  }
  const out = o.createGain();
  out.connect(dry); out.connect(wet);
  let seed = T.seed || 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  T.build(o, out, rnd);
  const buf = await o.startRendering();
  // fold the tail (echoes after the loop's end) onto the start, so the loop joins without a seam
  const res = new AudioBuffer({ length: n, numberOfChannels: 2, sampleRate: sr });
  let peak = 1e-6;
  for (let c = 0; c < 2; c++) {
    const a = buf.getChannelData(c), b = new Float32Array(n);
    b.set(a.subarray(0, n));
    for (let i = n; i < a.length; i++) b[i - n] += a[i];
    for (let i = 0; i < n; i++) peak = Math.max(peak, Math.abs(b[i]));
    res.copyToChannel(b, c);
  }
  for (let c = 0; c < 2; c++) {                         // every track at the same loudness
    const b = res.getChannelData(c), k = 0.55 / peak;
    for (let i = 0; i < n; i++) b[i] *= k;
  }
  return res;
};

// one note: oscillators with an attack and a release, through an optional filter
function mNote(o, dest, t, f, dur, { type = "sine", gain = 0.1, att = 0.01, rel = 0.3, detune = 0, lp = 0, q = 0.7, voices = 1, spread = 7 } = {}) {
  const g = o.createGain();
  g.gain.setValueAtTime(0, t);
  g.gain.linearRampToValueAtTime(gain, t + att);
  g.gain.setValueAtTime(gain, Math.max(t + att, t + dur - rel));
  g.gain.linearRampToValueAtTime(0, t + dur);
  let into = g;
  if (lp) { const fl = o.createBiquadFilter(); fl.type = "lowpass"; fl.frequency.value = lp; fl.Q.value = q; fl.connect(g); into = fl; }
  g.connect(dest);
  for (let v = 0; v < voices; v++) {
    const osc = o.createOscillator();
    osc.type = type;
    osc.frequency.value = f;
    osc.detune.value = detune + (voices > 1 ? (v / (voices - 1) - 0.5) * 2 * spread : 0);
    osc.connect(into);
    osc.start(t);
    osc.stop(t + dur + 0.05);
  }
}
// a struck bell: a few inharmonic partials that die away
function mBell(o, dest, t, f, gain = 0.06, decay = 2.4) {
  [[1, 1], [2.76, 0.35], [5.4, 0.12]].forEach(([m, a]) => {
    const osc = o.createOscillator(), g = o.createGain();
    osc.frequency.value = f * m;
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(gain * a, t + 0.005);
    g.gain.exponentialRampToValueAtTime(0.0001, t + decay / m ** 0.5);
    osc.connect(g).connect(dest);
    osc.start(t); osc.stop(t + decay + 0.1);
  });
}
// filtered noise (rain, hats)
function mNoise(o, dest, t, dur, { gain = 0.05, type = "bandpass", f = 2000, q = 0.8, att = 0.002, rel = null } = {}) {
  if (!o._noise) {                                         // one 2-second noise buffer per track, reused
    o._noise = o.createBuffer(1, 2 * o.sampleRate, o.sampleRate);
    const d = o._noise.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  }
  const s = o.createBufferSource(), fl = o.createBiquadFilter(), g = o.createGain();
  s.buffer = o._noise; s.loop = true; fl.type = type; fl.frequency.value = f; fl.Q.value = q;
  g.gain.setValueAtTime(0, t);
  g.gain.linearRampToValueAtTime(gain, t + att);
  if (rel === null) g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  else { g.gain.setValueAtTime(gain, t + dur - rel); g.gain.linearRampToValueAtTime(0, t + dur); }
  s.connect(fl).connect(g).connect(dest);
  s.start(t, Math.random() * 1.9); s.stop(t + dur);
}

MUSIC.SONGS = {
  // slow pads on four chords, a low root, and a few high notes drifting through the echoes
  drift: {
    len: 32, seed: 11, wet: 0.4,
    build(o, out, rnd) {
      const chords = [[48, 52, 55, 59, 62], [45, 48, 52, 55, 59], [41, 45, 48, 52, 59], [43, 47, 50, 55, 57]];
      chords.forEach((ch, k) => {
        const t = k * 8;
        for (const n of ch) mNote(o, out, t, M_HZ(n), 9.2, { type: "triangle", gain: 0.035, att: 2.6, rel: 3.2, lp: 1100, voices: 2, spread: 6 });
        mNote(o, out, t, M_HZ(ch[0] - 12), 8.6, { gain: 0.08, att: 1.5, rel: 2.5 });
      });
      const penta = [72, 74, 76, 79, 81, 84, 86];
      for (let t = 0.7; t < 31; t += 1.2 + rnd() * 1.6) mBell(o, out, t, M_HZ(penta[Math.floor(rnd() * penta.length)]), 0.035, 2.8);
    },
  },
  // a quiet carrier: sixteenth-note arpeggios that echo, half-note bass, a soft pad
  carrier: {
    len: 20, seed: 23, wet: 0.36, echo: 1.27,
    build(o, out, rnd) {
      const beat = 0.625, prog = [[57, 60, 64, 69], [53, 57, 60, 65], [48, 52, 55, 60], [55, 59, 62, 67]];
      prog.forEach((ch, k) => {
        const t0 = k * 8 * beat;                           // two bars each (20 s in all)
        const pat = [0, 1, 2, 3, 2, 1, 2, 3];
        for (let i = 0; i < 32; i++) {
          const n = ch[pat[i % 8]] + (i % 16 >= 8 ? 12 : 0);
          mNote(o, out, t0 + i * beat / 4, M_HZ(n), 0.22, { type: i % 2 ? "sine" : "triangle", gain: 0.045 + 0.02 * (i % 4 === 0), att: 0.005, rel: 0.18 });
        }
        for (let b = 0; b < 2; b++) mNote(o, out, t0 + b * 2.5, M_HZ(ch[0] - 24), 2.4, { gain: 0.11, att: 0.05, rel: 0.6 });
        for (const n of ch) mNote(o, out, t0, M_HZ(n - 12), 5.2, { type: "sawtooth", gain: 0.012, att: 1.2, rel: 1.6, lp: 700, voices: 2 });
      });
    },
  },
  // night drive: pulsing bass, wide chords, a soft beat (for the race)
  night: {
    len: 19.2, seed: 5, wet: 0.28, echo: 0.81,
    build(o, out, rnd) {
      const beat = 0.6, prog = [[45, 57, 60, 64], [41, 53, 57, 60], [48, 55, 60, 64], [43, 55, 59, 62]];
      prog.forEach((ch, k) => {
        const t0 = k * 8 * beat;
        for (let i = 0; i < 16; i++) {                      // eighth-note bass
          mNote(o, out, t0 + i * beat / 2, M_HZ(ch[0] - (i % 4 === 3 ? 0 : 12)), 0.28, { type: "sawtooth", gain: 0.05, att: 0.005, rel: 0.2, lp: 520, q: 4 });
        }
        for (const n of ch.slice(1)) mNote(o, out, t0, M_HZ(n), 4.9, { type: "sawtooth", gain: 0.014, att: 0.8, rel: 1.2, lp: 1500, voices: 3, spread: 10 });
        for (let b = 0; b < 8; b++) {
          const t = t0 + b * beat;
          if (b % 2 === 0) {                                  // kick
            const osc = o.createOscillator(), g = o.createGain();
            osc.frequency.setValueAtTime(120, t); osc.frequency.exponentialRampToValueAtTime(42, t + 0.18);
            g.gain.setValueAtTime(0.22, t); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
            osc.connect(g).connect(out); osc.start(t); osc.stop(t + 0.35);
          } else mNoise(o, out, t, 0.18, { gain: 0.05, f: 1800, q: 0.6 });   // soft snare
          mNoise(o, out, t + beat / 2, 0.05, { gain: 0.025, type: "highpass", f: 7000 });   // hat
        }
        const arp = [ch[1] + 12, ch[2] + 12, ch[3] + 12, ch[2] + 12];
        for (let i = 0; i < 8; i++) if (rnd() < 0.7) mNote(o, out, t0 + 2.4 + i * beat / 2, M_HZ(arp[i % 4]), 0.25, { type: "square", gain: 0.012, rel: 0.2, lp: 2400 });
      });
    },
  },
  // rain on the lab window and a few bells
  rain: {
    len: 30, seed: 31, wet: 0.45,
    build(o, out, rnd) {
      mNoise(o, out, 0, 35, { gain: 0.06, f: 1400, q: 0.35, att: 0.01, rel: 0.01 });
      mNoise(o, out, 0, 35, { gain: 0.025, type: "highpass", f: 5000, att: 0.01, rel: 0.01 });
      for (let i = 0; i < 140; i++) mNoise(o, out, rnd() * 30, 0.03, { gain: 0.03 + 0.05 * rnd(), type: "highpass", f: 3000 + 4000 * rnd() });
      const penta = [60, 62, 64, 67, 69, 72, 74, 76];
      for (let t = 0.5; t < 29; t += 1.6 + rnd() * 2.2) {
        mBell(o, out, t, M_HZ(penta[Math.floor(rnd() * penta.length)]), 0.07, 3.2);
        if (rnd() < 0.35) mBell(o, out, t + 0.18, M_HZ(penta[Math.floor(rnd() * penta.length)] + 12), 0.03, 2.4);
      }
      for (const [t, n] of [[0, 36], [10, 41], [20, 43]]) mNote(o, out, t, M_HZ(n), 10.5, { gain: 0.05, att: 3, rel: 3.5 });
    },
  },
};

// ------------------------------------------------------------------ wiring
document.addEventListener("DOMContentLoaded", () => {
  // start once the page may make sound
  ["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, () => setTimeout(MUSIC.sync, 0), { capture: true }));
  // voice notes and other audio on the page: music steps back while they play
  document.addEventListener("play", (e) => { if (e.target.tagName === "AUDIO" && !e.target.dataset.music) { MUSIC.playing.add(e.target); MUSIC.updateDuck(); } }, true);
  for (const ev of ["pause", "ended", "emptied"]) {
    document.addEventListener(ev, (e) => { if (MUSIC.playing.delete(e.target)) MUSIC.updateDuck(); }, true);
  }
});
