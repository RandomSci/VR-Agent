# VR Agent Room (multi-character show)

Architecture for turning the single autonomous streamer into a small autonomous virtual show with
several characters, attention, directors and games. The single-character livestream (`/?mode=live`)
stays exactly as it is and remains the ultimate fallback.

Guiding rule. Intelligence and world logic are separate. The LLM writes dialogue and personality.
Ordinary code owns game rules, scores, timers, attention, camera, animation, sound, cooldowns,
deterministic routing, idle behaviour and capability checks.

## Run it

1. Keep `conf.yaml` as it is. The room uses its LLM provider and TTS for Mika (`voice: inherit`).
   Luna speaks with the free Edge TTS voice `en-GB-SoniaNeural` (set in `room/characters/luna.yaml`).
2. Start the server as usual with `uv run run_server.py`.
3. In OBS point the Browser source (1920x1080, "Control audio via OBS" ticked) at
   `http://127.0.0.1:12393/vr-agent/room.html`
4. Chat works exactly as before. Viewers can now also type
   - a name to pick who answers (`Luna what do you think?`), `you both`, `which of you`, `Luna ask Mika ...`
   - `play trivia`, `what games do you have`, `next round`, `make it harder`, `science questions`,
     `let Luna answer first`, `stop the game`, and plain answers while a question is up
   - `zoom in on Luna`, `close up`, `zoom out`
5. `/?mode=live` still works unchanged. With no room page open the server behaves exactly as before,
   and the room page falls back to it by itself if the room is disabled or no model loads.

URL flags for the room page. `&subtitles=0` hides captions, `&card=0` the comment card, `&status=0`
the LIVE badge, `&idle=0` idle motion, `&sfx=0-100` sets effect volume, `&nofallback=1` disables the
fallback redirect (layout work).

Configuration lives in `room/room.yaml` (cast, relationship, director budget, camera, ambient life,
objects, SFX volume, `cost.speech_window_seconds`, game timeouts) and `room/characters/<id>.yaml`
(persona, voice, layout, look and mouth parameters, emotion and reaction maps, topics, trivia skill
and lines). Games live in `src/open_llm_vtuber/games/<id>/` with a `config.yaml`.

Developer monitoring (never on stream). `GET /vr-agent/room/status` shows usage counters
(`llm_requests`, `tts_requests`, `viewer_triggered_interactions`), the Director's last plan, traces
with chat to voice timings, the game state, camera and voices. `POST /vr-agent/room/attention`,
`/vr-agent/room/test-action` and `/vr-agent/room/event` drive the room by hand.
`uv run python -m tests.harness.room_server --port 12399` serves the room with a fake TTS and no LLM
for layout work (`POST /harness/chat {"user": "@me", "text": "play trivia"}`).

## 1. Current architecture (what exists and keeps working)

1. `YouTubePlaywrightChatSource` reads the public chat page with a MutationObserver. No API quota.
2. `YouTubeLiveChatService` filters, buffers and scores messages and wakes the reply loop instantly.
3. `WebSocketHandler.process_youtube_live_message` runs the intent parser, builds one prompt and starts
   `process_single_conversation`.
4. `process_single_conversation` streams `BasicMemoryAgent`, splits sentences, runs TTS in parallel
   through `TTSTaskManager`, sends ordered `audio` payloads and waits for `frontend-playback-complete`.
5. The prebuilt React bundle renders one Live2D model. `vr-agent.js` adds live mode, idle motions,
   allowlisted actions, the comment card and BRB.
6. `VRAgentRuntime` tracks state and latency. `obs_brb_watchdog.py` covers server restarts.

Why the room needs its own page. The bundle is the compiled Cubism sample renderer.
`LAppLive2DManager.changeScene` keeps exactly one model and lip sync calls `getModel(0)` directly.
Patching a second model into it would break on every upstream update. The room page renders with
`pixi-live2d-display` 0.4.0 on `pixi.js` 6.5.10 (MIT, vendored locally) and speaks the same WebSocket
protocol, so the chat, LLM and TTS pipeline is reused unchanged.

## 2. Target architecture

```
LIVE CHAT (LiveChatSource: YouTube today, TikTok or Twitch later)
   |
Chat Manager (existing buffer and scoring, plus a raw observe feed for games)
   |
RoomSession ---- EventBus (typed events, bounded log for observability)
   |
   +-- Conversation Director   who speaks, turn plans, budget, preemption
   +-- Game Engine             registry, active game, typed commands, rules, timers
   |      +-- Trivia Battle    (only game for now)
   +-- Attention Director      where each character looks
   +-- Action Director         semantic reactions mapped to real registry actions
   +-- Camera Director         shots, cooldowns, zoom requests
   +-- World Director          room objects (game area, rabbit, heart), positions
   +-- Speaking Coordinator    one voice at a time, per-character TTS
   |
RoomState (single source of truth, snapshot to the renderer)
   |
Renderer (room.html) Live2D models, attention, camera, game UI, SFX, ambient life
   |
OBS
```

These are Python modules and classes inside the existing server process, not services.

```
src/open_llm_vtuber/room/
    profiles.py     room.yaml and character files (done)
    events.py       EventBus and event names
    state.py        RoomState, CharacterState, AttentionTarget
    attention.py    Attention Director
    actions.py      Action Director
    camera.py       Camera Director
    world.py        World Director (objects)
    routing.py      deterministic addressing
    director.py     Conversation Director and turn plans
    speech.py       Speaking Coordinator and per-character runtimes
    session.py      glue, owns everything above, talks to the renderer
src/open_llm_vtuber/games/
    base.py         Game interface, commands, game events
    registry.py     Game Registry
    engine.py       Game Engine
    commands.py     chat text to typed commands
    trivia/game.py  Trivia Battle rules
    trivia/questions.json
    trivia/config.yaml
room/room.yaml, room/characters/*.yaml
frontend/vr-agent/room.html, room.js, room.css, vendor, sfx
```

`games` imports nothing from Live2D, YouTube or the room. It receives player ids and messages and
emits events. The room translates those events into looks, actions, camera, sound and speech.

## 3. Cast and models (tested headlessly)

| | Mika | Luna |
|---|---|---|
| Model | mao_pro (witch, wand, full body) | Hiyori (official Live2D sample, full body, standing) |
| Head | ParamAngleX Y Z, ParamBodyAngleX | ParamAngleX Y Z, ParamBodyAngleX |
| Eyes | ParamEyeBallX Y | ParamEyeBallX Y |
| Mouth | ParamA | ParamMouthOpenY |
| Expressions | 8 original | 7 authored from her real face parameters (neutral, smile, blush, surprised, pout, sad, suspicious) |
| Gestures | nod, cute pose, hat tip, magic heart, failed spell, rabbit | cheerful smile, playful pose, happy tilt, shy hands, shrug, cheer, bounce, side eye, head tilt |

Shizuku was dropped because she is a desk bust and does not stand next to a full-body character.
Rice has no mouth parameters, Ren does not load in this renderer, Haru and Natori are office and
butler looks. Hiyori matches Mika's scale and style. Her motion groups were rearranged so only the
calm motion loops as idle; gestures play only when chosen.

Neither model can wave, clap, dance, wink or jump. Those map to honest alternatives or stay unsupported.

## 4. Attention system

Every character has an explicit attention target.

```
AttentionTarget  VIEWER | CAMERA | NEUTRAL | GAME | CHARACTER:<id> | OBJECT:<id>

CharacterState
  id name position speaking expression action current_game_role
  attention         current AttentionTarget
  attention_source  conversation, game, action, world, ambient
  attention_until   when a directed target expires back to ambient
```

Attention Director (backend, deterministic, no LLM) listens to events and sets directed targets
with a priority and a hold time. When a directed target expires the character returns to ambient.

| Event | Speaker or subject | Others |
|---|---|---|
| Character speaks to viewer | VIEWER | CHARACTER speaker |
| Character asks another | CHARACTER addressee | addressee looks back at asker |
| Viewer addresses everyone | VIEWER | VIEWER |
| Question shown in a game | GAME | GAME |
| Character answers | VIEWER or GAME | CHARACTER answerer |
| Viewer answers | VIEWER | VIEWER |
| Winner decided | winner VIEWER | CHARACTER winner |
| Object appears (rabbit) | OBJECT rabbit | OBJECT rabbit, after a short delay |

Ambient attention is local to the renderer and costs nothing. Rare glances at another character,
a delayed glance back, a look around the room, long quiet periods looking at the viewer.

Renderer mapping. Each target becomes a point on the stage (viewer and camera are straight ahead,
game is the game panel, characters are face anchors, objects come from the World Director). The
point becomes a head and eye direction for that model. The renderer checks which parameters the
loaded model really has. Head plus eyes when both exist, eyes only when only eyes exist, nothing
faked otherwise. Body angle is used at a third of head strength. The model's focus controller eases
toward the target, so turns are smooth.

## 5. Action Director

Turns semantic reactions into real registry actions per character.

```
celebrate  cheer, lose_react  pout or sad, surprised, suspicious, agree, shy, thinking
Mika   celebrate -> magic_heart or nod     lose_react -> pout
Luna   celebrate -> cheer or smile         lose_react -> sad, suspicious -> side_eye
```

Mappings live in each character yaml. Unknown or missing actions are skipped, never invented.
Per-character cooldowns stop reaction spam. Large motions keep the existing 90 second gap.

## 6. World Director

Owns named objects with stage positions. `game_area` exists while a game panel is visible.
Transient objects come from actions, for example `rabbit` near Mika's hand while summon_rabbit plays
and `heart` while magic_heart plays. Objects expire on their own.

## 7. Game Engine and Game Registry

```
class Game
    info            GameInfo(id, display_name, players, viewer_participation, enabled)
    start(players, options) -> events
    handle_command(cmd) -> events
    handle_viewer_message(username, text, now) -> events   answer matching only
    tick(now) -> events                                   timers
    next_step() -> Step                                   what the show should do next
    state() -> dict                                       serialisable truth

GameEngine
    available_games()  start_game(id)  stop_game()  switch_game(id)
    handle_command(cmd)  handle_viewer_message(...)  handle_character_turn(...)
    current_state()
```

Game Registry is built from `games/*/config.yaml`. Counting and listing games is answered from it
directly ("We currently have 1 game, Trivia Battle"). Unknown games ("play chess") get an honest
template reply. The registry is also handed to the characters' prompts when games come up.

Typed commands, parsed deterministically from chat and validated against the registry and the game.

```
StartGame(game_id)  StopGame()  NextRound()  ChangeGame(game_id or None)  ListGames()
SetDifficulty(easy|medium|hard)  SetCategory(<category the game really has>)  SetFirstPlayer(<character id>)
```

Viewer text never touches state directly. Chat intent, validated command, engine, state transition.

## 8. Trivia Battle

Deterministic state machine.

```
INTRO -> QUESTION_SHOWN (viewer window, timer) -> TURN first character -> TURN second character
      -> REVEAL -> SCORE -> next round ... -> GAME_FINISHED
```

- Question data has id, question, correct_answer, accepted_answers, wrong_answers, category, difficulty.
- The engine decides whether a character is right with a per-character skill table (by difficulty)
  and a seeded random source. A wrong answer is one of the question's wrong_answers. No LLM decides
  answers, scores, winners or timers.
- Characters speak answers from local templates ("Is it Mars?") in their own voice. Reaction lines
  come from per-character template pools. The LLM is used at most once per game for closing banter,
  and only while viewers are active.
- Viewer answers are matched deterministically (normalised text, accepted answers, small typo
  tolerance for longer answers, number words). First correct viewer wins the round for VIEWERS and
  the card shows "@name got it first".
- The first answerer alternates each round. "let Luna answer first" overrides it for the next round.
- Score board MIKA, LUNA, VIEWERS. Rounds per game default 5.
- Boredom is tracked locally (round count, duration, repetition, viewer activity). After 10 rounds
  or 15 minutes `switch_suggestion_available` turns on and the next natural break uses it. With one
  game installed the honest line is keep going or take a break.

## 9. Game Board (visual games inside the room)

Games are real visual games drawn in the stream page, not dialogue roleplay. The room is the
application. Characters inhabit it, games and objects exist in it, attention focuses on them and the
camera frames them.

- The Game Board is a world object (`OBJECT:game_board`) placed between the characters, below face
  height. It lives in world coordinates, so camera zoom and pan move it with the characters, and the
  Attention Director can target it.
- The board frame is game agnostic. Title, round, status line, timer bar, scoreboard, viewer
  participation feed, winner highlight, correct and wrong flashes.
- The centre area is drawn by a per-game renderer registered in the page (`trivia` now, a `grid`
  renderer for Tic-Tac-Toe later) without changing the board.
- A backend game only emits a view model (`renderer`, round, scores, status, timer, body data). It
  never knows about Live2D, YouTube or the DOM.

Placement. A panel in the centre between the characters, below face height, so faces stay visible.

```
        MIKA                 LUNA
             TRIVIA BATTLE
       What planet is known
         as the Red Planet?
          [timer bar]
      MIKA 2   LUNA 3   VIEWERS 1
```

Question card slide in, timer bar, answer bubble under the answering character, correct and wrong
flashes, score pop, winner highlight. CSS transitions only. Large type sized for phones.

## 10. SoundEffectManager

Local short WAV files generated for the project (no licensing issues), played in the renderer.
Events GAME_START, QUESTION_APPEAR, ANSWER_CORRECT, ANSWER_WRONG, SCORE, ROUND_WIN, GAME_WIN, GAME_STOP.
Master volume in room.yaml plus `&sfx=0-100` in the URL. SFX duck under speech, at most one effect at
a time, lower priority effects are dropped instead of stacked, minimum gap between effects.

## 11. Camera and game integration

| Event | Shot |
|---|---|
| Game start | two shot with game panel |
| Question shown | game framing, characters visible |
| Character turn | subtle focus on that character |
| Winner | brief close up on winner, then back to two shot |
| Viewer "zoom in" | close up on the addressed character, hold, return |

Automatic moves have a cooldown. Viewer zoom requests have their own cooldown. Transitions ease.

## 12. Conversation Director with games

Deterministic routing by names, group words, capability and fairness. Plans have a hard turn budget
(default 3, max 4). Optional turns drop when a viewer is waiting. During a game, a normal viewer
question gets one short answer at the next game checkpoint while game timers pause, then the game
resumes with its state intact. Paid messages and direct questions can interrupt at the next
checkpoint. Game answers and game commands are consumed by the engine and never reach the LLM.

## 13. Cost control

- No chat means no LLM and no TTS. Ambient life, SFX, camera and UI are local.
- Games never start by themselves.
- Game speech (TTS) is only produced while a viewer has chatted recently (default 120 seconds).
  Without viewers the game keeps running silently only until the current round ends, then pauses.
  After a longer quiet period (default 180 seconds) it ends locally with UI and SFX only.
- Trivia answers, results and scores never call the LLM. Templates cover most lines. At most one LLM
  line per game.
- One viewer message to one character is one LLM call, the other character reacts silently.
- Group questions cost two calls at most by default.
- Each LLM call carries persona, relationship, the last 8 room lines and the turn instruction only.

## 14. Failure and recovery

| Failure | Behaviour |
|---|---|
| A character's model fails to load | Renderer reports it, the character becomes unavailable, the room continues |
| No model loads or the room config is invalid | Room page redirects to `/?mode=live` |
| A character's LLM or TTS fails | Turn skipped or rerouted once, 3 failures in a row cool the character down for 5 minutes |
| Game plugin raises | Game stopped, UI cleared, event logged, conversation continues |
| Question bank unreadable | Game marked disabled in the registry and never offered |
| Renderer reconnects mid game | Receives a full state snapshot and redraws the panel and score |
| Camera error | Reset to wide shot |
| Director error | Classic single-character reply by the primary character |
| Server down | Existing OBS watchdog shows BRB |

## 15. Status

All seven layers are implemented on `vr-agent/multi-character-room`, each committed separately.

| Check | Proof |
|---|---|
| Two characters render independently | headless browser test loads both models, per-model look and mouth parameters |
| Attention works, characters look at each other | browser test drives CHARACTER, GAME and OBJECT targets and reads head direction |
| Trivia runs deterministically, turns alternate | `tests/test_games_trivia.py` with seeded randomness and a fake clock |
| Viewers participate, score and state are correct | engine tests plus a browser test answering from chat |
| Sound effects play | browser test records the effect files the page plays |
| No chat means no LLM and no TTS | `tests/test_zero_activity.py`, 30 simulated idle minutes give 0 and 0 |
| Games end by themselves | a viewer-started game pauses, then stops as inactive, speech stops after the window |
| Long unattended runs stay bounded | `tests/test_room_soak.py`, six simulated hours with viewer bursts |
| Classic mode still works | `vr-agent.js`, `index.html` and the bundle are unchanged, all original tests pass |

Known limits.

- Neither model can wave, clap, dance, wink or jump; those map to honest alternatives.
- Luna's voice needs Edge TTS (free, online). If it fails, her lines show as captions and she cools
  down after three failures while Mika keeps going.
- "First token" timing is measured at the first audio chunk (LLM first sentence plus its TTS).
- Hiyori's expressions were authored for this project from her face parameters; they are subtler
  than mao_pro's original expression files.
