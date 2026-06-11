"""
Jarvis menu bar status icon (rumps).

Mirrors the pill's state (idle / listening / thinking / speaking) as a glyph in
the macOS menu bar, so Jarvis is visibly alive even when the pill is hidden or
the user is in a full-screen app. Runs as its own small process and connects
to the backend's UI bridge like any other client.

    python3 jarvis_tray.py
"""

import threading
import time

import rumps

from ui_bridge import BridgeClient

GLYPHS = {
    "idle": "◇",
    "listening": "◉",
    "recording": "◉",
    "thinking": "✦",
    "tool": "⚙",
    "speaking": "▶",
    "error": "▲",
    "offline": "◌",
}


class JarvisTray(rumps.App):
    def __init__(self):
        super().__init__("Jarvis", title=GLYPHS["offline"], quit_button="Quit Jarvis Tray")
        self._state = "offline"
        self._last_event = 0.0
        self._lock = threading.Lock()
        self.status_item = rumps.MenuItem("Status: starting…")
        self.menu = [self.status_item]
        # rumps wants UI mutations on the main thread; poll a shared var instead
        rumps.Timer(self._refresh, 0.5).start()
        client = BridgeClient(self._on_event)
        client.start()

    def _on_event(self, event: dict):
        if event.get("type") == "state":
            with self._lock:
                self._state = event.get("value", "idle")
                self._last_event = time.time()

    def _refresh(self, _timer):
        with self._lock:
            state = self._state
            stale = self._last_event and time.time() - self._last_event > 600
        if state != "offline" and stale and state == "idle":
            pass  # idle for a long time is still fine — backend pushes no idle heartbeats
        self.title = GLYPHS.get(state, GLYPHS["idle"])
        self.status_item.title = f"Status: {state}"


if __name__ == "__main__":
    JarvisTray().run()
