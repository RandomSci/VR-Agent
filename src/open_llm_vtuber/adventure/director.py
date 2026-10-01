"""Adventure Director: Mika and Luna's autonomous, persistent journey.

A deterministic state machine run from the room tick. No LLM, no TTS:

    arrive at a place -> stay a while (quiet moments, ambient events,
    discoveries, obstacles, weather, rests, local banter) -> move on
    (a traversal of the world, never a fake walk) -> next place ...
    objective done -> next objective -> region done -> next region ...
    destination reached -> epilogue -> a new journey

Everything it does goes through the ONE room state: scenes via the World
Director, objects via the World Director, positions via the Stage Director,
magic via Interactions, history via RoomState.record. So the screen, the
state and what the characters are told stay one reality.

Viewer conversation and games pause it at a safe beat (a line finishes, a hop
lands, a traversal completes) and it resumes the same place and activity
afterwards. Progress is saved atomically, so a restart continues the journey.
"""

from __future__ import annotations

import random
import time
from collections import Counter, deque
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from ..room import events as ev
from ..room.state import SceneState
from . import conditions
from .content import Adventure, AdventureLibrary, EventDef, Place
from .dialogue import DialoguePicker, VoiceClips
from .state import AdventureState, StateStore

if TYPE_CHECKING:  # pragma: no cover
    from ..room.session import RoomSession

DEFAULTS = {
    "quiet_min_seconds": 40,  # calm moments between things happening
    "quiet_max_seconds": 95,
    "banter_gap_minutes": 3.0,
    "event_gap_minutes": 2.5,
    "big_event_gap_minutes": 7.0,
    "weather_gap_minutes": 22.0,
    "rest_after_minutes": 45.0,
    "stay_scale": 1.0,  # multiplies every place's stay (tuning or tests)
    "actor_scale": 0.84,
    "save_every_seconds": 30,
    "resume_line_chance": 0.35,
}
LINE_GAP = 0.45
SIDE_PAN = {"LEFT": -0.8, "RIGHT": 0.8, "UP": 0.0, "DOWN": 0.0}


class AdventureDirector:
    def __init__(
        self,
        session: "RoomSession",
        library: AdventureLibrary,
        adventure_id: str = "",
        store: Optional[StateStore] = None,
        clips: Optional[VoiceClips] = None,
        settings: Optional[dict[str, Any]] = None,
        clock=time.time,
    ):
        self.session = session
        self.library = library
        self.store = store or StateStore(None)
        self.clips = clips or VoiceClips(None)
        self.clock = clock
        self.settings = {**DEFAULTS, **(settings or {})}
        self.picker = DialoguePicker(library)
        self.preferred = adventure_id
        self.adventure: Optional[Adventure] = None
        self.state = AdventureState()
        self.enabled = False
        # runtime (not persisted: a restart resumes from the place, calmly)
        self.queue: deque[dict[str, Any]] = deque()
        self.event: Optional[EventDef] = None
        self.focus: str = ""  # object the current event is about
        self.beat_until = 0.0
        self.beat_safe = True
        self.next_decision_at = 0.0
        self.pause_reasons: set[str] = set()
        self.paused = False
        self.paused_for: set[str] = set()
        self.last_tick = 0.0
        self.last_saved = 0.0
        self.last_talk = -999.0
        self.last_event = -999.0
        self.last_big = -999.0
        self.stats: Counter[str] = Counter()
        self.errors: deque[str] = deque(maxlen=20)

    # ------------------------------------------------------------------
    # start and persistence
    # ------------------------------------------------------------------
    def start(self) -> bool:
        if not self.library.adventures:
            logger.warning("Adventure: no adventures loaded; the room stays as it is")
            return False
        now = self.clock()
        self.last_tick = now
        saved = self.store.load()
        if saved and self._valid(saved):
            self.state = saved
            self.adventure = self.library.adventures[saved.adventure_id]
            self.state.prune(set(self.library.events), set(self.library.exchanges))
            logger.info(
                f"Adventure: continuing {self.adventure.title} at "
                f"{self.region().name}, {self.place().name} ({self.progress() * 100:.0f}%)"
            )
            self._enter_place(now, transition="instant", resumed=True)
        else:
            self._new_run(now, run=(saved.run + 1) if saved else 1, completed=saved.completed_runs if saved else 0)
        self.enabled = True
        self._save(now)
        return True

    def _valid(self, s: AdventureState) -> bool:
        adv = self.library.adventures.get(s.adventure_id)
        if not adv:
            return False
        try:
            region = adv.regions[s.region_index]
            region.objectives[s.objective_index].places[s.place_index]
            return True
        except (IndexError, TypeError):
            return False

    def _pick_adventure(self, run: int) -> Adventure:
        ids = sorted(self.library.adventures)
        if self.preferred in self.library.adventures and run == 1:
            return self.library.adventures[self.preferred]
        return self.library.adventures[ids[(run - 1) % len(ids)]]

    def _new_run(self, now: float, run: int, completed: int = 0) -> list[dict[str, Any]]:
        adv = self._pick_adventure(run)
        self.adventure = adv
        old_mood = dict(self.state.mood)
        self.state = AdventureState(
            adventure_id=adv.id,
            run=run,
            seed=f"{adv.id}-{run}-{int(now)}",
            completed_runs=completed,
            mood=old_mood,
        )
        self.state.weather = self._roll_weather(self.region(), exclude=None)
        self.session.state.record("journey", f"Mika and Luna set off on the {adv.title}")
        ops = self._enter_place(now, transition="fade")
        if adv.start_event:
            self._start_event(self.library.events[adv.start_event], now)
        logger.info(f"Adventure: starting {adv.title} (run {run})")
        return ops

    def _save(self, now: float) -> None:
        self.last_saved = now
        self.store.save(self.state)

    def _maybe_save(self, now: float) -> None:
        if now - self.last_saved >= self.settings["save_every_seconds"]:
            self._save(now)

    # ------------------------------------------------------------------
    # where we are
    # ------------------------------------------------------------------
    def region(self):
        return self.adventure.regions[self.state.region_index]

    def objective(self):
        return self.region().objectives[self.state.objective_index]

    def place(self) -> Place:
        return self.objective().places[self.state.place_index]

    def _place_number(self) -> tuple[int, int]:
        all_places = self.adventure.places()
        here = (self.state.region_index, self.state.objective_index, self.state.place_index)
        return all_places.index(here) + 1, len(all_places)

    def progress(self) -> float:
        if not self.adventure:
            return 0.0
        n, total = self._place_number()
        stay = max(0.1, self.state.place_stay)
        within = min(1.0, max(0.0, (self.state.active_minutes - self.state.place_arrived) / stay))
        return min(1.0, (n - 1 + within) / total)

    def _rng(self, salt: str = "") -> random.Random:
        self.state.decisions += 1
        return random.Random(f"{self.state.seed}:{self.state.decisions}:{salt}")

    def _roll_weather(self, region, exclude: Optional[str]) -> str:
        options = {w: v for w, v in region.weather.items() if w != exclude and v > 0} or {"clear": 1}
        rng = self._rng("weather")
        return rng.choices(list(options), weights=list(options.values()), k=1)[0]

    def _characters(self) -> list[str]:
        return self.session.state.available_characters() or list(self.session.state.characters)

    def _who(self, who: Any, last: list[str]) -> list[str]:
        chars = self._characters()
        if who in (None, "all"):
            return chars
        if who == "random":
            return [self._rng("who").choice(chars)] if chars else []
        if who == "other":
            others = [c for c in chars if c not in last]
            return others[:1] or chars[:1]
        if who == "magic":
            users = self.session.interactions.magic_users()
            return users[:1]
        return [who] if who in chars else []

    def context(self) -> conditions.Context:
        s = self.state
        region, place = self.region(), self.place()
        return conditions.Context(
            env=place.env or region.env,
            region=region.id,
            region_tags=frozenset(region.tags),
            time=place.time or region.time,
            weather=s.weather,
            objective=self.objective().id,
            activity=s.activity,
            flags=frozenset(s.flags),
            discovered=frozenset(s.discovered),
            recent=s.recent_ids(30),
            mood=dict(s.mood),
            region_minutes=s.active_minutes - s.region_started,
            since_rest=s.active_minutes - s.last_rest,
            progress=self.progress(),
            last_game=s.last_game,
            place=place.id,
            place_tags=frozenset(place.tags),
            visible=frozenset(o.type for o in self.session.state.visible_objects()),
        )

    # ------------------------------------------------------------------
    # places and traversal
    # ------------------------------------------------------------------
    def _scene(self, title: str = "", subtitle: str = "") -> SceneState:
        region, place = self.region(), self.place()
        return SceneState(
            id=f"{region.id}.{place.id}",
            name=f"{region.name}, {place.name}",
            env=place.env or region.env,
            region=region.id,
            time=place.time or region.time,
            weather=self.state.weather,
            description=region.description,
            zones=dict(place.zones),
            zone_labels=dict(place.zone_labels),
            title=title,
            subtitle=subtitle,
            ambience=list(region.ambience),
            actor_scale=float(self.settings["actor_scale"]),
        )

    def _enter_place(self, now: float, transition: str = "travel", resumed: bool = False, new_region: bool = False) -> list[dict[str, Any]]:
        s, region, place = self.state, self.region(), self.place()
        if new_region or resumed:
            title, subtitle = region.name, place.name[0].upper() + place.name[1:]
        else:
            title, subtitle = place.name[0].upper() + place.name[1:], self.objective().text
        note = "" if resumed else f"they arrived at {place.name} in {region.name}"
        ops = self.session.world.set_scene(self._scene(title, subtitle), note=note, transition=transition)
        for i, prop in enumerate(place.props):
            ops += self.session.world.spawn_object(
                prop.kind,
                zone=prop.zone,
                object_id=prop.id or f"{place.id}_{prop.kind}_{i}"[:24],
                state=prop.state,
            )
        if not resumed:
            s.place_arrived = s.active_minutes
            s.place_stay = self._rng("stay").uniform(*place.stay) * float(self.settings["stay_scale"])
            s.place_counts = {}
        s.phase = "place"
        s.activity = place.activity or "exploring"
        self._set_activity(s.activity)
        self.next_decision_at = now + self._rng("first").uniform(10, 22)
        if not resumed:
            self.queue.append({"say": {"trigger": "arrive"}, "chance": 0.75, "wait": 1.0})
            if place.arrive:
                self._start_event(self.library.events[place.arrive], now, append=True)
        ops.append(self.hud_op())
        self.stats["places"] += 1
        return ops

    def _set_activity(self, text: str) -> None:
        for character in self.session.state.characters.values():
            character.activity = text

    def _advance(self, now: float) -> list[dict[str, Any]]:
        """Leave this place: next place, next objective, next region, or arrive."""
        s, adv = self.state, self.adventure
        objective, region = self.objective(), self.region()
        s.places_done += 1
        s.objective_finishing = False
        next_region = False
        if s.place_index + 1 < len(objective.places):
            s.place_index += 1
        else:
            self.session.state.record("objective", f"they completed their goal: {objective.text}")
            s.add_flag(f"done_{objective.id}")
            s.place_index = 0
            if s.objective_index + 1 < len(region.objectives):
                s.objective_index += 1
            else:
                s.objective_index = 0
                if s.region_index + 1 < len(adv.regions):
                    s.region_index += 1
                    next_region = True
                else:
                    return self._arrive(now)
        if next_region:
            s.region_started = s.active_minutes
            new_region = self.region()
            if s.weather not in new_region.weather:
                s.weather = self._roll_weather(new_region, exclude=None)
        # Magical regions travel by Mika's magic between every place; elsewhere
        # the world slides past (and region changes use the region's style).
        transition = self.region().transition if (next_region or self.region().transition == "magic") else "travel"
        self.queue.clear()
        self.queue.append({"say": {"trigger": "depart_magic" if transition == "magic" else "depart"}, "chance": 0.6})
        self.queue.append({"_traverse": {"transition": transition, "new_region": next_region}})
        if not next_region and s.place_index == 0:
            self.queue.append({"say": {"trigger": "objective_new"}, "chance": 0.8, "wait": 1.0})
        s.phase = "traversing"
        return [self.hud_op()]

    def _arrive(self, now: float) -> list[dict[str, Any]]:
        s = self.state
        s.phase = "arrived"
        s.region_index = len(self.adventure.regions) - 1
        s.objective_index = len(self.region().objectives) - 1
        s.place_index = len(self.objective().places) - 1
        self.session.state.record("journey", f"they reached {self.adventure.destination}")
        s.add_flag("arrived")
        if self.adventure.arrive_event:
            self._start_event(self.library.events[self.adventure.arrive_event], now)
        self.queue.append({"hold": 90})
        self.queue.append({"_complete_run": True})
        return [self.hud_op()]

    # ------------------------------------------------------------------
    # pacing
    # ------------------------------------------------------------------
    def _decide(self, now: float) -> list[dict[str, Any]]:
        s = self.state
        rng = self._rng("decide")
        quiet = lambda: now + rng.uniform(self.settings["quiet_min_seconds"], self.settings["quiet_max_seconds"])  # noqa: E731
        if s.active_minutes - s.place_arrived >= s.place_stay:
            objective = self.objective()
            last_place = s.place_index == len(objective.places) - 1
            if last_place and objective.finish_event and not s.objective_finishing:
                s.objective_finishing = True
                self._start_event(self.library.events[objective.finish_event], now)
                return []
            return self._advance(now)
        a = s.active_minutes
        options: list[tuple[str, float]] = [("quiet", 2.6)]
        if a - self.last_talk >= self.settings["banter_gap_minutes"]:
            options.append(("banter", 2.2))
        if a - self.last_event >= self.settings["event_gap_minutes"]:
            options += [("ambient", 2.0), ("activity", 1.3)]
        if a - self.last_big >= self.settings["big_event_gap_minutes"]:
            options += [("discovery", 1.1), ("obstacle", 0.7), ("setback", 0.4)]
        if a - s.weather_since >= self.settings["weather_gap_minutes"]:
            options.append(("weather", 0.8))
        if self.region().rest_allowed and a - s.last_rest >= self.settings["rest_after_minutes"]:
            options.append(("rest", 3.5))
        for _ in range(4):
            kind = rng.choices([k for k, _ in options], weights=[w for _, w in options], k=1)[0]
            if kind == "quiet":
                self.next_decision_at = quiet()
                self.stats["quiet"] += 1
                return []
            if kind == "banter":
                self.queue.append({"say": {"trigger": "banter"}})
                self.next_decision_at = quiet()
                return []
            if kind == "weather":
                return self._change_weather(now, rng)
            event = self._pick_event(kind, rng)
            if event:
                self._start_event(event, now)
                return []
            options = [o for o in options if o[0] != kind]
        self.next_decision_at = quiet()
        return []

    def _pick_event(self, category: str, rng: random.Random) -> Optional[EventDef]:
        s = self.state
        ctx = self.context()
        pool = []
        for e in self.library.events.values():
            if e.category != category:
                continue
            if e.once and e.id in s.once_done:
                continue
            last = s.event_last.get(e.id)
            if last is not None and s.active_minutes - last < e.cooldown:
                continue
            if s.place_counts.get(e.id, 0) >= e.max_per_place:
                continue
            if conditions.matches(e.when, ctx):
                pool.append(e)
        if not pool:
            return None
        weights = [max(0.01, e.weight) * (1 + 0.4 * conditions.specificity(e.when)) for e in pool]
        return rng.choices(pool, weights=weights, k=1)[0]

    def _start_event(self, event: EventDef, now: float, append: bool = False) -> None:
        s = self.state
        if not append:
            self.queue.clear()
        self.event = event
        s.event_last[event.id] = s.active_minutes
        s.place_counts[event.id] = s.place_counts.get(event.id, 0) + 1
        if event.once and event.id not in s.once_done:
            s.once_done.append(event.id)
        s.remember(event.id, event.note)
        if event.note:
            self.session.state.record(event.category, event.note)
        if event.activity:
            s.activity = event.activity
            self._set_activity(event.activity)
        if event.category in ("discovery", "obstacle", "setback", "milestone"):
            self.last_big = s.active_minutes
        self.last_event = s.active_minutes
        if event.category == "rest":
            s.last_rest = s.active_minutes
        s.phase = "event"
        self.queue.extend(dict(b) for b in event.beats)
        self.stats["events"] += 1
        self.stats[f"event:{event.category}"] += 1

    def _finish_event(self, now: float) -> list[dict[str, Any]]:
        self.event = None
        self.focus = ""
        s = self.state
        if s.phase == "event":
            s.phase = "place"
        s.activity = self.place().activity or "exploring"
        self._set_activity(s.activity)
        rng = self._rng("after")
        self.next_decision_at = now + rng.uniform(self.settings["quiet_min_seconds"], self.settings["quiet_max_seconds"])
        return [self.hud_op()]

    def _change_weather(self, now: float, rng: random.Random) -> list[dict[str, Any]]:
        s = self.state
        new = self._roll_weather(self.region(), exclude=s.weather)
        if new == s.weather:
            self.next_decision_at = now + 60
            return []
        self.queue.append({"weather": new})
        self.queue.append({"say": {"trigger": f"weather_{new}"}, "chance": 0.85, "wait": 1.5})
        self.next_decision_at = now + rng.uniform(self.settings["quiet_min_seconds"], self.settings["quiet_max_seconds"])
        return []

    # ------------------------------------------------------------------
    # beats
    # ------------------------------------------------------------------
    def _run_beat(self, beat: dict[str, Any], now: float) -> tuple[list[dict[str, Any]], float, bool]:
        """(ops, seconds, safe_to_pause_during)."""
        session = self.session
        if "chance" in beat and self._rng("chance").random() >= float(beat["chance"]):
            return [], 0.0, True
        extra = float(beat.get("wait") or 0)
        kind = next((k for k in beat if not k.startswith("_") and k not in ("chance", "wait")), None)
        internal = next((k for k in beat if k.startswith("_")), None)
        value = beat.get(kind) if kind else None
        ops: list[dict[str, Any]] = []
        last: list[str] = beat.get("_last", [])
        self.stats["beats"] += 1

        if internal == "_line":
            return self._line(beat["_line"], extra)
        if internal == "_effects":
            self._apply_effects(beat["_effects"])
            return [], 0.0, True
        if internal == "_traverse":
            spec = beat["_traverse"]
            ops = self._enter_place(now, transition=spec["transition"], new_region=spec["new_region"])
            seconds = {"travel": 4.6, "magic": 3.6, "fade": 3.0}.get(spec["transition"], 3.0)
            self._save(now)
            return ops, seconds, False
        if internal == "_complete_run":
            self.state.completed_runs += 1
            return self._new_run(now, run=self.state.run + 1, completed=self.state.completed_runs), 3.0, False

        if kind == "wait":
            return [], float(value), True
        if kind == "hold":
            return [], float(value), True
        if kind == "sfx":
            name = value.get("name") if isinstance(value, dict) else value
            origin = value.get("from", "") if isinstance(value, dict) else ""
            pan = SIDE_PAN.get(origin)
            if pan is None:
                obj = session.state.objects.get(origin)
                pan = (obj.x * 2 - 1) if obj else 0.0
            ops.append({"op": "world_sfx", "name": name, "pan": round(pan, 2)})
            note = value.get("note") if isinstance(value, dict) else ""
            if note:
                session.state.record("heard", note)
            return ops, 0.4 + extra, True
        if kind == "look":
            at = str(value.get("at"))
            hold = float(value.get("seconds") or 2.6)
            for i, cid in enumerate(self._who(value.get("who", "all"), last)):
                if at == "EACH_OTHER":
                    others = [c for c in self._characters() if c != cid]
                    target = f"CHARACTER:{others[0]}" if others else "NEUTRAL"
                elif at.startswith("object:"):
                    target = "OBJECT:" + at[7:]
                    self.focus = at[7:]
                else:
                    target = at
                op = session.attention_op(cid, target, "adventure", hold, delay_seconds=0.25 * i)
                if op:
                    ops.append(op)
            return ops, 0.3 + extra, True
        if kind == "react":
            for cid in self._who(value.get("who", "random"), last):
                op = self._react(cid, value["reaction"])
                if op:
                    ops.append(op)
            return ops, 0.4 + extra, True
        if kind == "action":
            for cid in self._who(value.get("who"), last):
                ops += session.play(cid, value["name"])
            return ops, float(value.get("seconds") or 1.2) + extra, False
        if kind == "say":
            return self._say(value["trigger"], now), 0.0, True
        if kind == "gesture":
            gtype = value["type"]
            for cid in self._who(value.get("who", "random"), last):
                ok, _, g_ops = session.stage.gesture(cid, "hop" if gtype == "stumble" else gtype)
                ops += g_ops
                if gtype == "stumble":
                    op = self._react(cid, "surprised")
                    if op:
                        ops.append(op)
            return ops, (4.6 if gtype == "dance" else 1.4) + extra, False
        if kind == "glide":
            seconds = 0.0
            for cid in self._who(value.get("who", "random"), last):
                ok, _, g_ops = session.stage.move_to(cid, zone=value["zone"])
                ops += g_ops
                seconds = max(seconds, max((op.get("ms", 0) for op in g_ops), default=0) / 1000)
            return ops, seconds + extra, False
        if kind == "formation":
            ops += self._formation(value)
            return ops, 2.6 + extra, False
        if kind == "spawn":
            ops += session.world.spawn_object(
                value["kind"],
                zone=str(value.get("zone") or "center"),
                object_id=value["id"],
                state=str(value.get("state") or ""),
                seconds=float(value.get("seconds") or 0),
                note=str(value.get("note") or ""),
            )
            return ops, 0.6 + extra, True
        if kind == "remove":
            ops += session.world.remove_object(value["id"], note=str(value.get("note") or ""), effect=str(value.get("effect") or "poof"))
            return ops, 0.6 + extra, True
        if kind == "object_state":
            ops += session.world.set_object_state(value["id"], str(value.get("state") or "idle"), note=str(value.get("note") or ""))
            return ops, 0.3 + extra, True
        if kind == "magic":
            users = self._who(value.get("who", "magic"), last)
            if users:
                outcome = session.interactions.cast_on(users[0], value["target"])
                if outcome.performed:
                    self.focus = value["target"]
                    return outcome.ops, 2.4 + extra, False
            return [], 0.0, True
        if kind == "weather":
            self.state.weather = value
            self.state.weather_since = self.state.active_minutes
            session.state.scene.weather = value
            session.state.record("weather", _weather_note(value))
            self.state.remember(f"weather_{value}")
            return [session.world.scene_op()], 1.0 + extra, True
        if kind == "camera":
            target = self._who(value.get("who"), last) if value.get("who") else []
            ops.append({"op": "camera", "shot": value["shot"], "target": target[0] if target else None, "hold_ms": int(float(value.get("seconds") or 5) * 1000), "return_to": "wide"})
            return ops, 0.2 + extra, True
        if kind == "fx":
            ops.append({"op": "world_fx", "name": value["name"], "target": value.get("target"), "character": (self._who(value.get("who"), last) or [None])[0]})
            return ops, 0.3 + extra, True
        if kind == "set":
            self._apply_effects(value)
            return [], extra, True
        return [], 0.0, True

    def _react(self, cid: str, reaction: str) -> Optional[dict[str, Any]]:
        profile = self.session.room.get(cid)
        if not profile:
            return None
        if reaction in profile.reactions:
            return self.session.actions.react(cid, reaction, force=True)
        if profile.capabilities and profile.capabilities.get(reaction):
            return self.session.action_op(cid, reaction)
        return None

    def _formation(self, kind: str) -> list[dict[str, Any]]:
        stage = self.session.stage
        chars = sorted(self.session.state.characters.values(), key=lambda c: c.home_x)
        ops: list[dict[str, Any]] = []
        if kind == "home":
            for c in chars:
                ops += stage.home(c.id)[2]
        elif kind == "gather" and len(chars) >= 2:
            ops += stage.move_to(chars[0].id, x=0.36)[2]
            ops += stage.move_to(chars[-1].id, x=0.64)[2]
        elif kind == "apart" and len(chars) >= 2:
            ops += stage.move_to(chars[0].id, zone="far_left")[2]
            ops += stage.move_to(chars[-1].id, zone="far_right")[2]
        return ops

    def _say(self, trigger: str, now: float) -> list[dict[str, Any]]:
        exchange = self.picker.pick(trigger, self.context(), self.state, self._rng("say"))
        if exchange is None:
            return []
        self.last_talk = self.state.active_minutes
        self.stats["exchanges"] += 1
        beats = [{"_line": (line.who, line.text, line.reaction, exchange.line_id(i))} for i, line in enumerate(exchange.lines)]
        if exchange.effects:
            beats.append({"_effects": exchange.effects})
        for beat in reversed(beats):
            self.queue.appendleft(beat)
        return []

    def _line(self, spec: tuple, extra: float) -> tuple[list[dict[str, Any]], float, bool]:
        who, text, reaction, line_id = spec
        if who not in self._characters():
            return [], 0.0, True
        clip = self.clips.get(line_id, text)
        op: dict[str, Any] = {"op": "speak", "character": who, "text": text, "id": line_id, "source": "adventure"}
        if clip:
            op.update(clip)
            seconds = clip["duration"]
        else:
            seconds = min(8.0, 0.9 + len(text) * 0.065)
            op["ms"] = int(seconds * 1000)
        ops = [op]
        if reaction:
            r = self._react(who, reaction)
            if r:
                ops.append(r)
        others = [c for c in self._characters() if c != who]
        for other in others:  # the listener looks at the speaker
            look = self.session.attention_op(other, f"CHARACTER:{who}", "adventure", min(6.0, seconds + 0.5))
            if look:
                ops.append(look)
        self.session.state.add_line(who, text)
        self.stats["lines"] += 1
        self.stats["lines_voiced" if clip else "lines_caption"] += 1
        return ops, seconds + LINE_GAP + extra, False

    def _apply_effects(self, effects: dict[str, Any]) -> None:
        s = self.state
        for flag in _as_list(effects.get("flags")):
            s.add_flag(flag)
        for flag in _as_list(effects.get("unset")):
            s.remove_flag(flag)
        for key, delta in (effects.get("mood") or {}).items():
            try:
                s.change_mood(str(key), float(delta))
            except (TypeError, ValueError):
                pass
        found = effects.get("discover")
        if found and s.discover(found):
            self.session.state.record("discovery", f"they discovered {found.replace('_', ' ')}")
        if effects.get("note"):
            self.session.state.record("event", str(effects["note"]))
        if effects.get("delay"):  # a setback: this place takes longer
            s.place_stay += float(effects["delay"])

    # ------------------------------------------------------------------
    # the tick
    # ------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        if not self.enabled or not self.adventure:
            return []
        now = self.clock() if now is None else now
        dt = max(0.0, min(5.0, now - self.last_tick))
        self.last_tick = now
        ops: list[dict[str, Any]] = []
        try:
            self._sync_external()
            if self.pause_reasons:
                if not self.paused and self._safe(now):
                    ops += self._enter_pause(now)
                self._maybe_save(now)
                return ops
            if self.paused:
                ops += self._resume(now)
            self.state.active_minutes += dt / 60.0
            self.state.decay_moods(dt / 60.0)
            if now < self.beat_until:
                return ops
            if self.queue:
                beat = self.queue.popleft()
                beat_ops, seconds, safe = self._run_beat(beat, now)
                self.beat_until = now + seconds
                self.beat_safe = safe
                return ops + beat_ops
            self.beat_safe = True
            if self.event is not None:
                ops += self._finish_event(now)
            elif self.state.phase == "traversing":
                self.state.phase = "place"
            if now >= self.next_decision_at:
                ops += self._decide(now)
            self._maybe_save(now)
        except Exception as exc:  # the stream must never stop for the adventure
            self.errors.append(f"{type(exc).__name__}: {exc}"[:200])
            self.stats["errors"] += 1
            logger.error(f"Adventure: tick failed ({exc}); skipping the current beat")
            self.queue.clear()
            self.event = None
            self.beat_until = now + 5
            self.next_decision_at = now + 30
        return ops

    def _safe(self, now: float) -> bool:
        return self.beat_safe or now >= self.beat_until

    def _sync_external(self) -> None:
        engine = self.session.show.engine
        if engine.playing:
            self.request_pause("game")
        else:
            self.release_pause("game")
        from ..vr_agent.state import runtime

        if runtime.paused:
            self.request_pause("stream")
        else:
            self.release_pause("stream")

    def request_pause(self, reason: str) -> None:
        self.pause_reasons.add(reason)

    def release_pause(self, reason: str) -> None:
        self.pause_reasons.discard(reason)

    def _enter_pause(self, now: float) -> list[dict[str, Any]]:
        self.paused = True
        self.paused_for = set(self.pause_reasons)
        self.stats["pauses"] += 1
        remaining = max(0.0, self.beat_until - now)
        self._resume_after = min(remaining, 4.0)
        if self.event is not None and not self.event.resumable:
            self.queue.clear()
        self._save(now)
        return [{"op": "adventure", "state": "paused", "reasons": sorted(self.paused_for), "hud": self.hud()}]

    def _resume(self, now: float) -> list[dict[str, Any]]:
        self.paused = False
        reasons = self.paused_for
        self.paused_for = set()
        self.beat_until = now + 2.0 + getattr(self, "_resume_after", 0.0)
        self.beat_safe = True
        self.stats["resumes"] += 1
        ops: list[dict[str, Any]] = [{"op": "adventure", "state": "running", "hud": self.hud()}]
        # attention returns to what they were doing, not to a blank stare
        target = f"OBJECT:{self.focus}" if self.focus and self.focus in self.session.state.objects else "NEUTRAL"
        for i, cid in enumerate(self._characters()):
            op = self.session.attention_op(cid, target, "adventure", 2.5, 0.4 + i * 0.3)
            if op:
                ops.append(op)
        if "game" in reasons:
            self.queue.appendleft({"say": {"trigger": "after_game"}, "chance": 0.8})
        elif "conversation" in reasons:
            self.queue.appendleft({"say": {"trigger": "after_chat"}, "chance": float(self.settings["resume_line_chance"])})
        return ops

    # ------------------------------------------------------------------
    # games
    # ------------------------------------------------------------------
    def on_game_finished(self, event: ev.Event):
        winner = event.get("winner")
        if winner == "viewers":
            self.state.last_game = "chat_won"
        elif winner in self.session.state.characters:
            self.state.last_game = f"{winner}_won"
        else:
            self.state.last_game = "draw"
        self.session.state.record("game", f"a game ended ({self.state.last_game.replace('_', ' ')})")
        return []

    # ------------------------------------------------------------------
    # what the characters and the page are told
    # ------------------------------------------------------------------
    def hud(self) -> dict[str, Any]:
        if not self.adventure:
            return {}
        n, total = self._place_number()
        return {
            "title": self.adventure.title,
            "destination": self.adventure.destination,
            "region": self.region().name,
            "place": self.place().name,
            "objective": self.objective().text,
            "progress": round(self.progress(), 4),
            "step": n,
            "steps": total,
            "paused": self.paused,
        }

    def hud_op(self) -> dict[str, Any]:
        return {"op": "adventure", "state": "paused" if self.paused else "running", "hud": self.hud()}

    def context_note(self) -> str:
        if not self.enabled or not self.adventure:
            return ""
        s, adv, region, place = self.state, self.adventure, self.region(), self.place()
        pct = int(round(self.progress() * 100))
        parts = [
            f"Your adventure (real and ongoing, the stream runs it): you and your partner are on the {adv.title}, "
            f"heading for {adv.destination}. Why: {adv.summary}",
            f"Region: {region.name}. {region.description}".strip(),
            f"You are at {place.name}. Current objective: {self.objective().text}. What you were doing: {s.activity}.",
            f"Journey progress: about {pct} percent; you have NOT reached {adv.destination} yet."
            if s.phase != "arrived"
            else f"You have just reached {adv.destination}.",
        ]
        if s.discovered:
            parts.append("Found so far: " + ", ".join(d.replace("_", " ") for d in s.discovered[-4:]) + ".")
        if self.paused and "conversation" in self.paused_for:
            parts.append("You paused the journey to talk with chat and will carry on right after.")
        if s.mood.get("mika.annoyance", 0) >= 2 or s.mood.get("luna.annoyance", 0) >= 2:
            parts.append("There has been some friendly bickering between you two lately.")
        return " ".join(parts)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "adventure": self.adventure.id if self.adventure else None,
            "hud": self.hud(),
            "phase": self.state.phase,
            "activity": self.state.activity,
            "weather": self.state.weather,
            "active_minutes": round(self.state.active_minutes, 2),
            "paused": self.paused,
            "pause_reasons": sorted(self.pause_reasons),
            "queue": len(self.queue),
            "event": self.event.id if self.event else None,
            "stats": dict(self.stats),
            "errors": list(self.errors),
            "saves": self.store.saves,
            "voice_clips": len(self.clips.clips),
            "library": self.library.describe(),
        }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _weather_note(weather: str) -> str:
    return {
        "clear": "the sky cleared up",
        "rain": "it started to rain",
        "drizzle": "a light drizzle began",
        "storm": "a storm rolled in with thunder",
        "fog": "a thick fog crept in",
        "wind": "a strong wind picked up",
        "fireflies": "fireflies came out all around",
        "snow": "it started to snow",
    }.get(weather, f"the weather turned {weather}")
