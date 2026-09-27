/*
 * VR Agent Scene compositor: the environment around Mika and Luna.
 *
 * Draws the scene the backend's world state names (env kit, time, weather):
 *
 *   sky (static)  far  mid  near      <- behind the characters
 *   characters and world props        (room.js, sorted by their feet)
 *   fore                               <- framing in front of them
 *   lighting (multiply tint + flashes)
 *   weather particles (rain, fog, fireflies, leaves, snow)
 *
 * Long-distance travel is never shown as walking (the Live2D models have no
 * walk cycle). A scene change is a TRAVERSAL: the layers slide past with
 * parallax while the light dips, or Mika's magic carries everyone there, or a
 * quiet fade; then the new place slides in and a title card names it.
 *
 * Weather sprites are pooled with hard caps; old scene textures are destroyed
 * after a transition, so hours of travel never grow GPU memory.
 */
(() => {
  "use strict";

  const ENV_URL = "./world/env/";
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  const ease = (k) => 0.5 - Math.cos(Math.PI * clamp(k, 0, 1)) / 2;

  // Parallax factors and how each kit's light falls on the characters.
  const PARALLAX = { far: 0.15, mid: 0.4, near: 0.8, fore: 1.25 };
  const KITS = {
    city: { tint: 0x8a86c8, tintAlpha: 0.22, ambience: ["city_night"] },
    outskirts: { tint: 0xf0a88c, tintAlpha: 0.16, ambience: ["forest_day"] },
    forest: { tint: 0x6c8cc0, tintAlpha: 0.26, ambience: ["forest_night"] },
    deep_forest: { tint: 0x5aa0a8, tintAlpha: 0.3, ambience: ["forest_night"] },
    river: { tint: 0xf4c8c8, tintAlpha: 0.1, ambience: ["river"] },
    mountain: { tint: 0xc8d0e0, tintAlpha: 0.12, ambience: ["wind"] },
    valley: { tint: 0xe09080, tintAlpha: 0.2, ambience: ["wind"] },
    ruins: { tint: 0x8a78d8, tintAlpha: 0.3, ambience: ["ruins"] },
    camp: { tint: 0xd89060, tintAlpha: 0.22, ambience: ["forest_night", "campfire"], kit: "forest" },
  };
  const WEATHER_AMBIENCE = { rain: "rain", storm: "rain", drizzle: "rain", wind: "wind" };
  const CAPS = { rain: 170, drizzle: 70, storm: 220, snow: 120, fireflies: 34, leaves: 26, fog: 5 };

  function createScene(ctx) {
    const { app, stageW, stageH, log, layers } = ctx;
    const stats = { transitions: 0, errors: 0, missing: new Set(), textures: 0 };
    const back = new PIXI.Container(); // sky, far, mid, near (behind characters)
    const fore = new PIXI.Container(); // fore (in front of characters)
    const weatherBack = new PIXI.Container(); // fog behind characters
    const weatherFront = new PIXI.Container(); // rain, fireflies, leaves in front
    layers.envBack.addChild(back, weatherBack);
    layers.fore.addChild(fore);
    layers.weather.addChild(weatherFront);

    // Lighting: a multiply tint that makes the characters belong to the scene,
    // plus an additive flash for lightning and magic.
    const tint = new PIXI.Sprite(PIXI.Texture.WHITE);
    tint.width = stageW * 1.2;
    tint.height = stageH * 1.2;
    tint.position.set(-stageW * 0.1, -stageH * 0.1);
    tint.blendMode = PIXI.BLEND_MODES.MULTIPLY;
    tint.alpha = 0;
    const flash = new PIXI.Sprite(PIXI.Texture.WHITE);
    flash.width = tint.width;
    flash.height = tint.height;
    flash.position.copyFrom(tint.position);
    flash.blendMode = PIXI.BLEND_MODES.ADD;
    flash.alpha = 0;
    const dim = new PIXI.Sprite(PIXI.Texture.WHITE); // transition dip
    dim.width = tint.width;
    dim.height = tint.height;
    dim.position.copyFrom(tint.position);
    dim.tint = 0x05060f;
    dim.alpha = 0;
    layers.light.addChild(tint, flash, dim);

    const state = {
      current: null, // {env, set: {sky, far, mid, near, fore}, textures: []}
      scene: null, // last applied scene snapshot
      offset: 0, // parallax offset in stage px
      transition: null,
      weather: "clear",
      tintTarget: { color: 0xffffff, alpha: 0 },
      flashDecay: 0,
    };

    // ---- textures ----------------------------------------------------------
    function loadKit(env) {
      const kit = (KITS[env] && KITS[env].kit) || env;
      const names = ["sky.jpg", "far.png", "mid.png", "near.png", "fore.png"];
      return Promise.all(
        names.map((n) =>
          PIXI.Texture.fromURL(ENV_URL + kit + "/" + n).catch(() => {
            stats.missing.add(kit + "/" + n);
            return null;
          }),
        ),
      ).then(([sky, far, mid, near, foreT]) => ({ sky, far, mid, near, fore: foreT }));
    }

    function buildSet(env, tex) {
      const set = { env, parts: {}, textures: Object.values(tex).filter(Boolean) };
      if (tex.sky) {
        const s = new PIXI.Sprite(tex.sky);
        s.width = stageW * 1.04;
        s.height = stageH * 1.04;
        s.position.set(-stageW * 0.02, -stageH * 0.02);
        set.parts.sky = s;
      }
      for (const name of ["far", "mid", "near", "fore"]) {
        if (!tex[name]) continue;
        const t = new PIXI.TilingSprite(tex[name], stageW * 1.04, stageH * 1.04);
        t.position.set(-stageW * 0.02, -stageH * 0.02);
        t.tileScale.set((stageW * 1.04) / tex[name].width, (stageH * 1.04) / tex[name].height);
        set.parts[name] = t;
      }
      set.back = new PIXI.Container();
      set.front = new PIXI.Container();
      ["sky", "far", "mid", "near"].forEach((n) => set.parts[n] && set.back.addChild(set.parts[n]));
      if (set.parts.fore) set.front.addChild(set.parts.fore);
      return set;
    }

    function mount(set) {
      back.addChildAt(set.back, 0);
      fore.addChild(set.front);
      layers.roomBackground.visible = false;
    }

    function unmount(set, destroyTextures) {
      if (!set) return;
      back.removeChild(set.back);
      fore.removeChild(set.front);
      set.back.destroy({ children: true });
      set.front.destroy({ children: true });
      if (destroyTextures) {
        for (const t of set.textures) {
          try {
            t.destroy(true);
          } catch (_) {}
        }
      }
    }

    function setOffset(px) {
      state.offset = px;
      const set = state.current;
      if (!set) return;
      for (const [name, f] of Object.entries(PARALLAX)) {
        const p = set.parts[name];
        if (p) p.tilePosition.x = -px * f / p.tileScale.x;
      }
    }

    // ---- title card ------------------------------------------------------
    const title = document.createElement("div");
    title.className = "vrw-title";
    const titleMain = document.createElement("div");
    titleMain.className = "vrw-title-main";
    const titleSub = document.createElement("div");
    titleSub.className = "vrw-title-sub";
    title.append(titleMain, titleSub);
    ctx.domRoot.appendChild(title);
    let titleTimer = null;
    function showTitle(main, sub) {
      titleMain.textContent = String(main || "").slice(0, 40);
      titleSub.textContent = String(sub || "").slice(0, 70);
      title.classList.remove("vrw-show");
      void title.offsetWidth;
      title.classList.add("vrw-show");
      clearTimeout(titleTimer);
      titleTimer = setTimeout(() => title.classList.remove("vrw-show"), 4200);
    }

    // ---- weather ----------------------------------------------------------
    const weatherTex = {};
    function makeTextures() {
      const g = new PIXI.Graphics();
      g.beginFill(0xffffff, 1).drawRect(0, 0, 2, 26).endFill();
      weatherTex.rain = app.renderer.generateTexture(g);
      g.clear().beginFill(0xffffff, 1).drawCircle(6, 6, 6).endFill();
      weatherTex.dot = app.renderer.generateTexture(g);
      g.clear().beginFill(0xffffff, 1).drawEllipse(10, 5, 10, 5).endFill();
      weatherTex.leaf = app.renderer.generateTexture(g);
      g.destroy();
      weatherTex.glow = ctx.worldTexture("glow");
      weatherTex.smoke = ctx.worldTexture("smoke");
    }
    makeTextures();

    const drops = []; // {s, kind, vx, vy, life, ...}
    const spare = [];
    function take(tex) {
      let s = spare.pop();
      if (!s) {
        s = new PIXI.Sprite(tex);
        s.anchor.set(0.5);
      }
      s.texture = tex;
      s.visible = true;
      s.alpha = 1;
      s.rotation = 0;
      s.blendMode = PIXI.BLEND_MODES.NORMAL;
      return s;
    }
    function release(i) {
      const d = drops[i];
      d.s.visible = false;
      if (d.s.parent) d.s.parent.removeChild(d.s);
      spare.push(d.s);
      drops.splice(i, 1);
    }
    function count(kind) {
      let n = 0;
      for (const d of drops) if (d.kind === kind) n++;
      return n;
    }

    function spawnWeather(kind) {
      if (kind === "rain" || kind === "drizzle" || kind === "storm") {
        const s = take(weatherTex.rain);
        s.tint = 0xcfe0ff;
        s.alpha = kind === "drizzle" ? 0.35 : 0.55;
        s.scale.set(1, kind === "drizzle" ? 0.6 : 1);
        s.rotation = 0.18;
        s.position.set(rand(-100, stageW + 100), rand(-60, -10));
        weatherFront.addChild(s);
        drops.push({ s, kind: "rain", vx: -0.25, vy: kind === "drizzle" ? 1.1 : 1.7, life: 0, max: 1400 });
      } else if (kind === "snow") {
        const s = take(weatherTex.dot);
        s.scale.set(rand(0.25, 0.6));
        s.alpha = rand(0.5, 0.9);
        s.position.set(rand(0, stageW), -10);
        weatherFront.addChild(s);
        drops.push({ s, kind, vx: rand(-0.03, 0.03), vy: rand(0.05, 0.12), life: 0, max: 14000, seed: rand(0, 10) });
      } else if (kind === "fireflies") {
        const s = take(weatherTex.glow || weatherTex.dot);
        s.blendMode = PIXI.BLEND_MODES.ADD;
        s.tint = 0xd8ff90;
        s.scale.set(weatherTex.glow ? rand(0.08, 0.14) : 0.4);
        s.position.set(rand(0, stageW), rand(stageH * 0.45, stageH * 0.9));
        weatherFront.addChild(s);
        drops.push({ s, kind, vx: 0, vy: 0, life: 0, max: rand(9000, 16000), seed: rand(0, 100) });
      } else if (kind === "leaves" || kind === "wind") {
        const s = take(weatherTex.leaf);
        s.tint = [0x8fbf6a, 0xd8a050, 0xc07850][Math.floor(Math.random() * 3)];
        s.scale.set(rand(0.5, 1));
        s.position.set(kind === "wind" ? -20 : rand(0, stageW), kind === "wind" ? rand(stageH * 0.3, stageH * 0.9) : -10);
        weatherFront.addChild(s);
        drops.push({ s, kind: "leaves", vx: kind === "wind" ? rand(0.3, 0.5) : rand(-0.05, 0.08), vy: kind === "wind" ? rand(-0.02, 0.04) : rand(0.04, 0.08), life: 0, max: 12000, seed: rand(0, 10) });
      } else if (kind === "fog" && weatherTex.smoke) {
        const s = take(weatherTex.smoke);
        s.alpha = 0;
        s.scale.set(rand(9, 13), rand(2.4, 3.4));
        s.position.set(rand(-200, stageW + 200), rand(stageH * 0.55, stageH * 0.85));
        weatherBack.addChild(s);
        drops.push({ s, kind, vx: rand(0.008, 0.02) * (Math.random() < 0.5 ? -1 : 1), vy: 0, life: 0, max: rand(26000, 40000), peak: rand(0.28, 0.45) });
      }
    }

    function weatherKinds(w) {
      switch (w) {
        case "rain":
          return ["rain"];
        case "drizzle":
          return ["drizzle"];
        case "storm":
          return ["storm", "wind"];
        case "fog":
          return ["fog"];
        case "fireflies":
          return ["fireflies"];
        case "wind":
          return ["wind"];
        case "snow":
          return ["snow"];
        default:
          return [];
      }
    }

    function updateWeather(t, dt) {
      const env = state.scene && state.scene.env;
      let kinds = weatherKinds(state.weather);
      // Every forest night has a few fireflies, even when it is not their "weather".
      if ((env === "forest" || env === "deep_forest" || env === "camp") && state.scene.time === "night" && !kinds.includes("rain"))
        kinds = kinds.concat(kinds.includes("fireflies") ? [] : ["fireflies_few"]);
      for (const k of kinds) {
        const kind = k === "fireflies_few" ? "fireflies" : k;
        const cap = k === "fireflies_few" ? 12 : CAPS[kind] || 0;
        const key = kind === "storm" || kind === "drizzle" ? "rain" : kind === "wind" ? "leaves" : kind;
        const have = count(key);
        const rate = kind === "fog" ? 0.02 : kind === "fireflies" ? 0.06 : kind === "snow" ? 0.3 : kind === "wind" ? 0.05 : 1.2;
        if (have < cap && Math.random() < rate * (dt / 16)) spawnWeather(kind);
      }
      if (state.weather === "storm" && Math.random() < 0.0009 * dt) {
        flash.tint = 0xdde8ff;
        flash.alpha = 0.55;
        if (ctx.onThunder) ctx.onThunder();
      }
      for (let i = drops.length - 1; i >= 0; i--) {
        const d = drops[i];
        d.life += dt;
        const k = d.life / d.max;
        if (d.kind === "rain") {
          d.s.x += d.vx * dt;
          d.s.y += d.vy * dt;
          if (d.s.y > stageH + 30) d.life = d.max;
        } else if (d.kind === "snow" || d.kind === "leaves") {
          d.s.x += (d.vx + Math.sin((t / 1000) * 1.3 + d.seed) * 0.03) * dt;
          d.s.y += d.vy * dt;
          if (d.kind === "leaves") d.s.rotation += 0.003 * dt;
          if (d.s.y > stageH + 20 || d.s.x > stageW + 40) d.life = d.max;
        } else if (d.kind === "fireflies") {
          const ph = t / 1000 + d.seed;
          d.s.x += Math.sin(ph * 0.7) * 0.04 * dt;
          d.s.y += Math.cos(ph * 0.53) * 0.03 * dt;
          d.s.alpha = (0.35 + 0.65 * Math.max(0, Math.sin(ph * 1.9))) * Math.min(1, k * 8, (1 - k) * 8);
        } else if (d.kind === "fog") {
          d.s.x += d.vx * dt;
          d.s.alpha = d.peak * Math.min(1, k * 5, (1 - k) * 5);
        }
        if (d.life >= d.max || !weatherActive(d.kind)) {
          if (d.kind === "fog" && d.s.alpha > 0.02 && !weatherActive("fog")) {
            d.s.alpha -= 0.002 * dt; // fog thins out instead of popping
            if (d.s.alpha > 0.02) continue;
          }
          release(i);
        }
      }
    }

    function weatherActive(kind) {
      const kinds = weatherKinds(state.weather).map((k) => (k === "storm" || k === "drizzle" ? "rain" : k === "wind" ? "leaves" : k));
      if (kind === "fireflies") {
        const env = state.scene && state.scene.env;
        return kinds.includes("fireflies") || ((env === "forest" || env === "deep_forest" || env === "camp") && state.scene.time === "night");
      }
      return kinds.includes(kind);
    }

    // ---- applying a scene -------------------------------------------------
    function ambienceFor(scene) {
      const kit = KITS[scene.env] || {};
      const list = Array.isArray(scene.ambience) && scene.ambience.length ? scene.ambience.slice() : (kit.ambience || []).slice();
      const extra = WEATHER_AMBIENCE[scene.weather];
      if (extra && !list.includes(extra)) list.push(extra);
      return list.slice(0, 3);
    }

    function applyLight(scene) {
      const kit = KITS[scene.env];
      if (!kit) {
        state.tintTarget = { color: 0xffffff, alpha: 0 };
        return;
      }
      let a = kit.tintAlpha;
      if (scene.weather === "rain" || scene.weather === "storm") a += 0.1;
      if (scene.weather === "fog") a += 0.04;
      state.tintTarget = { color: kit.tint, alpha: clamp(a, 0, 0.5) };
    }

    function apply(scene) {
      if (!scene || typeof scene !== "object") return;
      const prev = state.scene;
      state.scene = scene;
      state.weather = String(scene.weather || "clear");
      applyLight(scene);
      if (ctx.audio) ctx.audio.setAmbience(scene.env && scene.env !== "room" ? ambienceFor(scene) : [], 3);
      if (ctx.onActorScale) ctx.onActorScale(Number(scene.actor_scale) || 1);
      const envChanged = !prev || prev.env !== scene.env || !state.current;
      if (!scene.env || scene.env === "room" || !KITS[scene.env]) {
        if (state.current) {
          const old = state.current;
          state.current = null;
          unmount(old, true);
        }
        layers.roomBackground.visible = true;
        return;
      }
      if (!envChanged) {
        if (scene.title && scene.transition) showTitle(scene.title, scene.subtitle);
        return;
      }
      traverse(scene, prev);
    }

    function traverse(scene, prev) {
      const style = state.current ? String(scene.transition || "travel") : "instant";
      const t0 = ctx.now();
      const dir = Number(scene.direction) < 0 ? -1 : 1;
      stats.transitions += 1;
      const pending = loadKit(scene.env);
      const tr = { style, start: t0, dir, swapped: false, set: null, ready: false, scene, fromOffset: state.offset, ms: style === "instant" ? 0 : style === "magic" ? 3200 : style === "fade" ? 2600 : 4200 };
      state.transition = tr;
      pending.then((tex) => {
        tr.set = buildSet(scene.env, tex);
        tr.ready = true;
        if (style === "instant") finishSwap(tr);
      });
      if (style === "travel" && ctx.audio) {
        ctx.audio.playSfx("whoosh", dir * 0.3);
        setTimeout(() => ctx.audio.playSfx("footsteps", 0), 300);
      }
      if (style === "magic" && ctx.onMagicTravel) ctx.onMagicTravel();
    }

    function finishSwap(tr) {
      if (tr.swapped || !tr.ready) return;
      tr.swapped = true;
      const old = state.current;
      state.current = tr.set;
      mount(tr.set);
      setOffset(tr.style === "travel" ? -tr.dir * stageW * 0.5 : 0);
      if (old) unmount(old, true);
      stats.textures = tr.set.textures.length;
      if (tr.scene.title) showTitle(tr.scene.title, tr.scene.subtitle);
    }

    function updateTransition(t) {
      const tr = state.transition;
      if (!tr) return;
      if (tr.style === "instant") {
        if (tr.swapped) state.transition = null;
        return;
      }
      const k = clamp((t - tr.start) / tr.ms, 0, 1);
      if (tr.style === "travel") {
        // slide the old place away while the light dips, swap in the dark,
        // then the new place slides in: the world moved, not the girls.
        if (!tr.swapped) {
          const kk = ease(Math.min(1, k / 0.45));
          setOffset(tr.fromOffset + tr.dir * stageW * 0.5 * kk);
          dim.alpha = kk * 0.92;
          if (k >= 0.45) {
            if (tr.ready) finishSwap(tr);
            else tr.start = t - tr.ms * 0.45; // hold in the dip until the new place has loaded
          }
        } else {
          const kk = ease((k - 0.5) / 0.5);
          setOffset(-tr.dir * stageW * 0.5 * (1 - kk));
          dim.alpha = 0.92 * (1 - kk);
        }
      } else if (tr.style === "magic") {
        if (!tr.swapped) {
          flash.tint = 0xe6d2ff;
          flash.alpha = Math.max(flash.alpha, ease(k / 0.4) * 0.85);
          if (k >= 0.4) {
            if (tr.ready) finishSwap(tr);
            else tr.start = t - tr.ms * 0.4;
          }
        } else {
          flash.alpha = 0.85 * (1 - ease((k - 0.4) / 0.6));
        }
      } else {
        if (!tr.swapped) {
          dim.alpha = ease(k / 0.45);
          if (k >= 0.45) {
            if (tr.ready) finishSwap(tr);
            else tr.start = t - tr.ms * 0.45;
          }
        } else dim.alpha = 1 - ease((k - 0.55) / 0.45);
      }
      if (k >= 1 && tr.swapped) {
        dim.alpha = 0;
        state.transition = null;
      }
    }

    function update(t, dt) {
      try {
        updateTransition(t);
        updateWeather(t, dt);
        const tt = state.tintTarget;
        tint.tint = tt.color;
        tint.alpha += (tt.alpha - tint.alpha) * 0.02;
        if (!state.transition || state.transition.style !== "magic") flash.alpha = Math.max(0, flash.alpha - 0.0025 * dt);
        // the faintest breathing of the far layers so the scene never looks frozen
        if (state.current && !state.transition) {
          const drift = Math.sin(t / 9000) * 6;
          const p = state.current.parts;
          if (p.far) p.far.tilePosition.y = drift * 0.3 / p.far.tileScale.y;
          if (p.fore) p.fore.tilePosition.y = Math.sin(t / 3100) * 1.5 / p.fore.tileScale.y;
        }
      } catch (err) {
        stats.errors += 1;
        if (stats.errors < 20) log("scene update failed", err && err.message);
      }
    }

    function snapshot() {
      return {
        env: state.current ? state.current.env : "room",
        scene: state.scene ? state.scene.id : null,
        weather: state.weather,
        transition: state.transition ? state.transition.style : null,
        dim: Math.round(dim.alpha * 100) / 100,
        flash: Math.round(flash.alpha * 100) / 100,
        tint: Math.round(tint.alpha * 100) / 100,
        drops: drops.length,
        spare: spare.length,
        transitions: stats.transitions,
        textures: stats.textures,
        missing: [...stats.missing],
        errors: stats.errors,
        title: title.classList.contains("vrw-show") ? titleMain.textContent : "",
      };
    }

    return { apply, update, state: snapshot, showTitle };
  }

  window.VRRoomScene = { createScene, KITS };
})();
