/*
 * VR Agent Room renderer.
 *
 * Renders every character of the room as an independent Live2D model on one
 * PIXI stage, plays the backend's audio payloads on the right character (one
 * voice at a time), and turns high level room state into what each model can
 * really do: attention targets become head and eye directions, registry
 * action names become motions or expressions.
 *
 * Everything that happens without viewers (idle motion, glances, quiet
 * periods) is local and makes no network request.
 *
 * Viewer text is only ever rendered with textContent. Ops from the backend
 * are validated against the cast, the registry and the model before use.
 */
(() => {
  "use strict";

  const STAGE_W = 1920;
  const STAGE_H = 1080;
  const params = new URLSearchParams(window.location.search);
  const FLAGS = {
    subtitles: params.get("subtitles") !== "0",
    card: params.get("card") !== "0",
    status: params.get("status") !== "0",
    idle: params.get("idle") !== "0",
    fallback: params.get("nofallback") !== "1",
    sfx: params.has("sfx") ? Math.max(0, Math.min(100, Number(params.get("sfx")) || 0)) / 100 : null,
  };
  const log = (...args) => console.log("[VR Room]", ...args);
  const now = () => performance.now();
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  const pick = (items) => items[Math.floor(Math.random() * items.length)];
  const own = (obj, key) => !!obj && Object.prototype.hasOwnProperty.call(obj, key);

  const PRIORITY_IDLE = 2;
  const PRIORITY_FORCE = 3;
  const TARGET_RE = /^(VIEWER|CAMERA|NEUTRAL|GAME|(CHARACTER|OBJECT):[a-z][a-z0-9_]{0,23})$/;

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  const room = {
    config: null,
    characters: new Map(), // id -> Character
    order: [],
    objects: {}, // id -> {x, y, width, height} fractions
    paused: false,
    phase: "listening",
    settings: { comment_card_max_chars: 180, overlay_title: "VR AGENT" },
    lastGestureAt: 0,
    lastSpeechEndAt: 0,
    booted: false,
  };

  // ---------------------------------------------------------------------------
  // Text safety
  // ---------------------------------------------------------------------------
  const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]/g;
  function safeText(value, max) {
    let text = String(value == null ? "" : value).replace(/\s+/g, " ").replace(CONTROL_CHARS, "").trim();
    if (max && text.length > max) text = text.slice(0, max - 1).trimEnd() + "…";
    return text;
  }

  // ---------------------------------------------------------------------------
  // Overlay (badge, comment card, caption, BRB) reusing vr-agent.css
  // ---------------------------------------------------------------------------
  const ui = {};
  function el(tag, className, parent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  function buildOverlay() {
    ui.root = el("div", "", document.body);
    ui.root.id = "vr-agent-overlay";
    ui.status = el("div", "vra-status", ui.root);
    const badge = el("div", "vra-badge", ui.status);
    el("span", "vra-dot", badge);
    el("span", "vra-live", badge).textContent = "Live";
    el("span", "vra-sep", badge);
    ui.title = el("span", "vra-title", badge);
    ui.title.textContent = "VR AGENT";
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
    ui.cardTo = el("div", "vrr-to", head);
    ui.cardTo.hidden = true;
    ui.cardAmount = el("div", "vra-card-amount", head);
    ui.cardAmount.hidden = true;
    ui.cardMessage = el("div", "vra-card-message", ui.card);

    ui.caption = el("div", "vra-caption", ui.root);
    ui.captionSpeaker = el("span", "vrr-speaker", ui.caption);
    ui.captionText = el("span", "", ui.caption);

    ui.brb = el("div", "vra-brb", ui.root);
    const brbText = el("div", "vra-brb-text", ui.brb);
    brbText.textContent = "Be right back";
    const brbImg = el("img", "", ui.brb);
    brbImg.alt = "";
    brbImg.src = "./brb.jpg";
    brbImg.addEventListener("error", () => brbImg.remove());

    ui.audioHint = el("div", "vrr-audio-hint", document.body);
    ui.audioHint.textContent = "Click to enable audio";
    ui.audioHint.hidden = true;
    ui.audioHint.addEventListener("click", () => {
      ui.audioHint.hidden = true;
    });
    renderPhase();
  }

  const PHASE_TEXT = { listening: "Listening to chat", thinking: "Thinking", responding: "Responding" };
  function renderPhase() {
    if (!ui.state) return;
    const phase = PHASE_TEXT[room.phase] ? room.phase : "listening";
    ui.state.dataset.phase = phase;
    ui.stateText.textContent = PHASE_TEXT[phase];
    ui.state.hidden = phase === "thinking";
  }

  let cardTimer = null;
  let cardId = null;
  function showCard(payload) {
    if (!FLAGS.card || !ui.card) return;
    const max = Math.max(20, Number(room.settings.comment_card_max_chars) || 180);
    const author = safeText(payload.author, 60) || "Viewer";
    ui.cardAuthor.textContent = author.startsWith("@") ? author : `@${author}`;
    ui.cardMessage.textContent = safeText(payload.message, max);
    const amount = safeText(payload.amount, 30);
    ui.cardAmount.textContent = amount;
    ui.cardAmount.hidden = !amount;
    const to = Array.isArray(payload.to) ? payload.to.filter((id) => room.characters.has(id)) : [];
    if (to.length) {
      ui.cardTo.textContent = "to " + to.map((id) => room.characters.get(id).name).join(" & ");
      ui.cardTo.hidden = false;
    } else {
      ui.cardTo.hidden = true;
    }
    ui.card.classList.toggle("vra-paid", !!payload.paid);
    cardId = payload.id || null;
    ui.card.classList.add("vra-visible");
    clearTimeout(cardTimer);
    cardTimer = setTimeout(hideCard, 60000);
  }

  function hideCard(payload) {
    if (!ui.card) return;
    if (payload && payload.id && cardId && payload.id !== cardId) return;
    clearTimeout(cardTimer);
    ui.card.classList.remove("vra-visible");
    cardId = null;
  }

  let captionTimer = null;
  function showCaption(character, text, holdMs) {
    if (!FLAGS.subtitles || !ui.caption) return;
    const clean = safeText(text, 220);
    if (!clean) return;
    ui.captionSpeaker.textContent = character ? character.name : "";
    ui.captionSpeaker.style.color = character ? character.color : "";
    ui.captionSpeaker.hidden = !character;
    ui.captionText.textContent = clean;
    ui.caption.classList.add("vra-visible");
    clearTimeout(captionTimer);
    const hold = holdMs ? clamp(Number(holdMs) || 0, 800, 8000) : 2500 + clean.length * 60;
    captionTimer = setTimeout(() => ui.caption.classList.remove("vra-visible"), hold);
  }

  function setPaused(paused) {
    room.paused = !!paused;
    document.documentElement.classList.toggle("vra-paused", room.paused);
    if (room.paused) hideCard();
  }

  // ---------------------------------------------------------------------------
  // Stage and camera
  // ---------------------------------------------------------------------------
  let app = null;
  const layers = {};
  const camera = {
    x: STAGE_W / 2,
    y: STAGE_H / 2,
    zoom: 1,
    from: null,
    to: null,
    start: 0,
    duration: 1400,
    shot: "wide",
  };

  function createStage() {
    app = new PIXI.Application({
      view: document.getElementById("vr-room-canvas"),
      resizeTo: window,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(2, window.devicePixelRatio || 1),
      preserveDrawingBuffer: true,
    });
    layers.root = new PIXI.Container(); // fits the 16:9 stage into the window
    layers.camera = new PIXI.Container(); // camera transform
    layers.background = new PIXI.Container();
    layers.characters = new PIXI.Container();
    layers.characters.sortableChildren = true;
    layers.camera.addChild(layers.background, layers.characters);
    layers.root.addChild(layers.camera);
    app.stage.addChild(layers.root);
    window.addEventListener("resize", fitStage);
    fitStage();
    app.ticker.add(onFrame);
  }

  function fitStage() {
    if (!app) return;
    const w = app.renderer.screen.width;
    const h = app.renderer.screen.height;
    const s = Math.min(w / STAGE_W, h / STAGE_H);
    layers.root.scale.set(s);
    layers.root.position.set((w - STAGE_W * s) / 2, (h - STAGE_H * s) / 2);
    applyCamera();
  }

  function setBackground(file) {
    const name = safeText(file, 120);
    if (!name || !/^[\w./ -]+$/.test(name) || name.includes("..")) return;
    const url = "/bg/" + encodeURI(name);
    document.getElementById("vr-room-backdrop").style.backgroundImage = `url("${url}")`;
    PIXI.Texture.fromURL(url)
      .then((texture) => {
        layers.background.removeChildren();
        const sprite = new PIXI.Sprite(texture);
        // Cover the stage with a little bleed so zooming never shows edges.
        const s = Math.max(STAGE_W / texture.width, STAGE_H / texture.height) * 1.02;
        sprite.scale.set(s);
        sprite.anchor.set(0.5);
        sprite.position.set(STAGE_W / 2, STAGE_H / 2);
        layers.background.addChild(sprite);
      })
      .catch(() => log("background not found", name));
  }

  function easeInOut(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function clampCamera(x, y, zoom) {
    zoom = clamp(zoom, 1, 2.6);
    const halfW = STAGE_W / 2 / zoom;
    const halfH = STAGE_H / 2 / zoom;
    return { x: clamp(x, halfW, STAGE_W - halfW), y: clamp(y, halfH, STAGE_H - halfH), zoom };
  }

  function moveCamera(target, duration) {
    const to = clampCamera(target.x, target.y, target.zoom);
    camera.from = { x: camera.x, y: camera.y, zoom: camera.zoom };
    camera.to = to;
    camera.start = now();
    camera.duration = Math.max(200, duration || (room.config && room.config.camera.transition_seconds * 1000) || 1400);
  }

  function updateCamera() {
    if (!camera.to) return;
    const t = clamp((now() - camera.start) / camera.duration, 0, 1);
    const k = easeInOut(t);
    camera.x = camera.from.x + (camera.to.x - camera.from.x) * k;
    camera.y = camera.from.y + (camera.to.y - camera.from.y) * k;
    camera.zoom = camera.from.zoom + (camera.to.zoom - camera.from.zoom) * k;
    if (t >= 1) camera.to = null;
    applyCamera();
  }

  function applyCamera() {
    if (!layers.camera) return;
    layers.camera.pivot.set(camera.x, camera.y);
    layers.camera.position.set(STAGE_W / 2, STAGE_H / 2);
    layers.camera.scale.set(camera.zoom);
    // Keep the world DOM layer (game board) in the same coordinate space.
    const world = document.getElementById("vr-room-world");
    if (world && layers.root) {
      const s = layers.root.scale.x;
      const z = camera.zoom * s;
      const tx = layers.root.position.x + (STAGE_W / 2 - camera.x * camera.zoom) * s;
      const ty = layers.root.position.y + (STAGE_H / 2 - camera.y * camera.zoom) * s;
      world.style.transform = `matrix(${z}, 0, 0, ${z}, ${tx}, ${ty})`;
    }
  }

  // Named shots. Resolved locally from the cast layout, so no numbers ever come
  // from the backend.
  function shotTarget(shot, ref) {
    const chars = [...room.characters.values()].filter((c) => c.loaded);
    const faces = chars.map((c) => c.facePoint());
    switch (shot) {
      case "two_shot": {
        if (faces.length < 2) return { x: STAGE_W / 2, y: STAGE_H / 2, zoom: 1 };
        const xs = faces.map((f) => f.x);
        const span = Math.max(...xs) - Math.min(...xs);
        const zoom = clamp((STAGE_W * 0.78) / (span + 500), 1, 1.25);
        const avgY = faces.reduce((a, f) => a + f.y, 0) / faces.length;
        return { x: (Math.max(...xs) + Math.min(...xs)) / 2, y: avgY + STAGE_H * 0.3 / zoom, zoom };
      }
      case "closeup":
      case "focus": {
        const c = room.characters.get(ref);
        if (!c || !c.loaded) return null;
        const f = c.facePoint();
        const zoom = shot === "closeup" ? 2.3 : 1.18;
        return { x: shot === "closeup" ? f.x : (f.x + STAGE_W / 2) / 2, y: f.y + (shot === "closeup" ? 90 : STAGE_H * 0.25), zoom };
      }
      case "board": {
        const box = room.objects.game_board;
        if (!box) return null;
        // Slight push-in that keeps both characters' heads and the whole board in frame.
        return { x: STAGE_W / 2, y: Math.min(box.y * STAGE_H, STAGE_H * 0.55), zoom: 1.06 };
      }
      case "wide":
      default:
        return { x: STAGE_W / 2, y: STAGE_H / 2, zoom: 1 };
    }
  }

  let cameraReturnTimer = null;
  function setShot(shot, ref, holdMs, returnTo) {
    try {
      if (room.config && room.config.camera && room.config.camera.enabled === false) return;
      const target = shotTarget(shot, ref);
      if (!target) return;
      camera.shot = shot;
      moveCamera(target);
      clearTimeout(cameraReturnTimer);
      const back = ["wide", "board", "two_shot"].includes(returnTo) ? returnTo : "wide";
      if (holdMs > 0) cameraReturnTimer = setTimeout(() => setShot(back, null, 0), holdMs);
    } catch (err) {
      log("camera error, back to wide", err);
      camera.to = null;
      camera.x = STAGE_W / 2;
      camera.y = STAGE_H / 2;
      camera.zoom = 1;
      applyCamera();
    }
  }

  // ---------------------------------------------------------------------------
  // Characters
  // ---------------------------------------------------------------------------
  class Character {
    constructor(spec) {
      this.id = spec.id;
      this.name = safeText(spec.name, 24) || spec.id;
      this.color = /^#[0-9a-fA-F]{6}$/.test(spec.color || "") ? spec.color : "#ffffff";
      this.primary = !!spec.primary;
      this.spec = spec;
      this.caps = spec.capabilities || { actions: {} };
      this.model = null;
      this.loaded = false;
      this.failed = false;
      this.look = { head: false, eyes: false, body: false };
      this.mouthParam = null;
      this.mouthLevel = 0;
      this.speaking = false;
      this.directed = null; // {target, until}
      this.nextDirected = null; // {target, start, hold} waiting for its delay
      this.ambient = { target: "VIEWER", start: 0, until: 0 };
      this.neutralOffset = { x: 0, y: -0.1 };
      this.focus = { x: 0, y: 0 };
      this.motionBusyUntil = 0;
      this.expressionTimer = null;
      this.lastIdle = [];
      this.lastLargeIdleAt = now();
      this.nextIdleAt = now() + rand(4000, 12000);
    }

    async load() {
      const url = String(this.spec.model_url || "");
      if (!/^\/live2d-models\/[\w./-]+\.model3\.json$/.test(url) || url.includes("..")) {
        throw new Error("invalid model url");
      }
      const load = PIXI.live2d.Live2DModel.from(url, {
        autoInteract: false,
        idleMotionGroup: this.caps.idle_group || "Idle",
      });
      const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("load timeout")), 45000));
      const model = await Promise.race([load, timeout]);
      this.model = model;
      model.anchor.set(0.5, 1);
      const layout = this.spec.layout || {};
      const baseHeight = model.height || 1;
      const scale = ((Number(layout.height) || 0.95) * STAGE_H) / baseHeight;
      model.scale.set(scale);
      model.position.set((Number(layout.x) || 0.5) * STAGE_W, (Number(layout.bottom) || 1.02) * STAGE_H);
      model.zIndex = Math.round(model.position.y);
      layers.characters.addChild(model);
      this.setupParameters();
      this.loaded = true;
      log(`loaded ${this.name}`, this.look, `mouth=${this.mouthParam || "none"}`);
    }

    hasParam(id) {
      if (!id || !this.model) return false;
      const core = this.model.internalModel.coreModel;
      try {
        const index = core.getParameterIndex(id);
        return index >= 0 && index < core.getParameterCount();
      } catch (_) {
        return false;
      }
    }

    // Use only the look parameters the model really has.
    setupParameters() {
      const spec = this.spec.look || {};
      const ids = {
        ax: this.hasParam(spec.angle_x) ? spec.angle_x : null,
        ay: this.hasParam(spec.angle_y) ? spec.angle_y : null,
        az: this.hasParam(spec.angle_z) ? spec.angle_z : null,
        bx: this.hasParam(spec.body_x) ? spec.body_x : null,
        ex: this.hasParam(spec.eye_x) ? spec.eye_x : null,
        ey: this.hasParam(spec.eye_y) ? spec.eye_y : null,
      };
      this.look = { head: !!ids.ax, eyes: !!ids.ex, body: !!ids.bx };
      const strength = clamp(Number(spec.strength) || 1, 0, 1.5);
      const internal = this.model.internalModel;
      internal.updateFocus = function () {
        const f = this.focusController;
        const core = this.coreModel;
        if (ids.ex) core.addParameterValueById(ids.ex, f.x * Math.min(1, strength));
        if (ids.ey) core.addParameterValueById(ids.ey, f.y * Math.min(1, strength));
        if (ids.ax) core.addParameterValueById(ids.ax, f.x * 30 * strength);
        if (ids.ay) core.addParameterValueById(ids.ay, f.y * 30 * strength);
        if (ids.az) core.addParameterValueById(ids.az, f.x * f.y * -30 * strength);
        if (ids.bx) core.addParameterValueById(ids.bx, f.x * 10 * strength * 0.5);
      };
      this.mouthParam = this.hasParam(this.spec.mouth) ? this.spec.mouth : null;
      internal.on("beforeModelUpdate", () => {
        if (!this.mouthParam) return;
        if (this.speaking || this.mouthLevel > 0.01) {
          internal.coreModel.setParameterValueById(this.mouthParam, clamp(this.mouthLevel, 0, 1));
        }
      });
    }

    facePoint() {
      if (!this.model) return { x: STAGE_W / 2, y: STAGE_H * 0.3 };
      const layout = this.spec.layout || {};
      const w = this.model.width;
      const h = this.model.height;
      return {
        x: this.model.position.x + ((Number(layout.face_x) || 0.5) - 0.5) * w,
        y: this.model.position.y - h + (Number(layout.face_y) || 0.2) * h,
      };
    }

    // ---- attention ---------------------------------------------------------
    currentTarget(t) {
      if (this.nextDirected && t >= this.nextDirected.start) {
        const next = this.nextDirected;
        this.nextDirected = null;
        this.directed = { target: next.target, until: t + next.hold };
      }
      if (this.directed && t < this.directed.until) return this.directed.target;
      this.directed = null;
      if (t >= this.ambient.start && t < this.ambient.until) return this.ambient.target;
      // Default: look at whoever is talking, otherwise at the viewer.
      const speaker = speakingCharacter();
      if (speaker && speaker !== this) return `CHARACTER:${speaker.id}`;
      return "VIEWER";
    }

    resolveTarget(target) {
      if (target === "VIEWER" || target === "CAMERA") return { x: 0, y: 0 };
      if (target === "NEUTRAL") return this.neutralOffset;
      let point = null;
      if (target === "GAME") {
        const box = room.objects.game_board;
        if (box) point = { x: box.x * STAGE_W, y: (box.y - box.height * 0.2) * STAGE_H };
      } else if (target.startsWith("CHARACTER:")) {
        const other = room.characters.get(target.slice(10));
        if (other && other.loaded && other !== this) point = other.facePoint();
      } else if (target.startsWith("OBJECT:")) {
        const box = room.objects[target.slice(7)];
        if (box) point = { x: box.x * STAGE_W, y: box.y * STAGE_H };
      }
      if (!point) return { x: 0, y: 0 };
      const face = this.facePoint();
      return {
        x: clamp((point.x - face.x) / 520, -1, 1) * 0.9,
        y: clamp(-(point.y - face.y) / 420, -1, 1) * 0.9,
      };
    }

    updateAttention(t) {
      if (!this.loaded || (!this.look.head && !this.look.eyes)) return;
      const dir = this.resolveTarget(this.currentTarget(t));
      if (Math.abs(dir.x - this.focus.x) > 0.01 || Math.abs(dir.y - this.focus.y) > 0.01) {
        this.focus = dir;
        this.model.internalModel.focusController.focus(dir.x, dir.y);
      }
    }

    // Delays run on the frame clock (not setTimeout) so they stay accurate
    // even when the page is busy rendering.
    direct(target, holdMs, delayMs) {
      if (!TARGET_RE.test(String(target))) return;
      const hold = clamp(Number(holdMs) || 4000, 300, 60000);
      const delay = clamp(Number(delayMs) || 0, 0, 10000);
      if (delay > 0) {
        this.nextDirected = { target: String(target), start: now() + delay, hold };
      } else {
        this.nextDirected = null;
        this.directed = { target: String(target), until: now() + hold };
      }
    }

    glance(target, holdMs, delayMs) {
      const start = now() + (delayMs || 0);
      this.ambient = { target, start, until: start + holdMs };
    }

    // ---- actions -----------------------------------------------------------
    action(name) {
      const actions = this.caps.actions || {};
      return own(actions, name) ? actions[name] : null;
    }

    play(name, source) {
      const action = this.action(name);
      if (!action || !this.loaded) {
        if (!action) log(`${this.name} ignored unknown action`, name);
        return false;
      }
      const manager = this.model.internalModel.motionManager;
      if (action.kind === "motion") {
        const group = typeof action.group === "string" ? action.group : null;
        const index = Number(action.index);
        const defs = group !== null && manager.definitions ? manager.definitions[group] : null;
        if (!defs || !Number.isInteger(index) || index < 0 || index >= defs.length) return false;
        this.model.motion(group, index, source === "idle" ? PRIORITY_IDLE : PRIORITY_FORCE);
        this.motionBusyUntil = now() + Math.max(1000, (Number(action.duration) || 3) * 1000);
        if (source !== "idle") room.lastGestureAt = now();
        log(`${this.name} ${source} motion`, name);
        return true;
      }
      if (action.kind === "expression") {
        const em = manager.expressionManager;
        const defs = em && em.definitions ? em.definitions : [];
        const exists = defs.some((d) => (d.Name || d.name) === action.expression);
        if (!exists) return false;
        this.model.expression(action.expression);
        clearTimeout(this.expressionTimer);
        const holdMs = Math.max(1000, (Number(action.hold) || 4) * 1000);
        this.expressionTimer = setTimeout(() => this.resetExpression(), holdMs);
        log(`${this.name} ${source} expression`, name);
        return true;
      }
      return false;
    }

    resetExpression() {
      try {
        const em = this.model.internalModel.motionManager.expressionManager;
        if (em && typeof em.resetExpression === "function") em.resetExpression();
      } catch (_) {}
    }

    playExpressionIndex(index) {
      // Classic payloads carry raw expression indices of the primary model.
      const em = this.loaded && this.model.internalModel.motionManager.expressionManager;
      if (!em || !Number.isInteger(index) || index < 0 || index >= (em.definitions || []).length) return;
      this.model.expression(index);
      clearTimeout(this.expressionTimer);
      this.expressionTimer = setTimeout(() => this.resetExpression(), 5000);
    }

    playEmotion(index) {
      const names = Array.isArray(this.spec.emotions) ? this.spec.emotions : [];
      const name = Number.isInteger(index) ? names[index] : null;
      if (name) this.play(name, "emotion");
    }

    busy(t) {
      return this.speaking || t < this.motionBusyUntil;
    }
  }

  function speakingCharacter() {
    for (const c of room.characters.values()) if (c.speaking) return c;
    return null;
  }

  function primaryCharacter() {
    for (const c of room.characters.values()) if (c.primary && c.loaded) return c;
    for (const c of room.characters.values()) if (c.loaded) return c;
    return null;
  }

  // ---------------------------------------------------------------------------
  // Ambient life: local only, no network, no LLM
  // ---------------------------------------------------------------------------
  const ambient = { nextGlanceAt: now() + rand(12000, 25000) };

  function ambientSettings() {
    return (room.config && room.config.ambient) || {
      idle_min_seconds: 12,
      idle_max_seconds: 22,
      glance_min_seconds: 25,
      glance_max_seconds: 70,
      expression_chance: 0.15,
    };
  }

  function ambientTick(t) {
    if (room.paused || !FLAGS.idle) return;
    const settings = ambientSettings();
    const chars = [...room.characters.values()].filter((c) => c.loaded);
    const someoneSpeaking = !!speakingCharacter();

    // Idle gestures, spaced out between characters, never during speech.
    for (const c of chars) {
      if (t < c.nextIdleAt) continue;
      if (someoneSpeaking || c.busy(t) || t - room.lastGestureAt < 5000 || t - room.lastSpeechEndAt < 2500) {
        c.nextIdleAt = t + rand(2500, 5000);
        continue;
      }
      // Sometimes simply exist quietly.
      if (Math.random() < 0.3) {
        c.nextIdleAt = t + rand(settings.idle_min_seconds, settings.idle_max_seconds) * 1000;
        continue;
      }
      const action = pickIdle(c, t);
      if (action && c.play(action.name, "idle")) {
        room.lastGestureAt = t;
        c.lastIdle.push(action.name);
        if (c.lastIdle.length > 2) c.lastIdle.shift();
        if ((c.caps.large_motion_actions || []).includes(action.name)) c.lastLargeIdleAt = t;
      }
      c.nextIdleAt = t + ((Number(action && action.duration) || 3) + rand(settings.idle_min_seconds, settings.idle_max_seconds)) * 1000;
    }

    // Glances between characters, looking around, quiet periods.
    if (t < ambient.nextGlanceAt) return;
    ambient.nextGlanceAt = t + rand(settings.glance_min_seconds, settings.glance_max_seconds) * 1000;
    if (someoneSpeaking) return;
    const free = chars.filter((c) => !c.directed && !c.busy(t));
    if (!free.length) return;
    const a = pick(free);
    const roll = Math.random();
    const others = chars.filter((c) => c !== a);
    if (roll < 0.55 && others.length) {
      const b = pick(others);
      const hold = rand(1500, 3200);
      a.glance(`CHARACTER:${b.id}`, hold);
      if (!b.directed && !b.busy(t) && Math.random() < 0.5) {
        // B notices a moment later and glances back, sometimes with a smile.
        const delay = rand(500, 1200);
        b.glance(`CHARACTER:${a.id}`, rand(1200, 2400), delay);
        if (Math.random() < settings.expression_chance) {
          const happy = ((b.spec.reactions || {}).happy || []).find((n) => {
            const act = b.action(n);
            return act && act.kind === "expression";
          });
          if (happy) dueActions.push({ at: t + delay + 300, character: b, name: happy });
        }
      }
    } else if (roll < 0.8) {
      a.neutralOffset = { x: rand(-0.45, 0.45), y: rand(-0.2, 0.1) };
      a.glance("NEUTRAL", rand(1500, 2600));
    }
    // Otherwise: stay quietly looking at the viewer.
  }

  function pickIdle(c, t) {
    const caps = c.caps;
    const large = new Set(caps.large_motion_actions || []);
    const gapMs = (Number(caps.large_motion_min_gap_seconds) || 90) * 1000;
    let pool = Object.values(caps.actions || {}).filter((a) => a.idle && a.kind === "motion" && a.idle_weight > 0);
    if (pool.length > 1) pool = pool.filter((a) => a.name !== c.lastIdle[c.lastIdle.length - 1]);
    if (pool.length > 2) pool = pool.filter((a) => !c.lastIdle.includes(a.name));
    const calm = pool.filter((a) => !large.has(a.name) || t - c.lastLargeIdleAt > gapMs);
    if (calm.length) pool = calm;
    const total = pool.reduce((sum, a) => sum + a.idle_weight, 0);
    let cursor = Math.random() * total;
    for (const a of pool) {
      if (cursor < a.idle_weight) return a;
      cursor -= a.idle_weight;
    }
    return pool[pool.length - 1] || null;
  }

  // ---------------------------------------------------------------------------
  // Speech: one global queue, so two voices never overlap
  // ---------------------------------------------------------------------------
  const speech = {
    queue: [],
    current: null, // {item, audio, character, startedAt, watchdog}
    synthComplete: false,
    pendingAction: null, // classic path: {name, deadline}
  };

  function enqueueAudio(payload) {
    const id = typeof payload.character === "string" ? payload.character : null;
    const character = (id && room.characters.get(id)) || primaryCharacter();
    speech.queue.push({
      character,
      audio: typeof payload.audio === "string" ? payload.audio : null,
      volumes: Array.isArray(payload.volumes) ? payload.volumes : [],
      slice: Number(payload.slice_length) || 20,
      text: payload.display_text && payload.display_text.text,
      actions: payload.actions || null,
      emotionMode: payload.emotion_mode === "profile" ? "profile" : "raw",
    });
    playNext();
  }

  function playNext() {
    if (speech.current || !speech.queue.length) {
      if (!speech.current && !speech.queue.length) maybeReportPlaybackComplete();
      return;
    }
    const item = speech.queue.shift();
    const character = item.character;
    const expectedMs = Math.max(600, item.volumes.length * item.slice);
    const current = { item, character, audio: null, startedAt: now(), watchdog: null };
    speech.current = current;

    const begin = () => {
      if (character) {
        character.speaking = true;
        applyExpressions(character, item);
        if (speech.pendingAction && character.primary) flushPendingAction();
      }
      showCaption(character, item.text);
      sendToServer({ type: "audio-play-start", display_text: item.text ? { text: item.text } : null, forwarded: true });
    };
    const finish = () => {
      if (speech.current !== current) return;
      clearTimeout(current.watchdog);
      if (character) {
        character.speaking = false;
        character.mouthLevel = 0;
      }
      room.lastSpeechEndAt = now();
      speech.current = null;
      playNext();
    };

    if (!item.audio) {
      begin();
      current.watchdog = setTimeout(finish, Math.min(6000, 800 + (item.text || "").length * 45));
      return;
    }
    const audio = new Audio("data:audio/wav;base64," + item.audio);
    current.audio = audio;
    audio.volume = 1;
    audio.addEventListener("ended", finish, { once: true });
    audio.addEventListener("error", finish, { once: true });
    current.watchdog = setTimeout(finish, expectedMs + 5000);
    audio
      .play()
      .then(begin)
      .catch((err) => {
        log("audio blocked", err && err.name);
        if (err && err.name === "NotAllowedError") ui.audioHint.hidden = false;
        // Still move the character's mouth and captions so nothing stalls.
        current.audio = null;
        begin();
        clearTimeout(current.watchdog);
        current.watchdog = setTimeout(finish, expectedMs);
      });
  }

  function applyExpressions(character, item) {
    const expressions = item.actions && Array.isArray(item.actions.expressions) ? item.actions.expressions : [];
    const first = expressions.find((x) => Number.isInteger(x));
    if (first == null) return;
    if (item.emotionMode === "profile") character.playEmotion(first);
    else character.playExpressionIndex(first);
  }

  function updateMouths() {
    const current = speech.current;
    for (const c of room.characters.values()) {
      if (!current || current.character !== c) {
        c.mouthLevel *= 0.7;
        continue;
      }
      const item = current.item;
      const elapsed = current.audio ? current.audio.currentTime * 1000 : now() - current.startedAt;
      const idx = Math.floor(elapsed / item.slice);
      const target = item.volumes.length ? Number(item.volumes[Math.min(idx, item.volumes.length - 1)]) || 0 : 0;
      const level = idx < item.volumes.length ? clamp(target * 1.1, 0, 1) : 0;
      c.mouthLevel += (level - c.mouthLevel) * 0.55;
    }
  }

  function stopSpeech() {
    speech.queue.length = 0;
    const current = speech.current;
    speech.current = null;
    if (current) {
      clearTimeout(current.watchdog);
      if (current.audio) {
        try {
          current.audio.pause();
        } catch (_) {}
      }
    }
    for (const c of room.characters.values()) {
      c.speaking = false;
      c.mouthLevel = 0;
    }
  }

  function maybeReportPlaybackComplete() {
    if (!speech.synthComplete) return;
    speech.synthComplete = false;
    sendToServer({ type: "frontend-playback-complete" });
  }

  function queuePendingAction(name) {
    speech.pendingAction = { name, deadline: now() + 20000 };
  }

  function flushPendingAction() {
    const pending = speech.pendingAction;
    speech.pendingAction = null;
    const c = primaryCharacter();
    if (pending && c) setTimeout(() => c.play(pending.name, "requested"), 0);
  }

  // ---------------------------------------------------------------------------
  // Frame loop
  // ---------------------------------------------------------------------------
  const dueActions = []; // delayed action ops, run from the frame loop
  function runDueActions(t) {
    for (let i = dueActions.length - 1; i >= 0; i--) {
      if (t >= dueActions[i].at) {
        const due = dueActions.splice(i, 1)[0];
        due.character.play(due.name, "requested");
      }
    }
  }

  let lastAmbientTick = 0;
  function onFrame() {
    const t = now();
    updateCamera();
    updateMouths();
    if (t - lastAmbientTick > 100) {
      lastAmbientTick = t;
      runDueActions(t);
      for (const c of room.characters.values()) c.updateAttention(t);
      ambientTick(t);
      if (speech.pendingAction && t > speech.pendingAction.deadline) flushPendingAction();
    }
  }

  // ---------------------------------------------------------------------------
  // Room config and typed updates from the backend
  // ---------------------------------------------------------------------------
  async function applyConfig(payload) {
    const spec = payload.room || {};
    if (!spec.enabled) {
      fallbackToClassic("room disabled on the server");
      return;
    }
    const signature = JSON.stringify((spec.characters || []).map((c) => [c.id, c.model_url]));
    room.config = spec;
    room.objects = {};
    for (const [id, box] of Object.entries(spec.objects || {})) {
      if (/^[a-z][a-z0-9_]{0,23}$/.test(id) && box && typeof box === "object") {
        room.objects[id] = {
          x: clamp(Number(box.x) || 0.5, 0, 1),
          y: clamp(Number(box.y) || 0.5, 0, 1),
          width: clamp(Number(box.width) || 0.2, 0, 1),
          height: clamp(Number(box.height) || 0.2, 0, 1),
        };
      }
    }
    if (ui.title) ui.title.textContent = safeText(spec.title, 40) || "VR AGENT";
    if (board) board.render(payload.board && typeof payload.board === "object" ? payload.board : null);
    if (room.booted && signature === room.signature) {
      reportModels();
      applySnapshot(payload.snapshot);
      return;
    }
    room.signature = signature;
    room.booted = true;
    setBackground(spec.background);
    room.characters.clear();
    layers.characters.removeChildren();
    room.order = [];
    for (const c of spec.characters || []) {
      if (!c || !/^[a-z][a-z0-9_]{0,23}$/.test(c.id || "")) continue;
      const character = new Character(c);
      room.characters.set(character.id, character);
      room.order.push(character.id);
    }
    await Promise.all(
      [...room.characters.values()].map((c) =>
        c.load().catch((err) => {
          c.failed = true;
          log(`could not load ${c.name}`, err && err.message);
        }),
      ),
    );
    reportModels();
    if (![...room.characters.values()].some((c) => c.loaded)) {
      fallbackToClassic("no character model loaded");
      return;
    }
    applySnapshot(payload.snapshot);
  }

  function applySnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    for (const [id, box] of Object.entries(snapshot.objects || {})) {
      if (own(room.objects, id) || !/^[a-z][a-z0-9_]{0,23}$/.test(id)) continue;
      room.objects[id] = {
        x: clamp(Number(box.x) || 0.5, 0, 1),
        y: clamp(Number(box.y) || 0.5, 0, 1),
        width: clamp(Number(box.width) || 0.1, 0, 1),
        height: clamp(Number(box.height) || 0.1, 0, 1),
      };
    }
  }

  function reportModels() {
    const loaded = [];
    const failed = [];
    for (const c of room.characters.values()) {
      if (c.loaded) loaded.push(c.id);
      else if (c.failed) failed.push(c.id);
    }
    sendToServer({ type: "vr-room-client-status", loaded, failed });
  }

  // Game Board and sound effects (room-board.js). Optional: the room still works without them.
  let board = null;
  let sfx = null;
  function createBoardAndSfx() {
    const B = window.VRRoomBoard;
    const world = document.getElementById("vr-room-world");
    if (!B || !world) return;
    try {
      board = B.createBoard(world, {
        box: () => room.objects.game_board || { x: 0.5, y: 0.6, width: 0.42, height: 0.48 },
        colorOf: (id) => {
          const c = room.characters.get(id);
          return c ? c.color : id === "viewers" ? "#ffd166" : "#ffffff";
        },
      });
      sfx = B.createSfx({
        volume: () => (FLAGS.sfx != null ? FLAGS.sfx : Number((room.config || {}).sfx_volume) || 0),
        speaking: () => !!speech.current,
      });
    } catch (err) {
      log("board unavailable", err);
      board = null;
      sfx = null;
    }
  }

  const opHandlers = {
    board(op) {
      if (board) board.render(op.view && typeof op.view === "object" ? op.view : null);
    },
    sfx(op) {
      if (sfx && typeof op.name === "string" && sfx.names.includes(op.name) && !room.paused) sfx.play(op.name);
    },
    line(op) {
      // A game line shown without voice (no viewers around, so no TTS request).
      const c = room.characters.get(op.character);
      if (c && typeof op.text === "string") showCaption(c, op.text, op.ms);
    },
    attention(op) {
      const c = room.characters.get(op.character);
      if (c) c.direct(op.target, op.hold_ms, op.delay_ms);
    },
    action(op) {
      const c = room.characters.get(op.character);
      if (!c || typeof op.name !== "string") return;
      const delay = clamp(Number(op.delay_ms) || 0, 0, 10000);
      if (delay > 0) dueActions.push({ at: now() + delay, character: c, name: op.name });
      else c.play(op.name, "requested");
    },
    camera(op) {
      const shots = ["wide", "two_shot", "closeup", "focus", "board"];
      if (!shots.includes(op.shot)) return;
      setShot(op.shot, typeof op.target === "string" ? op.target : null, clamp(Number(op.hold_ms) || 0, 0, 30000), op.return_to);
    },
    object(op) {
      const id = String(op.id || "");
      if (!/^[a-z][a-z0-9_]{0,23}$/.test(id)) return;
      if (op.remove) {
        delete room.objects[id];
        return;
      }
      room.objects[id] = {
        x: clamp(Number(op.x) || 0.5, 0, 1),
        y: clamp(Number(op.y) || 0.5, 0, 1),
        width: clamp(Number(op.width) || 0.1, 0, 1),
        height: clamp(Number(op.height) || 0.1, 0, 1),
      };
    },
  };

  function onServerMessage(payload) {
    if (!payload || typeof payload !== "object") return;
    lastMessageAt = now();
    switch (payload.type) {
      case "vr-room-config":
        configReceived = true;
        applyConfig(payload).catch((err) => log("config failed", err));
        break;
      case "vr-room-update":
        for (const op of Array.isArray(payload.ops) ? payload.ops : []) {
          const handler = op && own(opHandlers, op.op) ? opHandlers[op.op] : null;
          if (!handler) continue;
          try {
            handler(op);
          } catch (err) {
            log("op failed", op.op, err);
          }
        }
        break;
      case "vr-agent-config":
        if (payload.settings && typeof payload.settings === "object") {
          room.settings.comment_card_max_chars = Number(payload.settings.comment_card_max_chars) || 180;
        }
        if (payload.phase) room.phase = payload.phase;
        setPaused(payload.paused);
        renderPhase();
        break;
      case "vr-agent-state":
        if (PHASE_TEXT[payload.phase]) {
          room.phase = payload.phase;
          renderPhase();
        }
        break;
      case "vr-agent-pause":
        setPaused(payload.paused);
        break;
      case "vr-agent-action":
        // Classic single-character path: an action for the primary character.
        if (typeof payload.action === "string") {
          if (payload.sync === "now") {
            const c = primaryCharacter();
            if (c) c.play(payload.action, "requested");
          } else {
            queuePendingAction(payload.action);
          }
        }
        break;
      case "youtube-live-selected-message":
        if (payload.active) showCard(payload);
        else hideCard(payload);
        break;
      case "audio":
        enqueueAudio(payload);
        break;
      case "backend-synth-complete":
        speech.synthComplete = true;
        if (!speech.current && !speech.queue.length) maybeReportPlaybackComplete();
        break;
      case "control":
        if (payload.text === "interrupt") stopSpeech();
        if (payload.text === "conversation-chain-start") speech.synthComplete = false;
        if (payload.text === "conversation-chain-end" && speech.pendingAction) flushPendingAction();
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------------------
  // WebSocket with reconnect
  // ---------------------------------------------------------------------------
  let socket = null;
  let reconnectDelay = 1000;
  let configReceived = false;
  let lastMessageAt = now();

  function sendToServer(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    try {
      socket.send(JSON.stringify(payload));
    } catch (_) {}
  }

  function connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/client-ws`);
    configReceived = false;
    socket.addEventListener("open", () => {
      reconnectDelay = 1000;
      lastMessageAt = now();
      sendToServer({ type: "vr-agent-hello", mode: "room" });
      // A server without room support never answers the hello.
      setTimeout(() => {
        if (!configReceived && socket && socket.readyState === WebSocket.OPEN) {
          fallbackToClassic("server did not send a room config");
        }
      }, 10000);
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data !== "string" || event.data.length > 20000000) return;
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      onServerMessage(payload);
    });
    socket.addEventListener("close", () => {
      stopSpeech();
      setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(10000, reconnectDelay * 2);
    });
    socket.addEventListener("error", () => {
      try {
        socket.close();
      } catch (_) {}
    });
  }

  setInterval(() => {
    sendToServer({ type: "heartbeat" });
    if (socket && socket.readyState === WebSocket.OPEN && now() - lastMessageAt > 60000) {
      log("connection looks stale, reconnecting");
      try {
        socket.close();
      } catch (_) {}
    }
  }, 15000);

  let fallingBack = false;
  function fallbackToClassic(reason) {
    log("falling back to the classic livestream page:", reason);
    if (!FLAGS.fallback || fallingBack) return;
    fallingBack = true;
    const keep = new URLSearchParams();
    keep.set("mode", "live");
    for (const key of ["subtitles", "card", "status", "idle"]) if (params.has(key)) keep.set(key, params.get(key));
    location.replace("/?" + keep.toString());
  }

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------
  function boot() {
    buildOverlay();
    if (!window.PIXI || !PIXI.live2d || !window.Live2DCubismCore) {
      fallbackToClassic("renderer libraries missing");
      return;
    }
    createStage();
    createBoardAndSfx();
    connect();
    log("room page started", FLAGS);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();

  // Developer console hooks.
  window.vrRoom = Object.freeze({
    state: () => ({
      characters: [...room.characters.values()].map((c) => ({
        id: c.id,
        loaded: c.loaded,
        failed: c.failed,
        look: c.look,
        mouth: c.mouthParam,
        speaking: c.speaking,
        target: c.currentTarget(now()),
        focus: c.focus,
      })),
      camera: { x: camera.x, y: camera.y, zoom: camera.zoom, shot: camera.shot },
      objects: room.objects,
      queue: speech.queue.length,
    }),
    look: (id, target, ms) => {
      const c = room.characters.get(id);
      if (c) c.direct(target, ms || 4000, 0);
    },
    play: (id, name) => {
      const c = room.characters.get(id);
      return c ? c.play(String(name), "requested") : false;
    },
    shot: (shot, ref, ms) => setShot(shot, ref, ms || 0),
  });
})();
