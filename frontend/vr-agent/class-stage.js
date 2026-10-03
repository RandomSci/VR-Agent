// Class mode on the Stage: a slide Mika controls and a light, readable
// notebook whose cells type themselves and really run on the server.
// Driven by "class" ops (see room/class_mode.py); a page that joins mid
// lesson restores from the snapshot in vr-room-config.
(() => {
  "use strict";
  const MAX_CELLS = 6;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  let root = null;
  let teacher = "mika", sidekick = "luna";
  let quizTimer = 0, chatTimer = 0, captionTimer = 0, layoutTimer = 0;
  const typing = new Map(); // cell number -> {done, finish}

  function build() {
    if (root) return;
    root = el("section");
    root.id = "class-stage";
    root.setAttribute("aria-label", "Class");
    root.innerHTML = `
      <header id="class-head"><span id="class-course">Class</span><span id="class-lesson">Class starts in a moment</span>
        <span id="class-progress"><span id="class-count"></span><i><b></b></i></span></header>
      <article id="class-slide">
        <div id="class-slide-body"><h1></h1><ul></ul></div>
        <div id="class-visual"></div>
        <div id="class-caption"></div>
        <div id="class-quiz"></div>
        <div id="class-end"></div>
      </article>
      <section id="class-notebook">
        <header id="class-nb-bar"><span class="dot"></span><span>notebook.ipynb</span><span id="class-nb-state">Python 3 · ready</span></header>
        <div id="class-cells"></div>
      </section>
      <div id="class-chat"></div>`;
    document.body.appendChild(root);
  }

  // ------------------------------------------------------------ layout
  function enter(t, s) {
    build();
    teacher = t === "luna" ? "luna" : t || "mika";
    sidekick = s || (teacher === "mika" ? "luna" : "mika");
    document.documentElement.classList.add("class-mode");
    clearInterval(layoutTimer);
    let tries = 0;
    const place = () => {
      const ok = window.vrRoom && typeof window.vrRoom.classroom === "function" && window.vrRoom.classroom(teacher, sidekick);
      if (ok || ++tries > 60) clearInterval(layoutTimer);
    };
    layoutTimer = setInterval(place, 500);
    place();
  }

  function leave() {
    clearInterval(layoutTimer);
    document.documentElement.classList.remove("class-mode");
    if (window.teachingStage && window.teachingStage.normal) window.teachingStage.normal();
  }

  function header(op) {
    $("class-course").textContent = op.course || "Class";
    $("class-lesson").textContent = op.lesson || "";
    const total = Number(op.total) || 0, number = Number(op.number) || 0;
    $("class-count").textContent = number > 0 && total ? `Lesson ${number} of ${total}` : "";
    $("class-progress").querySelector("b").style.width = total && number > 0 ? `${Math.min(100, (number / total) * 100)}%` : "0%";
  }

  // ------------------------------------------------------------ slide
  function renderMath(target, latex) {
    if (window.katex) {
      try { window.katex.render(String(latex), target, { throwOnError: false, displayMode: true }); return; } catch (err) { /* fall through */ }
    }
    target.textContent = latex;
  }

  function visual(v) {
    const box = $("class-visual");
    box.replaceChildren();
    if (!v || typeof v !== "object") return;
    if (v.type === "list") {
      const wrap = el("div", "cv-list");
      if (v.name) wrap.appendChild(el("div", "cv-name", `${v.name} =`));
      const hi = new Set(Array.isArray(v.highlight) ? v.highlight : []);
      (v.items || []).forEach((item, i) => {
        const cell = el("div", "cv-cell" + (hi.has(i) ? " hi" : ""));
        cell.appendChild(el("b", "", item));
        cell.appendChild(el("small", "", i));
        wrap.appendChild(cell);
      });
      box.appendChild(wrap);
    } else if (v.type === "vars") {
      const wrap = el("div", "cv-vars");
      (v.items || []).forEach(([name, value]) => {
        const item = el("div", "cv-var");
        item.appendChild(el("span", "", name));
        item.appendChild(el("b", "", value));
        wrap.appendChild(item);
      });
      box.appendChild(wrap);
    } else if (v.type === "math" && v.latex) {
      const m = el("div", "cv-math");
      renderMath(m, v.latex);
      box.appendChild(m);
    } else if (v.type === "table") {
      const table = el("table", "cv-table");
      (v.rows || []).forEach((row) => {
        const tr = el("tr");
        row.forEach((c) => tr.appendChild(el("td", "", c)));
        table.appendChild(tr);
      });
      box.appendChild(table);
    }
    box.classList.remove("class-enter"); void box.offsetWidth; box.classList.add("class-enter");
  }

  function slide(s) {
    if (!s) return;
    $("class-end").classList.remove("show");
    const body = $("class-slide-body");
    body.querySelector("h1").textContent = s.title || "";
    const ul = body.querySelector("ul");
    ul.replaceChildren();
    (s.bullets || []).forEach((b, i) => {
      const li = el("li", "class-enter", b);
      li.style.animationDelay = `${0.15 + i * 0.18}s`;
      ul.appendChild(li);
    });
    body.classList.remove("class-enter"); void body.offsetWidth; body.classList.add("class-enter");
    visual(s.visual);
  }

  function caption(text) {
    const box = $("class-caption");
    box.textContent = text || "";
    box.classList.toggle("show", !!text);
    clearTimeout(captionTimer);
    if (text) captionTimer = setTimeout(() => box.classList.remove("show"), Math.min(9000, 2000 + text.length * 55));
  }

  // ------------------------------------------------------------ notebook
  const KEYWORDS = new Set("False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield".split(" "));
  const BUILTINS = new Set("print len range list dict set tuple str int float bool sum min max sorted abs round enumerate zip type input map filter isinstance".split(" "));
  const TOKEN = /(#[^\n]*)|("""[\s\S]*?"""|'''[\s\S]*?'''|f?"(?:\\.|[^"\\\n])*"?|f?'(?:\\.|[^'\\\n])*'?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)(?=\s*\()|([A-Za-z_]\w*)/g;

  function highlight(code, into) {
    into.replaceChildren();
    let last = 0;
    code.replace(TOKEN, (match, com, str, num, call, word, at) => {
      if (at > last) into.appendChild(document.createTextNode(code.slice(last, at)));
      let cls = "";
      if (com) cls = "tk-com";
      else if (str) cls = "tk-str";
      else if (num) cls = "tk-num";
      else if (call) cls = KEYWORDS.has(call) ? "tk-kw" : BUILTINS.has(call) ? "tk-bi" : "tk-fn";
      else if (word) cls = KEYWORDS.has(word) ? "tk-kw" : "";
      into.appendChild(cls ? el("span", cls, match) : document.createTextNode(match));
      last = at + match.length;
      return match;
    });
    if (last < code.length) into.appendChild(document.createTextNode(code.slice(last)));
  }

  function cellNode(n) {
    return $("class-cells").querySelector(`[data-n="${n}"]`);
  }

  function scrollDown() {
    const box = $("class-cells");
    requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
  }

  function busy(on, text) {
    $("class-nb-bar").querySelector(".dot").classList.toggle("busy", !!on);
    $("class-nb-state").textContent = text || (on ? "Python 3 · running" : "Python 3 · ready");
  }

  function addCell(op, instant) {
    const box = $("class-cells");
    box.querySelectorAll(".nb-cell.active").forEach((c) => c.classList.remove("active"));
    const old = cellNode(op.n);
    if (old) old.remove();
    const cell = el("div", "nb-cell active");
    cell.dataset.n = op.n;
    cell.appendChild(el("div", "nb-in", `In [${op.n}]:`));
    const holder = el("div");
    if (op.for) holder.appendChild(el("span", "nb-for", `asked by ${op.for}`));
    const pre = el("pre", "nb-code");
    holder.appendChild(pre);
    cell.appendChild(holder);
    box.appendChild(cell);
    while (box.children.length > MAX_CELLS) box.firstElementChild.remove();
    const code = String(op.code || "");
    if (instant) { highlight(code, pre); scrollDown(); return; }
    busy(true, "Python 3 · typing");
    const ms = Math.max(300, Number(op.typing_ms) || 1500);
    const started = performance.now();
    const caret = el("span", "nb-caret");
    const job = { done: false };
    typing.set(Number(op.n), job);
    job.finish = () => { if (job.done) return; job.done = true; highlight(code, pre); };
    const step = (now) => {
      if (job.done) return;
      const shown = Math.min(code.length, Math.ceil(((now - started) / ms) * code.length));
      highlight(code.slice(0, shown), pre);
      pre.appendChild(caret);
      if (shown >= code.length) { job.finish(); busy(true); return; }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    scrollDown();
  }

  function friendlyError(name) {
    const map = {
      NameError: "NameError · Python doesn't know that name",
      TypeError: "TypeError · those types don't mix",
      IndexError: "IndexError · that position doesn't exist",
      KeyError: "KeyError · that key isn't in the dictionary",
      AttributeError: "AttributeError · that thing has no such method",
      SyntaxError: "SyntaxError · Python couldn't read that",
      ZeroDivisionError: "ZeroDivisionError · can't divide by zero",
      ValueError: "ValueError · right type, wrong value",
      Timeout: "Took too long · the notebook restarted",
    };
    return map[name] || name || "Error";
  }

  function addOutput(op, instant) {
    const n = Number(op.n);
    const job = typing.get(n);
    if (job && job.finish) job.finish();
    typing.delete(n);
    busy(false);
    let cell = cellNode(n);
    if (!cell) { addCell({ n, code: op.code || "" }, true); cell = cellNode(n); }
    cell.querySelectorAll(".nb-out,.nb-result").forEach((x) => x.remove());
    const out = el("div", "nb-result" + (instant ? "" : " enter"));
    if (op.stdout) out.appendChild(el("pre", "", op.stdout));
    if (op.error) {
      const box = el("div", "nb-error");
      box.appendChild(el("b", "", friendlyError(op.error.name)));
      box.appendChild(el("pre", "", op.error.trace || op.error.value || ""));
      out.appendChild(box);
    }
    (op.images || []).forEach((b64) => {
      const img = el("img");
      img.alt = "plot";
      img.src = `data:image/png;base64,${b64}`;
      img.onload = scrollDown;
      out.appendChild(img);
    });
    const result = op.result;
    let label = "";
    if (result && (result.latex || result.text)) {
      label = `Out[${n}]:`;
      if (result.latex) {
        const m = el("div", "latex");
        renderMath(m, result.latex);
        out.appendChild(m);
        // What Python printed, under the pretty math (also a safety net if
        // the math font cannot be drawn).
        if (result.text) out.appendChild(el("pre", "value plain", result.text));
      } else {
        out.appendChild(el("pre", "value", result.text));
      }
    }
    if (!out.childNodes.length) return scrollDown();
    const gutter = el("div", "nb-out", label);
    cell.appendChild(gutter);
    cell.appendChild(out);
    scrollDown();
  }

  // ------------------------------------------------------------ quiz
  function quiz(op) {
    const box = $("class-quiz");
    box.replaceChildren();
    box.appendChild(el("span", "tag", "QUIZ TIME"));
    box.appendChild(el("h2", "", op.question || ""));
    (op.choices || []).forEach((choice, i) => {
      const row = el("div", "cq-choice");
      row.appendChild(el("i"));
      row.appendChild(el("b", "", "ABCD"[i]));
      row.appendChild(el("span", "", choice));
      row.appendChild(el("em", "", ""));
      box.appendChild(row);
    });
    const timer = el("div", "timer");
    const bar = el("i");
    timer.appendChild(bar);
    box.appendChild(timer);
    box.appendChild(el("div", "hint", "Type A, B or C in chat!"));
    box.classList.add("show");
    const seconds = Math.max(3, Number(op.seconds) || 20);
    const end = performance.now() + seconds * 1000;
    cancelAnimationFrame(quizTimer);
    const tick = (now) => {
      const left = Math.max(0, end - now) / (seconds * 1000);
      bar.style.transform = `scaleX(${left})`;
      if (left > 0) quizTimer = requestAnimationFrame(tick);
    };
    quizTimer = requestAnimationFrame(tick);
  }

  function quizResult(op) {
    cancelAnimationFrame(quizTimer);
    const box = $("class-quiz");
    const rows = box.querySelectorAll(".cq-choice");
    const counts = op.counts || [];
    const total = counts.reduce((a, b) => a + b, 0);
    rows.forEach((row, i) => {
      row.classList.toggle("right", i === op.answer);
      row.querySelector("i").style.width = total ? `${(counts[i] / total) * 100}%` : "0%";
      row.querySelector("em").textContent = total ? `${counts[i] || 0}` : "";
    });
    const hint = box.querySelector(".hint");
    if (hint) {
      const winners = op.winners || [];
      hint.textContent = winners.length ? `Correct: ${winners.join(", ")}` : `The answer is ${"ABCD"[op.answer] || "?"}`;
    }
    setTimeout(() => box.classList.remove("show"), 9000);
  }

  // ------------------------------------------------------------ chat + end
  function chat(op) {
    const box = $("class-chat");
    box.replaceChildren(el("small", "", `CHAT · ${op.author || "viewer"}`), el("div", "", op.text || ""));
    box.classList.add("show");
    clearTimeout(chatTimer);
    chatTimer = setTimeout(() => box.classList.remove("show"), 15000);
  }

  function end(op) {
    const box = $("class-end");
    box.replaceChildren();
    const inner = el("div");
    inner.appendChild(el("h2", "", "Lesson complete!"));
    inner.appendChild(el("p", "", op.summary || op.title || ""));
    box.appendChild(inner);
    box.classList.add("show");
  }

  // ------------------------------------------------------------ entry points
  function apply(op) {
    if (!op || typeof op !== "object") return;
    switch (op.kind) {
      case "start":
        enter(op.teacher, op.sidekick);
        header(op);
        $("class-cells").replaceChildren();
        $("class-quiz").classList.remove("show");
        $("class-end").classList.remove("show");
        busy(false);
        break;
      case "slide": build(); slide(op.slide); break;
      case "cell": build(); addCell(op, false); break;
      case "output": build(); addOutput(op, false); break;
      case "quiz": build(); quiz(op); break;
      case "quiz_result": build(); quizResult(op); break;
      case "caption": build(); caption(op.text); break;
      case "chat": build(); chat(op); break;
      case "chat_done": setTimeout(() => $("class-chat") && $("class-chat").classList.remove("show"), 4000); break;
      case "end": build(); end(op); break;
      case "stop": leave(); break;
    }
  }

  function restore(view) {
    if (!view || !view.active) return;
    enter(view.teacher, view.sidekick);
    header(view);
    if (view.slide) slide(view.slide);
    $("class-cells").replaceChildren();
    (view.cells || []).forEach((cell) => {
      addCell(cell, true);
      if (cell.output) addOutput({ n: cell.n, ...cell.output }, true);
    });
    if (view.quiz) quiz({ ...view.quiz, seconds: 10 });
  }

  window.classStage = { apply, restore };
})();
