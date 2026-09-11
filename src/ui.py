"""Оформление CLI: зелёный текст (ANSI) + звуковое сопровождение (afplay, macOS).

Звуковые файлы кладутся в sounds/ в корне проекта под именами ниже. Если файла нет —
звук просто не проигрывается (никаких ошибок), так что бот работает и без них.
"""
import os
import random
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

GREEN = "\033[92m"       # яркий зелёный — акценты, статусные фразы
DARK_GREEN = "\033[32m"  # тёмный зелёный — основной текст ответов
DIM_GREEN = "\033[2;32m" # приглушённый — вспомогательные подсказки
BOLD = "\033[1m"
RESET = "\033[0m"

# готик-индастриал палитра для второго баннера (256-цветной ANSI) — приглушённые,
# "состаренные" тона вместо ярких: латунь/бронза, потускневшее золото, кровь,
# кость/пергамент, холодный тусклый металл. Ориентир — палитра Adeptus Mechanicus
# (тёмно-красный, латунь, сталь) и общая готик-индастриал эстетика Warhammer 40k.
BRASS = "\033[1;38;5;178m"   # латунь/бронза, жирным — заголовок
BONE = "\033[38;5;223m"      # кость/пергамент — подзаголовок
BLOOD = "\033[38;5;131m"     # тусклый кровавый — акцентные символы
IRON = "\033[38;5;238m"      # тусклая сталь — структурные линии/орнамент
DULL_BRASS = "\033[38;5;136m"  # тусклая латунь без жирности — ненавязчивые подсказки команд

LETTER_GAP = 1  # межбуквенный интервал в баннере
WORD_GAP = 3    # межсловный интервал в баннере

TYPEWRITER_DELAY = 0.012  # секунд на символ

# свой блочный шрифт 5x5 для полноэкранного сплэша при старте — заливка "▓"
# (плотнее и текстурнее, чем сплошной "█") даёт более "гранитный", шершавый вид,
# нужные буквы только для "MECHANICUS"
_BLOCK_FONT = {
    "M": ["▓   ▓", "▓▓ ▓▓", "▓ ▓ ▓", "▓   ▓", "▓   ▓"],
    "E": ["▓▓▓▓▓", "▓    ", "▓▓▓  ", "▓    ", "▓▓▓▓▓"],
    "C": [" ▓▓▓▓", "▓    ", "▓    ", "▓    ", " ▓▓▓▓"],
    "H": ["▓   ▓", "▓   ▓", "▓▓▓▓▓", "▓   ▓", "▓   ▓"],
    "A": [" ▓▓▓ ", "▓   ▓", "▓▓▓▓▓", "▓   ▓", "▓   ▓"],
    "N": ["▓   ▓", "▓▓  ▓", "▓ ▓ ▓", "▓  ▓▓", "▓   ▓"],
    "I": ["▓▓▓▓▓", "  ▓  ", "  ▓  ", "  ▓  ", "▓▓▓▓▓"],
    "U": ["▓   ▓", "▓   ▓", "▓   ▓", "▓   ▓", " ▓▓▓ "],
    "S": [" ▓▓▓▓", "▓    ", " ▓▓▓ ", "    ▓", "▓▓▓▓ "],
    " ": ["  ", "  ", "  ", "  ", "  "],
}

# затухающая последовательность плотности символов для эффекта стекающих капель
_DRIP_CHARS = ["▓", "▒", "░"]

SPLASH_DURATION = 1.8  # секунд, полноэкранный сплэш держится перед стартом


def _block_text(word: str) -> list[str]:
    """Крупная надпись из блочного шрифта выше, посимвольно собранная в 5 строк."""
    glyphs = [_BLOCK_FONT.get(ch.upper(), _BLOCK_FONT[" "]) for ch in word]
    return [" ".join(g[row] for g in glyphs) for row in range(5)]


def _add_drips(lines: list[str], max_drip: int = 4) -> list[str]:
    """Добавляет под надписью несколько строк "стекающих капель": из каждой
    закрашенной колонки нижнего края буквы вниз тянется капля, плотность
    символа тает ▓ -> ▒ -> ░ по мере удаления, длина капель случайна и
    неравномерна — отсюда ощущение стекающей вязкой смолы."""
    width = len(lines[0])
    drip_rows = [[" "] * width for _ in range(max_drip)]
    for col in range(width):
        if lines[-1][col] == " ":
            continue
        if random.random() < 0.35:
            continue  # не из каждой точки капает — иначе выглядит слишком равномерно
        length = random.randint(1, max_drip)
        for row in range(length):
            char_idx = min(row, len(_DRIP_CHARS) - 1)
            drip_rows[row][col] = _DRIP_CHARS[char_idx]
    return lines + ["".join(row) for row in drip_rows]


def show_splash(word: str = "MECHANICUS", duration: float = SPLASH_DURATION) -> None:
    """Полноэкранный сплэш перед стартом программы: крупная надпись по центру
    терминала с эффектом стекающих капель под буквами, держится duration секунд,
    затем экран очищается — после этого вызывающий код показывает обычную
    компактную рамку-баннер."""
    lines = _add_drips(_block_text(word))
    letter_height = 5
    term_width, term_height = shutil.get_terminal_size(fallback=(80, 24))
    block_width = len(lines[0])
    start_row = max(0, (term_height - len(lines)) // 2)
    start_col = max(0, (term_width - block_width) // 2)

    clear_screen()
    for i, text_line in enumerate(lines):
        color = GREEN if i < letter_height else DIM_GREEN  # капли чуть приглушённее самих букв
        sys.stdout.write(f"\033[{start_row + i + 1};{start_col + 1}H{color}{text_line}{RESET}")
    sys.stdout.flush()
    time.sleep(duration)
    clear_screen()


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


def render_boot_stages(stages: list[str], current: int, frame: str = "⠋") -> str:
    """Список этапов прогрева (загрузка моделей/индексов) при холодном старте —
    пройденные помечены галочкой, текущий крутится спиннером, остальные
    приглушены. Без этого холодная загрузка (особенно с медленного носителя,
    вроде внешней флешки) выглядит как зависший терминал: reranker и
    e5-large вместе весят несколько GB и могут грузиться десятки секунд."""
    lines = [f"{BONE}Пробуждение Машинного Духа...{RESET}", ""]
    for i, label in enumerate(stages):
        if i < current:
            lines.append(f"{BRASS}  ✓ {label}{RESET}")
        elif i == current:
            lines.append(f"{BONE}  {frame} {label}...{RESET}")
        else:
            lines.append(dim(f"    {label}"))
    return "\n".join(lines)


def _spaced(text: str, letter_gap: int = LETTER_GAP, word_gap: int = WORD_GAP) -> str:
    """Разрядка текста: letter_gap пробелов между буквами, word_gap — между словами."""
    return (" " * word_gap).join((" " * letter_gap).join(word) for word in text.split(" "))


GREETING_VARIANTS = (
    "Machine Spirit awakened.",
    "Cognition matrix online.",
    "Sacred circuits humming.",
    "The Omnissiah watches.",
    "Binary rites complete.",
)

# один вариант на весь процесс — выбирается один раз при импорте, а не на каждой
# перерисовке экрана (render_banner вызывается при любом действии), иначе текст
# приветствия дёргался бы туда-сюда при каждом вопросе/команде в течение сессии
_GREETING = random.choice(GREETING_VARIANTS)


def render_banner() -> str:
    """Готик-индастриал баннер (без прямоугольной рамки): готическая арка сверху
    и зеркально снизу, "клёпаный" разделитель, орнамент из шестерён/крестов.
    Возвращает уже раскрашенную (ANSI) многоцветную строку — не оборачивать
    в green(), цвета заданы построчно внутри. Ширина подбирается под контент
    и центрируется по ширине терминала."""
    title1 = _spaced("ARCHIVE TERMINAL")
    title2 = _spaced("ADEPTUS MECHANICUS")
    info = _GREETING

    box_width = max(len(title1), len(title2), len(info)) + 14
    term_width = shutil.get_terminal_size(fallback=(box_width + 4, 24)).columns
    indent = " " * max(0, (term_width - box_width) // 2)

    def centered(text: str, color: str) -> str:
        return f"{indent}{color}{text.center(box_width)}{RESET}"

    def rivet_rule(color: str = IRON) -> str:
        body = "═" * (box_width - 4)
        return f"{indent}{color}  •{body}•  {RESET}"

    def arch(flip: bool = False) -> list[str]:
        apex = "✠"
        shoulders = ["╱   ╲", "╱       ╲", "╱           ╲"]
        rows = [apex] + shoulders
        if flip:
            rows = rows[::-1]
        return [f"{indent}{IRON}{row.center(box_width)}{RESET}" for row in rows]

    ornament = " ".join("⚙" if i % 2 == 0 else "✠" for i in range(7))

    rows = [
        *arch(),
        rivet_rule(BLOOD),
        centered("", GREEN),
        centered(title1, BRASS),
        centered(title2, BONE),
        centered("", GREEN),
        centered(ornament, IRON),
        rivet_rule(BLOOD),
        centered(info, DIM_GREEN),
        *arch(flip=True),
    ]
    return "\n".join(rows)


_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def start_spinner(redraw_fn, interval: float = 0.15):
    """Запускает текстовую анимацию "мышления" в фоновом потоке — вызывает
    redraw_fn(frame) каждые interval секунд, пока не остановлена. frame — один
    символ вращающегося индикатора. Использовать только пока основной поток
    занят чем-то блокирующим (например, ждёт ответ Groq) и не читает stdin —
    иначе анимация будет мешать вводу. Возвращает (stop_event, thread);
    останавливать строго через stop_spinner() перед любым следующим выводом."""
    stop_event = threading.Event()

    def _loop():
        i = 0
        while not stop_event.is_set():
            redraw_fn(_SPINNER_FRAMES[i % len(_SPINNER_FRAMES)])
            i += 1
            stop_event.wait(interval)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return stop_event, thread


def stop_spinner(stop_event: threading.Event, thread: threading.Thread) -> None:
    stop_event.set()
    thread.join()


def read_key() -> str:
    """Считывает одно нажатие клавиши в raw-режиме терминала (без ожидания
    Enter) и распознаёт стрелки вверх/вниз (ANSI escape-последовательности) и
    Enter. Возвращает 'up', 'down', 'enter' или сам введённый символ для
    остального. Терминал гарантированно возвращается в обычный режим даже при
    исключении (finally), иначе он остался бы "сломанным" после выхода."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x03":  # Ctrl+C в raw-режиме не поднимает KeyboardInterrupt сам
            raise KeyboardInterrupt
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            ch3 = sys.stdin.read(1) if ch2 == "[" else ""
            if ch2 == "[" and ch3 == "A":
                return "up"
            if ch2 == "[" and ch3 == "B":
                return "down"
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def select_menu(options: list, render_fn) -> int:
    """Интерактивный выбор стрелками вверх/вниз + Enter. render_fn(selected_index)
    должен полностью перерисовать экран с подсветкой текущего варианта — вызов
    происходит один раз перед началом и затем при каждом изменении выбора.
    Возвращает индекс выбранного варианта. Если stdin — не терминал (пайп,
    автотест) или в нём уже ничего нет, тихо возвращает 0 без ожидания ввода —
    иначе программа зависла бы, ожидая нажатие, которого никогда не будет."""
    if not sys.stdin.isatty():
        return 0
    selected = 0
    render_fn(selected)
    while True:
        key = read_key()
        if key == "up":
            selected = (selected - 1) % len(options)
            render_fn(selected)
        elif key == "down":
            selected = (selected + 1) % len(options)
            render_fn(selected)
        elif key == "enter":
            return selected


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
