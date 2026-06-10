"""
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
