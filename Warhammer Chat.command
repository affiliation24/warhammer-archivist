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

# Открыть окно терминала на весь экран. Сначала пробуем нативный fullscreen
# (Ctrl+Cmd+F) через System Events; если macOS не дал разрешение на управление
# (Accessibility) — откатываемся на разворот окна до границ рабочего стола,
# для которого никаких разрешений не нужно.
osascript <<'APPLESCRIPT' >/dev/null 2>&1 || true
tell application "Terminal" to activate
delay 0.3
try
	tell application "System Events"
		keystroke "f" using {control down, command down}
	end tell
on error
	tell application "Finder"
		set screenBounds to bounds of window of desktop
	end tell
	tell application "Terminal"
		set bounds of front window to screenBounds
	end tell
end try
APPLESCRIPT

# venv называется по архитектуре процессора — на флешке может лежать сразу
# несколько (например, .venv для Apple Silicon и .venv-x86_64 для Intel Mac),
# каждый Mac подхватывает свой автоматически.
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
    VENV_DIR=".venv"
else
    VENV_DIR=".venv-$ARCH"
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "Не найден $VENV_DIR для этого Mac (архитектура: $ARCH)."
    echo "Сначала запусти scripts/setup_portable.sh на этом компьютере."
    read -n 1 -s -r -p "Нажми любую клавишу, чтобы закрыть..."
    exit 1
fi

source "$VENV_DIR/bin/activate"
cd src && python chat.py
