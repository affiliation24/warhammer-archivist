"""Чанкинг книг (Этап 3 roadmap).

Читает data/processed/books/*.json (результат fb2_to_json.py), режет текст каждой
секции/главы на чанки по абзацам с overlap, пишет плоский data/processed/chunks.jsonl —
по одному JSON-объекту на строку, готово для эмбеддингов (Этап 4).

Режем строго внутри границ секции (главы не смешиваются), это то, ради чего
на Этапе 1 сохранялась структура <section> вместо плоского текста.
"""
import json
import glob
from pathlib import Path

BOOKS_DIR = Path(__file__).resolve().parent.parent / "data" / "processed" / "books"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "chunks.jsonl"

TARGET_CHARS = 1600   # ~ 400 токенов русского текста
OVERLAP_CHARS = 200


def split_section(text: str) -> list[str]:
    """Режет текст секции на чанки ~TARGET_CHARS по границам абзацев,
    с overlap ~OVERLAP_CHARS (последние предложения предыдущего чанка
    переносятся в начало следующего)."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        # одиночный абзац длиннее целого чанка — режем его отдельно по предложениям
        if len(para) > TARGET_CHARS * 1.5:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            chunks.extend(_split_long_paragraph(para))
            continue

        if current_len + len(para) > TARGET_CHARS and current:
            chunks.append("\n\n".join(current))
            # overlap: переносим хвост последнего абзаца в новый чанк
            tail = current[-1][-OVERLAP_CHARS:]
            current = [tail, para]
            current_len = len(tail) + len(para)
        else:
            current.append(para)
            current_len += len(para)

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def _split_long_paragraph(para: str) -> list[str]:
    sentences = para.replace("! ", "!\n").replace("? ", "?\n").replace(". ", ".\n").split("\n")
    chunks, current, current_len = [], [], 0
    for s in sentences:
        if current_len + len(s) > TARGET_CHARS and current:
            chunks.append(" ".join(current))
            current, current_len = [], 0
        current.append(s)
        current_len += len(s)
    if current:
        chunks.append(" ".join(current))
    return chunks


def main():
    book_files = sorted(glob.glob(str(BOOKS_DIR / "*.json")))
    print(f"Найдено {len(book_files)} книг для чанкинга")

    total_chunks = 0
    with open(OUT_PATH, "w", encoding="utf-8") as out:
        for bf in book_files:
            book = json.load(open(bf, encoding="utf-8"))
            slug = Path(bf).stem
            chunk_idx = 0
            for section in book["sections"]:
                pieces = split_section(section["text"])
                for piece in pieces:
                    record = {
                        "chunk_id": f"{slug}_{chunk_idx:04d}",
                        "source_type": "book",
                        "source_title": book["title"],
                        "author": book["author"],
                        "sequence_number": book["sequence_number"],
                        "cycle": book.get("cycle", "horus_heresy"),
                        "chapter_title": section["chapter_title"],
                        "chunk_index_in_source": chunk_idx,
                        "text": piece,
                    }
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    chunk_idx += 1
                    total_chunks += 1
            print(f"  {book['title']}: {chunk_idx} чанков")

    print(f"\nГотово: {total_chunks} чанков -> {OUT_PATH}")


if __name__ == "__main__":
    main()
