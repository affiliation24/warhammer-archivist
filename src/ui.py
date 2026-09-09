"""Оформление CLI: зелёный текст (ANSI) + звуковое сопровождение (afplay, macOS).

Звуковые файлы кладутся в sounds/ в корне проекта под именами ниже. Если файла нет —
звук просто не проигрывается (никаких ошибок), так что бот работает и без них.
"""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

GREEN = "\033[92m"       # яркий зелёный — баннер, акценты, статусные фразы
DARK_GREEN = "\033[32m"  # тёмный зелёный — основной текст ответов
DIM_GREEN = "\033[2;32m" # приглушённый — вспомогательные подсказки
BOLD = "\033[1m"
RESET = "\033[0m"

LETTER_GAP = 1  # межбуквенный интервал в баннере
WORD_GAP = 3    # межсловный интервал в баннере

TYPEWRITER_DELAY = 0.012  # секунд на символ

SOUNDS_DIR = Path(__file__).resolve().parent.parent / "sounds"

# событие -> имя файла в sounds/ (любое расширение, которое понимает afplay: mp3/wav/m4a...)
SOUND_FILES = {
    "startup": "startup",
    "answer": "answer",
    "exit": "exit",
    "error": "error",
}

# фоновая музыка, зацикленная на всё время работы CLI
BACKGROUND_MUSIC_FILE = SOUNDS_DIR / "warhammer_40000_mechanicus_02_Caestus_Metalican.mp3"

_background_process: subprocess.Popen | None = None


def clear_screen() -> None:
    """Очищает видимую область терминала (не трогает scrollback-буфер —
    прокрутка колесом мыши вверх всё ещё покажет историю)."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def green(text: str) -> str:
    return f"{GREEN}{text}{RESET}"


def dim(text: str) -> str:
    return f"{DIM_GREEN}{text}{RESET}"


def _spaced(text: str, letter_gap: int = LETTER_GAP, word_gap: int = WORD_GAP) -> str:
    """Разрядка текста: letter_gap пробелов между буквами, word_gap — между словами."""
    return (" " * word_gap).join((" " * letter_gap).join(word) for word in text.split(" "))


def render_banner() -> str:
    """Статичный баннер в рамке из псевдографики, отцентрованный по ширине терминала.
    Ширина рамки подбирается автоматически под самую длинную строку содержимого,
    затем вся рамка центрируется горизонтальным отступом."""
    title1 = _spaced("ARCHIVE TERMINAL")
    title2 = _spaced("ADEPTUS MECHANICUS")
    info = "Machine Spirit awakened."

    box_width = max(len(title1), len(title2), len(info)) + 8
    term_width = shutil.get_terminal_size(fallback=(box_width + 4, 24)).columns
    indent = " " * max(0, (term_width - box_width) // 2)

    def line(content: str = "", align: str = "center") -> str:
        inner = box_width - 2
        if align == "center":
            content = content.center(inner)
        else:
            content = content.ljust(inner)
        return f"{indent}║{content}║"

    top = f"{indent}╔{'═' * (box_width - 2)}╗"
    bottom = f"{indent}╚{'═' * (box_width - 2)}╝"
    sep = f"{indent}╟{'─' * (box_width - 2)}╢"

    rows = [
        top,
        line(),
        line(title1),
        line(title2),
        line(),
        sep,
        line(),
        line(info),
        line(),
        bottom,
    ]
    return "\n".join(rows)


def type_out(text: str, delay: float = TYPEWRITER_DELAY, color: str = DARK_GREEN) -> None:
    """Печатает текст посимвольно с задержкой, эффект печатной машинки."""
    sys.stdout.write(color)
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write(RESET + "\n")
    sys.stdout.flush()


def render_sources(chunks: list[dict]) -> str:
    """Пронумерованный, читаемый список источников для команды /sources."""
    if not chunks:
        return dim("Источников пока нет — сначала задайте вопрос.")
    lines = [f"{DIM_GREEN}--- ИСТОЧНИКИ ПОСЛЕДНЕГО ЗАПРОСА ---{RESET}"]
    for i, c in enumerate(chunks, 1):
        parts = [p.strip() for p in (c["chapter_title"] or "").split("\n") if p.strip()]
        chapter = " / ".join(parts) or "без главы"
        if len(chapter) > 60:
            chapter = chapter[:57] + "..."
        lines.append(
            f"{DARK_GREEN}  {i}. {c['source_title']}{RESET}"
            f"{DIM_GREEN}  ({chapter}, релевантность {c['score']:.2f}){RESET}"
        )
    return "\n".join(lines)


def play_sound(event: str) -> None:
    """Проигрывает звук для события в фоне, не блокируя CLI. Тихо ничего не делает,
    если подходящего файла нет (пользователь ещё не положил звуки в sounds/)."""
    base_name = SOUND_FILES.get(event)
    if not base_name or not SOUNDS_DIR.exists():
        return
    for path in SOUNDS_DIR.glob(f"{base_name}.*"):
        try:
            subprocess.Popen(
                ["afplay", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # afplay недоступен (не macOS) — просто пропускаем
        return


def kill_orphaned_background_music() -> None:
    """Убивает "осиротевшие" процессы фоновой музыки от прошлых сессий, которые
    не завершились штатно (например, окно терминала закрыли напрямую, минуя
    /exit — тогда finally в chat.py не успевает отработать, и цикл afplay
    остаётся висеть в фоне навсегда, т.к. запущен в своей сессии)."""
    try:
        subprocess.run(
            ["pkill", "-f", str(BACKGROUND_MUSIC_FILE)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # pkill недоступен — просто пропускаем


def start_background_music() -> None:
    """Запускает фоновую музыку на репите в отдельном процессе (shell-цикл вокруг
    afplay — сам afplay не умеет зацикливать). Не блокирует CLI. Тихо ничего не
    делает, если файла нет или afplay недоступен."""
    global _background_process
    if not BACKGROUND_MUSIC_FILE.exists():
        return
    kill_orphaned_background_music()
    try:
        _background_process = subprocess.Popen(
            ["sh", "-c", f'while true; do afplay "{BACKGROUND_MUSIC_FILE}"; done'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        pass


def stop_background_music() -> None:
    global _background_process
    if _background_process is None:
        return
    try:
        # процесс — обёртка "sh -c 'while true; do afplay ...; done'";
        # убиваем всю группу, иначе останется висеть текущий afplay внутри цикла
        os.killpg(os.getpgid(_background_process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _background_process = None
