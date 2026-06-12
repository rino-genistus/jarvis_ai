"""
Text-to-speech for Jarvis.

Default engine is Kokoro-82M running locally. ElevenLabs support is kept wired
(set JARVIS_TTS_ENGINE=elevenlabs) but OFF by default — it costs money.

Quality work happens before synthesis:

- `normalize_for_speech` expands the things Kokoro mispronounces — dates,
  acronyms, abbreviations, symbols — into their spoken forms.
- Pacing: short acknowledgements are spoken a touch faster, long explanations
  slightly slower, via Kokoro's speed parameter.
- Chunk streaming: Kokoro yields audio per text segment; each chunk goes to
  the speaker while the next is still synthesising, cutting time-to-first-word.

Everything routes through `TTSEngine.speak()` which guards empty input,
ducks Spotify, arms the interrupt monitor, and chimes when the floor is open.
"""

import re
import threading

import numpy as np

import config
from audio_io import AudioPlayer, InterruptMonitor, SpotifyDucker
from logging_setup import get_logger

log = get_logger("jarvis.tts")

# ---------------------------------------------------------------------------
# pronunciation pre-processing
# ---------------------------------------------------------------------------

_ABBREVIATIONS = {
    r"\bDr\.": "Doctor", r"\bMr\.": "Mister", r"\bMrs\.": "Missus", r"\bMs\.": "Miz",
    r"\bProf\.": "Professor", r"\bSt\.": "Street", r"\bAve\.": "Avenue",
    r"\bBlvd\.": "Boulevard", r"\bRd\.": "Road", r"\bApt\.": "Apartment",
    r"\betc\.": "et cetera", r"\be\.g\.": "for example", r"\bi\.e\.": "that is",
    r"\bvs\b\.?": "versus", r"\bapprox\.": "approximately", r"\bdept\.": "department",
    r"\bft\b": "feet", r"\bhrs\b": "hours", r"\bmins\b": "minutes",
}

# acronyms that are pronounced as words — leave them alone
_PRONOUNCEABLE = {
    "NASA", "ASAP", "LASER", "RADAR", "SCUBA", "GIF", "RAM", "JSON", "YOLO",
    "POTUS", "NATO", "FOMO", "AWOL", "LOL", "OK", "JARVIS", "SIRI", "WIFI",
    "AM", "PM",
}

_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth",
             9: "ninth", 12: "twelfth", 20: "twentieth", 30: "thirtieth"}
_MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _two_digit_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + ("-" + _ONES[ones] if ones else "")


def _day_ordinal(day: int) -> str:
    if day in _ORDINALS:
        return _ORDINALS[day]
    if day < 20:
        return _ONES[day] + "th"
    tens, ones = divmod(day, 10)
    if ones == 0:
        return _ORDINALS.get(day, _TENS[tens][:-1] + "ieth")
    return _TENS[tens] + "-" + _day_ordinal(ones)


def _year_words(year: int) -> str:
    if 2000 <= year < 2010:
        return "two thousand" + (" " + _ONES[year - 2000] if year > 2000 else "")
    if 1000 <= year < 10000:
        high, low = divmod(year, 100)
        if low == 0:
            return _two_digit_words(high) + " hundred"
        low_words = ("oh " + _ONES[low]) if low < 10 else _two_digit_words(low)
        return _two_digit_words(high) + " " + low_words
    return str(year)


def _spoken_date(year: int, month: int, day: int) -> str:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return f"{year}-{month:02d}-{day:02d}"
    return f"{_MONTHS[month]} {_day_ordinal(day)}, {_year_words(year)}"


def _expand_iso_date(m: re.Match) -> str:
    return _spoken_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _expand_us_date(m: re.Match) -> str:
    return _spoken_date(int(m.group(3)), int(m.group(1)), int(m.group(2)))


def _expand_24h_time(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return m.group(0)
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {suffix}" if minute else f"{hour12} {suffix}"


def _expand_acronym(m: re.Match) -> str:
    word = m.group(1)
    if word in _PRONOUNCEABLE:
        return m.group(0)
    spaced = " ".join(word)
    if m.group(2):  # plural: "APIs" -> "A P I s"
        spaced += " s"
    return spaced


def normalize_for_speech(text: str) -> str:
    """Expand text into the form Kokoro pronounces correctly."""
    # markdown that slips through despite the prompt
    text = re.sub(r"[*_`#]+", "", text)
    # URLs: drop protocol, speak dots
    text = re.sub(r"https?://(www\.)?", "", text)
    text = re.sub(r"(\w)\.(com|org|net|io|ai|dev|edu|gov)\b", r"\1 dot \2", text)
    # dates before times (ISO dates contain colons nowhere, but order is cheap insurance)
    text = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", _expand_iso_date, text)
    text = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _expand_us_date, text)
    # bare 24h clock times like 14:30 (leave 12h-with-AM/PM forms alone)
    text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*[APap]\.?[Mm])", _expand_24h_time, text)
    # symbols
    text = re.sub(r"°\s*F\b", " degrees Fahrenheit", text)
    text = re.sub(r"°\s*C\b", " degrees Celsius", text)
    text = text.replace("°", " degrees")
    text = re.sub(r"(\d)\s*%", r"\1 percent", text)
    text = text.replace("&", " and ")
    # abbreviations
    for pattern, spoken in _ABBREVIATIONS.items():
        text = re.sub(pattern, spoken, text)
    # acronyms: 2-6 capital letters, optional plural s
    text = re.sub(r"\b([A-Z]{2,6})(s)?\b", _expand_acronym, text)
    return re.sub(r"\s{2,}", " ", text).strip()


def pacing_speed(text: str) -> float:
    """Short acknowledgements faster, long explanations measured."""
    n = len(text)
    if n < 60:
        return 1.15
    if n > 250:
        return 0.95
    return 1.05


# ---------------------------------------------------------------------------
# engines
# ---------------------------------------------------------------------------

class KokoroEngine:
    """Local Kokoro-82M, loaded in a background thread on startup."""

    def __init__(self):
        self._pipeline = None
        self.ready = threading.Event()
        threading.Thread(target=self._load, name="kokoro-load", daemon=True).start()

    def _load(self) -> None:
        try:
            from kokoro import KPipeline
            self._pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
            log.info("Kokoro loaded")
        except Exception as e:  # noqa: BLE001
            log.error("Kokoro failed to load: %s", e)
        finally:
            self.ready.set()

    def synthesize(self, text: str, speed: float):
        """Yield audio chunks as Kokoro produces them (for streamed playback)."""
        self.ready.wait()
        if self._pipeline is None:
            return
        for _, _, audio in self._pipeline(text, voice=config.KOKORO_VOICE, speed=speed):
            yield np.asarray(audio, dtype=np.float32)


class ElevenLabsEngine:
    """
    Paid path, kept for final release. Requests raw PCM so playback stays
    interruptible through the shared AudioPlayer (the SDK's own play() blocks).
    """

    SAMPLE_RATE = 24000

    def __init__(self):
        import os
        from elevenlabs.client import ElevenLabs
        self._client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

    def synthesize(self, text: str, speed: float):
        stream = self._client.text_to_speech.convert(
            text=text,
            voice_id=config.ELEVENLABS_VOICE_ID,
            model_id=config.ELEVENLABS_TTS_MODEL,
            output_format="pcm_24000",
        )
        for chunk in stream:
            if chunk:
                yield np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# facade
# ---------------------------------------------------------------------------

class TTSEngine:
    """speak() = normalize -> duck Spotify -> arm interrupts -> stream -> chime."""

    def __init__(self, player: AudioPlayer, spotify_agent=None):
        self.player = player
        self.monitor = InterruptMonitor(player)
        self.spotify_agent = spotify_agent
        self.kokoro = KokoroEngine()
        self._eleven = None

    def wait_ready(self) -> None:
        self.kokoro.ready.wait()

    def _engine(self):
        if config.TTS_ENGINE == "elevenlabs":
            if self._eleven is None:
                try:
                    self._eleven = ElevenLabsEngine()
                except Exception as e:  # noqa: BLE001
                    log.error("ElevenLabs unavailable, falling back to Kokoro: %s", e)
                    self._eleven = False
            if self._eleven:
                return self._eleven, ElevenLabsEngine.SAMPLE_RATE
        return self.kokoro, config.KOKORO_SAMPLE_RATE

    def speak(self, text: str, chime: bool = True) -> bool:
        """
        Speak `text` aloud. Returns False if the user interrupted.
        Safe with empty/None input (the old safe_speak guarantee).
        """
        if not text or not text.strip():
            log.debug("empty response, skipping TTS")
            return True
        spoken = normalize_for_speech(text)
        engine, samplerate = self._engine()
        self.player.begin_utterance()
        try:
            with SpotifyDucker(self.spotify_agent), self.monitor:
                completed = self.player.play_stream(
                    engine.synthesize(spoken, pacing_speed(spoken)), samplerate
                )
        except Exception as e:  # noqa: BLE001
            log.error("TTS failed: %s", e)
            return True
        if completed and chime:
            self.player.play_chime()  # the floor is open — user may speak
        return completed
