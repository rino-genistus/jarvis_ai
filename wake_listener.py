"""
Always-on entry point for Jarvis — what Jarvis.app runs.

Two processes, so that idle Jarvis costs almost nothing:

    wake listener (this process, always running)
        openWakeWord models (wake_word.py) on 80 ms mic frames. Small and cheap.
            │  "Hey Jarvis"
            ▼
    engine process (jarvis.py: Whisper, Kokoro, agents, memory)
        Holds one conversation, then reports back and waits. After
        JARVIS_IDLE_UNLOAD_MINUTES with no wake word the listener retires it:
        the process exits and Ollama unloads its models, so every byte of the
        heavy stack is returned rather than merely freed inside Python.

A warm engine answers immediately. A cold one records the first command while
it is still loading, so the user never has to wait for a "ready" cue.

    python wake_listener.py     # run in the foreground
"""

import fcntl
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import time
from collections import deque

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))

# The listener reads .env too, so settings like JARVIS_WAKE_THRESHOLD apply here
from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

WAKE_THRESHOLD = float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.5"))
# Silero VAD gate inside openWakeWord: a wake score only counts if there is
# actual speech in the frame, which cuts false triggers from music and games.
VAD_THRESHOLD = 0.5
SAMPLE_RATE = 16000
FRAME = 1280                       # 80 ms, the frame size openWakeWord is built for
IDLE_UNLOAD_SECONDS = 60 * float(os.getenv("JARVIS_IDLE_UNLOAD_MINUTES", "10"))
HEARTBEAT_SECONDS = 60


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------- engine side

def _engine_main(conn, noise_floor=None):
    """
    Entry point of the engine process. Runs in a fresh interpreter.

    The first command is recorded on this thread while jarvis.py imports and
    warms up on another — loading takes several seconds, and the user is
    already talking. noise_floor is the room's loudness as the listener
    measured it, so the recorder knows what silence sounds like here.
    """
    import threading
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    import mic
    mic.set_noise_floor(noise_floor)

    loaded = {}

    def _load():
        import jarvis
        jarvis.startup()
        loaded["jarvis"] = jarvis

    loader = threading.Thread(target=_load)
    loader.start()
    first_command = mic.record_command()
    loader.join()

    jarvis = loaded.get("jarvis")
    if jarvis is None:
        return  # the import failed; its traceback is already in the log

    try:
        if first_command is None:
            jarvis.sleep_tone()
            jarvis.wait_until_spoken()
        else:
            jarvis.run_session(first_audio=first_command)
        conn.send("idle")

        while True:
            message, noise_floor = conn.recv()
            if message == "wake":
                mic.set_noise_floor(noise_floor)
                jarvis.run_session()
                conn.send("idle")
            elif message == "quit":
                break
    except (EOFError, BrokenPipeError):
        pass  # the listener is gone — don't linger holding gigabytes of models
    jarvis.shutdown()


# ---------------------------------------------------------------- listener side

def _tone(freqs, duration=0.12, rate=24000):
    t = np.linspace(0, duration, int(rate * duration), endpoint=False)
    return np.concatenate([np.sin(2 * np.pi * f * t) * 0.3 for f in freqs]).astype(np.float32)


WAKE_TONE = _tone([660, 880])      # rising: "I'm listening"
ERROR_TONE = _tone([220, 220], 0.2)


def _play(samples):
    try:
        sd.play(samples, 24000)
        sd.wait()
    except Exception as e:
        log(f"Could not play tone: {e}")


class WakeListener:

    def __init__(self, engine_target=_engine_main):
        from wake_word import WakeWordDetector
        self.detector = WakeWordDetector(vad_threshold=VAD_THRESHOLD)
        self.engine_target = engine_target     # swappable so tests can use a fake engine
        self.ctx = mp.get_context("spawn")
        self.engine = None
        self.conn = None
        self.last_active = time.monotonic()
        self.status = {
            "pid": os.getpid(),
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "state": "listening",
            "engine": "off",
            "last_wake": None,
            "wakes_today": 0,
            "mic_peak_dbfs": None,
            "best_wake_score": None,
            "mic_silent": None,
            "heartbeat": None,
            "services_off": missing_setup(),
        }
        self._window_peak = 0
        self._window_score = 0.0
        self._window_start = time.monotonic()
        # Loudness of the last ~30s of frames. Speech is sparse, so the median
        # is the room's background noise — what the recorder must ignore.
        self._frame_rms = deque(maxlen=int(30 * SAMPLE_RATE / FRAME))

    def noise_floor(self):
        return round(float(np.median(self._frame_rms)), 1) if self._frame_rms else None

    def run(self):
        log(f"Listening for 'Hey Jarvis' (engine retires after "
            f"{IDLE_UNLOAD_SECONDS / 60:g} idle minutes)")
        self._write_status()
        while True:
            self._wait_for_wake_word()
            log("Wake word detected")
            today = time.strftime("%Y-%m-%d")
            if (self.status["last_wake"] or "")[:10] != today:
                self.status["wakes_today"] = 0
            self.status["wakes_today"] += 1
            self.status["last_wake"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._set_state("conversation")
            _play(WAKE_TONE)
            self._hand_off()
            self._set_state("listening")

    def _set_state(self, state):
        self.status["state"] = state
        self.status["engine"] = "warm" if self._engine_alive() else "off"
        self._write_status()

    def _write_status(self):
        """
        logs/status.json — what `python status.py` and a second click on the
        app icon report. Written atomically so a reader never sees half a file.
        """
        path = os.path.join(HERE, "logs", "status.json")
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(self.status, f, indent=2)
            os.replace(path + ".tmp", path)
        except OSError as e:
            log(f"Could not write status: {e}")

    def _heartbeat(self, frame, score):
        """
        Track the loudest sample and best wake score, and once a minute record
        them. A mic that is permanently silent — what macOS delivers when
        microphone permission is missing — is otherwise indistinguishable from
        a quiet room, so it gets a warning in the log.
        """
        self._window_peak = max(self._window_peak, int(np.abs(frame.astype(np.int32)).max()))
        self._frame_rms.append(float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))))
        self._window_score = max(self._window_score, score)
        if time.monotonic() - self._window_start < HEARTBEAT_SECONDS:
            return
        peak = self._window_peak
        silent = peak == 0
        self.status.update({
            "mic_peak_dbfs": round(20 * np.log10(peak / 32768), 1) if peak else None,
            "best_wake_score": round(self._window_score, 3),
            "mic_silent": silent,
            "heartbeat": time.strftime("%Y-%m-%d %H:%M:%S"),
            "engine": "warm" if self._engine_alive() else "off",
            "noise_floor_rms": self.noise_floor(),
        })
        if silent:
            log("WARNING: microphone is delivering pure silence — check System Settings → "
                "Privacy & Security → Microphone and make sure Jarvis is allowed")
        self._write_status()
        self._window_peak, self._window_score = 0, 0.0
        self._window_start = time.monotonic()

    def _wait_for_wake_word(self):
        """
        Block until the wake word is heard. Retires the engine along the way
        if it has sat idle too long.
        """
        self.detector.reset()   # stale scores from the last detection would retrigger
        while True:
            try:
                with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                    dtype="int16", blocksize=FRAME) as stream:
                    while True:
                        frame, _ = stream.read(FRAME)
                        score = self.detector.score(frame[:, 0])
                        self._heartbeat(frame, score)
                        if score >= WAKE_THRESHOLD:
                            return
                        if self.engine and time.monotonic() - self.last_active > IDLE_UNLOAD_SECONDS:
                            self._retire_engine()
                            self._set_state("listening")
            except sd.PortAudioError as e:
                # Intermittent CoreAudio contention (-9986) used to take the
                # whole backend down. Wait it out and reopen instead.
                log(f"Microphone error, retrying in 2s: {e}")
                time.sleep(2)

    def _engine_alive(self):
        return self.engine is not None and self.engine.is_alive()

    def _hand_off(self):
        """Give the mic to the engine for one conversation and wait until it's done."""
        floor = self.noise_floor()
        if self._engine_alive():
            self.conn.send(("wake", floor))
        else:
            log(f"Starting engine (room noise {floor})")
            self.conn, child_conn = self.ctx.Pipe()
            self.engine = self.ctx.Process(target=self.engine_target, args=(child_conn, floor),
                                           name="jarvis-engine")
            self.engine.start()

        while True:
            if self.conn.poll(0.5):
                try:
                    if self.conn.recv() == "idle":
                        break
                except EOFError:
                    pass
            if not self.engine.is_alive():
                log(f"Engine exited unexpectedly (code {self.engine.exitcode}) — see log above")
                self.engine = None
                _play(ERROR_TONE)
                break
        self.last_active = time.monotonic()
        log("Conversation over, back to listening")

    def _retire_engine(self, reason="Engine idle, shutting it down to free memory"):
        log(reason)
        try:
            self.conn.send(("quit", None))
        except (BrokenPipeError, OSError):
            pass
        # Mid-conversation the engine won't read "quit" until the conversation
        # ends, so don't wait long; when idle, give it time to save memories.
        in_conversation = self.status.get("state") == "conversation"
        self.engine.join(timeout=5 if in_conversation else 60)
        if self.engine.is_alive():
            self.engine.terminate()
            self.engine.join()
        self.engine = None

    def close(self):
        if self._engine_alive():
            self._retire_engine("Shutting down")


def _single_instance():
    """
    Clicking the app icon while Jarvis is already running must not start a
    second copy fighting over the microphone. Returns the held lock, or None.
    """
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    lock = open(os.path.join(HERE, "logs", "jarvis.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return lock


def notify(title, message):
    """
    macOS notification. The app has no window or Dock icon, so this is the
    only visible sign that clicking it did anything.
    """
    # JSON string quoting is valid AppleScript quoting, as long as it stays unicode
    quote = lambda s: json.dumps(s, ensure_ascii=False)
    script = f"display notification {quote(message)} with title {quote(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


# Nothing is strictly required any more — jarvis.py switches off each agent
# whose key or file is missing — but the user should hear about it at the
# click, not discover it mid-conversation. token.json and .spotify_token are
# not listed: the first login creates them (`python jarvis.py`).
SETUP_FILES = {
    "credentials.json": "Calendar and Gmail",
    ".env": "weather, web search and Spotify",
}


def missing_setup():
    """What's switched off for lack of a file, as '<services> (no <file>)'."""
    return [f"{services} (no {name})" for name, services in SETUP_FILES.items()
            if not os.path.exists(os.path.join(HERE, name))]


def main():
    lock = _single_instance()
    if lock is None:
        # Reached when started again from a terminal. (A second click on the app
        # never gets here: macOS just brings the running app forward. That's
        # what Jarvis Status.app is for.)
        import status
        log("Jarvis is already running")
        notify("Jarvis is running", status.summary())
        return
    # pkill and logout send SIGTERM, which would otherwise skip `finally` and
    # orphan the engine. Turning it into SystemExit lets close() retire it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    listener = WakeListener()
    missing = missing_setup()
    if missing:
        log(f"Running with services off: {'; '.join(missing)}")
        notify("Jarvis is listening", f"Say \"Hey Jarvis\". Off until set up: {'; '.join(missing)}.")
    else:
        notify("Jarvis is listening", 'Say "Hey Jarvis" to talk.')
    try:
        listener.run()
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == "__main__":
    main()
