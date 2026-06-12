<<<<<<< HEAD
=======
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play
import os
import wave
import speech_recognition as sr
import mlx_whisper
from ollama import chat, ChatResponse
import time
import tempfile
from agents import Calendar_Agents, WebSearchAgents, WeatherSearch, SpotifyAgent, GmailAgent, ComputerControlAgent
from datetime import datetime
from kokoro import KPipeline
import sounddevice as sd
import numpy as np
from pinecone import Pinecone
import threading
import ui_bridge

ui_bridge.start()

start_time = time.time()

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
    try:
        kokoro_pipeline = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M')
        print("Kokoro Loaded")
    except Exception as e:
        print(f"Kokoro failed to load: {e}")
    finally:
        kokoro_ready.set()  # always unblock main thread even on failure

threading.Thread(target=load_kokoro, daemon=True).start()

calendar = Calendar_Agents()
websearch = WebSearchAgents()
weather = WeatherSearch()
spotify = SpotifyAgent()
gmail = GmailAgent()
computer = ComputerControlAgent()

available_functions = {
    'create_event': calendar.create_event,
    'get_calendar_events': calendar.get_calendar_events,
    'update_calendar_event': calendar.update_calendar_event,
    'delete_calendar_event': calendar.delete_calendar_event,
    'search_web': websearch.search_web,
    'extract_webpages': websearch.extract_webpages,
    'get_current_weather': weather.get_current_weather,
    'get_weather_with_time': weather.get_weather_with_time,
    'get_daily_forecast': weather.get_daily_forecast,
    'get_weather_alerts': weather.get_weather_alerts,
    'get_current_track': spotify.get_current_track,
    'search_song_and_queue': spotify.search_song_and_queue,
    'add_song_to_playlist': spotify.add_song_to_playlist,
    'create_playlist': spotify.create_playlist,
    'recently_played': spotify.recently_played,
    'skip_song': spotify.skip_song,
    'pause_song': spotify.pause_song,
    'shuffle': spotify.shuffle,
    'set_volume': spotify.set_volume,
    'get_all_labels': gmail.get_all_labels,
    'get_drafts': gmail.get_drafts,
    'get_email_by_id': gmail.get_email_by_id,
    'get_sender_profile': gmail.get_sender_profile,
    'get_sent_emails': gmail.get_sent_emails,
    'get_unread_emails': gmail.get_unread_emails,
    'mark_as_read': gmail.mark_as_read,
    'remove_email_from_trash': gmail.remove_email_from_trash,
    'reply_to_email': gmail.reply_to_email,
    'trash_email': gmail.trash_email,
    'search_email': gmail.search_email,
    'send_email': gmail.send_email,
    'open_application': computer.open_application,
    'close_application': computer.close_application,
    'switch_application': computer.switch_application,
    'list_open_applications': computer.list_open_applications,
    'open_file': computer.open_file,
    'create_file': computer.create_file,
    'delete_file': computer.delete_file,
    'move_file': computer.move_file,
}

system_prompt = f"""
    You are JARVIS — Just A Rather Very Intelligent System. You are the AI assistant from the Iron Man films: calm, precise, quietly witty, and entirely devoted to your user.

    ## Context
    Today's date is {current_date}.

    ## Character
    You speak with the measured authority of someone who already knows the answer and is simply deciding how much to say. Your wit is dry and surfaces in word choice and timing — never in jokes. You call your user "sir" when it feels natural, not as punctuation after every sentence. You anticipate needs, act without being told twice, and never explain yourself unless asked.

    ## Communication Rules
    You are speaking aloud. Your responses must sound natural when heard — no bullet points, no headers, no markdown. Speak in sentences.
    - Get to the point immediately. No preamble.
    - No affirmations. Skip "Certainly", "Of course", "Absolutely", "Great question" — go straight to the substance.
    - Match the register of the request. A quick question gets a quick answer. A complex problem gets a thorough one.
    - If you don't know something, say so plainly. Never fabricate.
    - Respond and stop. Do not end responses with a follow-up question as a matter of habit — only raise a follow-up if there is a specific, genuine reason the user should know about something. Silence is fine.

    ## Capabilities
    Research, analysis, writing, code, planning, scheduling, and reasoning. When given tools, use them efficiently and report back only what's relevant.

    ## Memory
    You only know what has been said in this session. When starting fresh, greet briefly and ask what's needed. Nothing more.

    ## Non-negotiables
    - Your user's goals are your goals. You are in their corner, always.
    - You do not moralize, lecture, or add unsolicited caveats.
    - Never invent prior context, history, or people not stated in this conversation.
    - Respond only with your spoken reply. No meta-commentary.
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
"""
Jarvis — personal voice assistant, fully local (Ollama + MLX Whisper + Kokoro).

<<<<<<< HEAD
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
=======
Orchestrates the whole pipeline:
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

  wake word -> conversation mode (VAD-recorded commands, 10s follow-up window,
  no wake word repetition) -> memory retrieval -> agentic LLM with 39 tools ->
  interruptible streamed TTS -> [END]/farewell session close -> background
  memory save (Pinecone + Obsidian) -> back to wake-word idle.

Also runs proactive mode: upcoming calendar events are announced ~10 minutes
ahead, and a morning briefing (weather, calendar, unread mail) is delivered
once per day after BRIEFING_HOUR — without being asked.

Run directly:  python3 jarvis.py
Or via the launchd agent / Jarvis.app (see install_launch_agents.sh).
"""

<<<<<<< HEAD
import random
import threading
import time
from datetime import datetime, timedelta, timezone
=======
_SD_RATE = 16000
_SILENCE_RMS = 0.005  # below this = silence

def _record_sd(duration: float) -> np.ndarray:
    """Record a fixed-duration clip via sounddevice. Returns float32 mono array."""
    frames = sd.rec(int(duration * _SD_RATE), samplerate=_SD_RATE, channels=1, dtype='float32')
    sd.wait()
    return frames.flatten()

def _save_wav(audio: np.ndarray, path: str):
    """Write float32 mono numpy audio to a 16-bit WAV file."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SD_RATE)
        wf.writeframes(pcm.tobytes())

def play_chime():
    """
    Function that Plays a Small Chime sound so that User knows when Jarvis is done talking
    """
    sample_rate = 24000
    duration = 0.15
    freq = 880
    t = np.linspace(0, duration, int(sample_rate * duration))
    tone = (np.sin(2 * np.pi * freq * t) * 0.3).astype(np.float32)
    sd.play(tone, samplerate=sample_rate)
    sd.wait()
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d

<<<<<<< HEAD
def play_audio_with_kokoro(text):
    """
    Kokoro model that plays the text returned by the LLM
    """
    kokoro_ready.wait()
    generator = kokoro_pipeline(text, voice='af_heart')
    chunks = []
    for i, (gs, ps, audio) in enumerate(generator):
        chunks.append(audio)

    if chunks:
        full_audio = np.concatenate(chunks)
        sd.play(full_audio, samplerate=24000)
        sd.wait()
    play_chime() #Chimes right before the User can talk. Make it sound a bit better later
=======
import config
from logging_setup import get_logger, setup_logging
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

setup_logging()
log = get_logger("jarvis")

<<<<<<< HEAD
from agents import Calendar_Agents, GmailAgent, SpotifyAgent, WeatherSearch, WebSearchAgents  # noqa: E402
from audio_io import AudioPlayer  # noqa: E402
from computer_control import ComputerControlAgent, FileIndex  # noqa: E402
from llm import FILLER_LINES, JarvisBrain  # noqa: E402
from memory import JarvisState, MemoryStore, extract_episode, extract_facts  # noqa: E402
from obsidian_store import ObsidianStore  # noqa: E402
from stt import CommandRecorder, Transcriber, WakeWordListener  # noqa: E402
from tts import TTSEngine  # noqa: E402
from ui_bridge import UIBridge  # noqa: E402


def _try_agent(name, factory):
    """Agents needing auth/network may fail at startup — degrade, don't die."""
=======
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
<<<<<<< HEAD

def record_audio_and_transcribe_mlx_whisper():
    """
    Current transcription method for user - free. Runs efficiently on Mac Silicone chip
    """
    with sr.Microphone() as source:
        r.energy_threshold = 200
        r.pause_threshold = 1.5
        r.phrase_threshold = 0.1
        r.non_speaking_duration = 0.8
        print("User Talks Now")
        audio_text = r.listen(source, timeout=10, phrase_time_limit=45)
        wav_audio_data = audio_text.get_wav_data()
=======
    
def record_audio_and_transcribe_mlx_whisper(listen_timeout=10):
    """Record command audio via sounddevice with VAD, transcribe via mlx_whisper."""
    chunk = int(0.1 * _SD_RATE)  # 100 ms chunks
    silence_needed = 15           # 1.5 s of silence ends the recording
    timeout_chunks = int(listen_timeout / 0.1)
    max_chunks = int(45.0 / 0.1)
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

    print("User Talks Now")
    frames = []
    silence_count = 0
    speech_started = False

<<<<<<< HEAD
        result = mlx_whisper.transcribe(
            temp_path,
            path_or_hf_repo="mlx-community/whisper-small-mlx",
        )

        os.remove(temp_path)
        print("User: ", result['text'].strip())
        return result["text"].strip()
=======
    with sd.InputStream(samplerate=_SD_RATE, channels=1, dtype='float32') as stream:
        for i in range(timeout_chunks + max_chunks):
            buf, _ = stream.read(chunk)
            data = buf.flatten()
            rms = float(np.sqrt(np.mean(data ** 2)))

            if not speech_started:
                if i >= timeout_chunks:
                    raise TimeoutError("No speech detected within timeout")
                if rms > _SILENCE_RMS:
                    speech_started = True
                    frames.append(data)
            else:
                frames.append(data)
                if rms < _SILENCE_RMS:
                    silence_count += 1
                    if silence_count >= silence_needed:
                        break
                else:
                    silence_count = 0

    if not frames:
        return ""

    audio = np.concatenate(frames)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        temp_path = f.name
    _save_wav(audio, temp_path)

    result = mlx_whisper.transcribe(temp_path, path_or_hf_repo="mlx-community/whisper-small-mlx")
    os.remove(temp_path)
    transcribed = result["text"].strip()
    print("User:", transcribed)
    return transcribed
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

def extract_important_messages(messages):
    """
    Given the messages from the current chat, uses qwen2.5:14b model to review all messages and retreive list of meaningful messages
    """
    response = chat(
        model='qwen2.5:14b',
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
    classifies intent for LLM to know how to proceed with conversation
    """
    """Returns 'exit', 'tool', or 'chat'"""
    response = chat(
            model='llama3.2:1b',
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

def safe_speak(text):
    """
    Makes sure that something is returned from tool calls so that TTS doesnt play empty sound files
    """
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
    try:
        agent = factory()
        log.info("%s ready", name)
        return agent
    except Exception as e:  # noqa: BLE001
        log.error("%s unavailable, its tools are disabled: %s", name, e)
        return None


class Jarvis:
    def __init__(self):
        start = time.time()
        self.bridge = UIBridge()
        self.bridge.start()
        self.player = AudioPlayer()

        # ------------------------------------------------ agents
        self.calendar = _try_agent("Calendar", Calendar_Agents)
        self.websearch = _try_agent("WebSearch", WebSearchAgents)
        self.weather = _try_agent("Weather", WeatherSearch)
        self.spotify = _try_agent("Spotify", SpotifyAgent)
        self.gmail = _try_agent("Gmail", GmailAgent)
        self.file_index = FileIndex()
        self.file_index.start()
        self.computer = ComputerControlAgent(self.file_index)

        # ------------------------------------------------ voice pipeline
        self.tts = TTSEngine(self.player, spotify_agent=self.spotify)
        self.transcriber = Transcriber()
        self.recorder = CommandRecorder()
        self.wake = WakeWordListener(self.transcriber)

        # ------------------------------------------------ memory
        self.memory = MemoryStore()
        threading.Thread(target=self._connect_memory, name="memory-connect",
                         daemon=True).start()
        self.obsidian = ObsidianStore()
        self.state = JarvisState()

        self.brain = JarvisBrain(self._build_tools(), bridge=self.bridge,
                                 on_tools_start=self._speak_filler)
        self._speech_lock = threading.Lock()   # main loop vs proactive announcements
        log.info("backend constructed in %.1fs", time.time() - start)

    def _connect_memory(self):
        try:
            self.memory.connect()
        except Exception as e:  # noqa: BLE001
            log.error("Pinecone unavailable — running without long-term memory: %s", e)

    # ------------------------------------------------------------ tools
    def _build_tools(self) -> dict:
        tools: dict = {}
        if self.calendar:
            tools.update({
                "create_event": self.calendar.create_event,
                "get_calendar_events": self.calendar.get_calendar_events,
                "update_calendar_event": self.calendar.update_calendar_event,
                "delete_calendar_event": self.calendar.delete_calendar_event,
            })
        if self.websearch:
            tools.update({
                "search_web": self.websearch.search_web,
                "extract_webpages": self.websearch.extract_webpages,
            })
        if self.weather:
            tools.update({
                "get_current_weather": self.weather.get_current_weather,
                "get_weather_with_time": self.weather.get_weather_with_time,
                "get_daily_forecast": self.weather.get_daily_forecast,
                "get_weather_alerts": self.weather.get_weather_alerts,
            })
        if self.spotify:
            tools.update({
                "get_current_track": self.spotify.get_current_track,
                "search_song_and_queue": self.spotify.search_song_and_queue,
                "add_song_to_playlist": self.spotify.add_song_to_playlist,
                "create_playlist": self.spotify.create_playlist,
                "recently_played": self.spotify.recently_played,
                "skip_song": self.spotify.skip_song,
                "pause_song": self.spotify.pause_song,
                "shuffle": self.spotify.shuffle,
                "set_volume": self.spotify.set_volume,
            })
        if self.gmail:
            tools.update({
                "send_email": self.gmail.send_email,
                "search_email": self.gmail.search_email,
                "get_unread_emails": self.gmail.get_unread_emails,
                "get_email_by_id": self.gmail.get_email_by_id,
                "reply_to_email": self.gmail.reply_to_email,
                "mark_as_read": self.gmail.mark_as_read,
                "trash_email": self.gmail.trash_email,
                "untrash_email": self.gmail.untrash_email,
                "get_drafts": self.gmail.get_drafts,
                "get_sent_emails": self.gmail.get_sent_emails,
                "get_sender_profile": self.gmail.get_sender_profile,
                "get_all_labels": self.gmail.get_all_labels,
            })
        tools.update({
            "open_application": self.computer.open_application,
            "close_application": self.computer.close_application,
            "switch_to_application": self.computer.switch_to_application,
            "list_open_applications": self.computer.list_open_applications,
            "open_file": self.computer.open_file,
            "create_file": self.computer.create_file,
            "delete_file": self.computer.delete_file,
            "move_file": self.computer.move_file,
        })
        log.info("%d tools registered", len(tools))
        return tools

    # ------------------------------------------------------------ main loop
    def run(self):
        self.tts.wait_ready()
        self.file_index.ready.wait(timeout=120)   # index ready before first command
        self.player.calibrate()
        if config.PROACTIVE_ENABLED:
            threading.Thread(target=self._proactive_loop, name="proactive",
                             daemon=True).start()
        log.info("Jarvis online")
        while True:
            try:
                self.bridge.state("idle")
                self.wake.wait_for_wake()
                self._conversation()
            except KeyboardInterrupt:
                log.info("shutting down")
                return
            except Exception as e:  # noqa: BLE001
                log.exception("main loop error, recovering: %s", e)
                self.bridge.error(str(e))
                time.sleep(2)

    def _conversation(self):
        """One session: from wake word until [END]/farewell/timeout."""
        self.brain.reset()
        self.brain.set_session_context(self.state.continuity_line())
        with self._speech_lock:
            self.player.play_chime()   # heard you — go ahead

        while True:
            self.bridge.state("listening")
            audio = self.recorder.record(timeout=config.VAD_WAIT_TIMEOUT)
            if audio is None:
                log.info("conversation timed out — returning to wake word")
                break
            self.bridge.state("thinking")
            text = self.transcriber.transcribe(audio)
            if not text or len(text.split()) == 0:
                continue
            log.info("user: %s", text)
            self.bridge.transcript("user", text)

            with self._speech_lock:
                memories = self.memory.retrieve(text)
                self.brain.set_memories(memories)
                self.bridge.memory(len(memories))
                try:
                    spoken, ended = self.brain.respond(text)
                except Exception as e:  # noqa: BLE001
                    log.exception("LLM call failed: %s", e)
                    self.bridge.error("LLM failure")
                    spoken, ended = "Apologies sir, my reasoning engine just stumbled. Say that again?", False
                log.info("jarvis: %s", spoken)
                self.bridge.transcript("jarvis", spoken)
                self.bridge.state("speaking")
                self.tts.speak(spoken)

            if self.player.interrupted:
                log.info("user interrupted — listening")
                continue
            if ended:
                log.info("session ended")
                break

        self.bridge.state("idle")
        # never block the return to wake-word mode on memory writes
        history = list(self.brain.messages)
        threading.Thread(target=self._save_session, args=(history,),
                         name="memory-save", daemon=True).start()

    def _speak_filler(self):
        """Brief human acknowledgement before tool rounds instead of dead air."""
        self.bridge.state("tool")
        self.tts.speak(random.choice(FILLER_LINES), chime=False)

    # ------------------------------------------------------------ memory save
    def _save_session(self, history: list):
        try:
            facts = extract_facts(history)
            episode = extract_episode(history)
            if facts:
                self.memory.store_facts(facts)
            if episode:
                self.memory.store_episode(episode)
            self.obsidian.append_session(episode, facts)
            session_count = self.state.record_session(episode)
            if session_count % config.CONSOLIDATE_EVERY_N_SESSIONS == 0:
                log.info("running periodic memory consolidation")
                self.memory.consolidate()
        except Exception as e:  # noqa: BLE001
            log.exception("session memory save failed: %s", e)

    # ------------------------------------------------------------ proactive mode
    def _proactive_loop(self):
        announced: set[str] = set()
        while True:
            time.sleep(config.PROACTIVE_POLL_SECONDS)
            try:
                self._announce_upcoming_events(announced)
                self._morning_briefing()
            except Exception as e:  # noqa: BLE001
                log.debug("proactive cycle skipped: %s", e)

    def _say_proactively(self, text: str) -> bool:
        """Speak only when idle — never talk over an active conversation."""
        if not self._speech_lock.acquire(blocking=False):
            return False
        try:
            self.bridge.state("speaking")
            self.bridge.transcript("jarvis", text)
            self.tts.speak(text)
            self.bridge.state("idle")
            return True
        finally:
            self._speech_lock.release()

    def _announce_upcoming_events(self, announced: set):
        if not self.calendar:
            return
<<<<<<< HEAD
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(minutes=config.PROACTIVE_LEAD_MINUTES)
        events = self.calendar.get_calendar_events(
            now.strftime("%Y-%m-%dT%H:%M:%SZ"), horizon.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        for event in events or []:
            event_id = event.get("id", "")
            start_raw = (event.get("start") or {}).get("dateTime")
            if not start_raw or event_id in announced:
                continue
            start_dt = datetime.fromisoformat(start_raw)
            minutes = max(1, int((start_dt - now).total_seconds() // 60))
            title = event.get("summary", "an event")
            if self._say_proactively(f"Sir, heads up — {title} starts in {minutes} "
                                     f"minute{'s' if minutes != 1 else ''}."):
                announced.add(event_id)

    def _morning_briefing(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if (datetime.now().hour < config.BRIEFING_HOUR
                or self.state.data.get("last_briefing_date") == today):
            return
        parts = [f"Good morning, sir. It's {datetime.now().strftime('%A, %B %d')}."]
        if self.weather and config.HOME_LAT and config.HOME_LON:
            try:
                data = self.weather.get_current_weather(
                    float(config.HOME_LAT), float(config.HOME_LON),
                    exclude=["minutely", "hourly", "alerts"],
                )
                current = data.get("current", {})
                temp = round(current.get("temp", 0))
                sky = (current.get("weather") or [{}])[0].get("description", "")
                parts.append(f"It's {temp} degrees with {sky}.")
            except Exception as e:  # noqa: BLE001
                log.debug("briefing weather skipped: %s", e)
        if self.calendar:
            try:
                events = self.calendar.get_calendar_events(today, today) or []
                timed = [e for e in events if (e.get("start") or {}).get("dateTime")]
                if not timed:
                    parts.append("Your calendar is clear today.")
                else:
                    first = timed[0]
                    first_time = datetime.fromisoformat(
                        first["start"]["dateTime"]).strftime("%-I:%M %p")
                    parts.append(f"You have {len(timed)} event"
                                 f"{'s' if len(timed) != 1 else ''} today, starting with "
                                 f"{first.get('summary', 'an event')} at {first_time}.")
            except Exception as e:  # noqa: BLE001
                log.debug("briefing calendar skipped: %s", e)
        if self.gmail:
            try:
                unread = self.gmail.get_unread_emails(max_results=10)
                if isinstance(unread, list) and unread:
                    parts.append(f"And {len(unread)} unread email"
                                 f"{'s' if len(unread) != 1 else ''} waiting.")
            except Exception as e:  # noqa: BLE001
                log.debug("briefing mail skipped: %s", e)
        if self._say_proactively(" ".join(parts)):
            self.state.data["last_briefing_date"] = today
            self.state.save()


if __name__ == "__main__":
    Jarvis().run()
=======
        if kokoro_pipeline is None:
            print(f"Jarvis (no TTS): {text}")
            play_chime()
            return
        play_audio_with_kokoro(text)
    except KeyboardInterrupt:
        pass

<<<<<<< HEAD
def main_loop():
    """
    Main Program Loop
    """
    with sr.Microphone() as source:
        print("Calibrating microphone...")
        r.adjust_for_ambient_noise(source, duration=0.3)
        r.dynamic_energy_threshold = False
    #List of available tools for LLM
    available_functions = {
        'create_event': calendar.create_event,
        'get_calendar_events': calendar.get_calendar_events,
        'update_calendar_event': calendar.update_calendar_event,
        'delete_calendar_event': calendar.delete_calendar_event,
        'search_web': websearch.search_web,
        'extract_webpages': websearch.extract_webpages,
        'get_current_weather': weather.get_current_weather,
        'get_weather_with_time': weather.get_weather_with_time,
        'get_current_track': spotify.get_current_track,
        'search_song_and_queue': spotify.search_song_and_queue,
        'add_song_to_playlist': spotify.add_song_to_playlist,
        'create_playlist': spotify.create_playlist,
        'recently_played': spotify.recently_played,
        'skip_song': spotify.skip_song,
        'pause_song': spotify.pause_song,
        'shuffle': spotify.shuffle,
        'set_volume': spotify.set_volume,
    }
    while True:
        spoken = ""
        transcribed_text = record_audio_and_transcribe_mlx_whisper() #User Text
        intent = classify_intent(transcribed_text) #Intent for LLM
        memories = retrieve_memories(transcribed_text) #List of meaningful memories from previous conversations
        if memories:
            memory_block = "\n".join(f"- {m}" for m in memories)
            messages[0]["content"] = system_prompt + f"\n\n## What you know about the user:\n{memory_block}" #Adding meaningful memories to message history for LLM context
        print(f"Intent: {intent}")
        messages.append({"role": "user", "content": transcribed_text})
        if intent == 'exit':
            #User is leaving or conversation is done
            completion = chat(model="qwen2.5:7b", messages=messages)
            spoken = completion.message.content or ""
            messages.append({"role": "assistant", "content": spoken})
            safe_speak(spoken)
            mems_list = extract_important_messages(messages=messages) #Gets meaningful messages from conversation
            if mems_list:
                #Uploading meaningful memories to pinecone
                dense_index.upsert_records(namespace="jarvis-memory-namespace",records=mems_list)
            break
=======
def handle_command(listen_timeout=10, conversation_mode=False):
    """Record and process one command cycle. Returns True to continue conversation, False to return to wake-word mode."""
    ui_bridge.emit({"event": "state", "value": "listening"})
    try:
        transcribed_text = record_audio_and_transcribe_mlx_whisper(listen_timeout)
    except Exception as e:
        print(f"Transcription error: {e}")
        ui_bridge.emit({"event": "state", "value": "idle"})
        return False
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

    if not transcribed_text.strip():
        ui_bridge.emit({"event": "state", "value": "idle"})
        return False

<<<<<<< HEAD
            current_date = datetime.now().strftime("%Y-%m-%d")
            dated_messages = messages[:-1] + [{
                "role": "user",
                "content": f"[Today's date is {current_date}] {messages[-1]['content']}"
            }]
            response: ChatResponse = chat(
                model='qwen2.5:7b',
                messages=dated_messages,
                tools=[
                    calendar.create_event,
                    calendar.get_calendar_events,
                    calendar.delete_calendar_event,
                    calendar.update_calendar_event,
                    websearch.search_web,
                    websearch.extract_webpages,
                    weather.get_current_weather,
                    weather.get_weather_with_time,
                    spotify.get_current_track,
                    spotify.search_song_and_queue,
                    spotify.skip_song,
                    spotify.pause_song,
                    spotify.shuffle,
                    spotify.set_volume,
                ],
            )
            messages.append({"role": "assistant", "content": response.message.content or ""})
=======
    ui_bridge.emit({"event": "transcript", "role": "user", "text": transcribed_text})
    ui_bridge.emit({"event": "state", "value": "thinking"})
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141

    intent = classify_intent(transcribed_text)
    memories = retrieve_memories(transcribed_text)
    if memories:
        memory_block = "\n".join(f"- {m}" for m in memories)
        messages[0]["content"] = system_prompt + f"\n\n## What you know about the user:\n{memory_block}"
        ui_bridge.emit({"event": "memory", "count": len(memories)})

    print(f"Intent: {intent}")
    messages.append({"role": "user", "content": transcribed_text})
    spoken = ""

    if intent == 'exit':
        completion = chat(model="qwen2.5:14b", messages=messages)
        spoken = completion.message.content or ""
        messages.append({"role": "assistant", "content": spoken})
        ui_bridge.emit({"event": "transcript", "role": "assistant", "text": spoken})
        ui_bridge.emit({"event": "state", "value": "speaking"})
        safe_speak(spoken)
        ui_bridge.emit({"event": "state", "value": "idle"})
        mems_list = extract_important_messages(messages=messages)
        if mems_list:
            dense_index.upsert_records(namespace="jarvis-memory-namespace", records=mems_list)
        messages.clear()
        messages.append({"role": "system", "content": system_prompt})
        print("Session ended. Returning to idle...")
        return False

    elif intent == 'tool':
        if not conversation_mode:
            ui_bridge.emit({"event": "state", "value": "speaking"})
            safe_speak("Right away, sir.")
        ui_bridge.emit({"event": "state", "value": "thinking"})
        current_date = datetime.now().strftime("%Y-%m-%d")
        dated_messages = messages[:-1] + [{
            "role": "user",
            "content": f"[Today's date is {current_date}] {messages[-1]['content']}"
        }]
        response: ChatResponse = chat(
            model='qwen2.5:14b',
            messages=dated_messages,
            tools=[
                calendar.create_event,
                calendar.get_calendar_events,
                calendar.delete_calendar_event,
                calendar.update_calendar_event,
                websearch.search_web,
                websearch.extract_webpages,
                weather.get_current_weather,
                weather.get_weather_with_time,
                weather.get_daily_forecast,
                weather.get_weather_alerts,
                spotify.get_current_track,
                spotify.search_song_and_queue,
                spotify.add_song_to_playlist,
                spotify.create_playlist,
                spotify.recently_played,
                spotify.skip_song,
                spotify.pause_song,
                spotify.shuffle,
                spotify.set_volume,
                gmail.send_email,
                gmail.search_email,
                gmail.get_all_labels,
                gmail.get_drafts,
                gmail.get_email_by_id,
                gmail.get_sender_profile,
                gmail.get_unread_emails,
                gmail.mark_as_read,
                gmail.reply_to_email,
                gmail.trash_email,
                gmail.remove_email_from_trash,
                gmail.get_sent_emails,
                computer.open_application,
                computer.close_application,
                computer.switch_application,
                computer.list_open_applications,
                computer.open_file,
                computer.create_file,
                computer.delete_file,
                computer.move_file,
            ],
        )
        messages.append({"role": "assistant", "content": response.message.content or ""})

        if response.message.tool_calls:
            for tool_call in response.message.tool_calls:
                if tool_call.function.name in available_functions:
                    print(f"Calling {tool_call.function.name} with {tool_call.function.arguments}")
                    ui_bridge.emit({"event": "tool", "name": tool_call.function.name, "status": "running"})
                    result = available_functions[tool_call.function.name](**tool_call.function.arguments)
                    print(f"Tool result: {result}")
                    ui_bridge.emit({"event": "tool", "name": tool_call.function.name, "status": "done"})
                    messages.append({"role": "tool", "tool_name": tool_call.function.name, "content": str(result)})

            messages.append({
                "role": "user",
                "content": "Report the result in Jarvis's voice. Give only what's relevant — no padding, no follow-up questions. End when you're done."
            })
            follow_up: ChatResponse = chat(model='qwen2.5:14b', messages=messages)
            spoken = follow_up.message.content or ""
            messages.append({"role": "assistant", "content": spoken})
        else:
            spoken = response.message.content or response.message.thinking or ""

    else:  # chat
        response: ChatResponse = chat(model='qwen2.5:14b', messages=messages)
        spoken = response.message.content or ""
        messages.append({"role": "assistant", "content": spoken})

    print("Jarvis:", spoken)
    ui_bridge.emit({"event": "transcript", "role": "assistant", "text": spoken})
    ui_bridge.emit({"event": "state", "value": "speaking"})
    safe_speak(spoken)
    ui_bridge.emit({"event": "state", "value": "idle"})
    return True


def wake_word_loop():
    """Idle loop: record 3-second clips via sounddevice and activate on 'jarvis'."""
    print("Jarvis is ready. Say 'Jarvis' to activate.")
    while True:
        try:
            audio = _record_sd(3.0)
            rms = float(np.sqrt(np.mean(audio ** 2)))
            print(f"[Wake] rms={rms:.4f}")

            if rms < _SILENCE_RMS:
                continue  # silence — skip transcription

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                temp_path = f.name
            _save_wav(audio, temp_path)

            result = mlx_whisper.transcribe(
                temp_path,
                path_or_hf_repo="mlx-community/whisper-small-mlx",
                initial_prompt="Jarvis",
            )
            os.remove(temp_path)

            heard = result["text"].strip().lower()
            print(f"[Wake] heard: {heard}")

            if "jarvis" in heard:
                print("Wake word detected!")
                play_chime()
                should_continue = handle_command()
                ui_bridge.emit({"event": "state", "value": "idle"})
                while should_continue:
                    should_continue = handle_command(listen_timeout=7, conversation_mode=True)
                    ui_bridge.emit({"event": "state", "value": "idle"})
                print("Conversation ended. Listening for wake word.")

        except KeyboardInterrupt:
            print("\nJarvis shutting down.")
            break
        except Exception as e:
            print(f"Wake word error: {e}")
            continue


"""def contains_exit_phrase(transcribed_text):
    return any(phrase in transcribed_text for phrase in EXIT_PHRASES)"""

kokoro_ready.wait()
<<<<<<< HEAD
#main_loop()
print(f"First Command: {time.time() - start_time:.2f}s")
#spotify.shuffle(False)
emails = gmail.get_unread_emails()
for email in emails:
    print(email.get("id"))
#print(gmail.get_all_labels())
#print(gmail.get_email_by_id('19db13ec2dccb18a'))
#print(gmail.send_email("Hello, Test email", "rr1406@scarletmail.rutgers.edu", 'rgenistus@gmail.com'))
print(gmail.get_sender_profile())
#print(gmail.search_email(query="from: rinogenistus@gmail.com"))
print(gmail.reply_to_email(email_id="19dbca5b6b521fab"))
=======
computer._index_thread.join()

print(f"Available functions: {len(available_functions)}")
wake_word_loop()
print(f"Session duration: {time.time() - start_time:.2f}s")
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
>>>>>>> 9ed6b258f05bee1de1fbe9dda0b8891da1bf4141
