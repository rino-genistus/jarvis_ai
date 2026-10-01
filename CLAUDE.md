# CLAUDE.md — Jarvis AI

Personal voice-controlled AI assistant running locally on Apple Silicon.
"Hey Jarvis" → Parakeet (MLX) transcribes → Ollama/qwen2.5:7b reasons and calls tools → Kokoro speaks back.

See `PROGRESS.md` for the public summary of what's built and what's next. Keep the two in sync.

---

## Project Structure

```
jarvis_ai/
├── wake_listener.py   # Always-on entry point (what Jarvis.app runs): wake word + engine lifecycle
├── wake_word.py       # Lean "Hey Jarvis" detector: openWakeWord's ONNX models on onnxruntime
├── jarvis.py          # Engine: conversation loop, intent + tool routing, memory, TTS
├── mic.py             # Command recording: speech-detector end of speech, room-fitted loudness (light imports)
├── memory_store.py    # On-device vector memory: Chroma + Ollama embeddings
├── obsidian_store.py  # Obsidian vault: daily notes, topic notes, keyword recall fallback
├── preferences.py     # Standing preferences (data/preferences.json, mirrored to Obsidian)
├── agents.py          # All agent classes; every public method is a tool
├── tool_catalog.py    # AGENT_TOOLS: each agent's tools, their trigger words and acknowledgements
├── status.py          # "Is Jarvis working?" report — CLI and the Jarvis Status app
├── tests.py           # Test harness
├── build_app.sh       # Builds Jarvis.app and Jarvis Status.app into Applications
├── data/              # On this Mac only (gitignored): chroma/ vector store, preferences.json
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
- Opening the app shows a "Jarvis is listening" notification, since there is no window or Dock icon. If `credentials.json` or `.env` is missing (`SETUP_FILES`), it names the services that are off.
- The pipe messages are `("wake", noise_floor)` / `("quit", None)` from the listener and `"idle"` from the engine. `noise_floor` is the median loudness of the last 30 s of mic frames; see Microphone.

### Health: status.json, status.py, Jarvis Status.app
- Once a minute (`HEARTBEAT_SECONDS`) the listener writes `logs/status.json`: state, engine warm/off, last wake and today's count, the minute's peak mic level and best wake score, the noise floor, and whether the mic is delivering pure silence. Pure silence is what macOS gives an app without microphone permission, and it is logged as a warning.
- `python status.py` checks every link: process alive, heartbeat fresh, mic level, Ollama running with the models, and any recent crash in the log. It exits 1 on a failure.
- **Jarvis Status.app** shows the same report as a dialog, with **Restart Jarvis** and **Open Log** buttons. It's a separate app because a second click on Jarvis.app never runs any code: macOS just brings the running app forward.
- A PortAudio error on the wake stream (for example `-9986`) is retried after 2 s instead of crashing.

### Wake Word (wake_word.py)
- A port of openWakeWord's streaming path, using the `hey_jarvis` pretrained model. It is verified to produce identical scores. Importing the `openwakeword` package pulls in scipy and scikit-learn (about 75 MB) for training code, so the listener never imports it. It only reads the model files, and downloads them in a subprocess on first run.
- A Silero VAD gate is built in (`vad_threshold=0.5`): a score only counts if there was speech 0.32–0.56 s earlier. This cuts false triggers from music and games.
- The wake phrase is **"Hey Jarvis"**. `reset()` must be called after each detection or it retriggers.

### Conversation (jarvis.py)
```
run_session(first_audio=None)
  start: messages[0] = build_system_prompt()   (dates + preferences)
         warm_conversation()   (Ollama reads it in the background while the user speaks)
  loop:
    wait_until_spoken() → mic.record_command(follow_up=...)
        None after 10 s silence → end;  NOISE (loud but not speech) → skip
        ↓
    transcribe() [Parakeet TDT 0.6B on MLX; Whisper small as fallback — numpy arrays, no ffmpeg]
        ↓  is_speech()? two non-speech results in a row → end
    handle_turn(text, transcript) → True on exit intent → end
  end: falling sleep tone → clear history → remember_in_background(transcript)
```

`handle_turn`:
```
clear lookup request? ── yes → select_tools() → acknowledgement() at ~0s → retrieve_memories() → tool
        │ no
classify_intent() [embeddings, ~10 ms]  ║  retrieve_memories() [Chroma → Obsidian]    ← run in parallel
        ↓
   ┌────────────────┼────────────────┐
 exit              tool             chat
   ↓                ↓                ↓
streamed      acknowledgement()   qwen2.5:7b
farewell      select_tools()      streamed reply
              → run_tool_loop():
                tool → result → next tool or answer
                (up to 4 rounds)
        ↓
speak_stream() → say() → speaker thread → Kokoro → sounddevice → chime
```

### Intent Classification
`classify_intent()` returns one of these in about 10 ms, with no LLM call:
- **`exit`**: the user is ending the conversation → streamed farewell, then `run_session` ends it
- **`tool`**: needs real-world action or data → tool routing and a tool call
- **`chat`**: general conversation → streamed `qwen2.5:7b` reply, no tools

How it decides:
- **Goodbyes** are recognised by their words (`GOODBYE`), and only those end a conversation. "That's it" counts only with "for now" or "thanks", so "that's it, I fixed the bug" isn't a goodbye.
- Otherwise the command is embedded with `nomic-embed-text` (`classification:` prefix) and compared with the labelled phrases in `INTENT_EXAMPLES`. Each label scores the mean similarity of its `INTENT_NEIGHBOURS` (3) closest examples.
- An `exit` reading becomes the runner-up: ending by mistake costs more than missing a goodbye, which ends on 10 s of silence anyway.
- A `tool` reading needs words that point at a service (`route_groups()`); otherwise it's `chat`.
- If the embedding model is unavailable, the rules alone decide: a request with service words is `tool`, anything else `chat`.

This replaced a `qwen2.5:7b` few-shot call. On the first 35 labelled phrases both scored 34/35. But qwen took ~220 ms and pushed the conversation out of Ollama's prompt cache, so the reply that followed re-read everything: routing measured 0.3–7 s. On 20 phrases written afterwards and scored unseen, qwen got 18/20 and the embeddings 16/20; the misses were missing keywords, now added. All 55 are in `INTENT_CASES`, and the current score is 54/55. `INTENT_EXAMPLES` is written separately from `INTENT_CASES`, so the test stays a test: add a phrase to the examples when a kind of command is misread.

`decide_intent(text, classified)` then applies two guards:
- A **stated preference** (keywords in the `preferences` group: "I prefer", "from now on", "call me", "I live in") is always `tool`, so it's saved immediately.
- A **request** (`looks_like_request()`: a `?`, a command verb up front, or an embedded "remind me" / "can you") whose keywords point at a lookup group (`LOOKUP_GROUPS`: weather, calendar, email, reminders, web, everyday) becomes `tool` even if the classifier said `chat`. Music is excluded, because "skip" and "play" turn up in ordinary talk.
- Any other **statement** is never `tool`. Offered calendar tools for "my sister's birthday is next week", the model may create an event nobody asked for.

Before qwen, a `llama3.2:1b` classifier scored 13/29 and never recognised a goodbye. Run `python tests.py intent` after changing the examples, the keywords or the guards.

**Fast path:** when `decide_intent(text, "chat")` is already `tool` (a clear request with lookup keywords, or a stated preference) and the text isn't a goodbye (`GOODBYE`), the classifier is skipped. The guards would overrule it anyway. The acknowledgement is spoken at ~0 s, before memory recall and tool selection.

### Tool Routing (intent == 'tool')
Tool selection time is prompt reading: qwen2.5:7b on the M4 reads about 200 tokens/s, and Ollama's single cache slot is usually taken by the previous chat or classifier call. All 55 schemas are about 5,400 tokens and make qwen ignore tools; all 14 Gmail schemas with the full system prompt measured 10.3 s cold. `select_tools()` narrows in two steps:
1. **Agent:** `route_tools()` matches `GROUP_KEYWORDS` with no LLM call, then `classify_tool_group()` (`llama3.2:1b`) if nothing matched, then `ALL_TOOLS` as a last resort.
   A request whose words match several groups ("read my emails and block time on my calendar") gets the picked tools of each, up to `MAX_TOOLS_OFFERED` (8), so the loop can chain across agents. The first group leads: it gives the acknowledgement, and the "not set up" reply if it's off.
2. **Tools within the agent:** `tool_catalog.pick()` keeps only the tools whose `words` the request contains, at most `MAX_PICKED` (4), best match first, plus any tool they `needs` (moving an event needs `get_calendar_events` to find its id). A tool that's only there as a helper never leads. With no match, it sends the agent's first `FALLBACK_COUNT` (2) tools, so the most common reads go first in each agent's entry.

`tool_catalog.AGENT_TOOLS` is `{agent class: {tool: Tool(words, ack, needs)}}`. `build_tool_groups()` raises at startup if a registered tool is missing from it or it names a tool no agent defines. A tool may be listed under a second agent to be offered in that group too (Apple Calendar under `Calendar_Agents`).

If the routed group yields no tool call:
1. **`DEFAULT_CALLS`**: if the text is a request and the group has an obvious action, Jarvis runs it itself:
   - weather runs `get_current_weather(location="home")`;
   - music turns "play X" into `search_song_and_queue(query=X)`;
   - everyday sends time questions to `get_current_time` and date questions to `resolve_date(<the question>)`.

   This exists because qwen2.5:7b narrates ("I'll check the weather at home…") or declines "play some jazz", even at temperature 0.
2. Otherwise, the call is retried once with the agent's whole tool list (`TOOL_GROUPS[group]`). There is no retry with `ALL_TOOLS`, which would take about 30 s.

### Tool Execution: the Agent Loop
1. Speak `acknowledgement(group, tools)`: a phrase from the leading tool's `ack` in `tool_catalog`, worded for the action ("Let me see what's come in." for unread email, "Sure, let me take that off your calendar." for a delete), never the same one twice in a row. It is queued, so it plays over everything that follows. A disabled group gets `not_set_up_reply()` instead.
2. Build the short conversation with `tool_messages()`: `TOOL_PROMPT` (the persona in a few lines, not the ~600-token system prompt) plus preferences, the last `TOOL_CONTEXT_TURNS` (4) turns in plain words, and the request prefixed with `date_reference()` (today, tomorrow and the coming week by name). qwen gets weekdays wrong from an ISO date alone. The chat path has the same dates in its system prompt instead (see Prompt Cache).
3. First call to `qwen2.5:7b` with the picked tools, at temperature 0 (`TOOL_CALL_OPTIONS`), then `DEFAULT_CALLS` and the whole-agent retry above if it made no call.
4. `run_tool_loop()` repeats, up to `MAX_TOOL_STEPS` (4) rounds:
   - `run_tool_calls()` executes each call via `TOOL_REGISTRY`. Arguments the tool doesn't take are dropped (qwen once invented `toolbench_rapidapi_key`), a call identical to one already made this turn isn't run again, and exceptions become a text result.
   - Results go in as `role: tool` messages. The main `messages` history gets the calls and results too, each cut to `TOOL_RESULT_HISTORY_CHARS` (400), for later chat turns. Every character there is read again by the next chat turn: at 1,200 a chat after the weather took 3.8 s to its first word.
   - A note restates the request: if the tools so far only looked something up and the request asks for a change, make it now; only when every part is done, give the result in two sentences.
   - The model either calls the next tool or answers. Every step uses the same messages, appended to, and the same tools, so Ollama only reads what's new.
5. **Unmade-change guard.** `tool_catalog.CHANGES` lists the tools that change something. While one is on offer and none has run, and the request isn't an information question (`INFO_QUESTION`: "what did I put on my calendar"), a step is read in full before anything is spoken. If it answers without making the change, `push_for_change()` tells the model nothing has changed yet. If it still only says it's about to act (`ABOUT_TO_ACT`), it gets told once more. If it answers again, that answer stands, since the request may not have asked for a change after all.
6. Once no change is pending, steps are streamed, so the answer starts at its first sentence. `speak_stream(already_said=)` skips any sentence identical to one already spoken this turn; qwen tends to open by repeating the acknowledgement.
7. After `MAX_TOOL_STEPS`, or an empty answer, one last call with no tools asks what was and wasn't done.
8. **Deletes and sends are confirmed first.** Every call goes through `run_tool_calls()`. Before any tool in `tool_catalog.CONFIRM` runs (`delete_calendar_event`, `trash_email`, `delete_reminder`, `send_email`, `reply_to_email`, `send_draft`), `confirm_calls()` asks once for the whole round:
   - `confirmation_question()` has qwen phrase it from what it just found ("Are you sure you want to cancel the team standup scheduled for tomorrow at 3:00 PM?"). It uses the loop's messages and tools, so the cache holds. `CONFIRM_FALLBACKS` is used if that fails.
   - `listen_for_reply()` waits for playback, chimes and records for up to `CONFIRM_TIMEOUT` (8 s) with the follow-up loudness bar.
   - `is_yes()` needs a clear yes (`SAYS_YES`), and any no word (`SAYS_NO`) wins. Silence or anything unclear counts as no.
   - A declined call isn't run. The model is told the action wasn't done and not to retry it, and a repeat of the same call is refused.

Wording of the note in step 4, measured on find-then-change requests at temperature 0:

| Wording | Changes made |
|---|---|
| "call the next tool, otherwise answer" | 0 of 3 |
| restate the request, change first, then answer | 3 of 3 |
| the same, plus "go straight to the result" | 0 of 3 |

Any hint to hurry to the answer makes qwen describe the change as done instead of making it. At the default temperature, it made the change in about half of runs.

`python tests.py loop` runs these chains through `handle_turn` against a fake calendar and inbox, with every change tool replaced by a recorder and the spoken yes/no faked. It covers both a yes and a no to a cancel, and a confirmed send.

Measured with a cold cache: tool selection 1.8–3.8 s, down from 6–8 s. A find-then-change request takes two or three model calls, roughly 15–25 s in total. Most of the rest is the tool itself (Gmail 2.8 s, Reminders 4–5 s through AppleScript) and reading its output (unread email is about 1,200 tokens, around 6 s). At temperature 0, qwen writes full ISO timestamps in date arguments, which roughly doubles a calendar call's length.

### Prompt Cache
Ollama keeps a prompt cache per slot, so a request that starts with the same tokens as the last one only reads what's new. Measured first-word times across chat, chat, tool, chat, chat:

| Change | First turn | Chat after chat | Chat after a tool turn |
|---|---|---|---|
| Before (qwen intent check) | 4.1 s | 1.4–1.7 s | 5.4 s |
| Embedding intent check, dates in the system prompt | 3.9 s | 0.5–0.6 s | 3.8 s |
| `TOOL_RESULT_HISTORY_CHARS` 1,200 → 400 | 3.7 s | 0.6 s | 2.5 s |
| `warm_conversation()` | **0.8 s** | **0.5–0.6 s** | **2.5 s** |

What keeps it warm:
- **Nothing else runs on the conversation's slot.** The intent check no longer calls qwen.
- **History matches what was sent.** The chat path used to add `date_reference()` to a copy of the last message and store it without the dates, so the next turn missed the cache from that message on. The dates are now in `build_system_prompt()`, fixed for the conversation.
- **The system prompt is read early.** `warm_conversation()` sends it with `num_predict: 1` in the background as each conversation starts, while the user is still speaking.
- **One slot is enough.** Ollama 0.34 keeps its own prompt cache in RAM (up to 8 GB): when another prompt takes the slot, the idle conversation is saved and restored in about 10 ms. `OLLAMA_NUM_PARALLEL=2` was measured against one slot on the same conversation, twice each, and made no difference (chat after a tool turn 2.44–2.51 s with one slot, 2.46–2.54 s with two), so it isn't set. It would cost about 230 MB per extra slot.

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

**Recall** (`retrieve_memories`), most detailed tier first, on every turn:
1. **Chroma** (`memory_store.py`): an on-device vector store in `data/chroma`, with embeddings from Ollama's `nomic-embed-text`, using the `search_query:` / `search_document:` prefixes it was trained with.
   - Records: `{text, kind: fact | session, date}`, with ids hashed from content, so re-adding is an upsert.
   - Hits beyond `MAX_DISTANCE = 0.40` cosine distance are dropped. Measured: related questions land at 0.29–0.36, unrelated ones at 0.43–0.61. At 0.55, a coding preference leaked into the reply to "call me Captain".
   - Hits come back tagged `(YYYY-MM-DD)`. Recall takes about 20–35 ms; Pinecone took 1–4 s.
   - On first open the store is seeded from the vault (`obsidian_store.all_records()`), so it starts with what the notes hold.
2. **Obsidian keyword search** (`obsidian_store.search`), if the store or the embedding model is unavailable: it scores sessions and topic lines by query-word overlap.

Hits are prepended to the **user message**, never `messages[0]`; rewriting the system prompt invalidated Ollama's prefix cache (measured 13.8 s penalty).

Pinecone was removed: its free tier ran out of monthly reads, and every failed recall cost 3–4 s. Memories written to it before the switch are still in that Pinecone account.

**Obsidian** (the knowledge graph the user browses), vault default `~/Desktop/Jarvis AI`:
- `Memory/YYYY-MM-DD.md`: `## Session (HH:MM) — <title>`, the summary, `### Facts learned`, then `### Topics` as `[[wikilinks]]`.
- `Memory/Entities/<Topic>.md`: one note per topic, gaining a line `- [[YYYY-MM-DD]] HH:MM — <note>` every session that mentions it. Name matching is case-insensitive, so "toronto" joins "Toronto". Characters that break links (`/ : # [ ]` and so on) are stripped.
- Days link to topics and topics link back to days, so the graph connects related sessions on its own.
- A missing vault is skipped, never created.

The vector store and Obsidian fail independently; one failing is logged and doesn't stop the other.

### Preferences (preferences.py)
Standing preferences the user states once and expects kept:
- **Storage:** `data/preferences.json` on this Mac. It is mirrored to `<vault>/Memory/Preferences.md` for reading; the JSON is the source of truth.
- **Known keys** change behaviour directly: `temperature_units` (the weather tools switch to metric), `home_location` (the default weather city, ahead of `JARVIS_HOME_LOCATION`) and `address_as` (replaces "sir" in acknowledgements and "not set up" replies). Anything else is a free-form note.
- **Always visible:** `build_system_prompt()` includes `preferences.prompt_block()` at the start of every conversation.
- **Two ways in:**
  - Immediately, through the `PreferencesAgent` tools when the user says it ("I prefer Celsius" routes to the `preferences` group, statement or not).
  - At session end, through a `preferences` field in the memory extraction, for anything the tool path missed. Only explicitly stated preferences are taken; "a place the user is travelling to is not their home".

---

## Models in Use

| Model | Purpose | Runtime |
|---|---|---|
| `hey_jarvis_v0.1` + melspectrogram, embedding, Silero VAD | Wake word | onnxruntime (listener process) |
| `qwen2.5:7b` | Intent classification, reasoning, tool calling, summarisation, memory extraction | Ollama (local) |
| `llama3.2:1b` | Tool-group fallback when no keyword matches | Ollama (local) |
| `mlx-community/parakeet-tdt-0.6b-v2` | Speech-to-text | MLX (parakeet-mlx) |
| `mlx-community/whisper-small-mlx` | Speech-to-text fallback (`JARVIS_STT=whisper`) | MLX (Apple Silicon) |
| `hexgrad/Kokoro-82M` (voice `af_heart`) | Text-to-speech | Kokoro (local) |
| `nomic-embed-text` | Memory embeddings (768-d) | Ollama (local) |

- Ollama `keep_alive` is `JARVIS_IDLE_UNLOAD_MINUTES + 5` minutes, not forever. `shutdown()` unloads the models explicitly (`generate(..., keep_alive=0)`, and `embed(..., keep_alive=0)` for the embedding model), and the bound frees them even after a crash. `prewarm()` loads all three.
- Spoken replies are capped with `GEN_OPTIONS = {"num_predict": 160}`.
- `MAIN_MODEL` (`JARVIS_MODEL`, default `qwen2.5:7b`) is used for every qwen call. `chat()` wraps `ollama.chat` and turns thinking off for models that think first (`THINKING_FAMILIES`: qwen3, deepseek-r1, gpt-oss).
- **Offline model loading:** Kokoro, Parakeet and Whisper each ask Hugging Face for newer files on every load. On a slow network that hung startup for minutes. Once all three are in `~/.cache/huggingface/hub`, `jarvis.py` sets `HF_HUB_OFFLINE=1` before importing them; set `HF_HUB_OFFLINE=0` to allow update checks. Full startup is about 7.7 s.
- **Speech to text:** Parakeet measured 0.15 s per command against Whisper small's 0.37 s on eight spoken commands, with about the same memory (~650 MB), fewer errors ("Priya" where Whisper heard "PREA") and the same punctuation, which `looks_like_request()` relies on. parakeet-mlx 0.5.2's default bfloat16 path fails in `get_logmel`, so audio goes in as float32. If Parakeet fails, `transcribe()` switches to Whisper for the rest of the run.

### Wired Up but Inactive
- **ElevenLabs TTS** (`eleven_turbo_v2_5`, voice `k7IRoeykhdGZUkTeJ1ID`): `play_audio_with_text_eleven_labs()`
- **ElevenLabs STT** (`scribe_v2`): `record_audio_and_transcribe_elevenlabs()`

Unused for cost reasons. Do not remove them: they are the target production audio stack.

---

## Agent Classes (agents.py)

55 tools across 8 agents. Every public bound method becomes a tool; methods starting with `_` are internal helpers.

| Class | Group | Tools | Service |
|---|---|---|---|
| `Calendar_Agents` | calendar | `create_event`, `get_calendar_events`, `update_calendar_event`, `delete_calendar_event` | Google Calendar API |
| `WebSearchAgents` | web | `search_web`, `extract_webpages`, `crawl_webpages`, `research` | Tavily |
| `WeatherSearch` | weather | `get_current_weather`, `get_weather_with_time`, `get_daily_forecast`, `get_weather_alerts` | OpenWeatherMap One Call 3.0 + Geocoding |
| `SpotifyAgent` | music | `get_current_track`, `search_song_and_queue`, `create_playlist`, `add_song_to_playlist`, `recently_played`, `skip_song`, `previous_song`, `pause_song`, `resume_song`, `shuffle`, `set_volume` | Spotify (spotipy) |
| `GmailAgent` | email | `send_email`, `search_email`, `get_unread_emails`, `get_email_by_id`, `reply_to_email`, `mark_as_read`, `mark_as_unread`, `trash_email`, `remove_email_from_trash`, `get_drafts`, `send_draft`, `get_sent_emails`, `get_sender_profile`, `get_all_labels` | Gmail API |
| `RemindersAgent` | reminders | `get_reminder_lists`, `add_reminder`, `get_reminders`, `get_due_reminders`, `complete_reminder`, `delete_reminder`, `update_reminder`, `create_reminder_list` | macOS Reminders via JXA |
| `EverydayToolsAgent` | everyday (+ calendar) | `get_current_time`, `resolve_date`, `days_between`, `find_contact`, `get_apple_calendar_events`, `search_notes`, `read_note` | parsedatetime, Contacts and Notes via JXA, Calendar via EventKit |
| `PreferencesAgent` | preferences | `set_preference`, `forget_preference`, `list_preferences` | `preferences.py` |

### Everyday Tools
Read-only lookups Jarvis can use whenever he needs a detail:
- **Dates:** `resolve_date` turns phrases into exact dates deterministically, using parsedatetime with fixes for phrases it gets wrong ("the day after tomorrow"). It never reports a time the user didn't say. For "next Friday" it names both candidate dates in a `note`, because the phrase is ambiguous.
- **Contacts and Notes** use JXA through the shared `_run_jxa` helper, with user text passed as JSON argv. Each takes about 5 s, and the first use asks for Automation permission.
- **Apple Calendar** uses EventKit, not Calendar scripting, which misses recurring events and is slow. It covers every account in the macOS Calendar app. macOS asks Jarvis.app for calendar access on first use; that needs `NSCalendarsFullAccessUsageDescription` in the Info.plist. A process without it, such as a terminal, is refused without a prompt.
- `get_apple_calendar_events` is also listed under `Calendar_Agents` in `tool_catalog`, so Apple Calendar is offered alongside the Google tools for "what's on my calendar".

### Weather Locations
Weather tools take a **place name** (`location`), not coordinates:
- `_locate()` geocodes it with OpenWeather's geocoding API (same key) and caches the result.
- `"home"`, `"here"` or `""` mean the `home_location` preference, then `JARVIS_HOME_LOCATION` from `.env`. If neither is set, the tool returns an error asking which city.
- Units follow the `temperature_units` preference: imperial by default, metric for celsius.
- Results carry the resolved `location` label. Forecast days are labelled with their date, plus "(today)" / "(tomorrow)".
- `get_daily_forecast` always returns at least 3 days, because the model passes `days=1` for "tomorrow".

### Optional Services
Every agent is optional. `start_agents()` walks `AGENT_REQUIREMENTS`, a list of `(class, [env vars or *.json files])` pairs:
- An agent missing any requirement is left out of `AGENTS`, so its tools never reach the model.
- An agent whose constructor raises is left out too, and the rest still start.
- The reason is recorded in `DISABLED_AGENTS` by class and `DISABLED_GROUPS` by group.

When a tool request routes to a disabled group, `handle_turn` speaks `not_set_up_reply()` ("Weather isn't set up yet, sir…") **before** the acknowledgement, instead of letting the model invent an answer. The reply avoids spelling out env var names, because Kokoro reads them letter by letter.

With nothing configured at all, the voice pipeline, chat, memory, Reminders, everyday tools and preferences still work.

### Adding a New Agent
1. Define a class in `agents.py`. Docstrings and type hints become the tool description Ollama sees, so write them for the model.
2. Add it to `AGENT_REQUIREMENTS` in `jarvis.py` with the env vars or files it needs.
3. Add its class name to `GROUP_BY_CLASS` (startup raises if it's missing), its trigger words to `GROUP_KEYWORDS` and its spoken name to `SERVICE_NAMES`.
4. Add an entry to `tool_catalog.AGENT_TOOLS` with every public method: trigger `words`, a few `ack` phrases, and `needs` for tools that take an id from another. Most common reads go first. Startup raises if a method is missing.
5. Tool names must be unique across all agents: `build_tool_registry()` raises on duplicates.
6. To offer a method in a second group too, list it under that group's agent in `tool_catalog` as well.

`TOOL_REGISTRY` and `ALL_TOOLS` are derived from `AGENTS`; `TOOL_GROUPS` from `AGENTS` and `tool_catalog`, checked against each other at startup.

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
`record_command()` streams 40 ms chunks from sounddevice into `endpoint()`, which decides where the command starts and ends. `endpoint()` is a plain function over chunks, so `tests.py listen` can drive it with synthetic audio.
- **Start:** 3 of the last 4 chunks are both speech (Silero VAD ≥ 0.5, `wake_word.SpeechDetector`) and louder than the threshold. `PRE_ROLL` (0.3 s) before that is kept, so the first word isn't clipped.
- **Keep going:** a chunk counts if it's as loud as the threshold, or if the detector hears speech (≥ 0.35) at `KEEP_LOUDNESS` (half) of it. Silero is less sure of some voices; on synthetic speech it dipped to 0.3 inside words, which cut "remind me to call … mom" short when every chunk had to pass it.
- **End:** `END_SILENCE` (0.6 s, `JARVIS_END_SILENCE`) of neither. Measured on synthetic speech: the recording ends 0.53 s after the last word (the old energy recorder needed 0.8 s of quiet, and loudness fades slowly). Pauses up to ~0.45 s mid-sentence are kept, and a quieter voice afterwards (a TV) ends it 0.77 s after the command. Only `TAIL` (0.15 s) of the silence is passed on.
- **Noise:** under `MIN_SPEECH_SECONDS` (0.3 s) of detected speech returns `NOISE`, which is never transcribed.

The threshold, and why:
A fixed energy threshold of 200 in a room whose background sat at about 270 meant 87% of silence counted as speech. Recording ran to the 45 s cap, Whisper transcribed the noise ("ʕᴗᴗᴗ…", "When my plane gets here"), and the conversation never ended. So:
1. **Room-fitted threshold:** `set_noise_floor()` takes the listener's measured noise floor and sets the threshold to `NOISE_MULTIPLIER` (2.5×) for the first command after the wake word. Follow-ups use `JARVIS_FOLLOW_UP_LOUDNESS` (default 4×), because they come without a wake word and a TV or other people would otherwise keep a conversation going. The minimum is 200. Standalone `python jarvis.py` measures the room with `calibrate()` instead.
2. **Transcript check:** `is_speech()` rejects transcripts that are mostly non-letters or stock phrases for silence ("Thank you.", "you"). Parakeet turns pure noise into "Yeah.", which the speech check above keeps from reaching it. Two in a row end the conversation.

Other settings: `LISTEN_TIMEOUT = 10 s` of silence ends the conversation; phrase limit `45 s`. Recordings are returned as 16 kHz float32 numpy arrays.

### Message History
- `messages` holds the current conversation only. `run_session` clears everything after `messages[0]` when a conversation ends.
- `messages[0]` is rebuilt by `build_system_prompt()` at the start of each conversation (today's date, preferences) and must stay byte-stable within it, for prefix caching.
- Cross-session memory comes from recall (Chroma, then Obsidian) and from preferences.

### Auth & Permissions
- **Google**: `get_google_creds()` is shared by Calendar and Gmail (calendar + gmail read/send/modify/labels scopes). `token.json` auto-refreshes; if refresh fails (Google revokes after 7 days in Testing mode), it opens a browser login. That blocks the background app, so complete logins by running `python jarvis.py` in a terminal. `credentials.json` must be at the project root.
- **Spotify**: token at `.spotify_token`; the first run needs a browser login in a terminal. Playback commands need an active Spotify device.
- **Spotify** also requires Spotify Premium on the account that owns the developer app. Without it, every request returns 403 "Active premium subscription required", and the agent starts with no connection.
- **Microphone**: macOS asks the first time Jarvis.app opens the mic (`NSMicrophoneUsageDescription`). The ad-hoc code signature keeps the grant across rebuilds; verified.
- **Calendars and Contacts**: `NSCalendarsFullAccessUsageDescription`, `NSCalendarsUsageDescription` and `NSContactsUsageDescription` are in the Info.plist, so macOS asks Jarvis.app on first use instead of refusing silently.
- **Reminders, Contacts, Notes (JXA)**: each needs Automation permission under System Settings → Privacy & Security → Automation. User text is passed as JSON argv, never concatenated into the script.

---

## Environment Variables (.env)

`.env.example` is the template, with where to get each key: `cp .env.example .env`. All keys are optional; see Optional Services.

| Key | Turns on | Source |
|---|---|---|
| `OPENWEATHER_API_KEY` | Weather (needs the One Call 3.0 subscription) | home.openweathermap.org |
| `TAVILY_API_KEY` | Web search and research | app.tavily.com |
| `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SPOTIPY_REDIRECT_URI` | Spotify (the redirect URI uses `127.0.0.1`, not `localhost`) | developer.spotify.com/dashboard |
| `ELEVENLABS_API_KEY` | Nothing yet; reserved for final release | elevenlabs.io |
| `JARVIS_HOME_LOCATION` | City used when a weather question names no place, e.g. `Boston` | |
| `OBSIDIAN_VAULT_PATH` | Vault location, default `~/Desktop/Jarvis AI` | |
| `JARVIS_IDLE_UNLOAD_MINUTES` | How long the engine stays warm, default 10 | |
| `JARVIS_WAKE_THRESHOLD` | Wake sensitivity, default 0.5; raise it if Jarvis triggers falsely | |
| `JARVIS_FOLLOW_UP_LOUDNESS` | How much louder than the room a follow-up must be, default 4; raise it if background voices keep conversations going | |
| `JARVIS_END_SILENCE` | Seconds of silence that end a command, default 0.6; raise it if Jarvis cuts you off mid-sentence | |
| `JARVIS_STT` | `parakeet` (default) or `whisper` | |
| `JARVIS_MODEL` | Main Ollama model, default `qwen2.5:7b`; for comparison runs | |

Calendar and Gmail need `credentials.json` (a Google Cloud OAuth desktop client) in the project folder instead of a key.

---

## Testing

```bash
python tests.py              # everything except the microphone test
python tests.py --quick      # structural checks only, no model calls
python tests.py intent       # intent routing on 35 labelled phrases (needs Ollama)
python tests.py loop         # agent loop: chains, no false claims, confirms deletes/sends (needs Ollama)
python tests.py listen       # recorder start/stop on synthetic room audio, no microphone
python tests.py wake memory prefs everyday   # need no API keys, Ollama or jarvis.py
python tests.py recall       # vector memory in a temp folder (needs Ollama)
python status.py             # is the running Jarvis healthy?
python tests.py voice        # interactive microphone check
python tests.py --list       # show suite names
```

- Tool suites check which tool the model **chooses** and never execute it, so they are safe against live accounts.
- The wake suite synthesises speech with macOS `say`, so it needs no fixtures or microphone.
- The memory, prefs and recall suites use temporary folders and never touch the real vault, `data/` or preferences.
- **Tests with the real app write real memories.** Playing "Hey Jarvis" through the speakers runs a full conversation, and its summary lands in the vault and `data/chroma`. Remove those afterwards.
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
- `AGENT_REQUIREMENTS`, `GROUP_BY_CLASS`, `GROUP_KEYWORDS`, `SERVICE_NAMES` and `tool_catalog.AGENT_TOOLS` entries when adding new agents
- Helper/utility functions (formatters, parsers, error handlers)
- New agent class shells following the existing pattern

---

## Known Limitations
- **The chat path can claim actions it didn't take.** Observed: "I've noted this in your calendar and will remind you" with no tool called, because no tools are offered there.
- **Dates in plain chat still depend on qwen.** Date questions route to `resolve_date` and are exact. But a date mentioned in passing in chat, and then in memory, can still be wrong.
- **Background voices** (TV, other people) pass the speech check. The louder follow-up bar holds them off, but not a loud TV next to the Mac. `JARVIS_FOLLOW_UP_LOUDNESS` is the knob.
- One remaining intent miss in the test set: "Can you help me write a poem about rain?" routes to weather.
- `messages` grows unbounded within a conversation, and the summarise instruction stays in history.
- The all-tools retry also fires when the model correctly answers without a tool.
- A cold wake (engine retired) takes about 6 s before the first answer starts. The command itself is recorded during that time.
- The app bakes in the project and `.venv` paths; re-run `build_app.sh` after moving either.

## Roadmap (mirrors PROGRESS.md)
1. **Honest chat and a multi-step loop**: keep the chat path from claiming actions it didn't take, and let tool results chain.
2. **Proactive help from the everyday tools**: check the calendar and contacts before scheduling or emailing, without being asked.
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
ollama pull nomic-embed-text

# First run in a terminal: completes the Google and Spotify logins, one conversation, no wake word
.venv/bin/python jarvis.py

# Background assistant with wake word, in the foreground for debugging
.venv/bin/python wake_listener.py

# Or install the apps (Launchpad / Spotlight / Applications)
bash build_app.sh
```
Check: open **Jarvis Status**, or run `.venv/bin/python status.py`. Logs: `logs/jarvis.log` (rotated at 5 MB). Stop: `pkill -f wake_listener.py`.
