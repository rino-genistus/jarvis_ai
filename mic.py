"""
Microphone capture for commands.

Deliberately free of heavy imports: the engine process records the user's
first command while jarvis.py is still loading its models, so this module has
to be usable within milliseconds of the wake word.
"""

import os
import queue

import numpy as np
import sounddevice as sd

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
PHRASE_TIME_LIMIT = 45     # longest single utterance, in seconds
LISTEN_TIMEOUT = 10        # silence before a conversation is considered over

WHISPER_RATE = 16000       # the speech models expect 16 kHz mono
CHUNK = 640                # 40 ms, the speech detector's step (wake_word.VAD_CHUNK)
CHUNK_SECONDS = CHUNK / WHISPER_RATE

# End of speech is decided by the speech detector as well as loudness. The
# old recorder waited for 0.8 s below an energy threshold, and loudness fades
# slowly, so it ended about a second after the last word; this ends about
# END_SILENCE after it (default 0.6 s, JARVIS_END_SILENCE overrides it).
# Raise it if Jarvis cuts you off mid-sentence.
END_SILENCE = 0.6
START_CHUNKS = 3           # 3 of the last 4 chunks (120 of 160 ms) of loud speech start a recording
START_PROBABILITY = 0.5    # speech probability to start
KEEP_PROBABILITY = 0.35    # lower bar to keep going, so soft word endings aren't cut
KEEP_LOUDNESS = 0.5        # ...and at least half the start loudness, so a TV doesn't
PRE_ROLL = 0.3             # seconds kept from before the start, so the first word isn't clipped
TAIL = 0.15                # seconds of silence left on the end for the transcriber

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
    audio = sd.rec(int(0.5 * WHISPER_RATE), samplerate=WHISPER_RATE, channels=1, dtype="int16")
    sd.wait()
    set_noise_floor(_rms(audio[:, 0]))


def _rms(chunk):
    return float(np.sqrt(np.mean(np.asarray(chunk, dtype=np.float32) ** 2)))


def end_silence():
    # Read at call time: this module is imported before jarvis.py loads .env
    return float(os.getenv("JARVIS_END_SILENCE", END_SILENCE))


def endpoint(chunks, threshold, timeout=LISTEN_TIMEOUT, detector=None):
    """
    Decide where one utterance starts and ends in a stream of CHUNK-sized
    int16 chunks. Pure apart from the speech detector, so tests can feed it
    synthetic audio. Returns int16 samples, None if nobody started speaking
    within `timeout` seconds, or NOISE if what was recorded wasn't speech.

    Starts when START_CHUNKS of the last START_CHUNKS + 1 chunks are both
    speech and louder than `threshold`. A chunk keeps it going if it's that
    loud, or if the detector hears speech at KEEP_LOUDNESS of it. Ends after
    end_silence() seconds of neither, so quieter voices in the room don't
    hold the recording open.

    Silero is less sure of some voices than others: on synthetic speech it
    dips to 0.3 inside words, which cut "remind me to call ... mom" short
    when every chunk had to pass it.
    """
    if detector is None:
        from wake_word import SpeechDetector   # onnxruntime: imported only when recording
        detector = SpeechDetector()
    pre_roll = max(1, int(PRE_ROLL / CHUNK_SECONDS))
    silence_needed = max(1, round(end_silence() / CHUNK_SECONDS))
    waited = quiet = speech = 0
    recent = []
    recorded = []
    started = False
    for chunk in chunks:
        p = detector.probability(chunk)
        loud = _rms(chunk)
        if not started:
            recorded = (recorded + [chunk])[-(pre_roll + START_CHUNKS + 1):]
            recent = (recent + [p >= START_PROBABILITY and loud >= threshold])[-(START_CHUNKS + 1):]
            if sum(recent) >= START_CHUNKS:
                started, speech = True, sum(recent)
            else:
                waited += 1
                if waited * CHUNK_SECONDS >= timeout:
                    return None
            continue
        recorded.append(chunk)
        voiced = p >= KEEP_PROBABILITY and loud >= threshold * KEEP_LOUDNESS
        if voiced or loud >= threshold:
            quiet = 0
            speech += voiced
        else:
            quiet += 1
        if quiet >= silence_needed or len(recorded) * CHUNK_SECONDS >= PHRASE_TIME_LIMIT:
            break
    if not started:
        return None
    # Keep a little of the silence; the rest is only more for the transcriber to read
    tail = int(TAIL / CHUNK_SECONDS)
    if quiet > tail:
        recorded = recorded[:len(recorded) - (quiet - tail)]
    if speech * CHUNK_SECONDS < MIN_SPEECH_SECONDS:
        return NOISE
    return np.concatenate(recorded)


def record_command(timeout=LISTEN_TIMEOUT, follow_up=False):
    """
    Record one utterance and return it as 16 kHz float32 samples ready for
    the transcriber. follow_up=True applies the louder bar for turns that
    come without a wake word.

    Returns None if nobody starts speaking within `timeout` seconds. Callers
    treat that as the user having finished, not as an error. Returns NOISE
    when something loud was recorded that the speech detector says isn't speech.
    """
    threshold = _threshold(follow_up)
    chunks = queue.Queue()

    def _callback(indata, frames, time_info, status):
        chunks.put(indata[:, 0].copy())

    def _stream():
        while True:
            yield chunks.get()

    # The stream opens first and buffers, so nothing is lost while the speech
    # detector loads on the first recording
    with sd.InputStream(samplerate=WHISPER_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK, callback=_callback):
        print("User Talks Now")
        result = endpoint(_stream(), threshold, timeout)
    if result is None or result is NOISE:
        if result is NOISE:
            print("Recording wasn't speech — treating it as noise")
        return result
    return result.astype(np.float32) / 32768.0
