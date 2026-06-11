# CLAUDE.md — Jarvis AI

Personal voice-controlled AI assistant running entirely on local infrastructure (Mac Silicon).
Users speak → MLX Whisper transcribes → Ollama/qwen2.5 reasons + calls tools → Kokoro speaks back.

---

## Project Structure

```
jarvis_ai/
├── jarvis.py          # Main loop: voice pipeline, intent routing, memory, TTS
├── agents.py          # All tool/agent class definitions
├── credentials.json   # Google OAuth2 credentials (never commit)
├── token.json         # Google OAuth2 token (auto-generated, never commit)
├── .spotify_token     # Spotify token cache (auto-generated)
├── .env               # All API keys (never commit)
└── directory_cache.json  # Auto-built file system index (~/.jarvis_ai/directory_cache.json)
```

---

## Architecture

### Voice Pipeline (jarvis.py)
```
Always-on background listener (pvporcupine)
        ↓  "Jarvis" detected
    🔔 chime → record command (mlx_whisper) → transcribed text
                                                      ↓
                                            classify_intent()  [llama3.2:1b]
                                                      ↓
                                  ┌──────────────────┼──────────────────┐
                                exit               tool                chat
                                  ↓                  ↓                   ↓
                          save memories      "Right away sir"     qwen2.5:14b
                          + reset session    + tool loop          direct reply
                                  ↓                  ↓
                           Pinecone upsert    summarise results
                           return to idle           ↓
                                             Kokoro TTS → sounddevice
                                                      ↓
                                                  🔔 chime → back to idle
```

### Intent Classification
Three intents routed by `classify_intent()` using `llama3.2:1b` (fast, local):
- **`exit`** — user is ending the session → farewell + extract + store memories → break
- **`tool`** — needs real-world action (weather, calendar, Spotify, email, computer) → tool loop
- **`chat`** — general Q&A or reasoning → direct `qwen2.5:14b` reply, no tools

### Tool Execution Loop (intent == 'tool')
1. Inject today's date into the user message
2. Call `qwen2.5:14b` with full tools list
3. If `tool_calls` present: execute each via `available_functions[name](**args)`
4. Append each result as a `role: tool` message
5. Ask model to summarise results in Jarvis's voice
6. Speak the summary via Kokoro

### Memory System (Pinecone)
- **Storage**: Pinecone dense index `jarvis-memory-namespace`, model `llama-text-embed-v2`
- **Retrieval**: `retrieve_memories(query)` runs on every turn — top-5 hits injected into system prompt
- **Extraction**: On `exit`, `extract_important_messages()` uses `qwen2.5:14b` to distil the session into facts
- **What gets stored**: preferences, habits, personal facts, goals — NOT small talk or one-off lookups

---

## Models in Use

| Model | Purpose | Runtime |
|---|---|---|
| `qwen2.5:14b` | Main reasoning, tool calling, summarisation, memory extraction | Ollama (local) |
| `llama3.2:1b` | Intent classification only (fast) | Ollama (local) |
| `mlx-community/whisper-small-mlx` | Speech-to-text transcription | MLX (Apple Silicon) |
| `hexgrad/Kokoro-82M` | Text-to-speech output | Kokoro (local) |
| `llama-text-embed-v2` | Memory embeddings in Pinecone | Pinecone hosted |

### Planned (Not Yet Active)
- **ElevenLabs TTS** (`eleven_turbo_v2_5`, voice `k7IRoeykhdGZUkTeJ1ID`) — final release TTS, replaces Kokoro
- **ElevenLabs STT** (`scribe_v2`) — final release transcription, replaces MLX Whisper

---

## Agent Classes (agents.py)

Each class is independently instantiated in `jarvis.py`. Methods are passed directly to Ollama as tools.

| Class | Tools | External Service |
|---|---|---|
| `Calendar_Agents` | `create_event`, `get_calendar_events`, `update_calendar_event`, `delete_calendar_event` | Google Calendar API |
| `WebSearchAgents` | `search_web`, `extract_webpages` | Tavily API |
| `WeatherSearch` | `get_current_weather`, `get_weather_with_time`, `get_daily_forecast`, `get_weather_alerts` | OpenWeatherMap API (One Call 3.0) |
| `SpotifyAgent` | `get_current_track`, `search_song_and_queue`, `create_playlist`, `add_song_to_playlist`, `recently_played`, `skip_song`, `pause_song`, `shuffle`, `set_volume` | Spotify API (spotipy) |
| `GmailAgent` | `send_email`, `search_email`, `get_unread_emails`, `get_email_by_id`, `reply_to_email`, `mark_as_read`, `trash_email`, `remove_email_from_trash`, `get_drafts`, `get_sent_emails`, `get_sender_profile`, `get_all_labels` | Gmail API |
| `ComputerControlAgent` | `open_application`, `close_application`, `switch_application`, `list_open_applications`, `open_file`, `create_file`, `delete_file`, `move_file` | macOS (subprocess, AppKit, osascript) |

### Adding a New Agent
1. Define a class in `agents.py` with methods that have clear docstrings — Ollama uses these as tool descriptions
2. Instantiate it at the top of `jarvis.py` alongside the other agents
3. Add all methods to both `available_functions` dict AND the `tools=[]` list in the tool call block
4. Both locations must stay in sync — missing from either breaks tool execution

---

## Key Behaviours & Constraints

### Voice Output Rules
- Kokoro loads async on startup via `threading.Thread` — `kokoro_ready.wait()` blocks main loop until ready
- All TTS goes through `safe_speak()` — never call `play_audio_with_kokoro()` directly; it skips empty string protection
- Response must be conversational prose — no markdown, no bullet points, no headers (these are spoken aloud)
- A chime plays after every Jarvis response so the user knows when to speak

### Microphone / Transcription Settings
- Energy threshold: `200` (intentionally low — quiet environments)
- Pause threshold: `1.5s` — wait this long after speech stops before transcribing
- Phrase time limit: `45s` max per utterance
- Timeout: `10s` waiting for speech to start
- Do not change these without testing; they affect latency and false triggers significantly

### Message History
- `messages` list persists for the entire session in memory
- System prompt is at `messages[0]` — memory retrieval mutates it each turn by appending the memory block
- Tool results use `role: tool` with `tool_name` field (Ollama format, not OpenAI format)
- No persistence between sessions — Pinecone is the only cross-session memory

### ComputerControlAgent Index
- Indexes the entire home directory on first run, cached at `~/jarvis_ai/directory_cache.json`
- Startup blocks `computer._index_thread.join()` before entering main loop
- Call `computer.refresh_index()` if the file system has changed significantly
- Skipped directories: `.git`, `.venv`, `__pycache__`, `node_modules`, `.Trash`, `Library`, `.cache`, `.npm`, `.conda`

### Google OAuth
- Shared credentials across Calendar and Gmail via `get_google_creds()`
- Scopes: full calendar + gmail read/send/modify/labels
- Token cached in `token.json` — auto-refreshes when expired
- `credentials.json` must be present at project root

### Spotify Auth
- Token cached at `.spotify_token`
- If no cached token on startup, browser opens for login — paste redirect URL when prompted
- Requires an active Spotify device before playback/queue commands will work

---

## Environment Variables (.env)

```
TAVILY_API_KEY=
OPENWEATHER_API_KEY=
SPOTIPY_CLIENT_ID=
SPOTIPY_CLIENT_SECRET=
SPOTIPY_REDIRECT_URI=
PINECONE_API_KEY=
ELEVENLABS_API_KEY=        # Not active yet — reserved for final release
```

---

## Division of Labour

### Ruban and CLAUDE writes
- `jarvis.py` main loop logic and intent routing
- Memory retrieval and storage logic
- Voice pipeline orchestration (record → transcribe → speak)
- Any new agent class in `agents.py`
- Tool method implementations inside agent classes
- System prompt tuning

### Claude Code generates
- Boilerplate method scaffolding inside new agent classes
- `available_functions` dict entries and `tools=[]` list entries when adding new agents
- Helper/utility functions (formatters, parsers, error handlers)
- New agent class shells following the existing pattern

---

## Development Notes

### Current Limitations to Be Aware Of
- No multi-turn tool chaining — if a task requires tool A's output to feed tool B, the model must handle this in a single `tool_calls` response
- `classify_intent()` with `llama3.2:1b` occasionally misclassifies ambiguous requests — check intent logs if a tool isn't firing when expected
- Memory retrieval injects into system prompt on every turn, which grows `messages[0]` over a long session
- ElevenLabs functions exist but are commented out — do not remove them, they are the target production audio stack

### Planned Improvements
- Migrate main LLM from Ollama → Anthropic Claude API (same pattern as AreaCompAgent migration)
- Activate ElevenLabs TTS + STT for final release quality
- Add menu bar app (`rumps`) — status icon that reflects idle/listening/thinking/speaking state
- Add `research()` and `crawl_webpages()` to `WebSearchAgents` (stubs already exist)
- Replace `zero-shot-classification` intent classifier (commented out) — current `llama3.2:1b` approach is the replacement
- Improve chime sound quality (`play_chime()`)
- Connect Obsidian vault as human-readable memory backend (currently Pinecone only)
- Add multi-turn tool chaining (currently one tool pass per command)
- Add message history pruning to prevent `messages[0]` bloat over long sessions

### Running the Project
```bash
# Ensure Ollama is running with required models pulled
ollama pull qwen2.5:14b
ollama pull llama3.2:1b

# Ensure Spotify has an active device open before starting

python jarvis.py
```
Startup sequence: Kokoro loads (background thread) → ComputerControl indexes (background thread) → both join → main loop starts.