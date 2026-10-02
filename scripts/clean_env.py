"""Tidy .env in place: one copy of each setting, publishing on, YouTube off.

Run from the project folder:  python scripts/clean_env.py
Keeps every key you already have (the first non-empty value of each), saves
the old file as .env.backup, and never prints a secret.
"""
import os, shutil, stat
from pathlib import Path

env = Path(".env")
lines = env.read_text(encoding="utf-8").splitlines()
values = {}
for raw in lines:
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if value and not values.get(key):
        values[key] = value  # first non-empty value wins, nothing is lost

if not values.get("GITHUB_PUBLISH_TOKEN") and values.get("githubpersonalaccesstoken"):
    values["GITHUB_PUBLISH_TOKEN"] = values["githubpersonalaccesstoken"]

backup = Path(".env.backup")
shutil.copy2(env, backup)
os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)

set_now = {
    "PUBLISHING_ENABLED": "true",
    "PUBLISHING_DRY_RUN": "false",
    "PUBLISHING_AUTO": "true",
    "GITHUB_PUBLISH_REPO": "randomsci/mika-generated-games",
    "GITHUB_PUBLISH_BRANCH": "main",
    "GITHUB_PAGES_BASE_URL": "https://randomsci.github.io/mika-generated-games",
    "YOUTUBE_PUBLISHING_ENABLED": "false",
    "YOUTUBE_AUTO_CHAT_REPLY": "false",
    "YOUTUBE_AUTO_DESCRIPTION": "false",
    "YOUTUBE_AUTO_COMMENT_REPLY": "false",
    "YOUTUBE_DESCRIPTION_LATEST": values.get("YOUTUBE_DESCRIPTION_LATEST", "5"),
}
order = [
    ("# Keys", ["OPENAI_API_KEY", "ELEVENLABS_API_KEY", "YOUTUBE_API_KEY"]),
    ("# Publishing", ["PUBLISHING_ENABLED", "PUBLISHING_DRY_RUN", "PUBLISHING_AUTO",
      "GITHUB_PUBLISH_REPO", "GITHUB_PUBLISH_BRANCH", "GITHUB_PUBLISH_TOKEN", "GITHUB_PAGES_BASE_URL"]),
    ("# YouTube (off in DEV, previews print in the terminal)", ["YOUTUBE_PUBLISHING_ENABLED",
      "YOUTUBE_AUTO_CHAT_REPLY", "YOUTUBE_AUTO_DESCRIPTION", "YOUTUBE_AUTO_COMMENT_REPLY",
      "YOUTUBE_CHANNEL_ID", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_DESCRIPTION_LATEST"]),
]
dropped = {"githubpersonalaccesstoken", "YOUTUBE_VIDEO_ID", "YOUTUBE_REFRESH_TOKEN"}
out, used = [], set()
for title, keys in order:
    out.append(title)
    for key in keys:
        value = set_now.get(key, values.get(key, ""))
        out.append(f"{key}={value}")
        used.add(key)
    out.append("")
extra = [k for k in values if k not in used and k not in dropped]
if extra:
    out.append("# Other")
    out += [f"{k}={values[k]}" for k in extra]
    out.append("")
env.write_text("\n".join(out), encoding="utf-8")
os.chmod(env, stat.S_IRUSR | stat.S_IWUSR)

print("Cleaned .env (backup in .env.backup). Values are not shown.")
for title, keys in order:
    for key in keys:
        value = set_now.get(key, values.get(key, ""))
        secret = "KEY" in key or "TOKEN" in key or "SECRET" in key
        print(f"  {key:<28} {('set' if value else 'MISSING') if secret else value or 'MISSING'}")
for k in extra:
    print(f"  {k:<28} kept")
