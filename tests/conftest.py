import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import os  # noqa: E402
import tempfile  # noqa: E402

# Trivia keeps its question rotation and AI questions in cache/. Tests use a
# throwaway folder so runs stay repeatable and never touch the real history.
os.environ.setdefault("VR_AGENT_CACHE_DIR", tempfile.mkdtemp(prefix="vr-agent-test-cache-"))
