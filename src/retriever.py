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

Reranker (2026-09-10): RRF даёт неплохой порядок, но остаётся статистическим
приближением — cross-encoder reranker (BAAI/bge-reranker-v2-m3, поддерживает
русский) читает пару (вопрос, текст чанка) целиком и оценивает релевантность
напрямую, без разрыва между кодированием вопроса и текста отдельно друг от
друга (в отличие от dense bi-encoder, где вопрос и документ кодируются
независимо). Это одним механизмом покрывает то, для чего раньше пришлось
вручную писать _title_match_chunks/_stem под каждый найденный на практике
edge-case — reranker сам разбирается и с точным совпадением имени, и со
смысловым сходством. См. rerank() и adaptive_k_relative() ниже; последняя —
отдельная от adaptive_k() функция отсечения, т.к. шкала оценок reranker'а
(сигмоида, резкий разброс 0.01-0.99) несовместима с шкалой dense-косинуса
(плавная, 0.55-0.85), на которую был откалиброван adaptive_k(margin=...).
"""
import json
import os
import re
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
# Кэш моделей — рядом с проектом, а не в домашней папке пользователя: если
# запускать с флешки/переносного носителя, веса моделей не должны оседать на
# диске хост-машины. Должно быть выставлено до первого импорта huggingface_hub/
# sentence_transformers, иначе библиотека уже выберет путь по умолчанию.
os.environ.setdefault("HF_HOME", str(Path(__file__).resolve().parent.parent / "hf_cache"))

import chromadb
import torch
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma_db"
CHUNKS_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "chunks.jsonl"
COLLECTION_NAME = "horus_heresy_e5large"
MODEL_NAME = "intfloat/multilingual-e5-large"
RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"

RRF_K = 60  # стандартная константа RRF (см. оригинальную статью, дефолт Elasticsearch/OpenSearch)

# Модули-singleton'ы ниже (_model, _collection, _all_titles, _bm25_index, _reranker) НЕ
# потокобезопасны — паттерн "if _x is None: строим" гонится, если retrieve()
# вызывается параллельно из нескольких потоков до прогрева кэша (например, из
# веб-сервера с несколькими воркерами на один процесс). Для текущего использования
# (однопоточный CLI, один вызов input()/ответ за раз в chat.py) это безопасно.
# Если код когда-нибудь переедет за FastAPI/подобный сервер с многопоточностью —
# нужно добавить threading.Lock() вокруг каждой инициализации.
_model = None
_collection = None
_all_titles = None  # кэш уникальных названий источников для гибридного поиска по title
_bm25_index = None
_bm25_chunks = None  # параллельный список dict-ов чанков (тот же порядок, что в BM25-индексе)
_reranker = None


def _pick_device() -> str:
    """CUDA -> MPS (Apple Silicon) -> CPU. Раньше было захардкожено "mps" —
    упало бы RuntimeError на любой машине без Apple Silicon (например, в
    докер-контейнере на линукс-сервере с CUDA, куда RAG-сервисы обычно и едут)."""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME, device=_pick_device())
    return _model


def _get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoder(RERANKER_MODEL_NAME, device=_pick_device(), max_length=512)
    return _reranker


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

        # BM25 читает chunks.jsonl, а dense-поиск — коллекцию ChromaDB: это два
        # независимых источника одних и тех же данных (см. модуль docstring про
        # причину — читать jsonl быстрее, чем постранично выгружать текст из
        # SQLite). Если когда-нибудь переиндексировать только Chroma (например,
        # поменять чанкинг) и забыть пересобрать chunks.jsonl, оба поиска будут
        # молча матчить разные версии текста под одними chunk_id — RRF тогда
        # сливает два разных мира без единой видимой ошибки. Дёшево проверить
        # хотя бы количество записей — не гарантия идентичности содержимого,
        # но ловит самый частый случай рассинхрона (забыли один из шагов пайплайна).
        chroma_count = _get_collection().count()
        if len(_bm25_chunks) != chroma_count:
            raise RuntimeError(
                f"Рассинхрон данных: в chunks.jsonl {len(_bm25_chunks)} записей, "
                f"в коллекции ChromaDB — {chroma_count}. Похоже, пайплайн "
                f"(fb2_to_json.py -> chunk_books.py -> embed_and_index.py) "
                f"прогнан не полностью после последнего изменения источников."
            )
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


def adaptive_k_relative(
    scores: list[float], k_min: int = 3, k_max: int | None = None, ratio: float = 0.3
) -> int:
    """Аналог adaptive_k, но для оценок reranker'а: держим чанки, пока их
    оценка не опустится ниже ratio * лучшая_оценка (мультипликативный порог),
    а не margin в абсолютных единицах.

    Reranker выдаёт sigmoid-подобные оценки с резким разбросом (0.01-0.99 —
    не плавная косинусная похожесть 0.55-0.85, на которую откалиброван
    adaptive_k выше). Абсолютный отступ margin=0.05 либо отрезал бы почти всё
    (top=0.99, второй релевантный=0.85 — уже за порогом), либо, наоборот,
    пропускал бы явно нерелевантные хвосты при низком top-score. Откалибровано
    эмпирически на реальных примерах трёх типов вопросов (широкий "перечисли
    примархов", узкий с одним лояльным чанком, узкий с множеством релевантных
    чанков) — ratio=0.3 давал разумную отсечку во всех трёх случаях."""
    n = len(scores) if k_max is None else min(len(scores), k_max)
    if n <= k_min:
        return n
    floor = scores[0] * ratio
    for i in range(k_min, n):
        if scores[i] < floor:
            return i
    return n


BOOT_STAGES = (
    ("chroma", "Активация архивной базы данных"),
    ("dense", "Инициализация нейросети эмбеддингов"),
    ("bm25", "Синхронизация BM25-индекса"),
    ("reranker", "Калибровка когнитивного фильтра"),
)


def warm_up(on_stage=None) -> None:
    """Явно загружает все модели/индексы по порядку вместо ленивой загрузки
    по требованию — иначе холодная загрузка (первый запрос) молча висит на
    спиннере "Машинный Дух обрабатывает запрос", и непонятно, идёт загрузка
    моделей (десятки секунд-минуты на медленном носителе) или что-то зависло.
    on_stage(stage_key), если передан, вызывается перед началом каждого этапа
    из BOOT_STAGES — используется для отрисовки прогресса в chat.py."""
    for stage_key, _ in BOOT_STAGES:
        if on_stage:
            on_stage(stage_key)
        if stage_key == "chroma":
            _get_collection()
        elif stage_key == "dense":
            _get_model()
        elif stage_key == "bm25":
            _get_bm25()
        elif stage_key == "reranker":
            _get_reranker()


def rerank(query: str, chunks: list[dict]) -> list[dict]:
    """Пересчитывает 'score' каждого чанка через cross-encoder reranker
    (пара вопрос+текст оцениваются моделью совместно, не раздельным
    кодированием как в dense-поиске) и возвращает тот же список, отсортированный
    по новой оценке по убыванию. Заменяет собой ad hoc-подстраховки типа
    _title_match_chunks/_stem одним универсальным механизмом — reranker сам
    разбирается и с точным совпадением имени, и со смысловым сходством, без
    необходимости вручную ловить каждый новый edge-case (см. модуль docstring)."""
    if not chunks:
        return chunks
    model = _get_reranker()
    scores = model.predict([(query, c["text"]) for c in chunks])
    reranked = sorted(zip(scores, chunks), key=lambda pair: -pair[0])
    return [{**c, "score": float(s)} for s, c in reranked]


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

    # отдельный .get() на каждый совпавший тайтл, а не один общий $in-запрос
    # с эвристическим запасом (limit * 3): общий лимит режет по всей выборке
    # сразу, не по группам — если один совпавший источник заметно длиннее
    # другого (больше чанков идёт первыми в выдаче Chroma), более короткий
    # рисковал остаться вообще без чанков, несмотря на per_title_cap. Отдельный
    # запрос на тайтл стоит на порядок дороже по количеству round-trip'ов, но
    # даёт точную гарантию баланса — при малом числе совпавших тайтлов (обычно
    # 0-2 за вопрос) цена пренебрежимо мала на локальной SQLite.
    chunks = []
    for title in matched_titles:
        result = collection.get(
            where={"source_title": title},
            include=["documents", "metadatas"],
            limit=per_title_cap,
        )
        # искусственно высокая оценка — гарантирует место в топе adaptive_k независимо
        # от того, что скажет векторное сходство
        chunks.extend(
            _meta_to_chunk(text, meta, score=1.0)
            for text, meta in zip(result["documents"], result["metadatas"])
        )
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


def retrieve(query: str, k: int = 6, k_prefetch: int = 30) -> list[dict]:
    """Возвращает top-k чанков: [{text, source_title, author, chapter_title,
    sequence_number, source_type, cycle, score}, ...], отсортированных по релевантности.
    Гарантированно не длиннее k — включая чанки из гибридного поиска по названию.

    Гибридный поиск: dense (векторный, ChromaDB) + sparse (BM25 по тексту чанков),
    слитые через Reciprocal Rank Fusion (RRF) — dense хорошо ловит смысловые
    совпадения, BM25 надёжнее на точных редких терминах/именах собственных, где
    у dense-эмбеддингов проявляется "semantic drift" (см. модуль docstring).
    Сверху подмешивается ещё и гибридный поиск по названию источника
    (см. _title_match_chunks) — он остаётся отдельной, более узкой подстраховкой
    именно для случая "источник целиком посвящён теме вопроса".

    k_prefetch — глубина префетча для dense и BM25 ДО слияния (не финальный
    размер выдачи). Если брать оба списка шириной ровно k, RRF вырождается в
    "первые k из dense плюс первые k из BM25" — документ, средне-хороший в обоих
    рейтингах (скажем, на 8-м месте в каждом), может быть значимо релевантнее
    любого из документов, попавших в оба top-k по отдельности, но просто не
    попадёт в пул для слияния при слишком узком префетче. Стандартная практика
    (см. также доки Qdrant) — префетчить широко (20-50) и резать до k только
    после фьюжна."""
    prefetch = max(k, k_prefetch)
    model = _get_model()
    collection = _get_collection()

    query_emb = model.encode(["query: " + query], normalize_embeddings=True)[0].tolist()

    dense_result = collection.query(
        query_embeddings=[query_emb],
        n_results=prefetch,
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
    top_bm25_positions = bm25_scores_all.argsort()[::-1][:prefetch]
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

    # без этой обрезки функция могла вернуть больше k (до k + до 4*per_title_cap
    # чанков от _title_match_chunks) — нарушение контракта "top-k", на которое
    # неявно рассчитывают вызывающие (например, оценка бюджета контекста в
    # generator.py по числу чанков)
    return chunks[:k]


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Кто предал Императора?"
    for c in retrieve(q, k=5):
        print(f"[{c['score']:.3f}] {c['source_title']} — {c['text'][:150]}...")
