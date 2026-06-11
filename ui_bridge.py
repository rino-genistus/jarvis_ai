"""
<<<<<<< HEAD
UI bridge — the socket layer between the Jarvis backend and its UIs.

The backend embeds a `UIBridge` (TCP server on localhost). The PyQt pill and
the menu-bar tray connect as clients and receive newline-delimited JSON events.
Emitting never blocks the voice pipeline: events are queued and a writer thread
fans them out; dead clients are dropped silently.

Event vocabulary (all messages carry "type"):

  {"type": "state",  "value": "idle|listening|recording|thinking|tool|speaking|error"}
  {"type": "transcript", "role": "user"|"jarvis", "text": "..."}
  {"type": "tool",   "name": "search_web", "label": "Searching the web...",
                     "status": "start"|"done"|"error"}
  {"type": "memory", "count": 3}          # memory hits used this turn
  {"type": "error",  "message": "..."}    # surfaced in the UI as a red flash
"""

import json
import queue
import socket
import threading
import time

from config import UI_BRIDGE_HOST, UI_BRIDGE_PORT
from logging_setup import get_logger

log = get_logger("jarvis.bridge")

# Human-readable labels shown in the pill while a tool runs.
TOOL_LABELS = {
    "create_event": "Adding to your calendar...",
    "get_calendar_events": "Reading your calendar...",
    "update_calendar_event": "Updating your calendar...",
    "delete_calendar_event": "Clearing a calendar event...",
    "search_web": "Searching the web...",
    "extract_webpages": "Reading the page...",
    "get_current_weather": "Checking the weather...",
    "get_weather_with_time": "Checking the forecast...",
    "get_daily_forecast": "Pulling the weekly forecast...",
    "get_weather_alerts": "Checking weather alerts...",
    "get_current_track": "Checking Spotify...",
    "search_song_and_queue": "Searching Spotify...",
    "add_song_to_playlist": "Updating your playlist...",
    "create_playlist": "Creating the playlist...",
    "recently_played": "Pulling your listening history...",
    "skip_song": "Skipping the track...",
    "pause_song": "Pausing playback...",
    "shuffle": "Toggling shuffle...",
    "set_volume": "Adjusting the volume...",
    "send_email": "Sending the email...",
    "search_email": "Searching your inbox...",
    "get_unread_emails": "Checking unread mail...",
    "get_email_by_id": "Opening the email...",
    "reply_to_email": "Drafting the reply...",
    "mark_as_read": "Marking as read...",
    "trash_email": "Moving to trash...",
    "untrash_email": "Restoring the email...",
    "get_drafts": "Fetching your drafts...",
    "get_sent_emails": "Checking sent mail...",
    "get_all_labels": "Reading your labels...",
    "get_sender_profile": "Checking the account...",
    "open_application": "Opening the app...",
    "close_application": "Closing the app...",
    "switch_to_application": "Switching apps...",
    "list_open_applications": "Checking open apps...",
    "open_file": "Finding the file...",
    "create_file": "Creating the file...",
    "delete_file": "Moving to Trash...",
    "move_file": "Moving the file...",
}


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, f"Running {name.replace('_', ' ')}...")


class UIBridge:
    """Backend-side event server. Safe to use before/without any UI connected."""

    def __init__(self, host: str = UI_BRIDGE_HOST, port: int = UI_BRIDGE_PORT):
        self.host = host
        self.port = port
        self._clients: list[socket.socket] = []
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=500)
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._accept_loop, name="bridge-accept", daemon=True).start()
        threading.Thread(target=self._writer_loop, name="bridge-writer", daemon=True).start()
        log.info("UI bridge listening on %s:%d", self.host, self.port)

    # -------------------------------------------------- emit helpers
    def emit(self, event_type: str, **payload) -> None:
        event = {"type": event_type, "ts": time.time(), **payload}
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass  # UI lag must never stall the voice pipeline

    def state(self, value: str) -> None:
        self.emit("state", value=value)

    def transcript(self, role: str, text: str) -> None:
        if text and text.strip():
            self.emit("transcript", role=role, text=text.strip())

    def tool(self, name: str, status: str) -> None:
        self.emit("tool", name=name, label=tool_label(name), status=status)

    def memory(self, count: int) -> None:
        if count > 0:
            self.emit("memory", count=count)

    def error(self, message: str) -> None:
        self.emit("error", message=message)
        self.state("error")

    # -------------------------------------------------- internals
    def _accept_loop(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.host, self.port))
        except OSError as e:
            log.error("UI bridge could not bind %s:%d — %s", self.host, self.port, e)
            return
        server.listen(4)
        while self._running:
            try:
                client, addr = server.accept()
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._lock:
                    self._clients.append(client)
                log.info("UI client connected from %s", addr)
            except OSError:
                break

    def _writer_loop(self) -> None:
        while self._running:
            event = self._queue.get()
            data = (json.dumps(event) + "\n").encode("utf-8")
            with self._lock:
                clients = list(self._clients)
            for client in clients:
                try:
                    client.sendall(data)
                except OSError:
                    with self._lock:
                        if client in self._clients:
                            self._clients.remove(client)
                    try:
                        client.close()
                    except OSError:
                        pass


class BridgeClient:
    """UI-side client with auto-reconnect. Calls `on_event(dict)` per event."""

    def __init__(self, on_event, host: str = UI_BRIDGE_HOST, port: int = UI_BRIDGE_PORT):
        self.on_event = on_event
        self.host = host
        self.port = port
        self._running = False

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._run, name="bridge-client", daemon=True).start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            try:
                with socket.create_connection((self.host, self.port), timeout=5) as sock:
                    sock.settimeout(None)
                    buffer = b""
                    while self._running:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if not line.strip():
                                continue
                            try:
                                self.on_event(json.loads(line))
                            except Exception:  # noqa: BLE001 — a bad event must not kill the UI
                                log.debug("bad bridge event: %r", line[:200])
            except OSError:
                pass
            if self._running:
                time.sleep(2.0)  # backend not up yet (or restarting) — retry
=======
Socket server embedded in jarvis.py process.
Call emit() anywhere in jarvis.py to push a JSON event to the UI.
"""
import json
import socket
import threading

HOST = "127.0.0.1"
PORT = 9_999

_clients: list[socket.socket] = []
_lock = threading.Lock()


def _server_thread():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(5)
    while True:
        conn, _ = srv.accept()
        with _lock:
            _clients.append(conn)


def start():
    t = threading.Thread(target=_server_thread, daemon=True)
    t.start()


def emit(event: dict):
    payload = (json.dumps(event) + "\n").encode()
    with _lock:
        dead = []
        for c in _clients:
            try:
                c.sendall(payload)
            except OSError:
                dead.append(c)
        for c in dead:
            _clients.remove(c)
>>>>>>> e2df5b4184e17ec1b4de6e796cafd4e189e5428d
