#!/usr/bin/env bash
# Готовит venv под Linux на уже подготовленной флешке (код + данные + кэш
# моделей должны там уже лежать — скопированы туда через
# scripts/setup_portable.sh с Mac).
#
# CPU-only torch ставится явно (--index-url .../cpu) — иначе pip по
# умолчанию поставит сборку с CUDA, которая весит в разы больше без всякой
# пользы на ноуте без Nvidia GPU (см. README).
#
# venv называется .venv-linux — отдельно от Mac (.venv/.venv-x86_64) и
# Windows (.venv-win), чтобы все три могли жить на одной флешке.
#
# Использование: scripts/setup_portable_linux.sh /путь/к/флешке
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "Использование: $0 <путь-к-флешке>" >&2
    exit 1
fi

TARGET="$1/warhammer-archivist"

if [ ! -d "$TARGET" ]; then
    echo "Ошибка: не найдено '$TARGET' — сначала скопируй код и данные на флешку." >&2
    exit 1
fi
if [ ! -d "$TARGET/src" ]; then
    echo "Ошибка: '$TARGET/src' не найден — похоже, на флешке нет кода проекта." >&2
    exit 1
fi

VENV_DIR="$TARGET/.venv-linux"

if [ -d "$VENV_DIR" ]; then
    echo "==> $VENV_DIR уже на флешке — пропускаю установку зависимостей"
else
    echo "==> Создаю venv ($VENV_DIR, --copies — без symlink'ов, нужно для exFAT/FAT32)"
    python3 -m venv --copies "$VENV_DIR"

    echo "==> Ставлю CPU-only torch (без CUDA — на ноуте без Nvidia GPU обычная сборка torch с PyPI весит в разы больше без всякой пользы)"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu

    echo "==> Ставлю остальные зависимости"
    "$VENV_DIR/bin/pip" install --quiet -r "$TARGET/requirements.txt"
fi

chmod +x "$TARGET/Warhammer Chat.sh" 2>/dev/null || true

echo "==> Готово: $VENV_DIR"
echo "    Запуск: открой терминал в '$TARGET' и выполни ./'Warhammer Chat.sh'"
echo "    (двойной клик в файловом менеджере тоже может сработать — зависит от дистрибутива)"
