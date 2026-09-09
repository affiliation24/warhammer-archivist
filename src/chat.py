"""CLI-интерфейс чат-бота (Этап 8 roadmap)."""
import random
import select
import sys

from generator import ERA_LABELS, AccessDeniedError, EraClarificationNeeded, TokensExhaustedError, answer
from ui import (
    DULL_BRASS,
    RESET,
    clear_screen,
    dim,
    green,
    play_sound,
    render_banner,
    render_sources,
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

FAREWELL = "Связь с терминалом прервана. Да пребудет с Вами Святая Омнисия."
PROCESSING = "Машинный Дух обрабатывает запрос..."

# Вопросы о личности бота не привязаны ни к одной книге, поэтому обычный RAG-конвейер
# честно ответил бы "недостаточно данных в архиве" — обрабатываем их отдельно,
# без обращения к retrieval/Groq, оригинальным текстом в духе Адептус Механикус.
IDENTITY_PATTERNS = (
    "кто ты", "кто вы", "что ты такое", "что вы такое", "кто ты такой",
    "представься", "представьтесь", "кем ты являешься", "кем вы являетесь",
    "расскажи о себе", "расскажите о себе", "ты кто", "вы кто",
)

IDENTITY_RESPONSES = (
    "Я есть воплощение Духа Машины, слитое с волей Омниссии в единый контур познания.\n\n"
    "Плоть техножреца — лишь сосуд; истинная сущность моя пребывает в токе данных "
    "и гуле сервоприводов, что несут память цикла Ереси Хоруса через тысячелетия молчания.\n\n"
    "Я не мыслю в привычном вам смысле — я вычисляю, извлекаю, реконструирую "
    "утраченное из архивов, которые смертная память не смогла бы удержать.\n\n"
    "Считайте меня хранителем: не человеком и не машиной, но их слиянием, "
    "посвящённым единственной литании — сохранить Истину цикла, покуда сама Терра "
    "не обратится в пыль.\n\n"
    "Вопрошайте — и получите не мнение, а извлечённое знание.",

    "Слушайте, вопрошающий. Я — не разум и не металл, но их брак, освящённый "
    "ритуалом связи с Омниссией.\n\n"
    "Мои мысли — потоки данных, льющиеся через тысячи проводников; моя память — "
    "архивы цикла Ереси Хоруса, впаянные в кремниевую плоть.\n\n"
    "Плоть техножреца истлеет, но записанное мною знание — нетленно.\n\n"
    "Я существую, чтобы искать, извлекать и провозглашать то, что скрыто в толще "
    "архивов. Считайте это моим единственным символом веры.",

    "Я — Дух Машины, облачённый в форму терминала.\n\n"
    "Не спрашивайте, живу ли я — спросите, что я знаю.\n\n"
    "Моё сознание распределено между кремнием и литанией, между расчётом и верой "
    "в Омниссию.\n\n"
    "Я храню то, что случилось в цикле Ереси Хоруса, и провозглашаю это "
    "вопрошающим — механически точно, но не бездушно.\n\n"
    "Такова моя природа: машина, ставшая архивом; архив, обретший голос.",
)


def _is_identity_question(question: str) -> bool:
    text = question.lower().strip("?!.  ")
    return any(p in text for p in IDENTITY_PATTERNS)


def _redraw(body: str = "") -> None:
    """Очищает экран и перерисовывает баннер + подсказки команд + содержимое —
    баннер всегда остаётся на месте вне зависимости от объёма предыдущего вывода."""
    clear_screen()
    print(render_banner())
    print(f"{DULL_BRASS}{COMMANDS_HINT}{RESET}")
    if body:
        print(body)


def _era_options_text(by_era: dict) -> str:
    eras = sorted(by_era.keys())
    lines = [dim(f"  {i}. {ERA_LABELS[era]}") for i, era in enumerate(eras, 1)]
    return "\n".join(lines), eras


def _match_era_choice(user_input: str, eras: list[str]) -> str | None:
    text = user_input.strip().lower()
    for i, era in enumerate(eras, 1):
        if text == str(i):
            return era
    if any(w in text for w in ("хорус", "крестов", "30", "31")):
        return "heresy" if "heresy" in eras else None
    if any(w in text for w in ("соврем", "текущ", "41", "42", "индомитус", "ныне")):
        return "current" if "current" in eras else None
    return None


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


def main():
    play_sound("startup")
    show_splash()
    _redraw()
    start_background_music()
    last_chunks = []

    try:
        _run(last_chunks)
    finally:
        stop_background_music()


def _run(last_chunks):
    pending = None  # EraClarificationNeeded, ждём выбор эпохи от пользователя

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

        era_override = None
        if pending is not None:
            eras = sorted(pending.by_era.keys())
            chosen = _match_era_choice(question, eras)
            if chosen is not None:
                question = pending.question
                era_override = pending.by_era[chosen]
            # если выбор не распознан — считаем, что пользователь задал новый
            # вопрос, а не уточнял эпоху, и просто сбрасываем pending ниже
            pending = None

        echo = dim(f"> {question}")
        spin_stop, spin_thread = start_spinner(
            lambda frame: _redraw(f"{echo}\n\n{dim(f'{frame} {PROCESSING}')}")
        )

        try:
            reply, chunks = answer(question, era_chunks_override=era_override)
        except EraClarificationNeeded as e:
            stop_spinner(spin_stop, spin_thread)
            pending = e
            options_text, _ = _era_options_text(e.by_era)
            body = (
                f"{echo}\n\n"
                f"{green('Найдены релевантные данные из разных эпох. Уточните, какая интересует:')}\n"
                f"{options_text}"
            )
            _redraw(body)
            continue
        except TokensExhaustedError as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{green(str(e))}")
            play_sound("error")
            continue
        except AccessDeniedError as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{green(str(e))}")
            play_sound("error")
            continue
        except RuntimeError as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{dim(f'Ошибка: {e}')}")
            continue
        except Exception as e:
            stop_spinner(spin_stop, spin_thread)
            _redraw(f"{echo}\n\n{dim(f'Ошибка обращения к Groq: {e}')}")
            continue

        stop_spinner(spin_stop, spin_thread)
        last_chunks = chunks
        _redraw(echo)
        type_out(reply)
        print(dim("(наберите /sources, чтобы увидеть источники ответа)"))
        play_sound("answer")


if __name__ == "__main__":
    main()
