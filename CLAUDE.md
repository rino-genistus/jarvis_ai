# CLAUDE.md — Jarvis AI

Personal voice-controlled AI assistant running locally on Apple Silicon.
"Hey Jarvis" → MLX Whisper transcribes → Ollama/qwen2.5:7b reasons and calls tools → Kokoro speaks back.

See `PROGRESS.md` for the public summary of what's built and what's next. Keep the two in sync.

---

## Project Structure

```
jarvis_ai/
├── wake_listener.py   # Always-on entry point (what Jarvis.app runs): wake word + engine lifecycle
├── wake_word.py       # Lean "Hey Jarvis" detector: openWakeWord's ONNX models on onnxruntime
├── jarvis.py          # Engine: conversation loop, intent + tool routing, memory, TTS
├── mic.py             # Command recording (light imports, usable before jarvis.py loads)
├── obsidian_store.py  # Daily memory notes in the Obsidian vault
├── agents.py          # All agent classes; every public method is a tool
├── tests.py           # Test harness
├── build_app.sh       # Builds Jarvis.app and installs it into Applications
├── assets/            # make_icon.py (draws the icon) and the generated Jarvis.icns
├── requirements.txt   # Python dependencies (Python 3.12 — Kokoro doesn't support 3.13+)
├── PROGRESS.md        # Public progress summary and roadmap
├── credentials.json   # Google OAuth2 client (never commit)
├── token.json         # Google OAuth2 token (auto-generated, never commit)
├── .spotify_token     # Spotify token cache (auto-generated, never commit)
├── .env.example       # Template for .env, with where to get each key
└── .env               # API keys and settings, all optional (never commit)
```

---

## Architecture

### Two processes (wake_listener.py)
```
wake listener — always running, ~95 MB, ~10% of one efficiency core (8 ms per 80 ms frame, mostly the embedding model)
    mic (sounddevice, 16 kHz, 80 ms frames) → WakeWordDetector.score()
        ↓  score ≥ JARVIS_WAKE_THRESHOLD
    rising wake tone → hand off the mic to the engine, wait for "idle"
        ↓
engine process — spawned on first wake, ~2.7 GB warm (+ Ollama models)
    cold: records the first command while jarvis.py imports and prewarms (~6 s)
    warm: starts recording immediately
    run_session() → "idle" → waits for the next "wake"
        ↓  no wake for JARVIS_IDLE_UNLOAD_MINUTES
    listener sends "quit" → engine flushes memory writes, unloads Ollama models, exits
```
- The pipe protocol is three strings: listener → engine `"wake"` / `"quit"`, engine → listener `"idle"`.
- Retiring the engine exits the process, so memory is returned to the OS, not just freed inside Python.
- The engine exits by itself if the listener disappears (EOF or broken pipe on the pipe). The listener turns SIGTERM into a clean shutdown, so `pkill` doesn't orphan the engine.
- An engine crash plays a low error tone, and the listener keeps running. The next wake word spawns a fresh engine.
- A file lock (`logs/jarvis.lock`) prevents two listeners fighting over the microphone.
- Clicking the app shows a macOS notification, since there is no window or Dock icon:
  - "Jarvis is listening" when it starts. If `credentials.json` or `.env` is missing (`SETUP_FILES`), the notification names the services that are off.
  - "already running" if it is running already.
- A PortAudio error on the wake stream (for example `-9986`) is retried after 2 s instead of crashing.

### Wake Word (wake_word.py)
- A port of openWakeWord's streaming path, using the `hey_jarvis` pretrained model. It is verified to produce identical scores. Importing the `openwakeword` package pulls in scipy and scikit-learn (about 75 MB) for training code, so the listener never imports it. It only reads the model files, and downloads them in a subprocess on first run.
- A Silero VAD gate is built in (`vad_threshold=0.5`): a score only counts if there was speech 0.32–0.56 s earlier. This cuts false triggers from music and games.
- The wake phrase is **"Hey Jarvis"**. `reset()` must be called after each detection or it retriggers.

### Conversation (jarvis.py)
```
run_session(first_audio=None)
  loop:
    wait_until_spoken() → mic.record_command()     None after 10 s silence → end
        ↓
    transcribe() [whisper-small-mlx, from a numpy array — no ffmpeg or temp file]
        ↓
    handle_turn(text, transcript) → True on exit intent → end
  end: falling sleep tone → clear history → remember_in_background(transcript)
```

`handle_turn`:
```
classify_intent() [llama3.2:1b]  ║  retrieve_memories() [Pinecone]    ← run in parallel
        ↓
   ┌────────────────┼────────────────┐
 exit              tool             chat
   ↓                ↓                ↓
streamed      acknowledgement()   qwen2.5:7b
farewell      select_tools()      streamed reply
              → tool call
              → summarise
        ↓
speak_stream() → say() → speaker thread → Kokoro → sounddevice → chime
```

### Intent Classification
`classify_intent()` uses `qwen2.5:7b` with worked examples (`INTENT_PROMPT`, temperature 0, 3 tokens) to return one of:
- **`exit`**: the user is ending the conversation → streamed farewell, then `run_session` ends it
- **`tool`**: needs real-world action or data → tool routing and a tool call
- **`chat`**: general conversation → streamed `qwen2.5:7b` reply, no tools

`decide_intent(text, classified)` then applies two guards:
- A **request** (`looks_like_request()`: a `?`, a command verb up front, or an embedded "remind me" / "can you") whose keywords point at a lookup group (`LOOKUP_GROUPS`: weather, calendar, email, reminders, web) becomes `tool` even if the classifier said `chat`. Music is excluded, because "skip" and "play" turn up in ordinary talk.
- A **statement** is never `tool`. Offered calendar tools for "my sister's birthday is next week", the model may create an event nobody asked for.

Measured on the 29 labelled phrases in `tests.py` (`INTENT_CASES`): the old `llama3.2:1b` classifier scored 13/29 and never recognised a goodbye; the qwen classifier plus the guards score 28/29, for about 170 ms more per turn. Run `python tests.py intent` after changing either.

### Tool Routing (intent == 'tool')
Sending all 45 tool schemas costs about 3,600 prompt tokens and makes qwen2.5:7b ignore tools. `select_tools()` narrows the set:
1. `route_tools()`: keyword match against `GROUP_KEYWORDS`, with no LLM call
2. `classify_tool_group()`: `llama3.2:1b` picks a group if no keyword matched
3. Fallback: `ALL_TOOLS`

If the routed group yields no tool call:
1. **`DEFAULT_CALLS`**: if the text is a request and the group has an obvious action, Jarvis runs it itself. Weather runs `get_current_weather(location="home")`; music turns "play X" into `search_song_and_queue(query=X)`. This exists because qwen2.5:7b narrates ("I'll check the weather at home…") or declines "play some jazz", even at temperature 0.
2. Otherwise, the call is retried once with `ALL_TOOLS`.

### Tool Execution
1. Prefix the user message with `date_reference()`: today's date and the named dates of the coming week (qwen gets weekdays wrong from an ISO date alone). The chat path gets the same prefix. Both use a copy of the last message, so the stored history, and the prompt cache, are unchanged.
2. Speak `acknowledgement(group)`, a short phrase fitting the group ("Let me check your calendar.", "Pulling up the forecast."), never the same one twice in a row. It is queued, so it plays over the model call. A disabled group gets `not_set_up_reply()` instead.
3. Call `qwen2.5:7b` with the selected tools.
4. Execute each `tool_call` via `TOOL_REGISTRY[name](**args)`. Exceptions become a text result instead of crashing.
5. Append results as `role: tool` messages with `tool_name` (Ollama format).
6. Ask for a two-sentence summary **with the same tools list**. This keeps Ollama's KV cache prefix; a different prefix cost 13.5 s on the next command.
7. Stream the summary to speech.

### Memory System
Each session is saved once it ends, on a background thread (`remember_in_background`). The thread is non-daemon and tracked, so `shutdown()` / `flush_memories()` wait for it.

**Extraction** is one `qwen2.5:7b` call, `extract_session_memory()`, with `format="json"`. It runs on the raw `(speaker, text)` transcript rather than `messages`, so injected memory notes aren't re-extracted. It returns:
- `title`: three to six words.
- `summary`: three to five sentences with the specifics (names, places, numbers, dates), written about "the user" with no guessed pronouns.
- `facts`: lasting, standalone sentences. Facts about other people name them ("The user's sister Priya loves jazz.").
- `topics`: up to six `{name, note}` for the people, places, projects and interests discussed. These are what link memories together.

Rules that came from observed failures:
- There is no up-front "worth remembering" flag: qwen dismissed a session full of personal facts when asked first. A session is skipped when it yields **no facts and no topics**; qwen always writes some summary, even for a bare greeting.
- The model sees only the existing topics that `relevant_topics()` finds in the conversation (any 4+ letter word of the name). Showing every topic made it attach unrelated ones.
- `date_reference(15)` is in the prompt so relative dates resolve to calendar dates.

**Pinecone** (search):
- Index `jarvis-ai`, namespace `jarvis-memory-namespace`, integrated embedding `llama-text-embed-v2`, `field_map {"text": "chunk_text"}`.
- Records are `{"id": "mem-<ts>-<i>" | "episode-<ts>", "chunk_text": ...}`. The field must stay `chunk_text`. The whole summary is stored as `"On <date>: <summary>"`, so recall brings back context, not a one-liner.
- `retrieve_memories(query, top_k=5)` runs on every turn. Hits are prepended to the **user message**, never `messages[0]`; rewriting the system prompt invalidated Ollama's prefix cache (measured 13.8 s penalty).

**Obsidian** (the knowledge graph the user browses), vault default `~/Desktop/Jarvis AI`:
- `Memory/YYYY-MM-DD.md`: `## Session (HH:MM) — <title>`, the summary, `### Facts learned`, then `### Topics` as `[[wikilinks]]`.
- `Memory/Entities/<Topic>.md`: one note per topic, gaining a line `- [[YYYY-MM-DD]] HH:MM — <note>` every session that mentions it. Name matching is case-insensitive, so "toronto" joins "Toronto". Characters that break links (`/ : # [ ]` and so on) are stripped.
- Days link to topics and topics link back to days, so the graph connects related sessions on its own.
- A missing vault is skipped, never created.

Pinecone and Obsidian fail independently; one failing is logged and doesn't stop the other.

---

## Models in Use

| Model | Purpose | Runtime |
|---|---|---|
| `hey_jarvis_v0.1` + melspectrogram, embedding, Silero VAD | Wake word | onnxruntime (listener process) |
| `qwen2.5:7b` | Intent classification, reasoning, tool calling, summarisation, memory extraction | Ollama (local) |
| `llama3.2:1b` | Tool-group fallback when no keyword matches | Ollama (local) |
| `mlx-community/whisper-small-mlx` | Speech-to-text | MLX (Apple Silicon) |
| `hexgrad/Kokoro-82M` (voice `af_heart`) | Text-to-speech | Kokoro (local) |
| `llama-text-embed-v2` | Memory embeddings | Pinecone hosted |

- Ollama `keep_alive` is `JARVIS_IDLE_UNLOAD_MINUTES + 5` minutes, not forever. `shutdown()` unloads the models explicitly (`generate(..., keep_alive=0)`), and the bound frees them even after a crash.
- Spoken replies are capped with `GEN_OPTIONS = {"num_predict": 160}`.

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
| `WeatherSearch` | weather | `get_current_weather`, `get_weather_with_time`, `get_daily_forecast`, `get_weather_alerts` | OpenWeatherMap One Call 3.0 + Geocoding |
| `SpotifyAgent` | music | `get_current_track`, `search_song_and_queue`, `create_playlist`, `add_song_to_playlist`, `recently_played`, `skip_song`, `previous_song`, `pause_song`, `resume_song`, `shuffle`, `set_volume` | Spotify (spotipy) |
| `GmailAgent` | email | `send_email`, `search_email`, `get_unread_emails`, `get_email_by_id`, `reply_to_email`, `mark_as_read`, `mark_as_unread`, `trash_email`, `remove_email_from_trash`, `get_drafts`, `send_draft`, `get_sent_emails`, `get_sender_profile`, `get_all_labels` | Gmail API |
| `RemindersAgent` | reminders | `get_reminder_lists`, `add_reminder`, `get_reminders`, `get_due_reminders`, `complete_reminder`, `delete_reminder`, `update_reminder`, `create_reminder_list` | macOS Reminders via JXA |

### Weather Locations
Weather tools take a **place name** (`location`), not coordinates:
- `_locate()` geocodes it with OpenWeather's geocoding API (same key) and caches the result.
- `"home"`, `"here"` or `""` mean `JARVIS_HOME_LOCATION` from `.env`. If that isn't set, the tool returns an error asking which city.
- Results carry the resolved `location` label. Forecast days are labelled with their date, plus "(today)" / "(tomorrow)".
- `get_daily_forecast` always returns at least 3 days, because the model passes `days=1` for "tomorrow".

### Optional Services
Every agent is optional. `start_agents()` walks `AGENT_REQUIREMENTS`, a list of `(class, [env vars or *.json files])` pairs:
- An agent missing any requirement is left out of `AGENTS`, so its tools never reach the model.
- An agent whose constructor raises is left out too, and the rest still start.
- The reason is recorded in `DISABLED_AGENTS` by class and `DISABLED_GROUPS` by group.

When a tool request routes to a disabled group, `handle_turn` speaks `not_set_up_reply()` ("Weather isn't set up yet, sir…") **before** the acknowledgement, instead of letting the model invent an answer. The reply avoids spelling out env var names, because Kokoro reads them letter by letter.

With nothing configured at all, the voice pipeline, chat, Reminders and Obsidian memory still work.

### Adding a New Agent
1. Define a class in `agents.py`. Docstrings and type hints become the tool description Ollama sees, so write them for the model.
2. Add it to `AGENT_REQUIREMENTS` in `jarvis.py` with the env vars or files it needs.
3. Add its class name to `GROUP_BY_CLASS` (startup raises if it's missing), its trigger words to `GROUP_KEYWORDS`, and its spoken name to `SERVICE_NAMES`.
4. Tool names must be unique across all agents: `build_tool_registry()` raises on duplicates.

There is no hand-maintained tool list. `TOOL_REGISTRY`, `TOOL_GROUPS` and `ALL_TOOLS` are all derived from `AGENTS`.

---

## Key Behaviours & Constraints

### Audio Cues
- **Rising two-note tone** (listener): the wake word was heard; start talking.
- **Single 880 Hz chime** (engine): Jarvis finished answering; your turn.
- **Falling two-note tone** (engine): the conversation is over; Jarvis is back to waiting for the wake word.
- **Low double tone** (listener): the engine crashed; check `logs/jarvis.log`.

### Voice Output
- A single speaker thread (`_speaker_worker`) owns one persistent output stream and drains `_SPEAK_Q` in order.
- `say(text)` queues speech and returns immediately. `safe_speak()` adds empty-string protection. `chime()` and `sleep_tone()` queue the cues.
- `speak_stream()` flushes streamed LLM output one sentence at a time. It splits only on punctuation **followed by whitespace**, so `72.4` is not cut.
- `wait_until_spoken()` must run before recording, or the mic hears Jarvis.
- Responses are spoken: conversational prose only, with no markdown, bullets or headers.

### Microphone / Transcription Settings (mic.py)
- Energy threshold `200`, fixed (`dynamic_energy_threshold = False`)
- Pause threshold `0.8 s` (was 1.5 s; dead air is felt directly as latency)
- `LISTEN_TIMEOUT = 10 s` of silence ends the conversation; phrase limit `45 s`
- Returns 16 kHz float32 numpy arrays; Whisper takes them directly
- Do not change these without testing: they affect latency and false triggers.

### Message History
- `messages` holds the current conversation only. `run_session` clears everything after `messages[0]` when a conversation ends.
- The system prompt at `messages[0]` must stay byte-stable for prefix caching.
- Cross-session memory comes only from Pinecone recall.

### Auth & Permissions
- **Google**: `get_google_creds()` is shared by Calendar and Gmail (calendar + gmail read/send/modify/labels scopes). `token.json` auto-refreshes; if refresh fails (Google revokes after 7 days in Testing mode), it opens a browser login. That blocks the background app, so complete logins by running `python jarvis.py` in a terminal. `credentials.json` must be at the project root.
- **Spotify**: token at `.spotify_token`; the first run needs a browser login in a terminal. Playback commands need an active Spotify device.
- **Microphone**: macOS asks the first time Jarvis.app opens the mic (`NSMicrophoneUsageDescription`). The ad-hoc code signature keeps the grant across rebuilds.
- **Reminders**: needs Automation permission under System Settings → Privacy & Security → Automation. User text is passed as JSON argv, never concatenated into the script.

---

## Environment Variables (.env)

`.env.example` is the template, with where to get each key: `cp .env.example .env`. All keys are optional; see Optional Services.

| Key | Turns on | Source |
|---|---|---|
| `PINECONE_API_KEY` | Memory recall (without it, Obsidian notes only) | app.pinecone.io |
| `OPENWEATHER_API_KEY` | Weather (needs the One Call 3.0 subscription) | home.openweathermap.org |
| `TAVILY_API_KEY` | Web search and research | app.tavily.com |
| `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SPOTIPY_REDIRECT_URI` | Spotify (the redirect URI uses `127.0.0.1`, not `localhost`) | developer.spotify.com/dashboard |
| `ELEVENLABS_API_KEY` | Nothing yet; reserved for final release | elevenlabs.io |
| `JARVIS_HOME_LOCATION` | City used when a weather question names no place, e.g. `Boston` | |
| `OBSIDIAN_VAULT_PATH` | Vault location, default `~/Desktop/Jarvis AI` | |
| `JARVIS_IDLE_UNLOAD_MINUTES` | How long the engine stays warm, default 10 | |
| `JARVIS_WAKE_THRESHOLD` | Wake sensitivity, default 0.5; raise it if Jarvis triggers falsely | |

Calendar and Gmail need `credentials.json` (a Google Cloud OAuth desktop client) in the project folder instead of a key.

---

## Testing

```bash
python tests.py              # everything except the microphone test
python tests.py --quick      # structural checks only, no model calls
python tests.py intent       # intent routing on 29 labelled phrases (needs Ollama)
python tests.py wake memory  # wake word + Obsidian notes; need no API keys, Ollama or jarvis.py
python tests.py voice        # interactive microphone check
python tests.py --list       # show suite names
```

- Tool suites check which tool the model **chooses** and never execute it, so they are safe against live accounts.
- The wake suite synthesises speech with macOS `say`, so it needs no fixtures or microphone.
- `jarvis.py` is guarded by `if __name__ == "__main__"` so tests can import it.

---

## Division of Labour

### Ruban and Claude write
- `jarvis.py` conversation logic and intent routing
- Memory retrieval and storage logic
- Voice pipeline orchestration (wake → record → transcribe → speak)
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
- **Relative dates in conversation are unreliable.** "Remind me Friday" resolves correctly in tool calls (tested). But in chat, qwen2.5:7b turned "next Friday" (from Tuesday September 29) into September 30 and later October 6, and memory extraction then records the wrong date. It needs deterministic date parsing or a stronger model.
- **The chat path can promise things it can't do**: "I've marked your flight", or "I'll use Celsius from now on" (weather units are fixed to imperial), because no tools are offered there.
- **Pinecone's free tier can run out.** When recall hits a quota or rate limit, it pauses for an hour (`RECALL_PAUSE_SECONDS`) instead of spending 3–4 s of client retries on every turn. Writes use a separate quota.
- One remaining intent miss in the test set: "Can you help me write a poem about rain?" routes to weather.
- One tool pass per command: tool A's output cannot feed tool B.
- `messages` grows unbounded within a conversation, and the summarise instruction stays in history.
- The all-tools retry also fires when the model correctly answers without a tool.
- A cold wake (engine retired) takes about 6 s before the first answer starts. The command itself is recorded during that time.
- The app bakes in the project and `.venv` paths; re-run `build_app.sh` after moving either.

## Roadmap (mirrors PROGRESS.md)
1. **Dates and honest chat**: deterministic parsing of relative dates, and keep the chat path from promising actions it can't take.
2. **Multi-step agent loop**: keep calling tools until the model stops, with a cap, so results can chain.
3. **Memory upgrades**: "last time we spoke" context at session start, recency-weighted recall, history trimming.
4. **Platform features**: restore ComputerControlAgent (macOS app/file control, recoverable from commit `3027c57`), menu-bar status icon, start at login, ElevenLabs voice for the final release.

Also under consideration: migrating the main LLM from Ollama to the Claude API for more reliable tool choice.

---

## Running the Project
```bash
# Python 3.12 environment
brew install uv portaudio
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# Ollama must be running with these models pulled
ollama pull qwen2.5:7b
ollama pull llama3.2:1b

# First run in a terminal: completes the Google and Spotify logins, one conversation, no wake word
.venv/bin/python jarvis.py

# Background assistant with wake word, in the foreground for debugging
.venv/bin/python wake_listener.py

# Or install the app (Launchpad / Spotlight / Applications)
bash build_app.sh
```
Logs: `logs/jarvis.log` (rotated at 5 MB). Stop: `pkill -f wake_listener.py`.
