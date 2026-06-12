"""
ComputerControlAgent — lets Jarvis drive this Mac: applications and files.

Applications go through `open -a` and System Events AppleScript. File
operations resolve fuzzy spoken names ("open my resume") against a FileIndex
of the home directory.

FileIndex:
- Full walk of ~ on first run (skipping .git, node_modules, Library, ...),
  cached at directory_cache.json so subsequent starts load instantly.
- An FSEvents watcher (via watchdog) marks the index dirty whenever the file
  system changes; after INDEX_REFRESH_DEBOUNCE seconds of quiet, a background
  rebuild runs — so files created after startup don't stay invisible.

Deletion is deliberately non-destructive: files go to the Trash via Finder,
never an unlink.
"""

import difflib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import config
from logging_setup import get_logger

log = get_logger("jarvis.computer")


def _osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


class FileIndex:
    def __init__(self):
        self.files: list[str] = []
        self.built_at: float = 0.0
        self.ready = threading.Event()
        self._dirty_at: float | None = None
        self._rebuilding = threading.Lock()

    # ------------------------------------------------------------ build/load
    def start(self) -> None:
        threading.Thread(target=self._initial_load, name="file-index", daemon=True).start()

    def _initial_load(self) -> None:
        if self._load_cache():
            log.info("file index loaded from cache (%d files)", len(self.files))
        else:
            self._build()
        self.ready.set()
        self._start_watcher()
        threading.Thread(target=self._refresh_loop, name="index-refresh", daemon=True).start()

    def _load_cache(self) -> bool:
        try:
            if config.DIRECTORY_CACHE.exists():
                data = json.loads(config.DIRECTORY_CACHE.read_text())
                self.files = data.get("files", [])
                self.built_at = data.get("built_at", 0.0)
                return bool(self.files)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("directory cache unreadable, rebuilding: %s", e)
        return False

    def _build(self) -> None:
        with self._rebuilding:
            log.info("building file index of %s ...", Path.home())
            started = time.time()
            found: list[str] = []
            for root, dirs, files in os.walk(Path.home()):
                dirs[:] = [d for d in dirs
                           if d not in config.INDEX_SKIP_DIRS and not d.startswith(".")]
                for name in files:
                    if not name.startswith("."):
                        found.append(os.path.join(root, name))
            self.files = found
            self.built_at = time.time()
            try:
                config.DIRECTORY_CACHE.write_text(
                    json.dumps({"built_at": self.built_at, "files": self.files})
                )
            except OSError as e:
                log.warning("could not write directory cache: %s", e)
            log.info("file index built: %d files in %.1fs", len(found), time.time() - started)

    def refresh_index(self) -> None:
        self._build()

    # ------------------------------------------------------------ staleness watcher
    def _start_watcher(self) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            log.warning("watchdog not installed — index will not auto-refresh")
            return

        index = self

        class DirtyMarker(FileSystemEventHandler):
            def on_any_event(self, event):
                path = getattr(event, "src_path", "") or ""
                parts = set(Path(path).parts)
                if parts & config.INDEX_SKIP_DIRS:
                    return
                index._dirty_at = time.time()

        try:
            observer = Observer()
            observer.schedule(DirtyMarker(), str(Path.home()), recursive=True)
            observer.daemon = True
            observer.start()
            log.info("FSEvents watcher active — index auto-refreshes on changes")
        except Exception as e:  # noqa: BLE001
            log.warning("file watcher failed to start: %s", e)

    def _refresh_loop(self) -> None:
        while True:
            time.sleep(15)
            dirty_at = self._dirty_at
            if dirty_at and time.time() - dirty_at > config.INDEX_REFRESH_DEBOUNCE:
                self._dirty_at = None
                log.info("file system changed — refreshing index")
                self._build()

    # ------------------------------------------------------------ search
    def search(self, query: str, limit: int = 5) -> list[str]:
        """Fuzzy-match a spoken name against indexed paths, best first."""
        self.ready.wait(timeout=30)
        q = query.lower().strip()
        if not q:
            return []
        scored: list[tuple[float, str]] = []
        for path in self.files:
            name = os.path.basename(path).lower()
            stem = os.path.splitext(name)[0]
            if stem == q or name == q:
                score = 100.0
            elif q in name:
                score = 80.0 - min(len(name) - len(q), 30) * 0.5
            elif all(token in path.lower() for token in q.split()):
                score = 50.0
            else:
                continue
            scored.append((score, path))
        if not scored:  # fall back to close spelling matches on extension-less names
            by_stem: dict[str, str] = {}
            for path in self.files:
                stem = os.path.splitext(os.path.basename(path))[0].lower()
                by_stem.setdefault(stem, path)
            for match in difflib.get_close_matches(q, by_stem.keys(), n=limit, cutoff=0.75):
                scored.append((40.0, by_stem[match]))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [path for _, path in scored[:limit]]


class ComputerControlAgent:
    """Tools exposed to the LLM for controlling this Mac."""

    def __init__(self, file_index: FileIndex):
        self.index = file_index

    # ------------------------------------------------------------ applications
    def open_application(self, app_name: str):
        """
        Open (launch) a macOS application by name, e.g. 'Safari', 'Spotify', 'Notes'.
        Use when the user asks to open, launch, or start an app.
        """
        result = subprocess.run(["open", "-a", app_name], capture_output=True, text=True)
        if result.returncode != 0:
            return f"Could not find an application called '{app_name}'."
        return f"Opened {app_name}."

    def close_application(self, app_name: str):
        """
        Quit a running macOS application by name.
        Use when the user asks to close, quit, or kill an app.
        """
        _osascript(f'tell application "{app_name}" to quit')
        return f"Closed {app_name}."

    def switch_to_application(self, app_name: str):
        """
        Bring an already-running application to the foreground.
        Use when the user asks to switch to, focus, or go to an app.
        """
        _osascript(f'tell application "{app_name}" to activate')
        return f"Switched to {app_name}."

    def list_open_applications(self):
        """
        List the applications currently running with a visible window.
        Use when the user asks what apps are open or running.
        """
        out = _osascript(
            'tell application "System Events" to get name of (processes where background only is false)'
        )
        return [name.strip() for name in out.split(",") if name.strip()]

    # ------------------------------------------------------------ files
    def open_file(self, file_name: str):
        """
        Find a file anywhere in the user's home directory by (fuzzy) name and
        open it with its default application. Use when the user asks to open,
        show, or pull up a file or document.
        """
        matches = self.index.search(file_name)
        if not matches:
            return f"No file matching '{file_name}' found."
        subprocess.run(["open", matches[0]], capture_output=True)
        others = f" (other matches: {', '.join(matches[1:3])})" if len(matches) > 1 else ""
        return f"Opened {matches[0]}.{others}"

    def create_file(self, file_name: str, directory: str = "Desktop", content: str = ""):
        """
        Create a new file. directory is relative to the home folder
        (e.g. 'Desktop', 'Documents/Notes'); content is optional initial text.
        """
        target_dir = Path.home() / directory
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / file_name
        if target.exists():
            return f"{target} already exists — not overwriting it."
        target.write_text(content, encoding="utf-8")
        self.index.files.append(str(target))
        return f"Created {target}."

    def delete_file(self, file_name: str):
        """
        Move a file to the Trash (recoverable — never permanently deletes).
        Finds the file by fuzzy name in the home directory.
        """
        matches = self.index.search(file_name)
        if not matches:
            return f"No file matching '{file_name}' found."
        if len(matches) > 1 and not file_name.lower() in os.path.basename(matches[0]).lower():
            return ("Multiple files match: " + ", ".join(matches[:3])
                    + ". Ask the user which one before deleting.")
        target = matches[0]
        _osascript(f'tell application "Finder" to delete POSIX file "{target}"')
        if target in self.index.files:
            self.index.files.remove(target)
        return f"Moved {target} to the Trash."

    def move_file(self, file_name: str, destination_directory: str):
        """
        Move a file to another folder. destination_directory is relative to the
        home folder, e.g. 'Documents' or 'Desktop/Archive'.
        """
        matches = self.index.search(file_name)
        if not matches:
            return f"No file matching '{file_name}' found."
        source = Path(matches[0])
        dest_dir = Path.home() / destination_directory
        dest_dir.mkdir(parents=True, exist_ok=True)
        destination = dest_dir / source.name
        if destination.exists():
            return f"{destination} already exists — not overwriting it."
        source.rename(destination)
        if str(source) in self.index.files:
            self.index.files.remove(str(source))
        self.index.files.append(str(destination))
        return f"Moved {source.name} to {dest_dir}."
