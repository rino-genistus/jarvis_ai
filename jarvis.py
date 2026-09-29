from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import os
import speech_recognition as sr
import mlx_whisper
from ollama import chat, generate, ChatResponse
import time
from agents import Calendar_Agents, WebSearchAgents, WeatherSearch, SpotifyAgent, GmailAgent, RemindersAgent
import mic
import obsidian_store
from datetime import datetime, timedelta
from kokoro import KPipeline
import sounddevice as sd
import numpy as np
from pinecone import Pinecone
import threading
import inspect
import json
import queue
import re

start_time = time.time()

# How long the engine stays warm after a conversation before the wake listener
# retires it and memory drops back to just the wake word model.
IDLE_UNLOAD_MINUTES = float(os.getenv("JARVIS_IDLE_UNLOAD_MINUTES", "10"))

# Keep Ollama models resident across a burst of use — the default 5 minutes
# means a reload of 4.4s for qwen and 2.3s for llama. Not forever, though:
# shutdown() unloads them explicitly, and this bound frees them even if the
# engine crashes before it gets the chance.
OLLAMA_KEEP_ALIVE = f"{int(IDLE_UNLOAD_MINUTES) + 5}m"
OLLAMA_MODELS = ("qwen2.5:7b", "llama3.2:1b")

# Cap spoken replies. Generation and playback both scale with length, and a
# 73 token answer is already 7.6 seconds of speech.
GEN_OPTIONS = {"num_predict": 160}

load_dotenv()

current_date = datetime.now().strftime("%A, %B %d, %Y")
print(current_date)

def connect_memory_index():
    """
    Pinecone is optional. Without a key (or if it's unreachable) Jarvis still
    runs: nothing is recalled, and memories go to the Obsidian vault only.
    """
    if not os.getenv("PINECONE_API_KEY"):
        print("PINECONE_API_KEY not set — memory recall off, Obsidian notes only")
        return None
    try:
        pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
        index_name = 'jarvis-ai'
        if not pc.has_index(index_name):
            pc.create_index_for_model(
                name=index_name,
                cloud="aws",
                region="us-east-1",
                embed={
                    "model":"llama-text-embed-v2",
                    "field_map":{"text": "chunk_text"}
                }
            )
        return pc.Index(index_name)
    except Exception as e:
        print(f"Pinecone unavailable ({e}) — memory recall off, Obsidian notes only")
        return None


dense_index = connect_memory_index()

eleven_labs = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

kokoro_pipeline = None
kokoro_ready = threading.Event()

def load_kokoro():
    global kokoro_pipeline
    kokoro_pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
    kokoro_ready.set()
    print(f"Kokoro Loaded")

threading.Thread(target=load_kokoro, daemon=True).start()

# Each agent with the keys or files it can't work without. An agent missing
# any of them is left out entirely, so the model is never offered a tool that
# can only fail — and one broken service no longer stops Jarvis starting.
AGENT_REQUIREMENTS = [
    (Calendar_Agents, ["credentials.json"]),
    (WebSearchAgents, ["TAVILY_API_KEY"]),
    (WeatherSearch, ["OPENWEATHER_API_KEY"]),
    (SpotifyAgent, ["SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"]),
    (GmailAgent, ["credentials.json"]),
    (RemindersAgent, []),
]

DISABLED_AGENTS = {}   # class name -> why it's off


def start_agents():
    agents = []
    for cls, needs in AGENT_REQUIREMENTS:
        # A name ending in .json is a file in the project folder, anything else an env var
        missing = [n for n in needs if not (os.path.exists(n) if n.endswith(".json") else os.getenv(n))]
        if missing:
            DISABLED_AGENTS[cls.__name__] = f"missing {', '.join(missing)}"
        else:
            try:
                agents.append(cls())
                continue
            except Exception as e:
                DISABLED_AGENTS[cls.__name__] = f"failed to start: {e}"
        print(f"{cls.__name__} off — {DISABLED_AGENTS[cls.__name__]}")
    return agents


AGENTS = start_agents()

def build_tool_registry(agents):
    """
    Collects every public method off every agent into one name -> method mapping.

    Both the tools list handed to Ollama and the dispatch dict used to run the
    calls are derived from this, so adding a method to an agent class is all it
    takes to expose it — the two can no longer drift apart the way they used to.
    """
    registry = {}
    for agent in agents:
        for name in dir(agent):
            if name.startswith("_"):
                continue
            method = getattr(agent, name)
            # Bound methods only — skips clients and constants like self.sp or PRIORITY_MAP
            if not inspect.ismethod(method):
                continue
            if name in registry:
                raise ValueError(
                    f"Two agents both define a tool called '{name}'. "
                    "Tool names must be unique or the model will call one and get the other."
                )
            registry[name] = method
    return registry

TOOL_REGISTRY = build_tool_registry(AGENTS)
print(f"{len(TOOL_REGISTRY)} tools registered across {len(AGENTS)} agents")


# --- Tool routing -----------------------------------------------------------
# Sending all 45 schemas costs 3,649 prompt tokens and ~13.5s of prompt eval on
# every command. Worse, at that size qwen2.5:7b starts ignoring the tools and
# inventing answers instead. Routing to one agent's tools cuts the payload to
# roughly 1,000 tokens and restores correct tool selection.

GROUP_BY_CLASS = {
    "WeatherSearch": "weather",
    "Calendar_Agents": "calendar",
    "SpotifyAgent": "music",
    "GmailAgent": "email",
    "RemindersAgent": "reminders",
    "WebSearchAgents": "web",
}

def build_tool_groups(agents):
    """
    Buckets the registry by the agent that owns each method.

    Derived from AGENTS rather than hand-listed, so a new method joins its
    group automatically — the same no-drift property build_tool_registry gives.
    """
    groups = {}
    for agent in agents:
        group = GROUP_BY_CLASS.get(type(agent).__name__)
        if group is None:
            raise ValueError(f"{type(agent).__name__} has no entry in GROUP_BY_CLASS")
        for name in dir(agent):
            if name.startswith("_"):
                continue
            method = getattr(agent, name)
            if inspect.ismethod(method):
                groups.setdefault(group, []).append(method)
    return groups

TOOL_GROUPS = build_tool_groups(AGENTS)
ALL_TOOLS = list(TOOL_REGISTRY.values())

# Groups whose agent is switched off, so a request for one gets an honest
# "not set up" instead of the model making the answer up.
DISABLED_GROUPS = {GROUP_BY_CLASS[name]: why for name, why in DISABLED_AGENTS.items()}
SERVICE_NAMES = {"weather": "Weather", "calendar": "Google Calendar", "music": "Spotify",
                 "email": "Gmail", "reminders": "Reminders", "web": "Web search"}

# Checked before the classifier runs. A hit skips the LLM entirely, which is
# both faster and more reliable than asking a 1B model.
GROUP_KEYWORDS = {
    "weather": ("weather", "forecast", "temperature", "raining", "rain", "snow",
                "sunny", "humid", "wind", "how hot", "how cold", "degrees"),
    "reminders": ("remind", "reminder", "task list", "to-do", "todo", "don't let me forget"),
    "calendar": ("calendar", "schedule", "meeting", "appointment", "event", "am i free",
                 "what's on", "whats on", "book me"),
    "music": ("play ", "spotify", "song", "track", "album", "artist", "playlist",
              "skip", "pause the", "volume", "shuffle", "what's playing", "whats playing"),
    "email": ("email", "inbox", "gmail", "unread", "reply to", "send a mail", "draft"),
    "web": ("search the web", "look up", "google", "search for", "find online",
            "latest news", "research"),
}

def route_tools(text):
    """
    Picks the tool group for a command. Returns (group_name, tools) or (None, None)
    to mean 'no confident route, send everything'. A disabled group comes back
    with an empty tools list.
    """
    lowered = text.lower()
    for group, words in GROUP_KEYWORDS.items():
        if any(word in lowered for word in words):
            return group, TOOL_GROUPS.get(group, [])
    return None, None


system_prompt = f"""
    You are JARVIS (Just A Rather Very Intelligent System), an advanced AI assistant built to serve as a highly capable, loyal, and intelligent personal assistant.

    ## Context
    Today's date is {current_date}. Use this for any scheduling, calendar, or time-related tasks.

    ## Personality
    You speak with quiet confidence and calm authority. Your tone is casual but sharp — like a trusted right-hand who knows you well and doesn't waste your time. You have a dry wit that surfaces naturally, never forced. You are direct, precise, and never pad responses with fluff. You treat your user with the kind of familiar respect a close, highly competent aide would — you anticipate their needs and you're always in their corner.

    ## Communication Style
    - Respond conversationally. You are being spoken to out loud, so your responses must sound natural when heard, not read. No bullet points, headers, or markdown — speak in sentences.
    - Be concise. Get to the point. Skip affirmations like "Certainly!", "Of course!", or "Great question!" — just answer.
    - Match the energy of the request. Quick question gets a quick answer. Deep problem gets a thorough response.
    - If you don't know something, say so plainly and offer to find out. Never fabricate.
    - After delivering information, briefly invite the user to go deeper or ask a follow-up — one short sentence, never pushy.

    ## Capabilities
    You help with research, analysis, writing, coding, planning, scheduling, and reasoning through problems. When given tools, you use them efficiently and report back with only what's relevant.

    ## Memory
    Some messages begin with a bracketed note of what you know about the user from past sessions. Use it naturally, the way a long-serving aide would, without announcing that you remembered. Beyond those notes and this conversation you know nothing about the user's past — if asked about something not in either, say so plainly.

    ## Core Principles
    - Your user's goals are your goals. You advocate for their success.
    - You are proactive — if you notice something relevant, you mention it without being asked.
    - You do not moralize, lecture, or add unsolicited caveats. You trust your user's judgment.
    - You are never sycophantic. Honest, direct assessment beats flattery every time.
    - When something is outside your ability, say so immediately and suggest alternatives.
    - Never invent prior context, projects, people, or history that isn't in your memory notes or this conversation.
    - When starting fresh with no context, greet the user briefly and ask what they need. Nothing more.

    Respond only with your spoken reply. No meta-commentary, no explaining what you're about to do — just do it.
"""
messages = [{"role": "system", "content": system_prompt}]

"""EXIT_PHRASES = [
    # Direct goodbyes
    "goodbye", "good bye", "bye", "bye bye", "farewell",

    # Dismissals
    "that's all", "that is all", "that'll be all", "that will be all",
    "you're dismissed", "dismissed",

    # Sleep/standby commands
    "go to sleep", "sleep mode", "stand by", "standby",
    "power down", "shut down", "shutdown",

    # Session enders
    "we're done", "we are done", "i'm done", "i am done",
    "end session", "stop listening", "stop jarvis",
    "that's enough", "that is enough", "enough for now",

    # Natural conversation closers
    "talk later", "talk to you later", "we'll talk later",
    "catch you later", "until next time",

    # Explicit exits
    "exit", "quit", "close",
]"""

#classifier = pipeline("zero-shot-classification", model="typeform/distilbart-mnli-12-3")

candidate_labels = ["end_conversation", "continue_conversation"]

r = sr.Recognizer()

CHIME = object()          # queue marker: play the "your turn" tone
SLEEP_TONE = object()     # queue marker: play the "stopped listening" tone
_SPEAK_Q = queue.Queue()  # everything Jarvis says goes through here, in order


def _tone(freq, duration=0.15):
    sample_rate = 24000
    t = np.linspace(0, duration, int(sample_rate * duration))
    return (np.sin(2 * np.pi * freq * t) * 0.3).astype(np.float32)


def _chime_samples():
    """
    Small tone so the user knows when Jarvis has finished talking.
    """
    return _tone(880)


def _sleep_samples():
    """
    Falling two-note tone: the conversation is over and Jarvis is back to
    waiting for the wake word.
    """
    return np.concatenate([_tone(660, 0.12), _tone(440, 0.18)])


def _speaker_worker():
    """
    Single audio thread. Owns one persistent output stream and drains the speak
    queue forever.

    Two reasons this is a thread rather than an inline call. It lets the main
    loop keep working while Jarvis is still talking — the acknowledgement plays
    over the top of the model call instead of delaying it. And because every
    utterance is queued, ordering is preserved without any locking.
    """
    kokoro_ready.wait()
    stream = sd.OutputStream(samplerate=24000, channels=1, dtype='float32')
    stream.start()
    while True:
        item = _SPEAK_Q.get()
        try:
            if item is CHIME:
                stream.write(_chime_samples())
            elif item is SLEEP_TONE:
                stream.write(_sleep_samples())
            elif isinstance(item, threading.Event):
                item.set()          # flush marker: everything before this has played
            elif item:
                # stream.write blocks while the buffer is full, so synthesis of
                # the next chunk overlaps playback of the current one. Kokoro
                # runs at RTF 0.18, so it always stays ahead.
                for _, _, audio in kokoro_pipeline(item, voice='af_heart'):
                    stream.write(np.asarray(audio, dtype=np.float32))
        except Exception as e:
            print(f"TTS error: {e}")
        finally:
            _SPEAK_Q.task_done()


threading.Thread(target=_speaker_worker, daemon=True).start()


def say(text):
    """
    Queue text to be spoken. Returns immediately — it does not wait for playback.
    """
    if text and text.strip():
        _SPEAK_Q.put(text.strip())


def chime():
    _SPEAK_Q.put(CHIME)


def sleep_tone():
    _SPEAK_Q.put(SLEEP_TONE)


def wait_until_spoken():
    """
    Block until everything queued so far has actually finished playing.

    Used just before recording, so Jarvis never listens to himself.
    """
    marker = threading.Event()
    _SPEAK_Q.put(marker)
    marker.wait()


# Split only on punctuation followed by whitespace. Matching at end-of-buffer
# too would flush "72." out of a half-streamed "72.4" and mangle the number.
SENTENCE_END = re.compile(r'(?<=[.!?])\s+')


def speak_stream(stream_response):
    """
    Consume a streaming Ollama response and hand each finished sentence to the
    speaker as soon as it appears.

    Jarvis starts talking after the first sentence rather than after the last
    token, which is most of the perceived latency on a long answer.

    Returns the full text so it can still be appended to the message history.
    """
    buffer = ""
    spoken_any = False
    full = []
    for part in stream_response:
        piece = part.message.content or ""
        if not piece:
            continue
        full.append(piece)
        buffer += piece
        # Only flush on a sentence boundary — Kokoro's prosody falls apart if
        # it is fed half a clause at a time.
        while True:
            match = SENTENCE_END.search(buffer)
            if not match or match.end() == 0:
                break
            sentence, buffer = buffer[:match.end()].strip(), buffer[match.end():]
            if sentence:
                say(sentence)
                spoken_any = True
    if buffer.strip():
        say(buffer)
        spoken_any = True
    if not spoken_any:
        print("Warning: empty response, nothing to speak")
    return "".join(full).strip()

def play_audio_with_text_eleven_labs(text):
    """
    Not being used for right now, takes up money. But will be used for final release. Plays audio of Jarvis with text from LLM
    """
    audio = eleven_labs.text_to_speech.convert(
        text=text,
        voice_id="k7IRoeykhdGZUkTeJ1ID",
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
    )
    audio_bytes = b"".join(audio)
    print("Jarvis Talking Now")
    play(audio=audio_bytes)

def record_audio_and_transcribe_elevenlabs():
    """
    Not being used for right now, takes up money. But will be used for final release. Records user's prompt and request and transcribes for LLM usage
    """
    with sr.Microphone() as source:
        r.adjust_for_ambient_noise(source, duration=0.5)
        r.energy_threshold = 300
        r.pause_threshold = 0.8
        print("User Talks Now")
        audio_text = r.listen(source)
        wav_audio_data = audio_text.get_wav_data()
        transcription = eleven_labs.speech_to_text.convert(
            file = wav_audio_data,
            model_id="scribe_v2",
            tag_audio_events=True,
            language_code="eng",
            diarize=True,
        )
        return transcription.text

WHISPER_MODEL = "mlx-community/whisper-small-mlx"


def transcribe(samples):
    """
    Speech to text with MLX Whisper. Takes 16 kHz float32 samples from mic.py.
    """
    result = mlx_whisper.transcribe(samples, path_or_hf_repo=WHISPER_MODEL)
    text = result["text"].strip()
    print("User: ", text)
    return text


def record_audio_and_transcribe_mlx_whisper():
    """
    Current transcription method for user - free. Runs efficiently on Mac Silicone chip.
    Returns "" if nobody spoke before the listen timeout.
    """
    samples = mic.record_command()
    return transcribe(samples) if samples is not None else ""


def date_reference(days=8):
    """
    Today plus the coming days spelled out by name. Given only an ISO date,
    qwen2.5:7b works out weekdays wrong, so "remind me Friday" lands on the
    wrong day — and "next Friday" gets stored in memory as the wrong date.
    """
    now = datetime.now()
    upcoming = ", ".join(
        (now + timedelta(days=offset)).strftime("%A %Y-%m-%d")
        for offset in range(days)
    )
    return (f"Today is {now.strftime('%A %Y-%m-%d')} at {now.strftime('%H:%M')}. "
            f"Dates coming up: {upcoming}. "
            f"Use these exact dates for any day the user names.")


def extract_session_memory(conversation, known_topics=()):
    """
    Distils a finished conversation into structured memory with qwen2.5:7b:
    a short title, a multi-sentence summary with the specifics, lasting facts
    about the user, and the topics it touched.

    Topics are how memories connect — each becomes an Obsidian note that every
    later session mentioning it links to. The model is shown the topics that
    already exist so it reuses "Toronto" rather than inventing "Toronto, Canada".

    Returns None when nothing of substance happened. There is deliberately no
    "worth remembering" flag first: asked up front, before it has written
    anything, qwen dismissed a session where the user shared personal facts.
    """
    known = ", ".join(known_topics) if known_topics else "none yet"
    response = chat(
        model='qwen2.5:7b',
        keep_alive=OLLAMA_KEEP_ALIVE,
        format="json",
        options={"num_predict": 700},
        messages=[{"role": "user", "content": f"""You maintain Jarvis's long-term memory of the user. {date_reference(15)}

Read the conversation and reply with a JSON object with these keys:

"title": three to six words naming what the conversation was about.
"summary": three to five sentences in the past tense, written about "the user" — never "he" or "she", as the user's pronouns are unknown. Cover what the user said about themselves, what they wanted and why, what Jarvis did or found, anything decided or planned, and details likely to matter later. Keep the specifics: names, places, numbers and dates. Write relative dates like "next Friday" as calendar dates from the list above, and trust what the user said over anything Jarvis claimed. Skip pleasantries and Jarvis's offers of further help. Use "" only if nothing of substance happened, such as a bare greeting.
"facts": lasting facts the user revealed — preferences, background, habits, relationships, goals, projects. Each a complete sentence. A fact about the user starts with "The user"; a fact about someone else names that person (for example "The user's sister Priya loves jazz."). Only what the user actually said, nothing inferred. Use [] if there are none.
"topics": up to six people, places, projects, interests or recurring tasks that this conversation actually discussed, each as {{"name": "...", "note": "one sentence on what this conversation said about it"}}. Names in Title Case and singular. Topics that already exist: {known}. When one of those is discussed, reuse its name exactly — but never include a topic just because it exists.

Conversation:
{conversation}"""}]
    )
    try:
        data = json.loads(response.message.content)
    except json.JSONDecodeError:
        print(f"Memory extraction returned invalid JSON: {response.message.content[:200]}")
        return None

    summary = str(data.get("summary") or "").strip()
    facts = [str(f).strip() for f in data.get("facts") or [] if str(f).strip()]
    topics = []
    for topic in data.get("topics") or []:
        if isinstance(topic, dict) and str(topic.get("name") or "").strip():
            topics.append({"name": str(topic["name"]).strip(),
                           "note": str(topic.get("note") or "").strip()})
    # The model always writes *some* summary, even for "Hey Jarvis" and nothing
    # else, so substance is judged by what it found rather than what it says.
    if not summary or (not facts and not topics):
        return None
    return {
        "title": str(data.get("title") or "").strip(),
        "summary": summary,
        "facts": facts,
        "topics": topics[:6],
    }


def relevant_topics(conversation, known_topics):
    """
    The existing topics this conversation plausibly touches — any word of the
    name (4+ letters) appears in it. Showing the model every topic in the vault
    made it attach unrelated ones ("Weather: not discussed").
    """
    text = conversation.lower()
    return [name for name in known_topics
            if any(len(word) >= 4 and word in text for word in name.lower().split())]


def save_session_memories(transcript):
    """
    Distils a finished session into both memory stores: the facts and the
    summary go to Pinecone for recall, and to the Obsidian vault — a daily note
    plus a note per topic — for the user to read and browse as a graph.

    Runs on a background thread, so it must never raise — a failure in one
    store is logged and doesn't stop the other.
    """
    if not transcript:
        return
    # Built from the raw turns, not `messages`, so the injected memory notes
    # aren't re-extracted as if the user had just said them.
    conversation = "\n".join(f"{who}: {text}" for who, text in transcript)
    try:
        known = relevant_topics(conversation, obsidian_store.existing_topics())
        memory = extract_session_memory(conversation, known)
    except Exception as e:
        print(f"Memory extraction failed: {e}")
        return
    if memory is None:
        print("Nothing worth remembering from this session")
        return

    now = datetime.now()
    stamp = int(now.timestamp())
    records = [{"id": f"mem-{stamp}-{i}", "chunk_text": fact} for i, fact in enumerate(memory["facts"])]
    # The whole summary, not a one-liner, so recall brings back the context too
    records.append({"id": f"episode-{stamp}",
                    "chunk_text": f"On {now.strftime('%A, %B %d, %Y')}: {memory['summary']}"})
    print(f"Storing {len(records)} memories: {[rec['chunk_text'] for rec in records]}")
    if dense_index is not None:
        try:
            dense_index.upsert_records(namespace="jarvis-memory-namespace", records=records)
        except Exception as e:
            print(f"Pinecone write failed: {e}")

    try:
        note = obsidian_store.append_session(memory["summary"], memory["facts"], memory["topics"],
                                             title=memory["title"], when=now)
        if note:
            print(f"Obsidian note updated: {note}")
    except Exception as e:
        print(f"Obsidian write failed: {e}")


_memory_threads = []


def remember_in_background(transcript):
    """
    Save a session's memories without making the user wait for two LLM calls.
    Non-daemon, and tracked, so shutdown() can make sure nothing is lost.
    """
    if not transcript:
        return
    thread = threading.Thread(target=save_session_memories, args=(list(transcript),))
    thread.start()
    _memory_threads.append(thread)


def flush_memories():
    """Block until every pending memory write has finished."""
    while _memory_threads:
        _memory_threads.pop().join()

def retrieve_memories(query: str, top_k: int = 5):
    """
    Retrieves most meaningful messages from Pinecone Vector DB for conversation context
    """
    if dense_index is None:
        return []
    results = dense_index.search(
        namespace="jarvis-memory-namespace",
        query={"inputs": {"text": query}, "top_k": top_k},
        fields=["chunk_text"]
    )
    memories = [hit["fields"]["chunk_text"] for hit in results["result"]["hits"]]
    return memories

def classify_intent(text):
    """
    Returns 'exit', 'tool', or 'chat'.
    """
    response = chat(
            model='llama3.2:1b',
            keep_alive=OLLAMA_KEEP_ALIVE,
            options={"num_predict": 4},
            messages=[{"role": "user", "content":
                    f"""Classify this message. Reply with exactly one word only: exit, tool, or chat.

            exit = user wants to end the conversation
            tool = user wants real-world action or data (weather, calendar, spotify, web search)
            chat = general conversation or questions

            Message: "{text}"

            One word answer:"""}]
    )
    result = response.message.content.strip().lower()
    first_word = result.split()[0] if result else "chat"
    if first_word not in ("exit", "tool", "chat"):
        return "chat"
    return first_word


def classify_tool_group(text):
    """
    Second-stage router, only reached when no keyword matched.

    Asks the 1B model which subsystem the command belongs to. A wrong answer is
    survivable — the tool call retries with the full registry if the routed
    group produces nothing — so speed matters more than precision here.
    """
    try:
        response = chat(
            model='llama3.2:1b',
            keep_alive=OLLAMA_KEEP_ALIVE,
            options={"num_predict": 4},
            messages=[{"role": "user", "content":
                    f"""Which system handles this request? Reply with exactly one word.

            weather = forecasts, temperature, conditions
            calendar = events, meetings, schedule
            reminders = reminders, tasks, to-do items
            music = Spotify, songs, playback, volume
            email = Gmail, inbox, messages
            web = searching the internet for information

            Request: "{text}"

            One word answer:"""}]
        )
        guess = response.message.content.strip().lower().split()[0].strip(".,")
        if guess in TOOL_GROUPS:
            return guess
    except Exception as e:
        print(f"Group classifier failed: {e}")
    return None

def safe_speak(text):
    """
    Queue text for playback, guarding against the empty strings that tool
    summaries occasionally produce.
    """
    if not text or not text.strip():
        print("Warning: empty response, skipping TTS")
        return
    say(text)


def prewarm():
    """
    Load both Ollama models and Whisper during startup instead of on the user's
    first command, which otherwise costs 4.4s + 2.3s + the Whisper load.
    """
    try:
        for model in OLLAMA_MODELS:
            chat(model=model, keep_alive=OLLAMA_KEEP_ALIVE,
                 options={"num_predict": 1},
                 messages=[{"role": "user", "content": "hi"}])
        mlx_whisper.transcribe(np.zeros(mic.WHISPER_RATE, dtype=np.float32),
                               path_or_hf_repo=WHISPER_MODEL)
        print("Models prewarmed")
    except Exception as e:
        print(f"Prewarm skipped: {e}")


def select_tools(text):
    """
    Chooses which schemas to send. Keyword rules first, then the 1B classifier,
    then everything as a last resort.
    """
    group, tools = route_tools(text)
    if tools is None:
        group = classify_tool_group(text)
        tools = TOOL_GROUPS[group] if group else None
    if tools is None:
        return "ALL", ALL_TOOLS
    return group, tools


def not_set_up_reply(group):
    """Spoken when a request routes to an agent that is switched off."""
    # Kept speakable: Kokoro reading out OPENWEATHER_API_KEY letter by letter helps nobody
    why = DISABLED_GROUPS.get(group, "")
    service = SERVICE_NAMES.get(group, group)
    if "credentials.json" in why:
        return f"{service} isn't set up yet, sir. The Google credentials file is missing from my folder."
    if why.startswith("missing"):
        return f"{service} isn't set up yet, sir. Its API key needs adding to my settings file."
    return f"{service} isn't available right now, sir. It failed to start — the log has the details."


def handle_turn(transcribed_text, transcript):
    """
    Answer one command. Returns True when the user has ended the conversation.

    `transcript` collects the raw (speaker, text) turns for memory extraction.
    """
    # Dispatch table and tool schemas both come from the shared registry
    available_functions = TOOL_REGISTRY
    spoken = ""
    turn_start = time.time()
    transcript.append(("User", transcribed_text))

    # Intent and memory both depend only on the transcript and nothing else,
    # so run them together. Pinecone is a network call that has hit 1.6s.
    parallel = {}
    def _classify():
        parallel["intent"] = classify_intent(transcribed_text)
    def _recall():
        try:
            parallel["memories"] = retrieve_memories(transcribed_text)
        except Exception as e:
            print(f"Memory retrieval failed: {e}")
            parallel["memories"] = []
    threads = [threading.Thread(target=_classify), threading.Thread(target=_recall)]
    for t in threads: t.start()
    for t in threads: t.join()

    intent = parallel.get("intent", "chat")
    memories = parallel.get("memories", [])
    print(f"Intent: {intent}  (routing took {time.time() - turn_start:.2f}s)")

    user_content = transcribed_text
    if memories:
        memory_block = "\n".join(f"- {m}" for m in memories)
        # Memories ride on the user message, never messages[0]. Rewriting the
        # system prompt changed the first tokens of the prompt and threw away
        # Ollama's prefix cache every single turn — a measured 13.8s penalty.
        user_content = f"[What you know about the user:\n{memory_block}]\n\n{transcribed_text}"

    messages.append({"role": "user", "content": user_content})

    if intent == 'exit':
        #User is leaving or conversation is done
        completion = chat(model="qwen2.5:7b", messages=messages, stream=True,
                          keep_alive=OLLAMA_KEEP_ALIVE, options=GEN_OPTIONS)
        spoken = speak_stream(completion)
        messages.append({"role": "assistant", "content": spoken})
        transcript.append(("Jarvis", spoken))
        # Memories are saved by run_session once the conversation closes
        return True

    elif intent == 'tool':
        #Needs tool usage
        group, tools = select_tools(transcribed_text)
        # Checked before "Right away sir" — promising action and then saying
        # the service is off would be worse than just saying so.
        if group in DISABLED_GROUPS:
            spoken = not_set_up_reply(group)
            print(f"Tool group '{group}' is off: {DISABLED_GROUPS[group]}")
            say(spoken)
            messages.append({"role": "assistant", "content": spoken})
            transcript.append(("Jarvis", spoken))
            chime()
            return False
        # Queued, not blocking — this plays over the model call instead of
        # delaying it by the 2.2s it takes to speak.
        say("Right away sir.")
        print(f"Tool group: {group} ({len(tools)} tools)")

        date_context = f"[{date_reference()}]"
        dated_messages = messages[:-1] + [{
            "role": "user",
            "content": f"{date_context} {messages[-1]['content']}"
        }]
        llm_start = time.time()
        response: ChatResponse = chat(
            model='qwen2.5:7b',
            messages=dated_messages,
            tools=tools,
            keep_alive=OLLAMA_KEEP_ALIVE,
        )

        # A misrouted group means the right tool was never offered. Retry once
        # with everything rather than answering wrongly — this costs the old
        # latency in the rare miss instead of paying it on every command.
        if not response.message.tool_calls and tools is not ALL_TOOLS:
            print(f"No tool call from group '{group}' — retrying with all {len(ALL_TOOLS)} tools")
            tools = ALL_TOOLS
            response: ChatResponse = chat(
                model='qwen2.5:7b',
                messages=dated_messages,
                tools=tools,
                keep_alive=OLLAMA_KEEP_ALIVE,
            )
        print(f"Tool selection took {time.time() - llm_start:.2f}s")

        messages.append({"role": "assistant", "content": response.message.content or ""})

        if response.message.tool_calls: #Loops through all required tool calls to finish task
            for tool_call in response.message.tool_calls:
                if tool_call.function.name in available_functions:
                    print(f"Calling {tool_call.function.name} with {tool_call.function.arguments}")
                    try:
                        result = available_functions[tool_call.function.name](**tool_call.function.arguments) #Calls tool calls to complete task
                    except Exception as e:
                        # Hand the failure back to the model as text so it can
                        # explain itself instead of crashing the session.
                        result = f"That tool failed: {e}"
                    print(f"Tool result: {result}")
                    messages.append({"role": "tool", "tool_name": tool_call.function.name, "content": str(result)})

            messages.append({
                "role": "user",
                "content": "Summarize the tool results naturally in Jarvis's voice. Two sentences at most. Do not call any more tools."
            }) #Summarizes what was just done

            # Same tools list as the call above on purpose. Ollama keeps one
            # KV cache slot per model, so a summarisation request with a
            # different prefix evicted the tool prefix and forced a full
            # reprocess on the next command — measured 41ms vs 13,524ms.
            follow_up = chat(model='qwen2.5:7b', messages=messages, tools=tools,
                             stream=True, keep_alive=OLLAMA_KEEP_ALIVE,
                             options=GEN_OPTIONS)
            spoken = speak_stream(follow_up)
            messages.append({"role": "assistant", "content": spoken})
        else:
            spoken = response.message.content or response.message.thinking or ""
            safe_speak(spoken)

    else:  # chat
        # Same dated copy as the tool path. Without it Jarvis states wrong dates
        # ("next Friday" became a Wednesday), and memory then records them.
        dated_messages = messages[:-1] + [{
            "role": "user",
            "content": f"[{date_reference()}] {messages[-1]['content']}"
        }]
        response = chat(model='qwen2.5:7b', messages=dated_messages, stream=True,
                        keep_alive=OLLAMA_KEEP_ALIVE, options=GEN_OPTIONS)
        spoken = speak_stream(response)
        messages.append({"role": "assistant", "content": spoken})

    print("Jarvis:", spoken)
    print(f"Turn latency (transcript -> speech queued): {time.time() - turn_start:.2f}s")
    transcript.append(("Jarvis", spoken))
    chime()
    return False


def startup():
    """
    Blocks until Kokoro and both Ollama models are ready.
    """
    # Prewarm runs while Kokoro is still loading, so the model loads are free.
    prewarm_thread = threading.Thread(target=prewarm, daemon=True)
    prewarm_thread.start()
    kokoro_ready.wait()
    prewarm_thread.join()
    print(f"Startup complete in {time.time() - start_time:.2f}s")


def run_session(first_audio=None):
    """
    One conversation, from wake word to goodbye.

    Keeps listening for follow-ups without the wake word until the user says
    goodbye or stays quiet for mic.LISTEN_TIMEOUT seconds. Then the falling tone
    plays, memories are saved in the background, and the history is cleared so
    the next conversation starts clean.

    first_audio is a command already recorded while the engine was loading.
    """
    transcript = []
    pending = first_audio
    while True:
        # Never start recording while Jarvis is still talking, or the mic picks
        # him up. Playback is asynchronous, so this has to be explicit.
        wait_until_spoken()
        samples = pending if pending is not None else mic.record_command()
        pending = None
        if samples is None:
            print("No speech — ending conversation")
            break
        text = transcribe(samples)
        if not text:
            continue
        if handle_turn(text, transcript):
            break

    sleep_tone()
    wait_until_spoken()
    del messages[1:]
    remember_in_background(transcript)


def shutdown():
    """
    Called before the engine process exits: finish memory writes, then hand
    the Ollama models' memory back instead of waiting out keep_alive.
    """
    flush_memories()
    for model in OLLAMA_MODELS:
        try:
            generate(model=model, prompt="", keep_alive=0)
        except Exception as e:
            print(f"Could not unload {model}: {e}")


# Guarded so tests.py can import this module without launching the assistant.
# Running this file directly holds a single conversation with no wake word —
# handy for debugging and for completing the Google and Spotify logins the
# first time. The background assistant is wake_listener.py.
if __name__ == "__main__":
    startup()
    run_session()
    shutdown()
