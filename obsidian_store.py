"""
Obsidian integration — the human-readable half of Jarvis's memory.

After each conversation a dated daily note (Memory/YYYY-MM-DD.md) gets the
session episode and any extracted facts appended. Facts keep the [[wikilinks]]
the extraction step put around people and project names, and each fact carries
a #type tag — so the vault is navigable as a graph (people, projects,
preferences) rather than just a chronological log.

For every [[Entity]] mentioned, a stub note is created under Memory/Entities/
if one doesn't exist, so links resolve and backlinks accumulate in Obsidian.
"""

import re
from datetime import datetime
from pathlib import Path

import config
from logging_setup import get_logger

log = get_logger("jarvis.obsidian")

_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


class ObsidianStore:
    def __init__(self, vault_path: Path = config.OBSIDIAN_VAULT):
        self.vault = vault_path
        self.memory_dir = self.vault / "Memory"
        self.entities_dir = self.memory_dir / "Entities"
        self.available = False
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            self.entities_dir.mkdir(parents=True, exist_ok=True)
            self.available = True
            log.info("Obsidian vault connected at %s", self.vault)
        except OSError as e:
            log.warning("Obsidian vault unavailable (%s): %s", self.vault, e)

    def append_session(self, episode: str | None, facts: list[dict]) -> None:
        """Write one session block into today's daily note."""
        if not self.available or (not episode and not facts):
            return
        now = datetime.now()
        note = self.memory_dir / f"{now.strftime('%Y-%m-%d')}.md"
        lines = []
        if not note.exists():
            lines.append(f"# {now.strftime('%A, %B %d, %Y')}\n")
            lines.append("#jarvis/daily\n")
        lines.append(f"\n## {now.strftime('%H:%M')} — Session\n")
        if episode:
            lines.append(f"> {episode}\n")
        if facts:
            lines.append("\n**Learned:**\n")
            for fact in facts:
                lines.append(f"- #{fact.get('type', 'personal')} {fact['text']}\n")
        try:
            with open(note, "a", encoding="utf-8") as f:
                f.writelines(lines)
            log.info("Obsidian daily note updated: %s", note.name)
        except OSError as e:
            log.warning("could not write daily note: %s", e)
            return
        self._ensure_entity_stubs(episode, facts, note.stem)

    def _ensure_entity_stubs(self, episode: str | None, facts: list[dict],
                             day_stem: str) -> None:
        """Create Memory/Entities/<Name>.md for each [[link]] so the graph resolves."""
        mentioned: set[str] = set()
        for text in [episode or ""] + [f["text"] for f in facts]:
            mentioned.update(m.strip() for m in _WIKILINK.findall(text))
        for name in mentioned:
            safe = re.sub(r'[/\\:*?"<>|]', "-", name).strip()
            if not safe:
                continue
            stub = self.entities_dir / f"{safe}.md"
            try:
                if not stub.exists():
                    stub.write_text(
                        f"# {name}\n\n#jarvis/entity\n\n"
                        f"First mentioned in [[{day_stem}]].\n",
                        encoding="utf-8",
                    )
                    log.debug("created entity stub %s", stub.name)
            except OSError as e:
                log.debug("entity stub failed for %r: %s", name, e)
