/*
 * VR Agent World renderer: draws what the backend's world state says exists.
 *
 * - Typed world objects (frog, campfire, signpost ...) from "object" ops,
 *   at the position the backend owns. Nothing here invents objects: if an
 *   object is on screen, RoomState has it, and the characters know it.
 * - Short-lived effects ("world_fx": magic bolts, poofs, sparkles) from a
 *   pooled particle system with a hard cap, so hours of streaming never
 *   grow memory.
 * - Scene environments (layered, parallax, weather) are added by
 *   VRRoomWorld.environments when a scene op names an env kit.
 *
 * Every asset is optional: a missing texture just means that object is not
 * drawn (and is logged once); the room and the characters keep working.
 */
(() => {
  "use strict";

  const PROP_URL = "./world/props/";
  const MAX_PARTICLES = 220;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);

  // Per type: how big it is on the 1920x1080 stage (px height) and its idle life.
  const KINDS = {
    frog: { h: 92, idle: "hop", glow: null },
    rabbit: { h: 112, idle: "hop", glow: null },
    butterfly: { h: 64, idle: "flutter", glow: 0x88ddff },
    owl: { h: 150, idle: "sway", glow: null },
    bush: { h: 150, idle: "rustle", glow: null },
    rock: { h: 80, idle: null, glow: null },
    boulder: { h: 190, idle: null, glow: null },
    campfire: { h: 105, idle: "fire", glow: 0xffa050 },
    lantern: { h: 150, idle: "swing", glow: 0xffb070 },
    signpost: { h: 230, idle: null, glow: null },
    glow_stone: { h: 86, idle: null, glow: 0x80e0ff },
    crystal: { h: 170, idle: null, glow: 0xb49cff },
    mushrooms: { h: 84, idle: null, glow: 0x70d8ff },
    shrine: { h: 170, idle: null, glow: 0xffd080 },
    fallen_log: { h: 76, idle: null, glow: null },
    rope_bridge: { h: 110, idle: null, glow: null },
    map_scroll: { h: 46, idle: null, glow: null },
  };
  const GLOWING_STATES = new Set(["glowing", "burning"]);

  function createWorld(ctx) {
    const { app, stageW, stageH, log } = ctx;
    const textures = new Map(); // name -> PIXI.Texture | null (failed)
    const objects = new Map(); // id -> {spec, root, sprite, glow, fx, born, seed}
    const layers = ctx.layers; // {props: sortable characters container, fx: container}
    const stats = { spawned: 0, removed: 0, fx: 0, missing: new Set(), errors: 0 };

    // ---- textures -------------------------------------------------------------
    function texture(name) {
      if (textures.has(name)) return textures.get(name);
      textures.set(name, null);
      PIXI.Texture.fromURL(PROP_URL + name + ".png")
        .then((tex) => {
          textures.set(name, tex);
          for (const obj of objects.values()) if (obj.waiting === name) dress(obj);
        })
        .catch(() => {
          stats.missing.add(name);
          log("world texture missing", name);
        });
      return null;
    }
    ["glow", "spark", "smoke", "campfire_fx"].forEach(texture);

    // ---- particles (pooled) ---------------------------------------------------
    const pool = [];
    const live = [];
    const fxLayer = new PIXI.Container();
    layers.fx.addChild(fxLayer);

    function emit(p) {
      if (live.length >= MAX_PARTICLES) return null;
      const tex = textures.get(p.tex || "spark");
      if (!tex) return null;
      let s = pool.pop();
      if (!s) {
        s = new PIXI.Sprite(tex);
        s.anchor.set(0.5);
      }
      s.texture = tex;
      s.blendMode = p.add === false ? PIXI.BLEND_MODES.NORMAL : PIXI.BLEND_MODES.ADD;
      s.tint = p.tint == null ? 0xffffff : p.tint;
      s.position.set(p.x, p.y);
      s.visible = true;
      fxLayer.addChild(s);
      const part = {
        s,
        x: p.x,
        y: p.y,
        vx: p.vx || 0,
        vy: p.vy || 0,
        g: p.g || 0,
        drag: p.drag == null ? 0.985 : p.drag,
        life: 0,
        max: p.life || 800,
        size: p.size || 0.4,
        grow: p.grow || 0,
        spin: p.spin || 0,
        alpha: p.alpha == null ? 1 : p.alpha,
      };
      live.push(part);
      return part;
    }

    function updateParticles(dt) {
      for (let i = live.length - 1; i >= 0; i--) {
        const p = live[i];
        p.life += dt;
        if (p.life >= p.max) {
          p.s.visible = false;
          fxLayer.removeChild(p.s);
          pool.push(p.s);
          live.splice(i, 1);
          continue;
        }
        const k = p.life / p.max;
        p.vx *= p.drag;
        p.vy = p.vy * p.drag + p.g * dt;
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.s.position.set(p.x, p.y);
        p.s.scale.set(p.size * (1 + p.grow * k));
        p.s.rotation += p.spin * dt;
        p.s.alpha = p.alpha * (k < 0.15 ? k / 0.15 : 1 - (k - 0.15) / 0.85);
      }
    }

    function sparkles(x, y, n, tint, spread) {
      for (let i = 0; i < n; i++) {
        const a = Math.random() * Math.PI * 2;
        const v = rand(0.04, 0.16) * (spread || 1);
        emit({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 0.03, life: rand(500, 1100), size: rand(0.18, 0.42), tint, spin: rand(-0.004, 0.004) });
      }
    }

    function poof(x, y) {
      for (let i = 0; i < 10; i++) {
        const a = Math.random() * Math.PI * 2;
        emit({ tex: "smoke", add: false, x: x + rand(-20, 20), y: y + rand(-16, 10), vx: Math.cos(a) * 0.05, vy: -rand(0.02, 0.06), life: rand(700, 1200), size: rand(0.5, 0.9), grow: 0.8, alpha: 0.85 });
      }
      sparkles(x, y, 14, 0xffe0ff, 1.3);
    }

    // ---- objects ----------------------------------------------------------------
    function place(obj) {
      const s = obj.spec;
      obj.root.position.set(s.x * stageW, s.y * stageH);
      obj.root.zIndex = Math.round(s.y * stageH) - 2; // characters' feet sort against this
    }

    function dress(obj) {
      const tex = textures.get(obj.spec.type);
      if (!tex) {
        obj.waiting = obj.spec.type;
        return;
      }
      obj.waiting = null;
      const kind = KINDS[obj.spec.type] || { h: 100 };
      if (!obj.sprite) {
        obj.sprite = new PIXI.Sprite(tex);
        obj.sprite.anchor.set(0.5, 0.96);
        obj.root.addChild(obj.sprite);
      }
      obj.sprite.texture = tex;
      obj.baseScale = kind.h / tex.height;
      obj.sprite.scale.set(obj.baseScale * (obj.born ? 0.01 : 1));
      if (kind.glow && !obj.glow && textures.get("glow")) {
        obj.glow = new PIXI.Sprite(textures.get("glow"));
        obj.glow.anchor.set(0.5);
        obj.glow.blendMode = PIXI.BLEND_MODES.ADD;
        obj.glow.tint = kind.glow;
        obj.glow.position.set(0, -kind.h * 0.45);
        obj.glow.scale.set((kind.h * 2.2) / 256);
        obj.root.addChildAt(obj.glow, 0);
      }
      if (obj.spec.type === "campfire" && !obj.flame && textures.get("campfire_fx")) {
        obj.flame = new PIXI.Sprite(textures.get("campfire_fx"));
        obj.flame.anchor.set(0.5, 1);
        obj.flame.position.set(0, -kind.h * 0.28);
        obj.flameScale = (kind.h * 1.25) / textures.get("campfire_fx").height;
        obj.root.addChild(obj.flame);
      }
    }

    function applyObject(op) {
      const id = String(op.id || "");
      if (!/^[a-z][a-z0-9_]{0,23}$/.test(id)) return;
      const existing = objects.get(id);
      if (op.remove) {
        if (existing) {
          if (op.effect === "poof") poof(existing.root.position.x, existing.root.position.y - (KINDS[existing.spec.type] || { h: 80 }).h * 0.45);
          removeObject(id);
        }
        return;
      }
      if (!op.type || op.by_motion || !KINDS[op.type] || op.visible === false) {
        if (existing) removeObject(id);
        return;
      }
      const spec = { id, type: op.type, x: clamp(Number(op.x) || 0.5, 0, 1), y: clamp(Number(op.y) || 0.9, 0, 1.05), state: String(op.state || "") };
      if (existing && existing.spec.type === spec.type) {
        existing.spec = spec;
        place(existing);
        return;
      }
      if (existing) removeObject(id);
      const root = new PIXI.Container();
      const obj = { spec, root, sprite: null, glow: null, flame: null, born: ctx.now(), seed: Math.random() * 1000, waiting: null, baseScale: 1 };
      objects.set(id, obj);
      layers.props.addChild(root);
      place(obj);
      texture(spec.type);
      dress(obj);
      stats.spawned += 1;
      if (!op.quiet) sparkles(root.position.x, root.position.y - 30, 6, 0xffffff, 0.6);
    }

    function removeObject(id) {
      const obj = objects.get(id);
      if (!obj) return;
      objects.delete(id);
      layers.props.removeChild(obj.root);
      obj.root.destroy({ children: true }); // textures stay cached and shared
      stats.removed += 1;
    }

    function clearObjects() {
      for (const id of [...objects.keys()]) removeObject(id);
    }

    function updateObjects(t) {
      for (const obj of objects.values()) {
        if (!obj.sprite) continue;
        const kind = KINDS[obj.spec.type] || {};
        const age = t - obj.born;
        const pop = age < 450 ? easeOutBack(age / 450) : 1;
        let sx = obj.baseScale * pop;
        let sy = obj.baseScale * pop;
        let dy = 0;
        let rot = 0;
        const ph = (t + obj.seed) / 1000;
        if (kind.idle === "hop" || obj.spec.state === "hopping") {
          const cycle = (ph % 5.2) / 5.2;
          if (cycle > 0.86) {
            const k = (cycle - 0.86) / 0.14;
            dy = -Math.sin(k * Math.PI) * 26;
            sy *= 1 + Math.sin(k * Math.PI) * 0.06;
          } else {
            sy *= 1 + Math.sin(ph * 2.2) * 0.02; // breathing
          }
        } else if (kind.idle === "flutter") {
          dy = Math.sin(ph * 2.1) * 18;
          sx *= 0.75 + Math.abs(Math.sin(ph * 9)) * 0.25;
        } else if (kind.idle === "sway" || kind.idle === "swing") {
          rot = Math.sin(ph * 1.3) * 0.05;
        } else if (kind.idle === "rustle" && obj.spec.state === "rustling") {
          rot = Math.sin(ph * 22) * 0.025;
        }
        if (obj.spec.state === "croaking") sy *= 1 + Math.max(0, Math.sin(ph * 7)) * 0.05;
        obj.sprite.scale.set(sx, sy);
        obj.sprite.position.y = dy;
        obj.sprite.rotation = rot;
        if (obj.glow) {
          const on = !["dim", "out"].includes(obj.spec.state);
          const target = on ? 0.55 + Math.sin(ph * 2.4) * 0.12 + (GLOWING_STATES.has(obj.spec.state) ? 0.25 : 0) : 0.08;
          obj.glow.alpha += (target - obj.glow.alpha) * 0.08;
        }
        if (obj.flame) {
          const lit = obj.spec.state !== "out";
          obj.flame.visible = lit;
          const f = 1 + Math.sin(ph * 11) * 0.05 + Math.sin(ph * 17.3) * 0.04;
          obj.flame.scale.set(obj.flameScale * (1 + Math.sin(ph * 7.7) * 0.03), obj.flameScale * f * (obj.spec.state === "embers" ? 0.45 : 1));
          if (lit && Math.random() < 0.08) emit({ x: obj.root.position.x + rand(-14, 14), y: obj.root.position.y - 70, vx: rand(-0.01, 0.01), vy: -rand(0.05, 0.1), life: rand(700, 1300), size: rand(0.1, 0.2), tint: 0xffa040 });
        }
      }
    }

    function easeOutBack(k) {
      const c = 1.7;
      return 1 + (c + 1) * Math.pow(k - 1, 3) + c * Math.pow(k - 1, 2);
    }

    // ---- effects ------------------------------------------------------------------
    const pending = []; // effects waiting for their delay
    function fx(op) {
      const delay = clamp(Number(op.delay_ms) || 0, 0, 10000);
      pending.push({ at: ctx.now() + delay, op });
    }

    function pointOf(ref) {
      if (!ref) return null;
      const obj = objects.get(ref);
      if (obj) return { x: obj.root.position.x, y: obj.root.position.y - (KINDS[obj.spec.type] || { h: 80 }).h * 0.5 };
      const box = ctx.objectBox(ref);
      if (box) return { x: box.x * stageW, y: box.y * stageH };
      return null;
    }

    const bolts = [];
    function runFx(op) {
      stats.fx += 1;
      const name = String(op.name || "");
      if (name === "magic_bolt") {
        const from = ctx.wandPoint(op.character);
        const to = pointOf(op.target);
        if (!from || !to) return;
        bolts.push({ from, to, start: ctx.now(), ms: clamp(Number(op.ms) || 800, 200, 3000) });
        sparkles(from.x, from.y, 10, 0xffd6ff, 0.8);
      } else if (name === "sparkles") {
        const at = pointOf(op.target) || ctx.facePoint(op.character);
        if (at) sparkles(at.x, at.y, 22, 0xfff0a0, 1.2);
      } else if (name === "poof") {
        const at = pointOf(op.target);
        if (at) poof(at.x, at.y);
      } else if (name === "magic_blink_out" || name === "magic_blink_in") {
        const at = ctx.facePoint(op.character);
        if (at) sparkles(at.x, at.y + 160, 26, 0xe8c8ff, 1.6);
      } else if (ctx.onFx) {
        ctx.onFx(op);
      }
    }

    function updateBolts(t) {
      for (let i = bolts.length - 1; i >= 0; i--) {
        const b = bolts[i];
        const k = clamp((t - b.start) / b.ms, 0, 1);
        const e = k * k * (3 - 2 * k);
        const x = b.from.x + (b.to.x - b.from.x) * e;
        const y = b.from.y + (b.to.y - b.from.y) * e - Math.sin(k * Math.PI) * 90;
        for (let n = 0; n < 2; n++) emit({ x: x + rand(-6, 6), y: y + rand(-6, 6), vx: rand(-0.02, 0.02), vy: rand(-0.02, 0.02), life: rand(300, 600), size: rand(0.2, 0.36), tint: n ? 0xffc8ff : 0xa8e8ff });
        if (k >= 1) {
          sparkles(b.to.x, b.to.y, 18, 0xffe8ff, 1.2);
          bolts.splice(i, 1);
        }
      }
    }

    // ---- frame ----------------------------------------------------------------------
    let last = ctx.now();
    function update(t) {
      const dt = clamp(t - last, 0, 100);
      last = t;
      for (let i = pending.length - 1; i >= 0; i--) {
        if (t >= pending[i].at) {
          const due = pending.splice(i, 1)[0];
          try {
            runFx(due.op);
          } catch (err) {
            stats.errors += 1;
            log("world fx failed", err && err.message);
          }
        }
      }
      if (pending.length > 64) pending.splice(0, pending.length - 64);
      try {
        updateObjects(t);
        updateBolts(t);
        updateParticles(dt);
        if (ctx.onUpdate) ctx.onUpdate(t, dt);
      } catch (err) {
        stats.errors += 1;
        if (stats.errors < 20) log("world update failed", err && err.message);
      }
    }

    function state() {
      return {
        objects: [...objects.values()].map((o) => ({ id: o.spec.id, type: o.spec.type, x: o.spec.x, state: o.spec.state, drawn: !!o.sprite })),
        particles: live.length,
        pooled: pool.length,
        bolts: bolts.length,
        pending: pending.length,
        spawned: stats.spawned,
        removed: stats.removed,
        fx: stats.fx,
        missing: [...stats.missing],
        errors: stats.errors,
      };
    }

    return { applyObject, clearObjects, fx, update, state, emit, sparkles, poof, texture, textures, objects };
  }

  window.VRRoomWorld = { createWorld, KINDS };
})();
