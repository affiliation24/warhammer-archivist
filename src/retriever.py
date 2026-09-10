"""Retrieval-логика (Этап 6 roadmap).

top-k поиск релевантных чанков в ChromaDB по запросу пользователя.
Модель эмбеддингов должна совпадать с той, что использовалась при индексации
(intfloat/multilingual-e5-large) — иначе векторные пространства несовместимы.

Апгрейд с e5-base (2026-09-10): e5-large заметно точнее на русском, в частности
сама, без гибридного поиска по названию, находит источники по точному совпадению
темы (см. _title_match_chunks ниже — тот костыль остаётся как подстраховка, но
нагрузка на него теперь меньше). Старая коллекция 'horus_heresy' (e5-base)
оставлена в data/chroma_db на случай отката, не используется активным кодом.

Гибридный поиск (2026-09-10, dense+sparse): чистый dense-поиск (эмбеддинги)
плохо находит редкие имена собственные — конкретный случай, который это выявил:
чанк с описанием второстепенного персонажа ("Бастиан Вервеук") не попал даже
в топ-100 результатов, хотя другой чанк с тем же именем без содержательного
описания оказался на первом месте. Механизм известен как "semantic drift"
редких сущностей в dense-энкодерах — они хорошо кодируют частотные слова,
а для единично встречающихся имён вектор "утекает" к семантически близким,
но частотным кластерам. Индустриальный стандарт для этой проблемы — гибрид
dense + sparse (BM25) поиск с Reciprocal Rank Fusion (RRF), см. LangChain
EnsembleRetriever / LlamaIndex QueryFusionRetriever(mode="reciprocal_rerank").
Реализовано здесь как BM25 через rank_bm25 (построен один раз из chunks.jsonl,
~1.1 ГБ RAM на весь корпус ~67k чанков — учтено при выборе, не пересобирается
на каждый запрос) + RRF-слияние с результатами ChromaDB в retrieve() ниже.
"""
import json
import os
import re
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

import chromadb
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma_db"
CHUNKS_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "chunks.jsonl"
COLLECTION_NAME = "horus_heresy_e5large"
MODEL_NAME = "intfloat/multilingual-e5-large"

RRF_K = 60  # стандартная константа RRF (см. оригинальную статью, дефолт Elasticsearch/OpenSearch)

_model = None
_collection = None
_all_titles = None  # кэш уникальных названий источников для гибридного поиска по title
_bm25_index = None
_bm25_chunks = None  # параллельный список dict-ов чанков (тот же порядок, что в BM25-индексе)


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


_BM25_STEM_LEN = 4  # слова длиннее этого усекаются до префикса — см. докстринг ниже


def _tokenize(text: str) -> list[str]:
    """Токенизация для BM25: слова (\\w+ включает кириллицу в Python 3 re
    с юникодом по умолчанию), в нижнем регистре, длинные слова усечены до
    фиксированного префикса (_BM25_STEM_LEN символов).

    Без усечения падежные формы одного слова (например, "Бастиан" в вопросе
    и "Бастиану" в тексте книги) вообще не пересекаются как термины — эмпирически
    поймано на реальном случае: BM25-score для чанка с описанием персонажа
    оказался 0.000, т.к. в тексте имя стояло в другом падеже. Пробовал полноценную
    морфологию (pymorphy3) и стеммер Snowball — оба давали НЕКОНСИСТЕНТНЫЕ леммы
    именно на вымышленных именах собственных (не в их словаре): например,
    pymorphy3 угадывал для "Бастиан"/"Бастиану" две разные псевдо-леммы вместо
    одной. Фиксированное усечение префикса грубее, но гарантированно консистентно
    для любого слова длиннее порога — а BM25 использует это как один из многих
    статистических сигналов (IDF-взвешенная сумма), а не точное совпадение, так
    что огрубление тут не критично, в отличие от _stem() для гибридного поиска
    по названию (там на кону all-or-nothing совпадение всех слов названия)."""
    words = re.findall(r"\w+", text.lower())
    return [w[:_BM25_STEM_LEN] if len(w) > _BM25_STEM_LEN else w for w in words]


def _get_bm25():
    """Строит BM25-индекс поверх ВСЕХ чанков из chunks.jsonl (не из ChromaDB —
    там текст лежит в тех же record'ах, читать jsonl быстрее, чем постранично
    выгружать document+metadata из SQLite). Кэшируется как singleton — на
    корпусе ~67k чанков это ~1.1 ГБ RAM и несколько секунд построения, поэтому
    строится один раз за процесс, не на каждый запрос."""
    global _bm25_index, _bm25_chunks
    if _bm25_index is None:
        chunks = []
        tokenized_corpus = []
        with open(CHUNKS_PATH, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                chunks.append({
                    "chunk_id": record["chunk_id"],
                    "text": record["text"],
                    "source_title": record["source_title"],
                    "author": record["author"],
                    "chapter_title": record["chapter_title"],
                    "sequence_number": record["sequence_number"],
                    "source_type": record.get("source_type", "book"),
                    "cycle": record.get("cycle", "horus_heresy"),
                })
                tokenized_corpus.append(_tokenize(record["text"]))
        _bm25_chunks = chunks
        _bm25_index = BM25Okapi(tokenized_corpus)
    return _bm25_index, _bm25_chunks


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
    важен. Сравнение по границам слов (стебель — префикс отдельного слова
    запроса), а не "подстрока где угодно в тексте запроса": короткое название
    вроде "Горе" (стебель "гор") иначе ложно совпадает с текстом внутри
    середины совсем другого слова — например "Грегор" содержит "гор" на
    позиции 3-5, хотя само слово "Грегор" не начинается на "гор" — поймано
    эмпирически на вопросе про "Грегора Эйзенхорна". Если все значимые слова
    названия совпали (по такому словному сравнению) — подключаем чанки этого
    источника напрямую, независимо от оценки эмбеддинга."""
    query_words = query.lower().split()
    matched_titles = []
    for title in _get_all_titles():
        if title in exclude_titles:
            continue
        words = [w for w in title.lower().split() if len(w) >= 3]
        if not words:
            continue
        if all(
            any(qw.startswith(_stem(w)) for qw in query_words)
            for w in words
        ):
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


def _rrf_fuse(dense_ids: list[str], bm25_ids: list[str], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(id) = sum(1/(k + rank)) по всем спискам,
    где id встретился (rank — позиция в списке, с 1). Стандартная константа
    k=60 (см. оригинальную статью RRF, дефолт Elasticsearch/OpenSearch) —
    не пытаемся подбирать своё значение, это устоявшийся индустриальный дефолт."""
    scores: dict[str, float] = {}
    for rank, doc_id in enumerate(dense_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    for rank, doc_id in enumerate(bm25_ids, start=1):
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


def retrieve(query: str, k: int = 6) -> list[dict]:
    """Возвращает top-k чанков: [{text, source_title, author, chapter_title,
    sequence_number, source_type, cycle, score}, ...], отсортированных по релевантности.

    Гибридный поиск: dense (векторный, ChromaDB) + sparse (BM25 по тексту чанков),
    слитые через Reciprocal Rank Fusion (RRF) — dense хорошо ловит смысловые
    совпадения, BM25 надёжнее на точных редких терминах/именах собственных, где
    у dense-эмбеддингов проявляется "semantic drift" (см. модуль docstring).
    Сверху подмешивается ещё и гибридный поиск по названию источника
    (см. _title_match_chunks) — он остаётся отдельной, более узкой подстраховкой
    именно для случая "источник целиком посвящён теме вопроса"."""
    model = _get_model()
    collection = _get_collection()

    query_emb = model.encode(["query: " + query], normalize_embeddings=True)[0].tolist()

    dense_result = collection.query(
        query_embeddings=[query_emb],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    dense_ids = dense_result["ids"][0]
    # dist — L2 на нормализованных векторах (2 - 2·cos), не косинусная дистанция;
    # тут не пересчитываем в истинный косинус — порядок сортировки не меняется
    dense_by_id = {
        doc_id: _meta_to_chunk(text, meta, score=1 - dist)
        for doc_id, text, meta, dist in zip(
            dense_ids, dense_result["documents"][0], dense_result["metadatas"][0], dense_result["distances"][0]
        )
    }
    dense_scores = [c["score"] for c in dense_by_id.values()]

    bm25_index, bm25_chunks = _get_bm25()
    bm25_scores_all = bm25_index.get_scores(_tokenize(query))
    top_bm25_positions = bm25_scores_all.argsort()[::-1][:k]
    bm25_ids = []
    bm25_by_id = {}
    for pos in top_bm25_positions:
        chunk = bm25_chunks[pos]
        doc_id = chunk["chunk_id"]
        bm25_ids.append(doc_id)
        bm25_by_id[doc_id] = _meta_to_chunk(chunk["text"], chunk, score=0.0)  # score пересчитан ниже

    fused_scores = _rrf_fuse(dense_ids, bm25_ids, k=RRF_K)
    ordered_ids = sorted(fused_scores.keys(), key=lambda i: fused_scores[i], reverse=True)[:k]

    if dense_scores:
        dense_min, dense_max = min(dense_scores), max(dense_scores)
    else:
        dense_min, dense_max = 0.0, 1.0
    rrf_values = list(fused_scores.values())
    rrf_min, rrf_max = (min(rrf_values), max(rrf_values)) if rrf_values else (0.0, 1.0)

    def rescaled(doc_id: str) -> float:
        if doc_id in dense_by_id:
            return dense_by_id[doc_id]["score"]
        # BM25-only чанк: своей dense cosine-оценки нет — вписываем RRF-скор
        # в тот же диапазон, что и у dense-результатов, чтобы adaptive_k (тюнингованный
        # на масштабе dense cosine similarity) продолжал работать предсказуемо
        if rrf_max == rrf_min:
            return dense_min
        r = fused_scores[doc_id]
        return dense_min + (r - rrf_min) / (rrf_max - rrf_min) * (dense_max - dense_min)

    chunks = []
    for doc_id in ordered_ids:
        base = dense_by_id.get(doc_id) or bm25_by_id.get(doc_id)
        if base is None:
            continue
        chunks.append({**base, "score": rescaled(doc_id)})

    matched_titles = {c["source_title"] for c in chunks}
    hybrid_chunks = _title_match_chunks(query, exclude_titles=matched_titles)
    chunks = hybrid_chunks + chunks  # гибридные по названию — в начало, приоритет выше RRF

    return chunks


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Кто предал Императора?"
    for c in retrieve(q, k=5):
        print(f"[{c['score']:.3f}] {c['source_title']} — {c['text'][:150]}...")
