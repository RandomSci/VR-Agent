# VR Agent livestream mode (Phase 7)

## Run it

1. Install the chat reader browser once with `uv sync` then `uv run playwright install chromium`
2. In `conf.yaml` under `live_config.youtube_live` set `youtube_live_enabled: true` and one of
   `video_id`, `channel_id` (UC...) or `channel_handle` (@name). `chat_source: 'playwright'` is the default.
3. Start the server with `uv run run_server.py`
4. In OBS add a Browser Source, URL `http://127.0.0.1:12393/?mode=live`, size 1920x1080,
   tick "Control audio via OBS" so the voice is captured.
5. Keep `http://127.0.0.1:12393/` for development. Opening it never steals the stream audio because
   the backend always prefers the page that announced `mode=live`.

Livestream URL flags. `&subtitles=1` shows captions of what the character says, `&card=0` hides
the viewer comment card, `&status=0` hides the LIVE badge, `&idle=0` turns idle motions off.

## What livestream mode shows

- Live2D character full screen over the room background the app is using
- Small `● LIVE | VR AGENT` badge and a state line (Listening to chat, Thinking, Responding)
- The one viewer comment being answered, bottom left, away from the face
- Nothing else. No settings, chat panel, camera, mic, hand button, toasts or debug info

The microphone cannot turn on in livestream mode (start-mic is dropped and audio capture is
refused), so the stream audio can never interrupt the character.

## Character actions

`live2d-models/mao_pro/vr_agent_actions.json` names the model's real motions and expressions.
Every entry is checked against `mao_pro.model3.json` at startup and dropped if the file or
expression does not exist. Names came from rendering each motion headlessly.

| Action | Live2D source | Viewer phrases |
|---|---|---|
| smile | expression exp_02 | smile, look happy, laugh, ngiti |
| sparkle | expression exp_04 | look excited |
| blush | expression exp_06 | blush, act shy |
| surprised | expression exp_07 | look surprised |
| sad | expression exp_05 | look sad |
| pout | expression exp_08 | look angry, pout |
| close_eyes | expression exp_03 | close your eyes |
| nod | motion mtn_02 (cheerful nod, arms open) | nod, tango |
| shy_sway | motion mtn_03 (hands behind back, head tilt) | do a pose, act cute |
| hat_tip | motion mtn_04 (tips her hat) | tip your hat, say hi |
| magic_heart | motion special_01 (draws a glowing heart) | make a heart, do magic |
| magic_fail | motion special_02 (heart spell fizzles) | fail a spell |
| summon_rabbit | motion special_03 (summons a rabbit) | rabbit, bunny |

Not supported by this model are clap, wave, dance, wink, bow, thumbs up, peace sign, jump, spin.
For some of these an honest alternative plays (wave and bow tip the hat, clap nods, dance does
the cute pose) and the character is told to say it cannot do the original.

The flow is viewer message, deterministic intent parser (no extra LLM call), registry lookup, one
prompt that includes the capability list and the outcome, normal LLM, TTS, then the frontend plays
the action at the moment the reply audio starts.

To add a model, copy the JSON next to its `.model3.json`, point each action at a real motion file
or expression name. Without the file the model still gets idle motions but advertises no actions.

## Idle behaviour

Every 10 to 15 seconds (config `vr_agent.idle_min_seconds` and `idle_max_seconds`) the frontend
plays a weighted random idle-safe motion, never the same one twice in a row, never while the
character is thinking, speaking or doing a requested action. Big spells (magic heart, rabbit) wait
at least 90 seconds between each other and never play in the first 90 seconds.

## Monitoring (developer only, never on stream)

- `GET /vr-agent/status` shows the state machine, recent transitions, chat to voice latency p50 and p90,
  capabilities, chat reader health (attached, mode, restarts, last error)
- `POST /vr-agent/test-action` plays an action, body `{"action": "nod"}`
- `POST /youtube-live/mock-message` injects a fake viewer message, body `{"author": "@me", "message": "can you smile?"}`

## Chat reader recovery

The Playwright reader restarts on page crash, closed tab, stuck heartbeat, chat list replacement
(re-attaches in place), chat ended (goes back to waiting for a stream), or a new stream on the
channel. Backoff 5s doubling to 120s, full browser restart after three failures in a row, and the
page is recycled every 6 hours to keep memory flat.

## Notes

- `frontend/index.html` loads `frontend/vr-agent/`. If an upstream Open-LLM-VTuber upgrade
  replaces `frontend/`, re-add the two tags in `index.html`.
- `model_dict.json` emotion map for mao_pro was corrected. Anger and disgust now use the pout
  (exp_08), sadness the sad face (exp_05), fear and surprise the surprised face (exp_07). The old
  map pointed anger at closed eyes and sadness at a smile.

## Speed and cost

- Chat is read the moment it appears (no polling) and the reply loop wakes instantly.
- Replies are asked for in one or two short sentences with no `<think>` tags, and the first
  audio starts after the first phrase (`faster_first_response: True` in the agent settings).
- Each livestream reply sends only the last `vr_agent.max_history_messages` (default 10)
  messages. Memory and the history file store just `@viewer: message`, not the per-turn
  instructions. Before this, history grew forever, so every reply got slower and more expensive
  the longer a stream ran.
- `response_cooldown_seconds` default is now 1 (was 5).
- Optional `max_tokens` under `openai_llm` caps reply length (90 is a good livestream value).
- The chat to voice time for every reply is logged (`VR Agent latency`) and summarised at
  `/vr-agent/status`.

## Be right back screen

Two layers, use either or both.

**Pause from the server (server keeps running).** Run
`curl -X POST http://127.0.0.1:12393/vr-agent/pause -d '{"paused": true}'` and the livestream page
fades to `frontend/vr-agent/brb.jpg`, the comment card and badge hide, and she stops replying.
`{"paused": false}` brings her back. A POST with no body toggles.

**Automatic while the server is down (restarts, code changes).** The page can't show anything when
the server is off, so OBS switches scenes instead.
1. OBS, Tools, WebSocket Server Settings, enable it and note the password.
2. Add a scene named `BRB` with an Image source pointing at `frontend/vr-agent/brb.jpg`.
3. In a second terminal run
   `uv run python scripts/obs_brb_watchdog.py --live-scene "Scene" --brb-scene "BRB" --browser-source "Browser" --password YOUR_PASSWORD`
   using your real live scene and Browser source names.

The watchdog checks `/vr-agent/health` every 2 seconds. Server down for two checks, or paused, means
BRB. When the server is back it refreshes the Browser source and switches to the live scene as soon as
the livestream page reconnects. It only switches between those two scenes, so other scenes you pick
yourself are left alone.
