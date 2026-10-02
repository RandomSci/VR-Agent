# Installing this DEV build

This is your whole DEV project with the Code in Public upgrade. It does not
include live2d-models, .venv or cache, so unpacking it over your DEV folder
keeps those. LIVE (~/Documents/Open-LLM-VTuber) is not touched.

```bash
cd ~/Downloads
cp -a VR-Agent-Mika-Dev VR-Agent-Mika-Dev.backup-before-upgrade
tar -xzf vr-agent-dev-upgrade.tar.gz
conda activate For_AI
cd VR-Agent-Mika-Dev
./start-teaching.sh
```

If the launcher says the coding Python is missing libraries, run the
install line it prints once (seaborn, scipy, sympy, pandas, pillow,
SciencePlots go into For_AI).

The browser check uses Playwright's Chromium. If it is not installed yet:

```bash
uv run playwright install chromium
```

## YouTube sign-in (once, only when you turn YouTube publishing on)

```bash
uv run python scripts/youtube_authorize.py
```

Sign in with the streaming channel. The token is saved to
`data/secrets/youtube_token.json`. Leave `YOUTUBE_REFRESH_TOKEN` and
`YOUTUBE_VIDEO_ID` empty in `.env`, both are handled automatically.

## Things to try in the chat window

```
lets code
teach me python lists
make it a dictionary instead
now make a flappy bird game where luna is the bird
make it faster
make a 3d solar system
show me a twinkling starfield in python
/jobs
```

Watch for the instant "on it" line, "Mika is writing the code", a new
program when you change topic, and "first word" in the timing line.

## Tests

```bash
uv run python -m pytest tests -q
```

154 tests. Browser tests skip themselves when Chromium is missing.

See CODE_IN_PUBLIC.md for how it all works, safety, and the publishing
setup (off by default).
