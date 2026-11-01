"""Textual-TUI в стиле arct (arc-academy-terminal) для Warhammer Archivist.

Полноэкранный терминальный интерфейс вместо построчного CLI:
- прокручиваемая история чата (колесо мыши, PgUp/PgDn, Ctrl+U/Ctrl+D);
- строка ввода закреплена внизу, с историей вопросов (↑/↓);
- нижняя панель с горячими клавишами (как бар в arct, всегда показывает актуальные);
- боковая панель источников последнего ответа (Ctrl+F);
- командная палитра (Ctrl+K / Ctrl+P), справка (F1), настройки (Ctrl+S);
- темы (Ctrl+T) — все на базе исходной палитры (зелёный/латунь/кость/кровь/сталь),
  меняется только фон/поверхности, сами цвета палитры не трогаются.

Вся RAG-логика остаётся в generator.py/retriever.py без изменений: ответы считаются
в фоновом потоке (workers), интерфейс в это время не блокируется.
"""
import asyncio
import contextlib
import json
import os
import random
import time
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import SimpleCommand
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Footer,
    Input,
    LoadingIndicator,
    SelectionList,
    Static,
    Switch,
)
from textual.worker import Worker, WorkerState

from generator import (
    ERA_LABELS,
    AccessDeniedError,
    EraClarificationNeeded,
    TokensExhaustedError,
    answer,
)
from persona import IDENTITY_RESPONSES, _is_identity_question
from retriever import BOOT_STAGES, models_cached, warm_up
from ui import (
    GREETING_VARIANTS,
    play_sound,
    start_background_music,
    stop_background_music,
)

# ---------------------------------------------------------------------------
# Палитра — перенесена из ui.py (ANSI 256): те же зелёный/латунь/кость/кровь/сталь.
# ---------------------------------------------------------------------------
BRASS = "#d7af5f"        # ANSI 178 — латунь/бронза
BONE = "#ffd7af"         # ANSI 223 — кость/пергамент
BLOOD = "#af5f5f"        # ANSI 131 — тусклый кровавый
IRON = "#444444"         # ANSI 238 — тусклая сталь
DULL_BRASS = "#af8700"   # ANSI 136 — тусклая латунь
GREEN = "#00ff00"        # яркий зелёный (акценты)
DARK_GREEN = "#008000"   # тёмный зелёный (основной текст ответов)
DIM_GREEN = "#6f9f6f"    # приглушённый зелёный (подсказки)

STYLE_BRASS = Style(color=BRASS, bold=True)
STYLE_BONE = Style(color=BONE)
STYLE_BLOOD = Style(color=BLOOD)
STYLE_IRON = Style(color=IRON)
STYLE_DULL_BRASS = Style(color=DULL_BRASS)
STYLE_GREEN = Style(color=GREEN)
STYLE_DARK_GREEN = Style(color=DARK_GREEN)
STYLE_DIM = Style(color=DIM_GREEN)

TYPEWRITER_DELAY = 0.012  # секунд на символ — как в старом CLI

THEME_NAMES = ("mechanicus", "mechanicus-dark", "mechanicus-void")
THEME_DISPLAY_NAMES = {
    "mechanicus": "Механикус",
    "mechanicus-dark": "Механикус тёмный",
    "mechanicus-void": "Механикус угольный",
}

EXIT_COMMANDS = ("/exit", "/quit")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "terminal_config.json"
CONFIG_DEFAULTS = {
    "theme": "mechanicus",
    "sounds": True,
    "typing": True,
    "music": True,
    "panel": False,
}

EXPECTED_BOOT_SECONDS = [1.0, 4.0, 5.0, 90.0]  # грубая интерполяция прогресса, как в старом CLI

# Файл с настоящим файловым дескриптором на время RAG-воркеров: Textual во
# время работы подменяет sys.stderr заглушкой с fileno() == -1, из-за чего
# multiprocessing.resource_tracker падает при спавне своего процесса
# (ValueError: bad value(s) in fds_to_keep) — см. _ask_worker.
_DEVNULL_STDERR = open(os.devnull, "w", encoding="utf-8")


def _make_theme(name: str, bg: str, surface: str, panel: str, fg: str) -> Theme:
    """Тема на базе исходной палитры: цвета акцентов одни и те же, меняется
    только фон/поверхности. Пользовательскую палитру таким образом не трогаем."""
    return Theme(
        name=name,
        primary=BRASS,
        secondary=DULL_BRASS,
        warning=BLOOD,
        error=BLOOD,
        success=GREEN,
        accent=BONE,
        foreground=fg,
        background=bg,
        surface=surface,
        panel=panel,
        boost=IRON,
        dark=True,
        variables={
            "bg": bg,
            "surface": surface,
            "panel": panel,
            "fg": fg,
            "brass": BRASS,
            "dull-brass": DULL_BRASS,
            "bone": BONE,
            "blood": BLOOD,
            "iron": IRON,
            "green": GREEN,
            "dark-green": DARK_GREEN,
            "dim-green": DIM_GREEN,
        },
    )


def _all_themes() -> list[Theme]:
    return [
        _make_theme("mechanicus", "#0a0c0a", "#0f130f", "#161d16", BONE),
        _make_theme("mechanicus-dark", "#070907", "#0c0f0c", "#121812", "#e8c9a5"),
        _make_theme("mechanicus-void", "#050705", "#0a0d0a", "#0f140f", "#d9bfa0"),
    ]


def _build_input_bindings() -> list[Binding]:
    """Биндинги QuestionInput: стандартные клавиши Input, плюс переопределение
    Ctrl+K/Ctrl+U/Ctrl+D в пользу TUI-приложения (палитра, прокрутка журнала),
    плюс история вопросов по ↑/↓.

    Textual объединяет BINDINGS по всей иерархии классов, поэтому убрать
    стандартный ctrl+k фильтрацией нельзя — ставим свой биндинг с priority=True,
    он проверяется раньше родительского (как в arct, где эти клавиши глобальные).
    """
    removed = {"ctrl+k", "ctrl+u", "ctrl+d"}
    keep: list[Binding] = []
    for b in Input.BINDINGS:
        keys = [k.strip() for k in b.key.split(",")]
        keys = [k for k in keys if k not in removed]
        if not keys:
            continue
        keep.append(
            Binding(
                ",".join(keys),
                b.action,
                b.description,
                show=b.show,
                key_display=b.key_display,
                priority=b.priority,
                tooltip=b.tooltip,
            )
        )
    keep.append(
        Binding("ctrl+k", "app_command_palette", "Палитра", show=False, priority=True)
    )
    keep.append(
        Binding("ctrl+u", "app_scroll_log_up", "Вверх", show=False, priority=True)
    )
    keep.append(
        Binding("ctrl+d", "app_scroll_log_down", "Вниз", show=False, priority=True)
    )
    keep.append(Binding("up", "history_prev", "Предыдущий вопрос", show=False))
    keep.append(Binding("down", "history_next", "Следующий вопрос", show=False))
    return keep


class QuestionInput(Input):
    """Input с историей вопросов (↑/↓) и освобождёнными для TUI клавишами.

    Ctrl+K (палитра), Ctrl+U/Ctrl+D (прокрутка журнала) в стандартном Input
    заняты редактированием строки — здесь они отдаются приложению, как в arct.
    """

    # ВАЖНО: присваивать в теле класса, а не после — Textual фиксирует
    # BINDINGS в момент создания класса.
    BINDINGS = _build_input_bindings()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def action_history_prev(self) -> None:
        self.app.history_step(-1)

    def action_history_next(self) -> None:
        self.app.history_step(1)

    def action_app_command_palette(self) -> None:
        self.app.action_command_palette()

    def action_app_scroll_log_up(self) -> None:
        self.app.action_scroll_log_up()

    def action_app_scroll_log_down(self) -> None:
        self.app.action_scroll_log_down()


class EraModal(ModalScreen):
    """Уточнение эпохи, когда retrieval нашёл данные из разных эпох (стрелки + Enter)."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, by_era: dict[str, list[dict]]):
        super().__init__()
        self.by_era = by_era

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                yield Static("НАЙДЕНЫ ДАННЫЕ ИЗ РАЗНЫХ ЭПОХ", classes="dialog-title")
                yield Static(
                    Text(
                        "Реливантные записи относятся к разным эпохам вселенной.\n"
                        "Выберите эпоху (↑/↓, Enter):",
                        style=STYLE_BONE,
                    ),
                    markup=False,
                )
                yield SelectionList(
                    *[(ERA_LABELS[era], era) for era in sorted(self.by_era)],
                    id="era-list",
                )
                yield Static(
                    Text("Esc — отменить вопрос", style=STYLE_DIM),
                    markup=False,
                )

    def on_mount(self) -> None:
        self.query_one("#era-list", SelectionList).focus()

    @on(SelectionList.SelectedChanged)
    def _on_selected(self, event: SelectionList.SelectedChanged) -> None:
        selected = event.selection_list.selected
        if selected:
            self.dismiss(selected[0])


class HelpModal(ModalScreen):
    """Справка по горячим клавишам (как ?/F1 в arct)."""

    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("enter", "close", "Закрыть", show=False),
    ]

    def action_close(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                yield Static("СПРАВКА", classes="dialog-title")
                yield Static(self._help_text(), markup=False)
                yield Static(
                    Text("Esc / Enter — закрыть", style=STYLE_DIM),
                    markup=False,
                )

    @staticmethod
    def _help_text() -> Text:
        rows = [
            ("Ctrl+F", "показать/скрыть панель источников последнего ответа"),
            ("Ctrl+K / Ctrl+P", "командная палитра"),
            ("Ctrl+S", "настройки (звук, печатная машинка, музыка, тема)"),
            ("Ctrl+T", "следующая тема (палитра цветов не меняется)"),
            ("Ctrl+U / Ctrl+D", "прокрутка журнала вверх/вниз"),
            ("PgUp / PgDn, колесо мыши", "прокрутка журнала"),
            ("↑ / ↓", "история вопросов в строке ввода"),
            ("Ctrl+L", "очистить журнал"),
            ("F1", "эта справка"),
            ("Ctrl+C / Ctrl+Q", "выход"),
            ("/sources", "источники последнего ответа"),
            ("/exit", "выход"),
        ]
        out = Text()
        for key, desc in rows:
            out.append("  ", style=STYLE_IRON)
            out.append(f"{key:<24}", style=STYLE_BRASS)
            out.append(f"{desc}\n", style=STYLE_BONE)
        return out


class SettingsModal(ModalScreen):
    """Настройки интерфейса: звук, печатная машинка, музыка, панель, тема."""

    BINDINGS = [Binding("escape", "close", "Закрыть")]

    def action_close(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                yield Static("НАСТРОЙКИ ТЕРМИНАЛА", classes="dialog-title")
                with Horizontal(classes="setting-row"):
                    yield Static("Звуковые сигналы", classes="setting-label")
                    yield Switch(value=False, id="sw-sounds")
                with Horizontal(classes="setting-row"):
                    yield Static("Эффект печатной машинки", classes="setting-label")
                    yield Switch(value=False, id="sw-typing")
                with Horizontal(classes="setting-row"):
                    yield Static("Фоновая музыка", classes="setting-label")
                    yield Switch(value=False, id="sw-music")
                with Horizontal(classes="setting-row"):
                    yield Static("Панель источников", classes="setting-label")
                    yield Switch(value=False, id="sw-panel")
                yield Static(
                    Text("Тема (палитра не меняется — только фон):", style=STYLE_BONE),
                    classes="setting-label",
                    markup=False,
                )
                yield SelectionList(
                    *[
                        (THEME_DISPLAY_NAMES[name], name)
                        for name in THEME_NAMES
                    ],
                    id="theme-list",
                )
                yield Button("Готово", id="done-btn", variant="primary")

    def on_mount(self) -> None:
        app = self.app
        self.query_one("#sw-sounds", Switch).value = app._sounds
        self.query_one("#sw-typing", Switch).value = app._typing_delay > 0
        self.query_one("#sw-music", Switch).value = app._music_on
        self.query_one("#sw-panel", Switch).value = app._panel_on
        self.query_one("#theme-list", SelectionList).select(app.theme)

    @on(Switch.Changed)
    def _on_switch(self, event: Switch.Changed) -> None:
        app = self.app
        switch_id = event.switch.id
        if switch_id == "sw-sounds":
            app._sounds = event.value
        elif switch_id == "sw-typing":
            app._typing_delay = TYPEWRITER_DELAY if event.value else 0.0
        elif switch_id == "sw-music":
            app._music_on = event.value
            if event.value:
                start_background_music()
            else:
                stop_background_music()
        elif switch_id == "sw-panel":
            app._panel_on = event.value
            app._apply_panel_visibility()
        app._save_config()

    @on(SelectionList.SelectedChanged)
    def _on_theme_selected(self, event: SelectionList.SelectedChanged) -> None:
        selected = event.selection_list.selected
        if selected:
            self.app.set_theme(selected[0])

    @on(Button.Pressed)
    def _on_done(self) -> None:
        self.dismiss(None)


class BootScreen(Screen):
    """Экран прогрева моделей при первом (холодном) запуске."""

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="boot-box"):
                yield Static(
                    Text("Пробуждение Машинного Духа...", style=STYLE_BONE),
                    markup=False,
                    id="boot-title",
                )
                yield Static(markup=False, id="boot-stages")
                yield Static(markup=False, id="boot-bar")

    def on_mount(self) -> None:
        self.set_interval(0.15, self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        app = self.app
        idx = app._boot_idx
        expected = EXPECTED_BOOT_SECONDS[idx]
        elapsed = time.monotonic() - app._boot_stage_started
        frac = min(elapsed / expected, 0.97) if expected > 0 else 0.97
        cumulative = [sum(EXPECTED_BOOT_SECONDS[:i]) for i in range(len(EXPECTED_BOOT_SECONDS))]
        total = sum(EXPECTED_BOOT_SECONDS)
        percent = (cumulative[idx] + expected * frac) / total * 100

        out = Text()
        out.append("\n")
        for i, (_, label) in enumerate(BOOT_STAGES):
            if i < idx:
                out.append(f"  ✓ {label}\n", style=STYLE_BRASS)
            elif i == idx:
                out.append(f"  ⠿ {label}...\n", style=STYLE_BONE)
            else:
                out.append(f"    {label}\n", style=STYLE_DIM)
        out.append("\n")

        width = 24
        filled = int(round(width * percent / 100))
        out.append(" " * 8)
        out.append(f"[{'▓' * filled}{'░' * (width - filled)}]", style=STYLE_BRASS)
        out.append(f" {percent:3.0f}%", style=STYLE_BONE)

        self.query_one("#boot-stages").update(out)
        self.query_one("#boot-bar").update("")


class MechanicusApp(App):
    """Главное приложение: макет в духе arct поверх существующей RAG-логики."""

    TITLE = "Warhammer Archivist"
    SUB_TITLE = "Терминал Адептус Механикус"

    CSS = """
    App {
        background: $bg;
        color: $fg;
    }

    Screen {
        background: $bg;
    }

    /* ВАЖНО: у виджетов с рамкой height считается ВМЕСТЕ с рамкой (border
       занимает строку внутри виджета) — поэтому height: 2 = строка контента
       + строка рамки снизу. */
    #header {
        height: 2;
        background: $panel;
        color: $brass;
        text-style: bold;
        padding: 0 1;
        border-bottom: solid $blood;
    }

    #main {
        height: 1fr;
    }

    #chat-scroll {
        width: 1fr;
        height: 1fr;
        background: $bg;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-background: $panel;
        scrollbar-color: $iron;
        scrollbar-color-hover: $brass;
        scrollbar-color-active: $brass;
    }

    #chat-scroll > .scrollbar {
        background: $panel;
    }

    #sources-col {
        width: 40;
        height: 1fr;
        display: none;
        background: $surface;
        border-left: solid $iron;
        padding: 0 1;
    }

    #sources-col.visible {
        display: block;
    }

    #sources-title {
        height: 1;
        background: $panel;
        color: $brass;
        text-style: bold;
        padding: 0 1;
        text-align: center;
    }

    #sources-list {
        background: $surface;
        height: 1fr;
    }

    #status-row {
        height: 1;
        background: $panel;
    }

    #status {
        width: 1fr;
        color: $dim-green;
        padding: 0 1;
    }

    #spinner {
        width: 2;
        color: $brass;
        display: none;
    }

    #spinner.busy {
        display: block;
    }

    #input-row {
        height: 4;
        background: $surface;
        border-top: solid $iron;
        padding: 0 1;
    }

    #prompt {
        width: 2;
        color: $dim-green;
        content-align: center middle;
        text-style: bold;
    }

    #question {
        background: $surface;
        color: $bone;
        border: none;
        padding: 0 1;
    }

    #question:focus {
        background: $bg;
    }

    #question:disabled {
        color: $dim-green;
    }

    Footer {
        height: 2;
        background: $panel;
        color: $dull-brass;
        border-top: solid $iron;
    }

    FooterKey {
        background: $iron;
        color: $brass;
    }

    FooterKey.-command-palette {
        background: $blood;
        color: $bg;
    }

    FooterLabel {
        color: $bone;
    }

    #dialog {
        width: 66;
        height: auto;
        background: $surface;
        border: thick $iron;
        padding: 1 2;
    }

    #boot-box {
        width: 62;
        height: auto;
        background: $bg;
        border: thick $iron;
        padding: 1 2;
    }

    .dialog-title {
        color: $brass;
        text-style: bold;
        background: $panel;
        padding: 0 1;
    }

    .setting-row {
        height: 3;
    }

    .setting-label {
        width: 1fr;
        color: $bone;
        padding-top: 1;
    }

    #era-list, #theme-list {
        background: $surface;
        border: solid $iron;
        padding: 0 1;
        height: auto;
    }

    #era-hint, #era-cancel-hint {
        color: $dim-green;
        height: auto;
    }

    SelectionList > .selection-list--button {
        color: $bone;
        background: $surface;
    }

    SelectionList > .selection-list--button-highlighted {
        color: $brass;
        background: $iron;
        text-style: bold;
    }

    SelectionList > .selection-list--button-selected {
        color: $green;
    }

    Button {
        width: 22;
        margin-top: 1;
    }

    #done-btn {
        dock: right;
    }

    .banner {
        text-align: center;
        width: 100%;
    }
    """

    BINDINGS = [
        Binding("ctrl+f", "toggle_sources", "Источники"),
        Binding("ctrl+t", "cycle_theme", "Тема"),
        Binding("ctrl+k", "command_palette", "Палитра"),
        Binding("ctrl+p", "command_palette", "Палитра", show=False),
        Binding("ctrl+s", "settings", "Настройки"),
        Binding("ctrl+u", "scroll_log_up", "Вверх"),
        Binding("ctrl+d", "scroll_log_down", "Вниз"),
        Binding("ctrl+l", "clear_log", "Очистить"),
        Binding("f1", "help", "Помощь"),
        Binding("ctrl+q", "quit", "Выход"),
    ]

    def __init__(self):
        super().__init__()
        for theme in _all_themes():
            self.register_theme(theme)

        self._config = self._load_config()
        self.theme = (
            self._config["theme"]
            if self._config["theme"] in THEME_NAMES
            else "mechanicus"
        )

        self._greeting = random.choice(GREETING_VARIANTS)
        self._sounds: bool = bool(self._config["sounds"])
        self._typing_delay: float = TYPEWRITER_DELAY if self._config["typing"] else 0.0
        self._music_on: bool = bool(self._config["music"])
        self._panel_on: bool = bool(self._config["panel"])

        self._busy = False
        self._typing_active = False
        self._skip_typing = False
        self._last_chunks: list[dict] = []
        self._input_history: list[str] = []
        self._hist_idx: int | None = None
        self._hist_draft = ""
        self._pending_question = ""
        self._pending_by_era: dict[str, list[dict]] = {}

        self._boot_screen: BootScreen | None = None
        self._boot_idx = 0
        self._boot_stage_started = time.monotonic()
        # Textual 8 может доставлять Worker.StateChanged (SUCCESS/ERROR) для
        # одного воркера несколько раз — запоминаем обработанные события,
        # чтобы ответ не печатался дважды, а boot не завершался дважды.
        self._handled_worker_events: set[tuple[int, str]] = set()

    # ------------------------------------------------------------------ данные
    def _load_config(self) -> dict:
        cfg = dict(CONFIG_DEFAULTS)
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return cfg
        if isinstance(data, dict):
            for key in CONFIG_DEFAULTS:
                if key in data:
                    cfg[key] = data[key]
        return cfg

    def _save_config(self) -> None:
        self._config.update(
            theme=self.theme,
            sounds=self._sounds,
            typing=self._typing_delay > 0,
            music=self._music_on,
            panel=self._panel_on,
        )
        try:
            CONFIG_PATH.write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass  # проект может лежать на read-only носителе — настройки тогда просто не сохранятся

    # ------------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(markup=False, id="header")
            with Horizontal(id="main"):
                yield VerticalScroll(id="chat-scroll")
                with Vertical(id="sources-col"):
                    yield Static("ИСТОЧНИКИ ПОСЛЕДНЕГО ОТВЕТА", id="sources-title")
                    yield VerticalScroll(id="sources-list")
            with Horizontal(id="status-row"):
                yield Static(markup=False, id="status")
                yield LoadingIndicator(id="spinner")
            with Horizontal(id="input-row"):
                yield Static(">", id="prompt")
                yield QuestionInput(
                    placeholder="Вопрос к архиву…  (/help — справка)",
                    id="question",
                )
            yield Footer()

    def on_mount(self) -> None:
        self.query_one("#header").update(
            Text.assemble(
                ("✠ ARCHIVE TERMINAL", STYLE_BRASS),
                ("  ·  ", STYLE_IRON),
                ("ADEPTUS MECHANICUS", STYLE_BONE),
                ("  |  ", STYLE_IRON),
                (self._greeting, STYLE_DIM),
            )
        )
        self._set_status("Готов к приёму запроса.")
        self._print_banner()
        self._apply_panel_visibility()
        self._refresh_sources_placeholder()

        if self._sounds:
            play_sound("startup")
        if self._music_on:
            start_background_music()

        if not models_cached():
            self._start_boot()
        else:
            self.query_one("#question", QuestionInput).focus()

    def on_unmount(self) -> None:
        self._save_config()
        stop_background_music()
        if self._sounds:
            play_sound("exit")

    # ------------------------------------------------------------- сообщения
    def _chat(self) -> VerticalScroll:
        return self.query_one("#chat-scroll", VerticalScroll)

    def _append_msg(self, content, classes: str = "msg") -> Static:
        chat = self._chat()
        at_end = chat.is_vertical_scroll_end
        widget = Static(content, markup=False, classes=classes)
        chat.mount(widget)
        if at_end:
            chat.scroll_end(force=True)
        return widget

    def _print_banner(self) -> None:
        lines = [
            ("        ✠", STYLE_IRON),
            ("    ╱       ╲", STYLE_IRON),
            ("  •═════════════•", STYLE_BLOOD),
            ("  ARCHIVE TERMINAL", STYLE_BRASS),
            ("  ADEPTUS MECHANICUS", STYLE_BONE),
            ("", STYLE_DIM),
            (self._greeting, STYLE_DIM),
            ("Вводите вопрос внизу. /help — справка, /sources — источники.", STYLE_DIM),
            ("", STYLE_DIM),
        ]
        banner = Text()
        for text, style in lines:
            banner.append(text + "\n", style=style)
        self._append_msg(banner, classes="banner")

    def _set_status(self, text: str) -> None:
        self.query_one("#status").update(Text(text, style=STYLE_DIM))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#question", QuestionInput).disabled = busy
        spinner = self.query_one("#spinner")
        if busy:
            spinner.add_class("busy")
        else:
            spinner.remove_class("busy")

    # -------------------------------------------------------------- панель
    def _apply_panel_visibility(self) -> None:
        col = self.query_one("#sources-col")
        if self._panel_on:
            col.add_class("visible")
        else:
            col.remove_class("visible")

    def _refresh_sources_placeholder(self) -> None:
        box = self.query_one("#sources-list", VerticalScroll)
        box.remove_children()
        if not self._last_chunks:
            box.mount(
                Static(
                    Text("Источников пока нет — сначала задайте вопрос.", style=STYLE_DIM),
                    markup=False,
                )
            )

    def _update_sources(self, chunks: list[dict]) -> None:
        self._last_chunks = chunks
        box = self.query_one("#sources-list", VerticalScroll)
        box.remove_children()
        if not chunks:
            self._refresh_sources_placeholder()
            return
        for i, chunk in enumerate(chunks, 1):
            title = chunk.get("source_title") or "без названия"
            parts = [
                p.strip()
                for p in (chunk.get("chapter_title") or "").split("\n")
                if p.strip()
            ]
            chapter = " / ".join(parts) or "без главы"
            if len(chapter) > 60:
                chapter = chapter[:57] + "..."
            score = chunk.get("score", 0.0)
            row = Text.assemble(
                (f"{i}. ", STYLE_BRASS),
                (title, STYLE_BONE),
                ("\n   ", STYLE_DIM),
                (f"{chapter} · релевантность {score:.2f}", STYLE_DIM),
            )
            box.mount(Static(row, markup=False))
            box.mount(Static("", markup=False))

    def action_toggle_sources(self) -> None:
        self._panel_on = not self._panel_on
        self._apply_panel_visibility()
        self._save_config()
        if self._panel_on and not self._last_chunks:
            self._set_status("Источников пока нет — сначала задайте вопрос.")
        else:
            self._set_status(
                "Панель источников " + ("открыта." if self._panel_on else "скрыта.")
            )

    # ------------------------------------------------------------------ темы
    def set_theme(self, name: str) -> None:
        if name not in THEME_NAMES:
            return
        self.theme = name
        self._save_config()
        self._set_status(f"Тема: {THEME_DISPLAY_NAMES[name]}.")

    def action_cycle_theme(self) -> None:
        idx = (THEME_NAMES.index(self.theme) + 1) % len(THEME_NAMES)
        self.set_theme(THEME_NAMES[idx])

    # --------------------------------------------------------------- журнал
    def action_scroll_log_up(self) -> None:
        self._chat().scroll_page_up()

    def action_scroll_log_down(self) -> None:
        self._chat().scroll_page_down()

    def action_clear_log(self) -> None:
        self._chat().remove_children()
        self._set_status("Журнал очищен.")

    # ------------------------------------------------------------- диалоги
    def action_settings(self) -> None:
        self.push_screen(SettingsModal())

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_command_palette(self) -> None:
        # SimpleCommand(name, callback, help_text) — порядок важен.
        commands = [
            SimpleCommand("Показать или скрыть источники", self.action_toggle_sources, "Ctrl+F"),
            SimpleCommand("Сменить тему", self.action_cycle_theme, "Ctrl+T"),
            SimpleCommand("Настройки: звук, печать, музыка", self.action_settings, "Ctrl+S"),
            SimpleCommand("Справка по клавишам", self.action_help, "F1"),
            SimpleCommand("Прокрутить журнал вверх", self.action_scroll_log_up, "Ctrl+U"),
            SimpleCommand("Прокрутить журнал вниз", self.action_scroll_log_down, "Ctrl+D"),
            SimpleCommand("Очистить журнал", self.action_clear_log, "Ctrl+L"),
            SimpleCommand("Выйти", self.action_quit, "Ctrl+Q"),
        ]
        self.search_commands(commands, placeholder="Команда…")

    def action_quit(self) -> None:
        self.exit()

    # ----------------------------------------------------------- история ввода
    def history_step(self, delta: int) -> None:
        inp = self.query_one("#question", QuestionInput)
        if not self._input_history:
            return
        if self._hist_idx is None:
            self._hist_draft = inp.value
            self._hist_idx = len(self._input_history)
        new_idx = min(max(self._hist_idx + delta, 0), len(self._input_history))
        if new_idx == len(self._input_history):
            inp.value = self._hist_draft
            self._hist_idx = None
        else:
            inp.value = self._input_history[new_idx]
            self._hist_idx = new_idx
        inp.action_end()

    # ------------------------------------------------------------------ ввод
    @on(Input.Submitted, "#question")
    def _on_question(self, event: Input.Submitted) -> None:
        question = (event.value or "").strip()
        self.query_one("#question", QuestionInput).value = ""
        if not question:
            return
        if self._busy:
            self._set_status("Машинный Дух ещё обрабатывает предыдущий запрос...")
            return
        if question in EXIT_COMMANDS:
            self.action_quit()
            return
        if question == "/sources":
            self.action_toggle_sources()
            return
        if question == "/help":
            self.action_help()
            return
        if question == "/theme":
            self.action_cycle_theme()
            return
        if question == "/clear":
            self.action_clear_log()
            return
        if question.startswith("/"):
            self._append_msg(
                Text(f"Неизвестная команда: {question}", style=STYLE_DIM)
            )
            self._set_status("Неизвестная команда. /help — список команд.")
            return

        self._input_history.append(question)
        self._hist_idx = None
        self._pending_question = question
        self._pending_by_era = {}
        self._append_msg(Text(f"> {question}", style=STYLE_DIM))
        self._append_msg(Text("", style=Style()))

        if _is_identity_question(question):
            self._set_busy(True)
            self._set_status("Машинный Дух отвечает...")
            self._type_reply(random.choice(IDENTITY_RESPONSES))
        else:
            self._set_busy(True)
            self._set_status("Машинный Дух обрабатывает запрос...")
            self._ask_worker(question, None)

    # ------------------------------------------------------- печатная машинка
    def _type_reply(self, text: str) -> None:
        self._typing_active = True
        self._typing_widget = Static(Text("", style=STYLE_DARK_GREEN), markup=False)
        chat = self._chat()
        at_end = chat.is_vertical_scroll_end
        chat.mount(self._typing_widget)
        if at_end:
            chat.scroll_end(force=True)
        self._type_out_worker(text)

    @work(thread=False, group="typing", exclusive=True)
    async def _type_out_worker(self, text: str) -> None:
        widget = self._typing_widget
        chat = self._chat()
        delay = self._typing_delay
        try:
            if delay <= 0:
                widget.update(Text(text, style=STYLE_DARK_GREEN))
            else:
                buf: list[str] = []
                chunk_size = 4
                for ch in text:
                    if self._skip_typing:
                        break
                    buf.append(ch)
                    if len(buf) % chunk_size == 0:
                        at_end = chat.is_vertical_scroll_end
                        widget.update(Text("".join(buf), style=STYLE_DARK_GREEN))
                        if at_end:
                            chat.scroll_end(force=True)
                        await asyncio.sleep(delay * chunk_size)
                widget.update(Text(text, style=STYLE_DARK_GREEN))
        finally:
            self._skip_typing = False
            self._typing_active = False
            self._append_msg(
                Text(
                    "(наберите /sources, чтобы увидеть источники ответа)",
                    style=STYLE_DIM,
                )
            )
            self._set_busy(False)
            self._set_status("Готов к приёму запроса.")
            self.query_one("#question", QuestionInput).focus()

    def on_key(self, event: events.Key) -> None:
        """Любая клавиша во время печати ответа — мгновенно допечатывает его
        целиком (как пропуск анимации), не отправляя клавишу дальше."""
        if self._typing_active:
            self._skip_typing = True
            event.stop()

    # ------------------------------------------------------------ RAG-воркеры
    @work(thread=True, group="answer", exclusive=True)
    def _ask_worker(self, question: str, era_override: list[dict] | None):
        # Textual на время работы подменяет sys.stderr своей заглушкой
        # (fileno() == -1). Если внутри answer() какая-то библиотека лениво
        # создаст multiprocessing-примитив (так делает tqdm при первом
        # trange в sentence-transformers), resource_tracker попытается
        # заспавнить свой процесс с fds_to_pass=[sys.stderr.fileno()=-1, ...]
        # и упадёт с ValueError("bad value(s) in fds_to_keep"). На время
        # вызова возвращаем stderr настоящий файловый дескриптор (/dev/null).
        with contextlib.redirect_stderr(_DEVNULL_STDERR):
            try:
                reply, chunks = answer(question, era_chunks_override=era_override)
                return {"kind": "ok", "reply": reply, "chunks": chunks}
            except EraClarificationNeeded as e:
                return {"kind": "era", "question": e.question, "by_era": e.by_era}
            except TokensExhaustedError as e:
                return {"kind": "error", "text": str(e)}
            except AccessDeniedError as e:
                return {"kind": "error", "text": str(e)}
            except RuntimeError as e:
                return {"kind": "error", "text": f"Ошибка: {e}"}
            except Exception as e:
                return {"kind": "error", "text": f"Ошибка обращения к DeepSeek: {e}"}

    @work(thread=True, group="boot", exclusive=True)
    def _boot_worker_run(self):
        def on_stage(stage_key: str) -> None:
            self.call_from_thread(self._set_boot_stage, stage_key)

        # То же самое, что в _ask_worker: прогрев грузит модели внутри
        # Textual-воркера, а загрузка тоже может лениво создавать
        # multiprocessing-примитивы (см. комментарий выше).
        with contextlib.redirect_stderr(_DEVNULL_STDERR):
            try:
                warm_up(on_stage=on_stage)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

    @on(Worker.StateChanged)
    def _on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        worker = event.worker
        if worker.state not in (WorkerState.SUCCESS, WorkerState.ERROR):
            return
        key = (id(worker), worker.state.name)
        if key in self._handled_worker_events:
            return  # дубль доставки события в Textual 8 (см. __init__)
        self._handled_worker_events.add(key)
        if worker.name == "_boot_worker_run":
            self._finish_boot_worker(worker)
        elif worker.name == "_ask_worker":
            self._finish_ask(worker)

    @work(thread=False, group="boot-finish", exclusive=True)
    async def _finish_boot_worker(self, worker: Worker) -> None:
        """Завершение прогрева: pop_screen() асинхронный, поэтому сначала
        дожидаемся снятия BootScreen, и только потом пишем в журнал главного
        экрана. Сразу после pop_screen() Textual ещё пересобирает дерево
        главного экрана (часть виджетов на миг отсутствует), поэтому ждём,
        пока #chat-scroll снова доступен в DOM."""
        result = worker.result
        if self._boot_screen is not None:
            self._boot_screen = None
            await self.pop_screen()
            await self._wait_for_chat()
        if result.get("ok"):
            self._append_msg(Text("Машинный Дух пробуждён. Архив открыт.", style=STYLE_DIM))
            self._set_status("Готов к приёму запроса.")
        else:
            self._append_msg(
                Text(f"Ошибка прогрева моделей: {result.get('error')}", style=STYLE_BLOOD)
            )
            self._set_status("Ошибка прогрева. См. журнал.")
        self.query_one("#question", QuestionInput).focus()

    async def _wait_for_chat(self, timeout: float = 5.0) -> None:
        """Ждёт, пока главный экран пересоберётся после смены экранов."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.query_one("#chat-scroll", VerticalScroll)
                return
            except Exception:
                await asyncio.sleep(0.01)
        self.query_one("#chat-scroll", VerticalScroll)  # последняя попытка — дать честную ошибку

    def _finish_ask(self, worker: Worker) -> None:
        result = worker.result
        kind = result.get("kind")

        if kind == "ok":
            self._update_sources(result.get("chunks", []))
            self._set_status("Протокол поиска завершён.")
            if self._sounds:
                play_sound("answer")
            self._type_reply(result.get("reply", ""))
            return

        if kind == "era":
            self._pending_question = result.get("question", self._pending_question)
            self._pending_by_era = result.get("by_era", {})
            self._append_msg(
                Text(
                    "Найдены релевантные данные из разных эпох — требуется уточнение.",
                    style=STYLE_DIM,
                )
            )
            self._set_status("Требуется выбор эпохи (↑/↓, Enter).")
            self.push_screen(EraModal(self._pending_by_era), callback=self._on_era_chosen)
            return

        # kind == "error"
        self._set_busy(False)
        self._append_msg(Text(result.get("text", "Неизвестная ошибка."), style=STYLE_DIM))
        self._set_status("Ошибка обработки запроса.")
        if self._sounds:
            play_sound("error")
        self.query_one("#question", QuestionInput).focus()

    def _on_era_chosen(self, era: str | None) -> None:
        if era is None:
            self._append_msg(Text("Уточнение эпохи отменено.", style=STYLE_DIM))
            self._set_busy(False)
            self._set_status("Готов к приёму запроса.")
            self.query_one("#question", QuestionInput).focus()
            return
        chunks = self._pending_by_era.get(era, [])
        self._append_msg(
            Text(f"Эпоха выбрана: {ERA_LABELS.get(era, era)}.", style=STYLE_DIM)
        )
        self._set_status("Машинный Дух обрабатывает запрос...")
        self._ask_worker(self._pending_question, chunks)

    # ------------------------------------------------------------------ boot
    def _start_boot(self) -> None:
        self._boot_idx = 0
        self._boot_stage_started = time.monotonic()
        self._boot_screen = BootScreen()
        self.push_screen(self._boot_screen)
        self._boot_worker_run()

    def _set_boot_stage(self, stage_key: str) -> None:
        for i, (key, _) in enumerate(BOOT_STAGES):
            if key == stage_key:
                self._boot_idx = i
                self._boot_stage_started = time.monotonic()
                return


def _prewarm_subprocess_primitives() -> None:
    """Создаёт multiprocessing-примитивы, которые библиотеки обычно создают
    лениво, ДО того как Textual подменит sys.stderr своей заглушкой.

    Без этого первый вопрос в TUI падает с «Ошибка обращения к DeepSeek:
    bad value(s) in fds_to_keep»: sentence-transformers при encode() вызывает
    tqdm.trange, tqdm при первом вызове создаёт multiprocessing.RLock,
    а multiprocessing.resource_tracker при этом спавнит свой процесс,
    передавая туда sys.stderr.fileno() — внутри Textual это -1, что
    _posixsubprocess отвергает. Здесь лок создаётся заранее (в главном
    потоке, пока stderr ещё настоящий), и спавн tracker'а проходит штатно.
    """
    try:
        from tqdm import tqdm as _tqdm

        _tqdm.get_lock()
    except Exception:
        pass  # tqdm недоступен/сломан — воркеры всё равно защищены redirect'ом в _ask_worker


def run() -> None:
    """Точка входа для chat.py."""
    _prewarm_subprocess_primitives()
    MechanicusApp().run()


if __name__ == "__main__":
    run()
