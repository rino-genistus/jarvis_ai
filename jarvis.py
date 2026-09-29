from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import os
import speech_recognition as sr
import mlx_whisper
from ollama import chat, embed, generate, ChatResponse, Message
import time
from agents import (Calendar_Agents, WebSearchAgents, WeatherSearch, SpotifyAgent, GmailAgent,
                    RemindersAgent, EverydayToolsAgent, PreferencesAgent)
import mic
import obsidian_store
import memory_store
import preferences
import tool_catalog
from datetime import datetime, timedelta
from kokoro import KPipeline
import sounddevice as sd
import numpy as np
import threading
import inspect
import json
import queue
import random
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

def open_memory_store():
    """
    On-device vector memory (Chroma + local embeddings). Seeded from the
    Obsidian vault the first time, so recall starts with what the notes hold.
    If it can't open, recall falls back to keyword search of the notes.
    """
    try:
        store = memory_store.MemoryStore(keep_alive=OLLAMA_KEEP_ALIVE)
        seeded = store.seed_from_obsidian(obsidian_store.all_records())
        if seeded:
            print(f"Memory store seeded with {seeded} memories from the Obsidian vault")
        print(f"Memory store: {store.count()} memories")
        return store
    except Exception as e:
        print(f"Memory store unavailable ({e}) — recall will search the Obsidian notes")
        return None


memory = open_memory_store()

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
    (EverydayToolsAgent, []),
    (PreferencesAgent, []),
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
# Tool selection time is prompt reading, at ~200 tokens a second for qwen2.5:7b
# on an M4. Sending all 55 schemas would be ~6,500 tokens and half a minute;
# at that size qwen also starts ignoring the tools and inventing answers. So a
# request is routed to one agent (GROUP_KEYWORDS), then narrowed to the few
# tools inside it that its words call for (tool_catalog.AGENT_TOOLS).

GROUP_BY_CLASS = {
    "WeatherSearch": "weather",
    "Calendar_Agents": "calendar",
    "SpotifyAgent": "music",
    "GmailAgent": "email",
    "RemindersAgent": "reminders",
    "WebSearchAgents": "web",
    "EverydayToolsAgent": "everyday",
    "PreferencesAgent": "preferences",
}
CLASS_BY_GROUP = {group: name for name, group in GROUP_BY_CLASS.items()}

def build_tool_groups(agents):
    """
    group -> the running agent's tools, in catalogue order.

    Checked against the registry both ways: a public method missing from the
    catalogue would never be offered to the model, and a catalogue name no
    agent defines is a typo that would never be offered either.
    """
    catalogued = {name for tools in tool_catalog.AGENT_TOOLS.values() for name in tools}
    unlisted = sorted(set(TOOL_REGISTRY) - catalogued)
    if unlisted:
        raise ValueError(f"Tools missing from tool_catalog.AGENT_TOOLS: {', '.join(unlisted)}")
    known_classes = [cls for cls, _ in AGENT_REQUIREMENTS]
    running = {type(agent).__name__ for agent in agents}
    groups = {}
    for agent_name, tools in tool_catalog.AGENT_TOOLS.items():
        if agent_name not in GROUP_BY_CLASS:
            raise ValueError(f"{agent_name} is in tool_catalog but has no entry in GROUP_BY_CLASS")
        typos = [n for n in tools if not any(hasattr(cls, n) for cls in known_classes)]
        if typos:
            raise ValueError(f"tool_catalog lists tools no agent defines: {', '.join(typos)}")
        if agent_name in running:
            # A tool borrowed from an agent that's off is simply left out
            groups[GROUP_BY_CLASS[agent_name]] = [TOOL_REGISTRY[n] for n in tools if n in TOOL_REGISTRY]
    return groups

TOOL_GROUPS = build_tool_groups(AGENTS)
ALL_TOOLS = list(TOOL_REGISTRY.values())

# Groups whose agent is switched off, so a request for one gets an honest
# "not set up" instead of the model making the answer up.
DISABLED_GROUPS = {GROUP_BY_CLASS[name]: why for name, why in DISABLED_AGENTS.items()}
SERVICE_NAMES = {"weather": "Weather", "calendar": "Google Calendar", "music": "Spotify",
                 "email": "Gmail", "reminders": "Reminders", "web": "Web search",
                 "everyday": "Everyday tools", "preferences": "Preferences"}

# Checked before the classifier runs. A hit skips the LLM entirely, which is
# both faster and more reliable than asking a 1B model.
GROUP_KEYWORDS = {
    # First, so "I prefer temperatures in Celsius" saves a preference rather
    # than fetching the weather
    "preferences": ("i prefer", "i'd prefer", "i would prefer", "from now on", "call me ",
                    "always use", "never use", "i live in", "my home is", "my preferences",
                    "forget my", "remember that i"),
    "weather": ("weather", "forecast", "temperature", "raining", "rain", "snow",
                "sunny", "humid", "wind", "how hot", "how cold", "degrees",
                "storm", "hurricane", "tornado", "weather alert", "weather warning"),
    "reminders": ("remind", "reminder", "task list", "to-do", "todo", "don't let me forget"),
    "calendar": ("calendar", "schedule", "meeting", "appointment", "event", "am i free",
                 "what's on", "whats on", "book me"),
    "music": ("play ", "spotify", "song", "track", "album", "artist", "playlist",
              "skip", "pause the", "volume", "shuffle", "what's playing", "whats playing"),
    "email": ("email", "inbox", "gmail", "unread", "reply to", "send a mail", "draft"),
    "web": ("search the web", "look up", "google", "search for", "find online",
            "latest news", "research"),
    "everyday": ("contact", "phone number", "number for", "address for", "email address",
                 "birthday", "my notes", "a note", "notes about", "in notes", "apple notes",
                 "what time is it", "what's the date", "what date", "what day", "days until",
                 "how many days", "how long until"),
}

# Groups whose keywords are specific enough to overrule the intent classifier
LOOKUP_GROUPS = {"weather", "calendar", "email", "reminders", "web", "everyday"}

REQUEST_OPENERS = {"what", "what's", "whats", "how", "is", "are", "will", "does", "do", "did",
                   "any", "can", "could", "would", "should", "when", "where", "which", "who",
                   "tell", "check", "give", "show", "find", "get", "look", "search", "read",
                   "remind", "add", "create", "set", "schedule", "book", "send", "reply", "move",
                   "cancel", "delete", "make", "put", "list", "email", "mark", "trash", "draft",
                   "play", "pause", "resume", "skip", "stop", "shuffle", "queue", "turn", "next",
                   "previous", "complete", "update", "change", "clear"}
EMBEDDED_REQUEST = re.compile(r"\b(remind me|can you|could you|would you|please|i need you to|"
                              r"i want you to|i'd like you to|let me know)\b")


def looks_like_request(text):
    """
    True for questions and commands, false for statements. Whisper punctuates
    reliably, so each sentence is checked: a '?', a command verb up front, or
    an embedded ask ("...on Friday. Remind me.", "can you...").

    "How hot is it outside?" and "Play some jazz." are requests; "I prefer
    temperatures in Celsius." and "My sister's birthday is next week." are not.
    """
    lowered = text.strip().lower()
    if EMBEDDED_REQUEST.search(lowered):
        return True
    for sentence in re.split(r"(?<=[.!?])\s+", lowered):
        if sentence.endswith("?"):
            return True
        words = re.sub(r"^(hey |ok |okay |so |and |oh |jarvis,? |please )+", "", sentence).split()
        if words and words[0].strip(",.!") in REQUEST_OPENERS:
            return True
    return False


def decide_intent(text, classified):
    """
    Final intent from the classifier's answer plus two guards:

    - A clear request whose keywords point at a lookup service is a tool call
      even if the classifier said chat — otherwise qwen invents a forecast.
      Music is left out: "skip", "play" and "track" turn up in ordinary talk.
    - A statement is never a tool call. Offered calendar tools for "my
      sister's birthday is next week", the model may create an event nobody
      asked for.
    """
    group = route_tools(text)[0]
    # Preferences are the exception to "statements never trigger tools":
    # "I prefer Celsius" is a statement, and saving it is the whole point.
    if group == "preferences" and classified != "exit":
        return "tool"
    if classified == "chat" and group in LOOKUP_GROUPS and looks_like_request(text):
        return "tool"
    if classified == "tool" and not looks_like_request(text):
        return "chat"
    return classified


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


def build_system_prompt():
    """
    The system prompt, rebuilt at the start of every conversation so today's
    date and the user's preferences are current even when the engine has been
    warm for hours. Stable within a conversation, which keeps Ollama's prefix
    cache intact.
    """
    current_date = datetime.now().strftime("%A, %B %d, %Y")
    return f"""
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

    {preferences.prompt_block()}
    Respond only with your spoken reply. No meta-commentary, no explaining what you're about to do — just do it.
"""
system_prompt = build_system_prompt()
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
    return transcribe(samples) if samples is not None and samples is not mic.NOISE else ""


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
"preferences": standing preferences the user explicitly asked Jarvis to keep ("I prefer...", "from now on...", "call me...", "I live in..."), as {{"temperature_units": "celsius" or "fahrenheit" or null, "home_location": a city or null, "address_as": what to call the user or null, "other": [short sentences]}}. Use null and [] for anything not explicitly stated — a place the user is travelling to is not their home.

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
    prefs = data.get("preferences") if isinstance(data.get("preferences"), dict) else {}
    return {
        "title": str(data.get("title") or "").strip(),
        "summary": summary,
        "facts": facts,
        "topics": topics[:6],
        "preferences": prefs,
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
    summary go to the on-device vector store for recall, and to the Obsidian
    vault — a daily note plus a note per topic — for the user to read and
    browse as a graph.

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
        session = extract_session_memory(conversation, known)
    except Exception as e:
        print(f"Memory extraction failed: {e}")
        return
    if session is None:
        print("Nothing worth remembering from this session")
        return

    # Standing preferences the tool path didn't already save ("I prefer
    # Celsius" said mid-answer, say) are kept from here on
    for name, value in (session.get("preferences") or {}).items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item and str(item).strip().lower() not in ("null", "none"):
                print(f"Preference learned: {preferences.set_preference('note' if name == 'other' else name, item)}")

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    records = [{"text": fact, "kind": "fact", "date": day} for fact in session["facts"]]
    # The whole summary, not a one-liner, so recall brings back the context too
    records.append({"text": f"On {now.strftime('%A, %B %d, %Y')}: {session['summary']}",
                    "kind": "session", "date": day})
    print(f"Storing {len(records)} memories: {[rec['text'] for rec in records]}")
    if memory is not None:
        try:
            memory.add(records)
        except Exception as e:
            print(f"Memory store write failed: {e}")

    try:
        note = obsidian_store.append_session(session["summary"], session["facts"], session["topics"],
                                             title=session["title"], when=now)
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
    Memories relevant to the query, most detailed tier first: semantic search
    of the on-device vector store, then keyword search of the Obsidian notes
    if the store — or the embedding model it needs — is unavailable.
    """
    if memory is not None:
        try:
            return memory.search(query, top_k)
        except Exception as e:
            print(f"Vector recall failed ({e}) — searching the Obsidian notes instead")
    return obsidian_store.search(query, top_k)


INTENT_PROMPT = """Classify the user's message to a voice assistant. Reply with exactly one word: exit, tool, or chat.

exit = the user is ending the conversation: goodbyes, "that's all", "I'm done", "go to sleep".
tool = the user wants something done or looked up: weather, calendar, email, reminders, music, the web, contacts, notes, exact dates, or saving a preference ("from now on...", "call me...").
chat = everything else: questions answered from general knowledge, jokes, explanations, and statements or remarks about themselves.

Examples:
"Thanks, that'll be all." -> exit
"Okay, bye." -> exit
"Go to sleep." -> exit
"What's on my calendar tomorrow?" -> tool
"Remind me to buy milk." -> tool
"Play some music." -> tool
"Skip this track." -> tool
"Is it cold outside?" -> tool
"Tell me a joke." -> chat
"What's the capital of Japan?" -> chat
"I had a long day at work." -> chat
"Thanks, that's helpful." -> chat

Message: "{text}"
Answer:"""


def classify_intent(text):
    """
    Returns 'exit', 'tool', or 'chat'.

    qwen2.5:7b with worked examples, not llama3.2:1b. On a fixed set of 29
    labelled phrases the 1B model scored 13 — it never once recognised a
    goodbye — while this scores 27 for about 170ms more per turn. qwen is
    already resident, so there's no load cost. decide_intent() then guards it.
    """
    response = chat(model='qwen2.5:7b', keep_alive=OLLAMA_KEEP_ALIVE,
                    options={"num_predict": 3, "temperature": 0},
                    messages=[{"role": "user", "content": INTENT_PROMPT.format(text=text)}])
    word = (response.message.content.strip().lower().split() or ["chat"])[0].strip(".,\"'")
    return word if word in ("exit", "tool", "chat") else "chat"


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
        if memory is not None:
            memory.search("warm up")    # loads the embedding model
        print("Models prewarmed")
    except Exception as e:
        print(f"Prewarm skipped: {e}")


def select_tools(text):
    """
    (group, tools) for a tool request: the agent from keyword rules or the 1B
    classifier, then only the tools within it that the request's words call
    for. Everything as a last resort. A group whose agent is off comes back
    with no tools.
    """
    group, _ = route_tools(text)
    if group is None:
        group = classify_tool_group(text)
    if group is None:
        return "ALL", ALL_TOOLS
    if group not in TOOL_GROUPS:
        return group, []
    names = tool_catalog.pick(CLASS_BY_GROUP[group], text)
    return group, [TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY] or TOOL_GROUPS[group]


# For requests with no catalogue entry to phrase an acknowledgement from —
# the rare "send everything" route. Each tool's own are in tool_catalog.
GENERIC_ACKNOWLEDGEMENTS = ["One moment.", "Let me take care of that.", "Sure, one second."]
_last_acknowledgement = None

def _default_music_call(text):
    """'Play some jazz' -> queue a search for 'jazz'. Anything else has no safe default."""
    match = re.match(r"^(?:hey |ok |okay |jarvis,? |please )*play (?:me |some |a little |a bit of |the song )*(.+?)[.!?]*$",
                     text.strip(), re.IGNORECASE)
    return ("search_song_and_queue", {"query": match.group(1)}) if match else None


# What to run when a request clearly belongs to a group but the model makes no
# tool call — qwen2.5:7b narrates "I'll check the weather at home..." or
# declines "play some jazz" outright, even at temperature 0. Each entry maps
# the utterance to (tool, args), or None when there's no obvious action.
def _default_everyday_call(text):
    """Time and date questions: the date tools parse the whole question fine."""
    lowered = text.lower()
    if re.search(r"\bwhat time\b|\btime is it\b", lowered):
        return ("get_current_time", {})
    if re.search(r"\b(date|day|days|week|weeks|month|tomorrow|yesterday)\b", lowered):
        return ("resolve_date", {"expression": text.rstrip("?.! ")})
    return None


DEFAULT_CALLS = {
    "weather": lambda text: ("get_current_weather", {"location": "home"}),
    "music": _default_music_call,
    "everyday": _default_everyday_call,
}


def acknowledgement(group, tools):
    """
    What Jarvis says as he starts, before the model has decided anything:
    phrased for the best-matching tool ("Let me see what's come in" for unread
    email, "Sure, I'll put that in your calendar" for a new event), and never
    the same phrase twice running.
    """
    global _last_acknowledgement
    lead = tools[0].__name__ if tools else None
    phrases = tool_catalog.acknowledgements(CLASS_BY_GROUP.get(group), lead) or GENERIC_ACKNOWLEDGEMENTS
    options = [p for p in phrases if p != _last_acknowledgement] or list(phrases)
    _last_acknowledgement = random.choice(options)
    return _last_acknowledgement.replace("sir", preferences.address())


def not_set_up_reply(group):
    """Spoken when a request routes to an agent that is switched off."""
    # Kept speakable: Kokoro reading out OPENWEATHER_API_KEY letter by letter helps nobody
    why = DISABLED_GROUPS.get(group, "")
    service = SERVICE_NAMES.get(group, group)
    if "credentials.json" in why:
        return f"{service} isn't set up yet, {preferences.address()}. The Google credentials file is missing from my folder."
    if why.startswith("missing"):
        return f"{service} isn't set up yet, {preferences.address()}. Its API key needs adding to my settings file."
    return f"{service} isn't available right now, {preferences.address()}. It failed to start — the log has the details."


# The tool path's system prompt: the persona in brief and nothing else. The
# full one is ~600 tokens — three seconds of prompt reading on every tool call
# once another call has pushed it out of Ollama's cache, which is most turns.
TOOL_PROMPT = """You are JARVIS, the user's personal assistant: calm, concise, with a dry wit.
Everything you say is spoken aloud, so talk in plain sentences, never lists or markdown.
Use the tools to do what the user asks. Never claim something was done that a tool didn't do or report.
Some messages begin with a bracketed note of what you know about the user; use it naturally."""

# Earlier turns given to the tool call, for follow-ups like "reply to that one"
TOOL_CONTEXT_TURNS = 4
# Tool output kept in the conversation for later turns; the tool call itself sees all of it
TOOL_RESULT_HISTORY_CHARS = 1200

# A goodbye that happens to mention the weather is still a goodbye
GOODBYE = re.compile(r"\b(bye|goodbye|good night|goodnight|that's all|that'll be all|that is all|"
                     r"i'm done|we're done|go to sleep|stop listening)\b")


def tool_messages(user_content, transcript):
    """
    The short conversation a tool call runs on: the brief persona, the last
    few turns in plain words, and the request with today's dates.
    """
    recent = [{"role": "user" if who == "User" else "assistant", "content": text}
              for who, text in transcript[-TOOL_CONTEXT_TURNS - 1:-1]]
    return ([{"role": "system", "content": TOOL_PROMPT + preferences.prompt_block()}] + recent
            + [{"role": "user", "content": f"[{date_reference()}] {user_content}"}])


def handle_turn(transcribed_text, transcript):
    """
    Answer one command. Returns True when the user has ended the conversation.

    `transcript` collects the raw (speaker, text) turns for memory extraction.
    """
    # Dispatch table and tool schemas both come from the shared registry
    available_functions = TOOL_REGISTRY
    spoken = ""
    ack = None
    group = tools = None
    turn_start = time.time()
    transcript.append(("User", transcribed_text))

    def _recall():
        try:
            return retrieve_memories(transcribed_text)
        except Exception as e:
            print(f"Memory retrieval failed: {e}")
            return []

    # A clear request for a lookup ("what's on my calendar tomorrow?") is a
    # tool call whatever the classifier says — decide_intent overrules it — so
    # skip the classifier and acknowledge straight away. The acknowledgement
    # then plays over memory recall and tool selection instead of after them.
    if decide_intent(transcribed_text, "chat") == "tool" and not GOODBYE.search(transcribed_text.lower()):
        intent = "tool"
        group, tools = select_tools(transcribed_text)
        if group not in DISABLED_GROUPS:
            ack = acknowledgement(group, tools)
            say(ack)
        print(f"Intent: tool, keyword fast path  (acknowledged after {time.time() - turn_start:.2f}s)")
        memories = _recall()
    else:
        # Intent and memory both depend only on the transcript, so run them together
        parallel = {}
        def _classify():
            parallel["intent"] = classify_intent(transcribed_text)
        def _recall_into():
            parallel["memories"] = _recall()
        threads = [threading.Thread(target=_classify), threading.Thread(target=_recall_into)]
        for t in threads: t.start()
        for t in threads: t.join()

        intent = parallel.get("intent", "chat")
        memories = parallel.get("memories", [])

        decided = decide_intent(transcribed_text, intent)
        if decided != intent:
            print(f"Intent override: {intent} -> {decided}")
            intent = decided
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
        if group is None:
            group, tools = select_tools(transcribed_text)
        # Checked before the acknowledgement — promising action and then saying
        # the service is off would be worse than just saying so.
        if group in DISABLED_GROUPS:
            spoken = not_set_up_reply(group)
            print(f"Tool group '{group}' is off: {DISABLED_GROUPS[group]}")
            say(spoken)
            messages.append({"role": "assistant", "content": spoken})
            transcript.append(("Jarvis", spoken))
            chime()
            return False
        if ack is None:
            # Queued, not blocking — this plays over the model call instead of
            # delaying it by the time it takes to speak.
            ack = acknowledgement(group, tools)
            say(ack)
        print(f"Tool group: {group} — offering {[t.__name__ for t in tools]}")

        lean = tool_messages(user_content, transcript)
        llm_start = time.time()
        response: ChatResponse = chat(model='qwen2.5:7b', messages=lean, tools=tools,
                                      keep_alive=OLLAMA_KEEP_ALIVE)

        # Some groups have an obvious default action. For a vague request like
        # "how hot is it outside", qwen2.5:7b narrates ("I'll check the weather
        # at home...") instead of calling the tool, even at temperature 0.
        # Run the default instead.
        default = (DEFAULT_CALLS[group](transcribed_text)
                   if group in DEFAULT_CALLS and looks_like_request(transcribed_text) else None)
        if not response.message.tool_calls and default:
            name, args = default
            print(f"No tool call from group '{group}' — running default {name}({args})")
            response.message.content = ""
            response.message.tool_calls = [
                Message.ToolCall(function=Message.ToolCall.Function(name=name, arguments=args))
            ]

        # The words picked the wrong tools, so the right one was never offered.
        # Retry once with the whole agent rather than answering wrongly — the
        # longer prompt is paid on the rare miss instead of on every request.
        whole_group = TOOL_GROUPS.get(group, [])
        if not response.message.tool_calls and len(whole_group) > len(tools):
            print(f"No tool call from {[t.__name__ for t in tools]} — retrying with all {len(whole_group)} {group} tools")
            tools = whole_group
            response = chat(model='qwen2.5:7b', messages=lean, tools=tools,
                            keep_alive=OLLAMA_KEEP_ALIVE)
        print(f"Tool selection took {time.time() - llm_start:.2f}s")

        if response.message.tool_calls: #Loops through all required tool calls to finish task
            lean.append(response.message)
            messages.append({"role": "assistant", "content": response.message.content or "",
                             "tool_calls": response.message.tool_calls})
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
                    lean.append({"role": "tool", "tool_name": tool_call.function.name, "content": str(result)})
                    messages.append({"role": "tool", "tool_name": tool_call.function.name,
                                     "content": str(result)[:TOOL_RESULT_HISTORY_CHARS]})

            lean.append({
                "role": "user",
                "content": f'You have already said "{ack}" to the user. Now tell them what the tool results '
                           "mean, naturally and without repeating that, in two sentences at most. "
                           "Do not call any more tools."
            }) #Summarizes what was just done

            # Same messages and tools as the call above, so Ollama reuses the
            # prompt it just read and only the tool results are new.
            follow_up = chat(model='qwen2.5:7b', messages=lean, tools=tools,
                             stream=True, keep_alive=OLLAMA_KEEP_ALIVE,
                             options=GEN_OPTIONS)
            spoken = speak_stream(follow_up)
            if not spoken:
                # qwen occasionally answers the summary request with another
                # tool call and no words, and Jarvis said nothing at all. Ask
                # again with no tools on offer, so words are the only option.
                print("Empty summary — asking again without tools")
                retry = chat(model='qwen2.5:7b', messages=lean, stream=True,
                             keep_alive=OLLAMA_KEEP_ALIVE, options=GEN_OPTIONS)
                spoken = speak_stream(retry)
        else:
            spoken = response.message.content or response.message.thinking or ""
            safe_speak(spoken)
        messages.append({"role": "assistant", "content": spoken})

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


# What Whisper produces from silence or room noise rather than speech
WHISPER_PHANTOMS = {"you", "thank you", "thanks for watching", "thank you for watching",
                    "so", "uh", "um", "hmm"}


def is_speech(text):
    """
    False for transcripts of noise: empty, almost no letters (Whisper turned
    fan noise into 'ʕᴗᴗᴗ ʕᴗᴗᴗ...'), or one of its stock phrases for silence.
    """
    letters = re.findall(r"[A-Za-z]", text or "")
    if len(letters) < 3 or len(letters) < 0.5 * len(text.replace(" ", "")):
        return False
    return re.sub(r"[^a-z ]", "", text.lower()).strip() not in WHISPER_PHANTOMS


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
    messages[0]["content"] = build_system_prompt()
    noise_in_a_row = 0
    while True:
        # Never start recording while Jarvis is still talking, or the mic picks
        # him up. Playback is asynchronous, so this has to be explicit.
        wait_until_spoken()
        # The first command after the wake word gets the normal bar; anything
        # after that is a follow-up and must be clearly louder than the room.
        samples = pending if pending is not None else mic.record_command(follow_up=bool(transcript))
        pending = None
        if samples is None:
            print("No speech — ending conversation")
            break
        text = "" if samples is mic.NOISE else transcribe(samples)
        if not is_speech(text):
            # Background noise that got past the recorder. Answering it is how
            # a conversation used to talk to itself forever; twice running
            # means nobody is there.
            noise_in_a_row += 1
            print(f"Ignoring non-speech transcript: {text[:60]!r}")
            if noise_in_a_row >= 2:
                print("Only noise — ending conversation")
                break
            continue
        noise_in_a_row = 0
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
    try:
        embed(model=memory_store.EMBED_MODEL, input="", keep_alive=0)
    except Exception as e:
        print(f"Could not unload {memory_store.EMBED_MODEL}: {e}")


# Guarded so tests.py can import this module without launching the assistant.
# Running this file directly holds a single conversation with no wake word —
# handy for debugging and for completing the Google and Spotify logins the
# first time. The background assistant is wake_listener.py.
if __name__ == "__main__":
    startup()
    mic.calibrate()     # no wake listener to report the room's noise level
    run_session()
    shutdown()
