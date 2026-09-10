"""Эмбеддинги + заливка в ChromaDB (Этапы 4-5 roadmap).

Модель: intfloat/multilingual-e5-large — точнее e5-base на русском (в 2 раза
больше параметров), но и заметно медленнее считать. Апгрейд с base сделан
через НОВУЮ коллекцию (другая размерность вектора: 1024 против 768 у base —
несовместимы, инкрементально дополнить старую нельзя), поэтому здесь
полный пересчёт эмбеддингов для всех чанков, а не только новых.

e5-модели требуют префикса "query: "/"passage: " перед текстом — это часть
их протокола обучения, а не опция.
"""
import json
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

CHUNKS_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "chunks.jsonl"
CHROMA_DIR = Path(__file__).resolve().parent.parent / "data" / "chroma_db"
COLLECTION_NAME = "horus_heresy_e5large"
MODEL_NAME = "intfloat/multilingual-e5-large"

EMBED_BATCH = 32  # ниже, чем у base (64) — e5-large тяжелее, экономим пиковую память на 8 ГБ RAM
CHROMA_BATCH = 500


def load_chunks():
    with open(CHUNKS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def main():
    chunks = load_chunks()
    print(f"Загружено {len(chunks)} чанков из chunks.jsonl")

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    collection = client.get_or_create_collection(
        COLLECTION_NAME, metadata={"embedding_model": MODEL_NAME}
    )

    # инкрементальный режим: эмбеддим и добавляем только то, чего ещё нет в коллекции —
    # пересчитывать уже проиндексированные чанки заново дорого (часы на Apple Silicon)
    existing_ids = set(collection.get(include=[])["ids"])
    new_chunks = [c for c in chunks if c["chunk_id"] not in existing_ids]
    print(f"Уже в коллекции: {len(existing_ids)}, новых для индексации: {len(new_chunks)}")

    if not new_chunks:
        print("Новых чанков нет, индексация не требуется.")
        return

    device = "mps"
    print(f"Загружаю модель {MODEL_NAME} (device={device})...")
    model = SentenceTransformer(MODEL_NAME, device=device)

    chunks = new_chunks
    texts = ["passage: " + c["text"] for c in chunks]

    print("Считаю эмбеддинги...")
    embeddings = model.encode(
        texts,
        batch_size=EMBED_BATCH,
        show_progress_bar=True,
        normalize_embeddings=True,
    )

    print("Заливаю в ChromaDB...")
    for i in range(0, len(chunks), CHROMA_BATCH):
        batch = chunks[i:i + CHROMA_BATCH]
        batch_emb = embeddings[i:i + CHROMA_BATCH]
        collection.add(
            ids=[c["chunk_id"] for c in batch],
            embeddings=[e.tolist() for e in batch_emb],
            documents=[c["text"] for c in batch],
            metadatas=[
                {
                    "source_type": c["source_type"],
                    "source_title": c["source_title"],
                    "author": c["author"] or "",
                    "sequence_number": c["sequence_number"] or 0,
                    "cycle": c.get("cycle", "horus_heresy"),
                    "chapter_title": c["chapter_title"] or "",
                    "chunk_index_in_source": c["chunk_index_in_source"],
                }
                for c in batch
            ],
        )
        print(f"  {min(i + CHROMA_BATCH, len(chunks))}/{len(chunks)}")

    print(f"\nГотово. Коллекция '{COLLECTION_NAME}': {collection.count()} записей в {CHROMA_DIR}")


if __name__ == "__main__":
    main()
