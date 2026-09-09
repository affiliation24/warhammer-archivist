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
_all_titles = None  # кэш уникальных названий источников для гибридного поиска по title


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


def _get_all_titles() -> list[str]:
    """Все уникальные source_title в базе, длинные вперёд — чтобы при проверке
    подстроки более специфичное название ("Дети Императора") матчилось раньше
    короткого общего слова. Считается один раз и кэшируется на время процесса."""
    global _all_titles
    if _all_titles is None:
        collection = _get_collection()
        titles: set[str] = set()
        batch_size = 5000  # collection.get() без пагинации падает на "too many SQL variables"
        offset = 0
        while True:
            result = collection.get(include=["metadatas"], limit=batch_size, offset=offset)
            metas = result["metadatas"]
            if not metas:
                break
            titles.update(m["source_title"] for m in metas if m.get("source_title"))
            offset += batch_size
        _all_titles = sorted(titles, key=len, reverse=True)
    return _all_titles


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


def _meta_to_chunk(text: str, meta: dict, score: float) -> dict:
    return {
        "text": text,
        "source_title": meta["source_title"],
        "author": meta["author"],
        "chapter_title": meta["chapter_title"],
        "sequence_number": meta["sequence_number"],
        "source_type": meta.get("source_type", "book"),
        "cycle": meta.get("cycle", "horus_heresy"),
        "score": score,
    }


def _stem(word: str) -> str:
    """Грубый "стебель" русского слова — обрезает типичные падежные окончания,
    чтобы "Дети" совпадало с "Детей", "Детям" и т.п. Не настоящая лемматизация,
    но достаточно для сравнения названия источника с вопросом.

    Для слов длиннее 4 символов оставляем минимум 4 символа: более короткий
    стебель (например, 3 символа у "Хорус" -> "хор") на практике ловит ложные
    совпадения с совершенно другими словами вроде "Хорст" — эмпирически поймано
    при тестировании."""
    n = len(word)
    if n <= 4:
        return word[:3]
    return word[:max(4, n - 2)]


def _title_match_chunks(query: str, exclude_titles: set, per_title_cap: int = 4) -> list[dict]:
    """Гибридный поиск по совпадению названия источника с вопросом: чисто
    векторный поиск иногда не находит очевидный источник, если название
    встречается в вопросе буквально (с точностью до падежа), а модель
    эмбеддингов не считает энциклопедическую статью похожей по смыслу на
    формулировку вопроса (проверено эмпирически на "Дети Императора" — в
    вопросе стоит "Детей" (родительный падеж), точное совпадение подстроки не
    срабатывает, а по чистому косинусному сходству источник на 497-м месте).
    Сравниваем не точную подстроку, а "стебли" слов названия — так падеж не
    важен. Если все значимые слова названия совпали — подключаем чанки этого
    источника напрямую, независимо от оценки эмбеддинга."""
    query_lower = query.lower()
    matched_titles = []
    for title in _get_all_titles():
        if title in exclude_titles:
            continue
        words = [w for w in title.lower().split() if len(w) >= 3]
        if not words:
            continue
        if all(_stem(w) in query_lower for w in words):
            matched_titles.append(title)
    if not matched_titles:
        return []
    collection = _get_collection()

    result = collection.get(
        where={"source_title": {"$in": matched_titles}},
        include=["documents", "metadatas"],
        limit=per_title_cap * len(matched_titles) * 3,  # запас, т.к. limit режет по всей выборке, не по группам
    )
    # ограничиваем per_title_cap чанками на каждое совпавшее название, чтобы
    # длинная статья не вытеснила из контекста всё остальное
    per_title_count: dict[str, int] = {}
    chunks = []
    for text, meta in zip(result["documents"], result["metadatas"]):
        title = meta["source_title"]
        if per_title_count.get(title, 0) >= per_title_cap:
            continue
        per_title_count[title] = per_title_count.get(title, 0) + 1
        # искусственно высокая оценка — гарантирует место в топе adaptive_k независимо
        # от того, что скажет векторное сходство
        chunks.append(_meta_to_chunk(text, meta, score=1.0))
    return chunks


def retrieve(query: str, k: int = 6) -> list[dict]:
    """Возвращает top-k чанков: [{text, source_title, author, chapter_title,
    sequence_number, source_type, cycle, score}, ...], отсортированных по релевантности.
    Сочетает векторный поиск с гибридным подключением источников, чьё название
    буквально встречается в вопросе (см. _title_match_chunks)."""
    model = _get_model()
    collection = _get_collection()

    query_emb = model.encode(["query: " + query], normalize_embeddings=True)[0].tolist()

    result = collection.query(
        query_embeddings=[query_emb],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    seen_ids = set(result["ids"][0])
    for chunk_id, text, meta, dist in zip(
        result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        # dist — L2 на нормализованных векторах (2 - 2·cos), не косинусная дистанция;
        # тут не пересчитываем в истинный косинус — порядок сортировки не меняется,
        # а abs. значение "score" используется только для сравнения внутри одной выдачи
        chunks.append(_meta_to_chunk(text, meta, score=1 - dist))

    matched_titles = {c["source_title"] for c in chunks}
    hybrid_chunks = _title_match_chunks(query, exclude_titles=matched_titles)
    chunks = hybrid_chunks + chunks  # гибридные — в начало, они гарантированно релевантны по названию

    return chunks


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Кто предал Императора?"
    for c in retrieve(q, k=5):
        print(f"[{c['score']:.3f}] {c['source_title']} — {c['text'][:150]}...")
