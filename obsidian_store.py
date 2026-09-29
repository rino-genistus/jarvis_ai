"""
Human-readable memory in the Obsidian vault — Jarvis's growing knowledge graph.

Pinecone is what Jarvis searches; this is what the user reads and browses.

    Memory/YYYY-MM-DD.md       daily note: one section per session, with a
                               multi-sentence summary, facts learned and
                               [[wikilinks]] to the topics it touched
    Memory/Entities/<Topic>.md one note per person, place, project or interest,
                               gaining a dated line — linked back to the day —
                               every time a session mentions it

Every session adds edges between days and topics, so Obsidian's graph view
connects related memories on its own as the vault grows.
"""

import os
import re
from datetime import datetime
from pathlib import Path

DEFAULT_VAULT = "~/Desktop/Jarvis AI"

# Characters that break file names or wikilinks
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')


def vault_path():
    """Read at call time so a value from .env is picked up after load_dotenv()."""
    return Path(os.path.expanduser(os.getenv("OBSIDIAN_VAULT_PATH", DEFAULT_VAULT)))


def _entities_dir():
    return vault_path() / "Memory" / "Entities"


def _clean_topic(name):
    return re.sub(r"\s+", " ", _UNSAFE.sub(" ", name)).strip()[:80]


def existing_topics():
    """Names of the topic notes already in the vault, so extraction can reuse them."""
    folder = _entities_dir()
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.md"))


def append_session(summary, facts=(), topics=(), title="", when=None):
    """
    Append one session to the day's note, and a line to each topic's note.

    `topics` is a list of {"name", "note"} dicts. Returns the daily note's
    path, or None if there was nothing to write or the vault doesn't exist.
    Never creates the vault itself — a typo in the path should be noticed, not
    silently become a new folder.
    """
    if not summary and not facts:
        return None
    vault = vault_path()
    if not vault.is_dir():
        print(f"Obsidian vault not found at {vault}, skipping daily note")
        return None

    when = when or datetime.now()
    day = f"{when:%Y-%m-%d}"
    memory_dir = vault / "Memory"
    memory_dir.mkdir(exist_ok=True)

    linked = _append_topics(topics, day, when)

    note = memory_dir / f"{day}.md"
    lines = []
    if not note.exists():
        lines += [f"# Memory — {when:%B} {when.day}, {when.year}", ""]
    heading = f"## Session ({when:%H:%M})" + (f" — {title}" if title else "")
    lines += ["", heading]
    if summary:
        lines.append(summary)
    if facts:
        lines += ["", "", "### Facts learned", ""]
        lines += [f"- {fact}" for fact in facts]
    if linked:
        lines += ["", "", "### Topics", ""]
        lines.append(" · ".join(f"[[{name}]]" for name in linked))
    lines.append("")

    with note.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return note


def _append_topics(topics, day, when):
    """
    Add a dated line to each topic's note, creating notes as needed.
    Returns the topic names as linked, matching existing notes regardless of case.
    """
    folder = _entities_dir()
    existing = {name.lower(): name for name in existing_topics()}
    linked = []
    for topic in topics:
        name = _clean_topic(topic.get("name", ""))
        if not name:
            continue
        name = existing.get(name.lower(), name)    # "toronto" joins the existing "Toronto"
        if name in linked:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.md"
        entry = f"- [[{day}]] {when:%H:%M} — {topic.get('note') or 'Mentioned.'}\n"
        with path.open("a", encoding="utf-8") as f:
            if path.stat().st_size == 0:
                f.write(f"# {name}\n\n")
            f.write(entry)
        existing[name.lower()] = name
        linked.append(name)
    return linked
