"""
Microphone capture for commands.

Deliberately free of heavy imports: the engine process records the user's
first command while jarvis.py is still loading its models, so this module has
to be usable within milliseconds of the wake word.
"""

import os

import numpy as np
import speech_recognition as sr

# Do not change these without testing — they trade latency against clipped
# speech and false starts.
ENERGY_THRESHOLD = 200     # floor for a very quiet room; raised to suit the actual room
NOISE_MULTIPLIER = 2.5     # first command after the wake word: this many times the room's noise
# Follow-ups need a higher bar. They arrive without a wake word, so in a room
# with a TV or other people talking, background voices would otherwise keep
# the conversation going forever. Raise it if that still happens; lower it if
# Jarvis misses follow-ups spoken at normal volume.
FOLLOW_UP_MULTIPLIER = 4.0   # default; JARVIS_FOLLOW_UP_LOUDNESS in .env overrides it
MIN_SPEECH_SECONDS = 0.3   # less than this, by Silero VAD, and a recording is noise

NOISE = "noise"            # record_command's answer for "something, but not speech"
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
_noise_floor = None


def set_noise_floor(rms):
    """
    Fit the speech threshold to the room. The wake listener measures the
    median loudness of the last 30s of mic frames and passes it in on every wake.

    With a fixed threshold of 200 in a room whose background noise sits at
    ~270, 87% of silence counted as speech: recording never stopped until the
    45s cap, Whisper transcribed the noise, and the conversation never ended.
    """
    global _noise_floor
    _noise_floor = float(rms) if rms else None
    print(f"Room noise {rms or 'unknown'}: speech threshold {_threshold(False):.0f}, "
          f"follow-ups {_threshold(True):.0f}")


def _threshold(follow_up):
    if _noise_floor is None:
        return ENERGY_THRESHOLD
    # Read at call time: this module is imported before jarvis.py loads .env
    follow_up_multiplier = float(os.getenv("JARVIS_FOLLOW_UP_LOUDNESS", FOLLOW_UP_MULTIPLIER))
    multiplier = follow_up_multiplier if follow_up else NOISE_MULTIPLIER
    return max(ENERGY_THRESHOLD, multiplier * _noise_floor)


def calibrate():
    """
    Measure the room directly, for when there's no wake listener to ask —
    `python jarvis.py` run on its own. Costs half a second of listening.
    """
    with sr.Microphone() as source:
        _recognizer.adjust_for_ambient_noise(source, duration=0.5)
        # adjust_for_ambient_noise sets 1.5x the average energy; use our margin
        noise = _recognizer.energy_threshold / _recognizer.dynamic_energy_ratio
    set_noise_floor(noise)


def record_command(timeout=LISTEN_TIMEOUT, follow_up=False):
    """
    Record one utterance and return it as 16 kHz float32 samples ready for Whisper.
    follow_up=True applies the louder bar for turns that come without a wake word.

    Returns None if nobody starts speaking within `timeout` seconds. Callers
    treat that as the user having finished, not as an error — this used to be
    an uncaught WaitTimeoutError that killed the whole process. Returns NOISE
    when something loud was recorded that the speech detector says isn't speech.
    """
    _recognizer.energy_threshold = _threshold(follow_up)
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
    samples = np.frombuffer(raw, dtype=np.int16)
    # Loud enough isn't the same as speech. Imported here, not at the top, so
    # the boot-time recording in the engine isn't held up by onnxruntime.
    from wake_word import speech_seconds
    heard = speech_seconds(samples)
    if heard < MIN_SPEECH_SECONDS:
        print(f"Recording had {heard:.2f}s of speech — treating it as noise")
        return NOISE
    return samples.astype(np.float32) / 32768.0
