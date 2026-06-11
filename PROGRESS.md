# Jarvis AI — Progress & Roadmap

Personal voice-controlled AI assistant running entirely on local Mac Silicon infrastructure.

## Architecture

| File | Responsibility |
|---|---|
| `jarvis.py` | Orchestrator: wake → conversation mode → brain → TTS → memory save; proactive mode |
| `config.py` | All tunables and feature flags (env-overridable) |
| `llm.py` | Ollama brain: unified routing, agentic tool loop, `[END]` session control, history pruning |
| `stt.py` | Wake word (Porcupine / Whisper polling), VAD command recorder, transcription |
| `tts.py` | Kokoro chunk streaming, pronunciation normalization, pacing; ElevenLabs flag-gated |
| `audio_io.py` | Interruptible playback, chime, interrupt monitor, Spotify ducking |
| `memory.py` | Pinecone store (recency-weighted retrieval, consolidation), typed extraction, session state |
| `obsidian_store.py` | Daily notes with wikilinks/tags + entity stubs |
| `agents.py` | Calendar, WebSearch, Weather, Spotify, Gmail agents |
| `computer_control.py` | App/file control + auto-refreshing home-directory index |
| `ui_bridge.py` | TCP JSON event server (backend) / client (UIs) |
| `jarvis_ui.py` | PyQt6 floating pill |
| `jarvis_tray.py` | rumps menu bar status icon |
| `logging_setup.py` | Structured logging (console INFO, jarvis.log DEBUG, errors.log WARN+) |
| `launcher.c` / `build_app.sh` | Jarvis.app native launcher |
| `install_launch_agents.sh` | launchd services (auto-start, auto-restart) |

---

### Core Voice Pipeline

- **Always-on wake word** — two engines, auto-selected: **pvporcupine** (sub-100ms, near-zero CPU; set `PICOVOICE_ACCESS_KEY`, free personal tier) or the MLX Whisper 3-second polling fallback, RMS-gated to skip silence
- **Ambient-noise hardening** on the Whisper path: minimum word count + `no_speech_prob` / `avg_logprob` confidence thresholds so game audio and background voices stop burning transcription cycles
- **Command recording** with voice-activity detection via sounddevice — 0.3s pre-roll so the first syllable isn't clipped, stops after 0.8s of silence, caps at 45s
- **Transcription** via `mlx-community/whisper-small-mlx` (configurable: `JARVIS_WHISPER_REPO`)
- **Conversation mode** — after the first response, Jarvis stays in a listening loop (10s timeout) so follow-ups don't need the wake word
- **Chime system** — soft-attack 880 Hz tone after every response so the user knows when to speak
- **Interruption handling** — talking over Jarvis (sustained energy above the calibrated ambient baseline) stops playback within ~20ms and returns straight to listening
- **Audio device error recovery** — PortAudio failures (the old `-9986` crash) back off and reopen the stream instead of killing the backend

### AI Reasoning

- **Main LLM**: `qwen2.5:14b` via Ollama, fully local (utility tasks on `qwen2.5:7b`)
- **Unified routing**: every command goes directly to the main model with the full tool list — no pre-classification step
- **Multi-turn tool chaining**: agentic loop keeps executing tools until the model stops requesting them (up to 6 rounds), so "find the emails, summarise, then book a follow-up" works in one command
- **Tool error recovery**: failures retry once, then reach the model as a structured error template — never raw tracebacks read aloud
- **Message history pruning**: beyond 24 messages, older turns are folded into a one-paragraph summary; long sessions never hit the context window
- **Character prompt**: JARVIS persona — precise, dry, no affirmations, no follow-up questions by habit, "sir" where natural

### Text-to-Speech

- **Kokoro-82M** (`hexgrad/Kokoro-82M`), voice `af_heart`, loaded in a background thread
- **Chunk streaming** — audio plays while later sentences are still synthesising
- **Pronunciation pre-processing** — dates → spoken form ("June eleventh, twenty twenty-six"), acronyms letter-spaced ("APIs" → "A P I s"), abbreviations expanded ("Dr." → "Doctor"), 24h times, °F/%, URLs
- **Pacing variation** — short acknowledgements faster (1.15×), long explanations measured (0.95×)
- **Filler behaviour** — "On it, sir." / "One moment." before tool rounds instead of dead air
- **Audio ducking** — Spotify drops to 25% while Jarvis speaks, restores after
- **ElevenLabs TTS/STT** — fully wired (`JARVIS_TTS_ENGINE=elevenlabs`, `JARVIS_STT_ENGINE=scribe`) but **off by default: costs money**; everything stays local until final release

### Agent System (39 tools across 6 agents)

| Agent | Tools |
|---|---|
| `Calendar_Agents` | create, read, update, delete calendar events (Google Calendar API) |
| `WebSearchAgents` | web search (Tavily), webpage extraction |
| `WeatherSearch` | current weather, weather at time, daily forecast, weather alerts (OpenWeatherMap One Call 3.0) |
| `SpotifyAgent` | current track, queue song, create/add to playlist, recently played, skip, pause/resume, shuffle, set volume |
| `GmailAgent` | send, search, unread, get by ID, reply (properly threaded), mark read, trash, untrash, drafts, sent, sender profile, labels |
| `ComputerControlAgent` | open/close/switch/list applications, open/create/delete/move files |

- **Spotify device guard** — playback tools pre-check for an active device and tell the user instead of failing silently
- Agents that fail auth at startup degrade gracefully: their tools are disabled, the rest of Jarvis runs

### ComputerControl File Index

- Indexes the home directory on startup, cached at `directory_cache.json`; cache loads instantly on subsequent starts (skips `.git`, `node_modules`, `Library`, etc.)
- **Auto-refresh**: an FSEvents watcher (watchdog) marks the index dirty on file-system changes; a rebuild runs after 60s of quiet — new files are no longer invisible
- Fuzzy search resolves spoken names ("open my resume") with substring + close-spelling matching
- `delete_file` moves to **Trash via Finder** — never a hard delete; ambiguous matches ask before deleting

### Memory System

**Pinecone Vector Store**
- Serverless dense index (`jarvis-ai`) with `llama-text-embed-v2` integrated embeddings
- The record text field is read from the live index's own `field_map` at startup — the class of silent upsert failure that broke memory for months can't recur
- **Recency-weighted retrieval**: similarity blended with exponential time decay (30-day half-life), top-8 injected into the system prompt each turn
- Relative time labels on every retrieved memory ("today", "yesterday", "3 days ago")
- **Consolidation job** every 15 sessions: dedupes and reconciles contradictory facts (newer wins)

**Memory Extraction**
- Background thread after every conversation — never blocks the response
- Typed facts: `preference`, `habit`, `project`, `personal`, `decision`; plus a one-sentence session episode
- Trivial sessions correctly return `NONE` and produce no write
- **Cross-session continuity**: the last episode opens the next session's prompt ("Last time you spoke (yesterday): ...")

**Obsidian Integration**
- Vault from `OBSIDIAN_VAULT_PATH`; `Memory/` auto-created
- Daily note per day with session episodes and typed facts (`#preference`, `#habit`, ... tags)
- **Backlinks**: people/projects come out of extraction wrapped in `[[wikilinks]]`; entity stub notes are created under `Memory/Entities/` so the vault is a navigable graph, not just a log

### Session Control

- **`[END]` token system** with strengthened prompt examples; token stripped before speaking
- **Dual fallback**: farewell phrases in the user's words AND goodbye patterns in the model's reply both trigger the ending path
- Memories saved on timeout exit too

### Proactive Mode

- **Upcoming event announcements** — calendar polled every 5 minutes; events starting within 10 minutes are announced ("Sir, heads up — ...") only when idle, never over a conversation
- **Morning briefing** — once per day after 8am (`JARVIS_BRIEFING_HOUR`): date, weather (set `JARVIS_HOME_LAT/LON`), today's calendar, unread mail count

### Infrastructure

- **`Jarvis.app`** — `launcher.c` compiled via `./build_app.sh` into a proper app bundle (mic usage description included)
- **LaunchAgent setup** — `./install_launch_agents.sh` registers `com.jarvisai.backend` + `com.jarvisai.ui` with `KeepAlive: true`: auto-start on login, auto-restart on crash
- **UI bridge** — TCP JSON event server embedded in the backend (`ui_bridge.py`); emits state changes, transcripts, tool activity, memory hits, errors; never blocks the voice pipeline
- **Structured logging** — console shows INFO+ (conversations, tools, memory); `logs/jarvis.log` has everything incl. wake-polling DEBUG noise; `logs/errors.log` is failures only

### UI (jarvis_ui.py)

- PyQt6, frameless, translucent, `NSStatusWindowLevel` (25) — floats above everything incl. the menu bar, on all Spaces
- **Idle**: 28×28 pill, breathing amber glow dot + orbiting arc
- **Active**: animates open (**150ms wake-up animation**, OutCubic) to 440×108 — state indicator, animated waveform bars, **descriptive tool labels** ("Searching Spotify...", "Reading your calendar...")
- **Memory hit indicator** — faint `✦ n` pulse when retrieved memories are used
- **Conversation history panel** — click the pill to expand to the last 6 exchanges
- **Error state** — pulsing red border flash + failure message on tool/LLM/audio errors
- **Transcript overflow** — fades the last line out instead of hard-cutting
- **Dark/light mode** — palette follows macOS appearance, re-checked periodically
- **Notch-aware** — anchors right of the camera notch via `NSScreen.auxiliaryTopRightArea`
- Ironman amber/gold palette (`#ffaa00`, `#ff6600`, warm white)

### Menu Bar (jarvis_tray.py)

- rumps tray icon mirroring pill state (◇ idle, ◉ listening, ✦ thinking, ⚙ working, ▶ speaking) so Jarvis is visibly alive at a glance

### Google OAuth / Spotify Auth

- Shared Google credentials (calendar + gmail full scopes) via `get_google_creds()`; token auto-refresh from `token.json`
- Spotify OAuth cached at `.spotify_token`, browser flow on first run

---

## Remaining / Deferred

- **Whisper model size benchmarking** — `JARVIS_WHISPER_REPO` makes swapping `whisper-base` / `whisper-medium` a one-line change; actual benchmarking against real usage still to do
- **Porcupine key** — wake latency drops to sub-100ms once a free `PICOVOICE_ACCESS_KEY` is added to `.env` (code path is ready; falls back to Whisper polling without it)
- **ElevenLabs TTS + Scribe STT** — wired and tested paths, deliberately off (paid). Flip `JARVIS_TTS_ENGINE` / `JARVIS_STT_ENGINE` for final release
- **Claude API migration** — deferred by decision: local-only, no paid LLMs for now. The brain is isolated in `llm.py`, so a future backend swap touches one file
- **SSML prosody** — only relevant if/when ElevenLabs is activated; Kokoro pacing is handled via its speed parameter
