// Minecraft mode on the Stage: both bots' first person views side by side,
// Mika and Luna in the bottom corners, who is talking, the chat that reached
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
  let ports = { mika: 3790, luna: 3791 };
  const seen = {}; // character id -> last time the bot was in the world
  const loaded = {}; // character id -> the view iframe was pointed at the bot
  let layoutTimer = 0;
  const bubbleTimers = {};
  // Who is talking: a small name pill with moving sound bars next to her, not
  // the full sentence (scrolling text on screen was too distracting).
  // ?captions=1 on the Stage URL shows the words again.
  const CAPTIONS = new URLSearchParams(location.search).get("captions") === "1";
  // One camera at a time (one 3D scene to draw): whoever talks gets it,
  // held at least FOCUS_HOLD ms so it does not flicker between them.
  let focus = "";
  let focusAt = 0;
  const FOCUS_HOLD = 15000;
  const CHAT_SHOW_MS = 2000; // a viewer comment stays on screen this long
  // The chat box: the last comments stay on the side (viewers see theirs on
  // stream, and a ✓ when it was answered). ?chatbox=0 brings back the old
  // two second popup in the middle.
  const CHATBOX = new URLSearchParams(location.search).get("chatbox") !== "0";
  const CHATBOX_ROWS = 4;
  const CHATBOX_MS = 20000; // gone after this at most
  const CHATBOX_ANSWERED_MS = 6000; // and this long after it was answered
  // "client": the owner's real Minecraft is the camera (OBS window capture
  // under this page), so the page is see-through and the web views stay off.
  let camera = "web";

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
      <div id="mc-chat"></div>
      <div id="mc-chatbox"><div class="mc-chead">💬 Live chat</div><div class="mc-clist"></div></div>
      <div id="mc-problem"></div>
      <div id="mc-status"></div>
      <div id="mc-project"><div class="mc-ptitle"></div><div class="mc-pstep"></div><i><b></b></i><div class="mc-psteps"></div></div>
      <div id="mc-done"></div>
      <div id="mc-net"><b>🧠 Neural network</b><span class="mc-nstats"></span><span class="mc-nlast"></span></div>
      <div id="mc-hint"></div>
      <div id="mc-score"></div>
      <div id="mc-toasts"></div>`;
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
        <div class="mc-bubble"></div>
        <div class="mc-pop"></div>
        <div class="mc-effects"></div>`;
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
    // Only the watched view is loaded: a hidden one would still stream chunks
    // and build meshes in the background, for nothing.
    if (was) {
      const old = viewPane(was);
      if (old) old.querySelector("iframe").src = "about:blank";
    }
    fresh(id);
    document.querySelectorAll("#mc-views .mc-view").forEach((v) => v.classList.toggle("focus", v.dataset.who === id));

  }

  function pane(id) { return document.querySelector(`#mc-panes .mc-pane[data-who="${id}"]`); }
  function viewPane(id) { return document.querySelector(`#mc-views .mc-view[data-who="${id}"]`); }

  function enter(op) {
    build();
    cast = (op.cast && op.cast.length ? op.cast : cast).slice(0, 2);
    names = op.names || names;
    ports = op.viewers || ports;
    document.documentElement.classList.add("minecraft-mode");
    // creative: no hearts, food or item lists (they never change), see the CSS
    document.documentElement.classList.toggle("mc-creative", op.creative !== false);
    views();
    focus = "";
    setFocus(cast[0], true);
    focusAt = 0; // the first speaker may take the camera right away
    problem(op.problem);
    if (op.hud) hud(op.hud);
    if (op.project) project(op.project);
    if (op.net) net(op.net);
    setCamera(op.camera || "web");
    startHints(op.hints);
    if (op.stats) stats(op.stats);
    clearInterval(layoutTimer);
    let tries = 0;
    const place = () => {
      const ok = window.vrRoom && typeof window.vrRoom.minecraft === "function" && window.vrRoom.minecraft(cast[0], cast[1]);
      if (ok || ++tries > 60) clearInterval(layoutTimer);
    };
    layoutTimer = setInterval(place, 500);
    place();
  }

  function setCamera(mode) {
    camera = mode === "client" ? "client" : "web";
    document.documentElement.classList.toggle("mc-client", camera === "client");
    document.querySelectorAll("#mc-views iframe").forEach((f) => {
      if (camera === "client") f.src = "about:blank";
    });
    if (camera === "web" && focus) fresh(focus);
  }

  function leave() {
    document.documentElement.classList.remove("mc-client");
    clearInterval(layoutTimer);
    document.documentElement.classList.remove("minecraft-mode", "mc-creative");
    clearInterval(hintTimer);
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
    if (camera === "client") return;
    const p = viewPane(id);
    if (!p || !loaded[id] || id !== focus) return;
    const frame = p.querySelector("iframe");
    frame.src = `http://${location.hostname || "127.0.0.1"}:${ports[id] || 3790}/?t=${Date.now()}`;
  }
  setInterval(() => { if (focus && document.documentElement.classList.contains("minecraft-mode")) fresh(focus); }, REFRESH_MS);

  // She was teleported or respawned: the view still shows the old place (or
  // nothing, white sky). Load it again once the new chunks have arrived.
  const reloadTimers = {};
  function reloadSoon(id) {
    clearTimeout(reloadTimers[id]);
    reloadTimers[id] = setTimeout(() => { if (id === focus) fresh(id); }, 2500);
  }

  function view(id) {
    const p = viewPane(id);
    if (!p) return;
    const now = Date.now();
    const gone = !seen[id] || now - seen[id] > 20000;
    seen[id] = now;
    if (loaded[id] && !gone) return;
    loaded[id] = true;
    setTimeout(() => {
      p.classList.add("live");
      if (id === focus) fresh(id);
    }, 2500);
  }

  function hud(all) {
    for (const [id, h] of Object.entries(all || {})) {
      const p = pane(id);
      if (!p) continue;
      view(id);
      // The watched one is not in the world (dead, restarting): show the other.
      if (camera !== "client" && (!seen[focus] || Date.now() - seen[focus] > 15000)) setFocus(id, true);
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
    if (camera !== "client" && seen[op.who] && Date.now() - seen[op.who] < 15000) setFocus(op.who);
    const bubble = p.querySelector(".mc-bubble");
    const wasShown = bubble.classList.contains("show");
    if (CAPTIONS) {
      bubble.classList.remove("wave");
      bubble.textContent = op.text || "";
    } else if (!bubble.classList.contains("wave") || !bubble.firstChild) {
      bubble.classList.add("wave");
      bubble.replaceChildren(el("b", "", names[op.who] || op.who), el("span", "mc-bars"));
      bubble.lastChild.append(el("i"), el("i"), el("i"), el("i"));
    }
    if (!wasShown || CAPTIONS) {
      bubble.classList.remove("show");
      void bubble.offsetWidth;
      bubble.classList.add("show");
    }
    clearTimeout(bubbleTimers[op.who]);
    bubbleTimers[op.who] = setTimeout(() => bubble.classList.remove("show"), 12000);
  }

  function said(op) {
    const p = pane(op.who);
    if (!p) return;
    clearTimeout(bubbleTimers[op.who]);
    // the pill goes as soon as she stops (a caption stays a moment to be read)
    bubbleTimers[op.who] = setTimeout(() => p.querySelector(".mc-bubble").classList.remove("show"), CAPTIONS ? 2500 : 400);
  }

  function chat(op) {
    if (CHATBOX) return chatRow(op);
    const box = $("mc-chat");
    if (!box) return;
    const row = el("div", "mc-msg");
    row.append(el("b", "", op.author || "viewer"));
    const to = (op.to || []).map((id) => names[id] || id).join(" & ");
    if (to) row.append(el("i", "", `→ ${to}`));
    row.append(el("span", "", op.text || ""));
    box.prepend(row);
    while (box.children.length > 3) box.lastChild.remove();
    // On screen for 2 seconds at most, then gone.
    setTimeout(() => row.classList.add("old"), CHAT_SHOW_MS - 350);
    setTimeout(() => row.remove(), CHAT_SHOW_MS);
  }

  function chatRow(op) {
    const box = $("mc-chatbox");
    if (!box) return;
    const list = box.querySelector(".mc-clist");
    const row = el("div", "mc-crow");
    row.dataset.author = op.author || "";
    row.append(el("b", "", op.author || "viewer"));
    row.append(el("span", "", op.text || ""));
    row.append(el("em", "mc-tick", ""));
    list.append(row);
    while (list.children.length > CHATBOX_ROWS) list.firstChild.remove();
    box.classList.add("show");
    row._gone = setTimeout(() => fadeRow(row), CHATBOX_MS);
  }

  function fadeRow(row) {
    clearTimeout(row._gone);
    row.classList.add("old");
    setTimeout(() => {
      const list = row.parentNode;
      row.remove();
      if (list && !list.children.length) $("mc-chatbox").classList.remove("show");
    }, 400);
  }

  function answered(op) {
    const box = $("mc-chatbox");
    if (!box) return;
    for (const row of box.querySelectorAll(".mc-crow")) {
      if ((op.authors || []).includes(row.dataset.author) && !row.classList.contains("done")) {
        row.classList.add("done");
        row.querySelector(".mc-tick").textContent = `✓ ${names[op.who] || op.who || ""}`;
        clearTimeout(row._gone);
        row._gone = setTimeout(() => fadeRow(row), CHATBOX_ANSWERED_MS);
      }
    }
  }

  function status(op) {
    const box = $("mc-status");
    if (!box) return;
    box.textContent = op.text || "";
    box.classList.add("show");
    setTimeout(() => box.classList.remove("show"), 8000);
  }

  function project(op) {
    const box = $("mc-project");
    if (!box) return;
    box.classList.add("show");
    box.querySelector(".mc-ptitle").textContent = op.title || "";
    box.querySelector(".mc-pstep").textContent = op.step ? `Building: ${op.step}` : "";
    box.querySelector("i b").style.width = `${Math.round(Math.min(1, op.progress || 0) * 100)}%`;
    box.querySelector(".mc-psteps").replaceChildren(
      ...(op.steps || []).map((s) => el("span", s.done ? "done" : (s.name === op.step ? "now" : ""), s.name))
    );
  }

  // The real network: live training numbers and its last guess.
  let netTimer = 0;
  function net(op) {
    const box = $("mc-net");
    if (!box) return;
    box.classList.add("show");
    // Only a moment after a viewer's digit was read (unless the network show is on)
    clearTimeout(netTimer);
    if (!op.always) netTimer = setTimeout(() => box.classList.remove("show"), 20000);
    const acc = Math.round((op.accuracy || 0) * 100);
    box.querySelector(".mc-nstats").textContent =
      `${op.training ? "training · " : ""}${op.steps || 0} steps · loss ${(op.loss || 0).toFixed(2)} · accuracy ${acc}%`;
    const last = op.last || {};
    const tag = box.querySelector(".mc-nlast");
    if (last.digit === undefined) { tag.textContent = ""; return; }
    tag.textContent = `${last.author ? last.author + ": " : ""}${last.digit} → guessed ${last.guess} ${last.right ? "✓" : "✗"} ${Math.round((last.confidence || 0) * 100)}%`;
    tag.className = `mc-nlast ${last.right ? "right" : "wrong"}`;
  }

  // ---- small things that make people stay: what they can do, counters,
  // a hello for each viewer, big words at big moments, potion timers.
  let hints = [];
  let hintAt = 0;
  let hintTimer = 0;
  const HINT_MS = 14000;

  function startHints(list) {
    hints = (list || []).filter(Boolean);
    clearInterval(hintTimer);
    if (!hints.length) return;
    const show = () => {
      const box = $("mc-hint");
      if (!box) return;
      box.classList.remove("show");
      setTimeout(() => {
        box.textContent = "💡 " + hints[hintAt++ % hints.length];
        box.classList.add("show");
      }, 450);
    };
    show();
    hintTimer = setInterval(show, HINT_MS);
  }

  function stats(op) {
    const box = $("mc-score");
    if (!box) return;
    const parts = [];
    if (op.blocks) parts.push(el("span", "", `🧱 ${Number(op.blocks).toLocaleString()} blocks`));
    const wins = op.wins || {};
    if (Object.keys(wins).length) {
      const who = cast.map((id) => `${(op.names || names)[id] || id} ${wins[id] || 0}`).join(" – ");
      parts.push(el("span", "", `🏁 ${who}`));
    }
    const before = box.textContent;
    box.replaceChildren(...parts);
    box.classList.toggle("show", parts.length > 0);
    if (before && box.textContent !== before) {
      box.classList.remove("tick");
      void box.offsetWidth;
      box.classList.add("tick");
    }
  }

  function pop(op) {
    const p = pane(op.who);
    if (!p) return;
    const box = p.querySelector(".mc-pop");
    box.textContent = op.text || "";
    box.className = `mc-pop ${op.tone || "wow"}`;
    void box.offsetWidth;
    box.classList.add("show");
  }

  function hello(op) {
    const box = $("mc-toasts");
    if (!box) return;
    const regular = (op.streams || 1) > 1;
    const row = el("div", `mc-toast ${regular ? "regular" : "new"}`);
    row.append(el("i", "", regular ? "⭐" : "👋"), el("b", "", op.author || "viewer"),
      el("span", "", regular ? `stream #${op.streams} with us` : "new here, welcome!"));
    box.prepend(row);
    while (box.children.length > 3) box.lastChild.remove();
    setTimeout(() => row.classList.add("old"), 4600);
    setTimeout(() => row.remove(), 5200);
  }

  const effectTimers = {};
  function effect(op) {
    const p = pane(op.who);
    if (!p) return;
    const box = p.querySelector(".mc-effects");
    const key = `${op.who}:${op.name}`;
    let chip = box.querySelector(`[data-name="${CSS.escape(op.name)}"]`);
    if (!chip) {
      chip = el("span", "mc-effect");
      chip.dataset.name = op.name;
      box.append(chip);
    }
    const ends = Date.now() + (op.seconds || 10) * 1000;
    clearInterval(effectTimers[key]);
    const tick = () => {
      const left = Math.ceil((ends - Date.now()) / 1000);
      if (left <= 0) { clearInterval(effectTimers[key]); chip.remove(); return; }
      chip.textContent = `🫧 ${op.name} ${left}s`;
    };
    tick();
    effectTimers[key] = setInterval(tick, 500);
  }

  function projectDone(op) {
    const box = $("mc-done");
    if (!box) return;
    box.textContent = `✨ ${op.step} finished! ✨`;
    box.classList.remove("show");
    void box.offsetWidth;
    box.classList.add("show");
    setTimeout(() => box.classList.remove("show"), 7000);
  }

  function apply(op) {
    if (!op) return;
    switch (op.kind) {
      case "start": enter(op); break;
      case "hud": if (!root) return; hud(op.hud); break;
      case "say": if (!root) return; say(op); break;
      case "said": if (!root) return; said(op); break;
      case "chat": if (!root) return; chat(op); break;
      case "answered": if (!root) return; answered(op); break;
      case "status": if (!root) return; status(op); break;
      case "project": if (!root) return; project(op); break;
      case "project_done": if (!root) return; projectDone(op); break;
      case "reload": if (!root) return; reloadSoon(op.who); break;
      case "camera": if (!root) return; setCamera(op.mode); break;
      case "net": if (!root) return; net(op); break;
      case "focus": if (!root) return; if (camera === "client") setFocus(op.who, true); break;
      case "pop": if (!root) return; pop(op); break;
      case "hello": if (!root) return; hello(op); break;
      case "stats": if (!root) return; stats(op); break;
      case "effect": if (!root) return; effect(op); break;
      case "stop": leave(); break;
    }
  }

  function restore(viewState) {
    if (!viewState || !viewState.active) return;
    enter(viewState);
  }

  window.minecraftStage = { apply, restore };
})();
