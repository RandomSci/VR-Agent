/*
 * VR Agent Room: Game Board and sound effects.
 *
 * The Game Board is a room object. It lives in the world layer, so the camera
 * zooms and pans it together with the characters, and the characters can look
 * at it (OBJECT:game_board). The frame (title, round, status, timer, scores,
 * chat feed, winner highlight) is shared by every game; the middle area is
 * drawn by a per-game renderer registered with VRRoomBoard.registerRenderer.
 * Trivia registers "trivia". Tic-Tac-Toe could register "grid" later.
 *
 * The backend sends a JSON view model only. Every string is set with
 * textContent; nothing is ever parsed as HTML.
 */
(() => {
  "use strict";

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]/g;
  function text(value, max) {
    let out = String(value == null ? "" : value).replace(/\s+/g, " ").replace(CONTROL_CHARS, "").trim();
    if (max && out.length > max) out = out.slice(0, max - 1).trimEnd() + "…";
    return out;
  }
  function el(tag, className, parent) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (parent) parent.appendChild(node);
    return node;
  }

  // ---------------------------------------------------------------------------
  // Per-game renderers
  // ---------------------------------------------------------------------------
  const renderers = new Map();

  function registerRenderer(name, renderer) {
    if (!/^[a-z][a-z0-9_]{0,23}$/.test(name)) throw new Error("bad renderer name");
    renderers.set(name, renderer);
  }

  // Trivia: category, question, each character's answer, the revealed answer.
  registerRenderer("trivia", {
    mount(body) {
      const r = {};
      r.meta = el("div", "vrb-meta", body);
      r.category = el("span", "vrb-pill", r.meta);
      r.difficulty = el("span", "vrb-pill vrb-pill-soft", r.meta);
      r.question = el("div", "vrb-question", body);
      r.answers = el("div", "vrb-answers", body);
      r.reveal = el("div", "vrb-reveal", body);
      return r;
    },
    update(r, view, ctx) {
      const b = view.body || {};
      r.category.textContent = text(b.category, 24);
      r.category.hidden = !b.category;
      r.difficulty.textContent = text(b.difficulty, 12);
      r.difficulty.hidden = !b.difficulty;
      const q = text(b.question, 200) || (view.phase === "intro" ? "Answer in chat to play along!" : "");
      if (r.question.textContent !== q) {
        r.question.textContent = q;
        r.question.classList.remove("vrb-in");
        void r.question.offsetWidth;
        r.question.classList.add("vrb-in");
      }
      r.question.classList.toggle("vrb-long", q.length > 70);

      r.answers.replaceChildren();
      for (const a of Array.isArray(b.answers) ? b.answers.slice(0, 4) : []) {
        const row = el("div", "vrb-answer", r.answers);
        const who = el("span", "vrb-answer-who", row);
        who.textContent = text(a.name, 20);
        who.style.color = ctx.colorOf(a.player);
        el("span", "vrb-answer-text", row).textContent = text(a.text, 40);
        const mark = el("span", "vrb-mark", row);
        if (a.correct === true) {
          row.classList.add("vrb-right");
          mark.textContent = "✓";
        } else if (a.correct === false) {
          row.classList.add("vrb-wrong");
          mark.textContent = "✗";
        } else {
          mark.textContent = "…";
        }
      }
      const reveal = text(b.reveal, 60);
      r.reveal.textContent = reveal ? `Answer: ${reveal}` : "";
      r.reveal.hidden = !reveal;
    },
  });

  // ---------------------------------------------------------------------------
  // Board frame
  // ---------------------------------------------------------------------------
  function createBoard(world, ctx) {
    const root = el("div", "vrb-board", world);
    root.hidden = true;
    const head = el("div", "vrb-head", root);
    const title = el("div", "vrb-title", head);
    const round = el("div", "vrb-round", head);
    const body = el("div", "vrb-body", root);
    const timer = el("div", "vrb-timer", root);
    const bar = el("i", "", timer);
    const status = el("div", "vrb-status", root);
    const scores = el("div", "vrb-scores", root);
    const feed = el("div", "vrb-feed", root);

    let mounted = null; // {name, state}
    let lastScores = {};
    let lastTimerKey = "";
    let lastAnswers = "";
    let hideTimer = null;

    function place(box) {
      if (!box) return;
      const w = box.width * 1920;
      const h = box.height * 1080;
      root.style.width = `${w}px`;
      root.style.minHeight = `${h * 0.72}px`;
      root.style.maxHeight = `${h}px`;
      root.style.left = `${box.x * 1920 - w / 2}px`;
      root.style.top = `${box.y * 1080 - h / 2}px`;
    }

    function flash(kind) {
      root.classList.remove("vrb-flash-right", "vrb-flash-wrong");
      void root.offsetWidth;
      root.classList.add(kind === "right" ? "vrb-flash-right" : "vrb-flash-wrong");
    }

    function render(view) {
      place(ctx.box());
      if (!view || typeof view !== "object") {
        if (!root.hidden) {
          root.classList.add("vrb-out");
          clearTimeout(hideTimer);
          hideTimer = setTimeout(() => {
            root.hidden = true;
            root.classList.remove("vrb-out");
          }, 450);
        }
        mounted = null;
        lastScores = {};
        return;
      }
      clearTimeout(hideTimer);
      root.classList.remove("vrb-out");
      root.hidden = false;
      const renderer = renderers.get(view.renderer);
      if (!renderer) return;
      if (!mounted || mounted.name !== view.renderer) {
        body.replaceChildren();
        mounted = { name: view.renderer, state: renderer.mount(body) };
      }
      root.dataset.phase = text(view.phase, 16);
      root.classList.toggle("vrb-paused", !!view.paused);
      root.classList.toggle("vrb-finished", !!view.finished);
      title.textContent = text(view.title, 30);
      const r = Number(view.round) || 0;
      const total = Number(view.rounds) || 0;
      round.textContent = r ? `Round ${r}/${total}` : "";
      status.textContent = text(view.status, 60);

      renderer.update(mounted.state, view, ctx);

      // Correct and wrong flashes when an answer is judged.
      const answers = JSON.stringify(((view.body || {}).answers || []).map((a) => a.correct));
      if (answers !== lastAnswers) {
        const judged = ((view.body || {}).answers || []).filter((a) => a.correct !== null && a.correct !== undefined);
        const latest = judged[judged.length - 1];
        if (latest && lastAnswers && answers.length >= lastAnswers.length) flash(latest.correct ? "right" : "wrong");
        lastAnswers = answers;
      }

      // Timer bar animates locally from the remaining time.
      const t = view.timer;
      const key = t ? `${view.phase}:${view.round}:${(view.body || {}).answers ? view.body.answers.length : 0}` : "";
      if (!t) {
        timer.hidden = true;
        lastTimerKey = "";
      } else if (key !== lastTimerKey) {
        lastTimerKey = key;
        timer.hidden = false;
        const totalMs = Math.max(1, Number(t.total_ms) || 1);
        const remaining = clamp(Number(t.remaining_ms) || 0, 0, totalMs);
        bar.style.transition = "none";
        bar.style.width = `${(remaining / totalMs) * 100}%`;
        void bar.offsetWidth;
        bar.style.transition = `width ${remaining}ms linear`;
        bar.style.width = "0%";
      }

      // Scores with a pop when one changes.
      scores.replaceChildren();
      for (const s of Array.isArray(view.scores) ? view.scores.slice(0, 5) : []) {
        const chip = el("div", "vrb-chip", scores);
        chip.style.setProperty("--vrb-color", ctx.colorOf(s.id));
        el("span", "vrb-chip-name", chip).textContent = text(s.name, 16);
        el("span", "vrb-chip-score", chip).textContent = String(Number(s.score) || 0);
        if (view.highlight === s.id) chip.classList.add(view.finished ? "vrb-winner" : "vrb-active");
        if (s.id in lastScores && lastScores[s.id] !== s.score) chip.classList.add("vrb-pop");
      }
      lastScores = Object.fromEntries((view.scores || []).map((s) => [s.id, s.score]));

      // Chat participation feed.
      feed.replaceChildren();
      const items = Array.isArray((view.body || {}).viewer_feed) ? view.body.viewer_feed.slice(-4) : [];
      for (const item of items) {
        const row = el("div", "vrb-feed-row" + (item.correct ? " vrb-right" : ""), feed);
        const user = text(item.user, 30);
        el("span", "vrb-feed-user", row).textContent = user.startsWith("@") ? user : `@${user}`;
        el("span", "vrb-feed-text", row).textContent = text(item.text, 30);
        el("span", "vrb-mark", row).textContent = item.correct ? "✓" : "✗";
      }
      feed.hidden = !items.length;
    }

    return { render, place: () => place(ctx.box()), element: root };
  }

  // ---------------------------------------------------------------------------
  // Sound effects: local files, short, never stacked, ducked under speech
  // ---------------------------------------------------------------------------
  const SFX = ["game_start", "question", "correct", "wrong", "score", "round_win", "game_win", "game_stop"];
  function createSfx(options) {
    const clips = new Map();
    let current = null;
    let lastAt = 0;
    for (const name of SFX) {
      const audio = new Audio(`./sfx/${name}.wav`);
      audio.preload = "auto";
      clips.set(name, audio);
    }
    function play(name) {
      const clip = clips.get(name);
      const volume = clamp(Number(options.volume()) || 0, 0, 1);
      if (!clip || volume <= 0) return false;
      const t = performance.now();
      if (current && !current.paused && t - lastAt < 700) return false; // never stack effects
      const ducked = options.speaking() ? 0.45 : 1;
      try {
        clip.currentTime = 0;
        clip.volume = clamp(volume * ducked, 0, 1);
        const p = clip.play();
        if (p && p.catch) p.catch(() => {});
        current = clip;
        lastAt = t;
        return true;
      } catch (_) {
        return false;
      }
    }
    return { play, names: SFX.slice() };
  }

  window.VRRoomBoard = Object.freeze({ registerRenderer, createBoard, createSfx, renderers: () => [...renderers.keys()] });
})();
