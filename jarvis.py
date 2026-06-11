"""
Jarvis — personal voice assistant, fully local (Ollama + MLX Whisper + Kokoro).

Orchestrates the whole pipeline:

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

import random
import threading
import time
from datetime import datetime, timedelta, timezone

import config
from logging_setup import get_logger, setup_logging

setup_logging()
log = get_logger("jarvis")

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
