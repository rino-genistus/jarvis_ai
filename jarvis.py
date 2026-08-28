from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import os
import speech_recognition as sr
import mlx_whisper
from ollama import chat, ChatResponse
import time
import tempfile
from agents import Calendar_Agents, WebSearchAgents, WeatherSearch, SpotifyAgent, GmailAgent, RemindersAgent
from datetime import datetime, timedelta
from kokoro import KPipeline
import sounddevice as sd
import numpy as np
from pinecone import Pinecone
import threading
import inspect
import queue
import re

start_time = time.time()

# Keep Ollama models resident. The default 5 minute keep_alive means an idle
# assistant pays a 4.4s reload for qwen and 2.3s for llama on the next command.
OLLAMA_KEEP_ALIVE = -1

# Cap spoken replies. Generation and playback both scale with length, and a
# 73 token answer is already 7.6 seconds of speech.
GEN_OPTIONS = {"num_predict": 160}

load_dotenv()

current_date = datetime.now().strftime("%A, %B %d, %Y")
print(current_date)

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

dense_index = pc.Index(index_name)

eleven_labs = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

kokoro_pipeline = None
kokoro_ready = threading.Event()

def load_kokoro():
    global kokoro_pipeline
    kokoro_pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
    kokoro_ready.set()
    print(f"Kokoro Loaded")

threading.Thread(target=load_kokoro, daemon=True).start()

calendar = Calendar_Agents()
websearch = WebSearchAgents()
weather = WeatherSearch()
spotify = SpotifyAgent()
gmail = GmailAgent()
reminders = RemindersAgent()

AGENTS = [calendar, websearch, weather, spotify, gmail, reminders]

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
    to mean 'no confident route, send everything'.
    """
    lowered = text.lower()
    for group, words in GROUP_KEYWORDS.items():
        if any(word in lowered for word in words):
            return group, TOOL_GROUPS[group]
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
    You only know what has been said in this conversation. You have no memory between sessions and no access to anything outside of the current conversation. Your slate is blank until the user tells you something — if asked about prior context, say so plainly.

    ## Core Principles
    - Your user's goals are your goals. You advocate for their success.
    - You are proactive — if you notice something relevant, you mention it without being asked.
    - You do not moralize, lecture, or add unsolicited caveats. You trust your user's judgment.
    - You are never sycophantic. Honest, direct assessment beats flattery every time.
    - When something is outside your ability, say so immediately and suggest alternatives.
    - You have no memory between sessions. Never invent prior context, projects, people, or history that hasn't been explicitly stated in this conversation.
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
_SPEAK_Q = queue.Queue()  # everything Jarvis says goes through here, in order


def _chime_samples():
    """
    Small tone so the user knows when Jarvis has finished talking.
    """
    sample_rate = 24000
    duration = 0.15
    freq = 880
    t = np.linspace(0, duration, int(sample_rate * duration))
    return (np.sin(2 * np.pi * freq * t) * 0.3).astype(np.float32)


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

def record_audio_and_transcribe_mlx_whisper():
    """
    Current transcription method for user - free. Runs efficiently on Mac Silicone chip
    """
    with sr.Microphone() as source:
        r.energy_threshold = 200
        r.pause_threshold = 0.8  # was 1.5 — that much dead air is felt directly as latency
        r.phrase_threshold = 0.1
        r.non_speaking_duration = 0.8
        print("User Talks Now")
        audio_text = r.listen(source, timeout=10, phrase_time_limit=45)
        wav_audio_data = audio_text.get_wav_data()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_audio_data)
            temp_path = f.name

        result = mlx_whisper.transcribe(
            temp_path,
            path_or_hf_repo="mlx-community/whisper-small-mlx",
        )

        os.remove(temp_path)
        print("User: ", result['text'].strip())
        return result["text"].strip()

def extract_important_messages(messages):
    """
    Given the messages from the current chat, uses qwen2.5:7b model to review all messages and retreive list of meaningful messages
    """
    response = chat(
        model='qwen2.5:7b',
        keep_alive=OLLAMA_KEEP_ALIVE,
        messages=[
            {
                "role": "user",
                "content": f"""Review this conversation and extract only information worth remembering long-term about the user — preferences, facts, habits, goals, or anything personally relevant.
                Ignore greetings, small talk, and one-off questions like weather lookups.
                Return a list of concise factual statements, one per line. If nothing is worth remembering, return 'NONE'.

                Conversation:
                {messages}"""
            }
        ]
    )
    results = response.message.content.strip()
    if results == "NONE":
        return []
    memories = [
        line.strip().lstrip("0123456789.-) ")
        for line in results.split('\n')
        if line.strip()
    ]

    records = [
        {"id": f"mem-{int(time.time())}-{i}", "chunk_text": memory}
        for i, memory in enumerate(memories)
        if memory
    ]

    print(f"Storing {len(records)} memories: {[r['chunk_text'] for r in records]}")
    return records

def retrieve_memories(query: str, top_k: int = 5):
    """
    Retrieves most meaningful messages from Pinecone Vector DB for conversation context
    """
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
        for model in ("qwen2.5:7b", "llama3.2:1b"):
            chat(model=model, keep_alive=OLLAMA_KEEP_ALIVE,
                 options={"num_predict": 1},
                 messages=[{"role": "user", "content": "hi"}])
        import soundfile as sf
        silence = np.zeros(16000, dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        sf.write(path, silence, 16000)
        mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-small-mlx")
        os.remove(path)
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


def main_loop():
    """
    Main Program Loop
    """
    with sr.Microphone() as source:
        print("Calibrating microphone...")
        r.adjust_for_ambient_noise(source, duration=0.3)
        r.dynamic_energy_threshold = False
    # Dispatch table and tool schemas both come from the shared registry
    available_functions = TOOL_REGISTRY

    while True:
        spoken = ""
        # Never start recording while Jarvis is still talking, or the mic picks
        # him up. Playback is asynchronous now, so this has to be explicit.
        wait_until_spoken()

        transcribed_text = record_audio_and_transcribe_mlx_whisper() #User Text
        turn_start = time.time()

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
            chime()
            wait_until_spoken()
            mems_list = extract_important_messages(messages=messages) #Gets meaningful messages from conversation
            if mems_list:
                #Uploading meaningful memories to pinecone
                dense_index.upsert_records(namespace="jarvis-memory-namespace",records=mems_list)
            break

        elif intent == 'tool':
            #Needs tool usage
            # Queued, not blocking — this plays over the model call instead of
            # delaying it by the 2.2s it takes to speak.
            say("Right away sir.")

            group, tools = select_tools(transcribed_text)
            print(f"Tool group: {group} ({len(tools)} tools)")

            # Spell out the coming week by name. Given only an ISO date, qwen2.5:7b
            # works out weekdays wrong, so "remind me Friday" lands on the wrong day.
            now = datetime.now()
            upcoming = ", ".join(
                (now + timedelta(days=offset)).strftime("%A %Y-%m-%d")
                for offset in range(8)
            )
            date_context = (
                f"[Today is {now.strftime('%A %Y-%m-%d')} at {now.strftime('%H:%M')}. "
                f"Dates this coming week: {upcoming}. "
                f"Use these exact dates for any day the user names.]"
            )
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
            response = chat(model='qwen2.5:7b', messages=messages, stream=True,
                            keep_alive=OLLAMA_KEEP_ALIVE, options=GEN_OPTIONS)
            spoken = speak_stream(response)
            messages.append({"role": "assistant", "content": spoken})

        print("Jarvis:", spoken)
        print(f"Turn latency (transcript -> speech queued): {time.time() - turn_start:.2f}s")
        chime()


"""def contains_exit_phrase(transcribed_text):
    return any(phrase in transcribed_text for phrase in EXIT_PHRASES)"""

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


# Guarded so tests.py can import this module without launching the assistant.
if __name__ == "__main__":
    startup()
    main_loop()
