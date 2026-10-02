"""Fake livestream chat for DEV: type a comment, watch who answers.

    uv run python tests/harness/mock_chat_console.py

Every line you type goes through the exact path a real YouTube comment takes
(viewer message -> room router -> ConversationDirector -> the speaking
character), so what you see here is what the stream would do.

    Replies are SPOKEN on the Stage. This window only shows who spoke, whether
    the voice was made and played, and how long it took (--show-text to also
    print the words).

    mika how are you?            speak as @you
    @ana luna how are you?       speak as someone else
    /state                       room, coding session and current source
    /publish                     publish the program on screen (needs publishing on)
    /timing                      show or hide response times
    /jobs                        creations, who asked, and their status
    /src                         the current coding source, with its hash
    /quit                        leave (the server keeps running)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:12399"
DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
COLOR = {"mika": "\033[38;5;215m", "luna": "\033[38;5;111m"}


def post(base: str, path: str, body: dict) -> dict:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode())


def get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return json.loads(response.read().decode())


RED, GREEN, YELLOW = "\033[38;5;203m", "\033[38;5;114m", "\033[38;5;221m"


def show_turn(turn: dict, show_text: bool) -> None:
    who = str(turn.get("character") or "?")
    color = COLOR.get(who, "")
    if show_text:
        kind = turn.get("kind") or ""
        tag = f" {DIM}[{kind}]{RESET}" if kind and kind != "answer" else ""
        print(f"  {color}{BOLD}{who.upper()}{RESET}{tag}: {turn.get('text') or ''}")
    else:
        # The reply is spoken on the Stage; here we only say who spoke.
        print(f"  {color}{BOLD}{who.upper()}{RESET} {DIM}spoke on the Stage{RESET}")


def show_voice(result: dict) -> None:
    """Did the voice get made, and did the Stage actually play it?"""
    if not result.get("voices_real", True):
        print(f"  {YELLOW}voice: test tone (--fake-voices){RESET}")
    lines = result.get("voice") or []
    made = [v for v in lines if v.get("ok")]
    failed = [v for v in lines if not v.get("ok")]
    if made:
        by_voice: dict = {}
        for v in made:
            item = by_voice.setdefault(
                v.get("voice") or "?", [0, 0, v.get("engine_voice") or ""]
            )
            item[0] += 1
            item[1] += int(v.get("ms") or 0)
        parts = [
            f"{name} {count} line{'s' if count != 1 else ''} ({engine}, {ms // max(count, 1)} ms each)"
            for name, (count, ms, engine) in by_voice.items()
        ]
        print(f"  {GREEN}voice made:{RESET} " + "; ".join(parts))
    for v in failed[:3]:
        print(
            f"  {RED}voice FAILED for {v.get('voice')}:{RESET} {v.get('error') or 'no audio'}"
        )
    if result.get("spoke") and not lines:
        print(
            f"  {RED}no voice was made for this reply{RESET} (check /tmp/vr-full-dev.log)"
        )
    sound = result.get("sound") or {}
    state = sound.get("state") or ""
    if state == "blocked":
        print(
            f"  {RED}{BOLD}SOUND BLOCKED by the browser.{RESET} {RED}Click the Stage window once,"
            f" or restart with ./start-teaching.sh (it opens the Stage with sound allowed).{RESET}"
        )
    elif state == "failed":
        print(
            f"  {RED}the Stage could not play the audio:{RESET} {sound.get('detail')}"
        )
    elif state == "no stage page yet":
        print(f"  {YELLOW}no Stage page is open, so nothing can be heard{RESET}")
    elif state in ("playing", "unlocked") and made:
        print(f"  {GREEN}sound playing on the Stage{RESET}")


def show_timing(result: dict) -> None:
    seconds = result.get("seconds")
    if seconds is None:
        return
    parts = []
    if result.get("first_voice_s") is not None:
        parts.append(f"first word {result['first_voice_s']}s")
    parts.append(f"{seconds}s total")
    if result.get("decision_ms"):
        parts.append(f"decide {result['decision_ms']} ms")
    if result.get("generation_ms"):
        parts.append(f"write code {result['generation_ms'] / 1000:.1f}s")
    print(f"  {DIM}{'  '.join(parts)}{RESET}")


def show_published(outcome: dict) -> None:
    """The game link, and what YouTube WOULD get (YouTube stays off in DEV)."""
    if not outcome.get("ok"):
        reason = outcome.get("reason") or "unknown reason"
        print(f"  {YELLOW}not published:{RESET} {reason}")
        return
    what = "updated" if outcome.get("updated") else "published"
    tag = " (dry run, local folder)" if outcome.get("dry_run") else ""
    print(f"  {GREEN}{BOLD}{what}{tag}:{RESET} {outcome.get('url')}")
    preview = outcome.get("preview") or {}
    if preview.get("chat") and not outcome.get("updated"):
        print(f"  {DIM}live chat message:{RESET} {preview['chat']}")
    section = str(preview.get("description") or "").strip()
    if section:
        print(f"  {DIM}description section:{RESET}")
        for text in section.splitlines():
            print(f"    {text}")


def start_publish_watcher(base: str, user: str) -> None:
    """Print publish results that land after the reply (GitHub can be slow)."""
    import threading
    import time as _time

    def watch() -> None:
        while True:
            _time.sleep(4)
            try:
                events = get(base, "/harness/published").get("published") or []
            except Exception:
                continue
            for outcome in events:
                print()
                show_published(outcome)
                print(f"{DIM}{user}:{RESET} ", end="", flush=True)

    threading.Thread(target=watch, daemon=True).start()


def show_state(state: dict) -> None:
    room = state.get("room") or {}
    teaching = state.get("teaching") or {}
    coding = state.get("coding") or {}
    print(f"  {DIM}room{RESET}    cast={room.get('cast')} pages={room.get('clients')}")
    print(
        f"  {DIM}coding{RESET}  active={teaching.get('active')} "
        f"owner={teaching.get('teacher')!r} phase={teaching.get('phase')!r}"
    )
    if coding:
        print(
            f"  {DIM}source{RESET}  language={coding.get('language')} "
            f"bytes={len(coding.get('code') or '')}"
        )
    sound = state.get("sound") or {}
    print(
        f"  {DIM}sound{RESET}   {sound.get('state')} {sound.get('detail') or ''}".rstrip()
    )
    error = teaching.get("last_error")
    if error:
        print(f"  {DIM}error{RESET}   {error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--user", default="@you")
    parser.add_argument(
        "--show-text", action="store_true", help="also print what they said"
    )
    parser.add_argument(
        "--timing", action="store_true", help="show response times (or type /timing)"
    )
    args = parser.parse_args()

    try:
        health = get(args.base, "/healthz")
    except Exception as exc:
        print(f"Cannot reach DEV at {args.base}: {exc}")
        sys.exit(1)

    print(f"{BOLD}Fake livestream chat{RESET}  ->  {args.base}")
    print(
        f"cast: {', '.join(health.get('cast') or [])}   "
        "/state  /src  /jobs  /publish  /timing  /quit\n"
    )
    state = {"times": args.timing}
    start_publish_watcher(args.base, args.user)
    import queue
    import threading

    outbox: "queue.Queue[tuple[str, str]]" = queue.Queue()

    def sender() -> None:
        while True:
            user, text = outbox.get()
            try:
                deliver(args, state, user, text)
            finally:
                outbox.task_done()
                print(f"{DIM}{args.user}:{RESET} ", end="", flush=True)

    threading.Thread(target=sender, daemon=True).start()

    while True:
        try:
            line = input(f"{DIM}{args.user}:{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/quit", "/exit", "/q"):
            return
        if line == "/state":
            try:
                show_state(get(args.base, "/harness/state"))
            except Exception as exc:
                print(f"  (state unavailable: {exc})")
            continue
        if line == "/publish":
            try:
                outcome = post(args.base, "/harness/publish", {})
            except Exception as exc:
                print(f"  (publish failed: {exc})")
                continue
            show_published(outcome)
            continue
        if line == "/timing":
            state["times"] = not state["times"]
            print(f"  {DIM}timing {'on' if state['times'] else 'off'}{RESET}")
            continue
        if line == "/jobs":
            try:
                data = get(args.base, "/harness/jobs")
            except Exception as exc:
                print(f"  (jobs unavailable: {exc})")
                continue
            settings = data.get("settings") or {}
            print(
                f"  {DIM}publishing enabled={settings.get('enabled')} dry_run={settings.get('dry_run')}{RESET}"
            )
            for job in data.get("jobs") or []:
                print(
                    f"  {job['job_id']}  {job['status']:<14} {job['project_slug']:<30} {job.get('public_url') or ''}"
                )
            continue
        if line == "/src":
            try:
                coding = (get(args.base, "/harness/state") or {}).get("coding") or {}
                code = coding.get("code") or ""
                if not code:
                    print("  (no source yet)")
                else:
                    digest = hashlib.sha256(code.encode()).hexdigest()[:12]
                    print(f"  {DIM}{coding.get('language')} sha256:{digest}{RESET}")
                    for number, text in enumerate(code.splitlines()[:40], 1):
                        print(f"  {DIM}{number:>3}{RESET} {text}")
            except Exception as exc:
                print(f"  (source unavailable: {exc})")
            continue

        user = args.user
        text = line
        if line.startswith("@") and " " in line:
            user, text = line.split(" ", 1)
        # Like a real chat: you can keep typing while they build. Messages
        # are handled one at a time, in order, the way the stream does it.
        if outbox.unfinished_tasks:
            print(f"  {DIM}(queued: they'll get to it after the current one){RESET}")
        outbox.put((user, text))


def deliver(args, state: dict, user: str, text: str) -> None:
    try:
        result = post(args.base, "/harness/chat", {"user": user, "text": text})
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  (server error: {exc})")
        return
    print()
    print(f"  {DIM}re: {text[:60]}{RESET}")
    if not result.get("accepted"):
        print(f"  {DIM}(no reply: {result.get('reason') or 'ignored'}){RESET}")
        return
    if result.get("consumed_by") == "room":
        print(f"  {DIM}(handled by the room: game or stage request){RESET}")
        return
    for turn in result.get("spoke") or []:
        show_turn(turn, args.show_text)
    for skipped in result.get("skipped") or []:
        print(f"  {DIM}({skipped['character']} skipped: {skipped['why']}){RESET}")
    if not (result.get("spoke") or result.get("skipped")):
        print(f"  {DIM}(nobody spoke){RESET}")
    show_voice(result)
    if state["times"]:
        show_timing(result)
    for outcome in result.get("published") or []:
        show_published(outcome)


if __name__ == "__main__":
    main()
