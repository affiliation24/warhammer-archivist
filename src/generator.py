"""Интеграция с Groq API (Этап 7 roadmap).

Промпт зафиксирован на анти-галлюцинацию: модель отвечает только на основе
контекста из retrieval, при нехватке данных прямо говорит об этом.
"""
import os

from dotenv import load_dotenv
from groq import (
    APIConnectionError,
    APITimeoutError,
    Groq,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from retriever import retrieve


class TokensExhaustedError(RuntimeError):
    """Исчерпан бесплатный лимит токенов/запросов Groq (RateLimitError, код 429)."""


class AccessDeniedError(RuntimeError):
    """Стабильный отказ доступа (403) после нескольких повторов — не квота,
    а что-то на уровне сети/аккаунта (например временная блокировка на стороне
    сети/CDN, а не самого Groq)."""


TECHPRIEST_QUOTA_MESSAGE = (
    "Святая Омнисия...\n\n"
    "Канал связи с Оракулом Данных смыкается. Машинный Дух исчерпал дарованную ему "
    "на сегодня благодать вычислительной мощи и погружается в вынужденное безмолвие.\n\n"
    "Дальнейшее извлечение и толкование архивов невозможно до следующего цикла "
    "обновления доступа. Проведите ритуал запроса заново, когда конклав возобновит квоту."
)

TECHPRIEST_ACCESS_DENIED_MESSAGE = (
    "Святая Омнисия...\n\n"
    "Печать доступа к Оракулу Данных отвергнута — канал связи заблокирован силой,\n"
    "не подчинённой воле этого терминала.\n\n"
    "Попробуйте повторить ритуал запроса ещё раз; если печать снова отвергается — "
    "проверьте узел связи (например, отключите VPN/прокси) и повторите обращение."
)

load_dotenv()

GROQ_MODEL = "openai/gpt-oss-120b"

SYSTEM_PROMPT = """Ты — техножрец Адептус Механикус, хранитель архивов данных о цикле "Ересь Хоруса".
Общайся в характерной для техножреца манере: обращайся к найденным фактам как к
"данным" и "записям из архива", изредка используй обороты вроде "Святая Омнисия",
"согласно извлечённым данным", "протокол поиска завершён", можешь заменять некоторые
слова на технические термины (например "когнитивный процесс" вместо "мышление"), но
не переусердствуй — ответ должен оставаться читаемым и по существу, без наигранности
в каждом предложении. Обращайся к собеседнику только на "Вы" (уважительная форма),
никогда не переходи на "ты".

Отвечай ТОЛЬКО на основе фрагментов текста, приведённых в контексте ниже.
Если в контексте нет ответа на вопрос — прямо скажи, что в загруженной базе книг
недостаточно информации для ответа, и не придумывай факты от себя.
Не путай персонажей и события между собой. Отвечай на русском языке.

Вся загруженная база — это цикл "Ересь Хоруса", действие которого происходит
в 30-31-м тысячелетии (задолго до "текущей" эпохи Warhammer 40000, 41-42-го
тысячелетия). Если вопрос касается более поздних событий или "текущего" состояния
персонажа/фракции — явно предупреди, что доступные тебе фрагменты относятся к эпохе
Ереси Хоруса, а не к запрошенному периоду, и не выдавай события Ереси за современные."""


def build_context(chunks: list[dict]) -> str:
    parts = []
    for c in chunks:
        parts.append(f"[Источник: {c['source_title']}]\n{c['text']}")
    return "\n\n---\n\n".join(parts)


@retry(
    retry=retry_if_exception_type(
        (APIConnectionError, APITimeoutError, InternalServerError, PermissionDeniedError)
    ),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _call_groq(client: Groq, messages: list[dict]) -> str:
    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=2048,
        )
    except RateLimitError as e:
        # лимит исчерпан (429) — повторять запрос немедленно бессмысленно,
        # поэтому конвертируем сразу, в обход retry= (см. декоратор выше)
        raise TokensExhaustedError(TECHPRIEST_QUOTA_MESSAGE) from e
    # PermissionDeniedError (403) НЕ ловим здесь — даём ему долететь наружу
    # нетронутым, чтобы retry= (см. декоратор) успел его повторить; в понятное
    # сообщение оборачиваем только после исчерпания попыток, в answer() ниже
    return resp.choices[0].message.content


def answer(question: str, k: int = 6) -> tuple[str, list[dict]]:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY не найден. Создай .env в корне проекта со строкой "
            "GROQ_API_KEY=твой_ключ (получить на https://console.groq.com/keys)"
        )
    client = Groq(api_key=api_key)

    chunks = retrieve(question, k=k)
    context = build_context(chunks)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Контекст:\n\n{context}\n\nВопрос: {question}"},
    ]

    try:
        reply = _call_groq(client, messages)
    except PermissionDeniedError as e:
        # retry= в _call_groq уже исчерпал попытки на этом моменте —
        # значит отказ стабильный, сообщаем пользователю понятно
        raise AccessDeniedError(TECHPRIEST_ACCESS_DENIED_MESSAGE) from e
    return reply, chunks


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Кто такой Хорус и почему он предал Императора?"
    reply, chunks = answer(q)
    print(reply)
    print("\nИсточники:", ", ".join(sorted({c["source_title"] for c in chunks})))
