"""Retrieval-логика (Этап 6 roadmap).

top-k поиск релевантных чанков в ChromaDB по запросу пользователя.
Модель эмбеддингов должна совпадать с той, что использовалась при индексации
(intfloat/multilingual-e5-base) — иначе векторные пространства несовместимы.
"""
import os
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import chromadb
from sentence_transformers import SentenceTransformer

CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma_db"
COLLECTION_NAME = "horus_heresy"
MODEL_NAME = "intfloat/multilingual-e5-base"

_model = None
_collection = None


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME, device="mps")
    return _model


def _get_collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_collection(COLLECTION_NAME)
    return _collection


def retrieve(query: str, k: int = 6) -> list[dict]:
    """Возвращает top-k чанков: [{text, source_title, author, chapter_title,
    sequence_number, score}, ...], отсортированных по релевантности."""
    model = _get_model()
    collection = _get_collection()

    query_emb = model.encode(["query: " + query], normalize_embeddings=True)[0].tolist()

    result = collection.query(
        query_embeddings=[query_emb],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for text, meta, dist in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        chunks.append({
            "text": text,
            "source_title": meta["source_title"],
            "author": meta["author"],
            "chapter_title": meta["chapter_title"],
            "sequence_number": meta["sequence_number"],
            "score": 1 - dist,  # т.к. эмбеддинги нормализованы, distance ~ косинусная
        })
    return chunks


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Кто предал Императора?"
    for c in retrieve(q, k=5):
        print(f"[{c['score']:.3f}] {c['source_title']} — {c['text'][:150]}...")
