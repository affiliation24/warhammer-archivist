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


def adaptive_k(scores: list[float], k_min: int = 3, k_max: int | None = None, margin: float = 0.05) -> int:
    """Определяет, сколько из отсортированных по убыванию оценок релевантности
    реально стоит использовать: держим чанки, пока их оценка остаётся в пределах
    margin от лучшей оценки в выдаче, обрезаем на первом, что отстал сильнее.

    Локальные разрывы между соседними оценками (первая версия этой функции)
    оказались ненадёжным сигналом на этом корпусе: и узкие, и широкие вопросы
    показывают одинаковый паттерн — один заметный скачок сразу после лучшего
    результата, а затем долгий плавный хвост, — так что "локоть" в соседних
    разрывах не отличает узкий вопрос от широкого. Абсолютный отступ от лучшей
    оценки работает надёжнее: на широком вопросе весь плавный хвост держится
    близко к лучшей оценке и укладывается в margin целиком (вернутся все); на
    узком — оценки расходятся быстрее и выходят за margin раньше."""
    n = len(scores) if k_max is None else min(len(scores), k_max)
    if n <= k_min:
        return n
    floor = scores[0] - margin
    for i in range(k_min, n):
        if scores[i] < floor:
            return i
    return n


def retrieve(query: str, k: int = 6) -> list[dict]:
    """Возвращает top-k чанков: [{text, source_title, author, chapter_title,
    sequence_number, source_type, cycle, score}, ...], отсортированных по релевантности."""
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
            "source_type": meta.get("source_type", "book"),
            "cycle": meta.get("cycle", "horus_heresy"),
            "score": 1 - dist,  # т.к. эмбеддинги нормализованы, distance ~ косинусная
        })
    return chunks


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Кто предал Императора?"
    for c in retrieve(q, k=5):
        print(f"[{c['score']:.3f}] {c['source_title']} — {c['text'][:150]}...")
