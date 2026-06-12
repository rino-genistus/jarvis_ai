# Jarvis AI — Progress & Roadmap

Personal voice-controlled AI assistant running entirely on local Mac Silicon infrastructure.

<<<<<<< HEAD
=======
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

>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141
---

### Core Voice Pipeline

<<<<<<< HEAD
- **Always-on background listener** using sounddevice to poll 3-second audio clips, RMS-gated to skip silence
- **Wake word detection** via MLX Whisper transcription scanning for "Jarvis" in each clip
- **Command recording** with voice-activity detection — starts capturing when speech is detected, stops after 0.8s of silence, caps at 45s
- **Transcription** via `mlx-community/whisper-small-mlx` running natively on Apple Silicon via MLX
- **Conversation mode** — after the first response, Jarvis stays in a listening loop (10s timeout) so follow-up commands don't require repeating the wake word
- **Chime system** — a short 880 Hz tone plays after every Jarvis response so the user knows when to speak

### AI Reasoning

- **Main LLM**: `qwen2.5:14b` via Ollama, running fully locally
- **Unified routing**: every command goes directly to qwen2.5 with the full tool list — no pre-classification step (previously required a separate `llama3.2:1b` classification call per turn, now eliminated)
- **Tool execution loop**: model requests tools → execute → append results → ask model to summarise in Jarvis's voice
- **Character prompt**: JARVIS persona tuned to be precise, dry, no affirmations, no follow-up questions by habit, calls the user "sir" naturally

### Text-to-Speech

- **Kokoro-82M** (`hexgrad/Kokoro-82M`) running fully locally, voice `af_heart`
- Loads in a background thread on startup; `kokoro_ready.wait()` blocks the main loop until ready
- **Chunk streaming** — audio plays chunk-by-chunk as Kokoro generates it rather than buffering the full response first, reducing time-to-first-word
- All TTS routed through `safe_speak()` which guards against empty string inputs
=======
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
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

### Agent System (39 tools across 6 agents)

| Agent | Tools |
|---|---|
<<<<<<< HEAD
| `Calendar_Agents` | create, read, update, delete calendar events via Google Calendar API |
| `WebSearchAgents` | web search (Tavily), webpage extraction |
| `WeatherSearch` | current weather, weather at time, daily forecast, weather alerts (OpenWeatherMap One Call 3.0) |
| `SpotifyAgent` | current track, queue song, create/add to playlist, recently played, skip, pause, shuffle, set volume |
| `GmailAgent` | send, search, unread, get by ID, reply, mark read, trash, untrash, drafts, sent, sender profile, all labels |
| `ComputerControlAgent` | open/close/switch/list applications, open/create/delete/move files |

### ComputerControl File Index

- Indexes the entire home directory on startup, cached at `~/jarvis_ai/directory_cache.json`
- Loads from cache on subsequent starts (skips `.git`, `.venv`, `__pycache__`, `node_modules`, `Library`, etc.)
- Background thread join on startup ensures the index is ready before the main loop begins
- `refresh_index()` available to rebuild when the file system changes
=======
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
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

### Memory System

**Pinecone Vector Store**
- Serverless dense index (`jarvis-ai`) with `llama-text-embed-v2` integrated embeddings
<<<<<<< HEAD
- `upsert_records` pushes extracted facts and session episodes after each conversation
- `retrieve_memories` fetches top-8 semantically relevant memories and injects them into the system prompt on every turn
- Fixed a critical silent bug where all upserts were failing since project inception: the record field was named `chunk_text` but Pinecone's API requires `text` matching the `field_map` key — every upsert was returning a 400 error swallowed by the background thread

**Memory Extraction**
- Runs in a background thread after every conversation so it never blocks the response
- `extract_important_messages` uses qwen2.5:14b to pull long-term facts from the conversation, typed as: `preference`, `habit`, `project`, `personal`, `decision`
- `extract_session_episode` writes a single-sentence "We discussed..." summary of what was accomplished
- Trivial sessions (weather lookups, one-off greetings) correctly return `NONE` and produce no write
- Relative time labels on retrieved memories ("today", "yesterday", "3 days ago", "last week") so the LLM has temporal context

**Obsidian Integration**
- Connected to vault at `/Users/rino/Desktop/Jarvis AI/`
- `Memory/` subfolder auto-created on startup
- After each conversation, a dated daily note (`YYYY-MM-DD.md`) is appended with the session episode and any extracted facts
- Provides a human-readable, searchable log of everything Jarvis has learned alongside the Pinecone vector index

### Session Control

- **`[END]` token system** — the LLM appends `[END]` to responses where the conversation is genuinely over; Jarvis strips it before speaking, saves memories in background, resets the message history, and returns to wake-word mode
- **Farewell phrase fallback** — if the model misses `[END]`, a client-side check on the user's own words catches clear farewells ("that'll be all", "goodbye", "talk later", etc.) and triggers the same ending path
- Session memory is saved on timeout exit too — if the conversation mode times out without a clean `[END]`, memories are still written

### Infrastructure

- **`Jarvis.app`** — compiled macOS application (`launcher.c` → binary) that launches the Python backend, so the app can be opened like any normal Mac app
- **LaunchAgent setup** — two `launchd` agents (`com.jarvisai.backend`, `com.jarvisai.ui`) registered as system services with `KeepAlive: true`, so Jarvis auto-starts on login and auto-restarts on crash
- **UI bridge** — socket server (`ui_bridge.py`) embedded in the backend process; the UI connects as a TCP client and receives JSON events for state changes, transcripts, tool calls, and memory hits

### UI (jarvis_ui.py)

- Built in PyQt6
- **Idle state**: 28×28 px dark pill with a slow amber glow dot and orbiting arc — barely visible, sits above the menu bar
- **Active state**: expands to 440×108 px pill showing state indicator, animated waveform bars, tool status, and up to 3 lines of transcript
- **Always-on-top** at `NSStatusWindowLevel` (25) so it floats above every app including the macOS menu bar
- **Notch-aware** — detects the camera notch position via `NSScreen.auxiliaryTopRightArea` and anchors the pill to the right of it
- Ironman amber/gold colour palette (`#ffaa00`, `#ff6600`, warm white text)

### Google OAuth

- Shared credentials across Calendar and Gmail via `get_google_creds()`
- Full scopes: calendar + gmail read/send/modify/labels
- Token auto-refreshes from `token.json`; re-authenticates via browser if expired

### Spotify Auth

- OAuth flow with token cached at `.spotify_token`
- Browser-based login on first run; redirect URL paste flow

---

## Further Improvements

### Fluid, Human-Like Speech

The current Kokoro-82M voice is functional but clearly synthetic. It lacks the natural rhythm, breathing patterns, and intonation variation of real speech.

- **Activate ElevenLabs TTS** — the code already exists (`play_audio_with_text_eleven_labs`, voice `k7IRoeykhdGZUkTeJ1ID`, model `eleven_turbo_v2_5`); just needs wiring into `safe_speak()`. This is the single biggest improvement to perceived naturalness.
- **Activate ElevenLabs Scribe STT** — replace MLX Whisper with ElevenLabs `scribe_v2`; better transcription accuracy especially for names, technical terms, and accented speech.
- **Interruption handling** — the system is fully blocking during TTS playback; the user cannot stop Jarvis mid-sentence. Add a hotkey or energy-threshold interrupt that stops `sounddevice` playback immediately. **DONE**
- **Audio ducking** — when Jarvis speaks, Spotify should lower its volume automatically and restore it after. Currently speech and music compete at equal volume.
- **TTS pronunciation fixes** — Kokoro-82M mispronounces dates, acronyms, and proper nouns. Add a pre-processing pass that expands abbreviations ("Dr." → "Doctor", "APIs" → "A P I s", dates → spoken form) before passing to TTS.
- **Speaking pace variation** — responses are spoken at a flat pace. A short acknowledgement should be faster; a detailed explanation should be measured. Add sentence-length-based pacing hints or SSML if ElevenLabs supports it.
- **Natural filler behaviour** — a brief "On it" or "One moment" before long tool calls would feel more human than silence during the tool execution gap. **DONE**

### UI Improvements

The current UI communicates state but lacks polish and information density.

- **Memory hit indicator** — when Jarvis pulls a memory and uses it, the UI should subtly show this (e.g. a faint memory icon or count pulse) so the user can see the memory system is working.
- **Conversation history panel** — an expandable view showing the last few exchanges, accessible by clicking the pill. Useful for reviewing what was said without asking Jarvis to repeat.
- **Menu bar status icon** — a `rumps`-based tray icon that mirrors the pill state (idle/listening/thinking/speaking) without requiring the full pill to be visible. Lets the user know Jarvis is alive at a glance.
- **Tool activity labels** — when a tool runs, the pill currently shows the tool name briefly. Expand this to show progress ("Searching Spotify...", "Reading your calendar...") with more descriptive text.
- **Wakeup animation** — the transition from idle pill to active panel is instant. A 150ms expand animation would make the activation feel more alive.
- **Error state** — the UI has no visual indicator when something goes wrong (tool error, LLM timeout, audio device failure). A brief red flash or warning text would help with debugging and user awareness.
- **Transcript overflow** — the 3-line transcript truncates long responses. Either scroll the transcript block or fade it out gracefully instead of hard-cutting.
- **Dark/light mode adaptation** — the colour palette is fixed amber-on-black; it should respect macOS appearance settings, or at minimum offer a lighter variant for users on light wallpapers.

### Wake Word & Latency

- **Replace Whisper polling with pvporcupine** — the current approach records a 3-second clip and runs Whisper transcription to detect "Jarvis". This alone adds 3-6 seconds before command recording even starts. pvporcupine provides sub-100ms hardware-accelerated wake word detection with near-zero CPU cost. This is the largest single remaining latency improvement.
- **Ambient noise false positives** — the logs show dozens of random ambient sounds (game audio, background voices) being transcribed. The RMS gate helps but isn't enough. A minimum-word-count filter and a confidence threshold on Whisper's output would reduce wasted transcription cycles.
- **Whisper model size** — `whisper-small-mlx` is a trade-off between speed and accuracy. `whisper-base` would be faster with slightly lower accuracy; `whisper-medium` would be more accurate with slightly higher latency. Worth benchmarking both with actual usage patterns.

### LLM & Intelligence

- **Migrate to Claude API** — qwen2.5:14b via Ollama is slower to respond and less capable at reasoning and instruction following than Claude. The migration is already planned; the pattern is established from earlier agent migrations. This would improve response quality, [END] token reliability, and memory extraction accuracy.
- **Multi-turn tool chaining** — currently one tool pass per command; if task A needs output from task B, the model must guess. An agentic loop that keeps calling tools until the model stops requesting them would enable more complex workflows ("find all emails from John this week, summarise them, then add a calendar block to follow up").
- **[END] token reliability** — even with the farewell fallback, the model occasionally misses obvious session endings. Strengthening the system prompt phrasing or adding a lightweight post-response check would make session termination feel seamless.
- **Proactive mode** — Jarvis is entirely reactive. It could poll for upcoming calendar events and announce them, alert on important emails, or greet the user in the morning with a brief briefing, all without being asked.

### Memory & Context

- **Cross-session continuity** — each session starts with only the Pinecone memory injected. Injecting the most recent session episode as an opening context line ("Last time we spoke, we...") would make Jarvis feel like it remembers the previous conversation naturally, not just facts.
- **Memory consolidation** — over time, Pinecone will accumulate duplicate and contradictory facts. A periodic merge job that deduplicates and reconciles conflicting records would keep memory quality high.
- **Recency weighting** — the current retrieval is purely semantic similarity. Blending recency into the score (more recent facts ranked higher for equal similarity) would surface the most up-to-date context.
- **Message history pruning** — the `messages` list grows unbounded within a session. For long conversations, this will hit the LLM context window limit or slow responses. A rolling window that keeps the system prompt, last N turns, and a summary of earlier turns would prevent this.
- **Obsidian backlinks** — the daily memory notes currently have no links between them. Adding tags and `[[wikilinks]]` to people, projects, and preferences mentioned would make the Obsidian vault navigable as a graph, not just a chronological log.

### Reliability & Robustness

- **Tool error recovery** — if a tool throws an exception, the raw error string is passed to the LLM as a tool result and it tries to report it gracefully. Better to add structured retry logic and a clean error template so the model always has something useful to work with.
- **Spotify device guard** — playback commands silently fail if no Spotify device is active. A pre-check that detects no active device and tells the user before attempting the command would prevent confusing silence.
- **Audio device error recovery** — the logs show `PaErrorCode -9986` (PortAudio internal error) causing the backend to restart. This appears intermittent (audio device contention). A graceful recovery that waits and retries instead of crashing the wake-word loop would make the backend more stable.
- **Directory cache staleness** — the file index is built once on startup and never auto-refreshes. Files created or moved after startup are invisible to the ComputerControlAgent. An inotify/FSEvents watcher that marks the cache dirty on file system changes would keep it current.
- **Structured logging** — the current log mixes wake-word polling noise, active conversation turns, tool calls, and memory operations in one flat stream. Splitting into levels (DEBUG for ambient RMS, INFO for conversations, WARN/ERROR for failures) would make debugging much faster.
=======
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
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141
