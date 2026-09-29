"""
Lean "Hey Jarvis" detector.

Runs openWakeWord's pretrained ONNX models directly with onnxruntime. Importing
the openwakeword package itself drags in scipy and scikit-learn for its
training utilities — about 75 MB in a process that is always running and only
ever does inference. This is a port of its streaming path for 80 ms frames,
checked to produce the same scores.

Pipeline per 80 ms frame:
    audio → melspectrogram model → embedding model (last 76 mel frames)
          → wake word model (last 16 embeddings) → score 0..1
    Silero VAD runs alongside; a score only counts if there was speech.
"""

import importlib.util
import os
import subprocess
import sys
from collections import deque

import numpy as np
import onnxruntime as ort

FRAME = 1280                 # 80 ms at 16 kHz
MEL_CONTEXT = 160 * 3        # extra samples the mel model needs around a frame
MEL_WINDOW = 76              # mel frames per embedding
MEL_KEEP = 970               # ~10 s of mel history, as openWakeWord keeps
FEATURE_KEEP = 120           # ~10 s of embedding history
WARMUP_PREDICTIONS = 5       # openWakeWord zeroes the first few scores
VAD_CHUNK = 640

WAKE_MODEL_FILE = "hey_jarvis_v0.1.onnx"
SUPPORT_FILES = ("melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx")


def models_dir():
    # find_spec locates the package without executing its heavy __init__
    spec = importlib.util.find_spec("openwakeword")
    return os.path.join(spec.submodule_search_locations[0], "resources", "models")


def ensure_models():
    """
    Download the models on first run. Done in a throwaway subprocess so the
    heavy openwakeword import never lands in the long-running listener.
    """
    folder = models_dir()
    if all(os.path.exists(os.path.join(folder, f)) for f in (WAKE_MODEL_FILE,) + SUPPORT_FILES):
        return
    print("Downloading wake word models (first run only)", flush=True)
    subprocess.run([sys.executable, "-c",
                    "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"],
                   check=True)


def _session(filename):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False     # smaller resident set for tiny models
    return ort.InferenceSession(os.path.join(models_dir(), filename), sess_options=options,
                                providers=["CPUExecutionProvider"])


_vad_session = None


def speech_seconds(samples, threshold=0.5):
    """
    How many seconds of an utterance Silero VAD hears as speech. Takes 16 kHz
    audio, float32 in [-1, 1] or int16.

    The engine checks every recording with this before transcribing. In a
    room with fan or traffic noise the energy threshold still lets bursts
    through, and Whisper turns them into confident nonsense ("When my plane
    gets here") that Jarvis would otherwise answer.
    """
    global _vad_session
    if _vad_session is None:
        ensure_models()
        _vad_session = _session("silero_vad.onnx")
    audio = np.asarray(samples)
    if audio.dtype != np.int16:
        audio = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    h = np.zeros((2, 1, 64), dtype=np.float32)
    c = np.zeros((2, 1, 64), dtype=np.float32)
    speech_chunks = 0
    for i in range(0, len(audio) - VAD_CHUNK + 1, VAD_CHUNK):
        chunk = (audio[i:i + VAD_CHUNK] / 32767).astype(np.float32)
        out, h, c = _vad_session.run(None, {"input": chunk[None], "h": h, "c": c,
                                            "sr": np.array(16000, dtype=np.int64)})
        speech_chunks += out[0][0] >= threshold
    return speech_chunks * VAD_CHUNK / 16000


class WakeWordDetector:

    def __init__(self, vad_threshold=0.5):
        ensure_models()
        self.vad_threshold = vad_threshold
        self._mel = _session("melspectrogram.onnx")
        self._embed = _session("embedding_model.onnx")
        self._vad = _session("silero_vad.onnx")
        self._wake = _session(WAKE_MODEL_FILE)
        self._wake_input = self._wake.get_inputs()[0].name
        self._wake_window = self._wake.get_inputs()[0].shape[1]
        self.reset()

    def reset(self):
        """Clear all buffers — call after each detection or it retriggers."""
        self._raw = np.zeros(0, dtype=np.int16)
        self._mels = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        self._features = self._noise_features()
        self._predictions = 0
        self._vad_h = np.zeros((2, 1, 64), dtype=np.float32)
        self._vad_c = np.zeros((2, 1, 64), dtype=np.float32)
        self._vad_scores = deque(maxlen=125)

    def _melspectrogram(self, samples):
        spec = self._mel.run(None, {"input": samples[None].astype(np.float32)})[0]
        return np.squeeze(spec) / 10 + 2    # openWakeWord's match to the original TF features

    def _noise_features(self):
        # openWakeWord seeds its history with embeddings of low noise rather than
        # zeros; the wake model was trained against that starting state.
        noise = np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16)
        spec = self._melspectrogram(noise)
        windows = [spec[i:i + MEL_WINDOW] for i in range(0, spec.shape[0], 8)
                   if spec[i:i + MEL_WINDOW].shape[0] == MEL_WINDOW]
        batch = np.array(windows, dtype=np.float32)[..., None]
        return self._embed.run(None, {"input_1": batch})[0].squeeze()

    def _speech_probability(self, frame):
        scores = []
        for i in range(0, frame.shape[0], VAD_CHUNK):
            chunk = (frame[i:i + VAD_CHUNK] / 32767).astype(np.float32)
            out, self._vad_h, self._vad_c = self._vad.run(None, {
                "input": chunk[None], "h": self._vad_h, "c": self._vad_c,
                "sr": np.array(16000, dtype=np.int64)})
            scores.append(out[0][0])
        return float(np.mean(scores))

    def score(self, frame):
        """
        Feed one 80 ms frame of 16 kHz int16 audio; returns the wake score, 0..1.
        """
        self._raw = np.concatenate([self._raw, frame])[-(FRAME + MEL_CONTEXT):]
        self._mels = np.vstack([self._mels, self._melspectrogram(self._raw)])[-MEL_KEEP:]

        window = self._mels[-MEL_WINDOW:].astype(np.float32)[None, :, :, None]
        embedding = self._embed.run(None, {"input_1": window})[0].squeeze()
        self._features = np.vstack([self._features, embedding])[-FEATURE_KEEP:]

        features = self._features[-self._wake_window:][None].astype(np.float32)
        score = float(np.squeeze(self._wake.run(None, {self._wake_input: features})[0]))
        if self._predictions < WARMUP_PREDICTIONS:
            score = 0.0
        self._predictions += 1

        if self.vad_threshold > 0:
            self._vad_scores.append(self._speech_probability(frame))
            # Speech from 0.32–0.56 s ago, when "Hey Jarvis" was being said
            recent = list(self._vad_scores)[-7:-4]
            if (max(recent) if recent else 0) < self.vad_threshold:
                score = 0.0
        return score
