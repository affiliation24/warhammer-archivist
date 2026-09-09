"""CLI-интерфейс чат-бота (Этап 8 roadmap)."""
from generator import AccessDeniedError, TokensExhaustedError, answer
from ui import (
    clear_screen,
    dim,
    green,
    play_sound,
    render_banner,
    render_sources,
    start_background_music,
    stop_background_music,
    type_out,
)

COMMANDS_HINT = """
  /exit, /quit — прервать связь с терминалом
  /sources     — явить источники последнего ответа
  /k N         — изменить глубину поиска в архиве (сейчас 6)
"""

EXIT_COMMANDS = ("/exit", "/quit")

FAREWELL = "Связь с терминалом прервана. Да пребудет с Вами Святая Омнисия."
PROCESSING = "Машинный Дух обрабатывает запрос..."


def _redraw(body: str = "") -> None:
    """Очищает экран и перерисовывает баннер + подсказки команд + содержимое —
    баннер всегда остаётся на месте вне зависимости от объёма предыдущего вывода."""
    clear_screen()
    print(green(render_banner()))
    print(green(COMMANDS_HINT))
    if body:
        print(body)


def main():
    _redraw()
    play_sound("startup")
    start_background_music()
    k = 6
    last_chunks = []

    try:
        _run(k, last_chunks)
    finally:
        stop_background_music()


def _run(k, last_chunks):
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            _redraw(green(FAREWELL))
            play_sound("exit")
            break

        if not question:
            continue
        if question in EXIT_COMMANDS:
            _redraw(green(FAREWELL))
            play_sound("exit")
            break
        if question == "/sources":
            _redraw(render_sources(last_chunks))
            continue
        if question.startswith("/k "):
            try:
                k = int(question.split()[1])
                _redraw(dim(f"Глубина поиска установлена: k = {k}"))
            except (IndexError, ValueError):
                _redraw(dim("Использование: /k 8"))
            continue

        echo = dim(f"> {question}")
        _redraw(f"{echo}\n\n{dim(PROCESSING)}")

        try:
            reply, chunks = answer(question, k=k)
        except TokensExhaustedError as e:
            _redraw(f"{echo}\n\n{green(str(e))}")
            play_sound("error")
            continue
        except AccessDeniedError as e:
            _redraw(f"{echo}\n\n{green(str(e))}")
            play_sound("error")
            continue
        except RuntimeError as e:
            _redraw(f"{echo}\n\n{dim(f'Ошибка: {e}')}")
            continue
        except Exception as e:
            _redraw(f"{echo}\n\n{dim(f'Ошибка обращения к Groq: {e}')}")
            continue

        last_chunks = chunks
        _redraw(echo)
        type_out(reply)
        print(dim("(наберите /sources, чтобы увидеть источники ответа)"))
        play_sound("answer")


if __name__ == "__main__":
    main()
