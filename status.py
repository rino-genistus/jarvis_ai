"""
Is Jarvis working? One command that checks every link in the chain.

    .venv/bin/python status.py

Reads logs/status.json, which the wake listener rewrites every minute, and
checks the pieces it can't report itself: that its process is alive, that the
heartbeat is recent, that Ollama is up with the models, and whether the log
shows a recent crash. Clicking the Jarvis icon while it's running shows the
short version of the same report as a notification.

Standard library only, so it runs even when the environment is broken.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(HERE, "logs", "status.json")
LOG_FILE = os.path.join(HERE, "logs", "jarvis.log")
REQUIRED_MODELS = ("qwen2.5:7b", "llama3.2:1b", "nomic-embed-text:latest")
WAKE_THRESHOLD = float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.5"))


def _age(stamp):
    """Seconds since a 'YYYY-mm-dd HH:MM:SS' stamp, or None."""
    if not stamp:
        return None
    return (datetime.now() - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).total_seconds()


def _ago(seconds):
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    return f"{seconds / 3600:.1f} h ago"


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def _ollama_models():
    """Installed model names, or None if Ollama isn't answering."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            return {m["name"] for m in json.load(r).get("models", [])}
    except Exception:
        return None


def _recent_crash():
    """The last engine crash or traceback in the log's final stretch, if any."""
    try:
        with open(LOG_FILE, errors="replace") as f:
            lines = f.readlines()[-300:]
    except OSError:
        return None
    for line in reversed(lines):
        if "Engine exited unexpectedly" in line or line.startswith("Traceback"):
            return line.strip()
        if "Listening for 'Hey Jarvis'" in line:
            return None     # anything older predates the current run
    return None


def checks():
    """
    [(level, message)] with level 'ok', 'warn' or 'fail', in the order a
    problem should be looked for.
    """
    results = []
    try:
        with open(STATUS_FILE) as f:
            status = json.load(f)
    except (OSError, json.JSONDecodeError):
        status = None

    if not status or not _pid_alive(status.get("pid")):
        results.append(("fail", "Jarvis isn't running — open it from Launchpad or Spotlight"))
        return results
    results.append(("ok", f"Listener running (pid {status['pid']}, since {status['started'][11:16]})"))

    beat = _age(status.get("heartbeat"))
    started = _age(status.get("started"))
    if beat is None and started is not None and started < 90:
        results.append(("ok", "Just started — first mic check within a minute"))
    elif beat is None or beat > 180:
        if status.get("state") == "conversation":
            results.append(("ok", "In a conversation right now"))
        else:
            results.append(("fail", f"No heartbeat for {_ago(beat)} — the listener may be stuck; restart it"))
    else:
        results.append(("ok", f"Heartbeat {_ago(beat)}"))

    if status.get("mic_silent"):
        results.append(("fail", "Microphone is silent — allow Jarvis in System Settings → "
                                "Privacy & Security → Microphone, then restart it"))
    elif status.get("mic_peak_dbfs") is not None:
        level = status["mic_peak_dbfs"]
        quiet = level < -50
        results.append(("warn" if quiet else "ok",
                        f"Microphone level {level} dBFS in the last minute"
                        + (" — very quiet; check the input device in Sound settings" if quiet else "")))

    score = status.get("best_wake_score")
    if score is not None:
        results.append(("ok", f"Best wake score last minute {score:.2f} (triggers at {WAKE_THRESHOLD})"))

    results.append(("ok", f"Last wake {_ago(_age(status.get('last_wake')))}, "
                          f"{status.get('wakes_today', 0)} today; engine {status.get('engine', 'off')}"))

    models = _ollama_models()
    if models is None:
        results.append(("fail", "Ollama isn't running — open the Ollama app"))
    else:
        missing = [m for m in REQUIRED_MODELS if m not in models]
        if missing:
            results.append(("fail", f"Ollama is missing {', '.join(missing)} — run: ollama pull {missing[0]}"))
        else:
            results.append(("ok", "Ollama running with the models"))

    crash = _recent_crash()
    if crash:
        results.append(("fail", f"Recent crash in logs/jarvis.log: {crash[:120]}"))

    for item in status.get("services_off") or []:
        results.append(("warn", f"Off: {item}"))
    return results


def summary():
    """One line for a notification: the worst problem, or all clear."""
    results = checks()
    for level in ("fail", "warn"):
        for lvl, message in results:
            if lvl == level:
                return f"{'Problem' if level == 'fail' else 'Note'}: {message}"
    return 'Listening and healthy. Say "Hey Jarvis".'


def _applescript_string(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dialog():
    """
    The report as a macOS dialog, with buttons to restart Jarvis or open the
    log — what the "Jarvis Status" app shows when clicked.
    """
    import subprocess
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    results = checks()
    healthy = not any(level == "fail" for level, _ in results)
    body = "\n".join(f"{marks[level]}  {message}" for level, message in results)
    title = "Jarvis is working" if healthy else "Jarvis needs attention"
    # "activate" first, or the dialog from a background app opens behind other windows
    script = (f"activate\ndisplay dialog {_applescript_string(body)} with title {_applescript_string(title)} "
              f'buttons {{"Open Log", "Restart Jarvis", "OK"}} default button "OK"')
    choice = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout
    if "Restart Jarvis" in choice:
        subprocess.run(["pkill", "-f", "wake_listener.py"])
        for _ in range(30):     # the listener gives a mid-conversation engine 5s to stop
            if subprocess.run(["pgrep", "-f", "wake_listener.py"], capture_output=True).returncode:
                break
            time.sleep(0.5)
        subprocess.run(["open", "-a", "Jarvis"])
    elif "Open Log" in choice:
        subprocess.run(["open", "-a", "Console", LOG_FILE])


def main():
    if "--dialog" in sys.argv:
        dialog()
        return 0
    marks = {"ok": "\033[32m✓\033[0m", "warn": "\033[33m!\033[0m", "fail": "\033[31m✗\033[0m"}
    results = checks()
    print(f"Jarvis status — {time.strftime('%H:%M:%S')}")
    for level, message in results:
        print(f"  {marks[level]} {message}")
    return 1 if any(level == "fail" for level, _ in results) else 0


if __name__ == "__main__":
    sys.exit(main())
