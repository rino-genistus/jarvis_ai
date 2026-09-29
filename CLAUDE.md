# CLAUDE.md — Jarvis AI

Personal voice-controlled AI assistant running locally on Apple Silicon.
User speaks → MLX Whisper transcribes → Ollama/qwen2.5:7b reasons and calls tools → Kokoro speaks back.

See `PROGRESS.md` for the public summary of what's built and what's next. Keep the two in sync.

---

## Project Structure

```
jarvis_ai/
├── jarvis.py          # Main loop: voice pipeline, intent + tool routing, memory, TTS
├── agents.py          # All agent classes; every public method is a tool
├── tests.py           # Test harness (structural, routing, tool choice, latency, voice)
├── requirements.txt   # Python dependencies
├── PROGRESS.md        # Public progress summary and roadmap
├── credentials.json   # Google OAuth2 client (never commit)
├── token.json         # Google OAuth2 token (auto-generated, never commit)
├── .spotify_token     # Spotify token cache (auto-generated, never commit)
└── .env               # API keys (never commit)
```

---

## Architecture

### Voice Pipeline (jarvis.py)
```
startup(): Kokoro loads + Ollama/Whisper prewarm in parallel
        ↓
record_audio_and_transcribe_mlx_whisper()   speech_recognition mic → whisper-small-mlx
        ↓
classify_intent() [llama3.2:1b]  ║  retrieve_memories() [Pinecone]    ← run in parallel
        ↓
   ┌────────────────┼────────────────┐
 exit              tool             chat
   ↓                ↓                ↓
farewell      "Right away sir."   qwen2.5:7b
+ extract     select_tools()      streamed reply
+ upsert      → tool call
+ exit        → summarise
        ↓
speak_stream() → say() → speaker thread → Kokoro → sounddevice → chime
```

There is no wake word yet: the loop starts recording immediately, and the `exit` intent ends the process.

### Intent Classification
`classify_intent()` uses `llama3.2:1b` (4 tokens max) to return one of:
- **`exit`**: ending the session → streamed farewell, extract memories, upsert to Pinecone, `break`
- **`tool`**: needs real-world action or data → tool routing + tool call
- **`chat`**: general conversation → streamed `qwen2.5:7b` reply, no tools

### Tool Routing (intent == 'tool')
Sending all 45 tool schemas costs about 3,600 prompt tokens and makes qwen2.5:7b ignore tools. `select_tools()` narrows it:
1. `route_tools()`: keyword match against `GROUP_KEYWORDS`, with no LLM call
2. `classify_tool_group()`: `llama3.2:1b` picks a group if no keyword matched
3. Fallback: `ALL_TOOLS`

If the routed group yields no tool call, the call is retried once with `ALL_TOOLS`.

### Tool Execution
1. Prefix the user message with today's date and the named dates of the coming week (qwen gets weekdays wrong from an ISO date alone)
2. Call `qwen2.5:7b` with the selected tools
3. Execute each `tool_call` via `TOOL_REGISTRY[name](**args)`; exceptions become a text result instead of crashing
4. Append results as `role: tool` messages with `tool_name` (Ollama format)
5. Ask for a two-sentence summary **with the same tools list** (keeps Ollama's KV cache prefix; a different prefix cost 13.5s on the next command)
6. Stream the summary to speech

### Memory System (Pinecone)
- **Index**: `jarvis-ai`, namespace `jarvis-memory-namespace`, integrated embedding `llama-text-embed-v2`, `field_map {"text": "chunk_text"}`
- **Records**: `{"id": "mem-<ts>-<i>", "chunk_text": ...}`. The field name must stay `chunk_text` to match the field map.
- **Retrieval**: `retrieve_memories(query, top_k=5)` on every turn. Hits are prepended to the **user message**, never `messages[0]`; rewriting the system prompt invalidated Ollama's prefix cache (measured 13.8s penalty).
- **Extraction**: only on `exit`, synchronously, via `extract_important_messages()` with `qwen2.5:7b`. Stores preferences, habits, personal facts and goals, not small talk or one-off lookups.

---

## Models in Use

| Model | Purpose | Runtime |
|---|---|---|
| `qwen2.5:7b` | Reasoning, tool calling, summarisation, memory extraction | Ollama (local) |
| `llama3.2:1b` | Intent classification + tool-group fallback | Ollama (local) |
| `mlx-community/whisper-small-mlx` | Speech-to-text | MLX (Apple Silicon) |
| `hexgrad/Kokoro-82M` (voice `af_heart`) | Text-to-speech | Kokoro (local) |
| `llama-text-embed-v2` | Memory embeddings | Pinecone hosted |

Ollama models use `keep_alive=-1` so they stay resident. Spoken replies are capped with `GEN_OPTIONS = {"num_predict": 160}`.

### Wired Up but Inactive
- **ElevenLabs TTS** (`eleven_turbo_v2_5`, voice `k7IRoeykhdGZUkTeJ1ID`): `play_audio_with_text_eleven_labs()`
- **ElevenLabs STT** (`scribe_v2`): `record_audio_and_transcribe_elevenlabs()`

Unused for cost reasons. Do not remove them: they are the target production audio stack.

---

## Agent Classes (agents.py)

45 tools across 6 agents. Every public bound method becomes a tool; methods starting with `_` are internal helpers.

| Class | Group | Tools | Service |
|---|---|---|---|
| `Calendar_Agents` | calendar | `create_event`, `get_calendar_events`, `update_calendar_event`, `delete_calendar_event` | Google Calendar API |
| `WebSearchAgents` | web | `search_web`, `extract_webpages`, `crawl_webpages`, `research` | Tavily |
| `WeatherSearch` | weather | `get_current_weather`, `get_weather_with_time`, `get_daily_forecast`, `get_weather_alerts` | OpenWeatherMap One Call 3.0 |
| `SpotifyAgent` | music | `get_current_track`, `search_song_and_queue`, `create_playlist`, `add_song_to_playlist`, `recently_played`, `skip_song`, `previous_song`, `pause_song`, `resume_song`, `shuffle`, `set_volume` | Spotify (spotipy) |
| `GmailAgent` | email | `send_email`, `search_email`, `get_unread_emails`, `get_email_by_id`, `reply_to_email`, `mark_as_read`, `mark_as_unread`, `trash_email`, `remove_email_from_trash`, `get_drafts`, `send_draft`, `get_sent_emails`, `get_sender_profile`, `get_all_labels` | Gmail API |
| `RemindersAgent` | reminders | `get_reminder_lists`, `add_reminder`, `get_reminders`, `get_due_reminders`, `complete_reminder`, `delete_reminder`, `update_reminder`, `create_reminder_list` | macOS Reminders via JXA |

### Adding a New Agent
1. Define a class in `agents.py`. Docstrings and type hints become the tool description Ollama sees, so write them for the model.
2. Instantiate it in `jarvis.py` and add it to `AGENTS`.
3. Add its class name to `GROUP_BY_CLASS` (startup raises if missing) and its trigger words to `GROUP_KEYWORDS`.
4. Tool names must be unique across all agents: `build_tool_registry()` raises on duplicates.

There is no hand-maintained tool list. `TOOL_REGISTRY`, `TOOL_GROUPS` and `ALL_TOOLS` are all derived from `AGENTS`.

---

## Key Behaviours & Constraints

### Voice Output
- A single speaker thread (`_speaker_worker`) owns one persistent output stream and drains `_SPEAK_Q` in order.
- `say(text)` queues speech and returns immediately. `safe_speak()` adds empty-string protection. `chime()` queues the "your turn" tone.
- `speak_stream()` flushes streamed LLM output one sentence at a time. It splits only on punctuation **followed by whitespace**, so `72.4` is not cut.
- `wait_until_spoken()` must run before recording, or the mic hears Jarvis.
- Responses are spoken: conversational prose only, with no markdown, bullets or headers.

### Microphone / Transcription Settings
- Energy threshold `200`, `dynamic_energy_threshold = False` after a 0.3s calibration
- Pause threshold `0.8s` (was 1.5s; dead air is felt directly as latency)
- Timeout `10s` waiting for speech; phrase limit `45s`
- Do not change these without testing: they affect latency and false triggers.

### Message History
- `messages` persists for the whole session; the system prompt at `messages[0]` must stay byte-stable for prefix caching.
- No persistence between sessions except Pinecone.

### Auth & Permissions
- **Google**: `get_google_creds()` is shared by Calendar and Gmail (calendar + gmail read/send/modify/labels scopes). `token.json` auto-refreshes; if refresh fails (Google revokes after 7 days in Testing mode), a browser login runs. `credentials.json` must be at the project root.
- **Spotify**: token at `.spotify_token`; the first run opens a browser login. Playback commands need an active Spotify device.
- **Reminders**: needs Automation permission for the terminal/Python under System Settings → Privacy & Security → Automation. User text is passed as JSON argv, never concatenated into the script.

---

## Environment Variables (.env)

```
TAVILY_API_KEY=
OPENWEATHER_API_KEY=
SPOTIPY_CLIENT_ID=
SPOTIPY_CLIENT_SECRET=
SPOTIPY_REDIRECT_URI=
PINECONE_API_KEY=
ELEVENLABS_API_KEY=        # Inactive, reserved for final release
```

---

## Testing

```bash
python tests.py              # everything except the microphone test
python tests.py --quick      # structural checks only, no model calls
python tests.py voice        # interactive microphone check
python tests.py cache tools  # named suites only
python tests.py --list       # show suite names
```

Tool suites check which tool the model **chooses** and never execute it, so they are safe against live accounts. `jarvis.py` is guarded by `if __name__ == "__main__"` so tests can import it.

---

## Division of Labour

### Ruban and Claude write
- `jarvis.py` main loop logic and intent routing
- Memory retrieval and storage logic
- Voice pipeline orchestration (record → transcribe → speak)
- Any new agent class in `agents.py`
- Tool method implementations inside agent classes
- System prompt tuning

### Claude Code generates
- Boilerplate method scaffolding inside new agent classes
- `GROUP_BY_CLASS` and `GROUP_KEYWORDS` entries when adding new agents
- Helper/utility functions (formatters, parsers, error handlers)
- New agent class shells following the existing pattern

---

## Known Limitations
- No wake word; `exit` intent ends the process.
- `r.listen(timeout=10)` raises `WaitTimeoutError` on silence, which is uncaught in `main_loop()`, so the session ends without saving memories.
- One tool pass per command: tool A's output cannot feed tool B.
- `messages` grows unbounded within a session, and the summarise instruction stays in history.
- The system prompt says Jarvis has no memory between sessions, which contradicts the Pinecone memories injected each turn.
- Weather tools need latitude/longitude and there is no geocoding tool, so the model guesses coordinates.
- The all-tools retry also fires when the model correctly answers without a tool.

## Roadmap (mirrors PROGRESS.md)
1. **Always-on wake word** (pvporcupine or openWakeWord): idle → conversation → idle, instead of exiting.
2. **Multi-step agent loop**: keep calling tools until the model stops, capped, so results can chain.
3. **Memory upgrades**: cross-session summaries, recency-weighted recall, history trimming, background extraction.
4. **Platform features**: restore ComputerControlAgent (macOS app/file control, recoverable from commit `3027c57`), menu-bar status icon, ElevenLabs voice for the final release.

Also under consideration: migrating the main LLM from Ollama to the Claude API for more reliable tool choice.

---

## Running the Project
```bash
pip install -r requirements.txt

# Ollama must be running with these models pulled
ollama pull qwen2.5:7b
ollama pull llama3.2:1b

# Open Spotify on a device before using music commands

python jarvis.py
```
Startup sequence: Kokoro loads (background thread) while `prewarm()` loads both Ollama models and Whisper → both join → `main_loop()` starts.
