/*
 * VR Agent frontend layer for Open-LLM-VTuber.
 *
 * Loaded as a classic script before the prebuilt app bundle so it can observe
 * the app's WebSocket. It does not modify the bundle.
 *
 * Modes
 *   /              normal development UI, unchanged, plus the comment card
 *   /?mode=live    livestream presentation for OBS: character + room only,
 *                  LIVE badge, state line, selected viewer comment
 *   Extra flags (livestream mode): &subtitles=1  &card=0  &status=0  &idle=0
 *
 * Responsibilities
 *   - render the selected viewer comment safely (textContent only)
 *   - play viewer-requested actions through an allowlist registry sent by the
 *     backend, in sync with the start of the spoken reply
 *   - schedule natural idle motions while the character is not speaking
 *   - keep the microphone off in livestream mode
 */
(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const LIVE = params.get("mode") === "live";
  const FLAGS = {
    subtitles: LIVE && params.get("subtitles") === "1",
    card: params.get("card") !== "0",
    status: params.get("status") !== "0",
    idle: params.get("idle") !== "0",
  };
  if (LIVE) {
    document.documentElement.setAttribute("data-vr-mode", "live");
    document.title = "VR Agent Live";
  }

  const log = (...args) => console.log("[VR Agent]", ...args);

  // -------------------------------------------------------------------------
  // Shared state
  // -------------------------------------------------------------------------
  const state = {
    settings: {
      overlay_title: "VR AGENT",
      show_comment_card: true,
      show_state_indicator: true,
      comment_card_max_chars: 180,
      idle_motions_enabled: true,
      idle_min_seconds: 10,
      idle_max_seconds: 15,
      idle_in_dev_mode: true,
    },
    capabilities: null, // { actions: {name: {...}}, idle_group, ... }
    modelInfo: null,
    phase: "listening",
    conversationActive: false,
    lastAudioAt: 0,
    motionBusyUntil: 0,
    pendingAction: null, // { name, deadline }
    lastIdle: [],
    lastLargeIdleAt: Date.now(), // no big spell in the first minutes after start
    cardId: null,
  };

  // -------------------------------------------------------------------------
  // Text safety: viewer text is only ever assigned via textContent.
  // -------------------------------------------------------------------------
  const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]/g;
  function safeText(value, max) {
    let text = String(value == null ? "" : value).replace(/\s+/g, " ").replace(CONTROL_CHARS, "").trim();
    if (max && text.length > max) text = text.slice(0, max - 1).trimEnd() + "…";
    return text;
  }

  // -------------------------------------------------------------------------
  // Overlay DOM
  // -------------------------------------------------------------------------
  const ui = {};
  function el(tag, className, parent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  function buildOverlay() {
    if (ui.root) return;
    ui.backdrop = el("div", "", document.body);
    ui.backdrop.id = "vr-agent-backdrop";

    ui.root = el("div", "", document.body);
    ui.root.id = "vr-agent-overlay";
    ui.root.setAttribute("aria-hidden", "true");

    ui.status = el("div", "vra-status", ui.root);
    const badge = el("div", "vra-badge", ui.status);
    el("span", "vra-dot", badge);
    el("span", "vra-live", badge).textContent = "Live";
    el("span", "vra-sep", badge);
    ui.title = el("span", "vra-title", badge);
    ui.title.textContent = state.settings.overlay_title;
    ui.state = el("div", "vra-state", ui.status);
    ui.stateText = el("span", "", ui.state);
    const dots = el("span", "vra-state-dots", ui.state);
    el("i", "", dots);
    el("i", "", dots);
    el("i", "", dots);
    if (!FLAGS.status) ui.status.style.display = "none";

    ui.card = el("div", "vra-card", ui.root);
    const head = el("div", "vra-card-head", ui.card);
    ui.cardAuthor = el("div", "vra-card-author", head);
    ui.cardAmount = el("div", "vra-card-amount", head);
    ui.cardAmount.hidden = true;
    ui.cardMessage = el("div", "vra-card-message", ui.card);

    ui.caption = el("div", "vra-caption", ui.root);
    renderPhase();
  }

  const PHASE_TEXT = {
    listening: "Listening to chat",
    thinking: "Thinking",
    responding: "Responding",
  };

  function renderPhase() {
    if (!ui.state) return;
    const phase = PHASE_TEXT[state.phase] ? state.phase : "listening";
    ui.state.dataset.phase = phase;
    ui.stateText.textContent = PHASE_TEXT[phase];
    // No "Thinking" label on stream; the line reappears once she answers.
    ui.state.hidden = !state.settings.show_state_indicator || phase === "thinking";
  }

  let cardHideTimer = null;
  function showCard(payload) {
    if (!FLAGS.card || !state.settings.show_comment_card || !ui.card) return;
    const max = Math.max(20, Number(state.settings.comment_card_max_chars) || 180);
    const author = safeText(payload.author, 60) || "Viewer";
    ui.cardAuthor.textContent = author.startsWith("@") ? author : `@${author}`;
    ui.cardMessage.textContent = safeText(payload.message, max);
    const amount = safeText(payload.amount, 30);
    ui.cardAmount.textContent = amount;
    ui.cardAmount.hidden = !amount;
    ui.card.classList.toggle("vra-paid", !!payload.paid);
    state.cardId = payload.id || null;
    ui.card.classList.add("vra-visible");
    clearTimeout(cardHideTimer);
    // Safety net in case the "done" signal is lost with a dropped socket.
    cardHideTimer = setTimeout(hideCard, 60000);
  }

  function hideCard(payload) {
    if (!ui.card) return;
    if (payload && payload.id && state.cardId && payload.id !== state.cardId) return;
    clearTimeout(cardHideTimer);
    ui.card.classList.remove("vra-visible");
    state.cardId = null;
  }

  let captionTimer = null;
  function showCaption(text) {
    if (!FLAGS.subtitles || !ui.caption) return;
    const clean = safeText(text, 220);
    if (!clean) return;
    ui.caption.textContent = clean;
    ui.caption.classList.add("vra-visible");
    clearTimeout(captionTimer);
    captionTimer = setTimeout(() => ui.caption.classList.remove("vra-visible"), 2500 + clean.length * 60);
  }

  // -------------------------------------------------------------------------
  // Livestream stage: full-screen Live2D canvas over the current background
  // -------------------------------------------------------------------------
  let lastBgSrc = "";
  function syncStage() {
    if (!LIVE) return;
    const wrapper = document.getElementById("live2d-internal-wrapper");
    const root = document.getElementById("root");
    if (wrapper && root) {
      let stage = wrapper;
      while (stage.parentElement && stage.parentElement !== root) stage = stage.parentElement;
      if (stage.parentElement === root && !stage.hasAttribute("data-vr-stage")) {
        root.querySelectorAll("[data-vr-stage]").forEach((n) => n.removeAttribute("data-vr-stage"));
        stage.setAttribute("data-vr-stage", "");
        window.dispatchEvent(new Event("resize"));
      }
    }
    // Mirror the room background the app is currently using.
    if (root && ui.backdrop) {
      let best = null;
      let bestArea = 0;
      root.querySelectorAll("img").forEach((img) => {
        const src = img.currentSrc || img.src || "";
        if (!src) return;
        const score = (src.includes("/bg/") ? 1e9 : 0) + (img.naturalWidth || 0) * (img.naturalHeight || 0);
        if (score > bestArea) {
          bestArea = score;
          best = src;
        }
      });
      if (best && best !== lastBgSrc) {
        lastBgSrc = best;
        try {
          ui.backdrop.style.backgroundImage = `url("${encodeURI(decodeURI(best))}")`;
        } catch (_) {
          ui.backdrop.style.backgroundImage = "";
        }
      }
    }
  }

  function watchStage() {
    if (!LIVE) return;
    const root = document.getElementById("root");
    if (!root) return;
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(() => {
        scheduled = false;
        syncStage();
      });
    });
    observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["src"] });
    syncStage();
    // Once the canvas settles, make sure the renderer picked up the new size.
    [300, 1200, 3000].forEach((ms) => setTimeout(() => window.dispatchEvent(new Event("resize")), ms));
  }

  // -------------------------------------------------------------------------
  // Live2D control through the app's own adapter
  // -------------------------------------------------------------------------
  const PRIORITY_IDLE = 2; // normal: never interrupts a forced motion
  const PRIORITY_REQUESTED = 3; // force: viewer requests win over idle

  function adapter() {
    try {
      const a = window.getLAppAdapter && window.getLAppAdapter();
      return a && a.getModel && a.getModel() ? a : null;
    } catch (_) {
      return null;
    }
  }

  let expressionRevertTimer = null;
  function neutralExpressionName(a) {
    try {
      const idx = state.modelInfo && state.modelInfo.emotionMap ? state.modelInfo.emotionMap.neutral : 0;
      return a.getExpressionName(Number.isInteger(idx) ? idx : 0);
    } catch (_) {
      return null;
    }
  }

  // Executes a registry action. `name` must exist in the capability list the
  // backend sent; group, index and expression come only from that list and
  // are checked against what the loaded model reports.
  function playAction(name, source) {
    const caps = state.capabilities;
    const action = caps && caps.actions && Object.prototype.hasOwnProperty.call(caps.actions, name) ? caps.actions[name] : null;
    if (!action) {
      log("ignored unknown action", name);
      return false;
    }
    const a = adapter();
    if (!a) return false;

    if (action.kind === "motion") {
      const group = typeof action.group === "string" ? action.group : null;
      const index = Number(action.index);
      if (group === null || !Number.isInteger(index) || index < 0) return false;
      if (!(a.getMotionCount(group) > index)) {
        log("model does not have motion", group, index);
        return false;
      }
      a.startMotion(group, index, source === "idle" ? PRIORITY_IDLE : PRIORITY_REQUESTED);
      const durationMs = Math.max(1000, (Number(action.duration) || 3) * 1000);
      state.motionBusyUntil = Date.now() + durationMs;
      log(`${source} motion`, name);
      return true;
    }

    if (action.kind === "expression") {
      const count = typeof a.getExpressionCount === "function" ? a.getExpressionCount() : 0;
      const idx = Number(action.expression_index);
      if (!Number.isInteger(idx) || idx < 0 || idx >= count) return false;
      const exprName = a.getExpressionName(idx);
      if (exprName !== action.expression) return false;
      a.setExpression(exprName);
      clearTimeout(expressionRevertTimer);
      const holdMs = Math.max(1000, (Number(action.hold) || 4) * 1000);
      expressionRevertTimer = setTimeout(() => {
        const current = adapter();
        const neutral = current && neutralExpressionName(current);
        if (neutral) current.setExpression(neutral);
      }, holdMs);
      log(`${source} expression`, name);
      return true;
    }
    return false;
  }

  // -------------------------------------------------------------------------
  // Viewer-requested actions: queued, then played when the reply audio starts
  // -------------------------------------------------------------------------
  const PENDING_TIMEOUT_MS = 20000;
  let pendingTimer = null;

  function queueRequestedAction(name, sync) {
    if (sync === "now") {
      playAction(name, "requested");
      return;
    }
    state.pendingAction = { name, deadline: Date.now() + PENDING_TIMEOUT_MS };
    clearTimeout(pendingTimer);
    // If no audio ever starts (TTS failure), still honour the request.
    pendingTimer = setTimeout(flushPendingAction, PENDING_TIMEOUT_MS);
  }

  function flushPendingAction() {
    clearTimeout(pendingTimer);
    const pending = state.pendingAction;
    state.pendingAction = null;
    if (pending) playAction(pending.name, "requested");
  }

  // -------------------------------------------------------------------------
  // Idle scheduler
  // -------------------------------------------------------------------------
  let idleTimer = null;

  function idleAllowed() {
    return (
      FLAGS.idle &&
      state.settings.idle_motions_enabled &&
      (LIVE || state.settings.idle_in_dev_mode) &&
      state.capabilities &&
      Object.values(state.capabilities.actions || {}).some((a) => a.idle && a.kind === "motion")
    );
  }

  function characterBusy() {
    const now = Date.now();
    if (state.conversationActive) return true;
    if (state.phase !== "listening") return true;
    if (now - state.lastAudioAt < 2500) return true;
    if (state.pendingAction) return true;
    if (now < state.motionBusyUntil) return true;
    // The model's looping Idle motion always keeps its motion manager busy, so
    // our own duration bookkeeping decides when a gesture has finished.
    return !adapter();
  }

  function pickIdleAction() {
    const caps = state.capabilities;
    const large = new Set(caps.large_motion_actions || []);
    const gapMs = (Number(caps.large_motion_min_gap_seconds) || 90) * 1000;
    let pool = Object.values(caps.actions).filter((a) => a.idle && a.kind === "motion" && a.idle_weight > 0);
    if (pool.length > 1) pool = pool.filter((a) => a.name !== state.lastIdle[state.lastIdle.length - 1]);
    if (pool.length > 2) pool = pool.filter((a) => !state.lastIdle.includes(a.name));
    const now = Date.now();
    const calm = pool.filter((a) => !large.has(a.name) || now - state.lastLargeIdleAt > gapMs);
    if (calm.length) pool = calm;
    const total = pool.reduce((sum, a) => sum + a.idle_weight, 0);
    let cursor = Math.random() * total;
    for (const a of pool) {
      if (cursor < a.idle_weight) return a;
      cursor -= a.idle_weight;
    }
    return pool[pool.length - 1] || null;
  }

  function scheduleIdle(delayMs) {
    clearTimeout(idleTimer);
    if (!idleAllowed()) return;
    const min = Number(state.settings.idle_min_seconds) || 10;
    const max = Math.max(min + 1, Number(state.settings.idle_max_seconds) || 15);
    const wait = delayMs != null ? delayMs : (min + Math.random() * (max - min)) * 1000;
    idleTimer = setTimeout(idleTick, wait);
  }

  function idleTick() {
    if (!idleAllowed()) return;
    if (characterBusy()) {
      // Try again shortly after the character is free, with a little jitter.
      scheduleIdle(2500 + Math.random() * 2500);
      return;
    }
    const action = pickIdleAction();
    if (action && playAction(action.name, "idle")) {
      state.lastIdle.push(action.name);
      if (state.lastIdle.length > 2) state.lastIdle.shift();
      if ((state.capabilities.large_motion_actions || []).includes(action.name)) {
        state.lastLargeIdleAt = Date.now();
      }
      const durationMs = (Number(action.duration) || 3) * 1000;
      scheduleIdle(durationMs + (Number(state.settings.idle_min_seconds) || 10) * 1000 * (0.8 + Math.random() * 0.5));
      return;
    }
    scheduleIdle();
  }

  // -------------------------------------------------------------------------
  // Messages from the backend
  // -------------------------------------------------------------------------
  const VR_TYPES = new Set(["vr-agent-config", "vr-agent-state", "vr-agent-action", "youtube-live-selected-message"]);

  function onServerMessage(payload) {
    if (!payload || typeof payload !== "object") return;
    switch (payload.type) {
      case "set-model-and-conf":
        state.modelInfo = payload.model_info || null;
        break;
      case "vr-agent-config":
        if (payload.settings && typeof payload.settings === "object") {
          Object.assign(state.settings, payload.settings);
        }
        state.capabilities = payload.capabilities || null;
        if (payload.phase) state.phase = payload.phase;
        if (ui.title) ui.title.textContent = safeText(state.settings.overlay_title, 40) || "VR AGENT";
        renderPhase();
        scheduleIdle();
        break;
      case "vr-agent-state":
        if (PHASE_TEXT[payload.phase]) {
          state.phase = payload.phase;
          renderPhase();
        }
        break;
      case "vr-agent-action":
        if (typeof payload.action === "string") {
          clearTimeout(idleTimer);
          queueRequestedAction(payload.action, payload.sync);
          scheduleIdle();
        }
        break;
      case "youtube-live-selected-message":
        if (payload.active) showCard(payload);
        else hideCard(payload);
        break;
      case "control":
        if (payload.text === "conversation-chain-start") state.conversationActive = true;
        if (payload.text === "conversation-chain-end") {
          state.conversationActive = false;
          if (state.pendingAction) flushPendingAction();
          scheduleIdle(3000 + Math.random() * 3000);
        }
        if (payload.text === "interrupt") state.conversationActive = false;
        break;
      case "audio":
        state.lastAudioAt = Date.now();
        break;
      default:
        break;
    }
  }

  // Messages the app sends: audio playback start is the sync point for actions.
  function onClientMessage(payload) {
    if (!payload || typeof payload !== "object") return;
    if (payload.type === "audio-play-start") {
      state.lastAudioAt = Date.now();
      if (payload.display_text && payload.display_text.text) showCaption(payload.display_text.text);
      if (state.pendingAction) {
        // Run after the app applied its own per-sentence expression.
        setTimeout(flushPendingAction, 0);
      }
    }
  }

  // Livestream mode: nothing may switch the microphone on.
  function blockedForApp(payload) {
    if (!payload || typeof payload !== "object") return false;
    if (VR_TYPES.has(payload.type)) return true;
    if (LIVE && payload.type === "control" && payload.text === "start-mic") return true;
    return false;
  }

  // -------------------------------------------------------------------------
  // WebSocket observation
  // -------------------------------------------------------------------------
  const NativeWebSocket = window.WebSocket;

  function parse(data) {
    if (typeof data !== "string" || data.length > 20000000) return null;
    try {
      return JSON.parse(data);
    } catch (_) {
      return null;
    }
  }

  function VRAgentWebSocket(...args) {
    const socket = new NativeWebSocket(...args);
    const isAppSocket = String(args[0] || "").includes("/client-ws");
    if (!isAppSocket) return socket;

    socket.addEventListener("message", (event) => onServerMessage(parse(event.data)));
    socket.addEventListener("open", () => {
      try {
        NativeWebSocket.prototype.send.call(socket, JSON.stringify({ type: "vr-agent-hello", mode: LIVE ? "live" : "dev" }));
      } catch (_) {}
    });

    // Filter VR Agent messages (and start-mic in livestream mode) out of the
    // app's own handler; they are handled here.
    let appHandler = null;
    Object.defineProperty(socket, "onmessage", {
      configurable: true,
      get: () => appHandler,
      set: (fn) => {
        appHandler = typeof fn === "function" ? fn : null;
      },
    });
    socket.addEventListener("message", (event) => {
      if (!appHandler) return;
      if (blockedForApp(parse(event.data))) return;
      appHandler.call(socket, event);
    });

    const nativeSend = socket.send.bind(socket);
    socket.send = (data) => {
      onClientMessage(parse(data));
      return nativeSend(data);
    };
    return socket;
  }
  VRAgentWebSocket.prototype = NativeWebSocket.prototype;
  ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach((key) => {
    VRAgentWebSocket[key] = NativeWebSocket[key];
  });
  window.WebSocket = VRAgentWebSocket;

  // Livestream mode never records audio, whatever the app settings say.
  if (LIVE && navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
    const nativeGUM = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = (constraints) => {
      if (constraints && constraints.audio) {
        return Promise.reject(new DOMException("Microphone disabled in VR Agent livestream mode", "NotAllowedError"));
      }
      return nativeGUM(constraints);
    };
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------
  function boot() {
    buildOverlay();
    watchStage();
    log(LIVE ? "livestream mode" : "development mode", FLAGS);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  // Small developer hook for the browser console.
  window.vrAgent = Object.freeze({
    play: (name) => playAction(String(name), "requested"),
    state: () => JSON.parse(JSON.stringify({ ...state, capabilities: state.capabilities && Object.keys(state.capabilities.actions || {}) })),
  });
})();
