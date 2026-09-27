/*
 * VR Agent Room Audio Director: background music under the characters.
 *
 * - Two looping tracks made once with ElevenLabs Music and saved as files
 *   (./audio/bgm_room.mp3 and ./audio/bgm_game.mp3). Playing them costs
 *   nothing: no API request is ever made from here.
 * - Web Audio loops the decoded tracks without a gap and crossfades between
 *   the room track and the game track when a game starts or ends.
 * - Music ducks while a character speaks and comes back after the line.
 * - Volumes come from room.yaml (music:) and can be overridden in the URL
 *   with &bgm=0-100 (0 turns the music off).
 * - Nothing here depends on the page layout, so resizing or rotating the
 *   viewport never restarts the music.
 */
(() => {
  "use strict";

  const TRACKS = { room: "./audio/bgm_room.mp3", game: "./audio/bgm_game.mp3" };
  // World sounds (made once, see scripts/adventure/build_audio.py): spatial
  // one-shots ("owl", "magic") and looping ambience beds ("forest_night").
  const WORLD_SFX_URL = (name) => `./audio/world/${name}.mp3`;
  const AMBIENCE_URL = (name) => `./audio/world/amb_${name}.mp3`;
  const SFX_MIN_GAP_MS = 350;
  const MAX_SFX_VOICES = 8;
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  function createAudioDirector(options) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const state = {
      ctx: null,
      master: null,
      buffers: {},
      voices: {}, // track name -> {source, gain}
      track: null, // what should be playing: "room" | "game" | null
      ducked: false,
      paused: false,
      started: false,
      failed: {},
      ambience: {}, // name -> {source, gain}
      ambienceWanted: [],
      sfxLast: {},
      sfxVoices: 0,
      sfxPlayed: 0,
    };

    function settings() {
      const s = options.settings() || {};
      const override = options.override();
      const base = override != null ? override : Number(s.volume);
      return {
        room: clamp(Number.isFinite(base) ? base : 0.14, 0, 1),
        game: clamp(
          override != null ? override * 1.15 : Number.isFinite(Number(s.game_volume)) ? Number(s.game_volume) : 0.17,
          0,
          1
        ),
        duck: clamp(Number.isFinite(Number(s.duck_to)) ? Number(s.duck_to) : 0.35, 0, 1),
        fade: clamp(Number(s.crossfade_seconds) || 1.6, 0.2, 6),
        enabled: s.enabled !== false && (override == null || override > 0),
      };
    }

    function ensureContext() {
      if (state.ctx || !Ctx) return state.ctx;
      try {
        state.ctx = new Ctx();
        state.master = state.ctx.createGain();
        state.master.gain.value = 1;
        state.master.connect(state.ctx.destination);
      } catch (err) {
        state.ctx = null;
      }
      return state.ctx;
    }

    async function load(name, url) {
      if (state.buffers[name] || state.failed[name]) return state.buffers[name] || null;
      const ctx = ensureContext();
      if (!ctx) return null;
      try {
        const res = await fetch(url || TRACKS[name], { cache: "force-cache" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.arrayBuffer();
        state.buffers[name] = await new Promise((resolve, reject) => ctx.decodeAudioData(data, resolve, reject));
        return state.buffers[name];
      } catch (err) {
        state.failed[name] = String(err && err.message ? err.message : err);
        return null;
      }
    }

    function targetGain(name) {
      const s = settings();
      if (!s.enabled || state.paused || state.track !== name) return 0;
      return (name === "game" ? s.game : s.room) * (state.ducked ? s.duck : 1);
    }

    function ramp(gainNode, value, seconds) {
      const ctx = state.ctx;
      const t = ctx.currentTime;
      gainNode.gain.cancelScheduledValues(t);
      gainNode.gain.setValueAtTime(gainNode.gain.value, t);
      gainNode.gain.linearRampToValueAtTime(value, t + Math.max(0.02, seconds));
    }

    async function startVoice(name) {
      if (state.voices[name]) return state.voices[name];
      const buffer = await load(name);
      if (!buffer || state.voices[name]) return state.voices[name] || null;
      const ctx = state.ctx;
      const gain = ctx.createGain();
      gain.gain.value = 0;
      gain.connect(state.master);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.loop = true;
      source.connect(gain);
      // Start the game track from the top, the room track anywhere, so
      // restarts of the page do not always begin with the same bar.
      const offset = name === "room" ? Math.random() * buffer.duration : 0;
      source.start(0, offset);
      state.voices[name] = { source, gain };
      return state.voices[name];
    }

    function stopVoiceLater(name, seconds) {
      const voice = state.voices[name];
      if (!voice) return;
      setTimeout(() => {
        if (state.track === name || state.voices[name] !== voice) return;
        try {
          voice.source.stop();
        } catch (_) {}
        voice.gain.disconnect();
        delete state.voices[name];
      }, (seconds + 0.3) * 1000);
    }

    async function apply(fadeSeconds) {
      if (!ensureContext()) return;
      const s = settings();
      const fade = fadeSeconds != null ? fadeSeconds : s.fade;
      if (state.track && s.enabled && !state.paused) await startVoice(state.track);
      for (const name of Object.keys(state.voices)) {
        ramp(state.voices[name].gain, targetGain(name), fade);
        if (name !== state.track) stopVoiceLater(name, fade);
      }
    }

    function sfxVolume() {
      const v = options.sfxVolume ? Number(options.sfxVolume()) : 0.35;
      return clamp(Number.isFinite(v) ? v : 0.35, 0, 1);
    }

    function ambienceGain() {
      const s = options.settings() || {};
      const v = Number.isFinite(Number(s.ambience_volume)) ? Number(s.ambience_volume) : 0.22;
      if (state.paused || !settings().enabled) return 0;
      return clamp(v, 0, 1) * (state.ducked ? 0.55 : 1);
    }

    async function playSfx(name, pan) {
      const now = performance.now();
      if (now - (state.sfxLast[name] || 0) < SFX_MIN_GAP_MS || state.sfxVoices >= MAX_SFX_VOICES) return false;
      state.sfxLast[name] = now;
      const buffer = await load("sfx:" + name, WORLD_SFX_URL(name));
      const ctx = state.ctx;
      if (!buffer || !ctx || state.paused) return false;
      const gain = ctx.createGain();
      gain.gain.value = sfxVolume() * (state.ducked ? 0.6 : 1); // voices always win
      let tail = gain;
      if (ctx.createStereoPanner) {
        const panner = ctx.createStereoPanner();
        panner.pan.value = clamp(Number(pan) || 0, -1, 1);
        gain.connect(panner);
        tail = panner;
      }
      tail.connect(state.master);
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(gain);
      state.sfxVoices += 1;
      state.sfxPlayed += 1;
      source.onended = () => {
        state.sfxVoices = Math.max(0, state.sfxVoices - 1);
        try {
          tail.disconnect();
          gain.disconnect();
        } catch (_) {}
      };
      source.start();
      return true;
    }

    async function applyAmbience(fade) {
      if (!ensureContext()) return;
      for (const name of state.ambienceWanted) {
        if (state.ambience[name]) continue;
        const buffer = await load("amb:" + name, AMBIENCE_URL(name));
        if (!buffer || state.ambience[name] || !state.ambienceWanted.includes(name)) continue;
        const gain = state.ctx.createGain();
        gain.gain.value = 0;
        gain.connect(state.master);
        const source = state.ctx.createBufferSource();
        source.buffer = buffer;
        source.loop = true;
        source.connect(gain);
        source.start(0, Math.random() * buffer.duration);
        state.ambience[name] = { source, gain };
      }
      const target = ambienceGain();
      for (const [name, voice] of Object.entries(state.ambience)) {
        const wanted = state.ambienceWanted.includes(name);
        ramp(voice.gain, wanted ? target : 0, fade);
        if (!wanted) {
          setTimeout(() => {
            if (state.ambienceWanted.includes(name) || state.ambience[name] !== voice) return;
            try {
              voice.source.stop();
            } catch (_) {}
            voice.gain.disconnect();
            delete state.ambience[name];
          }, (fade + 0.3) * 1000);
        }
      }
    }

    function unlock() {
      const ctx = ensureContext();
      if (ctx && ctx.state === "suspended") ctx.resume().catch(() => {});
    }
    for (const evt of ["pointerdown", "keydown", "touchstart"]) {
      window.addEventListener(evt, unlock, { passive: true });
    }

    return {
      start() {
        if (state.started) return;
        state.started = true;
        state.track = "room";
        unlock();
        load("game"); // warm the game track so a game starts with music at once
        apply(2.5);
      },
      setGame(active) {
        const next = active ? "game" : "room";
        if (state.track === next) return;
        state.track = next;
        if (state.started) apply();
      },
      setSpeaking(speaking) {
        speaking = !!speaking;
        if (state.ducked === speaking) return;
        state.ducked = speaking;
        if (state.started) apply(speaking ? 0.25 : 0.9);
        applyAmbience(speaking ? 0.25 : 0.9);
      },
      setPaused(paused) {
        paused = !!paused;
        if (state.paused === paused) return;
        state.paused = paused;
        if (state.started) apply(1.2);
        applyAmbience(1.2);
      },
      playSfx(name, pan) {
        return playSfx(name, pan).catch(() => false);
      },
      setAmbience(names, fade) {
        const list = (Array.isArray(names) ? names : []).filter((n) => /^[a-z_]{1,24}$/.test(n)).slice(0, 3);
        state.ambienceWanted = list;
        applyAmbience(fade == null ? 2.5 : fade).catch(() => {});
      },
      refresh() {
        if (state.started) apply(0.4);
      },
      state() {
        const out = {
          track: state.track,
          ducked: state.ducked,
          paused: state.paused,
          context: state.ctx ? state.ctx.state : "none",
          loaded: Object.keys(state.buffers),
          ambience: Object.keys(state.ambience),
          sfxPlayed: state.sfxPlayed,
          sfxVoices: state.sfxVoices,
          failed: { ...state.failed },
          gains: {},
        };
        for (const [name, voice] of Object.entries(state.voices)) out.gains[name] = Number(voice.gain.gain.value.toFixed(3));
        return out;
      },
    };
  }

  window.VRRoomAudio = Object.freeze({ createAudioDirector, tracks: { ...TRACKS } });
})();
