# YouTube Live VTuber Mode

This mode lets Open-LLM-VTuber read YouTube Live chat, buffer and filter messages, select one worth answering, and route it through the existing character LLM, TTS, Live2D expression, lip sync, and frontend playback pipeline.

## Required Environment Variables

Place secrets in `.env` at the repository root. `.env` is already ignored by Git.

```env
OPENAI_API_KEY=...
YOUTUBE_API_KEY=...
YOUTUBE_CHANNEL_ID=UCDhDUZCr4Hqsf9jUlKIQ4PQ
```

`ELEVENLABS_API_KEY` is only required if `tts_model` is set to `elevenlabs_tts`.

## Enable YouTube Mode

In `conf.yaml`:

```yaml
live_config:
  youtube_live:
    youtube_live_enabled: true
    api_key: '${YOUTUBE_API_KEY}'
    channel_id: '${YOUTUBE_CHANNEL_ID}'
    video_id: null
```

Set `youtube_live_enabled: false` to return to normal microphone/manual mode only.

## Start Open-LLM-VTuber

```bash
cd /home/selwyn/Documents/Open-LLM-VTuber
uv run run_server.py
```

Open the local UI:

```text
http://localhost:12393
```

The YouTube responder needs one frontend client connected because it reuses the existing WebSocket audio and Live2D pipeline.

## OBS / YouTube Livestream

1. Create or start a YouTube livestream for the configured channel.
2. In OBS, capture the Open-LLM-VTuber browser window or local page.
3. Stream from OBS to YouTube as usual.
4. The backend will discover the active live video and acquire `activeLiveChatId` automatically.

## Active Chat Discovery

The integration uses API-key-only read access:

1. If `video_id` is set, `videos.list(part=liveStreamingDetails)` is used directly. This is useful for unlisted test streams.
2. Otherwise, `search.list(channelId=..., eventType=live, type=video)` finds public active livestreams for the configured channel.
3. `videos.list(part=liveStreamingDetails)` retrieves `activeLiveChatId`.
4. If no live stream is active, the service logs a waiting state and retries periodically.

Private streams generally require OAuth and are not supported by this read-only API-key mode.

## Message Processing

The service:

- reads live chat with `liveChatMessages.streamList` when available
- falls back to `liveChatMessages.list` and respects `pollingIntervalMillis`
- deduplicates message IDs
- filters empty, stale, repeated, unsafe, and spammy messages
- asks the configured LLM to select one eligible message
- injects the selected message into the existing character conversation pipeline
- waits for playback completion before selecting another message

## Mock Testing

You can test without a public audience by opening the UI, then posting a mock message locally:

```bash
curl -X POST http://localhost:12393/youtube-live/mock-message \
  -H 'Content-Type: application/json' \
  -d '{"author":"Test Viewer","message":"What is your favorite thing about being an AI VTuber?"}'
```

Check status:

```bash
curl http://localhost:12393/youtube-live/status
```

## Disable YouTube Mode

Set this in `conf.yaml`:

```yaml
live_config:
  youtube_live:
    youtube_live_enabled: false
```

Restart the server. Microphone/manual conversation mode remains available.
