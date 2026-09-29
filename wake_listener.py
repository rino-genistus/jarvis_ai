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

import numpy as np
import sounddevice as sd

HERE = os.path.dirname(os.path.abspath(__file__))

WAKE_THRESHOLD = float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.5"))
# Silero VAD gate inside openWakeWord: a wake score only counts if there is
# actual speech in the frame, which cuts false triggers from music and games.
VAD_THRESHOLD = 0.5
SAMPLE_RATE = 16000
FRAME = 1280                       # 80 ms, the frame size openWakeWord is built for
IDLE_UNLOAD_SECONDS = 60 * float(os.getenv("JARVIS_IDLE_UNLOAD_MINUTES", "10"))


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------- engine side

def _engine_main(conn):
    """
    Entry point of the engine process. Runs in a fresh interpreter.

    The first command is recorded on this thread while jarvis.py imports and
    warms up on another — loading takes several seconds, and the user is
    already talking.
    """
    import threading
    os.chdir(HERE)
    sys.path.insert(0, HERE)
    import mic

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
            message = conn.recv()
            if message == "wake":
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

    def run(self):
        log(f"Listening for 'Hey Jarvis' (engine retires after "
            f"{IDLE_UNLOAD_SECONDS / 60:g} idle minutes)")
        while True:
            self._wait_for_wake_word()
            log("Wake word detected")
            _play(WAKE_TONE)
            self._hand_off()

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
                        if score >= WAKE_THRESHOLD:
                            return
                        if self.engine and time.monotonic() - self.last_active > IDLE_UNLOAD_SECONDS:
                            self._retire_engine()
            except sd.PortAudioError as e:
                # Intermittent CoreAudio contention (-9986) used to take the
                # whole backend down. Wait it out and reopen instead.
                log(f"Microphone error, retrying in 2s: {e}")
                time.sleep(2)

    def _engine_alive(self):
        return self.engine is not None and self.engine.is_alive()

    def _hand_off(self):
        """Give the mic to the engine for one conversation and wait until it's done."""
        if self._engine_alive():
            self.conn.send("wake")
        else:
            log("Starting engine")
            self.conn, child_conn = self.ctx.Pipe()
            self.engine = self.ctx.Process(target=self.engine_target, args=(child_conn,),
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

    def _retire_engine(self):
        log("Engine idle, shutting it down to free memory")
        try:
            self.conn.send("quit")
        except (BrokenPipeError, OSError):
            pass
        self.engine.join(timeout=60)
        if self.engine.is_alive():
            self.engine.terminate()
            self.engine.join()
        self.engine = None

    def close(self):
        if self._engine_alive():
            self._retire_engine()


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
    ".env": "memory recall, weather, web search and Spotify",
}


def missing_setup():
    """What's switched off for lack of a file, as '<services> (no <file>)'."""
    return [f"{services} (no {name})" for name, services in SETUP_FILES.items()
            if not os.path.exists(os.path.join(HERE, name))]


def main():
    lock = _single_instance()
    if lock is None:
        log("Jarvis is already running")
        notify("Jarvis is already running", 'Say "Hey Jarvis" to talk.')
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
