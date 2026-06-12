"""
Speech-to-text for Jarvis: wake word detection, command recording, transcription.

Wake word — two engines, selected by JARVIS_WAKE_ENGINE (default "auto"):

- "porcupine": pvporcupine streams 512-sample frames for sub-100ms detection at
  near-zero CPU. Needs PICOVOICE_ACCESS_KEY (free personal tier). The built-in
  "jarvis" keyword is used. This removes the 3-6s wake latency entirely.
- "whisper": the original approach — record a 3s clip, RMS-gate silence, then
  run MLX Whisper and scan for "jarvis". Hardened against ambient noise with a
  minimum word count and Whisper confidence thresholds (no_speech_prob /
  avg_logprob), so game audio and background voices stop burning cycles.

Command recording is voice-activity detected with sounddevice directly: starts
on speech onset (with a 0.3s pre-roll so the first syllable isn't clipped),
stops after VAD_STOP_SILENCE of quiet, hard-capped at VAD_MAX_COMMAND seconds.

Transcription defaults to MLX Whisper (free, local). ElevenLabs Scribe stays
wired behind JARVIS_STT_ENGINE=scribe for the paid release build.

All mic loops recover from PortAudio errors (the -9986 crash) by backing off
and reopening the stream instead of taking the process down.
"""

import collections
import io
import time
import wave

import numpy as np
import sounddevice as sd

import config
from logging_setup import get_logger

log = get_logger("jarvis.stt")
wake_log = get_logger("jarvis.wake")  # DEBUG-level polling noise stays out of the console

_FRAME = int(0.03 * config.AUDIO_SAMPLE_RATE)  # 30ms VAD frames


def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


class Transcriber:
    """Whisper (local, default) or ElevenLabs Scribe (paid, flag-gated)."""

    def __init__(self):
        self._scribe = None

    def transcribe(self, audio: np.ndarray) -> str:
        if config.STT_ENGINE == "scribe":
            try:
                return self._transcribe_scribe(audio)
            except Exception as e:  # noqa: BLE001
                log.error("Scribe failed, falling back to Whisper: %s", e)
        return self._transcribe_whisper(audio)["text"].strip()

    def _transcribe_whisper(self, audio: np.ndarray) -> dict:
        import mlx_whisper
        return mlx_whisper.transcribe(
            np.asarray(audio, dtype=np.float32).reshape(-1),
            path_or_hf_repo=config.WHISPER_REPO,
        )

    def transcribe_with_confidence(self, audio: np.ndarray) -> tuple[str, float, float]:
        """Returns (text, avg_logprob, no_speech_prob) for false-positive filtering."""
        result = self._transcribe_whisper(audio)
        segments = result.get("segments") or []
        if not segments:
            return result["text"].strip(), -10.0, 1.0
        avg_logprob = float(np.mean([s.get("avg_logprob", -10.0) for s in segments]))
        no_speech = float(np.mean([s.get("no_speech_prob", 1.0) for s in segments]))
        return result["text"].strip(), avg_logprob, no_speech

    def _transcribe_scribe(self, audio: np.ndarray) -> str:
        import os
        from elevenlabs.client import ElevenLabs
        if self._scribe is None:
            self._scribe = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
        pcm16 = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(config.AUDIO_SAMPLE_RATE)
            wav.writeframes(pcm16.tobytes())
        buffer.seek(0)
        result = self._scribe.speech_to_text.convert(
            file=buffer, model_id="scribe_v2", language_code="eng",
        )
        return result.text.strip()


class CommandRecorder:
    """VAD recording: pre-roll + speech onset + trailing-silence cutoff."""

    def record(self, timeout: float = config.VAD_WAIT_TIMEOUT) -> np.ndarray | None:
        """Capture one spoken command. Returns None if no speech before timeout."""
        preroll = collections.deque(maxlen=int(0.3 / 0.03))  # last 0.3s before onset
        captured: list[np.ndarray] = []
        silence_limit = int(config.VAD_STOP_SILENCE / 0.03)
        max_frames = int(config.VAD_MAX_COMMAND / 0.03)
        speaking = False
        silent_streak = 0
        deadline = time.monotonic() + timeout

        try:
            with sd.InputStream(samplerate=config.AUDIO_SAMPLE_RATE, channels=1,
                                dtype="float32", blocksize=_FRAME) as stream:
                while True:
                    block, _ = stream.read(_FRAME)
                    block = block.reshape(-1)
                    level = _rms(block)
                    if not speaking:
                        preroll.append(block.copy())
                        if level > config.VAD_START_THRESHOLD:
                            speaking = True
                            captured.extend(preroll)
                            log.debug("speech onset (rms %.4f)", level)
                        elif time.monotonic() > deadline:
                            return None
                    else:
                        captured.append(block.copy())
                        silent_streak = silent_streak + 1 if level < config.VAD_START_THRESHOLD else 0
                        if silent_streak >= silence_limit or len(captured) >= max_frames:
                            break
        except sd.PortAudioError as e:
            log.warning("command recording stream error: %s", e)
            return None
        return np.concatenate(captured) if captured else None


class WakeWordListener:
    """Blocks in wait_for_wake() until the wake word is heard."""

    def __init__(self, transcriber: Transcriber):
        self.transcriber = transcriber
        self.engine = self._pick_engine()
        log.info("wake word engine: %s", self.engine)

    def _pick_engine(self) -> str:
        if config.WAKE_ENGINE == "whisper":
            return "whisper"
        if config.PICOVOICE_ACCESS_KEY:
            try:
                import pvporcupine  # noqa: F401
                return "porcupine"
            except ImportError:
                log.warning("pvporcupine not installed; using whisper polling")
        elif config.WAKE_ENGINE == "porcupine":
            log.warning("PICOVOICE_ACCESS_KEY not set; using whisper polling")
        return "whisper"

    def wait_for_wake(self) -> None:
        """Returns when 'Jarvis' is heard. Recovers from audio device errors."""
        backoff = 1.0
        while True:
            try:
                if self.engine == "porcupine":
                    self._wait_porcupine()
                else:
                    self._wait_whisper_polling()
                return
            except sd.PortAudioError as e:
                # PaErrorCode -9986 class of failures: device contention.
                # Wait and reopen instead of crashing the backend.
                log.warning("wake loop audio error (%s); retrying in %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------ porcupine
    def _wait_porcupine(self) -> None:
        import pvporcupine
        porcupine = pvporcupine.create(
            access_key=config.PICOVOICE_ACCESS_KEY, keywords=["jarvis"]
        )
        try:
            with sd.InputStream(samplerate=porcupine.sample_rate, channels=1,
                                dtype="int16", blocksize=porcupine.frame_length) as stream:
                while True:
                    frame, _ = stream.read(porcupine.frame_length)
                    if porcupine.process(frame.reshape(-1)) >= 0:
                        log.info("wake word detected (porcupine)")
                        return
        finally:
            porcupine.delete()

    # ------------------------------------------------------------ whisper polling
    def _wait_whisper_polling(self) -> None:
        clip_frames = int(config.WAKE_CLIP_SECONDS * config.AUDIO_SAMPLE_RATE)
        while True:
            clip = sd.rec(clip_frames, samplerate=config.AUDIO_SAMPLE_RATE,
                          channels=1, dtype="float32")
            sd.wait()
            clip = clip.reshape(-1)
            level = _rms(clip)
            if level < config.WAKE_RMS_GATE:
                wake_log.debug("silence (rms %.4f), skipping transcription", level)
                continue
            text, avg_logprob, no_speech = self.transcriber.transcribe_with_confidence(clip)
            words = text.lower().split()
            wake_log.debug("heard %r (logprob %.2f, no_speech %.2f)", text, avg_logprob, no_speech)
            # ambient-noise filters: enough words, confident, probably speech
            if (len(words) < config.WAKE_MIN_WORDS
                    or no_speech > config.WAKE_MAX_NO_SPEECH_PROB
                    or avg_logprob < config.WAKE_MIN_AVG_LOGPROB):
                continue
            cleaned = {w.strip(".,!?'\"") for w in words}
            if cleaned & set(config.WAKE_VARIANTS):
                log.info("wake word detected in %r", text)
                return
