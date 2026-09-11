#!/usr/bin/env bash
# Копирует бота на переносной носитель (флешка/внешний диск) для запуска на
# другом Mac того же типа процессора (Apple Silicon): код, данные (ChromaDB +
# chunks.jsonl), кэш моделей и venv с --copies (venv без --copies создаёт
# symlink'и, которых не бывает на exFAT/FAT32).
#
# Использование: scripts/setup_portable.sh /Volumes/ИмяФлешки
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Использование: $0 <путь-к-флешке>" >&2
    exit 1
fi

TARGET="$1/warhammer-archivist"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$1" ]; then
    echo "Ошибка: '$1' не существует или не примонтирован" >&2
    exit 1
fi

echo "==> Копирую код, конфиг и лаунчеры (двойной клик — не нужно вручную набирать команды)"
mkdir -p "$TARGET/data/processed"
cp -R "$PROJECT_ROOT/src" "$TARGET/"
cp -R "$PROJECT_ROOT/scripts" "$TARGET/"
cp "$PROJECT_ROOT/requirements.txt" "$TARGET/"
cp "$PROJECT_ROOT/Warhammer Chat.command" "$TARGET/"
chmod +x "$TARGET/Warhammer Chat.command"
cp "$PROJECT_ROOT/Warhammer Chat.bat" "$TARGET/"
if [ -f "$PROJECT_ROOT/.env" ]; then
    cp "$PROJECT_ROOT/.env" "$TARGET/"
else
    echo "    .env не найден — создай его на флешке вручную (GROQ_API_KEY=...)"
fi

echo "==> Копирую векторную базу и chunks.jsonl (может занять минуту)"
cp -R "$PROJECT_ROOT/data/chroma_db" "$TARGET/data/"
cp "$PROJECT_ROOT/data/processed/chunks.jsonl" "$TARGET/data/processed/"

echo "==> Копирую кэш моделей (multilingual-e5-large, bge-reranker-v2-m3)"
HF_SRC="${HF_HOME:-$HOME/.cache/huggingface}/hub"
mkdir -p "$TARGET/hf_cache/hub"
for model_dir in "models--intfloat--multilingual-e5-large" "models--BAAI--bge-reranker-v2-m3"; do
    if [ -d "$HF_SRC/$model_dir" ]; then
        cp -R "$HF_SRC/$model_dir" "$TARGET/hf_cache/hub/"
    else
        echo "    внимание: $model_dir не найден в $HF_SRC — модель скачается заново при первом запуске"
    fi
done

echo "==> Создаю venv на флешке (--copies, без symlink'ов — нужно для exFAT/FAT32)"
python3 -m venv --copies "$TARGET/.venv"
"$TARGET/.venv/bin/pip" install --quiet --upgrade pip
"$TARGET/.venv/bin/pip" install --quiet -r "$TARGET/requirements.txt"

echo "==> Готово: $TARGET"
echo "    Запуск на новом Mac (Apple Silicon): дважды кликни 'Warhammer Chat.command' на флешке"
