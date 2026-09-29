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


# --- Reading memories back -------------------------------------------------

_STOPWORDS = {"the", "and", "for", "you", "are", "was", "what", "with", "that", "this", "have",
              "about", "your", "from", "they", "their", "user", "jarvis", "does", "did", "can",
              "know", "tell", "there", "when", "where", "how", "who", "which", "any", "all"}


def _sessions():
    """
    Yield (date, summary, facts) for every session in the daily notes. Parses
    the format append_session writes, including the older one-line entries.
    """
    memory_dir = vault_path() / "Memory"
    if not memory_dir.is_dir():
        return
    for note in sorted(memory_dir.glob("????-??-??.md")):
        day = note.stem
        text = note.read_text(encoding="utf-8", errors="replace")
        for block in re.split(r"^## Session", text, flags=re.MULTILINE)[1:]:
            summary_lines, facts, section = [], [], "summary"
            for line in block.splitlines()[1:]:          # first line is the heading
                stripped = line.strip()
                if stripped.startswith("### "):
                    section = stripped[4:].lower()
                elif not stripped:
                    continue
                elif section == "summary":
                    summary_lines.append(stripped)
                elif section.startswith("facts") and stripped.startswith("- "):
                    # older notes tagged facts like "[preference] Prefers..."
                    facts.append(re.sub(r"^\[\w+\]\s*", "", stripped[2:]))
            yield day, " ".join(summary_lines), facts


def all_records():
    """Every session summary and fact in the vault, as MemoryStore records."""
    records = []
    for day, summary, facts in _sessions():
        if summary:
            records.append({"text": f"On {day}: {summary}", "kind": "session", "date": day})
        records += [{"text": fact, "kind": "fact", "date": day} for fact in facts]
    return records


def search(query, top_k=5):
    """
    Keyword recall straight from the notes — the fallback when the vector
    store or embedding model is unavailable. Scores each session and topic
    line by how many of the query's words it contains.
    """
    words = {w for w in re.findall(r"[a-z]{3,}", query.lower()) if w not in _STOPWORDS}
    if not words:
        return []
    candidates = [(day, f"{summary} {' '.join(facts)}".strip()) for day, summary, facts in _sessions()]
    folder = _entities_dir()
    if folder.is_dir():
        for note in folder.glob("*.md"):
            for line in note.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"- \[\[(\d{4}-\d{2}-\d{2})\]\] \S+ — (.+)", line)
                if match:
                    candidates.append((match.group(1), f"{note.stem}: {match.group(2)}"))
    scored = []
    for day, text in candidates:
        lowered = text.lower()
        score = sum(1 for w in words if w in lowered)
        if score:
            scored.append((score, day, text))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)   # best match, then newest
    return [f"({day}) {text[:400]}" for _, day, text in scored[:top_k]]
