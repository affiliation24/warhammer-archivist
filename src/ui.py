"""Оформление CLI: зелёный текст (ANSI) + звуковое сопровождение (afplay на
macOS, PowerShell/WPF MediaPlayer на Windows, первый найденный из mpg123/
ffplay/mpv/cvlc на Linux — см. _sound_player_cmd).

Звуковые файлы кладутся в sounds/ в корне проекта под именами ниже. Если файла нет,
или на этой ОС не нашлось ни одного проигрывателя — звук просто не проигрывается
(никаких ошибок), так что бот работает и без них.
"""
import random
import shutil
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

# PowerShell-скрипт для Windows: WPF MediaPlayer умеет mp3 из коробки (в
# отличие от System.Media.SoundPlayer, который только wav), но не отдаёт
# длительность сразу после Open() — она подгружается асинхронно, поэтому
# ждём HasTimeSpan в цикле перед тем как ждать реального времени трека.
_WINDOWS_PLAY_SCRIPT = (
    "Add-Type -AssemblyName presentationCore; "
    "$p = New-Object system.windows.media.mediaplayer; "
    "$p.Open([uri]'{path}'); "
    "while (-not $p.NaturalDuration.HasTimeSpan) {{ Start-Sleep -Milliseconds 100 }}; "
    "$p.Play(); "
    "Start-Sleep -Seconds ([math]::Ceiling($p.NaturalDuration.TimeSpan.TotalSeconds)); "
    "$p.Stop()"
)

# Линуксовые дистрибутивы не гарантируют общего проигрывателя "из коробки" —
# перебираем то, что реально может стоять, и берём первое найденное.
_LINUX_PLAYERS = (
    ("mpg123", ["-q"]),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ("mpv", ["--no-video", "--really-quiet"]),
    ("cvlc", ["--play-and-exit", "-q"]),
)


def _sound_player_cmd(path: str) -> list[str] | None:
    """Команда для одноразового (блокирующего до конца трека) воспроизведения
    файла на этой ОС, или None, если подходящего плеера не нашлось —
    воспроизведение тогда молча пропускается, как и при отсутствии самого
    файла (см. модульный docstring)."""
    if sys.platform == "darwin":
        return ["afplay", path]
    if sys.platform == "win32":
        script = _WINDOWS_PLAY_SCRIPT.format(path=path.replace("'", "''"))
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script]
    for player, args in _LINUX_PLAYERS:
        found = shutil.which(player)
        if found:
            return [found, *args, path]
    return None


_background_stop: threading.Event | None = None
_background_thread: threading.Thread | None = None
_background_proc: subprocess.Popen | None = None
_background_lock = threading.Lock()


def clear_screen() -> None:
    """Очищает видимую область терминала (не трогает scrollback-буфер —
    прокрутка колесом мыши вверх всё ещё покажет историю)."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def green(text: str) -> str:
    return f"{GREEN}{text}{RESET}"


def dim(text: str) -> str:
    return f"{DIM_GREEN}{text}{RESET}"


_PROGRESS_LINE_WIDTH = 56  # под ширину строки боевого баннера ниже


def render_progress_bar(percent: float, width: int = 24) -> str:
    """Полоска загрузки вида [▓▓▓▓░░░░] NN% — выровнена по правому краю
    строки фиксированной ширины (см. _PROGRESS_LINE_WIDTH), чтобы смотреться
    как HUD-индикатор, а не просто текст в подвале."""
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100))
    bar_plain = f"[{'▓' * filled}{'░' * (width - filled)}]"
    percent_plain = f" {percent:3.0f}%"
    pad = max(0, _PROGRESS_LINE_WIDTH - len(bar_plain) - len(percent_plain))
    return f"{' ' * pad}{BRASS}{bar_plain}{RESET}{BONE}{percent_plain}{RESET}"


def render_boot_stages(stages: list[str], current: int, frame: str = "⠋", percent: float = 0.0) -> str:
    """Список этапов прогрева (загрузка моделей/индексов) при холодном старте —
    пройденные помечены галочкой, текущий крутится спиннером, остальные
    приглушены, справа — общая полоска прогресса с процентами. Без этого
    холодная загрузка (особенно с медленного носителя, вроде внешней флешки)
    выглядит как зависший терминал: reranker и e5-large вместе весят
    несколько GB и могут грузиться десятки секунд."""
    lines = [f"{BONE}Пробуждение Машинного Духа...{RESET}", ""]
    for i, label in enumerate(stages):
        if i < current:
            lines.append(f"{BRASS}  ✓ {label}{RESET}")
        elif i == current:
            lines.append(f"{BONE}  {frame} {label}...{RESET}")
        else:
            lines.append(dim(f"    {label}"))
    lines.append("")
    lines.append(render_progress_bar(percent))
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
    если подходящего файла нет или на этой ОС не нашлось проигрывателя."""
    base_name = SOUND_FILES.get(event)
    if not base_name or not SOUNDS_DIR.exists():
        return
    for path in SOUNDS_DIR.glob(f"{base_name}.*"):
        cmd = _sound_player_cmd(str(path))
        if cmd is None:
            return
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass  # проигрыватель пропал между which() и запуском — крайне маловероятно, но не критично
        return


def kill_orphaned_background_music() -> None:
    """Убивает "осиротевшие" процессы фоновой музыки от прошлых сессий, которые
    не завершились штатно (например, окно терминала закрыли напрямую, минуя
    /exit — тогда finally в chat.py не успевает отработать). pkill есть на
    macOS и обычно на Linux; на Windows его нет — тихо пропускаем, там
    осиротевший процесс не так критичен (закрытие окна терминала обычно и
    завершает дочерний powershell)."""
    try:
        subprocess.run(
            ["pkill", "-f", str(BACKGROUND_MUSIC_FILE)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # pkill недоступен (Windows) — просто пропускаем


def start_background_music() -> None:
    """Запускает фоновую музыку на репите в фоновом потоке (не shell-цикле —
    команда воспроизведения разная на каждой ОС, см. _sound_player_cmd).
    Не блокирует CLI. Тихо ничего не делает, если файла нет или на этой ОС
    не нашлось проигрывателя."""
    global _background_stop, _background_thread
    if not BACKGROUND_MUSIC_FILE.exists():
        return
    cmd = _sound_player_cmd(str(BACKGROUND_MUSIC_FILE))
    if cmd is None:
        return
    kill_orphaned_background_music()

    _background_stop = threading.Event()

    def _loop(stop_event: threading.Event) -> None:
        global _background_proc
        while True:
            with _background_lock:
                if stop_event.is_set():
                    return
                try:
                    _background_proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                except FileNotFoundError:
                    return
            _background_proc.wait()

    _background_thread = threading.Thread(target=_loop, args=(_background_stop,), daemon=True)
    _background_thread.start()


def stop_background_music() -> None:
    """Останавливает музыку и гарантированно не оставляет висящий процесс:
    _background_lock синхронизирует это с _loop() выше — иначе возможна
    гонка (проверили stop_event, ещё не успели запустить/сохранить в
    _background_proc новый процесс — а stop() тем временем уже прочитал
    старое значение и завершился, новый процесс остаётся играть трек
    до конца, никем не остановленный)."""
    global _background_stop, _background_thread, _background_proc
    if _background_stop is None:
        return
    with _background_lock:
        _background_stop.set()
        if _background_proc is not None:
            try:
                _background_proc.terminate()
            except (ProcessLookupError, PermissionError):
                pass
    _background_stop = None
    _background_thread = None
    _background_proc = None
