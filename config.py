"""
Central configuration for Jarvis.

Everything tunable lives here: model names, feature flags, audio thresholds,
paths, and ports. Flags read from the environment so behaviour can be changed
without touching code (e.g. `JARVIS_TTS_ENGINE=elevenlabs python3 jarvis.py`).

Paid services (ElevenLabs TTS/STT) stay OFF by default — Jarvis runs fully
locally on Ollama + Kokoro + MLX Whisper unless explicitly switched.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- paths
JARVIS_HOME = Path(__file__).resolve().parent
LOG_DIR = JARVIS_HOME / "logs"
STATE_FILE = JARVIS_HOME / "state.json"            # session counters, last episode, briefing date
DIRECTORY_CACHE = JARVIS_HOME / "directory_cache.json"
OBSIDIAN_VAULT = Path(os.getenv("OBSIDIAN_VAULT_PATH", str(Path.home() / "Desktop" / "Jarvis AI")))

# ---------------------------------------------------------------- LLM (local only — no paid APIs)
MAIN_MODEL = os.getenv("JARVIS_MAIN_MODEL", "qwen2.5:14b")      # reasoning + tool use
UTILITY_MODEL = os.getenv("JARVIS_UTILITY_MODEL", "qwen2.5:7b")  # memory extraction, summaries
MAX_TOOL_ROUNDS = 6           # agentic loop: max tool-call passes per command
HISTORY_MAX_TURNS = 24        # prune message history beyond this many non-system messages
HISTORY_KEEP_RECENT = 12      # turns kept verbatim when pruning; older ones get summarised

# ---------------------------------------------------------------- TTS
TTS_ENGINE = os.getenv("JARVIS_TTS_ENGINE", "kokoro")  # "kokoro" | "elevenlabs" (paid — off by default)
KOKORO_VOICE = "af_heart"
KOKORO_SAMPLE_RATE = 24000
ELEVENLABS_VOICE_ID = "k7IRoeykhdGZUkTeJ1ID"
ELEVENLABS_TTS_MODEL = "eleven_turbo_v2_5"

# ---------------------------------------------------------------- STT
STT_ENGINE = os.getenv("JARVIS_STT_ENGINE", "whisper")  # "whisper" | "scribe" (paid — off by default)
# whisper-small is the speed/accuracy trade-off; swap to base (faster) or medium (more accurate)
WHISPER_REPO = os.getenv("JARVIS_WHISPER_REPO", "mlx-community/whisper-small-mlx")
AUDIO_SAMPLE_RATE = 16000

# ---------------------------------------------------------------- wake word
WAKE_WORD = "jarvis"
# Whisper sometimes hears the wake word slightly off; accept close variants.
WAKE_VARIANTS = ("jarvis", "jervis", "jarvas", "jarvys", "javis", "garvis")
# "porcupine" = sub-100ms detection (needs PICOVOICE_ACCESS_KEY, free personal tier)
# "whisper"   = 3s clip polling (no key needed)
# "auto"      = porcupine if available + key set, else whisper
WAKE_ENGINE = os.getenv("JARVIS_WAKE_ENGINE", "auto")
PICOVOICE_ACCESS_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "")
WAKE_CLIP_SECONDS = 3.0
WAKE_RMS_GATE = 0.010              # skip transcription below this RMS (silence)
WAKE_MIN_WORDS = 1                 # ambient-noise filter: require at least this many words
WAKE_MAX_NO_SPEECH_PROB = 0.5      # Whisper confidence filters to cut false positives
WAKE_MIN_AVG_LOGPROB = -1.0

# ---------------------------------------------------------------- command recording (VAD)
VAD_START_THRESHOLD = 0.015        # RMS that counts as speech onset
VAD_STOP_SILENCE = 0.8             # seconds of silence that ends the command
VAD_MAX_COMMAND = 45.0             # hard cap on a single command, seconds
VAD_WAIT_TIMEOUT = 10.0            # conversation mode: give up waiting for speech after this

# ---------------------------------------------------------------- interruption / ducking
INTERRUPT_ENABLED = _env_bool("JARVIS_INTERRUPT", True)
INTERRUPT_RMS_MULTIPLIER = 4.0     # speech must exceed ambient baseline by this factor
INTERRUPT_SUSTAIN = 0.35           # seconds the energy must stay high (rejects bumps/clicks)
DUCKING_ENABLED = _env_bool("JARVIS_DUCKING", True)
DUCK_VOLUME = 25                   # Spotify volume while Jarvis speaks (restored after)

# ---------------------------------------------------------------- memory
PINECONE_INDEX = "jarvis-ai"
PINECONE_NAMESPACE = "jarvis-memory-namespace"
MEMORY_TOP_K = 8
MEMORY_RECENCY_HALF_LIFE_DAYS = 30.0   # recency-weighted retrieval decay
MEMORY_RECENCY_WEIGHT = 0.15           # blended into the similarity score
CONSOLIDATE_EVERY_N_SESSIONS = 15      # periodic dedupe/merge job cadence

# ---------------------------------------------------------------- UI bridge
UI_BRIDGE_HOST = "127.0.0.1"
UI_BRIDGE_PORT = int(os.getenv("JARVIS_UI_PORT", "51361"))

# ---------------------------------------------------------------- proactive mode
PROACTIVE_ENABLED = _env_bool("JARVIS_PROACTIVE", True)
PROACTIVE_POLL_SECONDS = 300       # calendar check cadence
PROACTIVE_LEAD_MINUTES = 10        # announce events starting within this window
BRIEFING_HOUR = int(os.getenv("JARVIS_BRIEFING_HOUR", "8"))   # morning briefing after this hour
HOME_LAT = os.getenv("JARVIS_HOME_LAT")    # optional: enables weather in the briefing
HOME_LON = os.getenv("JARVIS_HOME_LON")

# ---------------------------------------------------------------- file index
INDEX_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "Library",
    ".Trash", ".cache", ".npm", ".cargo", ".rustup", "Applications",
    ".docker", ".ollama", "Pictures/Photos Library.photoslibrary",
}
INDEX_REFRESH_DEBOUNCE = 60.0      # seconds of FS quiet before a dirty index rebuilds
