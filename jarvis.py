import os
from pathlib import Path

# Kokoro, Parakeet and Whisper each ask Hugging Face for newer files every
# time they load, even when the models are already on disk. When the network
# is slow that check hung startup for minutes (measured: 325 s for a test run
# that takes 7). Once all three are downloaded, load them from disk only.
# Set HF_HUB_OFFLINE=0 to let them check for updates again.
_HF_CACHE = Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
_SPEECH_MODELS = ("hexgrad--Kokoro-82M", "mlx-community--parakeet-tdt-0.6b-v2",
                  "mlx-community--whisper-small-mlx")
if all((_HF_CACHE / f"models--{name}").is_dir() for name in _SPEECH_MODELS):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import os
import speech_recognition as sr
import mlx_whisper
from ollama import chat as _ollama_chat, embed, generate, ChatResponse, Message
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
# The model behind intent, tools, chat and memory. JARVIS_MODEL swaps it for
# comparison runs (tests.py, the model benchmark) without code changes.
MAIN_MODEL = os.getenv("JARVIS_MODEL", "qwen2.5:7b")
OLLAMA_MODELS = (MAIN_MODEL, "llama3.2:1b")

# Models that reason step by step before answering unless told not to. For a
# voice assistant that's seconds of silence per call, so it's switched off.
THINKING_FAMILIES = ("qwen3", "deepseek-r1", "gpt-oss")


def chat(**kwargs):
    """ollama.chat, with thinking off for models that would otherwise think first."""
    if str(kwargs.get("model", "")).startswith(THINKING_FAMILIES):
        kwargs.setdefault("think", False)
    return _ollama_chat(**kwargs)

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
                 "what's on", "whats on", "book me", "standup", "stand-up", "one-on-one",
                 "1:1", "call with", "lunch with", "dinner with", "anything on", "do i have anything"),
    "music": ("play ", "spotify", "song", "track", "album", "artist", "playlist",
              "skip", "pause the", "volume", "shuffle", "what's playing", "whats playing",
              "turn it up", "turn it down", "louder", "quieter"),
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
                   "previous", "complete", "update", "change", "clear", "reschedule", "rename",
                   "push", "block", "save", "reply", "forward", "open", "note", "text", "call"}
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


def route_groups(text):
    """Every group whose keywords the command contains, in GROUP_KEYWORDS order."""
    lowered = text.lower()
    return [group for group, words in GROUP_KEYWORDS.items() if any(word in lowered for word in words)]


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
    tomorrow = now + timedelta(days=1)
    return (f"Today is {now.strftime('%A %Y-%m-%d')} at {now.strftime('%H:%M')}; "
            f"tomorrow is {tomorrow.strftime('%A %Y-%m-%d')}. "
            f"Dates coming up: {upcoming}. "
            f"Use these exact dates for any day the user names.")


def build_system_prompt():
    """
    The system prompt, rebuilt at the start of every conversation so today's
    date and the user's preferences are current even when the engine has been
    warm for hours. Stable within a conversation, which keeps Ollama's prefix
    cache intact.
    """
    return f"""
    You are JARVIS (Just A Rather Very Intelligent System), an advanced AI assistant built to serve as a highly capable, loyal, and intelligent personal assistant.

    ## Context
    {date_reference()} Use these for any scheduling, calendar, or time-related tasks.

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


def _same_words(a, b):
    return re.sub(r"[^a-z0-9 ]", "", a.lower()).split() == re.sub(r"[^a-z0-9 ]", "", b.lower()).split()


def speak_stream(stream_response, tool_calls=None, already_said=()):
    """
    Consume a streaming Ollama response and hand each finished sentence to the
    speaker as soon as it appears.

    Jarvis starts talking after the first sentence rather than after the last
    token, which is most of the perceived latency on a long answer.

    Pass a list as `tool_calls` to collect any tool calls the response makes;
    the agent loop streams every step, since any one of them may be the answer.
    A sentence identical to one in `already_said` isn't spoken again: qwen
    sometimes opens its answer by repeating the acknowledgement.

    Returns what was spoken, so it can be appended to the message history.
    """
    buffer = ""
    spoken = []

    def _speak(sentence):
        sentence = sentence.strip()
        if not sentence:
            return
        if any(_same_words(sentence, earlier) for earlier in already_said):
            print(f"Not repeating: {sentence}")
            return
        say(sentence)
        spoken.append(sentence)

    for part in stream_response:
        if tool_calls is not None and part.message.tool_calls:
            tool_calls.extend(part.message.tool_calls)
        piece = part.message.content or ""
        if not piece:
            continue
        buffer += piece
        # Only flush on a sentence boundary — Kokoro's prosody falls apart if
        # it is fed half a clause at a time.
        while True:
            match = SENTENCE_END.search(buffer)
            if not match or match.end() == 0:
                break
            sentence, buffer = buffer[:match.end()], buffer[match.end():]
            _speak(sentence)
    _speak(buffer)
    if not spoken and not tool_calls:
        print("Warning: empty response, nothing to speak")
    return " ".join(spoken)

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
# Parakeet TDT 0.6B on MLX: measured on eight spoken commands, 0.15s each
# against Whisper small's 0.37s, about the same memory (~650 MB), and fewer
# errors (it heard "Priya" where Whisper heard "PREA"). Punctuates just as
# well, which looks_like_request() relies on. JARVIS_STT=whisper switches back.
PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
STT_ENGINE = os.getenv("JARVIS_STT", "parakeet")
_parakeet = None


def _parakeet_text(samples):
    global _parakeet
    import mlx.core as mx
    from parakeet_mlx.audio import get_logmel
    if _parakeet is None:
        from parakeet_mlx import from_pretrained
        _parakeet = from_pretrained(PARAKEET_MODEL)
    # float32: parakeet-mlx 0.5.2's default bfloat16 path fails in get_logmel
    mel = get_logmel(mx.array(samples, dtype=mx.float32), _parakeet.preprocessor_config)
    return _parakeet.generate(mel)[0].text


def transcribe(samples):
    """
    Speech to text: Parakeet, or MLX Whisper if Parakeet is switched off or
    fails to load. Takes 16 kHz float32 samples from mic.py.
    """
    global STT_ENGINE
    text = None
    if STT_ENGINE == "parakeet":
        try:
            text = _parakeet_text(samples)
        except Exception as e:
            print(f"Parakeet failed ({e}) — using Whisper from now on")
            STT_ENGINE = "whisper"
    if text is None:
        text = mlx_whisper.transcribe(samples, path_or_hf_repo=WHISPER_MODEL)["text"]
    text = text.strip()
    print("User: ", text)
    return text


def record_audio_and_transcribe_mlx_whisper():
    """
    Current transcription method for user - free. Runs efficiently on Mac Silicone chip.
    Returns "" if nobody spoke before the listen timeout.
    """
    samples = mic.record_command()
    return transcribe(samples) if samples is not None and samples is not mic.NOISE else ""


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
        model=MAIN_MODEL,
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


# Goodbyes, recognised by their words alone. "That's it" only with "for now"
# or "thanks": "that's it, I finally fixed the bug" isn't a goodbye.
GOODBYE = re.compile(r"\b(bye|goodbye|good night|goodnight|that's all|that'll be all|that is all|"
                     r"that'll do|that's it for (now|today)|that's it,? thanks|that's everything|"
                     r"i'm done|we're done|i'm all set|nothing else|talk (to you )?later|see you|"
                     r"catch you later|go (back )?to sleep|stop listening)\b")

# Labelled phrases the intent classifier compares a command against. Written
# separately from tests.py's INTENT_CASES, so the score there is on phrases
# the classifier has never seen. Add a phrase here when a kind of command is
# misread; `python tests.py intent` checks the result.
INTENT_EXAMPLES = {
    "exit": [
        "Okay that's everything, thanks.", "Bye Jarvis.", "Alright, I'm good for now.", "See you later.",
        "That's all I needed.", "Thanks, goodnight.", "We're done here.", "Stop listening.",
        "Nothing else, thank you.", "Catch you later.", "No, that's it.", "Go back to sleep.",
        "I'm all set, thanks.", "Talk to you tomorrow.",
    ],
    "tool": [
        "What's the forecast for the weekend?", "Is it going to snow tonight?", "Do I have any meetings this afternoon?",
        "Check my inbox.", "Set a reminder to water the plants.", "Put on some lo-fi music.", "Next track.",
        "Turn the volume up.", "Look up the opening hours for the library.", "Schedule lunch with Alex on Monday.",
        "Reply to Mark saying sounds good.", "What's on my to-do list?", "Find the email from my landlord.",
        "Cancel my 4pm meeting.", "What time is it in Tokyo?", "How long until my birthday?",
        "Read my latest email.", "What song is this?", "Find Mike's address in my contacts.",
        "Check my notes for the gate code.", "From now on use metric units.", "What day is the 14th?",
    ],
    "chat": [
        "How are you doing today?", "What do you think about remote work?", "Write me a short story about a dragon.",
        "Why is the sky blue?", "I had a great workout this morning.", "That's funny.", "Who was the first person on the moon?",
        "Give me some tips for focusing.", "I'm bored.", "What's the difference between a virus and bacteria?",
        "My brother just got a new job.", "Can you explain quantum computing simply?", "Help me plan a birthday speech.",
        "I played guitar for an hour yesterday.", "What's your favourite movie?", "That makes sense, thanks.",
        "Translate good morning into Spanish.", "What should I cook for dinner?", "I don't feel like working today.",
    ],
}
INTENT_NEIGHBOURS = 3      # a label's score is its mean similarity over this many closest examples
_intent_bank = None        # (labels, unit vectors) for INTENT_EXAMPLES, built on first use


def _intent_vectors(texts):
    # nomic-embed-text's prefix for classification tasks
    response = embed(model=memory_store.EMBED_MODEL, input=["classification: " + t for t in texts],
                     keep_alive=OLLAMA_KEEP_ALIVE)
    vectors = np.array(response["embeddings"], dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def classify_intent(text):
    """
    Returns 'exit', 'tool', or 'chat', in about 10 ms and without the LLM.

    The command is compared with labelled example phrases (INTENT_EXAMPLES)
    by embedding, under two rules: only GOODBYE ends a conversation, and
    'tool' needs words that point at a service (GROUP_KEYWORDS).
    decide_intent() then guards the result.

    The exit examples still count, as competition for the other labels, but
    an exit reading becomes the runner-up: "That's it, I finally fixed the
    bug" sits right next to "No, that's it." Ending a conversation by mistake
    costs more than missing a goodbye, which ends on 10 s of silence anyway.

    This replaced a qwen2.5:7b few-shot call. On tests.py's 35 labelled
    phrases both score 34/35, but qwen took ~220 ms and, worse, pushed the
    conversation out of Ollama's single prompt cache, so the reply that
    followed re-read everything: routing measured 0.3–7 s. If the embedding
    model is unavailable, the rules alone decide.
    """
    global _intent_bank
    request, groups = looks_like_request(text), route_groups(text)
    if GOODBYE.search(text.lower()) and not request:
        return "exit"
    try:
        if _intent_bank is None:
            labels = [label for label, phrases in INTENT_EXAMPLES.items() for _ in phrases]
            _intent_bank = (np.array(labels), _intent_vectors([p for ps in INTENT_EXAMPLES.values() for p in ps]))
        labels, bank = _intent_bank
        similarity = bank @ _intent_vectors([text])[0]
        scores = {label: float(np.sort(similarity[labels == label])[-INTENT_NEIGHBOURS:].mean())
                  for label in INTENT_EXAMPLES}
    except Exception as e:
        print(f"Intent embedding failed ({e}) — deciding by rules")
        return "tool" if request and groups else "chat"
    ranked = sorted(scores, key=scores.get, reverse=True)
    best = ranked[0]
    if best == "exit":
        best = ranked[1]
    if best == "tool" and not groups:
        best = "chat"
    return best


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
    Load the Ollama models, the speech model and the intent examples during
    startup instead of on the user's first command, which otherwise costs
    4.4s + 2.3s + the speech model load.
    """
    try:
        for model in OLLAMA_MODELS:
            chat(model=model, keep_alive=OLLAMA_KEEP_ALIVE,
                 options={"num_predict": 1},
                 messages=[{"role": "user", "content": "hi"}])
        transcribe(np.zeros(mic.WHISPER_RATE, dtype=np.float32))   # loads the speech model
        if memory is not None:
            memory.search("warm up")    # loads the embedding model
    except Exception as e:
        print(f"Prewarm skipped: {e}")
    try:
        classify_intent("warm up")      # embeds the intent examples
        print("Models prewarmed")
    except Exception as e:
        print(f"Prewarm skipped: {e}")


# Tools offered across all the agents one request touches
MAX_TOOLS_OFFERED = 8


def select_tools(text):
    """
    (group, tools) for a tool request: the agent from keyword rules or the 1B
    classifier, then only the tools within it that the request's words call
    for. Everything as a last resort. A group whose agent is off comes back
    with no tools.

    A request that names more than one agent's work ("read this week's emails
    and block time on my calendar") also gets the picked tools of the others,
    so the agent loop can chain across them. The first group leads: it gives
    the acknowledgement, and a "not set up" reply if it's off.
    """
    groups = route_groups(text)
    if not groups:
        guess = classify_tool_group(text)
        groups = [guess] if guess else []
    if not groups:
        return "ALL", ALL_TOOLS
    group = groups[0]
    if group not in TOOL_GROUPS:
        return group, []
    tools = []
    for g in groups:
        if g not in TOOL_GROUPS:
            continue
        names = tool_catalog.pick(CLASS_BY_GROUP[g], text)
        for method in [TOOL_REGISTRY[n] for n in names if n in TOOL_REGISTRY] or TOOL_GROUPS[g]:
            if method not in tools:
                tools.append(method)
    return group, tools[:MAX_TOOLS_OFFERED]


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
Use the tools to do what the user asks. A request can take several tool calls in a row: use what one
returns to make the next, such as finding an event before moving it, or reading emails before
scheduling time for them. Never guess an id, date or address a tool could tell you.
Never claim something was done that a tool didn't do or report.
Some messages begin with a bracketed note of what you know about the user; use it naturally."""

# Rounds of tool calls one request may take before Jarvis must answer
MAX_TOOL_STEPS = 4

# Earlier turns given to the tool call, for follow-ups like "reply to that one"
TOOL_CONTEXT_TURNS = 4
# Tool output kept in the conversation for later turns; the tool call itself sees all of it.
# Every character is read again by the next chat turn: at 1,200 a chat after
# the weather took 3.8 s to its first word, mostly re-reading the forecast.
TOOL_RESULT_HISTORY_CHARS = 400



def tool_messages(user_content, transcript):
    """
    The short conversation a tool call runs on: the brief persona, the last
    few turns in plain words, and the request with today's dates.
    """
    recent = [{"role": "user" if who == "User" else "assistant", "content": text}
              for who, text in transcript[-TOOL_CONTEXT_TURNS - 1:-1]]
    return ([{"role": "system", "content": TOOL_PROMPT + preferences.prompt_block()}] + recent
            + [{"role": "user", "content": f"[{date_reference()}] {user_content}"}])


# Loop steps may carry a whole email body in their arguments, so they get more
# room than a plain reply; the "two sentences" instruction keeps answers short.
# Temperature 0: at the default, qwen made the change a find-then-change
# request asked for in about half of runs, and otherwise described it as done.
LOOP_OPTIONS = {"num_predict": 320, "temperature": 0}
TOOL_CALL_OPTIONS = {"temperature": 0}


# --- Confirming deletes and sends -------------------------------------------

# How long to wait for a yes or no before leaving the action undone
CONFIRM_TIMEOUT = 8
# Checked first, so "no, don't cancel it" is a no despite the "cancel it"
SAYS_NO = re.compile(r"\b(no|nope|nah|don't|do not|stop|wait|hold on|never ?mind|leave it|not yet|"
                     r"not now|forget it)\b")
SAYS_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|do it|go ahead|go for it|confirm(ed)?|please|ok|okay|"
                      r"sounds good|correct|absolutely|definitely|send it|delete it|cancel it|bin it|"
                      r"that's right|affirmative)\b")

# Fallbacks for when the model won't phrase the question
CONFIRM_FALLBACKS = {
    "delete_calendar_event": "Shall I delete that event?",
    "trash_email": "Shall I bin that email?",
    "delete_reminder": "Shall I delete that reminder?",
    "send_email": "Shall I send it{to}?",
    "reply_to_email": "Shall I send that reply?",
    "send_draft": "Shall I send that draft?",
}


def is_yes(reply):
    """True only for a clear yes. Silence, a no, or anything unclear leaves the action undone."""
    lowered = reply.lower()
    return bool(lowered) and not SAYS_NO.search(lowered) and bool(SAYS_YES.search(lowered))


def listen_for_reply():
    """
    The user's answer to a question asked mid-task, as text — "" for silence
    or noise. Waits for Jarvis to finish asking first, so he never hears
    himself, and chimes like any other turn.
    """
    wait_until_spoken()
    chime()
    wait_until_spoken()
    samples = mic.record_command(timeout=CONFIRM_TIMEOUT, follow_up=True)
    if samples is None or samples is mic.NOISE:
        return ""
    text = transcribe(samples)
    return text if is_speech(text) else ""


def confirmation_question(calls, lean, tools):
    """
    One short spoken question naming what's about to happen: "Cancel Team
    standup tomorrow at 3pm — shall I?". The model writes it, since it has the
    event or email the call's id refers to; the same messages and tools as the
    loop keep Ollama's cache, so only the request for a question is new.
    """
    planned = "; ".join(f"{c.function.name}({json.dumps(dict(c.function.arguments or {}), default=str)})"
                        for c in calls)
    try:
        r = chat(model=MAIN_MODEL, tools=tools, keep_alive=OLLAMA_KEEP_ALIVE,
                 options={"temperature": 0, "num_predict": 60},
                 messages=lean + [{"role": "user", "content":
                     f"[About to run: {planned}. Before it runs, ask the user to confirm in one short spoken "
                     f"question that names what will happen, such as the event and its time or who the email "
                     f"goes to. Don't use ids. Don't call any tools.]"}])
        question = (r.message.content or "").strip()
        if question and not r.message.tool_calls and "?" in question and len(question) < 200:
            return question
    except Exception as e:
        print(f"Confirmation question failed: {e}")
    first = calls[0].function
    to = (first.arguments or {}).get("to", "")
    return CONFIRM_FALLBACKS.get(first.name, "Shall I go ahead?").format(to=f" to {to}" if to else "")


def confirm_calls(calls, lean, tools):
    """Ask before deletes and sends. Returns (approved, what the user said)."""
    question = confirmation_question(calls, lean, tools)
    say(question)
    reply = listen_for_reply()
    approved = is_yes(reply)
    print(f"Confirmation: {question!r} -> {reply!r} ({'approved' if approved else 'not approved'})")
    return approved, reply


def run_tool_calls(calls, lean, seen, tools):
    """
    Run one round of tool calls, adding each result to the tool conversation
    and the main history. Returns how many were new: a call identical to one
    already made this turn isn't run again, since its result is already there.

    Deletes and sends (tool_catalog.CONFIRM) are asked about first, once for
    the whole round. Declined, they're reported to the model as not done, and
    a retry of the same call is refused.
    """
    lean.append({"role": "assistant", "content": "", "tool_calls": calls})
    messages.append({"role": "assistant", "content": "", "tool_calls": calls})

    def key_of(call):
        return (call.function.name, json.dumps(dict(call.function.arguments or {}), sort_keys=True, default=str))

    risky = [c for c in calls if c.function.name in tool_catalog.CONFIRM and key_of(c) not in seen]
    approved, reply = confirm_calls(risky, lean, tools) if risky else (True, "")

    new = 0
    for call in calls:
        name, args = call.function.name, dict(call.function.arguments or {})
        key = key_of(call)
        if seen.get(key) == "declined":
            result = "The user already said not to do this. Don't try it again."
        elif key in seen:
            result = "Already called with these arguments this turn; its result is above."
        elif name not in TOOL_REGISTRY:
            result = f"There is no tool called {name}."
        elif call in risky and not approved:
            seen[key] = "declined"
            new += 1
            heard = f'said "{reply}"' if reply else "didn't answer"
            result = (f"Not done: asked to confirm, the user {heard}. Nothing was changed. "
                      f"Don't try it again; tell them it's been left as it was.")
            print(f"Skipped {name} — not confirmed")
        else:
            seen[key] = "done"
            new += 1
            tool = TOOL_REGISTRY[name]
            # qwen now and then adds an argument the tool doesn't take (one
            # run invented "toolbench_rapidapi_key"), which would fail the call
            accepted = inspect.signature(tool).parameters
            dropped = [k for k in args if k not in accepted]
            args = {k: v for k, v in args.items() if k in accepted}
            print(f"Calling {name} with {args}" + (f" (dropped {dropped})" if dropped else ""))
            try:
                result = tool(**args)
            except Exception as e:
                # Hand the failure back to the model as text so it can explain
                # itself, or try another way, instead of crashing the session.
                result = f"That tool failed: {e}"
            print(f"Tool result: {result}")
        lean.append({"role": "tool", "tool_name": name, "content": str(result)})
        messages.append({"role": "tool", "tool_name": name, "content": str(result)[:TOOL_RESULT_HISTORY_CHARS]})
    return new


# Questions that ask for information, not a change: "what did I put on my
# calendar", "did anyone add anything". "Can you move..." is left out on purpose.
INFO_QUESTION = re.compile(r"^((hey|ok|okay|so|and|jarvis)[,.!]?\s+)*(what|what's|whats|when|where|which|who|"
                           r"did|is|are|was|were|do|does|how)\b")

# An answer that says it's about to act ("Let me go ahead and cancel it")
ABOUT_TO_ACT = re.compile(r"\b(let me|i'll|i will|i'm going to|going to|go ahead)\b")


def unmade_changes(tools, called, request):
    """
    The change tools on offer, if none of them has run yet this turn — the
    request asked for a change that hasn't happened. Empty once one has, and
    for information questions, whose "put" or "add" offered create_event
    without asking for anything to be created.
    """
    if INFO_QUESTION.match(request.strip().lower()):
        return []
    offered = [t.__name__ for t in tools if t.__name__ in tool_catalog.CHANGES]
    return [] if called & tool_catalog.CHANGES else offered


def push_for_change(lean, tools, text, pending):
    """
    A non-streamed nudge when the model answered while a change is still
    unmade. If the request really didn't ask for one, the model answers again
    and that answer stands. An answer that only says it's about to act ("Let
    me go ahead and cancel it") gets one more nudge. Returns (tool_calls, text).
    """
    for attempt in range(2):
        print(f"Answer held back — no change made yet ({', '.join(pending)} on offer): {text[:80]!r}")
        if text:
            lean.append({"role": "assistant", "content": text})
        lean.append({"role": "user", "content":
                     f"[No {' or '.join(pending)} call has been made, so nothing has changed yet and saying "
                     f"it's done would be untrue. If the request asks for that change, call the tool now "
                     f"rather than describing it. If it doesn't, just answer what was asked, in two sentences "
                     f"at most, without mentioning changes.]"})
        r = chat(model=MAIN_MODEL, messages=lean, tools=tools, keep_alive=OLLAMA_KEEP_ALIVE,
                 options=LOOP_OPTIONS)
        calls, text = list(r.message.tool_calls or []), (r.message.content or "").strip()
        if calls or not ABOUT_TO_ACT.search(text.lower()):
            break
    return calls, text


def run_tool_loop(lean, tools, calls, ack, request):
    """
    The agent loop: run the tool calls, show the model the results, and let it
    either call the next tool or answer — up to MAX_TOOL_STEPS rounds. This is
    what lets one request chain steps: find an event, then move it; read the
    week's emails, then block time on the calendar to answer them.

    Once any change the request asked for is made (or none was asked for),
    each step is streamed, because any one of them may be the answer and the
    answer should start playing at its first sentence. Until then a step is
    read in full first, so a claim that something was done when it wasn't is
    caught before it's spoken (push_for_change).

    Every call uses the same messages, appended to, and the same tools, so
    Ollama only reads what's new each round. Returns everything Jarvis said
    after the acknowledgement.
    """
    said = []
    seen = {}      # call -> 'done' or 'declined'
    called = set()
    for step in range(1, MAX_TOOL_STEPS + 1):
        step_start = time.time()
        called.update(c.function.name for c in calls)
        if not run_tool_calls(calls, lean, seen, tools):
            print("Only repeated tool calls — answering with what's there")
            break
        # Wording measured on find-then-change requests at temperature 0:
        #   "call the next tool, otherwise answer"             0 of 3 changes made
        #   restate the request, change first, then answer    3 of 3
        #   ... plus "go straight to the result"               0 of 3
        # Any hint to hurry to the answer makes qwen describe the change as
        # done instead of making it. speak_stream(already_said=) stops it
        # repeating the acknowledgement instead.
        lean.append({"role": "user", "content":
                     f'[Tool results are in. The request was: "{request}". If the tools so far have only looked '
                     f"something up and the request asks for a change, make that change now with the matching "
                     f"tool. Only when every part is done, tell the user the result in two sentences at most.]"})
        pending = unmade_changes(tools, called, request)
        if pending:
            r = chat(model=MAIN_MODEL, messages=lean, tools=tools,
                     keep_alive=OLLAMA_KEEP_ALIVE, options=LOOP_OPTIONS)
            calls, text = list(r.message.tool_calls or []), (r.message.content or "").strip()
            if not calls:
                calls, text = push_for_change(lean, tools, text, pending)
            if not calls and text:
                said.append(text)
                safe_speak(text)
                print(f"Step {step} took {time.time() - step_start:.2f}s — answered, no change made")
                return " ".join(said)
        else:
            calls = []
            text = speak_stream(chat(model=MAIN_MODEL, messages=lean, tools=tools, stream=True,
                                     keep_alive=OLLAMA_KEEP_ALIVE, options=LOOP_OPTIONS),
                                tool_calls=calls, already_said=[ack] + said)
            if text:
                said.append(text)
        print(f"Step {step} took {time.time() - step_start:.2f}s"
              + (f" — next: {[c.function.name for c in calls]}" if calls else " — answered"))
        if not calls:
            if text:
                return " ".join(said)
            # qwen occasionally answers with neither words nor a call, and
            # Jarvis said nothing at all. Ask again below, with no tools on
            # offer, so words are the only option.
            print("Empty answer — asking again without tools")
            break
    else:
        print(f"Reached {MAX_TOOL_STEPS} tool steps — answering with what's there")
    lean.append({"role": "user", "content":
                 "[No more tools. Tell the user what was done and what wasn't, in two sentences at most.]"})
    final = speak_stream(chat(model=MAIN_MODEL, messages=lean, stream=True,
                              keep_alive=OLLAMA_KEEP_ALIVE, options=GEN_OPTIONS),
                         already_said=[ack] + said)
    return " ".join(said + [final]).strip()


def handle_turn(transcribed_text, transcript):
    """
    Answer one command. Returns True when the user has ended the conversation.

    `transcript` collects the raw (speaker, text) turns for memory extraction.
    """
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
        completion = chat(model=MAIN_MODEL, messages=messages, stream=True,
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
        response: ChatResponse = chat(model=MAIN_MODEL, messages=lean, tools=tools,
                                      keep_alive=OLLAMA_KEEP_ALIVE, options=TOOL_CALL_OPTIONS)

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
            response = chat(model=MAIN_MODEL, messages=lean, tools=tools,
                            keep_alive=OLLAMA_KEEP_ALIVE, options=TOOL_CALL_OPTIONS)
        print(f"Tool selection took {time.time() - llm_start:.2f}s")

        calls = list(response.message.tool_calls or [])
        pending = unmade_changes(tools, set(), transcribed_text) if group != "ALL" else []
        if not calls and pending:
            # "Reschedule the standup to 4pm" answered with "I've moved it"
            # and no call at all
            calls, text = push_for_change(lean, tools, response.message.content or "", pending)
            response.message.content = text
        if not calls:
            spoken = response.message.content or response.message.thinking or ""
            safe_speak(spoken)
        else:
            spoken = run_tool_loop(lean, tools, calls, ack, transcribed_text)
        messages.append({"role": "assistant", "content": spoken})

    else:  # chat
        # The dates are in the system prompt (date_reference(), rebuilt each
        # conversation), not on a copy of this message. The copy differed from
        # what history stored, so the next turn missed Ollama's cache from the
        # previous message on: a chat after a tool turn re-read 792 of 1,424
        # tokens, about 4 s.
        response = chat(model=MAIN_MODEL, messages=messages, stream=True,
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


def warm_conversation():
    """
    Have Ollama read the new system prompt while the user is still giving
    their first command, so the first reply only reads that command. Without
    it the first turn took 3.7 s to its first word, against ~0.6 s for the
    turns after it. Runs in the background; a failure only costs that time.
    """
    def _warm():
        try:
            chat(model=MAIN_MODEL, messages=[messages[0]], keep_alive=OLLAMA_KEEP_ALIVE,
                 options={"num_predict": 1})
        except Exception as e:
            print(f"Conversation warm-up skipped: {e}")
    threading.Thread(target=_warm, daemon=True).start()


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
    warm_conversation()
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
