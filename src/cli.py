"""Классический CLI-интерфейс чат-бота (fallback, если Textual недоступен).

Основной интерфейс теперь — Textual-TUI в tui.py (запускается из chat.py),
этот модуль сохранён как запасной вариант для случаев, когда TUI невозможен
(нет терминала, stdin — пайп, Textual не установлен).
"""
import random
import select
import sys
import time

from generator import ERA_LABELS, AccessDeniedError, EraClarificationNeeded, TokensExhaustedError, answer
from persona import IDENTITY_PATTERNS, IDENTITY_RESPONSES, _is_identity_question
from retriever import BOOT_STAGES, models_cached, warm_up
from ui import (
    DULL_BRASS,
    RESET,
    clear_screen,
    dim,
    green,
    play_sound,
    render_banner,
    render_boot_stages,
    render_sources,
    select_menu,
    show_splash,
    start_background_music,
    start_spinner,
    stop_background_music,
    stop_spinner,
    type_out,
)

COMMANDS_HINT = """
  /exit    — прервать связь с терминалом
  /sources — явить источники последнего ответа
"""

EXIT_COMMANDS = ("/exit", "/quit")

# Команды fallback-CLI — используются Tab-автодополнением readline.
COMMANDS = ("/exit", "/quit", "/sources")

FAREWELL = "Связь с терминалом прервана. Да пребудет с Вами Святая Омнисия."
PROCESSING = "Машинный Дух обрабатывает запрос..."

# Вопросы о личности бота и канонические ответы на них — в persona.py,
# общие для CLI и TUI-интерфейсов.


def _install_tab_completion() -> None:
    """Автодополнение команд по Tab для встроенного input().

    Работает через readline, который есть только на POSIX-системах; на Windows
    модуль отсутствует — там Tab сохраняет стандартное поведение терминала.
    """
    try:
        import readline
    except ImportError:
        return

    def _complete(text: str, state: int) -> str | None:
        matches = [cmd for cmd in COMMANDS if cmd.startswith(text)]
        return matches[state] if state < len(matches) else None

    readline.set_completer(_complete)
    # Без этого «/» считается разделителем слова, и completer получает «s»
    # вместо «/s» — команды с косой чертой не находятся.
    readline.set_completer_delims(" \t\n")
    # На macOS Python использует libedit, где «tab: complete» не привязывает
    # Tab; GNU readline (Linux) понимает стандартный синтаксис.
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def _redraw(body: str = "") -> None:
    """Очищает экран и перерисовывает баннер + подсказки команд + содержимое —
    баннер всегда остаётся на месте вне зависимости от объёма предыдущего вывода."""
    clear_screen()
    print(render_banner())
    print(f"{DULL_BRASS}{COMMANDS_HINT}{RESET}")
    if body:
        print(body)


def _era_menu_body(echo: str, eras: list[str], selected: int) -> str:
    lines = [
        echo,
        "",
        green("Найдены релевантные данные из разных эпох. Выберите (↑/↓, Enter):"),
    ]
    for i, era in enumerate(eras):
        label = ERA_LABELS[era]
        if i == selected:
            lines.append(green(f"  → {label}"))
        else:
            lines.append(dim(f"    {label}"))
    return "\n".join(lines)


def _drain_pasted_lines() -> int:
    """input() читает только первую строку — если пользователь вставил
    многострочный текст (например, случайно скопировал вопрос вместе с
    предыдущим ответом бота), остальные строки остаются в буфере stdin и на
    следующих итерациях цикла подхватываются как отдельные новые "вопросы".
    Так как это обычно фрагменты предыдущего ответа, они снова находят похожий
    контент — выглядит как "бот бесконечно отвечает на один и тот же вопрос".
    Забираем и отбрасываем всё, что уже лежит в буфере stdin (пришло с тем же
    вставленным блоком), не дожидаясь новых нажатий Enter от пользователя."""
    drained = 0
    while select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if not line:
            break
        drained += 1
    return drained


def _boot() -> None:
    """Явно прогревает модели/индексы с индикацией прогресса по этапам —
    без этого холодная загрузка (особенно с медленного носителя) молча висит
    на первом вопросе, и непонятно, идёт загрузка или терминал завис.

    Проценты — не реальный прогресс байт (модели не отдают такой колбэк), а
    интерполяция по ожидаемой длительности каждого этапа (EXPECTED_SECONDS,
    грубая прикидка: reranker и BM25-индекс заметно дольше остальных). На
    саму смену этапа это не влияет — она наступает по факту завершения
    соответствующего вызова в warm_up(), интерполяция только сглаживает
    процент внутри текущего этапа между двумя такими фактическими сменами."""
    labels = [label for _, label in BOOT_STAGES]
    expected_seconds = [1.0, 4.0, 5.0, 90.0]
    total_seconds = sum(expected_seconds)
    cumulative_before = [sum(expected_seconds[:i]) for i in range(len(expected_seconds))]

    current = [0]
    stage_started_at = [time.monotonic()]

    def percent_now() -> float:
        elapsed = time.monotonic() - stage_started_at[0]
        expected = expected_seconds[current[0]]
        frac = min(elapsed / expected, 0.97) if expected > 0 else 0.97
        done_seconds = cumulative_before[current[0]] + expected * frac
        return done_seconds / total_seconds * 100

    spin_stop, spin_thread = start_spinner(
        lambda frame: _redraw(render_boot_stages(labels, current[0], frame, percent_now()))
    )

    def on_stage(stage_key: str) -> None:
        current[0] = next(i for i, (key, _) in enumerate(BOOT_STAGES) if key == stage_key)
        stage_started_at[0] = time.monotonic()

    try:
        warm_up(on_stage=on_stage)
    finally:
        stop_spinner(spin_stop, spin_thread)
    _redraw(render_boot_stages(labels, len(labels), percent=100.0))


def main():
    _install_tab_completion()
    play_sound("startup")
    show_splash()
    # Экран прогрева с прогресс-баром показываем только на холодном старте
    # (первый запуск на новой машине, модели ещё не скачаны) — если веса уже
    # в кэше, прогрев занимает секунды и молча происходит при первом вопросе,
    # отдельный экран тут был бы лишним ритуалом на каждый обычный запуск.
    if not models_cached():
        _boot()
    _redraw()
    start_background_music()
    last_chunks = []

    try:
        _run(last_chunks)
    finally:
        stop_background_music()


def _resolve_and_answer(question: str):
    """Обрабатывает один вопрос целиком, включая цикл уточнения эпохи —
    неоднозначность разрешается сразу же через интерактивное меню (стрелки +
    Enter), а не ожиданием следующего свободного ввода в основном цикле.
    Возвращает (reply, chunks, echo) при успехе или None, если по пути
    произошла ошибка (уже отображена пользователю на экране)."""
    era_override = None
    while True:
        echo = dim(f"> {question}")
        spin_stop, spin_thread = start_spinner(
            lambda frame: _redraw(f"{echo}\n\n{dim(f'{frame} {PROCESSING}')}")
        )
        try:
            reply, chunks = answer(question, era_chunks_override=era_override)
        except EraClarificationNeeded as e:
            stop_spinner(spin_stop, spin_thread)
            eras = sorted(e.by_era.keys())
            idx = select_menu(eras, lambda i: _redraw(_era_menu_body(echo, eras, i)))
            question = e.question
            era_override = e.by_era[eras[idx]]
            continue
        except TokensExhaustedError as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{green(str(e))}")
            play_sound("error")
            return None
        except AccessDeniedError as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{green(str(e))}")
            play_sound("error")
            return None
        except RuntimeError as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{dim(f'Ошибка: {e}')}")
            return None
        except Exception as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{dim(f'Ошибка обращения к DeepSeek: {e}')}")
            return None
        stop_spinner(spin_stop, spin_thread)
        return reply, chunks, echo


def _run(last_chunks):
    while True:
        try:
            question = input("\n> ").strip()
            extra = _drain_pasted_lines()
        except (EOFError, KeyboardInterrupt):
            _redraw(green(FAREWELL))
            play_sound("exit")
            break

        if extra:
            print(dim(
                f"(во вставленном тексте было несколько строк — учтена только "
                f"первая, остальные {extra} отброшены)"
            ))

        if not question:
            continue
        if question in EXIT_COMMANDS:
            _redraw(green(FAREWELL))
            play_sound("exit")
            break
        if question == "/sources":
            _redraw(render_sources(last_chunks))
            continue
        if _is_identity_question(question):
            echo = dim(f"> {question}")
            _redraw(echo)
            type_out(random.choice(IDENTITY_RESPONSES))
            play_sound("answer")
            continue

        result = _resolve_and_answer(question)
        if result is None:
            continue
        reply, chunks, echo = result

        last_chunks = chunks
        _redraw(echo)
        play_sound("answer")
        type_out(reply.replace("**", ""))  # в CLI жирный не рендерится — снимаем звёздочки
        print(dim("(наберите /sources, чтобы увидеть источники ответа)"))


if __name__ == "__main__":
    main()
