# Warhammer Archivist

Локальный RAG-чат-бот по лору Warhammer 40000 (цикл "Ересь Хоруса" и предыстории примархов). Отвечает на вопросы по загруженным книгам, опираясь только на их текст, с указанием источников — без сторонней информации сверх контекста.

Векторный поиск и вся RAG-логика работают полностью локально. Для генерации ответов используется Groq API (бесплатный тир).

## Как это устроено

```
book/            -> .fb2 / .epub книги (нужно наполнить самостоятельно)
book_pre_heresy/ -> книги серии "Primarchs" (опционально)
src/
  fb2_to_json.py     — конвертация книг в структурированный JSON по главам
  chunk_books.py     — нарезка текста на чанки для эмбеддингов
  embed_and_index.py — расчёт эмбеддингов и заливка в локальный ChromaDB
  retriever.py        — top-k поиск релевантных чанков по запросу
  generator.py         — промпт + вызов Groq API
  chat.py              — CLI-интерфейс
  ui.py                — оформление терминала (цвет, баннер, звук)
```

Эмбеддинги — `intfloat/multilingual-e5-base` (sentence-transformers), векторная БД — ChromaDB, локально на диске.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Создай `.env` из шаблона и впиши свой ключ Groq (бесплатно на [console.groq.com/keys](https://console.groq.com/keys)):

```bash
cp .env.example .env
```

## Наполнение базы книгами

Это единственный вручную наполняемый шаг — сам репозиторий книг не содержит (авторские права). Положи свои `.fb2`/`.epub` файлы в `book/` в формате имени `NN_Название_Автор.расширение`, затем зарегистрируй их в `BOOK_META` в `src/fb2_to_json.py` (номер, название, автор, цикл).

Дальше прогони пайплайн по порядку:

```bash
python src/fb2_to_json.py     # книги -> data/processed/books/*.json
python src/chunk_books.py     # -> data/processed/chunks.jsonl
python src/embed_and_index.py # -> data/chroma_db/ (инкрементально, повторный запуск добавит только новое)
```

## Запуск

```bash
./run.sh
```

или дважды кликнуть `Warhammer Chat.command`.

Команды внутри чата: `/exit`, `/quit` — выход, `/sources` — источники последнего ответа, `/k N` — глубина поиска.

## Опционально: звук и музыка

Положи файлы в `sounds/`: `startup.*`, `answer.*`, `exit.*`, `error.*` — короткие звуки на события, плюс любой mp3-трек как фоновая музыка (путь задаётся в `BACKGROUND_MUSIC_FILE` в `src/ui.py`). Без файлов бот работает тихо, ничего не ломается.
