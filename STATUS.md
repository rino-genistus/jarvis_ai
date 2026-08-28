# Jarvis AI — Status Report

**Generated:** 2026-08-27
**Branch:** `main` (in sync with `origin/main`)
**HEAD:** `238eaaa` — *Revert to original baseline: restore clean jarvis.py, agents.py, PROGRESS.md, .gitignore; remove conflicted module files* (2026-06-11)
**Working tree:** clean except `logs/ui.err` (modified — see [Live Issues](#live-issues))

---

## TL;DR

The repository is **not** in the state that `CLAUDE.md` and `PROGRESS.md` describe. On 2026-06-11 a bad merge between two parallel development lines was resolved by reverting `jarvis.py` and `agents.py` all the way back to the **2026-04-23 first commit**, and deleting every module added since. Both docs still describe the post-merge feature set (UI, modular refactor, Obsidian memory, wake-word app, ComputerControlAgent), none of which exists on disk today.

Meanwhile, two `launchd` agents installed in June are still loaded and crash-looping against files the revert deleted.

---

## What Actually Exists on Disk

```
jarvis_ai/
├── jarvis.py                  422 lines — main loop, voice pipeline, memory, intent routing
├── agents.py                  537 lines — 5 agent classes
├── calendar_quickstart.py      69 lines — Google Calendar OAuth scratch script
├── CLAUDE.md                            — project instructions (STALE, see divergence)
├── PROGRESS.md                          — roadmap (STALE, describes reverted code)
├── setup_autostart.sh                   — installs/uninstalls the two LaunchAgents
├── com.jarvisai.backend.plist           — points at Jarvis.app/Contents/MacOS/Jarvis (DELETED)
├── com.jarvisai.ui.plist                — points at jarvis_ui.py (DELETED)
├── logs/                                — backend.log, backend.err, ui.log, ui.err
├── voices/                              — Piper .onnx voices (unused by current code)
├── .env, credentials.json, token.json, .spotify_token   — secrets, gitignored
└── __pycache__/                         — contains .pyc for jarvis_ui and ui_bridge, sources gone
```

**`jarvis.py` and `agents.py` at HEAD are byte-identical (modulo trailing whitespace) to commit `1563273`, 2026-04-23.** Verified with `git diff -w 1563273 HEAD -- jarvis.py agents.py` → empty.

---

## Current Architecture (as implemented, not as documented)

### Voice pipeline — `jarvis.py`

```
record_audio_and_transcribe_mlx_whisper()   speech_recognition mic → whisper-small-mlx
        ↓
classify_intent()                            llama3.2:1b → "exit" | "tool" | "chat"
        ↓
retrieve_memories()                          Pinecone top-5 → injected into messages[0]
        ↓
   ┌────────────┼────────────┐
 exit          tool         chat
   ↓            ↓            ↓
farewell   "Right away    qwen2.5:7b
+ extract   sir." →        direct reply
+ upsert    tool loop →
+ break     summarise
        ↓
safe_speak() → Kokoro-82M (af_heart) → sounddevice → play_chime()
```

**There is no wake word.** `pvporcupine` is not imported and no always-on listener exists. The loop starts recording immediately.

### Models actually referenced in code

| Model | Role | Note |
|---|---|---|
| `qwen2.5:7b` | reasoning, tool calls, summarisation, memory extraction | **CLAUDE.md and PROGRESS.md both say `14b`** |
| `llama3.2:1b` | intent classification | matches docs |
| `mlx-community/whisper-small-mlx` | STT | matches docs |
| `hexgrad/Kokoro-82M` | TTS, voice `af_heart` | matches docs |
| `llama-text-embed-v2` | Pinecone embeddings | matches docs |
| `eleven_turbo_v2_5` / `scribe_v2` | ElevenLabs TTS/STT | code present, deliberately unused (cost) |

All required Ollama models are pulled locally (`qwen2.5:7b`, `qwen2.5:14b`, `llama3.2:1b` all present).

### Memory

- Pinecone serverless index `jarvis-ai`, namespace `jarvis-memory-namespace`, created on startup if absent.
- `retrieve_memories(query, top_k=5)` runs every turn; hits are appended to the system prompt at `messages[0]`.
- `extract_important_messages()` runs **only on `exit` intent**, synchronously, via `qwen2.5:7b`.
- Records are written as `{"id": ..., "chunk_text": ...}`. PROGRESS.md claims this field name was a bug fixed to `text` — **that fix was in the reverted code and is not present here.**
- No Obsidian integration. No session episodes. No recency weighting. No background extraction thread.

### Agents — `agents.py` (5 classes, 39 methods declared)

| Class | Methods | Status |
|---|---|---|
| `Calendar_Agents` | `create_event`, `get_calendar_events`, `update_calendar_event`, `delete_calendar_event` | working |
| `WebSearchAgents` | `search_web`, `extract_webpages` | working; `crawl_webpages`, `research` are empty stubs |
| `WeatherSearch` | `get_current_weather`, `get_weather_with_time`, `get_daily_forecast`, `get_weather_alerts` | working |
| `SpotifyAgent` | `get_current_track`, `search_song_and_queue`, `create_playlist`, `add_song_to_playlist`, `recently_played`, `skip_song`, `pause_song`, `shuffle`, `set_volume` | working; needs an active device |
| `GmailAgent` | `search_email`, `send_email`, `get_unread_emails`, `get_email_by_id`, `get_all_labels`, `reply_to_email`, `get_sender_profile` | working |
| `GmailAgent` (stubs) | `mark_as_read`, `delete_email`, `get_drafts`, `get_sent_emails` | `return` only — **and missing `self`**, so they'd fail if called |
| `ComputerControlAgent` | — | **does not exist** (documented in CLAUDE.md and PROGRESS.md) |

Both Google agents share `get_google_creds()` with calendar + gmail read/send/modify/labels scopes.

---

## Live Issues

### 1. `main_loop()` is commented out — the app does not run

At the bottom of [jarvis.py:409-422](jarvis.py#L409-L422), the entry point is disabled and replaced with a Gmail scratch block left over from a debugging session:

```python
kokoro_ready.wait()
#main_loop()                       # ← disabled
print(f"First Command: {time.time() - start_time:.2f}s")
emails = gmail.get_unread_emails()
for email in emails:
    print(email.get("id"))
print(gmail.get_sender_profile())
print(gmail.reply_to_email(email_id="19dbca5b6b521fab"))   # ← hardcoded email ID
```

`python jarvis.py` today loads Kokoro, dumps unread email IDs, and exits. It never listens.

### 2. LaunchAgents are crash-looping — `logs/ui.err` has grown to 24 MB

`launchctl list` shows both agents still loaded with `KeepAlive: true`:

```
-  2   com.jarvisai.ui        # exit 2
-  78  com.jarvisai.backend   # exit 78
```

`com.jarvisai.ui` execs `/Users/rino/jarvis_ai/jarvis_ui.py`, which the revert deleted. Every restart appends:

```
can't open file '/Users/rino/jarvis_ai/jarvis_ui.py': [Errno 2] No such file or directory
```

`logs/ui.err` is now **24,471,245 bytes / 132,346 lines**, last written 2026-08-27 18:17 — it is still growing right now, ~2.5 months after the file it needs was deleted. The backend agent points at `Jarvis.app/Contents/MacOS/Jarvis`, also deleted.

**Fix:** `bash setup_autostart.sh --uninstall`, then truncate `logs/ui.err`. The file is tracked in git, which is why it shows as the only working-tree modification.

### 3. `PaErrorCode -9986` in the June backend log

The last recorded backend session ended with a PortAudio failure during wake-word polling:

```
||PaMacCore (AUHAL)|| Error on line 2744: err=''what'', msg=Unspecified Audio Hardware Error
Wake word error: Error starting stream: Internal PortAudio error [PaErrorCode -9986]
```

Intermittent audio-device contention. With `KeepAlive: true` this took the whole backend down and restarted it.

### 4. Wake-word false positives (from the June logs)

The reverted wake-word implementation transcribed every 3-second RMS-gated clip through Whisper looking for "Jarvis". `logs/backend.log` shows it firing on game audio and background TV constantly (`[Wake] heard: no way. no way.`, `[Wake] heard: get up, get up, get up...`). Expensive and noisy — this is why pvporcupine was on the roadmap.

### 5. Documentation is stale

`CLAUDE.md` and `PROGRESS.md` both describe the reverted code. Concretely wrong today:

| Claim | Reality |
|---|---|
| `pvporcupine` always-on wake word | no wake word at all |
| `qwen2.5:14b` main model | code uses `qwen2.5:7b` |
| `ComputerControlAgent` + directory index | class doesn't exist; no `directory_cache.json` |
| Obsidian vault memory backend | not implemented |
| `[END]` token session control | not implemented; uses `classify_intent` → `exit` |
| PyQt6 UI, `ui_bridge.py`, `Jarvis.app` | all deleted |
| Modular files (`llm.py`, `tts.py`, `stt.py`, `memory.py`, `config.py`, …) | all deleted |
| Pinecone `chunk_text` → `text` field fix | not present; still `chunk_text` |
| Background memory extraction | synchronous, on exit only |
| `requirements.txt` | does not exist |

### 6. Secrets and noise are tracked in git

`.DS_Store`, `__pycache__/*.pyc` (including `.pyc` for source files that no longer exist), `jarvis_sample.wav` (4.4 MB), and `logs/*` are all committed. `.gitignore` covers `.env`, tokens and `voices/` but not these.

---

## Git History

| Commit | Date | Message | What happened |
|---|---|---|---|
| `1563273` | 2026-04-23 | First Jarvis_AI commit | 2,039 lines. `jarvis.py` + `agents.py` with Calendar, WebSearch, Weather, Spotify, Gmail agents. **This is what HEAD contains today.** |
| `f5160a1` | 2026-04-23 | git ignore file update | tokens + credentials ignored |
| `3027c57` | 2026-04-26 | Finished Gmail Agent, working on Computer Agent | +327 lines to `agents.py`: `ComputerControlAgent` + directory index. **Lost in the revert.** |
| `e2df5b4` | 2026-06-10 | Made Jarvis into an app that runs in the background at all times | Line A. +1,864 lines: `jarvis_ui.py` (560), `ui_bridge.py`, `launcher.c`, `Jarvis.app`, both `.plist` files, `setup_autostart.sh`, `CLAUDE.md`. Wake-word loop added to `jarvis.py`. |
| `637e8a1` | 2026-06-11 | base commit to revert back when needed | `PROGRESS.md` (157 lines) — the safety marker |
| `f241353` | 2026-06-11 | Commits | Line B, the modular refactor. +3,246 lines: `config.py`, `llm.py`, `memory.py`, `stt.py`, `tts.py`, `audio_io.py`, `computer_control.py`, `obsidian_store.py`, `logging_setup.py`, `jarvis_tray.py`, `build_app.sh`, `install_launch_agents.sh`, `requirements.txt`. `jarvis.py` rewritten. |
| `9ed6b25` | 2026-06-11 | Merge branch 'main' | first merge of Line A into local |
| `22472c0` | 2026-06-11 | git ignore file update | |
| `ad4217a` | 2026-06-11 | Merge branch 'main' … merge | Line A + Line B combined. +5,172 / −120. Two `jarvis_ui.py` versions (560 vs 955 lines) and two `ui_bridge.py` versions collided. |
| `238eaaa` | 2026-06-11 | **Revert to original baseline** | **−4,732 lines.** Deleted 22 files, restored `jarvis.py`/`agents.py` to the April 23 state. Current HEAD. |

```
1563273 ──> f5160a1 ──> 3027c57 ──┬──> e2df5b4 (Line A: app + UI) ──┐
                                  │                                 ├──> ad4217a ──> 238eaaa (revert)
                                  └──> 637e8a1 ──> f241353 (Line B: modules) ──┘
```

Two months of work (2026-04-26 → 2026-06-11) is reachable in history but not on `main`.

---

## Recovering the Reverted Work

Nothing is lost — the revert was a commit, not a rewrite. Everything is one command away:

```bash
# See what the merged state looked like
git show ad4217a --stat

# Recover the modular refactor line by line
git checkout f241353 -- llm.py memory.py stt.py tts.py audio_io.py config.py \
                        computer_control.py obsidian_store.py logging_setup.py \
                        requirements.txt

# Recover just ComputerControlAgent (April line)
git show 3027c57:agents.py > /tmp/agents_with_computer.py

# Recover the UI (two versions exist — pick one)
git checkout ad4217a -- jarvis_ui.py     # 955-line version
git checkout e2df5b4 -- jarvis_ui.py     # 560-line version

# Undo the revert entirely
git revert 238eaaa
```

---

## Recommended Next Steps

Ordered by impact, cheapest first.

1. **Stop the crash loop.** `bash setup_autostart.sh --uninstall`, then `: > logs/ui.err` and commit. Two months of a 24 MB error file is the only thing actively wrong with the machine right now.
2. **Make `jarvis.py` runnable.** Uncomment `main_loop()`, delete the Gmail scratch block at the bottom, remove the hardcoded email ID.
3. **Decide the architecture.** Line A (single-file + UI) and Line B (modular) both exist in history. Pick one deliberately and cherry-pick forward rather than re-merging `ad4217a`. Line B is the better base — it separates `llm`/`stt`/`tts`/`memory` cleanly and includes `requirements.txt`.
4. **Restore `ComputerControlAgent`** from `3027c57` or `f241353:computer_control.py` — it is documented as a shipped feature in both docs.
5. **Rewrite `CLAUDE.md` and `PROGRESS.md`** to match whatever the code actually does after step 3. Right now they actively mislead: 12+ documented features do not exist.
6. **Fix the Gmail stubs** — `mark_as_read`, `delete_email`, `get_drafts`, `get_sent_emails` are missing `self` and would raise on call.
7. **Verify the Pinecone field name.** The reverted branch claimed `chunk_text` silently failed every upsert against `field_map`. Confirm which key the index actually wants before trusting any memory write in the current code.
8. **Clean the repo.** Add `.DS_Store`, `__pycache__/`, `logs/`, `*.wav` to `.gitignore` and `git rm --cached` them.
9. **Add `requirements.txt`** — recoverable from `f241353`. There is currently no record of what this project needs to run.

---

## Environment

- macOS Darwin 25.4.0, Apple Silicon
- Python 3.12.2 (`/Library/Frameworks/Python.framework/Versions/3.12`)
- Ollama with `qwen2.5:7b`, `qwen2.5:14b`, `llama3.2:1b` pulled (plus `qwen3`, `gemma4`, `llama3.1:8b`, `granite4.1:3b`, `mxbai-embed-large`)
- Remote: `https://github.com/rino-genistus/jarvis_ai.git`
