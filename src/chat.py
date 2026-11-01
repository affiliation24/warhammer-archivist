"""Точка входа Warhammer Archivist.

По умолчанию запускает Textual-TUI в стиле arct (src/tui.py). Если TUI невозможен
(Textual не установлен, нет терминала, stdin — пайп), тихо откатывается на
классический построчный CLI (src/cli.py), чтобы пайплайны/скрипты не ломались.
"""
import sys

from dotenv import load_dotenv


def _run_cli() -> None:
    from cli import main as cli_main

    cli_main()


def main() -> None:
    load_dotenv()

    if sys.stdin is None or not sys.stdin.isatty():
        # Нет интерактивного терминала (пайп, автотест) — TUI тут не место.
        _run_cli()
        return

    try:
        from tui import run
    except ImportError:
        print(
            "Textual не установлен — запускаю классический CLI.\n"
            "Для нового интерфейса выполните: pip install textual"
        )
        _run_cli()
        return

    try:
        run()
    except Exception:
        # Textual восстановил терминал сам; откатываемся на проверенный CLI.
        print("\nНе удалось запустить TUI — переключаюсь на классический CLI...")
        _run_cli()


if __name__ == "__main__":
    main()
