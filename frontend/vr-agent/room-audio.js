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

    async function load(name) {
      if (state.buffers[name] || state.failed[name]) return state.buffers[name] || null;
      const ctx = ensureContext();
      if (!ctx) return null;
      try {
        const res = await fetch(TRACKS[name], { cache: "force-cache" });
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
      },
      setPaused(paused) {
        paused = !!paused;
        if (state.paused === paused) return;
        state.paused = paused;
        if (state.started) apply(1.2);
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
