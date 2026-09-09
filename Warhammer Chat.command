#!/bin/bash
# Запуск чат-бота. Работает независимо от того, откуда вызван —
# включая запуск через симлинк/алиас (например, ярлык на рабочем столе),
# поэтому разрешаем цепочку симлинков до реального пути скрипта.
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

cd "$DIR" || exit 1
source .venv/bin/activate
cd src && python chat.py
