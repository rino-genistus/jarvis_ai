"""
Long-term memory for Jarvis: Pinecone vector store + LLM extraction + session state.

The field-name bug that silently broke every upsert is fixed structurally: the
record text field is read from the live index's own field_map at startup, so
records always match whatever the index was created with (legacy "chunk_text"
indexes keep working; new indexes use "text").

Retrieval blends recency into pure semantic similarity: equal-similarity facts
rank higher when newer (exponential decay, MEMORY_RECENCY_HALF_LIFE_DAYS), and
every memory is labelled with relative time ("today", "3 days ago") so the LLM
has temporal context.

Extraction runs after each conversation (the orchestrator calls it from a
background thread): typed facts (preference / habit / project / personal /
decision) plus a one-sentence session episode. People and project names come
back wrapped in [[wikilinks]] for the Obsidian layer; links are stripped before
vectors are stored.

`consolidate()` is the periodic merge job — it dedupes and reconciles
contradictory facts so memory quality stays high over months of use.
"""

import json
import re
import time

from ollama import chat

import config
from logging_setup import get_logger

log = get_logger("jarvis.memory")

FACT_TYPES = ("preference", "habit", "project", "personal", "decision")


def relative_time_label(created_at: float | None) -> str:
    if not created_at:
        return "a while ago"
    days = max(0.0, (time.time() - created_at) / 86400)
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 7:
        return f"{int(days)} days ago"
    if days < 14:
        return "last week"
    if days < 30:
        return f"{int(days / 7)} weeks ago"
    if days < 60:
        return "last month"
    return f"{int(days / 30)} months ago"


def strip_wikilinks(text: str) -> str:
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)


# ---------------------------------------------------------------------------
# vector store
# ---------------------------------------------------------------------------

class MemoryStore:
    def __init__(self):
        self._index = None
        self._text_field = "text"

    def connect(self) -> None:
        import os
        from pinecone import Pinecone
        pc = Pinecone(api_key=os.getenv("PINECONE_API_KEY"))
        if not pc.has_index(config.PINECONE_INDEX):
            pc.create_index_for_model(
                name=config.PINECONE_INDEX,
                cloud="aws",
                region="us-east-1",
                embed={"model": "llama-text-embed-v2", "field_map": {"text": "text"}},
            )
        # read the record field name from the index itself so upserts can
        # never silently mismatch the field_map again
        try:
            description = pc.describe_index(config.PINECONE_INDEX)
            field_map = dict(description.embed.field_map or {})
            self._text_field = field_map.get("text", "text")
        except Exception as e:  # noqa: BLE001
            log.warning("could not read index field_map, assuming 'text': %s", e)
        self._index = pc.Index(config.PINECONE_INDEX)
        log.info("Pinecone connected (record text field: %r)", self._text_field)

    @property
    def available(self) -> bool:
        return self._index is not None

    # ------------------------------------------------------------ writes
    def store_facts(self, facts: list[dict]) -> None:
        """facts: [{"type": "preference", "text": "..."}] — wikilinks stripped here."""
        if not self.available or not facts:
            return
        now = time.time()
        records = [
            {
                "_id": f"mem-{int(now)}-{i}",
                self._text_field: strip_wikilinks(fact["text"]),
                "type": fact.get("type", "personal"),
                "kind": "fact",
                "created_at": now,
            }
            for i, fact in enumerate(facts)
        ]
        self._index.upsert_records(namespace=config.PINECONE_NAMESPACE, records=records)
        log.info("stored %d facts: %s", len(records),
                 [r[self._text_field][:60] for r in records])

    def store_episode(self, episode: str) -> None:
        if not self.available or not episode:
            return
        now = time.time()
        self._index.upsert_records(
            namespace=config.PINECONE_NAMESPACE,
            records=[{
                "_id": f"ep-{int(now)}",
                self._text_field: strip_wikilinks(episode),
                "kind": "episode",
                "created_at": now,
            }],
        )
        log.info("stored episode: %s", episode[:80])

    # ------------------------------------------------------------ retrieval
    def retrieve(self, query: str, top_k: int = config.MEMORY_TOP_K) -> list[str]:
        """Semantic search rescored with recency; returns time-labelled lines."""
        if not self.available or not query.strip():
            return []
        try:
            results = self._index.search(
                namespace=config.PINECONE_NAMESPACE,
                query={"inputs": {"text": query}, "top_k": top_k * 3},
                fields=[self._text_field, "created_at", "type"],
            )
        except Exception as e:  # noqa: BLE001
            log.warning("memory retrieval failed: %s", e)
            return []

        scored = []
        for hit in results["result"]["hits"]:
            fields = hit.get("fields", {})
            text = fields.get(self._text_field)
            if not text:
                continue
            try:  # Pinecone may hand numeric fields back as strings
                created_at = float(fields.get("created_at") or 0) or None
            except (TypeError, ValueError):
                created_at = None
            age_days = (time.time() - created_at) / 86400 if created_at else 365.0
            recency = 0.5 ** (age_days / config.MEMORY_RECENCY_HALF_LIFE_DAYS)
            score = float(hit.get("_score", 0.0)) + config.MEMORY_RECENCY_WEIGHT * recency
            scored.append((score, text, created_at))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [f"({relative_time_label(created)}) {text}"
                for _, text, created in scored[:top_k]]

    # ------------------------------------------------------------ consolidation
    def consolidate(self, max_records: int = 300) -> None:
        """Periodic merge job: dedupe and reconcile contradictory facts."""
        if not self.available:
            return
        try:
            ids = []
            for page in self._index.list(namespace=config.PINECONE_NAMESPACE):
                ids.extend(page)
                if len(ids) >= max_records:
                    break
            fact_records = {}
            for start in range(0, len(ids), 100):
                fetched = self._index.fetch(ids=ids[start:start + 100],
                                            namespace=config.PINECONE_NAMESPACE)
                for rid, vector in fetched.vectors.items():
                    metadata = vector.metadata or {}
                    if metadata.get("kind") == "fact" and metadata.get(self._text_field):
                        fact_records[rid] = metadata
            if len(fact_records) < 10:
                return  # not enough accumulated to be worth a merge pass

            numbered = "\n".join(
                f"{i}. [{m.get('type', 'personal')}] {m[self._text_field]}"
                for i, m in enumerate(fact_records.values())
            )
            response = chat(
                model=config.UTILITY_MODEL,
                messages=[{
                    "role": "user",
                    "content": "These are memory records about one user. Merge duplicates and "
                               "resolve contradictions (prefer the later-numbered fact — those are "
                               "newer). Return the cleaned list, one fact per line formatted as "
                               "'type | fact' where type is one of "
                               f"{', '.join(FACT_TYPES)}. No other text.\n\n{numbered}",
                }],
            )
            cleaned = _parse_typed_lines(response.message.content or "")
            if not cleaned or len(cleaned) > len(fact_records):
                log.warning("consolidation produced unusable output, skipping")
                return
            self._index.delete(ids=list(fact_records.keys()),
                               namespace=config.PINECONE_NAMESPACE)
            self.store_facts(cleaned)
            log.info("consolidated %d facts -> %d", len(fact_records), len(cleaned))
        except Exception as e:  # noqa: BLE001
            log.warning("consolidation failed: %s", e)


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

def _conversation_text(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            lines.append(f"{m['role']}: {m['content']}")
    return "\n".join(lines)


def _parse_typed_lines(raw: str) -> list[dict]:
    facts = []
    for line in raw.splitlines():
        line = line.strip().lstrip("0123456789.-) ")
        if not line or line.upper() == "NONE":
            continue
        if "|" in line:
            fact_type, _, text = line.partition("|")
            fact_type = fact_type.strip().lower().strip("[]")
            text = text.strip()
        else:
            fact_type, text = "personal", line
        if fact_type not in FACT_TYPES:
            fact_type = "personal"
        if text:
            facts.append({"type": fact_type, "text": text})
    return facts


def extract_facts(messages: list[dict]) -> list[dict]:
    """Typed long-term facts from a finished conversation. [] for trivial sessions."""
    transcript = _conversation_text(messages)
    if not transcript:
        return []
    try:
        response = chat(
            model=config.UTILITY_MODEL,
            messages=[{
                "role": "user",
                "content": f"""Review this conversation and extract only information worth remembering long-term about the user.

Each fact must be typed as one of: {', '.join(FACT_TYPES)}.
- preference: likes, dislikes, tastes
- habit: routines, recurring behaviour
- project: ongoing work or goals
- personal: relationships, identity, life facts
- decision: choices the user has made

Format: one fact per line as 'type | fact'. Wrap names of people and projects in [[double brackets]], e.g. "personal | His brother [[Daniel]] lives in Austin".
Ignore greetings, small talk, and one-off lookups like weather. If nothing is worth remembering, reply with exactly NONE.

Conversation:
{transcript}""",
            }],
        )
        return _parse_typed_lines(response.message.content or "")
    except Exception as e:  # noqa: BLE001
        log.warning("fact extraction failed: %s", e)
        return []


def extract_episode(messages: list[dict]) -> str | None:
    """One-sentence 'We discussed...' summary, or None for trivial sessions."""
    transcript = _conversation_text(messages)
    if not transcript:
        return None
    try:
        response = chat(
            model=config.UTILITY_MODEL,
            messages=[{
                "role": "user",
                "content": "Summarize what was accomplished in this conversation in exactly one "
                           "sentence starting with 'We discussed' or 'We '. If it was trivial "
                           "(a greeting, a single weather lookup), reply with exactly NONE.\n\n"
                           + transcript,
            }],
        )
        episode = (response.message.content or "").strip()
        if not episode or episode.upper().startswith("NONE"):
            return None
        return episode.splitlines()[0].strip()
    except Exception as e:  # noqa: BLE001
        log.warning("episode extraction failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# persistent session state (cross-session continuity, counters)
# ---------------------------------------------------------------------------

class JarvisState:
    """Tiny JSON state file: last episode, session counter, briefing date."""

    def __init__(self):
        self.data = {"last_episode": None, "last_episode_at": None,
                     "session_count": 0, "last_briefing_date": None}
        try:
            if config.STATE_FILE.exists():
                self.data.update(json.loads(config.STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("state file unreadable, starting fresh: %s", e)

    def save(self) -> None:
        try:
            config.STATE_FILE.write_text(json.dumps(self.data, indent=2))
        except OSError as e:
            log.warning("could not save state: %s", e)

    def continuity_line(self) -> str | None:
        """'Last time we spoke (yesterday), we discussed...' opener for the prompt."""
        episode = self.data.get("last_episode")
        if not episode:
            return None
        label = relative_time_label(self.data.get("last_episode_at"))
        return f"Last time you spoke ({label}): {episode}"

    def record_session(self, episode: str | None) -> int:
        """Returns the new session count (used for consolidation cadence)."""
        if episode:
            self.data["last_episode"] = episode
            self.data["last_episode_at"] = time.time()
        self.data["session_count"] = int(self.data.get("session_count", 0)) + 1
        self.save()
        return self.data["session_count"]
