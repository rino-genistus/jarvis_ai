"""
Structured logging for Jarvis.

Splits the previously flat log stream into levels and destinations:

- console            INFO+   (conversations, tool calls, memory ops)
- logs/jarvis.log    DEBUG+  (everything, incl. ambient RMS / wake polling noise)
- logs/errors.log    WARNING+ (failures only — the file to check when something breaks)

Wake-word polling chatter goes to the "jarvis.wake" logger at DEBUG so it never
clutters the console but is still on disk when latency needs investigating.
"""

import logging
import logging.handlers

from config import LOG_DIR

_CONSOLE_FMT = "%(asctime)s  %(levelname)-7s %(name)-18s %(message)s"
_FILE_FMT = "%(asctime)s  %(levelname)-7s %(name)-18s [%(threadName)s] %(message)s"
_DATE_FMT = "%H:%M:%S"

_configured = False


def setup_logging(console_level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    LOG_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, _DATE_FMT))
    root.addHandler(console)

    full = logging.handlers.RotatingFileHandler(
        LOG_DIR / "jarvis.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    full.setLevel(logging.DEBUG)
    full.setFormatter(logging.Formatter(_FILE_FMT))
    root.addHandler(full)

    errors = logging.handlers.RotatingFileHandler(
        LOG_DIR / "errors.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    errors.setLevel(logging.WARNING)
    errors.setFormatter(logging.Formatter(_FILE_FMT))
    root.addHandler(errors)

    # third-party libraries are far too chatty at DEBUG
    for noisy in ("urllib3", "httpx", "httpcore", "googleapiclient",
                  "spotipy", "pinecone", "watchdog", "numba", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
