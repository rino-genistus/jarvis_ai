"""
Microphone capture for commands.

Deliberately free of heavy imports: the engine process records the user's
first command while jarvis.py is still loading its models, so this module has
to be usable within milliseconds of the wake word.
"""

import numpy as np
import speech_recognition as sr

# Do not change these without testing — they trade latency against clipped
# speech and false starts.
ENERGY_THRESHOLD = 200     # intentionally low, tuned for a quiet room
PAUSE_THRESHOLD = 0.8      # was 1.5 — that much dead air is felt directly as latency
PHRASE_TIME_LIMIT = 45     # longest single utterance, in seconds
LISTEN_TIMEOUT = 10        # silence before a conversation is considered over

WHISPER_RATE = 16000       # Whisper expects 16 kHz mono

_recognizer = sr.Recognizer()
_recognizer.energy_threshold = ENERGY_THRESHOLD
_recognizer.dynamic_energy_threshold = False
_recognizer.pause_threshold = PAUSE_THRESHOLD
_recognizer.phrase_threshold = 0.1
_recognizer.non_speaking_duration = 0.8


def record_command(timeout=LISTEN_TIMEOUT):
    """
    Record one utterance and return it as 16 kHz float32 samples ready for Whisper.

    Returns None if nobody starts speaking within `timeout` seconds. Callers
    treat that as the user having finished, not as an error — this used to be
    an uncaught WaitTimeoutError that killed the whole process.
    """
    with sr.Microphone() as source:
        print("User Talks Now")
        try:
            audio = _recognizer.listen(source, timeout=timeout,
                                       phrase_time_limit=PHRASE_TIME_LIMIT)
        except sr.WaitTimeoutError:
            return None
    # Handing Whisper an array rather than a WAV file skips the temp file and
    # the ffmpeg dependency mlx_whisper needs to decode one.
    raw = audio.get_raw_data(convert_rate=WHISPER_RATE, convert_width=2)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
