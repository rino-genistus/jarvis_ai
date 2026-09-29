"""
On-device long-term memory: a Chroma vector store with embeddings computed
locally by Ollama (nomic-embed-text). Nothing leaves the Mac, and there are no
quotas — Pinecone's free tier ran out of reads mid-month and took recall with it.

Recall is tiered, most detailed first:
    1. Chroma      semantic search over every fact and session summary
    2. Obsidian    keyword search over the vault's notes, if Chroma or the
                   embedding model is unavailable (obsidian_store.search)

The Obsidian vault stays the human-readable copy. On first run the store is
seeded from it, so recall starts with everything the notes already hold.
"""

import hashlib
from pathlib import Path

import ollama

EMBED_MODEL = "nomic-embed-text"
COLLECTION = "jarvis-memory"
DATA_DIR = Path(__file__).resolve().parent / "data" / "chroma"

# Cosine distance above which a hit is too loosely related to be worth putting
# in front of the model. Measured with nomic-embed-text: related questions
# ("what coding style do I prefer") land at 0.29-0.36, unrelated ones ("call
# me Captain", "what's the date next Friday") at 0.43-0.61. At 0.55 an
# unrelated coding preference leaked into the reply to "call me Captain".
MAX_DISTANCE = 0.40


def record_id(text, date=""):
    """Stable id from content, so re-importing the same memory updates it rather than duplicating it."""
    return "mem-" + hashlib.sha1(f"{date}|{text}".encode()).hexdigest()[:16]


class MemoryStore:

    def __init__(self, keep_alive="15m"):
        import chromadb
        from chromadb.config import Settings
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.keep_alive = keep_alive
        self.client = chromadb.PersistentClient(path=str(DATA_DIR),
                                                settings=Settings(anonymized_telemetry=False))
        self.collection = self.client.get_or_create_collection(
            COLLECTION, metadata={"hnsw:space": "cosine"})

    def _embed(self, texts, as_query):
        # nomic-embed-text is trained with these task prefixes; without them
        # query-to-document matching is noticeably worse.
        prefix = "search_query: " if as_query else "search_document: "
        response = ollama.embed(model=EMBED_MODEL, input=[prefix + t for t in texts],
                                keep_alive=self.keep_alive)
        return response["embeddings"]

    def add(self, records):
        """
        records: [{"text", "kind" ("fact" | "session"), "date" (YYYY-MM-DD), optional "id"}].
        Upserts, so adding the same memory twice is harmless.
        """
        records = [r for r in records if r.get("text", "").strip()]
        if not records:
            return 0
        texts = [r["text"].strip() for r in records]
        self.collection.upsert(
            ids=[r.get("id") or record_id(t, r.get("date", "")) for r, t in zip(records, texts)],
            documents=texts,
            embeddings=self._embed(texts, as_query=False),
            metadatas=[{"kind": r.get("kind", "fact"), "date": r.get("date", "")} for r in records],
        )
        return len(records)

    def search(self, query, top_k=5):
        """The closest memories as short strings, each tagged with the day it's from."""
        if self.collection.count() == 0:
            return []
        result = self.collection.query(query_embeddings=self._embed([query], as_query=True),
                                       n_results=min(top_k, self.collection.count()))
        memories = []
        for text, meta, distance in zip(result["documents"][0], result["metadatas"][0],
                                        result["distances"][0]):
            if distance <= MAX_DISTANCE:
                memories.append(f"({meta.get('date')}) {text}" if meta.get("date") else text)
        return memories

    def count(self):
        return self.collection.count()

    def seed_from_obsidian(self, records):
        """First run only: load what the vault already knows, so recall doesn't start empty."""
        if self.count() > 0 or not records:
            return 0
        added = 0
        for i in range(0, len(records), 64):      # batches keep each embed call small
            added += self.add(records[i:i + 64])
        return added
