#!/usr/bin/env bash
# Запуск чат-бота на Linux. Двойной клик в файловом менеджере может сразу
# открыть терминал и запустить это (зависит от дистрибутива/настроек) —
# если нет, открой терминал в этой папке и выполни: ./"Warhammer Chat.sh"
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
done
DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

cd "$DIR" || exit 1

if [ ! -d ".venv-linux" ]; then
    echo "Не найден .venv-linux на этой флешке."
    echo "Сначала запусти: scripts/setup_portable_linux.sh /путь/к/флешке"
    read -n 1 -s -r -p "Нажми любую клавишу, чтобы закрыть..."
    exit 1
fi

source .venv-linux/bin/activate
cd src && python chat.py
