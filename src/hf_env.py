"""Общая настройка окружения Hugging Face до первого импорта HF-библиотек.

huggingface_hub / transformers / sentence_transformers читают часть настроек
(HF_HOME, HF_HUB_OFFLINE и т.п.) в момент собственного импорта, поэтому этот
модуль должен подключаться первым делом в retriever.py и embed_and_index.py —
до строк `from sentence_transformers import ...`.

Главное здесь — авто-офлайн: если веса моделей уже лежат в кэше рядом с
проектом (hf_cache/), включается HF_HUB_OFFLINE=1, и библиотека вообще не
ходит в сеть. Без этого sentence_transformers при каждой загрузке модели
стучится в HF Hub для проверки метаданных и выводит в терминал (в том числе
поверх области ввода CLI/TUI):

    Warning: You are sending unauthenticated requests to the HF Hub.
    Please set a HF_TOKEN to enable higher rate limits and faster downloads.

Пользователь может переопределить поведение через .env (или переменные
окружения): HF_TOKEN — аутентифицированные запросы (выше лимиты, возможна
докачка недостающих файлов), HF_HUB_OFFLINE=0/1 — принудительно онлайн/офлайн.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# .env может содержать HF_TOKEN / HF_HUB_OFFLINE и должен быть загружен до
# того, как huggingface_hub прочитает окружение при своём импорте. chat.py
# загружает .env сам (до импорта tui -> retriever), но embed_and_index.py
# запускается напрямую, поэтому дублируем загрузку и здесь. load_dotenv не
# перезаписывает уже выставленные переменные окружения — дублирование безопасно.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# Модели, чьи веса должны лежать в HF-кэше рядом с проектом. Порядок не важен.
MODEL_NAMES = (
    "intfloat/multilingual-e5-large",
    "BAAI/bge-reranker-v2-m3",
)


def models_are_cached() -> bool:
    """True, если веса всех моделей из MODEL_NAMES уже лежат в HF-кэше (HF_HOME)."""
    hf_home = Path(os.environ.get("HF_HOME") or (PROJECT_ROOT / "hf_cache"))
    hf_hub = hf_home / "hub"
    for name in MODEL_NAMES:
        snapshots = hf_hub / f"models--{name.replace('/', '--')}" / "snapshots"
        if not snapshots.is_dir() or not any(snapshots.iterdir()):
            return False
    return True


def _auto_offline_when_cached() -> None:
    """Включает офлайн-режим, если модели уже в кэше и пользователь не
    попросил иное: нет ни HF_TOKEN (аутентификация), ни явного
    HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE. В офлайне HF Hub не делает сетевых
    запросов — исчезает предупреждение об неаутентифицированных запросах,
    а загрузка моделей ускоряется (нет round-trip'а к API Hub)."""
    if os.environ.get("HF_HUB_OFFLINE") or os.environ.get("TRANSFORMERS_OFFLINE"):
        return
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
        # Есть токен — оставляем онлайн: аутентифицированные запросы не выводят
        # предупреждение и позволяют докачать недостающие файлы модели.
        return
    if models_are_cached():
        os.environ["HF_HUB_OFFLINE"] = "1"


def configure_hf_env() -> None:
    """Вызывается при импорте retriever.py / embed_and_index.py, до импорта
    sentence_transformers и других HF-библиотек. Идемпотентна."""
    # Прогресс-бары tqdm ломают отрисовку TUI/CLI — выключены.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    # Кэш моделей — рядом с проектом, а не в домашней папке пользователя: если
    # запускать с флешки/переносного носителя, веса моделей не должны оседать
    # на диске хост-машины. Должно быть выставлено до первого импорта
    # huggingface_hub / sentence_transformers, иначе библиотека уже выберет
    # путь по умолчанию.
    os.environ.setdefault("HF_HOME", str(PROJECT_ROOT / "hf_cache"))
    _auto_offline_when_cached()
