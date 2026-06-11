"""
Audio output for Jarvis: interruptible playback, the end-of-turn chime,
and Spotify ducking.

Playback is written to a sounddevice OutputStream in small blocks, checking a
stop event between blocks — so speech can be cut off ~20ms after an interrupt
fires instead of blocking until the clip finishes (the old sd.play/sd.wait
behaviour).

While Jarvis speaks, an InterruptMonitor watches the microphone: if the user
talks over Jarvis loudly enough for INTERRUPT_SUSTAIN seconds (threshold is a
multiple of the calibrated ambient baseline, so speaker bleed and desk bumps
don't trigger it), playback stops immediately and the caller is told the user
wants the floor.
"""

import threading
import time

import numpy as np
import sounddevice as sd

import config
from logging_setup import get_logger

log = get_logger("jarvis.audio")

_BLOCK = 1024  # frames per write; the granularity of interruption


class AudioPlayer:
    """Single shared playback channel with interruption support."""

    def __init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()       # one clip at a time
        self.interrupted = False            # set when the last playback was cut off
        self.ambient_rms = 0.005            # updated by calibrate()

    # ------------------------------------------------------------ calibration
    def calibrate(self, seconds: float = 0.5) -> None:
        """Sample ambient room noise so the interrupt threshold adapts to the room."""
        try:
            recording = sd.rec(int(seconds * config.AUDIO_SAMPLE_RATE),
                               samplerate=config.AUDIO_SAMPLE_RATE,
                               channels=1, dtype="float32")
            sd.wait()
            self.ambient_rms = max(float(np.sqrt(np.mean(recording ** 2))), 0.002)
            log.debug("ambient RMS baseline: %.4f", self.ambient_rms)
        except Exception as e:  # noqa: BLE001
            log.warning("mic calibration failed, keeping default baseline: %s", e)

    # ------------------------------------------------------------ playback
    def play(self, audio: np.ndarray, samplerate: int) -> bool:
        """Play a clip to completion or interruption. Returns False if interrupted."""
        with self._lock:
            return self._play_locked(audio, samplerate)

    def _play_locked(self, audio: np.ndarray, samplerate: int) -> bool:
        if audio is None or len(audio) == 0:
            return True
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        try:
            with sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32",
                                 blocksize=_BLOCK) as stream:
                for start in range(0, len(audio), _BLOCK):
                    if self._stop.is_set():
                        self.interrupted = True
                        return False
                    stream.write(audio[start:start + _BLOCK])
        except sd.PortAudioError as e:
            log.warning("playback PortAudio error: %s", e)
            return True
        return True

    def play_stream(self, chunk_iter, samplerate: int) -> bool:
        """
        Stream chunks from a generator (Kokoro) straight to the device as they
        are produced — first words play while later sentences are still being
        synthesised. Returns False if interrupted.
        """
        with self._lock:
            try:
                with sd.OutputStream(samplerate=samplerate, channels=1, dtype="float32",
                                     blocksize=_BLOCK) as stream:
                    for chunk in chunk_iter:
                        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
                        for start in range(0, len(chunk), _BLOCK):
                            if self._stop.is_set():
                                self.interrupted = True
                                return False
                            stream.write(chunk[start:start + _BLOCK])
            except sd.PortAudioError as e:
                log.warning("stream playback PortAudio error: %s", e)
        return True

    # ------------------------------------------------------------ control
    def begin_utterance(self) -> None:
        self._stop.clear()
        self.interrupted = False

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ chime
    def play_chime(self) -> None:
        """Short 880 Hz cue with a soft attack/decay so it doesn't click."""
        sr = config.KOKORO_SAMPLE_RATE
        duration = 0.15
        t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
        tone = np.sin(2 * np.pi * 880 * t) * 0.3
        fade = min(len(tone) // 4, int(0.02 * sr))
        ramp = np.linspace(0, 1, fade, dtype=np.float32)
        tone[:fade] *= ramp
        tone[-fade:] *= ramp[::-1]
        self.play(tone.astype(np.float32), sr)


class InterruptMonitor:
    """
    Watches the mic while Jarvis speaks. Sustained loud input (the user talking
    over Jarvis) stops playback via player.stop().
    """

    def __init__(self, player: AudioPlayer):
        self.player = player
        self._active = threading.Event()
        if config.INTERRUPT_ENABLED:
            threading.Thread(target=self._run, name="interrupt-monitor", daemon=True).start()

    def __enter__(self):
        self._active.set()
        return self

    def __exit__(self, *exc):
        self._active.clear()
        return False

    def _run(self) -> None:
        frame = int(0.05 * config.AUDIO_SAMPLE_RATE)  # 50ms analysis windows
        needed = max(1, int(config.INTERRUPT_SUSTAIN / 0.05))
        while True:
            self._active.wait()
            hot_streak = 0
            try:
                with sd.InputStream(samplerate=config.AUDIO_SAMPLE_RATE, channels=1,
                                    dtype="float32", blocksize=frame) as stream:
                    while self._active.is_set():
                        block, _ = stream.read(frame)
                        rms = float(np.sqrt(np.mean(block ** 2)))
                        threshold = self.player.ambient_rms * config.INTERRUPT_RMS_MULTIPLIER
                        if rms > threshold:
                            hot_streak += 1
                            if hot_streak >= needed:
                                log.info("user interrupt detected (rms %.4f > %.4f)", rms, threshold)
                                self.player.stop()
                                self._active.clear()
                        else:
                            hot_streak = 0
            except sd.PortAudioError as e:
                # mic contention while speakers are busy — back off, try again
                log.debug("interrupt monitor stream error: %s", e)
                time.sleep(1.0)


class SpotifyDucker:
    """Lowers Spotify while Jarvis speaks and restores it afterwards."""

    def __init__(self, spotify_agent):
        self.spotify = spotify_agent
        self._saved_volume = None

    def __enter__(self):
        if not config.DUCKING_ENABLED:
            return self
        threading.Thread(target=self._duck, name="duck", daemon=True).start()
        return self

    def __exit__(self, *exc):
        if config.DUCKING_ENABLED:
            threading.Thread(target=self._restore, name="unduck", daemon=True).start()
        return False

    def _duck(self) -> None:
        try:
            sp = getattr(self.spotify, "sp", None)
            if not sp:
                return
            playback = sp.current_playback()
            if playback and playback.get("is_playing"):
                device = playback.get("device") or {}
                volume = device.get("volume_percent")
                if volume is not None and volume > config.DUCK_VOLUME:
                    self._saved_volume = volume
                    sp.volume(config.DUCK_VOLUME)
                    log.debug("ducked Spotify %d%% -> %d%%", volume, config.DUCK_VOLUME)
        except Exception as e:  # noqa: BLE001
            log.debug("ducking skipped: %s", e)

    def _restore(self) -> None:
        try:
            if self._saved_volume is not None:
                self.spotify.sp.volume(self._saved_volume)
                log.debug("restored Spotify volume to %d%%", self._saved_volume)
        except Exception as e:  # noqa: BLE001
            log.debug("volume restore failed: %s", e)
        finally:
            self._saved_volume = None
