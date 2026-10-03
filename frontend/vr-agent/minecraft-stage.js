// Minecraft mode on the Stage: both bots' first person views side by side,
// Mika and Luna in the bottom corners, speech bubbles, the chat that reached
// them and a small HUD. Driven by "minecraft" ops (room/minecraft_mode.py).
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  let root = null;
  let cast = ["mika", "luna"];
  let names = { mika: "Mika", luna: "Luna" };
  let ports = { mika: 3000, luna: 3001 };
  const seen = {}; // character id -> last time the bot was in the world
  const loaded = {}; // character id -> the view iframe was pointed at the bot
  let layoutTimer = 0;
  const bubbleTimers = {};
  // One camera at a time (one 3D scene to draw): whoever talks gets it,
  // held at least FOCUS_HOLD ms so it does not flicker between them.
  let focus = "";
  let focusAt = 0;
  const FOCUS_HOLD = 15000;

  function build() {
    if (root) return;
    // Two layers: the game views under the Live2D canvas, the HUD above it.
    root = el("section");
    root.id = "mc-stage";
    root.innerHTML = `<div id="mc-views"></div>`;
    const over = el("section");
    over.id = "mc-over";
    over.innerHTML = `
      <div id="mc-panes"></div>
      <div id="mc-watch"></div>
      <div id="mc-chat"></div>
      <div id="mc-problem"></div>
      <div id="mc-status"></div>`;
    document.body.append(root, over);
  }

  function side(i) { return i === 0 ? "left" : "right"; }

  function views() {
    const box = $("mc-views"), top = $("mc-panes");
    box.replaceChildren();
    top.replaceChildren();
    cast.forEach((id, i) => {
      const view = el("div", `mc-view mc-${side(i)}`);
      view.dataset.who = id;
      view.innerHTML = `
        <div class="mc-wait"><span>Loading the world…</span></div>
        <iframe title="view" scrolling="no" tabindex="-1"></iframe>`;
      const hudPane = el("div", `mc-pane mc-${side(i)}`);
      hudPane.dataset.who = id;
      hudPane.innerHTML = `
        <header class="mc-tag"><span class="mc-name"></span><span class="mc-hearts"></span><span class="mc-food"></span></header>
        <div class="mc-doing"></div>
        <div class="mc-items"></div>
        <div class="mc-bubble"></div>`;
      hudPane.querySelector(".mc-name").textContent = names[id] || id;
      box.appendChild(view);
      top.appendChild(hudPane);
    });
  }

  function setFocus(id, force) {
    if (!id || id === focus) return;
    if (!force && Date.now() - focusAt < FOCUS_HOLD) return;
    const was = focus;
    focus = id;
    focusAt = Date.now();
    if (was) fresh(id); // a view that sat hidden starts clean (no frozen or blank 3D)
    document.querySelectorAll("#mc-views .mc-view").forEach((v) => v.classList.toggle("focus", v.dataset.who === id));
    const watch = $("mc-watch");
    if (watch) {
      watch.textContent = `👀 ${names[id] || id}'s eyes`;
      watch.className = `mc-${side(cast.indexOf(id))}`;
    }
  }

  function pane(id) { return document.querySelector(`#mc-panes .mc-pane[data-who="${id}"]`); }
  function viewPane(id) { return document.querySelector(`#mc-views .mc-view[data-who="${id}"]`); }

  function enter(op) {
    build();
    cast = (op.cast && op.cast.length ? op.cast : cast).slice(0, 2);
    names = op.names || names;
    ports = op.viewers || ports;
    document.documentElement.classList.add("minecraft-mode");
    views();
    focus = "";
    setFocus(cast[0], true);
    focusAt = 0; // the first speaker may take the camera right away
    problem(op.problem);
    if (op.hud) hud(op.hud);
    clearInterval(layoutTimer);
    let tries = 0;
    const place = () => {
      const ok = window.vrRoom && typeof window.vrRoom.minecraft === "function" && window.vrRoom.minecraft(cast[0], cast[1]);
      if (ok || ++tries > 60) clearInterval(layoutTimer);
    };
    layoutTimer = setInterval(place, 500);
    place();
  }

  function leave() {
    clearInterval(layoutTimer);
    document.documentElement.classList.remove("minecraft-mode");
    if (window.vrRoom && window.vrRoom.normal) window.vrRoom.normal();
  }

  function problem(text) {
    const box = $("mc-problem");
    if (!box) return;
    box.textContent = text || "";
    box.classList.toggle("show", !!text);
  }

  // The bot's view only exists once it is in the world: point the frame at it
  // then, and again after the bot was gone for a while (a restart).
  // The 3D view can go blank or freeze in OBS after a while (lost graphics
  // context, unloaded chunks). A fresh load fixes it: on every camera switch
  // and every few minutes on the view being watched.
  const REFRESH_MS = 6 * 60 * 1000;
  function fresh(id) {
    const p = viewPane(id);
    if (!p || !loaded[id]) return;
    const frame = p.querySelector("iframe");
    frame.src = `http://${location.hostname || "127.0.0.1"}:${ports[id] || 3000}/?t=${Date.now()}`;
  }
  setInterval(() => { if (focus && document.documentElement.classList.contains("minecraft-mode")) fresh(focus); }, REFRESH_MS);

  function view(id) {
    const p = viewPane(id);
    if (!p) return;
    const now = Date.now();
    const gone = !seen[id] || now - seen[id] > 20000;
    seen[id] = now;
    if (loaded[id] && !gone) return;
    loaded[id] = true;
    const frame = p.querySelector("iframe");
    setTimeout(() => {
      frame.src = `http://${location.hostname || "127.0.0.1"}:${ports[id] || 3000}/?t=${now}`;
      p.classList.add("live");
    }, 2500);
  }

  function hud(all) {
    for (const [id, h] of Object.entries(all || {})) {
      const p = pane(id);
      if (!p) continue;
      view(id);
      // The watched one is not in the world (dead, restarting): show the other.
      if (!seen[focus] || Date.now() - seen[focus] > 15000) setFocus(id, true);
      const hearts = Math.max(0, Math.min(10, Math.ceil((h.health || 0) / 2)));
      const food = Math.max(0, Math.min(10, Math.ceil((h.hunger || 0) / 2)));
      p.querySelector(".mc-hearts").textContent = "♥".repeat(hearts) + "♡".repeat(10 - hearts);
      p.querySelector(".mc-food").textContent = `🍗 ${food}/10`;
      p.classList.toggle("hurt", (h.health || 0) <= 6);
      const action = String(h.doing || "").replace(/^action:\s*/i, "").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
      const doing = [action, h.time, h.biome].filter(Boolean).join(" · ");
      p.querySelector(".mc-doing").textContent = doing;
      const items = p.querySelector(".mc-items");
      items.replaceChildren(...(h.items || []).map(([name, n]) => el("span", "", `${name} ×${n}`)));
    }
  }

  function say(op) {
    const p = pane(op.who);
    if (!p) return;
    if (seen[op.who] && Date.now() - seen[op.who] < 15000) setFocus(op.who);
    const bubble = p.querySelector(".mc-bubble");
    bubble.textContent = op.text || "";
    bubble.classList.remove("show");
    void bubble.offsetWidth;
    bubble.classList.add("show");
    clearTimeout(bubbleTimers[op.who]);
    bubbleTimers[op.who] = setTimeout(() => bubble.classList.remove("show"), 12000);
  }

  function said(op) {
    const p = pane(op.who);
    if (!p) return;
    clearTimeout(bubbleTimers[op.who]);
    bubbleTimers[op.who] = setTimeout(() => p.querySelector(".mc-bubble").classList.remove("show"), 2500);
  }

  function chat(op) {
    const box = $("mc-chat");
    if (!box) return;
    const row = el("div", "mc-msg");
    row.append(el("b", "", op.author || "viewer"));
    const to = (op.to || []).map((id) => names[id] || id).join(" & ");
    if (to) row.append(el("i", "", `→ ${to}`));
    row.append(el("span", "", op.text || ""));
    box.prepend(row);
    while (box.children.length > 3) box.lastChild.remove();
    setTimeout(() => row.classList.add("old"), 20000);
  }

  function status(op) {
    const box = $("mc-status");
    if (!box) return;
    box.textContent = op.text || "";
    box.classList.add("show");
    setTimeout(() => box.classList.remove("show"), 8000);
  }

  function apply(op) {
    if (!op) return;
    switch (op.kind) {
      case "start": enter(op); break;
      case "hud": if (!root) return; hud(op.hud); break;
      case "say": if (!root) return; say(op); break;
      case "said": if (!root) return; said(op); break;
      case "chat": if (!root) return; chat(op); break;
      case "status": if (!root) return; status(op); break;
      case "stop": leave(); break;
    }
  }

  function restore(viewState) {
    if (!viewState || !viewState.active) return;
    enter(viewState);
  }

  window.minecraftStage = { apply, restore };
})();
