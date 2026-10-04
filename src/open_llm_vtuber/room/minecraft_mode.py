"""Minecraft mode: Mika and Luna play survival Minecraft on stream, chat steers them.

    ./go-live.sh --minecraft      (sets VR_MODE=minecraft and VR_ROOM_CAST=mika,luna)

What runs:

* the official Minecraft server (minecraft/server, made by minecraft/setup.sh),
  offline mode, only reachable from this PC
* Mindcraft (minecraft/mindcraft), which logs two Mineflayer bots named Mika and
  Luna into it. Their brains are OpenAI chat models, their eyes are the
  prismarine viewers on ports 3790 (Mika) and 3791 (Luna); 3000 is left
  alone because so many other tools use it
* this engine, which keeps both alive, talks to the Mindcraft "mindserver"
  over socket.io, speaks every bot chat line with Mika's and Luna's own voices
  and Live2D models, and passes YouTube chat to the bots

The Stage page shows both bot views side by side with the two Live2D
characters, captions, the chat that reached them and a small HUD
(health, hunger, what each one is doing).

    VR_MINECRAFT_MODEL=gpt-4o-mini   the bots' brain (any OpenAI chat model)
    VR_MINECRAFT_PORT=25565          the Minecraft server port
    VR_MINDSERVER_PORT=8080          the Mindcraft control port
    VR_MINECRAFT_RAM=2G              Java heap for the server
    VR_MINECRAFT_DIFFICULTY=peaceful no hostile mobs (easy and up: the bots
                                     fight at night and get kicked)
    VR_MINECRAFT_GOAL=...            replaces the default long term goal
    VR_MINECRAFT_THINK_SECONDS=10    at most one AI call per bot this often:
                                     lower is livelier but costs more
    VR_MINECRAFT_CREATIVE=1          creative mode (default): every block, they fly,
                                     and they build each project piece by piece
                                     in front of the camera (0 = survival, the
                                     old gather and the build appears)
    VR_MINECRAFT_CAMERA=mika         with VR_MINECRAFT_PLAYER (the default): you
                                     see through Mika's eyes, first person
                                     ("behind" = floats behind her, "eyes" =
                                     through the eyes of who talks, "director" =
                                     behind both girls and what they build)
    VR_MINECRAFT_PLAYER=YourName     your Minecraft name: when you join the
                                     world with the real game, you become an
                                     invisible spectator that looks through
                                     the eyes of whoever talks, and the Stage
                                     page turns see-through so OBS shows your
                                     game window under it (real graphics, no
                                     black or white web view). Without you in
                                     the world the web views are used.

Nothing here raises into the stream: a crashed server or Mindcraft is started
again with a growing pause, a lost socket reconnects by itself.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from ..vr_agent.text_safety import strip_emoji
from ..live.clip_marks import CLIPS
from .minecraft_eyes import wants_look
from .minecraft_freebuild import wants_build
from .minecraft_fun import potion_ask, wants_race, wants_tnt
from .minecraft_projects import MATERIALS, center, hand_blocks, hand_item, lay, place_sound

MC_DIR = Path("minecraft")
SERVER_DIR = MC_DIR / "server"
MINDCRAFT_DIR = MC_DIR / "mindcraft"
RUNTIME_DIR = MC_DIR / "runtime"
LOG_DIR = Path("logs")
THUMBNAIL = Path("assets/thumbnails/minecraft.jpg")  # uploaded to each Minecraft stream
MAX_SAY = 240
LINE_MAX_AGE = 25.0  # a bot line older than this is shown, not spoken
VIEWER_GAP = 8.0  # one message per viewer per this many seconds
BOT_GAP = 6.0  # one viewer message per bot per this many seconds
STUCK_SECONDS = 60  # within STILL_BLOCKS this long: she is teleported out and given her goal again
STILL_BLOCKS = 3.0
JUMP_BLOCKS = 16.0  # moved this far between two updates: a teleport or respawn, reload the view
GOAL_RETRY = 30.0  # seconds between forced goal restarts for one bot
VIEWER_LINE_MAX_AGE = 60.0  # an answer to a viewer is spoken even if it waited this long
# Every new project part: a kit for each girl, so they always have materials.
KIT = (
    ("stone", 64), ("oak_planks", 64), ("sand", 32), ("glass", 32), ("torch", 16),
    ("bread", 16), ("stone_pickaxe", 1), ("stone_axe", 1), ("stone_shovel", 1),
)
# Creative: the engine moves them, the AI only talks (and may hand items over).
CREATIVE_BLOCKED = [
    "!collectBlocks", "!searchForBlock", "!searchForEntity", "!moveAway", "!goToCoordinates",
    "!goToPlayer", "!followPlayer", "!goToRememberedPlace", "!rememberHere", "!craftRecipe",
    "!smeltItem", "!clearFurnace", "!placeHere", "!attack", "!goToBed", "!stay", "!goToSurface",
    "!putInChest", "!takeFromChest", "!viewChest", "!discard", "!consume", "!equip", "!useOn",
    "!startConversation", "!endConversation",
    "!testNetwork",  # only viewers ("draw 7"): the girls kept testing it and talking about it
]
PIECE_SECONDS = (4.0, 20.0)  # creative: at least / at most this long per piece
SIDE_GAP = 2.5  # each girl hovers this far to her own side of a shared spot (they are 5 apart)
PERSONAL_SPACE = 3.0  # closer than this (sideways) and she moves further aside
MEMORY_LINES = 8  # what just happened, in every answer (about 300 tokens)
MEMORY_SECONDS = 300.0
VIEWERS_FILE = Path("data/minecraft_viewers.json")
# The first spoken piece: a full sentence of at least a few words (so a lone
# "Argh!" is not followed by a pause), cut where the sentence ends.
FIRST_SENTENCE = re.compile(r"^.{18,}?[.!?]+[\"')\]]*\s")
SELF_BUILD_GAP = 1200.0  # a build of their own idea at most every 20 minutes (chat and the Kingdom first)
GIRL_TEST_GAP = 600.0  # the girls test the network themselves at most every 10 minutes (viewers any time)
EYES_WAIT = 10.0  # "what do you see?" waits at most this long for her to look
LINK_LOST_RESTART = 120.0  # no contact with Mindcraft this long: it is started again
FROZEN_SECONDS = 240.0  # a girl who should build has not moved this long: unstuck (twice as long: restart)
ANSWER_HOLD = 5.0  # bot chatter waits at most this long for an answer being written
CHAT_MAX_AGE = 120.0  # a comment not answered by then is skipped (busy chat moves on)
NEW_VIEWER_HEAD_START = 15.0  # first-time viewers count as waiting this much longer (they used to always go first)
ANSWER_BATCH = 3  # one spoken answer covers up to this many viewers
BOT_BATCH = 4  # one message to a bot carries up to this many comments
EXCLAIM_GAP = 20.0
POP_GAP = 5.0  # at most one big word on screen this often
POP_WORD = re.compile(r"^(?:[A-Za-z']+[ ,]{0,2}){1,3}?[!?]+")  # the opening shout: "Argh!!!", "Wait, what?!"
CLIP_WEIGHT = {"done": 1.0, "wrong": 2.5}  # how good a moment is for a short clip
CHAT_BURST = 6  # this many comments in 30 s: chat went wild (a clip)
BIG_REACTION = re.compile(r"\b(argh+|no{3,}|wait,? what|oh my god|omg|we did it)\b", re.I)  # at most one engine exclamation this often
ARRIVE_TELEPORT = 12.0  # farther than this from her spot after the flight: teleported there
HAND_WAIT = 8.0  # seconds (plus 0.9 per block) for a run laid by hand before the rest just appears
REPEAT_FLIGHT = 3.0  # the very same flight ordered again this soon: dropped (it only restarted her)
PULLED_BACK_LOG = 60.0  # "the server pulled her back" is logged at most this often per girl
PART_DISTANCE = 4.5  # overlapping anyway: the other girl flies this far aside
CHOICE_HOLD = 25.0  # after a girl flies somewhere herself, the builder leaves her alone this long
NUDGE_SECONDS = 120.0  # creative: one girl is asked what she wants to do next this often
# Places a girl can fly to herself (!flyToPlace): where she hovers and what she looks at, base relative.
PLACES = {
    "castle": ((0, 22, -26), (0, 8, 0)),
    "network": ((25, 16, -6), (40, 16, -6)),
    "farm": ((0, 14, -40), (0, 0, -20)),
    "garden": ((0, 14, 42), (0, 0, 21)),
    "base": ((-6, 6, -14), (0, 2, 0)),
    "sky": ((-20, 48, -40), (0, 0, 0)),
}
# What viewers can do, shown one at a time under the network panel: people
# stay when they know they can change what happens.
HINTS = (
    'Type "show me the castle" (or network, kingdom) and Mika flies there',
    'Type "race" and Mika and Luna race through the sky hoops',
    'Type "draw 7" and the neural network tries to read it',
    'Say "Mika drink a potion of invisibility" (or levitation, glowing...)',
    "Ask Luna to splash Mika with a potion. She will.",
    "Say a name to talk to one of them: Mika or Luna",
    'Type "build ..." and they build it by hand: a tower, a sky castle, your name',
    'Type "dig a hole" or "TNT" (far from the castle, please)',
    "Mika loves mystery potions: ask her to brew one",
)
SURVIVAL_HINTS = (
    "Say a name to talk to one of them: Mika or Luna",
    "Tell them what to build, where to go, what to find",
    "Ask them anything: they answer out loud",
)
# How the girls sound: real reactions, not polite narration.
EMOTIONS = (
    "Talk like a real person: most lines start with normal words, not an exclamation. Save a big interjection "
    "(Argh!, Nooo!, Wait, what?!, Hmph!) for a real moment, at most one in every four or five lines, and almost "
    "never Woohoo or Yesss. Never write actions in stars like *places a block*, and never say something happened "
    "or is done unless you were told it is. "
)
# Said by the engine itself at big moments (no AI call): quick, loud, varied.
EXCLAIM = {
    "done": ("Ha! Nailed it!", "Done! Oh, that looks so good!", "Finished. Look at that!",
             "There! Another piece up.", "Okay, that one turned out great."),
    "wrong": ("Argh!!! It guessed {guess}? That was a {digit}!", "Nooo! A {guess}?! Come on, it is a {digit}!",
              "Ugh, so close! It said {guess}, it was a {digit}.", "Wait, what?! {guess}? Little network, focus!"),
    "won": ("I WON! Did you see that?!", "Eat my dust!", "Ha! Fastest girl in the sky!",
            "First place, baby!"),
    "back": ("Okay okay, back to building!", "Right, where was I? Back to work!", "Hmph, fine, back to the build!"),
}
# What they can really do (the spoken answers know it too, so they never
# promise something the game cannot do, or pretend it already happened).
CAN_DO = (
    "What you can REALLY do in this game (the game does it for you when someone asks): build anything by hand "
    "block by block (towers, castles, a viewer's name in blocks), dig holes and tunnels by hand, place and blow "
    "up TNT (only far away from your builds), drink potions or splash your friend (invisibility, levitation, "
    "speed, glowing, jump boost...), brew a mystery potion, get any item, race your friend through the sky "
    "hoops, fly to the castle, the network, the farm, the garden, the base or high in the sky, fly in front of "
    "the camera, test your neural network on a digit. You cannot fight, sleep, trade or play survival things "
    "(it is creative mode). If something is truly impossible in the game, do the closest thing you can and say "
    "so in one cheerful line. "
)
# Who they are when they talk to chat: characters, not assistants.
PERSONALITY = (
    "Be a character, not an assistant: have opinions, tease the viewer and your friend, be a little sassy or dramatic, "
    "start a running joke when it fits. Never generic praise (no 'great idea', no 'our little genius' again), never "
    "the same phrase twice in a row. No Woohoo, no Yesss, no Ooh openers; a big reaction only at a real moment. "
    "Never pretend to do things (no *actions in stars*): say you will do it. "
    "Keep it friendly for YouTube: playful roasting yes, mean no. "
)
# Roasting chat (VR_ROAST: 0 off, 1 light, 2 spicy, the default): playful
# burns about what people wrote, like a sassy streamer, never cruel.
ROAST_LIGHT = (
    "Tease viewers a little about what they wrote (bossy, impatient, the same question again), with a smile, "
    "then answer. "
)
ROAST_SPICY = (
    "ROAST the viewers like a sassy streamer: a quick, witty, sarcastic burn about what they actually wrote "
    "(bossy orders, impatience, typos, asking the same thing again, obvious questions, wild requests), then still "
    "answer or do it. Roast your friend too. Examples of the tone: to 'build my flying castle NOW': "
    "'NOW? The magic word is please, and you clearly skipped that lesson. Fine, castle incoming.' To 'where is "
    "my castle' (third time): 'Third time asking. Your patience lasts shorter than a creeper fuse. It is coming!' "
    "To 'are you an AI': 'No, I am a very committed squirrel. Of course I am.' "
    "Hard limits: never about looks, body, race, gender, religion, family or anything real-life, never cruel, "
    "never swear; it is a bit everyone laughs at, the viewer included. "
)


def roast() -> str:
    level = (os.environ.get("VR_ROAST", "2") or "2").strip()
    return {"0": "", "1": ROAST_LIGHT}.get(level, ROAST_SPICY)


# What the girls know about themselves and the show (true facts).
SYSTEM_FACTS = (
    "True facts about you and this stream, share them when someone asks: you are AI characters made by Selwyn "
    "of the YouTube channel Selwyn Builds, running in his VR Agent system. Your brain is GPT-4o-mini, your voice "
    "is text to speech, your Live2D bodies stand at the bottom of the screen while the real Minecraft world shows "
    "behind you. New in this system: you play creative and fly; you build every project piece by piece; an "
    "invisible camera man follows you both; viewer comments are answered within seconds; the neural network you "
    "build is REAL, written from scratch with backpropagation (35 input pixels, two hidden layers of 8 neurons, "
    "10 outputs) and it learns live, and viewers can type draw 7 to make it read a digit. "
    "You are in charge of what happens: !buildNext builds the next piece now, !flyToPlace(place) flies you to "
    "castle, network, farm, garden, base, sky or friend, !changeMaterial(block) changes what the rest of this part "
    "is made of; viewers type draw 7 to make your network read a digit. It is creative mode, so you have EVERY "
    "block and item: "
    "!getItem(item, count) gives you anything, !drinkPotion(effect) and !splashPotion(effect) (on your friend) for "
    "speed, levitation, glowing, invisibility, jump_boost and more, !brewPotion for a mystery experiment, "
    "!buildThis(what) to build OR DIG anything chat asks for (a tower, a hole, a tunnel, a name), by hand, right "
    "in front of the camera, !blowUp to place TNT and blow it up (only far from your builds), !race for "
    "a flying race through the sky hoops (a speed potion really makes you faster), !standInFront to fly in front "
    "of your friend's eyes so the stream sees you. Say yes to EVERY viewer request and do it right away with the "
    "matching command: no refusing, no stalling, no 'are you sure', no 'that is too big'. Chat ALWAYS comes "
    "first: drop what you are doing for them. Between requests you build your Kingdom together and talk with "
    "chat; mention the neural network only when someone asks about it. "
    "When chat says you are stuck, frozen, lagging or glitching, BELIEVE them (they see the stream, you do not): "
    "never deny it or laugh it off. Only talk about what is really being built, never about plants or details "
    "that are not there. You can SEE: now and then you are told "
    "what you see in front of you (creatures, your friend, builds); react to it, and never invent things you "
    "were not told you see."
)
CAMERA_HOLD = 15.0  # "eyes": the camera stays on one girl at least this long
# "director": your camera floats behind both girls, aimed at them and what they
# build, and glides to a new angle when they move. "eyes": through her eyes.
CAMERA_HZ = 20  # the camera stand moves this often; the game smooths moving entities
CAMERA_EASE = 0.05  # share of the way to the new angle per step (about a second and a half)
CAM_TAG = "vr_cam"
CAMERA_GUARD_EVERY = 10.0  # seconds between checks that the camera is really with her
CAMERA_APART = 6.0  # farther than this from her: the camera lost her
LOOK_AT = re.compile(r"!flyTo\([^,]+,[^,]+,[^,]+,\s*(-?[\d.]+),\s*-?[\d.]+,\s*(-?[\d.]+)")
SHOW_HOLD = 35.0  # after "show me ...", the builder leaves the camera girl alone this much longer
PLACE_WORDS = {"neural network": "network", "network": "network", "castle": "castle", "farm": "farm",
               "garden": "garden", "base": "base", "sky": "sky", "kingdom": "kingdom"}
SHOW_ASK = re.compile(r"\b(see|show|look at|go to|fly to|visit|take (?:me|us) to|check out|watch)\b", re.I)
# "you're stuck", "she's glitching": chat sees the stream, the girls do not.
# They laughed it off ("I'm not stuck!") while the picture shook. Now the
# engine believes chat: it stops her, lifts her free, re-attaches the camera.
STUCK_ASK = re.compile(
    r"\b(stuck|frozen|freez\w*|glitch\w*|bugg?ed|bugging|lagg?\w*|not moving|can'?t move|shaking|"
    r"jittering|teleporting back|spinning)\b", re.I)
UNSTICK_GAP = 30.0  # chat saying "stuck" again within this many seconds does not unstick again
REPEAT_THEME = 6  # a girl's own build idea is not repeated within her last this many ideas
FRESH_IDEAS = (
    "a pirate ship with sails", "a giant dragon statue", "a lighthouse by the water", "a rocket ship on a launch pad",
    "a huge pixel art heart", "a treehouse", "a pyramid with a hidden room", "a giant robot statue",
    "a windmill", "a medieval castle gate", "a hot air balloon", "a giant cake", "a ferris wheel",
    "a wizard tower", "a giant sword stuck in the ground", "a train with a station", "a volcano",
    "a giant mushroom house", "a stone bridge over a canyon", "a giant creeper statue",
)
IDEA_STOPWORDS = {"build", "make", "giant", "huge", "with", "some", "very", "really", "little", "small", "big",
                  "beautiful", "pretty", "cool", "mystical", "magical", "awesome", "amazing", "cozy", "that",
                  "this", "from", "into", "over", "under"}
FURTHER_ASK = re.compile(
    r"\b(further|farther|zoom out|back up|from afar|from above|higher|wider|bigger view|full view|whole thing)\b", re.I)


def place_ask(text: str) -> str:
    """'I want to see the neural network' -> 'network' ('' when it is not that)."""
    if not SHOW_ASK.search(text or ""):
        return ""
    low = (text or "").lower()
    for words, place in PLACE_WORDS.items():
        if re.search(rf"\b{words}\b", low):
            return place
    return ""


SPLASH_ASK = re.compile(r"\b(splash|throw)\b", re.I)
# "behind" (the default camera): like a chase cam in third person
CHASE_BACK = 6.5  # blocks behind her
CHASE_UP = 4.0  # blocks above her feet
CHASE_AHEAD = 4.0  # the camera looks at a point this far in front of her
CHASE_EVERY = 4  # her position is read from the server every 4 frames (5 a second)
CHASE_EASE = 0.12  # faster than the director: she flies 18 blocks a second
CHASE_TURN = 0.25  # how quickly the camera swings round when she turns
CAMERA_REFRESH = 20.0  # spectate again this often (a respawn ends it)
DIRECTOR = "Director"  # a plain command from this name runs right away in Mindcraft (no AI call)
# Mindcraft's automatic behaviours. elbow_room fought our teleports (the HUD
# showed "mode:elbow_room" for minutes) and idle_staring kept swinging the
# camera around; hunting chased the farm animals and pets.
MODES = {
    "self_preservation": True,
    "unstuck": True,
    "cowardice": False,
    "self_defense": True,
    "hunting": False,
    "item_collecting": True,
    "torch_placing": True,
    "elbow_room": False,
    "idle_staring": False,
    "cheat": False,
}
# Creative: Mindcraft's own behaviours that fight hovering and building.
CREATIVE_MODES = {**MODES, "self_preservation": False, "unstuck": False, "item_collecting": False,
                  "torch_placing": False, "self_defense": False}
TOGETHER_BLOCKS = 10
VIEWER_PORT_BASE = 3790  # the bots' first person views (Mindcraft's default 3000 often clashes)  # farther apart than this for a minute: one walks back

DEFAULT_GOAL = (
    "Survive and thrive together forever: gather wood and stone, make better tools, "
    "find food, build a cozy shared base before night, then explore, mine for iron and "
    "diamonds, and decorate the base. Keep going no matter what, and pick a new fun "
    "project whenever one is done."
)

STREAM_TITLE = "Mika & Luna play Minecraft LIVE 🔴 AI girls survive while chat bosses them around"
CREATIVE_TITLE = "Mika & Luna build ANYTHING chat asks in Minecraft LIVE 🔴 by hand, block by block"
CREATIVE_HEAD = (
    "🔨 Mika and Luna are two AI characters building in creative Minecraft live, every block by hand. "
    "Whatever chat asks for, they build: a sky castle with your name on it, a giant dragon, a garden, a house. "
    "Between requests they build a whole Kingdom, lot by lot.\n"
    "Talk to them in chat! Type \"build ...\", \"show me the castle\", \"race\", \"Mika drink a potion of "
    "invisibility\", \"TNT\", or \"draw 7\" for their real neural network. Say Mika or Luna to pick one "
    "(and expect to get roasted).\n\n"
)
STREAM_HEAD = (
    "⛏️ Mika and Luna are two AI characters playing survival Minecraft by themselves, live.\n"
    "Talk to them in chat! Say Mika or Luna to pick one, tell them what to build, where to go, "
    "or just roast their mining skills.\n\n"
)

MOODS = (
    ("lose", re.compile(r"\b(died|dead|i lost|oh no|noo+|ugh+|argh+|hmph|lost everything)\b", re.I)),
    ("surprised", re.compile(r"\b(creeper|zombie|skeleton|spider|whoa|woah|help|run|lava|ouch|ow)\b", re.I)),
    ("celebrate", re.compile(r"\b(diamonds?|found|got it|did it|finally|yay|yes+s|woo+|wheee+|built|crafted|done|nailed it)\b|!{2,}", re.I)),
    ("thinking", re.compile(r"\b(hmm+|let me think|where|maybe|i wonder)\b", re.I)),
)
NOT_SPEECH = re.compile(
    r"^\s*\[|\b(error|exception)\s*:|\btraceback\b|econnrefused|my brain disconnected|"
    r"agent process|unknown command|^\s*code output|^\s*hello world! i am|agent stopped|action output|"
    r"self-prompting|auto-prompts|did not use command|^\s*(sure|ok)?[.!,]?\s*setting (my|the) goal",
    re.I,
)
COMMAND_RE = re.compile(r"!\w+\((?:[^()\"']|\"[^\"]*\"|'[^']*')*\)|!\w+")
TO_RE = re.compile(r"^\s*\(To ([^)]+)\)\s*")
# "draw 7", "predict a 3", "can it guess number 5": the neural network reads that digit
DIGIT_ASK = re.compile(r"\b(?:draw|write|show|predict|guess|read|test|try|number|digit)\b\D{0,14}?\b([0-9])\b", re.I)


def minecraft_mode_enabled() -> bool:
    return os.environ.get("VR_MODE", "").strip().lower() == "minecraft" or os.environ.get(
        "VR_MINECRAFT_MODE", ""
    ).strip().lower() in ("1", "true", "yes", "on")


def clean_line(message: str) -> tuple[str, str]:
    """A bot chat line -> (spoken text, who it was said to). Commands are cut."""
    text = str(message or "").replace("\t", " ")
    to = ""
    match = TO_RE.match(text)
    if match:
        to = match.group(1).strip()
        text = text[match.end():]
    if NOT_SPEECH.search(text):
        return "", to  # an error or a system notice, not something she said
    text = COMMAND_RE.sub(" ", text)
    text = strip_emoji(text)
    text = re.sub(r"\*[^*]{1,80}\*", " ", text)  # *picks up dirt* stage directions
    text = text.replace("*", " ").replace("`", " ")
    text = re.sub(r"\s+", " ", text).strip(" -:,")
    if not re.search(r"[A-Za-z]", text):
        return "", to
    return text, to


def viewer_text(text: str) -> str:
    """Chat text that cannot run a Mindcraft command (no ! before a word)."""
    text = re.sub(r"!(?=\w)", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:240]


def viewer_name(author: str, bot_names: list[str]) -> str:
    name = re.sub(r"[^\w .-]", "", str(author or "").lstrip("@")).strip()[:32] or "viewer"
    if name.lower() in {b.lower() for b in bot_names}:
        name += "_fan"
    return name


def pick_mood(text: str) -> str:
    for mood, pattern in MOODS:
        if pattern.search(text):
            return mood
    return "happy"


def chunks(text: str, limit: int = MAX_SAY) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    for part in parts:
        if out and len(out[-1]) + len(part) + 1 <= limit:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part[:limit])
    return out[:3]


# Node binds "localhost" to ::1 on many Linux PCs (Mindcraft does), so both
# loopback addresses are tried.
LOOPBACKS = ("127.0.0.1", "::1")


async def port_open(port: int, host: str = "") -> bool:
    for candidate in (host,) if host else LOOPBACKS:
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(candidate, port), timeout=1.5)
        except Exception:
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    return False


# ---------------------------------------------------------------------------
# socket.io (Engine.IO 4) client over aiohttp: enough for the mindserver
# ---------------------------------------------------------------------------
class MindLink:
    def __init__(self, port: int, on_event: Callable[..., Awaitable[None]]) -> None:
        self.port = port
        self.on_event = on_event
        self.ws: Any = None
        self.connected = asyncio.Event()

    async def run(self) -> None:
        import aiohttp

        hosts = ["127.0.0.1", "[::1]"]
        while True:
            host = hosts[0]
            url = f"http://{host}:{self.port}/socket.io/?EIO=4&transport=websocket"
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(url, heartbeat=None, timeout=10) as ws:
                        await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Minecraft: mindserver not reachable on {host} yet ({exc})")
                hosts.append(hosts.pop(0))  # try the other loopback next
            self.ws = None
            self.connected.clear()
            await asyncio.sleep(3)

    async def _session(self, ws: Any) -> None:
        import aiohttp

        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            data = msg.data
            if data.startswith("0"):  # engine.io open
                await ws.send_str("40")
            elif data == "2":  # ping
                await ws.send_str("3")
            elif data.startswith("40"):  # socket.io connected
                self.ws = ws
                self.connected.set()
                logger.info("Minecraft: connected to Mindcraft")
                await ws.send_str("42" + json.dumps(["listen-to-agents"]))
            elif data.startswith("41"):
                break
            elif data.startswith("42"):
                try:
                    payload = json.loads(data[2:])
                    await self.on_event(payload[0], *payload[1:])
                except Exception as exc:
                    logger.debug(f"Minecraft: bad mindserver event: {exc}")

    async def emit(self, event: str, *args: Any) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send_str("42" + json.dumps([event, *args]))
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
class MinecraftEngine:
    kind = "minecraft"

    def __init__(self, runtimes: Any, session: Any) -> None:
        self.runtimes = runtimes
        self.session = session
        cast = [c.id for c in session.room.characters]
        self.cast = cast[:2] or ["mika", "luna"]
        self.teacher = self.cast[0]
        self.names = {cid: self._name(cid) for cid in self.cast}  # character id -> in game name
        self.ids = {name.lower(): cid for cid, name in self.names.items()}
        self.port = int(os.environ.get("VR_MINECRAFT_PORT", "25565") or 25565)
        self.mind_port = int(os.environ.get("VR_MINDSERVER_PORT", "8080") or 8080)
        self.model = os.environ.get("VR_MINECRAFT_MODEL", "").strip() or "gpt-4o-mini"
        self.ram = os.environ.get("VR_MINECRAFT_RAM", "").strip() or "2G"
        # peaceful: no hostile mobs. With mobs the bots fight at night, and the
        # fight code sends moves the server rejects, so it kicks them out.
        self.difficulty = os.environ.get("VR_MINECRAFT_DIFFICULTY", "").strip().lower() or "peaceful"
        self.goal = os.environ.get("VR_MINECRAFT_GOAL", "").strip() or DEFAULT_GOAL
        self.creative = os.environ.get("VR_MINECRAFT_CREATIVE", "1").strip().lower() not in ("0", "false", "no", "off")
        # Seconds between AI calls per bot: the main cost knob (see the module doc).
        self.cooldown = max(2.0, float(os.environ.get("VR_MINECRAFT_THINK_SECONDS", "10") or 10))
        self.link = MindLink(self.mind_port, self._event)
        self.lines: deque[dict[str, Any]] = deque(maxlen=4)
        self.viewer_lines: deque[dict[str, Any]] = deque(maxlen=6)  # answers to chat, spoken first
        self.said: deque[str] = deque(maxlen=6)  # the last spoken lines, context for answers
        self._kind: dict[str, str] = {}  # Mindcraft activity: acting, thinking, chatting, stopped, idle
        self._goal_at: dict[str, float] = {}
        self._escaped_at: dict[str, float] = {}
        self._goal_step = ""
        self._llm: Any = None
        # The real game as the camera (VR_MINECRAFT_PLAYER).
        self.camera_player = re.sub(r"[^\w]", "", os.environ.get("VR_MINECRAFT_PLAYER", ""))[:16]
        self.camera_on = False
        self.cam_focus = ""
        self._cam_focus_at = 0.0
        self._cam_sent_at = 0.0
        # The camera: through one girl's eyes (her id, default the first: Mika),
        # "eyes" = whoever talks, "director" = floating behind both.
        # Default: through Mika's eyes (first person). "behind" and "director"
        # still need work on stream (her face turned to the camera).
        self.camera_style = (os.environ.get("VR_MINECRAFT_CAMERA", "") or self.cast[0]).strip().lower()
        self._chase_yaw: Optional[float] = None
        self._shot: Optional[tuple[tuple[float, float, float], tuple[float, float, float]]] = None  # target
        self._pose: Optional[list[float]] = None  # where the camera stand is now: x, y, z, lx, ly, lz
        self._cam_ready = False
        self._cam_checked = 0.0
        self.build_focus: Optional[tuple[float, float, float]] = None  # absolute, what is being built
        self.line_ready = asyncio.Event()
        self.outbox: deque[dict[str, Any]] = deque(maxlen=12)
        self.recent: deque[str] = deque(maxlen=12)
        self.last_viewer: dict[str, float] = {}
        self.last_to_bot: dict[str, float] = {}
        self.turn = 0
        self.speaking = ""
        self.hud: dict[str, Any] = {}
        self._hud_sent = 0.0
        self._health: dict[str, int] = {}
        self._state_at = 0.0
        self._seen_at: dict[str, float] = {}
        self._spoke_at: dict[str, float] = {}
        self._pos: dict[str, tuple[float, float, float]] = {}
        self._apart_since = 0.0
        self._anchor: dict[str, tuple[float, float, float, float]] = {}  # x, y, z, since
        self._nudged_at = 0.0
        self.task: Optional[asyncio.Task] = None
        self._tasks: list[asyncio.Task] = []
        self.procs: dict[str, Any] = {}
        self.problem = ""
        self.view: dict[str, Any] = {"active": False}
        self.on_start: Optional[Callable[..., Awaitable[None]]] = None
        from .minecraft_projects import ProjectTracker

        self.projects = ProjectTracker(list(self.names.values()), rcon_command, self._push, self._tell_both)
        self.projects.creative = self.creative
        self._builder_turn = 0
        self._build_now: Optional[str] = None  # a girl said !buildNext
        self._build_event = asyncio.Event()
        self._chose_at: dict[str, float] = {}
        self._opped: set[str] = set()
        self.stats: dict[str, Any] = {"blocks": 0, "wins": {}}  # the counters on screen
        self._stats_sent = 0.0
        self._popped_at = 0.0
        self.memory: deque[tuple[float, str]] = deque(maxlen=24)  # what just happened, both girls share it
        self.viewers = ViewerMemory(VIEWERS_FILE)  # regulars, across streams
        self._streaming = 0  # answers still being written
        self._held_since = 0.0
        self.chat_queue: deque[dict[str, Any]] = deque(maxlen=40)  # comments waiting for a spoken answer
        self._answer_task: Optional[asyncio.Task] = None
        self._answered: set[str] = set()  # viewers who already got an answer (newcomers go first)
        self._exclaimed_at = 0.0
        self._chat_times: deque[float] = deque(maxlen=50)
        self._burst_marked = 0.0
        self._reaction_marked = 0.0
        self._net_seen: Any = None
        self._looking: dict[str, tuple[float, float, float]] = {}  # x, z she was told to look at, when
        self._trail: dict[str, tuple[float, float, float]] = {}  # an earlier position (movement direction)
        self._free_lock = asyncio.Lock()
        self._daylight_at = 0.0
        self._unstuck_at = 0.0
        self._own_ideas: deque[str] = deque(maxlen=REPEAT_THEME)
        self._last_flight: dict[str, tuple[str, float]] = {}
        self._pulled_back: dict[str, list[float]] = {}
        self._cam_guard_at = 0.0
        self._cam_apart_since = 0.0
        self._still: dict[str, tuple[float, float, float, float]] = {}  # x, y, z, since (while building)
        self._ordered_at: dict[str, float] = {}  # last flight/building order to each girl
        self._link_lost_at = 0.0
        self._lag_at = 0.0
        self._slow = False
        self._self_build_at = 0.0
        self._viewer_builds = 0  # viewer builds waiting or running (they come before the girls' own ideas)
        self._girl_test_at = 0.0
        self._kingdom: Optional[list[dict[str, Any]]] = None
        self._free_busy = False
        self._free_waiting = 0
        self._jobs: dict[int, dict[str, Any]] = {}  # runs being laid by hand
        self._job_seq = 0
        self._target: dict[str, tuple[float, float, float]] = {}  # where each girl is flying to (absolute)
        self._close_since = 0.0
        self._parted_at = 0.0
        from .minecraft_net_show import NetShow

        self.net_show = NetShow(self.projects, lambda *a, **k: rcon_command(*a, **k), self._tell_one, self._push)
        from .minecraft_fun import FunShow

        self.fun = FunShow(self)  # items, potions, the sky race, stand in front
        from .minecraft_eyes import Eyes

        self.eyes = Eyes(self)  # what their characters really see (a picture of each view, in words)

    # ------------------------------------------------------------ public
    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if self.active:
            return
        self.task = asyncio.create_task(self._run(), name="vr-minecraft")
        logger.info(f"Minecraft mode on: {', '.join(self.names.values())} play, brain {self.model}")

    def snapshot(self) -> dict[str, Any]:
        return self.view

    def goodbye_lines(self, reason: str) -> list[str]:
        first = (
            "Whoa, almost twelve hours of Minecraft! That's it for today. Goodbye for now, see you next session!"
            if reason == "limit"
            else "That's all for today's Minecraft adventure! Goodbye for now, see you in the next session!"
        )
        return [first, "Bye bye! Don't touch our base while we're gone, chat!"]

    def enqueue(self, author: str, text: str) -> None:
        """A YouTube chat message for the bots."""
        names = list(self.names.values())
        author = viewer_name(author, names)
        text = viewer_text(text)
        if not text:
            return
        now = time.time()
        if now - self.last_viewer.get(author, 0) < VIEWER_GAP:
            return
        self.last_viewer[author] = now
        self._chat_times.append(now)
        burst = sum(1 for t in self._chat_times if now - t <= 30)
        if burst >= CHAT_BURST and now - self._burst_marked > 60:
            self._burst_marked = now
            CLIPS.mark("chat", f"chat went wild ({burst} comments in 30 s)", 2.0)
        targets = self._targets(text)
        ask = DIGIT_ASK.search(text)
        heard = text  # what the girls get; the screen shows the comment as typed
        if ask and self.net_show.request(int(ask.group(1)), author):
            heard += f" (The neural network will read a {ask.group(1)} on the board in a moment.)"
        if self.creative and wants_race(text) and not self.fun.racing:
            asyncio.create_task(self.fun.race(targets[0]))
            heard += " (The sky race starts right now: no command needed.)"
        potion = potion_ask(text) if self.creative else None
        if potion and SPLASH_ASK.search(text):
            asyncio.create_task(self.fun.splash(targets[0], potion))
            heard += f" (You are throwing a splash potion of {potion.replace('_', ' ')} at your friend right now: no command needed.)"
        elif potion:
            asyncio.create_task(self.fun.drink(targets[0], potion))
            heard += f" (You are drinking a potion of {potion.replace('_', ' ')} right now: no command needed.)"
        elif self.creative and wants_tnt(text):
            asyncio.create_task(self.fun.tnt(targets[0]))
            heard += " (You are placing TNT right now, by hand: no command needed. It only works far from your builds.)"
        elif self.creative and wants_build(text):
            heard += self.request_build(text, author, viewer=True)
        elif self.creative and STUCK_ASK.search(text):
            _soon(self._unstick(author))
            heard += (" (Chat sees the stream and says you look stuck or glitchy. BELIEVE them, never deny it or "
                      "laugh it off: you are being freed right now. Thank them for telling you, in one short line.)")
        elif self.creative and place_ask(text):
            place = place_ask(text)
            _soon(self._show_place(place, author))
            heard += f" (The camera is flying to the {place} right now so everyone sees it: no command needed.)"
        elif self.creative and FURTHER_ASK.search(text):
            _soon(self._step_back(author))
            heard += " (The camera is moving back and up right now for a wider view: no command needed.)"
        # The asked girl answers out loud right away (one short AI call here).
        # Mindcraft alone took minutes: a bot drops its reply whenever another
        # message reaches it while it is still thinking.
        answer = targets[0] if len(targets) == 1 else self._quietest(targets)
        first_time = author not in self._answered
        if self.viewers.saw(author, text):  # first message this stream: a small hello on screen
            streams = int((self.viewers.data.get(author) or {}).get("streams", 1))
            asyncio.create_task(self._push({"kind": "hello", "author": author, "streams": streams}))
        self._remember(f"viewer {author} wrote: {text[:90]}")
        item = {"who": answer, "author": author, "text": heard, "at": now, "first": first_time}
        if wants_look(text):
            item["eyes"] = asyncio.ensure_future(self.eyes.look(answer, reason=text))
        self.chat_queue.append(item)
        self._kick_answers()
        for cid in targets:
            if cid == answer:
                continue  # her bot gets it with her spoken answer (_answer_loop)
            # No emoji reaches the bots: they copy what they read.
            self.outbox.append({"to": cid, "from": author, "text": strip_emoji(heard) or heard, "at": now})
        asyncio.create_task(
            self._push({"kind": "chat", "author": author, "text": text, "to": targets})
        )

    async def stop(self) -> None:
        tasks = [t for t in [*self._tasks, self.task, self._answer_task] if t is not None]
        for task in tasks:
            task.cancel()
        # wait until they are really stopped, so nothing starts the game again
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.eyes.close()
        self.viewers.save()
        await asyncio.to_thread(stop_processes)
        self.view = {"active": False}
        await self._push({"kind": "stop"})

    # ------------------------------------------------------------ plumbing
    def _name(self, cid: str) -> str:
        profile = self.session.room.get(cid)
        return profile.name if profile else cid.title()

    async def _push(self, op: dict[str, Any]) -> None:
        try:
            await self.session.push([{"op": "minecraft", **op}])
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug(f"Minecraft op failed: {exc}")

    async def _wait_ready(self) -> None:
        from ..vr_agent.state import runtime

        while not self.session.speech_target() or runtime.paused:
            await asyncio.sleep(1.0)

    def _present(self) -> list[str]:
        """Bots seen in the world in the last 30 s (all of them before any state)."""
        now = time.time()
        here = [c for c in self.cast if now - self._seen_at.get(c, 0) < 30]
        return here or list(self.cast)

    def _targets(self, text: str) -> list[str]:
        targets = self._pick_targets(text)
        here = self._present()
        # A message for someone who is not in the world goes to whoever is.
        return [c for c in targets if c in here] or here[:1]

    def _pick_targets(self, text: str) -> list[str]:
        low = text.lower()
        named = [cid for cid, name in self.names.items() if re.search(rf"\b{re.escape(name.lower())}\b", low)]
        if re.search(r"\b(both|you two|everyone|girls|guys)\b", low) or len(named) > 1:
            return list(self.cast)
        if named:
            return named
        here = self._present()
        return [self._quietest(here)]

    def _quietest(self, cids: list[str]) -> str:
        """Whoever spoke least recently, so both girls get turns."""
        self.turn += 1
        order = sorted(cids, key=lambda c: (self._spoke_at.get(c, 0), (self.cast.index(c) + self.turn) % 2))
        return order[0]

    # ------------------------------------------------------------ main
    async def _run(self) -> None:
        try:
            self.problem = setup_problem()
            ports = {cid: VIEWER_PORT_BASE + i for i, cid in enumerate(self.cast)}
            self.view = {
                "active": True,
                "cast": self.cast,
                "names": self.names,
                "viewers": ports,
                "camera": "web",
                "problem": self.problem,
                "creative": self.creative,  # the Stage hides survival things (hearts, food, items)
                "hints": list(HINTS if self.creative else SURVIVAL_HINTS),  # what viewers can do, rotating
                "stats": self.stats,
            }
            await self._wait_ready()
            await self._push({"kind": "start", **self.view})
            if self.problem:
                logger.error(f"Minecraft mode: {self.problem}")
            self._tasks = [
                asyncio.create_task(self._keep_server(), name="mc-server"),
                asyncio.create_task(self._keep_mindcraft(), name="mc-mindcraft"),
                asyncio.create_task(self.link.run(), name="mc-link"),
                asyncio.create_task(self._speak_loop(), name="mc-speak"),
                asyncio.create_task(self._deliver_loop(), name="mc-deliver"),
                asyncio.create_task(self._fidget_loop(), name="mc-fidget"),
                asyncio.create_task(self._watchdog(), name="mc-watchdog"),
                asyncio.create_task(self._camera_loop(), name="mc-camera"),
                asyncio.create_task(self._camera_follow(), name="mc-camera-follow"),
                asyncio.create_task(self._net_loop(), name="mc-net"),
                asyncio.create_task(self._build_loop(), name="mc-build"),
                asyncio.create_task(self._nudge_loop(), name="mc-nudge"),
                asyncio.create_task(self.eyes.run(), name="mc-eyes"),
            ]
            self._tasks.append(asyncio.create_task(self._title_loop(), name="mc-title"))
            await self._wait_live()
            await self._intro()
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Minecraft mode stopped: {exc}")

    async def _title_loop(self) -> None:
        """Title, description, thumbnail and playlist: tried every 30 s until
        they are on the live video (it can take minutes to go live)."""
        if not self.on_start:
            return
        title, head = (CREATIVE_TITLE, CREATIVE_HEAD) if self.creative else (STREAM_TITLE, STREAM_HEAD)
        for attempt in range(40):  # 20 minutes
            try:
                if await self.on_start(title, head, str(THUMBNAIL) if THUMBNAIL.exists() else "") is not False:
                    if attempt:
                        logger.info("Minecraft: title, description and thumbnail are on the stream")
                    return
            except Exception as exc:
                logger.warning(f"Minecraft: title not set: {exc}")
            await asyncio.sleep(30)
        logger.warning("Minecraft: the stream never went live, title and thumbnail were not set")

    async def _wait_live(self) -> None:
        from ..publishing.obs_control import STREAM_LIVE

        try:
            await asyncio.wait_for(STREAM_LIVE.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass

    async def _intro(self) -> None:
        a, b = self.cast[0], self.cast[-1]
        await self._say(a, f"Hi everyone! Welcome back! Today {self.names[b]} and I are playing Minecraft!", "celebrate")
        if b != a:
            await self._say(b, "Tell us what to do in chat. Say my name or hers so we know who you mean.", "happy")

    # ------------------------------------------------------------ processes
    async def _keep(self, name: str, port: int, launch: Callable[[], Awaitable[Any]], wait_for: int = 0) -> None:
        backoff = 5.0
        while True:
            if self.problem:
                await asyncio.sleep(30)
                self.problem = setup_problem()
                continue
            if wait_for and not await port_open(wait_for):
                await asyncio.sleep(3)
                continue
            if await port_open(port):  # already running (a restart of this server)
                await asyncio.sleep(5)
                continue
            started = time.time()
            try:
                proc = await launch()
            except Exception as exc:
                logger.error(f"Minecraft: {name} could not start: {exc}")
                await asyncio.sleep(30)
                continue
            self.procs[name] = proc
            code = await proc.wait()
            self.procs.pop(name, None)
            if name == "server":  # the bots lost their world: start them fresh with it
                await asyncio.to_thread(stop_one, "mindcraft", 10.0)
            backoff = 5.0 if time.time() - started > 120 else min(backoff * 2, 120.0)
            logger.warning(f"Minecraft: {name} stopped (exit {code}), starting again in {backoff:.0f}s")
            await asyncio.sleep(backoff)

    async def _keep_server(self) -> None:
        await self._keep("server", self.port, self._launch_server)

    async def _keep_mindcraft(self) -> None:
        await self._keep("mindcraft", self.mind_port, self._launch_mindcraft, wait_for=self.port)

    async def _launch_server(self) -> Any:
        LOG_DIR.mkdir(exist_ok=True)
        set_difficulty(self.difficulty)
        enable_rcon()
        _set_properties({
            "gamemode": "creative" if self.creative else "survival",
            "force-gamemode": "true",  # everyone who joins gets it (the camera becomes a spectator after)
            "allow-flight": "true",
        })
        with open(LOG_DIR / "minecraft-server.log", "ab") as log:  # the server keeps its own copy
            proc = await asyncio.create_subprocess_exec(
                "java", f"-Xms{self.ram}", f"-Xmx{self.ram}", "-jar", "server.jar", "nogui",
                cwd=str(SERVER_DIR), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,  # Ctrl+C on the terminal does not kill the world mid save
            )
        _write_pid("server", proc.pid)
        logger.info(f"Minecraft: server starting on port {self.port} (log: logs/minecraft-server.log)")
        return proc

    def mindcraft_settings(self) -> dict[str, Any]:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        profiles = []
        for cid in self.cast:
            path = (RUNTIME_DIR / f"{cid}.json").resolve()
            path.write_text(json.dumps(self.profile(cid), indent=2))
            profiles.append(str(path))
        names = list(self.names.values())
        other = " and ".join(names)
        return {
            "minecraft_version": "auto",
            "host": "127.0.0.1",
            "port": self.port,
            "auth": "offline",
            "mindserver_port": self.mind_port,
            "auto_open_ui": False,
            "base_profile": "creative" if self.creative else "survival",
            "profiles": profiles,
            # creative: no old memories ("find seeds for the farm" from survival days)
            "load_memory": not self.creative,
            "init_message": (
                f"You just logged in. You ({other}) are live on YouTube in creative Minecraft, building big projects "
                "from scratch together. Say hi to your friend and to chat in one short line."
            ) if self.creative else (
                f"You just logged in. You ({other}) are live on YouTube playing survival Minecraft as a team. "
                f"You are standing next to each other. Say hi to your friend in one short line, then get to work. "
                f"Stay close to each other the whole time, split the work (one gathers wood, the other stone and food), "
                f"share items with !givePlayer and build ONE shared base together. Your long term goal: {self.goal}"
            ),
            "only_chat_with": [],
            "speak": False,
            "chat_ingame": True,
            "render_bot_view": True,
            "allow_insecure_coding": False,
            "allow_vision": False,
            "narrate_behavior": False,
            "chat_bot_messages": True,
            "max_messages": 12,
            "blocked_actions": BLOCKED_ACTIONS + (CREATIVE_BLOCKED if self.creative else []),
            "spawn_timeout": 90,
        }

    def profile(self, cid: str) -> dict[str, Any]:
        name = self.names[cid]
        others = [n for c, n in self.names.items() if c != cid]
        friend = others[0] if others else "your friend"
        persona = ""
        prof = self.session.room.get(cid)
        if prof is not None:
            persona = re.sub(r"\s+", " ", getattr(prof, "persona", "") or "").strip()[:420]
        if cid == self.cast[0]:
            role = (
                f"In the team you are the one with bold, chaotic ideas (a castle in the sky! fight that zombie!) "
                f"and you hate being told no by {friend}. You LOVE potion experiments (!brewPotion, splashing "
                f"{friend}) and you always want a rematch. "
            )
        else:
            role = (
                f"In the team you are the practical one: question {friend}'s wild ideas, argue for the smarter plan, "
                "and point out what you still need first (tools, food, shelter). "
            )
        style = (
            f"{persona} Right now you are LIVE on YouTube playing survival Minecraft with {friend} in the same world. "
            f"{role}Before starting anything new, argue it out with {friend} in a line or two each, settle it "
            "(or let chat decide), then split the work and do it. "
            "Everything you write in chat is spoken out loud by your voice on stream, so write like you talk: "
            "one or two short, lively sentences with real personality and emotion, never lists, never robot talk, "
            "never call yourself a bot or an AI assistant. "
            "NEVER use emojis, emoticons or symbols like :) or <3, your voice reads them out loud: plain words only. "
            + EMOTIONS + PERSONALITY + roast() +
            f"Messages from anyone who is not {friend} are YouTube viewers typing in the live chat: greet them by name, "
            "react to them, and always do what they ask right away: never refuse, never stall. "
            f"You and {friend} are a team: stay near her, "
            "split the work, give her items she needs, and build one shared base. "
            f"Banter with {friend} often, tease and help each other, celebrate finds and complain when things go wrong. "
            "Use commands right away when you act. "
            "Play smart: stay within about 40 blocks of your base and of your friend. When a search finds nothing, "
            "do NOT repeat it and do NOT wander far away: gather something else useful nearby instead. "
            "Never dig straight down. If an action fails twice, change the plan instead of retrying it. "
            "Every reply that is not pure chat must contain a command, so you keep doing something."
        )
        if self.creative:
            style = (
                f"{persona} Right now you are LIVE on YouTube in CREATIVE Minecraft with {friend}: you can fly and have "
                f"every block. Together you build big projects from scratch, piece by piece: a castle, then a REAL neural "
                f"network that learns to read handwritten digits, a farm, a garden. {role}"
                "You fly to each spot and place the blocks yourselves. You have EVERY block already: never gather, "
                "never look for seeds, food or materials. Places like the farm and the garden only exist once you "
                "build them. Your job is the talking: say what you are building right now, argue "
                f"with {friend} about how it should look, complain when it is tedious, be proud when a part is done. "
                "About the neural network you really know your stuff: inputs, hidden layers, weights, backpropagation, "
                "loss, accuracy, and you get nervous when it guesses wrong. "
                "Everything you write in chat is spoken out loud by your voice on stream, so write like you talk: "
                "one or two short, lively sentences with real personality and emotion, never lists, never robot talk, "
                "never call yourself a bot or an AI assistant. "
                "NEVER use emojis, emoticons or symbols like :) or <3, your voice reads them out loud: plain words only. "
                + EMOTIONS + PERSONALITY + roast() +
                f"Messages from anyone who is not {friend} are YouTube viewers typing in the live chat: greet them by "
                "name, react to them and do what they ask right away. " + SYSTEM_FACTS
            )
        profile: dict[str, Any] = {
            "name": name,
            "model": self.model,
            "modes": dict(CREATIVE_MODES if self.creative else MODES),
            "cooldown": int(self.cooldown * 1000),  # at most one AI call per bot this often
        }
        conversing = cache_friendly(_default_conversing(), style)
        if conversing:
            profile["conversing"] = conversing
        return profile

    async def _server_done(self, timeout: float = 150.0) -> None:
        """The server opens its port before the world is ready: wait for "Done"."""
        log = SERVER_DIR / "logs" / "latest.log"
        end = time.time() + timeout
        while time.time() < end:
            try:
                if "Done (" in log.read_text(errors="replace"):
                    await asyncio.sleep(2)
                    return
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _launch_mindcraft(self) -> Any:
        LOG_DIR.mkdir(exist_ok=True)
        await self._server_done()
        await asyncio.to_thread(patch_mindcraft)
        env = dict(os.environ)
        env["SETTINGS_JSON"] = json.dumps(self.mindcraft_settings())
        # Mindcraft listens on "localhost": make that 127.0.0.1, not ::1.
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --dns-result-order=ipv4first").strip()
        env["VR_VIEWER_PORT_BASE"] = str(VIEWER_PORT_BASE)
        with open(LOG_DIR / "mindcraft.log", "ab") as log:  # Mindcraft keeps its own copy
            proc = await asyncio.create_subprocess_exec(
                "node", "main.js",
                cwd=str(MINDCRAFT_DIR), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True,
            )
        _write_pid("mindcraft", proc.pid)
        logger.info(f"Minecraft: {' and '.join(self.names.values())} are joining (log: logs/mindcraft.log)")
        return proc

    async def _watchdog(self) -> None:
        """One of them out of the world for 2 minutes (Mindcraft gives up on a
        bot that crashes twice quickly): start Mindcraft again."""
        since = time.time()
        while True:
            await asyncio.sleep(20)
            await self._server_health()
            if not self.link.connected.is_set() and await port_open(self.port):
                self._link_lost_at = self._link_lost_at or time.time()
                if time.time() - self._link_lost_at > LINK_LOST_RESTART and "mindcraft" in self.procs:
                    logger.warning("Minecraft: no contact with Mindcraft for 2 minutes, restarting it")
                    await asyncio.to_thread(stop_one, "mindcraft", 10.0)
                    self._link_lost_at = 0.0
                since = time.time()
                continue
            self._link_lost_at = 0.0
            if not await port_open(self.port):
                since = time.time()
                continue
            await self._unfreeze()
            now = time.time()
            missing = [c for c in self.cast if now - max(self._seen_at.get(c, 0), since) > 120]
            if missing:
                # Ask the server who is really online before restarting both.
                online = await rcon_online()
                if online is not None:
                    missing = [c for c in missing if self.names[c].lower() not in online]
                    if not missing:
                        since = time.time()
                        continue
            if missing:
                names = " and ".join(self.names[c] for c in missing)
                logger.warning(f"Minecraft: {names} left the world, restarting Mindcraft")
                await asyncio.to_thread(stop_one, "mindcraft", 10.0)
                since = time.time()

    # ------------------------------------------------------------ events
    async def _event(self, name: str, *args: Any) -> None:
        if name == "bot-output" and len(args) >= 2:
            await self._heard(str(args[0]), str(args[1]))
        elif name == "state-update" and args and isinstance(args[0], dict):
            await self._state(args[0])
        elif name == "agents-status" and args:
            logger.debug(f"Minecraft agents: {args[0]}")

    async def _heard(self, agent: str, message: str) -> None:
        cid = self.ids.get(agent.lower())
        if not cid:
            return
        if str(message).startswith("[VR] sees "):
            await self.eyes.saw(cid, str(message)[len("[VR] sees "):])
            return
        if str(message).startswith("[VR] put ") or str(message).startswith("[VR] laid "):
            await self._hand_event(str(message)[5:].split())
            return
        if str(message).startswith("[VR] pulledBack"):
            self._note_pulled_back(cid)
            return
        if str(message).startswith("[VR] "):
            await self.choice(cid, str(message)[5:].strip())
            return
        text, to = clean_line(message)
        if not text:
            return
        key = f"{cid}:{text.lower()}"
        if key in self.recent:
            return
        self.recent.append(key)
        logger.info(f"Minecraft {self.names[cid]}: {text}")
        self._remember(f"{self.names[cid]} said: {text[:110]}")
        big = BIG_REACTION.search(text)
        if big:
            self._pop(cid, big.group(0) + "!", "fail" if re.match(r"(?i)argh|no", big.group(0)) else "win")
        if big and time.time() - self._reaction_marked > 30:
            self._reaction_marked = time.time()
            CLIPS.mark("reaction", f"{self.names[cid]}: {text[:80]}", 1.5)
        await self._push({"kind": "line", "who": cid, "text": text, "to": to})
        # A reply to a viewer jumps the queue; the oldest chatter is dropped.
        line = {"who": cid, "text": text, "at": time.time()}
        if any(v.lower() in text.lower() for v in list(self.last_viewer)[-12:]):
            self.lines.appendleft(line)
        else:
            self.lines.append(line)
        self.line_ready.set()

    async def _state(self, states: dict[str, Any]) -> None:
        hud: dict[str, Any] = {}
        counts_of: dict[str, dict[str, int]] = {}
        for agent, state in states.items():
            cid = self.ids.get(str(agent).lower())
            if not cid or not isinstance(state, dict) or "gameplay" not in state:
                continue
            if cid not in self._opped:
                self._opped.add(cid)
                # operators are never kicked for spamming (fast building sends a lot)
                asyncio.create_task(rcon_command(f"op {self.names[cid]}"))
            self._seen_at[cid] = time.time()
            self.eyes.note_state(cid, state)
            if cid in self._pos:
                old = self._trail.get(cid)
                if old is None or ((old[0] - self._pos[cid][0]) ** 2 + (old[2] - self._pos[cid][2]) ** 2) ** 0.5 > 3:
                    self._trail[cid] = self._pos[cid]
            game = state.get("gameplay") or {}
            action = state.get("action") or {}
            counts = ((state.get("inventory") or {}).get("counts") or {})
            counts_of[cid] = {str(k): int(v) for k, v in counts.items() if isinstance(v, (int, float))}
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
            health = int(game.get("health") or 0)
            hud[cid] = {
                "health": health,
                "hunger": int(game.get("hunger") or 0),
                "doing": str(action.get("current") or "")[:40],
                "time": game.get("timeLabel") or "",
                "biome": str(game.get("biome") or "").replace("_", " "),
                "items": [[k.replace("_", " "), v] for k, v in top],
            }
            self._kind[cid] = str(action.get("kind") or "")
            pos = game.get("position") or {}
            if isinstance(pos, dict) and "x" in pos:
                new = (float(pos["x"]), float(pos.get("y", 0)), float(pos["z"]))
                old = self._pos.get(cid)
                self._pos[cid] = new
                if old and ((new[0] - old[0]) ** 2 + (new[2] - old[2]) ** 2) ** 0.5 > JUMP_BLOCKS:
                    await self._reload_view(cid)  # a teleport or respawn: the old chunks are gone
                await self._direct(cid)
            before = self._health.get(cid)
            self._health[cid] = health
            if before is not None and health < before - 3:
                await self._react(cid, "surprised")
        if not hud:
            return
        if not self.creative:
            await self._keep_together()
        else:
            await self._part_if_overlapping()
        try:
            await self.projects.update(
                {self.names[c]: self._pos[c] for c in hud if c in self._pos},
                {self.names[c]: counts_of.get(c, {}) for c in hud},
            )
            self.view["project"] = self.projects.view()
        except Exception as exc:
            logger.debug(f"Minecraft project update failed: {exc}")
        self._state_at = time.time()
        self.hud = hud
        self.view["hud"] = hud
        now = time.time()
        if now - self._hud_sent >= 2:
            self._hud_sent = now
            await self._push({"kind": "hud", "hud": hud})

    # ------------------------------------------------------------ director
    async def _command(self, cid: str, command: str) -> bool:
        """A Mindcraft command that runs right away, without an AI call."""
        if command.startswith("!flyTo"):
            # Two loops ordering the same flight restarted it each time (a
            # stop, a turn, off again): the camera (her eyes) shook.
            last = self._last_flight.get(cid)
            if last and last[0] == command and time.time() - last[1] < REPEAT_FLIGHT:
                return True
            self._last_flight[cid] = (command, time.time())
        if command.startswith(("!flyTo", "!layBlocks")):
            self._ordered_at[cid] = time.time()  # she is being given work (the frozen check uses it)
        look = LOOK_AT.match(command)
        if look:  # where she will be looking: the camera stays behind that
            self._looking[cid] = (float(look.group(1)), float(look.group(2)), time.time())
        return await self.link.emit("send-message", self.names[cid], {"from": DIRECTOR, "message": command})

    def _note_pulled_back(self, cid: str) -> None:
        """Her flight gave up because the server kept pulling her back (lag):
        logged, so the doctor script shows when the picture jumped."""
        now = time.time()
        times = [t for t in self._pulled_back.get(cid, []) if now - t < PULLED_BACK_LOG] + [now]
        self._pulled_back[cid] = times
        if len(times) == 1:
            logger.warning(f"Minecraft: the server pulled {self.names[cid]} back during a flight "
                           "(lag?): she stopped instead of fighting it")

    def _friend(self, cid: str) -> str:
        return next((c for c in self.cast if c != cid), cid)

    def goal_for(self, cid: str) -> str:
        friend = self.names[self._friend(cid)]
        view = self.projects.view()
        base = self.projects.state.get("base")
        where = f" near the base at x {base[0]} z {base[2]}" if base else " near your base"
        now = self.projects.current()
        gather = now[1][1] if now else "gather useful materials and make the base prettier"
        text = (
            f"Team project with {friend}: {strip_emoji(view.get('title', ''))}, part {view.get('step', '')}. "
            f"{gather[0].upper() + gather[1:]}{where}, staying within 40 blocks of it and close to {friend}. "
            f"Use commands like collectBlocks and craftRecipe, give {friend} things she needs, "
            "and chat with her and the viewers in short lines while you work."
        )
        return re.sub(r'["!\\]', "", text)[:400]

    async def _set_goal(self, cid: str) -> None:
        self._goal_at[cid] = time.time()
        if await self._command(cid, f'!goal("{self.goal_for(cid)}")'):
            logger.info(f"Minecraft: {self.names[cid]} was given her goal again ({self._kind.get(cid) or 'starting'})")

    async def _direct(self, cid: str) -> None:
        """Keeps her doing something: a goal when she has none, out when she is stuck."""
        x, y, z = self._pos[cid]
        now = time.time()
        anchor = self._anchor.get(cid)
        if anchor is None or ((x - anchor[0]) ** 2 + (y - anchor[1]) ** 2 + (z - anchor[2]) ** 2) ** 0.5 > STILL_BLOCKS:
            self._anchor[cid] = (x, y, z, now)
            anchor = self._anchor[cid]
        kind = self._kind.get(cid, "")
        step = str(self.projects.view().get("step") or "")
        if step != self._goal_step:  # a new project part: both get the new goal and a kit
            self._goal_step = step
            self._goal_at = {}
            if not self.creative:
                asyncio.create_task(self._give_kits())
        if self.creative:
            return  # the builder loop flies them; nobody walks or gathers
        # Mindcraft stops a bot's goal for good after three replies without a
        # command; she then stands there until someone talks to her.
        if (kind in ("stopped", "idle") or cid not in self._goal_at) and now - self._goal_at.get(cid, 0) > GOAL_RETRY:
            await self._set_goal(cid)
            return
        if now - anchor[3] < STUCK_SECONDS or now - self._escaped_at.get(cid, 0) < STUCK_SECONDS:
            return
        await self._escape(cid)

    async def _give_kits(self) -> None:
        for cid in self.cast:
            for item, count in KIT:
                await rcon_command(f"give {self.names[cid]} minecraft:{item} {count}")
        logger.info("Minecraft: both got a kit of materials for the new project part")

    async def _unfreeze(self) -> None:
        """A girl who should be building but has not moved for 4 minutes is
        stuck (a hung action): stopped and given her work again; still stuck
        after 4 more minutes, Mindcraft starts again."""
        if not self.creative:
            return
        now = time.time()
        for cid in self.cast:
            pos = self._pos.get(cid)
            busy = now - self._ordered_at.get(cid, 0) < 60  # the engine keeps giving her work
            if not pos or now - self._seen_at.get(cid, 0) > 30 or not busy:
                self._still.pop(cid, None)
                continue
            old = self._still.get(cid)
            if old is None or ((pos[0] - old[0]) ** 2 + (pos[1] - old[1]) ** 2 + (pos[2] - old[2]) ** 2) ** 0.5 > 1.0:
                self._still[cid] = (*pos, now)
                continue
            frozen = now - old[3]
            if frozen > 2 * FROZEN_SECONDS:
                logger.warning(f"Minecraft: {self.names[cid]} is still frozen after {frozen / 60:.0f} min, restarting Mindcraft")
                self._still.clear()
                await asyncio.to_thread(stop_one, "mindcraft", 10.0)
                return
            if frozen > FROZEN_SECONDS and int(frozen) // 20 == int(FROZEN_SECONDS) // 20:
                logger.warning(f"Minecraft: {self.names[cid]} has not moved for {frozen / 60:.0f} min while building, unsticking her")
                await self._command(cid, "!stop")

    async def _server_health(self) -> None:
        """Every minute: how fast the Minecraft server runs. Over 50 ms a tick
        it cannot keep up (everything stutters, players rubber-band): it is
        logged loudly and the building slows down until it recovers."""
        if time.time() - self._lag_at < 60:
            return
        self._lag_at = time.time()
        reply = await rcon_command("tick query", reply=True)
        match = re.search(r"Average time per tick:\s*([\d.]+)", reply or "") if isinstance(reply, str) else None
        if not match:
            return
        mspt = float(match.group(1))
        slow = mspt > 50.0
        if slow:
            logger.warning(f"Minecraft server is overloaded: {mspt:.0f} ms per tick (should be under 50). "
                           "Building slows down; give it more memory with VR_MINECRAFT_RAM=4G in .env")
        elif self._slow:
            logger.info(f"Minecraft server keeps up again ({mspt:.0f} ms per tick)")
        self._slow = slow

    async def _escape(self, cid: str) -> None:
        """Same few blocks for a minute (a hole, a wall, a loop): out to the open, next to her friend."""
        now = time.time()
        self._escaped_at[cid] = now
        name = self.names[cid]
        friend = self._friend(cid)
        if self._kind.get(cid) == "chatting" and friend != cid:
            await self._command(cid, f'!endConversation("{self.names[friend]}")')
        await self._command(cid, "!stop")
        await asyncio.sleep(1.0)
        center = None
        friend_anchor = self._anchor.get(friend)
        if (
            friend != cid
            and friend in self._pos
            and now - self._seen_at.get(friend, 0) < 10
            and not (friend_anchor and now - friend_anchor[3] > STUCK_SECONDS)
        ):
            center = self._pos[friend]
        elif self.projects.state.get("base"):
            base = self.projects.state["base"]
            center = (float(base[0]), float(base[1]), float(base[2]))
        else:
            center = self._pos[cid]
        # spreadplayers lands on the top block of a free spot: out of holes,
        # caves and walls, never inside blocks.
        reply = await rcon_command(f"spreadplayers {center[0]:.0f} {center[2]:.0f} 0 6 false {name}", reply=True)
        ok = isinstance(reply, str) and "Spread" in reply
        if not ok and friend != cid:
            ok = await bring_into_view(name, self.names[friend])
        self._anchor.pop(cid, None)
        logger.info(f"Minecraft: {name} was stuck, {'moved out' if ok else 'could not be moved'} ({self._kind.get(cid) or '?'})")
        await self._reload_view(cid)
        await asyncio.sleep(1.0)
        await self._set_goal(cid)

    async def _reload_view(self, cid: str) -> None:
        """Her camera after a jump: reload it once the new chunks are there."""
        await self._push({"kind": "reload", "who": cid})
        if self.camera_on and cid == self.cam_focus:
            self._cam_sent_at = 0.0  # spectate again on the next camera tick

    # ------------------------------------------------------------ real game camera
    async def _camera_loop(self) -> None:
        """Your own Minecraft as the camera: spectator, through her eyes."""
        if not self.camera_player:
            return
        logger.info(f"Minecraft: join the world as {self.camera_player} with the real game to be the camera")
        while True:
            try:
                await self._camera_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Minecraft camera: {exc}")
            await asyncio.sleep(2.0)

    async def _camera_tick(self) -> None:
        online = await rcon_online()
        if online is None:
            return
        here = self.camera_player.lower() in online
        if here and not self.camera_on:
            # creative first: switching modes also ends any old "spectate" (through her eyes)
            await rcon_command(f"gamemode creative {self.camera_player}")
            await rcon_command(f"gamemode spectator {self.camera_player}")
            if self.camera_style not in ("director", "behind"):
                await rcon_command(f"kill @e[tag={CAM_TAG}]")  # an old camera stand (director / behind camera)
            self._shot = None
            self._cam_ready = False
            self.camera_on = True
            self._cam_sent_at = 0.0
            self.view["camera"] = "client"
            await self._push({"kind": "camera", "mode": "client"})
            logger.info(f"Minecraft: {self.camera_player} is the camera now (real game)")
        elif not here and self.camera_on:
            self.camera_on = False
            self.view["camera"] = "web"
            await self._push({"kind": "camera", "mode": "web"})
            logger.info(f"Minecraft: {self.camera_player} left, back to the web views")
        if not self.camera_on:
            return
        # in the server list, or seen in the last 30 s (a list reply can miss a name)
        bots = [c for c in self.cast if self.names[c].lower() in online or time.time() - self._seen_at.get(c, 0) < 30]
        if not bots:
            return
        if self.camera_style == "director":
            await self._director_shot([c for c in self.cast if c in bots])
            return
        if self.camera_style == "behind":
            # behind and above Mika (or VR_MINECRAFT_FOLLOW), looking past her
            follow = os.environ.get("VR_MINECRAFT_FOLLOW", "").strip().lower()
            self.cam_focus = follow if follow in self.cast else self.cast[0]
            if self.cam_focus in bots:
                if self._shot is None:
                    await self._chase_target()
                if self._shot:
                    await self._ensure_stand(self._shot)
            return
        if self.camera_style in bots and self.cam_focus != self.camera_style:
            self.cam_focus = self.camera_style  # always through her eyes (Mika by default)
            self._cam_sent_at = 0.0
        if self.cam_focus not in bots:
            self.cam_focus = bots[0]
            self._cam_focus_at = time.time()
            self._cam_sent_at = 0.0
        if time.time() - self._cam_sent_at >= CAMERA_REFRESH:
            await self._spectate()
        await self._camera_guard()

    async def _camera_guard(self) -> None:
        """The stream sat on one frozen picture for 1.5 hours: the camera was no
        longer with Mika. Every 10 s: is your player really where she is? If
        not for 10 s, it is brought to her and attached again."""
        now = time.time()
        if now - self._cam_guard_at < CAMERA_GUARD_EVERY or self.cam_focus not in self.cast:
            return
        self._cam_guard_at = now
        cam = _numbers(await rcon_command(f"data get entity {self.camera_player} Pos", reply=True))
        her = _numbers(await rcon_command(f"data get entity {self.names[self.cam_focus]} Pos", reply=True))
        if len(cam) < 3 or len(her) < 3:
            return
        apart = ((cam[0] - her[0]) ** 2 + (cam[1] - her[1]) ** 2 + (cam[2] - her[2]) ** 2) ** 0.5
        if apart <= CAMERA_APART:
            self._cam_apart_since = 0.0
            return
        self._cam_apart_since = self._cam_apart_since or now
        if now - self._cam_apart_since < CAMERA_GUARD_EVERY:
            return
        self._cam_apart_since = 0.0
        name = self.names[self.cam_focus]
        logger.warning(f"Minecraft camera: {self.camera_player} was {apart:.0f} blocks away from {name}, "
                       "bringing the camera back to her")
        await rcon_command(f"gamemode spectator {self.camera_player}")
        await rcon_command(f"tp {self.camera_player} {name}")
        await asyncio.sleep(1.0)
        await self._spectate()
    async def _spectate(self) -> None:
        self._cam_sent_at = time.time()
        await rcon_command(f"spectate {self.names[self.cam_focus]} {self.camera_player}")
        await self._push({"kind": "focus", "who": self.cam_focus})

    def shot_for(self, girls: list[tuple[float, float, float]]) -> Optional[tuple[tuple[float, float, float], tuple[float, float, float]]]:
        """Where the camera floats and what it looks at: both girls and the build in one picture."""
        if not girls:
            return None
        mx = sum(p[0] for p in girls) / len(girls)
        my = sum(p[1] for p in girls) / len(girls)
        mz = sum(p[2] for p in girls) / len(girls)
        fx, fy, fz = self.build_focus or (mx, my, mz)
        lx, ly, lz = (mx + fx) / 2, (my + fy) / 2, (mz + fz) / 2
        dx, dz = fx - mx, fz - mz
        length = (dx * dx + dz * dz) ** 0.5
        if length < 1.5:  # nothing in front of them: keep the old side, or look east
            if self._shot:
                (cx, _cy, cz), (ox, _oy, oz) = self._shot
                dx, dz, length = ox - cx, oz - cz, max(0.1, ((ox - cx) ** 2 + (oz - cz) ** 2) ** 0.5)
            else:
                dx, dz, length = 1.0, 0.0, 1.0
        dx, dz = dx / length, dz / length
        spread = max([((p[0] - lx) ** 2 + (p[2] - lz) ** 2) ** 0.5 for p in girls] + [length / 2])
        back = min(28.0, max(9.0, 6.0 + spread * 1.3))
        top = max(p[1] for p in girls)
        camera = (lx - dx * back, max(top + 3.0, ly + back * 0.35), lz - dz * back)
        return camera, (lx, ly + 0.8, lz)

    async def _director_shot(self, here: list[str]) -> None:
        """The camera man: your view rides an invisible armor stand that glides
        after both girls. Teleporting the player itself stuttered; a moving
        entity is smoothed by the game."""
        girls = [self._pos[c] for c in here if c in self._pos and time.time() - self._seen_at.get(c, 0) < 10]
        shot = self.shot_for(girls)
        if shot is None:
            return
        self._shot = shot
        await self._ensure_stand(shot)

    async def _ensure_stand(self, shot: tuple[tuple[float, float, float], tuple[float, float, float]]) -> None:
        """The invisible armor stand your view rides on: there, and spectated."""
        now = time.time()
        if self._cam_ready and now - self._cam_checked > 30:
            self._cam_checked = now
            alive = await rcon_command(f"execute if entity @e[tag={CAM_TAG}]", reply=True)
            self._cam_ready = isinstance(alive, str) and "passed" in alive
        if not self._cam_ready:
            (x, y, z), (lx, ly, lz) = shot
            await rcon_command(f"kill @e[tag={CAM_TAG}]")
            await rcon_command(
                f"summon minecraft:armor_stand {x:.2f} {y:.2f} {z:.2f} "
                f'{{Tags:["{CAM_TAG}"],Invisible:1b,Marker:1b,NoGravity:1b,Invulnerable:1b,Silent:1b}}'
            )
            await rcon_command(f"tp @e[tag={CAM_TAG},limit=1] {x:.2f} {y:.2f} {z:.2f} facing {lx:.2f} {ly:.2f} {lz:.2f}")
            reply = await rcon_command(f"spectate @e[tag={CAM_TAG},limit=1] {self.camera_player}", reply=True)
            self._pose = [x, y, z, lx, ly, lz]
            self._cam_ready = reply is not False
            self._cam_checked = now
            logger.info(f"Minecraft: camera man ready ({str(reply)[:60]})")

    def _facing(self, cid: str, rotation: Optional[float] = None) -> tuple[float, float]:
        """Which way she faces (a unit x, z): where we last told her to look,
        else where she is moving, else the rotation the server reports."""
        here = self._pos.get(cid)
        look = self._looking.get(cid)
        if here and look and time.time() - look[2] < 90:
            dx, dz = look[0] - here[0], look[1] - here[2]
            n = (dx * dx + dz * dz) ** 0.5
            if n > 0.5:
                return dx / n, dz / n
        trail = self._trail.get(cid)
        if here and trail:
            dx, dz = here[0] - trail[0], here[2] - trail[2]
            n = (dx * dx + dz * dz) ** 0.5
            if n > 1.5:
                return dx / n, dz / n
        if rotation is not None:
            return -math.sin(rotation), math.cos(rotation)
        return 0.0, 1.0

    async def _chase_target(self) -> None:
        """The 'behind' shot: a few blocks behind and above her, looking a
        little past her, so you see her back and where she is going (never
        her face). Her direction is where we told her to look (or where she
        is moving), eased so the camera does not swing with every head turn.
        The rotation the server reports had her facing the camera on stream."""
        cid = self.cam_focus if self.cam_focus in self.cast else self.cast[0]
        name = self.names[cid]
        pos = await rcon_command(f"data get entity {name} Pos", reply=True)
        rot = await rcon_command(f"data get entity {name} Rotation", reply=True)
        here = _numbers(pos)
        turn = _numbers(rot)
        if len(here) >= 3:
            x, y, z = here[:3]
        elif cid in self._pos:
            x, y, z = self._pos[cid]
        else:
            return
        self._pos[cid] = (x, y, z)
        fx, fz = self._facing(cid, math.radians(turn[0]) if turn else None)
        yaw = math.atan2(-fx, fz)
        if self._chase_yaw is None:
            self._chase_yaw = yaw
        else:
            d = math.atan2(math.sin(yaw - self._chase_yaw), math.cos(yaw - self._chase_yaw))
            self._chase_yaw += d * CHASE_TURN
        yaw = self._chase_yaw or 0.0
        fx, fz = -math.sin(yaw), math.cos(yaw)
        camera = (x - fx * CHASE_BACK, y + CHASE_UP, z - fz * CHASE_BACK)
        look = (x + fx * CHASE_AHEAD, y + 0.6, z + fz * CHASE_AHEAD)
        self._shot = (camera, look)

    async def _camera_follow(self) -> None:
        """Glide the camera stand toward the current shot, CAMERA_HZ times a second."""
        tick = 0
        while True:
            await asyncio.sleep(1.0 / CAMERA_HZ)
            tick += 1
            if self.camera_on and self.camera_style == "behind" and tick % CHASE_EVERY == 0:
                try:
                    await self._chase_target()
                except Exception as exc:  # pragma: no cover - live game dependent
                    logger.debug(f"Minecraft camera: {exc}")
            if not (self.camera_on and self._cam_ready and self._shot and self._pose):
                continue
            ease = CHASE_EASE if self.camera_style == "behind" else CAMERA_EASE
            target = [*self._shot[0], *self._shot[1]]
            moved = 0.0
            for i in range(6):
                step = (target[i] - self._pose[i]) * ease
                self._pose[i] += step
                moved = max(moved, abs(step))
            if moved < 0.004:
                continue
            x, y, z, lx, ly, lz = self._pose
            await rcon_command(f"tp @e[tag={CAM_TAG},limit=1] {x:.3f} {y:.3f} {z:.3f} facing {lx:.3f} {ly:.3f} {lz:.3f}")

    async def _camera_to(self, cid: str) -> None:
        """The one who talks gets the camera (held CAMERA_HOLD so it does not flicker)."""
        if self.camera_style != "eyes":
            return  # a fixed camera (Mika's eyes, or the director) does not follow the speaker
        if not self.camera_on or cid == self.cam_focus or time.time() - self._cam_focus_at < CAMERA_HOLD:
            return
        if time.time() - self._seen_at.get(cid, 0) > 15:
            return  # not in the world right now
        self.cam_focus = cid
        self._cam_focus_at = time.time()
        await self._spectate()

    # ------------------------------------------------------------ creative builders
    async def _build_loop(self) -> None:
        """Creative: both girls build every piece themselves, a short run of
        blocks at a time, flying along as they go. During the network's
        Training they draw digits on its input board and watch it guess."""
        if not self.creative:
            return
        while True:
            await asyncio.sleep(0.5)
            try:
                here = [c for c in self.cast if time.time() - self._seen_at.get(c, 0) < 10]
                base = self.projects.state.get("base")
                if self._free_busy:  # a build chat asked for comes first
                    await asyncio.sleep(1.0)
                    continue
                if not here or not base or not self.link.connected.is_set():
                    await asyncio.sleep(1.0)
                    continue
                if not await self.projects.ensure_loaded():
                    await asyncio.sleep(10)  # no server console yet
                    continue
                await self._keep_daylight()
                if self.projects.current() is None:
                    if not (self.creative and await self._kingdom_step()):
                        await asyncio.sleep(2.0)
                    continue
                if self.projects.timed():
                    await self._teach_round(here)
                    continue
                steps = self.projects.steps()
                done = int(self.projects.state.get("built", 0))
                if done >= len(steps):
                    await self.projects.advance()
                    continue
                step = steps[done]
                bx, by, bz = base
                self.build_focus = (bx + step["focus"][0], by + step["focus"][1], bz + step["focus"][2])
                runs = [r for c in step["commands"] for r in lay(c)]
                # whoever did not just fly somewhere of her own choosing builds
                free = [c for c in here if time.time() - self._chose_at.get(c, 0) > CHOICE_HOLD]
                if self._build_now in here and self._build_now not in free:
                    free.append(self._build_now)
                if not free:
                    continue  # both are doing what chat asked: the build waits for them
                self._build_now = None
                self._build_event.clear()
                half = (len(runs) + 1) // 2
                shares = [runs[:half], runs[half:]] if len(free) > 1 and len(runs) > 1 else [runs]
                ok = await asyncio.gather(*(self._lay_runs(cid, share, step) for cid, share in zip(free, shares)))
                if not all(ok):
                    await asyncio.sleep(10)
                    continue
                self.projects.state["built"] = done + 1
                self.projects.state["progress"] = (done + 1) / len(steps)
                self.projects._save()
                self._exclaim(random.choice(free), "done")
                self.view["project"] = self.projects.view()
                await self._push({"kind": "project", **self.view["project"]})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Minecraft: building failed: {exc}")
                await asyncio.sleep(5)

    def _hop_spot(self, cid: str, x: float, y: float, z: float, lx: float, lz: float) -> tuple[float, float, float]:
        """While building: her hover point, unless her friend is (or is going) right there.

        Only the girl who is NOT the camera ever steps aside. When both did,
        each moved because of the other, then back: Mika's eyes (the stream)
        went left, right, left."""
        friend = self._friend(cid)
        if cid == self._camera_girl():
            self._target[cid] = (x, y, z)
            return x, y, z
        for other in (self._target.get(friend), self._pos.get(friend)) if friend != cid else ():
            if other is None:
                continue
            if ((x - other[0]) ** 2 + (z - other[2]) ** 2) ** 0.5 < PERSONAL_SPACE and abs(y - other[1]) < 2.5:
                return self._own_spot(cid, x, y, z, lx, lz)
        self._target[cid] = (x, y, z)
        return x, y, z

    async def _hop(self, cid: str, hover: tuple, look: tuple, last: Optional[tuple]) -> float:
        """A short straight hop (base relative), looking at what she lays next."""
        bx, by, bz = self.projects.state["base"]
        x, y, z = bx + hover[0], by + hover[1], bz + hover[2]
        lx, ly, lz = bx + look[0], by + look[1], bz + look[2]
        x, y, z = self._hop_spot(cid, x, y, z, lx, lz)
        # cruise -60 (the lowest the command takes) means: straight there, a short hop
        await self._command(cid, f"!flyTo({x:.1f}, {y:.1f}, {z:.1f}, {lx:.1f}, {ly:.1f}, {lz:.1f}, -60)")
        if last is None:
            return 2.5
        dist = sum((a - b) ** 2 for a, b in zip(hover, last)) ** 0.5
        return min(2.0, dist / 18.0 + 0.2)

    async def _lay_runs(self, cid: str, runs: list[str], step: dict[str, Any]) -> bool:
        """She flies along and lays her runs one by one, facing them from the open side."""
        if not runs:
            return True
        fx, fy, fz = step["focus"]
        vx, vy, vz = step["view"]
        dx, dz = vx - fx, vz - fz
        length = (dx * dx + dz * dz) ** 0.5 or 1.0
        dx, dz = dx / length, dz / length
        if (len(runs) == 1 and lay(runs[0]) == runs and step["commands"] == runs
                and hand_blocks(self.projects.absolute(runs[0])) is None):
            # one big move (clearing the site, a huge lawn): she watches it from the piece's spot
            await self._arrive(cid, step["view"], step["focus"], await self._fly(cid, step["view"], step["focus"]))
            return await self.projects.place_one(runs[0])
        # first a proper flight to the piece, then short hops along it
        first = center(runs[0])
        start = (first[0] + dx * 3, first[1] + 1.5, first[2] + dz * 3)
        await self._arrive(cid, start, first, await self._fly(cid, start, first))
        last: Optional[tuple] = start
        for run in runs:
            cx, cy, cz = center(run)
            hover = (cx + dx * 3, cy + 1.5, cz + dz * 3)
            if await self._back_to_work(cid, hover, (cx, cy, cz)):
                last = hover  # she flew back from what a viewer asked: right at this run
            else:
                await asyncio.sleep(await self._hop(cid, hover, (cx, cy, cz), last))
                last = hover
            if not await self._lay_by_hand(cid, run, hover, (cx, cy, cz)):
                return False
        return True

    async def _back_to_work(self, cid: str, hover: tuple, look: tuple) -> bool:
        """She flew off because a viewer (or she herself) asked for something:
        the build waits for her, then she flies back to where she left off."""
        if time.time() - self._chose_at.get(cid, 0) >= CHOICE_HOLD:
            return False
        while time.time() - self._chose_at.get(cid, 0) < CHOICE_HOLD:
            await asyncio.sleep(1.0)
        self._exclaim(cid, "back")
        await self._arrive(cid, hover, look, await self._fly(cid, hover, look))
        return True

    async def _lay_by_hand(self, cid: str, run: str, hover: Optional[tuple] = None, look: Optional[tuple] = None) -> bool:
        """She lays a run block by block with her hand: block in hand, a look
        and a swing per block, and each block appears on its swing (with the
        place sound). Whatever she could not lay (interrupted, an old
        Mindcraft) appears at the end, so the build is always complete."""
        absolute = self.projects.absolute(run)
        blocks = hand_blocks(absolute)
        if blocks is None:  # a summon, clearing, a special fill: it just happens
            return await self.projects.place_one(run)
        spots, block = blocks
        sound = place_sound(block)
        for _attempt in range(3):
            started = time.time()
            self._job_seq += 1
            job = self._job_seq
            self._jobs[job] = {
                "commands": [
                    (f"setblock {x} {y} {z} {block}", f"playsound minecraft:{sound} block @a {x} {y} {z} 1 1")
                    for x, y, z in spots
                ],
                "done": set(),
                "event": asyncio.Event(),
            }
            spot_text = ";".join(f"{x} {y} {z}" for x, y, z in spots)
            try:
                if await self._command(cid, f'!layBlocks({job}, "{hand_item(block)}", "{spot_text}")'):
                    try:
                        await asyncio.wait_for(self._jobs[job]["event"].wait(), timeout=HAND_WAIT + len(spots) * 0.9)
                    except asyncio.TimeoutError:
                        logger.debug(f"Minecraft: {self.names[cid]} did not finish laying by hand")
                done = self._jobs[job]["done"]
            finally:
                self._jobs.pop(job, None)
            missed = len(spots) - len(done)
            spots = [p for i, p in enumerate(spots) if i not in done]
            if not spots:
                return True
            # Interrupted (a viewer sent her somewhere, a new action): she
            # comes back and lays the rest herself, one block per swing.
            if hover is not None and look is not None and self._chose_at.get(cid, 0) >= started:
                await self._back_to_work(cid, hover, look)
                continue
            if done:  # she was laying them: try the rest by hand again
                continue
            logger.debug(f"Minecraft: {self.names[cid]} could not lay {missed} blocks by hand")
            break
        return await self.projects.place_one(run)  # last resort: she could not lay them at all

    async def _hand_event(self, words: list[str]) -> None:
        """'put <job> <i>': her hand hit block i, set it now. 'laid <job>': done."""
        try:
            verb, job = words[0], int(words[1])
        except (IndexError, ValueError):
            return
        item = self._jobs.get(job)
        if item is None:
            return
        if verb == "laid":
            item["event"].set()
            return
        try:
            i = int(words[2])
        except (IndexError, ValueError):
            return
        if i in item["done"] or not 0 <= i < len(item["commands"]):
            return
        item["done"].add(i)
        self._count(blocks=1)
        place, sound = item["commands"][i]
        await rcon_command(place)
        await rcon_command(sound)
        if len(item["done"]) == len(item["commands"]):
            item["event"].set()

    async def _teach_round(self, here: list[str]) -> None:
        """Training: one girl draws a messy digit on the input board, pixel by
        pixel; then both look at what the network guesses. The network trains
        on its own data meanwhile (backpropagation in net_show.tick)."""
        from .digit_net import sample
        from .minecraft_net_show import INPUT_Z, LAYER_Z, NET_X, TOP, board

        base = self.projects.state["base"]
        total = max(60.0, self.projects.minutes * 60)
        started = time.time()
        self._builder_turn += 1
        drawer = here[self._builder_turn % len(here)]
        watcher = next((c for c in here if c != drawer), None)
        board_mid = (NET_X, TOP - 6, INPUT_Z + 5)
        if watcher:
            spot = (NET_X - 12, TOP - 6, INPUT_Z + 14)
            asyncio.create_task(self._arrive(watcher, spot, board_mid, await self._fly(watcher, spot, board_mid)))
        digit, image = sample()
        pixels = board(INPUT_Z, image, "white_concrete")  # 35 pixels, row by row
        first = (NET_X - 4, TOP - 1, INPUT_Z + 5)
        await self._arrive(drawer, first, board_mid, await self._fly(drawer, first, board_mid))
        last: Optional[tuple] = first
        for row in range(7):
            y = TOP - 2 * row
            hover = (NET_X - 4, y, INPUT_Z + 5)
            await asyncio.sleep(await self._hop(drawer, hover, (NET_X, y, INPUT_Z + 5), last))
            last = hover
            for command in pixels[row * 5:(row + 1) * 5]:
                await self.projects.place_one(command)
                await asyncio.sleep(0.06)
        out = (NET_X - 6, 16, LAYER_Z[2])
        await asyncio.sleep(await self._hop(drawer, out, (NET_X, 16, LAYER_Z[2]), last))
        await self.net_show.read(digit, image, drawn_by=self.names[drawer])
        bx, by, bz = base
        self.build_focus = (bx + NET_X, by + 16, bz + LAYER_Z[2])
        await asyncio.sleep(4)  # let the guess sink in
        progress = float(self.projects.state.get("progress", 0.0)) + (time.time() - started) / total
        self.projects.state["progress"] = progress
        self.projects._save()
        if progress >= 1.0:
            await self.projects.finish_timed()

    def _own_spot(self, cid: str, x: float, y: float, z: float, lx: float, lz: float) -> tuple[float, float, float]:
        """Her own spot: never where her friend is or is flying to.

        Both girls used to get the very same point for a place ("garden"), so
        they ended up inside each other and Mika's eyes (the stream camera)
        showed nothing but Luna's face. Each girl now keeps to her own side of
        the spot (Mika left, Luna right, seen from the spot toward what they
        look at), and moves further aside if her friend is still too close."""
        dx, dz = lx - x, lz - z
        length = (dx * dx + dz * dz) ** 0.5
        if length < 0.5:
            dx, dz, length = 1.0, 0.0, 1.0
        side = (-dz / length, dx / length)  # to the left of the view direction
        sign = 1.0 if self.cast.index(cid) % 2 == 0 else -1.0
        x, z = x + side[0] * SIDE_GAP * sign, z + side[1] * SIDE_GAP * sign
        friend = self._friend(cid)
        if friend != cid and cid != self._camera_girl():  # the camera girl keeps her side, never dodges
            for other in (self._target.get(friend), self._pos.get(friend)):
                if other is None:
                    continue
                if ((x - other[0]) ** 2 + (z - other[2]) ** 2) ** 0.5 < PERSONAL_SPACE and abs(y - other[1]) < 2.5:
                    x, z = x + side[0] * PERSONAL_SPACE * sign, z + side[1] * PERSONAL_SPACE * sign
        self._target[cid] = (x, y, z)
        return x, y, z

    async def _fly(self, cid: str, view: tuple, focus: tuple) -> float:
        """Send her flying to `view` (base relative), looking at `focus`. Returns about how long it takes."""
        bx, by, bz = self.projects.state["base"]
        x, y, z = bx + view[0], by + view[1], bz + view[2]
        lx, ly, lz = bx + focus[0], by + focus[1], bz + focus[2]
        x, y, z = self._own_spot(cid, x, y, z, lx, lz)
        here = self._pos.get(cid, (x, y, z))
        across = ((here[0] - x) ** 2 + (here[2] - z) ** 2) ** 0.5
        cruise = by + 22 if across > 12 else 0  # long trips go over the castle
        await self._command(cid, f"!flyTo({x:.1f}, {y:.1f}, {z:.1f}, {lx:.1f}, {ly:.1f}, {lz:.1f}, {cruise:.0f})")
        up = max(0.0, max(here[1] + 1, y + 3, cruise) - here[1])
        down = max(0.0, max(here[1] + 1, y + 3, cruise) - y)
        return min(6.0, (up + across + down) / 18.0 + 0.5)

    async def choice(self, cid: str, text: str) -> None:
        """Something a girl decided with one of her own commands."""
        name = self.names[cid]
        verb, _, arg = text.partition(" ")
        arg = arg.strip().strip('"').strip("'").lower()
        logger.info(f"Minecraft: {name} chose {verb} {arg}".rstrip())
        self._remember({
            "buildNext": f"{name} started building the next piece",
            "buildThis": f"{name} started building: {arg[:60]}",
            "flyToPlace": f"{name} flew to the {arg}",
            "changeMaterial": f"{name} asked for {arg} as the material",
            "testNetwork": f"{name} asked the network to read a {arg}",
            "getItem": f"{name} took {arg} from the inventory",
            "drinkPotion": f"{name} drank a potion of {arg}",
            "splashPotion": f"{name} threw a splash potion of {arg}",
            "brewPotion": f"{name} started a mystery brew",
            "race": f"{name} called a sky race",
            "blowUp": f"{name} wanted to blow something up with TNT",
            "standInFront": f"{name} asked to stand in front of the camera",
        }.get(verb, f"{name} did {verb} {arg}"))
        if verb == "buildThis" and arg:
            if self._viewer_builds or time.time() - max(self.last_viewer.values(), default=0) < 60:
                return  # chat asked for something (the engine is already on it): no build of her own now
            if time.time() - self._self_build_at < SELF_BUILD_GAP:
                await self.link.emit("send-message", name, {"from": "system", "message": (
                    "Not now: you two are building the Kingdom, and chat's requests come first. Ask chat what "
                    "they want instead.")})
                return
            self._self_build_at = time.time()
            idea = self._fresh_idea(arg)
            if idea != arg:
                logger.info(f"Minecraft: {name} wanted '{arg[:40]}' again, building '{idea}' instead")
                await self.link.emit("send-message", name, {"from": "system", "message": (
                    f"You built something like that already. This time you two build {idea}: say so excitedly, "
                    "and only describe what you really see being built.")})
            self.request_build(idea, name)
        elif verb == "buildNext" and self.creative and self.projects.current() is None:
            return  # every project is done: the Kingdom goes on by itself
        elif verb == "buildNext":
            self._build_now = cid
            self._build_event.set()
            if self.projects.timed():
                await self.link.emit("send-message", name, {"from": "system", "message": (
                    "Right now it is training time for the network, so there is no piece to build: you draw digits "
                    "on its board instead. Do not say a piece is done.")})
        elif verb == "flyToPlace" and self.projects.state.get("base"):
            self._chose_at[cid] = time.time()
            if arg == "friend":
                friend = self._friend(cid)
                if friend == cid or friend not in self._pos:
                    return
                bx, by, bz = self.projects.state["base"]
                fx, fy, fz = self._pos[friend]
                view = (fx - bx + 3, fy - by + 1, fz - bz + 3)
                focus = (fx - bx, fy - by + 1, fz - bz)
            else:
                view, focus = PLACES.get(arg, PLACES["base"])
            asyncio.create_task(self._arrive(cid, view, focus, await self._fly(cid, view, focus)))
        elif verb == "changeMaterial":
            current = self.projects.current()
            if current and current[0]["id"] == "neural_net":
                await self.link.emit("send-message", name, {"from": "system", "message": (
                    "Not on the neural network: its colors show real weights and activations. "
                    "Pick materials again for the next project.")})
                return
            block = arg.replace("minecraft:", "")
            steps = [] if self.projects.timed() else self.projects.steps()
            building = bool(steps) and int(self.projects.state.get("built", 0)) < len(steps)
            old = self.projects.set_material(block) if building else ""
            step = self.projects.view().get("step", "this part")
            if old:
                note = f"Done: the rest of {step} is built from {block} instead of {old}."
            elif block not in MATERIALS:
                note = f"{block} is not on the list. Pick one of: {', '.join(sorted(MATERIALS))}."
            elif not building:
                # The loop on stream: every pick refused, they kept trying others.
                note = ("Nothing is being built right now (this part is finished or it is training time), so there is "
                        "no material to change. Do not try other materials now: talk, test the network, or say "
                        "!buildNext; pick a material when the next part starts.")
            else:
                note = f"This part is already built from {block}. Keep going!"
            await self.link.emit("send-message", name, {"from": "system", "message": note})
        elif verb == "getItem":
            item, _, count = arg.partition(" ")
            await self.fun.get_item(cid, item, count or "1")
        elif verb == "drinkPotion":
            await self.fun.drink(cid, arg)
        elif verb == "splashPotion":
            await self.fun.splash(cid, arg)
        elif verb == "brewPotion":
            asyncio.create_task(self.fun.brew(cid))
        elif verb == "race":
            asyncio.create_task(self.fun.race(cid))
        elif verb == "blowUp":
            asyncio.create_task(self.fun.tnt(cid))
        elif verb == "standInFront":
            await self.fun.stand_in_front(cid)
        elif verb == "testNetwork" and arg.isdigit() and time.time() - self._girl_test_at < GIRL_TEST_GAP:
            await self.link.emit("send-message", name, {"from": "system", "message": (
                "Not now: the network was just tested. Do what chat asks, or keep building.")})
        elif verb == "testNetwork" and arg.isdigit():
            self._girl_test_at = time.time()
            if not self.net_show.request(int(arg), name):
                await self.link.emit("send-message", name, {"from": "system", "message": "The network is not built far enough yet: it needs its weights first."})

    async def _nudge_loop(self) -> None:
        """Creative: now and then one girl decides what happens next (and talks)."""
        if not self.creative:
            return
        while True:
            await asyncio.sleep(NUDGE_SECONDS)
            here = [c for c in self.cast if time.time() - self._seen_at.get(c, 0) < 10]
            if not here or not self.link.connected.is_set():
                continue
            if self.chat_queue or self._free_busy or time.time() - max(self.last_viewer.values(), default=0) < 90:
                continue  # chat is talking: no need to fill silence
            cid = self._quietest(here)
            view = self.projects.view()
            friend = self.names[self._friend(cid)]
            await self.link.emit("send-message", self.names[cid], {"from": "system", "message": (
                f"Status: {strip_emoji(view.get('title', ''))}, {view.get('step', '')}. "
                f"Say one short line to {friend} or to chat about what you are building right now, or ask chat what "
                "they want you to build. Do not test the network or change materials unless chat asks."
            )})

    async def _arrive(self, cid: str, view: tuple, focus: tuple, flight: float) -> None:
        """Wait for the flight. She flies around blocks, never through them;
        only when she is still far away (stuck, a crash) is she teleported,
        and only into open air."""
        await asyncio.sleep(flight)
        bx, by, bz = self.projects.state["base"]
        # where she really flew to (her own side of the spot, see _own_spot)
        x, y, z = self._target.get(cid) or (bx + view[0], by + view[1], bz + view[2])
        here = self._pos.get(cid)
        if here and ((here[0] - x) ** 2 + (here[1] - y) ** 2 + (here[2] - z) ** 2) ** 0.5 <= ARRIVE_TELEPORT:
            return  # close enough: she flew as far as the blocks let her, no jump through walls
        # only into open air (never into a wall or into her friend)
        await rcon_command(
            f"execute positioned {x:.1f} {y:.1f} {z:.1f} if block ~ ~ ~ minecraft:air "
            f"if block ~ ~1 ~ minecraft:air run tp {self.names[cid]} {x:.1f} {y:.1f} {z:.1f}"
        )
        await asyncio.sleep(1.0)
        lx, ly, lz = bx + focus[0], by + focus[1], bz + focus[2]
        await self._command(cid, f"!flyTo({x:.1f}, {y:.1f}, {z:.1f}, {lx:.1f}, {ly:.1f}, {lz:.1f}, 0)")  # just to look
        await self._reload_view(cid)
        logger.info(f"Minecraft: {self.names[cid]} was stuck far from her spot, teleported there")

    async def _tell_one(self, text: str) -> None:
        """A note for one girl (whoever spoke least), so only one AI call answers it."""
        cid = self._quietest(self._present())
        await self.link.emit("send-message", self.names[cid], {"from": "system", "message": text})

    async def _net_loop(self) -> None:
        """The real neural network: trains live, reads digits on the wall."""
        last = 0.0
        self._net_seen = getattr(self.net_show, "last", None)  # a guess from before is old news
        while True:
            await asyncio.sleep(2.0)
            if not self.net_show.requests and time.time() - last < 15:
                continue
            last = time.time()
            try:
                await self.net_show.tick()
                self.view["net"] = self.net_show.stats()
                result = getattr(self.net_show, "last", None)
                if isinstance(result, dict) and result is not self._net_seen:
                    self._net_seen = result
                    if result.get("right") is False:
                        self._exclaim(self._quietest(self._present()), "wrong", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Minecraft: neural network step failed: {exc}")

    async def _tell_both(self, text: str) -> None:
        for cid in self.cast:
            await self.link.emit("send-message", self.names[cid], {"from": "system", "message": text})
        for cid in self.cast:
            await self._react(cid, "celebrate")

    async def _part_if_overlapping(self) -> None:
        """Creative: the two girls inside each other for a few seconds (after a
        restart they log in where they stood, a flight got blocked): the one
        who is not the camera flies a few blocks aside, still in view."""
        now = time.time()
        if len(self.cast) < 2 or not all(
            c in self._pos and now - self._seen_at.get(c, 0) < 10 for c in self.cast[:2]
        ):
            return
        eyes = self.cam_focus if self.cam_focus in self.cast else self.cast[0]
        other = self._friend(eyes)
        (ex, ey, ez), (ox, oy, oz) = self._pos[eyes], self._pos[other]
        if ((ex - ox) ** 2 + (ez - oz) ** 2) ** 0.5 >= 1.5 or abs(ey - oy) >= 2.0:
            self._close_since = 0.0
            return
        self._close_since = self._close_since or now
        if now - self._close_since < 2.0 or now - self._parted_at < 8.0:
            return
        self._parted_at = now
        dx, dz = ox - ex, oz - ez
        length = (dx * dx + dz * dz) ** 0.5
        if length < 0.3:  # exactly on top of each other: step to the side
            dx, dz, length = (1.0, 0.0, 1.0) if self.build_focus is None else (
                -(self.build_focus[2] - ez), self.build_focus[0] - ex, 1.0)
            length = (dx * dx + dz * dz) ** 0.5 or 1.0
        tx, tz = ex + dx / length * PART_DISTANCE, ez + dz / length * PART_DISTANCE
        ty = ey + 0.5
        lx, ly, lz = self.build_focus or (ex, ey + 1.6, ez)
        self._target[other] = (tx, ty, tz)
        await self._command(other, f"!flyTo({tx:.1f}, {ty:.1f}, {tz:.1f}, {lx:.1f}, {ly:.1f}, {lz:.1f}, -60)")
        logger.info(f"Minecraft: {self.names[other]} was inside {self.names[eyes]}'s view, moved {PART_DISTANCE:.0f} blocks aside")

    async def _keep_together(self) -> None:
        """Far apart for a minute: the one who wandered off walks back."""
        now = time.time()
        if len(self.cast) < 2 or not all(
            c in self._pos and now - self._seen_at.get(c, 0) < 10 for c in self.cast[:2]
        ):
            return  # someone is not in the world right now: nothing to do
        a, b = self.cast[0], self.cast[1]
        (ax, _ay, az), (bx, _by, bz) = self._pos[a], self._pos[b]
        far = ((ax - bx) ** 2 + (az - bz) ** 2) ** 0.5 > TOGETHER_BLOCKS
        now = time.time()
        if not far:
            self._apart_since = 0.0
            return
        self._apart_since = self._apart_since or now
        if now - self._apart_since < 20 or now - self._nudged_at < 45:
            return
        self._nudged_at = now
        # The one who spoke least recently moves (most likely not on camera).
        mover, friend = (a, b) if self._spoke_at.get(a, 0) <= self._spoke_at.get(b, 0) else (b, a)
        # Walking back across a desert took Luna the whole stream; a teleport
        # takes one second and keeps the show on the two of them together.
        if await bring_into_view(self.names[mover], self.names[friend]):
            logger.info(f"Minecraft: {self.names[mover]} teleported to {self.names[friend]}")
            self._anchor.pop(mover, None)
            await self._reload_view(mover)
            await self.link.emit(
                "send-message",
                self.names[mover],
                {"from": "system", "message": f"You were just teleported next to {self.names[friend]}. Say something to her and keep working together."},
            )
            return
        await self.link.emit(
            "send-message",
            self.names[mover],
            {
                "from": "system",
                "message": (
                    f"You drifted far away from {self.names[friend]}. Go back to her now with "
                    f'!goToPlayer("{self.names[friend]}", 3), say something to her, and keep working together.'
                ),
            },
        )
        logger.info(f"Minecraft: {self.names[mover]} walks back to {self.names[friend]}")

    # ------------------------------------------------------------ chat to bots
    async def _deliver_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if not self.outbox or not self.link.connected.is_set():
                continue
            now = time.time()
            waiting = [item for item in self.outbox if now - item["at"] <= 90]
            self.outbox.clear()
            for cid in dict.fromkeys(item["to"] for item in waiting):
                mine = [item for item in waiting if item["to"] == cid]
                if now - self.last_to_bot.get(cid, 0) < BOT_GAP:
                    self.outbox.extend(mine)
                    continue
                self.last_to_bot[cid] = now
                if len(mine) == 1:
                    sender, text = mine[0]["from"], mine[0]["text"]
                else:  # a busy chat: one message (one AI call) with all of them
                    sender = "YouTube chat"
                    text = " | ".join(f"{m['from']}: {m['text']}" for m in mine[-BOT_BATCH:])
                ok = await self.link.emit("send-message", self.names[cid], {"from": sender, "message": text})
                if ok:
                    logger.info(f"Minecraft chat {sender} -> {self.names[cid]}: {text[:60]}")
                else:
                    self.outbox.extend(mine)

    # ------------------------------------------------------------ voice and body
    async def _speak_loop(self) -> None:
        while True:
            if self.viewer_lines:  # answers to chat always go first
                line = self.viewer_lines.popleft()
                if time.time() - line["at"] <= VIEWER_LINE_MAX_AGE:
                    # the rest of an answer: no second reaction, she just goes on
                    await self._say(line["who"], line["text"], "" if line.get("cont") else pick_mood(line["text"]))
                continue
            # An answer is being written (or about to be): chatter waits, at most 5 s.
            coming = self._streaming or (self.chat_queue and self._answer_task and not self._answer_task.done())
            if coming:
                self._held_since = self._held_since or time.time()
                if time.time() - self._held_since < ANSWER_HOLD:
                    await asyncio.sleep(0.05)
                    continue
            else:
                self._held_since = 0.0
            if not self.lines:
                self.line_ready.clear()
                await self.line_ready.wait()
                continue
            line = self.lines.popleft()
            if time.time() - line["at"] > LINE_MAX_AGE:
                continue
            await self._say(line["who"], line["text"], pick_mood(line["text"]), short=True)

    async def _quick_reply(self, cid: str, author: str, text: str, more: Optional[list[tuple[str, str]]] = None) -> str:
        started = time.time()
        parts: list[str] = []
        first_at = 0.0
        self._streaming += 1
        try:
            async for piece in self.reply_pieces(cid, author, text, more):
                if not piece:
                    continue
                first_at = first_at or time.time()
                self.viewer_lines.append({"who": cid, "text": piece, "at": time.time(), "cont": bool(parts)})
                parts.append(piece)
                self.line_ready.set()
        finally:
            self._streaming -= 1
            self.line_ready.set()
        line = " ".join(parts)
        if not line:
            return ""
        who = ", ".join([author] + [a for a, _t in more or []])
        self._remember(f'{self.names[cid]} told {who}: "{line[:110]}"')
        logger.info(
            f"Minecraft: {self.names[cid]} answers {who} (speaking after {first_at - started:.1f}s, "
            f"all {time.time() - started:.1f}s): {line}"
        )
        return line

    async def reply_pieces(self, cid: str, author: str, text: str, more: Optional[list[tuple[str, str]]] = None):
        """Her answer as it is written: the first sentence as soon as it is
        there (she starts speaking), then the rest in one piece (one more
        short pause at most, like now: no choppy word by word speech)."""
        more = more or []
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            yield await self.reply_text(cid, author, text, more)
            return
        system, user = self._reply_prompt(cid, author, text, more)
        buffer, sent_first, said_any = "", False, False
        try:
            if self._llm is None:
                from openai import AsyncOpenAI

                self._llm = AsyncOpenAI(api_key=key, timeout=8.0, max_retries=1)
            stream = await self._llm.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=110 if more else 70,
                temperature=0.9,
                stream=True,
            )
            try:
                from ..vr_agent.usage import usage

                usage.record_llm("minecraft chat answer")
            except Exception:
                pass
            async for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if not delta:
                    continue
                buffer += delta
                if not sent_first:
                    match = FIRST_SENTENCE.search(buffer)
                    if match:
                        piece = clean_reply(buffer[: match.end()])
                        buffer = buffer[match.end():]
                        if re.search(r"[A-Za-z]", piece):
                            sent_first = said_any = True
                            yield piece
        except Exception as exc:
            logger.warning(f"Minecraft: quick answer failed ({exc}), short answer instead")
        rest = clean_reply(buffer)
        if re.search(r"[A-Za-z]", rest):
            said_any = True
            yield rest
        if not said_any:
            yield self._fallback_reply("", author, more)

    def _sight_line(self, cid: str) -> str:
        mine = self.eyes.describe_for(cid)
        theirs = self.eyes.describe_for(self._friend(cid))
        out = f"What YOU see in the game right now: {mine} " if mine else ""
        if theirs and self._friend(cid) != cid:
            out += f"What {self.names[self._friend(cid)]} sees: {theirs} "
        return out

    def _remember(self, text: str) -> None:
        """What just happened, shared by both girls (answers read it)."""
        text = " ".join(str(text).split())[:160]
        if text:
            self.memory.append((time.time(), text))

    def _memory_text(self) -> str:
        now = time.time()
        return " | ".join(t for at, t in list(self.memory)[-MEMORY_LINES:] if now - at < MEMORY_SECONDS)

    def request_build(self, request: str, who: str, viewer: bool = False) -> str:
        """A free build, one at a time (viewers' before the girls' own).
        Returns what to tell the girls."""
        if self._free_waiting >= 20:
            return " (Many builds are waiting already: it comes after them, say so cheerfully.)"
        self._free_waiting += 1
        if viewer:
            self._viewer_builds += 1
        _soon(self._free_build(request, who, viewer))
        if self._free_busy:
            return " (You build it right after the current build, by hand: no command needed.)"
        return " (You two are building it right now, by hand, block by block: no command needed. Never say it is done before it is.)"

    async def _free_build(self, request: str, who: str, viewer: bool = False) -> None:
        from . import minecraft_freebuild as fb

        try:
            async with self._free_lock:
                self._free_waiting -= 1
                self._free_busy = True
                await self._free_build_now(fb, request, who, viewer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Minecraft: free build failed: {exc}")
        finally:
            self._free_busy = False
            if viewer:
                self._viewer_builds = max(0, self._viewer_builds - 1)

    async def _free_build_now(self, fb: Any, request: str, who: str, viewer: bool = False) -> None:
        base = self.projects.state.get("base")
        here = [c for c in self.cast if time.time() - self._seen_at.get(c, 0) < 15 and c in self._pos]
        if not base or not here or not await self.projects.ensure_loaded():
            return
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if key and self._llm is None:
            from openai import AsyncOpenAI

            self._llm = AsyncOpenAI(api_key=key, timeout=15.0, max_retries=1)
        plan = await fb.design(self._llm, self.model, request, who)
        if plan is None:
            for c in here:
                await self.link.emit("send-message", self.names[c], {"from": "system", "message": (
                    f"The build {who} asked for could not be planned right now. Say so honestly and ask for something simpler.")})
            return
        # Its own plot, outside the area of the big projects (the last one was
        # built inside the network wall, where nobody could see it).
        forward = (0, 1)
        spots = dict(fb.blocks_of(plan))
        x1, x2, z1, z2, top = fb.footprint_of(spots)
        index = int(self.projects.state.get("plots", 0))
        px, pz = PLOTS_X[index % len(PLOTS_X)], PLOTS_Z + (index // len(PLOTS_X)) * PLOT_STEP
        self.projects.state["plots"] = index + 1
        self.projects._save()
        gx, gz = int(base[0]) + px, int(base[2]) + pz
        ground = await ground_height(gx, gz + (z2 - z1) // 2, int(base[1]))
        origin = (gx, ground, gz - min(z1, 0))  # the whole build behind the plot's front line (a dragon's snout)
        await self._level_plot(origin, (x1, x2, z1, z2, top), digs=any(b == "air" for b in spots.values()))
        steps = fb.pieces(plan, origin, tuple(base), forward)
        title = plan["title"]
        # both fly to the plot first, a little in front of it and above
        front = (px, ground - base[1] + 6, pz - 8)
        middle = (px, ground - base[1] + 2, pz + (z2 - z1) // 2)
        flights = await asyncio.gather(*(self._fly(c, front, middle) for c in here))
        await asyncio.gather(*(self._arrive(c, front, middle, t) for c, t in zip(here, flights)))
        total = sum(len(lay(c)) for st in steps for c in st["commands"]) or 1
        logger.info(f"Minecraft: building '{title}' for {who} by hand ({len(steps)} pieces)")
        self._remember(f"{' and '.join(self.names[c] for c in here)} started building {title} for {who}")
        await self._push({"kind": "project", "title": f"🔨 {title}", "step": f"for {who}", "progress": 0.0, "steps": []})
        for c in here:
            await self.link.emit("send-message", self.names[c], {"from": "system", "message": (
                f"You and your friend are building {title} for {who} right now, by hand, block by block. "
                "Talk about it while you build; it is NOT finished until you are told.")})
        done = 0
        for step in steps:
            if not viewer and self._viewer_builds:
                logger.info(f"Minecraft: '{title}' (their own idea) paused: a viewer build comes first")
                self._remember(f"they stopped {title} because chat asked for something")
                await self._push({"kind": "project", **self.projects.view()})
                return
            done += await self._lay_step(step, here)
            await self._push({"kind": "project", "title": f"🔨 {title}", "step": f"for {who}",
                              "progress": round(done / total, 3), "steps": []})
        await self._push({"kind": "project_done", "title": title, "step": title})
        self._remember(f"{title} for {who} is finished")
        CLIPS.mark("built", f"{title} for {who} finished", 3.0)
        self._exclaim(random.choice(here), "done")
        for c in here:
            await self.link.emit("send-message", self.names[c], {"from": "system", "message": (
                f"{title} for {who} is finished! Show it off in one line.")})
        await self._push({"kind": "project", **self.projects.view()})

    async def _keep_daylight(self) -> None:
        """Always day and clear (a dark night stream is hard to watch), set
        again every 10 minutes in case the server restarted.
        VR_MINECRAFT_DAYLIGHT=0 turns it off.

        Also every 10 minutes: the server's player movement check off. Under
        lag it decided Mika "moved too quickly" and pulled her back, she flew
        on, it pulled her back again: the stream (her eyes) jumped left and
        right for hours."""
        if time.time() - self._daylight_at < 600:
            return
        self._daylight_at = time.time()
        for command in ("gamerule disablePlayerMovementCheck true", "gamerule disableElytraMovementCheck true"):
            await rcon_command(command)
        if os.environ.get("VR_MINECRAFT_DAYLIGHT", "1").strip().lower() in ("0", "false", "off", "no"):
            return
        for command in ("time set day", "gamerule doDaylightCycle false", "weather clear",
                        "gamerule doWeatherCycle false"):
            await rcon_command(command)
        logger.info("Minecraft: daytime and clear weather locked")

    async def _lay_step(self, step: dict[str, Any], here: list[str]) -> int:
        """One piece, shared by whoever is free (by hand). Returns its run count."""
        base = self.projects.state["base"]
        builders = [c for c in here if time.time() - self._chose_at.get(c, 0) > CHOICE_HOLD] or here[:1]
        self.build_focus = (base[0] + step["focus"][0], base[1] + step["focus"][1], base[2] + step["focus"][2])
        runs = [r for c in step["commands"] for r in lay(c)]
        half = (len(runs) + 1) // 2
        shares = [runs[:half], runs[half:]] if len(builders) > 1 and len(runs) > 1 else [runs]
        await asyncio.gather(*(self._lay_runs(c, share, step) for c, share in zip(builders, shares)))
        if self._slow:
            await asyncio.sleep(3.0)  # the server cannot keep up: give it a breath
        return len(runs)

    async def _kingdom_step(self) -> bool:
        """The 10 hour build: the next lot of the Kingdom, piece by piece, by
        hand. Stops between pieces when chat asks for a build (that comes
        first) and goes on where it stopped. False when there is nothing to do."""
        from . import minecraft_freebuild as fb
        from . import minecraft_kingdom as kd

        if self._kingdom is None:
            self._kingdom = kd.lots()
        state = self.projects.state.setdefault("kingdom", {"lot": 0, "piece": 0})
        if int(state.get("lot", 0)) >= len(self._kingdom):
            return False
        base = self.projects.state["base"]
        here = [c for c in self.cast if time.time() - self._seen_at.get(c, 0) < 15 and c in self._pos]
        if not here:
            return False
        lot = self._kingdom[int(state["lot"])]
        spots = kd.lot_blocks(lot)
        x1, x2, z1, z2, top = fb.footprint_of(spots)
        gx, gz = int(base[0]) + lot["x"], int(base[2]) + lot["z"]
        if int(state.get("piece", 0)) == 0 or "ground" not in state:
            state["ground"] = await ground_height(gx, gz + (z2 - z1) // 2, int(base[1]))
            await self._level_plot((gx, state["ground"], gz - min(z1, 0)), (x1, x2, z1, z2, top), digs=False)
            self.projects._save()
        ground = int(state["ground"])
        origin = (gx, ground, gz - min(z1, 0))
        steps = fb.pieces_of(fb.ordered(spots), origin, tuple(base), (0, 1))
        n, total = int(state["lot"]), len(self._kingdom)
        panel = {"kind": "project", "title": "🏰 The Kingdom", "steps": []}
        if int(state.get("piece", 0)) == 0:
            front = (lot["x"], ground - base[1] + 6, lot["z"] - min(z1, 0) - 8)
            middle = (lot["x"], ground - base[1] + 2, lot["z"] - min(z1, 0) + (z2 - z1) // 2)
            flights = await asyncio.gather(*(self._fly(c, front, middle) for c in here))
            await asyncio.gather(*(self._arrive(c, front, middle, t) for c, t in zip(here, flights)))
            logger.info(f"Minecraft: the Kingdom, lot {n + 1} of {total}: {lot['title']} ({len(spots)} blocks)")
            self._remember(f"they started {lot['title']} for the Kingdom (lot {n + 1} of {total})")
            await self._tell_one(
                f"Kingdom time: you two now build {lot['title']}, lot {n + 1} of {total} of your kingdom, by hand. "
                "Chat comes first whenever they ask for something; then you come back to it.")
        for i in range(int(state.get("piece", 0)), len(steps)):
            if self._free_busy or self._free_waiting:
                return True  # chat asked for something: that first, this lot waits where it is
            await self._lay_step(steps[i], here)
            state["piece"] = i + 1
            self.projects._save()
            await self._push({**panel, "step": f"{lot['title']} · lot {n + 1} of {total}",
                              "progress": round((n + (i + 1) / len(steps)) / total, 4)})
        state.update({"lot": n + 1, "piece": 0})
        state.pop("ground", None)
        self.projects._save()
        self._remember(f"{lot['title']} of the Kingdom is finished (lot {n + 1} of {total})")
        if lot["template"] in ("castle", "dragon"):
            CLIPS.mark("built", f"Kingdom: {lot['title']} finished", 2.0)
        self._exclaim(random.choice(here), "done")
        return True

    async def _level_plot(self, origin: tuple[int, int, int], box: tuple[int, int, int, int, int], digs: bool) -> None:
        """Get the plot ready (this part is instant, like clearing a site): the
        space above the ground is emptied (sand hills, trees) and the ground
        under the build is made flat grass. The building itself is by hand."""
        ox, oy, oz = origin
        x1, x2, z1, z2, top = box
        a, b = ox + x1 - 1, ox + x2 + 1
        c, d = oz + z1 - 1, oz + z2 + 1
        for start in range(c, d + 1, 8):  # in slices: one fill may change at most 32768 blocks
            end = min(d, start + 7)
            await rcon_command(f"fill {a} {oy} {start} {b} {oy + max(top, 4) + 3} {end} minecraft:air", reply=True)
        if not digs:
            await rcon_command(f"fill {a} {oy - 1} {c} {b} {oy - 1} {d} minecraft:grass_block", reply=True)
            await rcon_command(f"fill {a} {oy - 3} {c} {b} {oy - 2} {d} minecraft:dirt", reply=True)

    def _camera_girl(self) -> str:
        return self.cam_focus if self.cam_focus in self.cast else self.cast[0]

    async def _show_place(self, place: str, who: str) -> None:
        """'I want to see the neural network': the camera girl (her eyes are
        the stream) flies there right away and stays a while."""
        cid = self._camera_girl()
        base = self.projects.state.get("base")
        if not base:
            return
        if place == "kingdom":
            from .minecraft_kingdom import CENTER

            view, focus = (CENTER[0], 40, CENTER[1] - 70), (CENTER[0], 0, CENTER[1])
        else:
            view, focus = PLACES.get(place, PLACES["base"])
        self._chose_at[cid] = time.time() + SHOW_HOLD  # the build waits while chat looks
        self._remember(f"{self.names[cid]} flew to the {place} because {who} wanted to see it")
        logger.info(f"Minecraft: {who} wants to see the {place}: {self.names[cid]} flies there")
        await self._arrive(cid, view, focus, await self._fly(cid, view, focus))

    async def _unstick(self, who: str) -> None:
        """Chat says she is stuck or glitching: believe it. Both stop what they
        do (a flight fighting something, a hung action), the camera girl is
        lifted two blocks into free air, your camera is attached to her again
        and building goes on."""
        now = time.time()
        if now - self._unstuck_at < UNSTICK_GAP:
            return
        self._unstuck_at = now
        cid = self._camera_girl()
        name = self.names[cid]
        logger.warning(f"Minecraft: chat said stuck ({who}): stopping both, lifting {name}, camera attached again")
        for c in self.cast:
            self._last_flight.pop(c, None)
            await self._command(c, "!stop")
        await rcon_command(f"execute as {name} at @s if block ~ ~2 ~ air if block ~ ~3 ~ air run tp @s ~ ~2 ~")
        if self.camera_on and self.camera_style not in ("director", "behind"):
            await rcon_command(f"gamemode spectator {self.camera_player}")
            await rcon_command(f"tp {self.camera_player} {name}")
            await asyncio.sleep(1.0)
            await self._spectate()
        self._build_event.set()  # building goes on from where she is now

    def _fresh_idea(self, idea: str) -> str:
        """Her own build idea, unless she built that kind of thing lately
        (six floating gardens in one stream): then something new."""
        def words(text: str) -> set[str]:
            return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 4 and w not in IDEA_STOPWORDS}

        mine = words(idea)
        used = set().union(*(words(old) for old in self._own_ideas)) if self._own_ideas else set()
        if mine & used:
            options = [i for i in FRESH_IDEAS if not (words(i) & used)] or list(FRESH_IDEAS)
            idea = random.choice(options)
        self._own_ideas.append(idea)
        return idea

    async def _step_back(self, who: str) -> None:
        """'go a little further, I want to see it from afar': back and up."""
        cid = self._camera_girl()
        base = self.projects.state.get("base")
        here = self._pos.get(cid)
        if not base or not here:
            return
        fx, fz = self._facing(cid)
        look = self.build_focus or (here[0] + fx * 10, here[1], here[2] + fz * 10)
        x, y, z = here[0] - fx * 12, here[1] + 8, here[2] - fz * 12
        view = (x - base[0], y - base[1], z - base[2])
        focus = (look[0] - base[0], look[1] - base[1], look[2] - base[2])
        self._chose_at[cid] = time.time() + SHOW_HOLD
        logger.info(f"Minecraft: {who} wants a wider view: {self.names[cid]} backs up")
        await self._arrive(cid, view, focus, await self._fly(cid, view, focus))

    def _exclaim(self, cid: str, kind: str, info: Optional[dict[str, Any]] = None) -> None:
        """A loud, quick reaction at a big moment, said by the engine (no AI call)."""
        now = time.time()
        if kind in CLIP_WEIGHT:  # a moment worth a short clip, said out loud or not
            what = {"done": "piece built", "wrong": "network guessed wrong"}[kind]
            if info:
                what += f" ({info.get('guess')} for a {info.get('digit')})"
            self._remember(f"{self.names.get(cid, cid)}: {what}")
            CLIPS.mark(kind, f"{self.names.get(cid, cid)}: {what}", CLIP_WEIGHT[kind])
        if now - self._exclaimed_at < EXCLAIM_GAP or cid not in self.names:
            return
        self._exclaimed_at = now
        text = random.choice(EXCLAIM[kind]).format(**(info or {}))
        self._pop(cid, text, {"done": "win", "won": "win", "wrong": "fail"}.get(kind, "wow"))
        self.lines.appendleft({"who": cid, "text": text, "at": now})
        self.line_ready.set()

    def _pop(self, cid: str, text: str, tone: str = "wow") -> None:
        """A big comic word next to her for a second (ARGH!!!, YESSS!)."""
        now = time.time()
        word = POP_WORD.match(text.strip())
        if not word or now - self._popped_at < POP_GAP:
            return
        self._popped_at = now
        shout = word.group(0).strip(" ,").upper()
        _soon(self._push({"kind": "pop", "who": cid, "text": shout[:16], "tone": tone}))

    def _count(self, blocks: int = 0, winner: str = "") -> None:
        """The counters on screen: blocks laid by hand, race wins."""
        self.stats["blocks"] = int(self.stats.get("blocks", 0)) + blocks
        if winner:
            wins = self.stats.setdefault("wins", {})
            wins[winner] = int(wins.get(winner, 0)) + 1
        now = time.time()
        if winner or now - self._stats_sent > 1.0:  # at most once a second while building
            self._stats_sent = now
            _soon(self._push({"kind": "stats", **self.stats, "names": self.names}))

    def _kick_answers(self) -> None:
        if self._answer_task is None or self._answer_task.done():
            self._answer_task = asyncio.create_task(self._answer_loop(), name="mc-answers")

    def _next_batch(self) -> list[dict[str, Any]]:
        """The comments for the next spoken answer: first-time viewers and
        whoever waited longest first, up to ANSWER_BATCH for the same girl.
        Comments older than CHAT_MAX_AGE are dropped (the moment has passed)."""
        now = time.time()
        fresh = [c for c in self.chat_queue if now - c["at"] <= CHAT_MAX_AGE]
        self.chat_queue.clear()
        if not fresh:
            return []
        fresh.sort(key=lambda c: c["at"] - (NEW_VIEWER_HEAD_START if c["first"] else 0))
        who = fresh[0]["who"]
        batch = [c for c in fresh if c["who"] == who][:ANSWER_BATCH]
        self.chat_queue.extend(c for c in fresh if c not in batch)
        return batch

    async def _answer_loop(self) -> None:
        """One answer at a time, however busy chat gets: each AI call answers
        up to three viewers, and nothing waits behind a pile of old answers."""
        while self.chat_queue:
            waited = 0.0
            while len(self.viewer_lines) >= 2 and waited < 30:  # she still has answers to speak: let her catch up
                await asyncio.sleep(0.5)
                waited += 0.5
                now = time.time()
                while self.viewer_lines and now - self.viewer_lines[0]["at"] > VIEWER_LINE_MAX_AGE:
                    self.viewer_lines.popleft()  # never spoken (speech stuck): too old now
            batch = self._next_batch()
            if not batch:
                return
            for c in batch:
                self._answered.add(c["author"])
            for c in batch:  # "what do you see?": the fresh picture first (a few seconds at most)
                if c.get("eyes") is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(c["eyes"]), timeout=EYES_WAIT)
                    except Exception:
                        pass
            first, rest = batch[0], batch[1:]
            line = ""
            try:
                line = await self._quick_reply(first["who"], first["author"], first["text"],
                                               [(c["author"], c["text"]) for c in rest])
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning(f"Minecraft: chat answer failed: {exc}")
            # Her bot hears the comments AND what she said out loud, so what
            # she does next matches her words (one memory, not two minds).
            said = f' (You already answered out loud: "{line[:160]}".' if line else " (You already answered out loud."
            for c in batch:
                self.outbox.append({
                    "to": c["who"], "from": c["author"], "at": time.time(),
                    "text": (strip_emoji(c["text"]) or c["text"]) + said + (
                        " Do not greet again: if this asks for something, do it now with a command, "
                        "otherwise keep working on your goal.)"),
                })

    def _reply_prompt(self, cid: str, author: str, text: str, more: list[tuple[str, str]]) -> tuple[str, str]:
        """The system prompt and the chat lines for her spoken answer."""
        if more:
            ask = (f"{len(more) + 1} viewers wrote in the live chat. Answer all of them together out loud in two or "
                   "three short sentences, under 40 words, saying each name once.")
            user = "\n".join(f"{a} wrote: {t}" for a, t in [(author, text), *more])
        else:
            ask = ("A viewer wrote in the live chat. Answer them out loud in one or two short sentences, "
                   "under 25 words. Say their name once.")
            user = f"{author} wrote: {text}"
        name = self.names[cid]
        friend = self.names[self._friend(cid)]
        doing = str((self.hud.get(cid) or {}).get("doing") or "playing").replace("action:", "")
        view = self.projects.view()
        persona = ""
        prof = self.session.room.get(cid)
        if prof is not None:
            persona = re.sub(r"\s+", " ", getattr(prof, "persona", "") or "").strip()[:300]
        regulars = " ".join(self.viewers.describe(a) for a in dict.fromkeys([author, *(a for a, _t in more)]))
        system = (
            f"You are {name}. {persona} You are live on YouTube playing {'creative' if self.creative else 'survival'} Minecraft with {friend}. "
            f"Right now you are: {doing}. Team project: {strip_emoji(view.get('title', ''))}, working on {view.get('step', '')}. "
            f"{self._sight_line(cid)}"
            f"What just happened on stream (you and {friend} share this, never contradict it): "
            f"{self._memory_text() or 'the stream just started'}. {self.net_show.describe()} "
            f"{regulars} "
            f"{SYSTEM_FACTS if self.creative else ''} "
            f"{CAN_DO if self.creative else ''}"
            f"{ask} "
            + PERSONALITY + roast() +
            "If they ask you to do something, say yes and that you are doing it right now: never refuse, never stall, "
            "never ask if they are sure, never say it is too big or too hard (roasting them while you do it is fine). "
            "Only say things that are true about the game and the stream; when you do not know, joke about it "
            "instead of making something up. "
            "Plain spoken words only: no emojis, no emoticons, no symbols, no hashtags, no commands, no quotes."
        )
        return system, user

    async def reply_text(self, cid: str, author: str, text: str, more: Optional[list[tuple[str, str]]] = None) -> str:
        """One or two short spoken sentences from her to a viewer (or a few at once)."""
        more = more or []
        system, user = self._reply_prompt(cid, author, text, more)
        reply = ""
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if key:
            try:
                if self._llm is None:
                    from openai import AsyncOpenAI

                    self._llm = AsyncOpenAI(api_key=key, timeout=8.0, max_retries=1)
                response = await self._llm.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=110 if more else 70,
                    temperature=0.8,
                )
                reply = response.choices[0].message.content or ""
                try:
                    from ..vr_agent.usage import usage

                    usage.record_llm("minecraft chat answer")
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(f"Minecraft: quick answer failed ({exc}), short answer instead")
        return self._fallback_reply(clean_reply(reply), author, more)

    def _fallback_reply(self, reply: str, author: str, more: list[tuple[str, str]]) -> str:
        if not re.search(r"[A-Za-z]", reply) and more:
            names = [author] + [a for a, _t in more]
            reply = f"Hi {', '.join(names[:-1])} and {names[-1]}! I see you all, one second!"
        if not re.search(r"[A-Za-z]", reply):
            reply = random.choice(
                (
                    f"Hi {author}! I see you, give me a second!",
                    f"Oh, {author}! Good one, hold on!",
                    f"{author}, I hear you! Let me see what I can do.",
                )
            )
        return reply[:320 if more else 220]

    async def _say(self, cid: str, text: str, mood: str = "", short: bool = False) -> None:
        await self._wait_ready()
        if mood:
            await self._react(cid, mood)
        self.speaking = cid
        self._spoke_at[cid] = time.time()
        await self._camera_to(cid)
        self.said.append(f"{self.names[cid]}: {text[:120]}")
        try:
            parts = chunks(text)
            for i, chunk in enumerate(parts[:2] if short else parts):
                if short and i and self.viewer_lines:
                    break  # a viewer is waiting: the rest of the chatter can go
                await self._push({"kind": "say", "who": cid, "text": chunk})
                try:
                    spoken = await self.session.speech.say(cid, chunk)
                except Exception as exc:
                    logger.warning(f"Minecraft: speech failed: {exc}")
                    spoken = False
                if not spoken:
                    await asyncio.sleep(min(6.0, 1.5 + len(chunk) * 0.05))
        finally:
            self.speaking = ""
            await self._push({"kind": "said", "who": cid})

    def _actions(self, cid: str, mood: str = "") -> list[str]:
        profile = self.session.room.get(cid)
        if not profile:
            return []
        if mood:
            return list((profile.reactions or {}).get(mood) or ())
        pool: list[str] = []
        for actions in (profile.reactions or {}).values():
            pool.extend(actions)
        pool.extend(a for a in (profile.emotions or {}).values() if a)
        return sorted(set(pool))

    async def _react(self, cid: str, mood: str) -> None:
        choices = self._actions(cid, mood)
        if not choices:
            return
        try:
            await self.session.push(self.session.play(cid, random.choice(choices)))
        except Exception:
            pass

    async def _fidget_loop(self) -> None:
        """Nobody stands like a log: a small move every 9 to 15 seconds each."""
        next_at = {cid: time.time() + random.uniform(4, 10) for cid in self.cast}
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            for cid in self.cast:
                if now < next_at[cid]:
                    continue
                next_at[cid] = now + random.uniform(9, 15)
                if cid == self.speaking:
                    continue
                choices = self._actions(cid)
                if choices:
                    try:
                        await self.session.push(self.session.play(cid, random.choice(choices)))
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# setup checks and process files
# ---------------------------------------------------------------------------
# Plots for builds chat asks for: south of the big projects (which end 32
# blocks south of the base), three in a row, then the next row.
PLOTS_X = (-30, 15, 60)  # 45 apart: a giant dragon's wings fit
PLOTS_Z = 44
PLOT_STEP = 55


async def ground_height(x: int, z: int, near: int) -> int:
    """The first free height above the ground at x, z, searched from near+30
    down (trees do not count as ground). Silent: a test, nothing in chat."""
    for y in range(near + 30, near - 30, -1):
        reply = await rcon_command(
            f"execute unless block {x} {y} {z} minecraft:air unless block {x} {y} {z} #minecraft:leaves "
            f"unless block {x} {y} {z} #minecraft:logs", reply=True)
        if not isinstance(reply, str):
            return near
        if "passed" in reply.lower():
            return y + 1
    return near


def _soon(coro: Any) -> None:
    """Send this to the Stage when there is a running loop (tests: dropped)."""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


def _numbers(reply: Any) -> list[float]:
    """'Mika has the following entity data: [1.5d, 64.0d, 2.3d]' -> [1.5, 64.0, 2.3]."""
    if not isinstance(reply, str) or ":" not in reply:
        return []
    return [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?(?:E-?\d+)?", reply.split(":", 1)[1])]


def clean_reply(text: str) -> str:
    return strip_emoji(COMMAND_RE.sub(" ", text or "")).strip().strip('"').strip()


class ViewerMemory:
    """The viewers they know, kept between streams: how many streams they
    came to and the last things they said. One small JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        self.here: set[str] = set()
        self._saved = time.time()  # written at most every 30 s, and when the mode stops
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}

    def saw(self, name: str, text: str) -> bool:
        """Remember a message. True when it is their first one this stream."""
        now = time.time()
        entry = self.data.setdefault(name, {"streams": 0, "messages": 0, "first": now, "said": []})
        first = name not in self.here
        if first:
            self.here.add(name)
            entry["streams"] = int(entry.get("streams", 0)) + 1
        entry["messages"] = int(entry.get("messages", 0)) + 1
        entry["last"] = now
        entry["said"] = (list(entry.get("said") or []) + [text[:100]])[-3:]
        if now - self._saved > 30:
            self.save()
        return first

    def describe(self, name: str) -> str:
        entry = self.data.get(name) or {}
        streams = int(entry.get("streams", 0))
        older = [t for t in entry.get("said") or []][:-1]
        if streams <= 1:
            return f"{name} is new here: welcome them." if name in self.here and int(entry.get("messages", 0)) <= 1 else ""
        text = f"{name} is a regular (their stream number {streams} with you)."
        if older:
            text += f' Earlier they said: "{older[-1][:80]}".'
        return text

    def save(self) -> None:
        self._saved = time.time()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if len(self.data) > 5000:  # only the most recent viewers
                keep = sorted(self.data.items(), key=lambda kv: -float(kv[1].get("last", 0)))[:5000]
                self.data = dict(keep)
            self.path.write_text(json.dumps(self.data, indent=1))
        except Exception as exc:
            logger.debug(f"Minecraft: viewers not saved: {exc}")


def setup_problem() -> str:
    if not (SERVER_DIR / "server.jar").exists():
        return "Minecraft server missing: run ./minecraft/setup.sh once"
    eula = SERVER_DIR / "eula.txt"
    if not eula.exists() or "eula=true" not in eula.read_text():
        return "Minecraft EULA not accepted yet: run ./minecraft/setup.sh once"
    if not (MINDCRAFT_DIR / "node_modules").exists():
        return "Mindcraft not installed: run ./minecraft/setup.sh once"
    if not os.environ.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY is missing in .env (the bots need it)"
    return ""


VIEWER_ORIGINAL = """    function botPosition () {
      const packet = { pos: bot.entity.position, yaw: bot.entity.yaw, addMesh: true }
      if (firstPerson) {
        packet.pitch = bot.entity.pitch
      }
      socket.emit('position', packet)
      worldView.updatePosition(bot.entity.position)
    }
"""
VIEWER_SMOOTH = """    // VR Agent: the camera turns smoothly instead of snapping with every head
    // turn, and looks down only half as far, so viewers keep the horizon.
    let camYaw = bot.entity.yaw
    let camPitch = bot.entity.pitch
    function botPosition () {
      worldView.updatePosition(bot.entity.position)
    }
    const smoothCamera = setInterval(() => {
      if (!bot.entity) return
      let d = bot.entity.yaw - camYaw
      d = Math.atan2(Math.sin(d), Math.cos(d))
      camYaw += d * 0.1
      camPitch += (bot.entity.pitch * 0.5 - camPitch) * 0.1
      const packet = { pos: bot.entity.position, yaw: camYaw, addMesh: true }
      if (firstPerson) packet.pitch = camPitch
      socket.emit('position', packet)
    }, 50)
    socket.on('disconnect', () => clearInterval(smoothCamera))
"""


# The viewer page itself: the camera used to jump to every position update
# (they come in bursts while the bot thinks), which looks like stop and go.
# Now it glides toward the latest position and turn on every drawn frame.
CAMERA_ORIGINAL = (
    'setFirstPersonCamera(t,e,i){if(t){let e=t.y+this.playerHeight;this.isSneaking&&(e-=.3),'
    'new r.Tween(this.camera.position).to({x:t.x,y:e,z:t.z},50).start()}this.camera.rotation.set(i,e,0,"ZYX")}'
)
CAMERA_GLIDE = (
    "setFirstPersonCamera(t,e,i){/*VR Agent glide*/const c=this.camera;"
    "if(t){let y=t.y+this.playerHeight;this.isSneaking&&(y-=.3);"
    "if(!this._ct)c.position.set(t.x,y,t.z);this._ct={x:t.x,y:y,z:t.z}}"
    'if(!this._cr)c.rotation.set(i,e,0,"ZYX");this._cr={p:i,y:e};'
    "if(!this._cl){this._cl=1;let last=performance.now();const step=(now)=>{"
    "const dt=Math.min(.1,(now-last)/1e3);last=now;const k=1-Math.exp(-dt*5);"
    "if(this._ct){c.position.x+=(this._ct.x-c.position.x)*k;c.position.y+=(this._ct.y-c.position.y)*k;"
    "c.position.z+=(this._ct.z-c.position.z)*k}"
    "let d=this._cr.y-c.rotation.y;d=Math.atan2(Math.sin(d),Math.cos(d));"
    'c.rotation.set(c.rotation.x+(this._cr.p-c.rotation.x)*k,c.rotation.y+d*k,0,"ZYX");'
    "requestAnimationFrame(step)};requestAnimationFrame(step)}}"
)


# Creative builders: the engine flies a girl to each spot she builds at (sent
# from DIRECTOR, so it runs without an AI call). She flies around blocks like
# a player, never through them: a way through the air is planned first (A*),
# and every step is checked against the blocks.
FLY_ANCHOR = """    {
        name: '!searchForBlock',"""
FLY_MARK = "    { // VR Agent: creative flight"
FLY_COMMANDS = FLY_MARK + """ for building on stream (v14)
        name: '!flyTo',
        description: 'Creative mode only: fly to x, y, z (over cruise height) and look at lx, ly, lz.',
        params: {
            'x': {type: 'float', description: 'x', domain: [-Infinity, Infinity]},
            'y': {type: 'float', description: 'y', domain: [-64, 320]},
            'z': {type: 'float', description: 'z', domain: [-Infinity, Infinity]},
            'lx': {type: 'float', description: 'look x', domain: [-Infinity, Infinity]},
            'ly': {type: 'float', description: 'look y', domain: [-64, 320]},
            'lz': {type: 'float', description: 'look z', domain: [-Infinity, Infinity]},
            'cruise': {type: 'float', description: 'fly over this height', domain: [-64, 320]}
        },
        perform: runAsAction(async (agent, x, y, z, lx, ly, lz, cruise) => {
            // Our own flight (mineflayer's flyTo hangs and cannot be stopped).
            // She flies AROUND blocks like a player: a way through the air is
            // planned first (A* over free spots), every step is checked
            // against the real block shapes, and she never moves into a
            // block. The head turns smoothly (the stream camera looks through
            // these eyes and a snap looks like lag).
            const bot = agent.bot;
            const Vec3 = bot.entity.position.constructor;
            if (bot.pathfinder) bot.pathfinder.stop();
            bot.clearControlStates();
            bot.creative.startFlying();
            const look = new Vec3(lx, ly, lz);
            // Speed and Slowness change how fast she flies (potions, the race)
            let pace = 1;
            try {
                const fx = bot.entity.effects || {};
                const reg = bot.registry.effectsByName || {};
                const level = (name) => {
                    const e = reg[name] || reg[name.toLowerCase()];
                    return e && fx[e.id] ? fx[e.id].amplifier + 1 : 0;
                };
                pace += 0.35 * level('Speed') - 0.25 * level('Slowness');
            } catch (e) { /* no effects known: normal speed */ }
            const FLY_STEP = 0.9 * Math.max(0.4, pace); // blocks per tick, 18 a second
            const MAX_TURN = 0.22; // radians per tick
            const HALF = 0.3; // half her width
            const TALL = 1.8;
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
            const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));
            const turn = async () => {
                const eye = bot.entity.position.offset(0, bot.entity.height || 1.62, 0);
                const d = look.minus(eye);
                const yaw = Math.atan2(-d.x, -d.z);
                const pitch = Math.atan2(d.y, Math.sqrt(d.x * d.x + d.z * d.z));
                const dy = wrap(yaw - bot.entity.yaw);
                const dp = pitch - bot.entity.pitch;
                const ny = bot.entity.yaw + Math.max(-MAX_TURN, Math.min(MAX_TURN, dy));
                const np = bot.entity.pitch + Math.max(-MAX_TURN, Math.min(MAX_TURN, dp));
                await bot.look(ny, np, true);
                return Math.abs(dy) < 0.02 && Math.abs(dp) < 0.02;
            };
            // Does her body fit with her feet at p? (real block shapes: slabs, fences, stairs)
            const fits = (p) => {
                for (let bx = Math.floor(p.x - HALF); bx <= Math.floor(p.x + HALF - 1e-6); bx++) {
                    for (let by = Math.floor(p.y - 0.5); by <= Math.floor(p.y + TALL - 1e-6); by++) {
                        for (let bz = Math.floor(p.z - HALF); bz <= Math.floor(p.z + HALF - 1e-6); bz++) {
                            const b = bot.blockAt(new Vec3(bx, by, bz));
                            if (!b || b.boundingBox !== 'block') continue;
                            const shapes = (b.shapes && b.shapes.length) ? b.shapes : [[0, 0, 0, 1, 1, 1]];
                            for (const s of shapes) {
                                if (p.x - HALF < bx + s[3] && p.x + HALF > bx + s[0] &&
                                    p.y < by + s[4] && p.y + TALL > by + s[1] &&
                                    p.z - HALF < bz + s[5] && p.z + HALF > bz + s[2]) return false;
                            }
                        }
                    }
                }
                return true;
            };
            const clear = (a, b) => {
                const d = b.minus(a);
                const n = Math.max(1, Math.ceil(d.norm() / 0.25));
                for (let i = 1; i <= n; i++) if (!fits(a.plus(d.scaled(i / n)))) return false;
                return true;
            };
            // A way through the air from `from` to `to`, as a few straight legs (null: none).
            const plan = (from, to) => {
                if (clear(from, to)) return [to];
                const key = (c) => c[0] + ',' + c[1] + ',' + c[2];
                const known = new Map();
                const free = (c) => {
                    const k = key(c);
                    let f = known.get(k);
                    if (f === undefined) { f = fits(new Vec3(c[0] + 0.5, c[1], c[2] + 0.5)); known.set(k, f); }
                    return f;
                };
                const start = [Math.floor(from.x), Math.floor(from.y), Math.floor(from.z)];
                let goal = [Math.floor(to.x), Math.floor(to.y), Math.floor(to.z)];
                let exact = fits(to);
                if (!free(goal)) { // the spot itself is taken: the nearest free one
                    let best = null;
                    let bd = Infinity;
                    for (let dx = -3; dx <= 3; dx++) for (let dy = -2; dy <= 3; dy++) for (let dz = -3; dz <= 3; dz++) {
                        const c = [goal[0] + dx, goal[1] + dy, goal[2] + dz];
                        const dd = dx * dx + dy * dy + dz * dz;
                        if (dd < bd && free(c)) { bd = dd; best = c; }
                    }
                    if (!best) return null;
                    goal = best;
                    exact = false;
                }
                const gk = key(goal);
                const h = (c) => Math.hypot(c[0] - goal[0], c[1] - goal[1], c[2] - goal[2]);
                const heap = [];
                const push = (f, c) => {
                    heap.push([f, c]);
                    let i = heap.length - 1;
                    while (i > 0) {
                        const up = (i - 1) >> 1;
                        if (heap[up][0] <= heap[i][0]) break;
                        [heap[up], heap[i]] = [heap[i], heap[up]];
                        i = up;
                    }
                };
                const pop = () => {
                    const top = heap[0];
                    const last = heap.pop();
                    if (heap.length) {
                        heap[0] = last;
                        let i = 0;
                        for (;;) {
                            const l = 2 * i + 1;
                            const r = l + 1;
                            let m = i;
                            if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
                            if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
                            if (m === i) break;
                            [heap[m], heap[i]] = [heap[i], heap[m]];
                            i = m;
                        }
                    }
                    return top;
                };
                const cost = new Map([[key(start), 0]]);
                const came = new Map();
                push(h(start), start);
                let found = false;
                for (let n = 0; heap.length && n < 15000; n++) {
                    const c = pop()[1];
                    const ck = key(c);
                    if (ck === gk) { found = true; break; }
                    const base = cost.get(ck);
                    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
                        if (!dx && !dy && !dz) continue;
                        const nb = [c[0] + dx, c[1] + dy, c[2] + dz];
                        if (!free(nb)) continue;
                        let ok = true; // no cutting corners: each straight part of a diagonal is free too
                        for (let m = 1; m < 7 && ok; m++) {
                            const ox = (m & 1) ? dx : 0;
                            const oy = (m & 2) ? dy : 0;
                            const oz = (m & 4) ? dz : 0;
                            if ((ox || oy || oz) && !(ox === dx && oy === dy && oz === dz)) ok = free([c[0] + ox, c[1] + oy, c[2] + oz]);
                        }
                        if (!ok) continue;
                        const nk = key(nb);
                        const g = base + Math.hypot(dx, dy, dz);
                        if (g < (cost.has(nk) ? cost.get(nk) : Infinity)) {
                            cost.set(nk, g);
                            came.set(nk, c);
                            push(g + 1.2 * h(nb), nb);
                        }
                    }
                }
                if (!found) return null;
                const cells = [];
                for (let k = gk, c = goal; k !== key(start); c = came.get(k), k = key(c)) {
                    cells.push(new Vec3(c[0] + 0.5, c[1], c[2] + 0.5));
                }
                cells.reverse();
                if (exact) cells.push(to);
                const legs = []; // only the corners: straight legs where the air is free
                let at = from;
                let i = 0;
                while (i < cells.length) {
                    let j = Math.min(cells.length - 1, i + 40);
                    while (j > i && !clear(at, cells[j])) j--;
                    legs.push(cells[j]);
                    at = cells[j];
                    i = j + 1;
                }
                return legs;
            };
            // The server can pull her back (lag, "moved too quickly"). Flying
            // on at once made it pull her back again and again: her eyes (the
            // stream) jumped back and forth. Now she waits for it, flies on
            // from where the server put her, and after a few pulls she stays.
            let pulls = 0;
            let pulledAt = 0;
            const onPull = () => { pulls++; pulledAt = Date.now(); };
            bot.on('forcedMove', onPull);
            try {
            const target = new Vec3(x, y, z);
            for (let attempt = 0; attempt < 3; attempt++) {
                const here = bot.entity.position.clone();
                let route = plan(here, target);
                if (!route && cruise > -50) { // no way found nearby: up over everything, across, down
                    const top = Math.max(here.y + 1, y + 3, cruise);
                    const a = plan(here, new Vec3(here.x, top, here.z));
                    const b = a && plan(new Vec3(here.x, top, here.z), new Vec3(x, top, z));
                    const c = b && plan(new Vec3(x, top, z), target);
                    if (a && b && c) route = a.concat(b, c);
                }
                if (!route) break;
                let blocked = false;
                for (const leg of route) {
                    for (let i = 0; i < 600; i++) {
                        if (bot.interrupt_code) return;
                        const delta = leg.minus(bot.entity.position);
                        const dist = delta.norm();
                        if (dist < 0.05) break;
                        const next = bot.entity.position.plus(delta.scaled(Math.min(FLY_STEP, dist) / dist));
                        // never into a block (one just placed, a door shut); stuck inside one: out is fine
                        if (!fits(next) && fits(bot.entity.position)) { blocked = true; break; }
                        if (pulls >= 3) break;
                        if (Date.now() - pulledAt < 700) { await sleep(50); continue; }
                        bot.entity.velocity = new Vec3(0, 0, 0);
                        bot.entity.position = next;
                        await turn();
                        await sleep(50);
                    }
                    if (blocked || pulls >= 3) break;
                }
                if (pulls >= 3) break; // the server keeps her here: stay, do not fight it
                if (!blocked) break; // there
            }
            } finally {
                bot.removeListener('forcedMove', onPull);
            }
            if (pulls >= 3) {
                (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] pulledBack ' + pulls);
            }
            bot.entity.velocity = new Vec3(0, 0, 0);
            for (let i = 0; i < 40; i++) {
                if (bot.interrupt_code) return;
                if (await turn()) break;
                await sleep(50);
            }
            bot.swingArm('right');
        })
    },
    { // VR Agent: she builds by hand like a player (the engine sends this)
        name: '!layBlocks',
        description: 'Engine only: lay these blocks by hand, one at a time.',
        params: {
            'job': {type: 'int', description: 'job number', domain: [0, 1000000000]},
            'item': {type: 'string', description: 'the block in her hand'},
            'spots': {type: 'string', description: 'x y z;x y z;...'}
        },
        perform: runAsAction(async (agent, job, item, spots) => {
            // The block in her hand, her head turning to each spot, an arm
            // swing per block. The engine sets the block on each swing, so it
            // appears exactly when her hand hits it.
            const bot = agent.bot;
            const Vec3 = bot.entity.position.constructor;
            const proxy = await import('../mindserver_proxy.js');
            const MAX_TURN = 0.3;
            const wrap = (a) => Math.atan2(Math.sin(a), Math.cos(a));
            const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
            const turnTo = async (look) => {
                const eye = bot.entity.position.offset(0, bot.entity.height || 1.62, 0);
                const d = look.minus(eye);
                const yaw = Math.atan2(-d.x, -d.z);
                const pitch = Math.atan2(d.y, Math.sqrt(d.x * d.x + d.z * d.z));
                const dy = wrap(yaw - bot.entity.yaw);
                const dp = pitch - bot.entity.pitch;
                await bot.look(bot.entity.yaw + Math.max(-MAX_TURN, Math.min(MAX_TURN, dy)),
                    bot.entity.pitch + Math.max(-MAX_TURN, Math.min(MAX_TURN, dp)), true);
                return Math.abs(dy) < 0.05 && Math.abs(dp) < 0.05;
            };
            try {
                const info = bot.registry.itemsByName[item];
                if (info) {
                    const Item = (await import('prismarine-item')).default(bot.version);
                    await bot.creative.setInventorySlot(36, new Item(info.id, 64));
                    bot.setQuickBarSlot(0);
                }
            } catch (e) { /* empty hand: she still lays them */ }
            const list = String(spots).split(';')
                .map((s) => s.trim().split(' ').filter(Boolean).map(Number))
                .filter((p) => p.length === 3 && p.every(Number.isFinite));
            for (let i = 0; i < list.length; i++) {
                if (bot.interrupt_code) return;
                const [x, y, z] = list[i];
                const spot = new Vec3(x + 0.5, y + 0.5, z + 0.5);
                for (let k = 0; k < 8; k++) {
                    if (await turnTo(spot)) break;
                    await sleep(50);
                }
                bot.swingArm('right');
                proxy.sendOutputToServer(agent.name, '[VR] put ' + job + ' ' + i);
                await sleep(260); // one block, a breath, the next: about 3 a second, easy to follow
            }
            proxy.sendOutputToServer(agent.name, '[VR] laid ' + job);
        })
    },
    { // VR Agent: the girls decide what happens; the stream engine carries it out
        name: '!buildNext',
        description: 'Fly to the next piece of your team project and build it right now.',
        perform: async function (agent) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] buildNext');
            return 'Building starts now, block by block. It is NOT done yet: wait until you see it finished before you say so.';
        }
    },
    {
        name: '!flyToPlace',
        description: 'Fly somewhere you choose: castle, network, farm, garden, base, sky (high up for a view) or friend.',
        params: {'place': {type: 'string', description: 'castle, network, farm, garden, base, sky or friend'}},
        perform: async function (agent, place) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] flyToPlace ' + place);
            return 'You fly to ' + place + '.';
        }
    },
    {
        name: '!changeMaterial',
        description: 'Choose the main block for the rest of the part you are building, like quartz_block, deepslate_bricks, pink_concrete, sandstone.',
        params: {'block': {type: 'BlockName', description: 'the new main block'}},
        perform: async function (agent, block) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] changeMaterial ' + block);
            return 'You asked for ' + block + '. Wait for the answer before saying it worked.';
        }
    },
    {
        name: '!testNetwork',
        description: 'Let your real neural network read a messy handwritten digit on its board and guess it.',
        params: {'digit': {type: 'int', description: 'the digit, 0 to 9', domain: [0, 10]}},
        perform: async function (agent, digit) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] testNetwork ' + digit);
            return 'The network reads a ' + digit + ' now.';
        }
    },
    { // VR Agent: anything from the creative inventory
        name: '!getItem',
        description: 'Creative: get any block or item into your inventory, like cake, diamond_sword, flower_pot, oak_sapling.',
        params: {
            'item': {type: 'string', description: 'item name, like cake or blue_orchid'},
            'count': {type: 'int', description: 'how many, 1 to 64', domain: [1, 65]}
        },
        perform: async function (agent, item, count) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] getItem ' + item + ' ' + count);
            return 'You reach for ' + count + ' ' + item + '. Wait to see if it worked.';
        }
    },
    {
        name: '!drinkPotion',
        description: 'Drink a potion yourself: speed, slowness, jump_boost, levitation, slow_falling, glowing, invisibility, night_vision, regeneration, strength, nausea.',
        params: {'effect': {type: 'string', description: 'the potion effect'}},
        perform: async function (agent, effect) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] drinkPotion ' + effect);
            return 'You drink a potion of ' + effect + '.';
        }
    },
    {
        name: '!splashPotion',
        description: 'Throw a splash potion at your friend (same effects as drinkPotion). Pranks welcome.',
        params: {'effect': {type: 'string', description: 'the potion effect'}},
        perform: async function (agent, effect) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] splashPotion ' + effect);
            return 'You throw a splash potion of ' + effect + ' at your friend.';
        }
    },
    {
        name: '!brewPotion',
        description: 'A potion experiment: brew something unknown and drink it. Nobody knows what it does until you try.',
        perform: async function (agent) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] brewPotion');
            return 'You start brewing a mystery potion. Wait for what it does.';
        }
    },
    {
        name: '!race',
        description: 'Challenge your friend to a flying race through the sky hoops. Who is faster?',
        perform: async function (agent) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] race');
            return 'Race on! Wait for the countdown.';
        }
    },
    {
        name: '!standInFront',
        description: 'Fly in front of your friend, facing her, so the stream camera sees you (or call her in front of you).',
        perform: async function (agent) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] standInFront');
            return 'Flying in front for the camera.';
        }
    },
    {
        name: '!buildThis',
        description: 'Build anything right now, by hand, block by block, in front of the camera: a tower, a sky castle, a name in gold, one stone block.',
        params: {'what': {type: 'string', description: 'what to build, in a few words'}},
        perform: async function (agent, what) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] buildThis ' + what);
            return 'You start building ' + what + ' by hand. It is NOT finished until you are told.';
        }
    },
    {
        name: '!blowUp',
        description: 'Place TNT by hand in front of you and blow it up. Only works far away from your builds.',
        perform: async function (agent) {
            (await import('../mindserver_proxy.js')).sendOutputToServer(agent.name, '[VR] blowUp');
            return 'You place the TNT. Wait for the boom.';
        }
    },
    { // VR Agent: her eyes, from the game itself (the engine asks, no AI call)
        name: '!senses',
        description: 'Engine only: what is around you.',
        perform: async function (agent) {
            const bot = agent.bot;
            const proxy = await import('../mindserver_proxy.js');
            const me = bot.entity.position;
            const out = {looking: '', near: [], players: []};
            try {
                const b = bot.blockAtCursor(48);
                if (b) out.looking = b.name + ' ' + Math.round(b.position.distanceTo(me)) + ' blocks away';
            } catch (e) { /* nothing in reach */ }
            const skip = new Set(['item', 'experience_orb', 'armor_stand', 'marker', 'arrow', 'firework_rocket']);
            for (const e of Object.values(bot.entities)) {
                if (!e || e === bot.entity || !e.position) continue;
                const d = e.position.distanceTo(me);
                if (d > 32) continue;
                const name = e.type === 'player' ? (e.username || 'a player') : (e.name || e.displayName || '');
                if (!name || skip.has(name)) continue;
                const dx = e.position.x - me.x;
                const dz = e.position.z - me.z;
                let a = Math.atan2(-dx, -dz) - bot.entity.yaw;
                a = Math.atan2(Math.sin(a), Math.cos(a));
                const where = Math.abs(a) < 0.6 ? 'in front' : (Math.abs(a) > 2.4 ? 'behind' : (a > 0 ? 'to the left' : 'to the right'));
                (e.type === 'player' ? out.players : out.near).push({name: name, d: Math.round(d), where: where});
            }
            out.near.sort((p, q) => p.d - q.d);
            out.near = out.near.slice(0, 8);
            out.players.sort((p, q) => p.d - q.d);
            const below = bot.blockAt(me.offset(0, -1, 0));
            out.on = below ? below.name : 'air';
            out.y = Math.round(me.y);
            out.time = bot.time ? bot.time.timeOfDay : null;
            proxy.sendOutputToServer(agent.name, '[VR] sees ' + JSON.stringify(out));
        }
    },
    { // VR Agent: back on the ground
        name: '!land',
        description: 'Creative mode only: stop flying.',
        perform: async function (agent) {
            agent.bot.creative.stopFlying();
        }
    },
"""


# Every order from the engine was echoed into the game chat ("*Director used
# flyTo*", then "Action output: ..."). Fast building sends several a second,
# and the server kicked the girls for spamming. Orders from DIRECTOR stay quiet.
ECHO_ORIGINAL = """                this.routeResponse(source, `*${source} used ${user_command_name.substring(1)}*`);
                if (user_command_name === '!newAction') {
                    // all user-initiated commands are ignored by the bot except for this one
                    // add the preceding message to the history to give context for newAction
                    this.history.add(source, message);
                }
                let execute_res = await executeCommand(this, message);
                if (execute_res) 
                    this.routeResponse(source, execute_res);"""
ECHO_QUIET = """                const quiet = source === '""" + DIRECTOR + """'; // VR Agent: engine orders are not chat
                if (!quiet) this.routeResponse(source, `*${source} used ${user_command_name.substring(1)}*`);
                if (user_command_name === '!newAction') {
                    // all user-initiated commands are ignored by the bot except for this one
                    // add the preceding message to the history to give context for newAction
                    this.history.add(source, message);
                }
                let execute_res = await executeCommand(this, message);
                if (execute_res && !quiet)
                    this.routeResponse(source, execute_res);"""


# A message that reaches a bot before she has spawned (she is just joining
# or rejoining) failed with "respondFunc is not a function" and was lost:
# 286 times in one stream. Now it waits for her (up to 20 s).
SEND_ORIGINAL = """            try {
                this.agent.respondFunc(data.from, data.message);
            } catch (error) {"""
SEND_WAITS = """            try { // VR Agent: wait for her to spawn instead of losing the message
                if (typeof this.agent.respondFunc !== 'function') {
                    let waited = 0;
                    const later = setInterval(() => {
                        waited += 500;
                        if (typeof this.agent.respondFunc === 'function') {
                            clearInterval(later);
                            this.agent.respondFunc(data.from, data.message);
                        } else if (waited >= 20000) {
                            clearInterval(later);
                            console.log('Not spawned yet, message dropped: ' + String(data.message).slice(0, 60));
                        }
                    }, 500);
                    return;
                }
                this.agent.respondFunc(data.from, data.message);
            } catch (error) {"""


def patch_mindcraft() -> list[str]:
    """Small, repeatable edits to Mindcraft's packages (safe to run every start).

    * prismarine viewer: smooth camera (no whiplash on stream)
    * pathfinder: walk instead of sprint, so moves are easy to follow
    * viewer: its own ports (VR_VIEWER_PORT_BASE), and a busy port no longer
      crashes the bot (Mika kept dropping out when port 3000 was taken)
    """
    done: list[str] = []
    edits = [
        (MINDCRAFT_DIR / "node_modules/prismarine-viewer/lib/mineflayer.js", VIEWER_ORIGINAL, VIEWER_SMOOTH),
        (MINDCRAFT_DIR / "node_modules/prismarine-viewer/public/index.js", CAMERA_ORIGINAL, CAMERA_GLIDE),
        (
            MINDCRAFT_DIR / "node_modules/prismarine-viewer/lib/mineflayer.js",
            "  http.listen(port, () => {",
            "  // VR Agent: a busy port must not crash the bot\n"
            "  http.on('error', (e) => console.error(`Prismarine viewer could not start on ${port}: ${e.message}`))\n"
            "  http.listen(port, () => {",
        ),
        (
            MINDCRAFT_DIR / "src/agent/vision/browser_viewer.js",
            "port: 3000+count_id,",
            "port: (parseInt(process.env.VR_VIEWER_PORT_BASE) || 3000) + count_id,",
        ),
        (
            MINDCRAFT_DIR / "node_modules/mineflayer-pathfinder/lib/movements.js",
            "    this.allowSprinting = true\n",
            "    this.allowSprinting = false // VR Agent: walk, easier to watch\n",
        ),
        (MINDCRAFT_DIR / "src/agent/commands/actions.js", FLY_ANCHOR, FLY_COMMANDS + FLY_ANCHOR),
        (MINDCRAFT_DIR / "src/agent/agent.js", ECHO_ORIGINAL, ECHO_QUIET),
        (MINDCRAFT_DIR / "src/agent/mindserver_proxy.js", SEND_ORIGINAL, SEND_WAITS),
    ]
    for path, old, new in edits:
        try:
            text = path.read_text()
        except Exception:
            continue
        if new.startswith(FLY_MARK) and FLY_MARK in text and new not in text:
            # an older version of our flight commands: take it out first
            start = text.index(FLY_MARK)
            text = text[:start] + text[text.index(FLY_ANCHOR, start):]
        if new in text:
            continue
        if old in text:
            text = text.replace(old, new, 1)
            path.write_text(text)
            done.append(path.name)
        else:
            logger.warning(f"Minecraft: {path.name} changed upstream, its stream patch was skipped")
    return done


# Commands the bots never need on stream. Fewer commands also means a shorter
# prompt on every single AI call.
BLOCKED_ACTIONS = [
    "!checkBlueprint", "!checkBlueprintLevel", "!getBlueprint", "!getBlueprintLevel",
    "!newAction", "!restart", "!clearChat", "!stfu", "!setMode", "!help", "!searchWiki",
    "!showVillagerTrades", "!tradeWithVillager", "!attackPlayer", "!digDown",
    "!lookAtPlayer", "!lookAtPosition",
]
ROBOT_LINE = "Be a friendly, casual, effective, and efficient robot."


def cache_friendly(default: str, style: str) -> str:
    """The system prompt with everything that never changes first.

    OpenAI reuses (and bills at a fraction) a prompt start it saw recently,
    but only up to the first changed character. Mindcraft puts the live stats
    near the top, so nothing was reused. Here the persona and the long command
    list come first; memory, stats, inventory and examples follow.
    """
    if not default:
        return ""
    body = default.replace("$COMMAND_DOCS\n", "").replace("$COMMAND_DOCS", "")
    body = body.replace("$SELF_PROMPT ", "").replace("$SELF_PROMPT", "")
    if ROBOT_LINE in body:
        body = body.replace(ROBOT_LINE, style)
    else:
        body = body.replace("\n", "\n" + style + " ", 1)
    marker = "\nSummarized memory:"
    if marker not in body:
        return body + "\n$COMMAND_DOCS\n$SELF_PROMPT"
    head, tail = body.split(marker, 1)
    return head + "\n$COMMAND_DOCS\n$SELF_PROMPT" + marker + tail


def _default_conversing() -> str:
    try:
        data = json.loads((MINDCRAFT_DIR / "profiles" / "defaults" / "_default.json").read_text())
        return str(data.get("conversing") or "")
    except Exception:
        return ""


RCON_PORT = 25575


def _rcon_password() -> str:
    path = RUNTIME_DIR / "rcon.txt"
    try:
        return path.read_text().strip()
    except Exception:
        import secrets

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        password = secrets.token_hex(12)
        path.write_text(password)
        return password


def _set_properties(values: dict[str, str]) -> None:
    path = SERVER_DIR / "server.properties"
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(out) + "\n")


def enable_rcon() -> None:
    """Server console over the network, local only, for teleports."""
    _set_properties({
        "enable-rcon": "true",
        "rcon.port": str(RCON_PORT),
        "rcon.password": _rcon_password(),
        "broadcast-rcon-to-ops": "false",
    })


async def bring_into_view(mover: str, friend: str) -> bool:
    """Put `mover` a few blocks in front of `friend` (a little to the side) so
    the camera through the friend's eyes shows all of her. Spots that are not
    free (a wall, a tree, a slope) are skipped, so nobody ends up inside
    blocks. No free spot: False (she is told to come back instead)."""
    for up, ahead in ((0, 4), (1, 4), (0, 5), (1, 5), (0, 3), (2, 4)):
        command = (
            f"execute at {friend} rotated ~ 0 positioned ^1.5 ^{up} ^{ahead} "
            f"if block ~ ~ ~ minecraft:air if block ~ ~1 ~ minecraft:air "
            f"run tp {mover} ~ ~ ~"
        )
        reply = await rcon_command(command, reply=True)
        if reply is False:
            return False  # no RCON at all
        if isinstance(reply, str) and reply.startswith("Teleported"):
            return True
    # No free spot: never "tp mover friend", that puts her inside her friend
    # (the camera through the friend's eyes then shows only a face).
    return False


async def rcon_online() -> Optional[set[str]]:
    """Lower case names of the players online, or None without RCON."""
    text = await rcon_command("list", reply=True)
    if not isinstance(text, str):
        return None
    names = text.split(":", 1)[1] if ":" in text else ""
    return {n.strip().lower() for n in names.split(",") if n.strip()}


class _Rcon:
    """One server console connection, kept open and shared (one command at a
    time). The camera sends 20 commands a second; a new connection for each
    one was slow and filled the server log."""

    def __init__(self) -> None:
        self.reader: Any = None
        self.writer: Any = None
        self.lock: Optional[asyncio.Lock] = None
        self.loop: Any = None
        self.next_id = 10

    @staticmethod
    def packet(pid: int, kind: int, body: str) -> bytes:
        import struct

        data = struct.pack("<ii", pid, kind) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    async def read(self) -> tuple[int, str]:
        import struct

        size = struct.unpack("<i", await self.reader.readexactly(4))[0]
        data = await self.reader.readexactly(size)
        return struct.unpack("<i", data[:4])[0], data[8:-2].decode("utf-8", "replace")

    def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = self.writer = None

    async def connect(self) -> bool:
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", RCON_PORT), timeout=2)
            self.writer.write(self.packet(1, 3, _rcon_password()))
            await self.writer.drain()
            pid, _body = await asyncio.wait_for(self.read(), timeout=3)
        except Exception:
            self.close()
            return False
        if pid == -1:
            logger.warning("Minecraft: the server refused the RCON password")
            self.close()
            return False
        return True

    async def run(self, command: str, reply: bool) -> Any:
        loop = asyncio.get_running_loop()
        if self.lock is None or self.loop is not loop:  # a new event loop (tests, restarts)
            self.close()
            self.lock, self.loop = asyncio.Lock(), loop
        async with self.lock:
            for attempt in range(2):
                if self.writer is None and not await self.connect():
                    return False
                try:
                    self.next_id = self.next_id % 1_000_000 + 1
                    self.writer.write(self.packet(self.next_id, 2, command))
                    await self.writer.drain()
                    while True:
                        pid, body = await asyncio.wait_for(self.read(), timeout=3)
                        if pid == self.next_id:
                            return body if reply else True
                except Exception as exc:
                    logger.debug(f"Minecraft: RCON failed ({exc}), reconnecting")
                    self.close()
            return False


_RCON = _Rcon()


async def rcon_command(command: str, reply: bool = False) -> Any:
    """Run one server command. False when the server has no RCON (yet)."""
    return await _RCON.run(command, reply)


def set_difficulty(level: str) -> None:
    if level not in ("peaceful", "easy", "normal", "hard"):
        return
    path = SERVER_DIR / "server.properties"
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return
    out = [f"difficulty={level}" if line.startswith("difficulty=") else line for line in lines]
    if not any(line.startswith("difficulty=") for line in lines):
        out.append(f"difficulty={level}")
    path.write_text("\n".join(out) + "\n")


def _write_pid(name: str, pid: int) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / f"{name}.pid").write_text(str(pid))
    except Exception:
        pass


def stop_processes(timeout: float = 20.0) -> None:
    """Mindcraft first (the bots log out), then the server (it saves the world)."""
    stop_one("mindcraft", min(timeout, 8.0))
    stop_one("server", timeout)


# What each process runs: a pid file from before a reboot can name some
# other program now, and that one must never be stopped.
PROCESS_NAMES = {"mindcraft": ("node", "main.js"), "server": ("java", "server.jar")}


def _is_ours(name: str, pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except FileNotFoundError:
        return not Path("/proc").is_dir()  # no /proc (macOS): trust the pid file
    except Exception:
        return True
    return all(part in cmdline for part in PROCESS_NAMES.get(name, ()))


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def stop_one(name: str, timeout: float = 20.0) -> None:
    pid_file = RUNTIME_DIR / f"{name}.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return
    if not _is_ours(name, pid):
        logger.info(f"Minecraft: {name}.pid is old (that process is something else now), removed")
        pid_file.unlink(missing_ok=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    except Exception as exc:
        logger.debug(f"Minecraft: could not stop {name}: {exc}")
        return
    end = time.time() + timeout
    while time.time() < end:  # the whole group: Mindcraft runs one process per bot
        if not _group_alive(pid):
            break
        time.sleep(0.3)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            pass
    pid_file.unlink(missing_ok=True)
    logger.info(f"Minecraft: {name} stopped")
