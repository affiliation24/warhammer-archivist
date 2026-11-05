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
from textual.geometry import Offset
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.timer import Timer
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

BRASS = "#d7af5f"
BONE = "#ffd7af"
BLOOD = "#af5f5f"
IRON = "#444444"
DULL_BRASS = "#af8700"
GREEN = "#00ff00"
DARK_GREEN = "#1e9e1e"
DIM_GREEN = "#6f9f6f"
PHOSPHOR_BRIGHT = "#8dff3a"
ALERT = "#ff7a1a"

STYLE_BRASS = Style(color=BRASS)
STYLE_BONE = Style(color=BONE)
STYLE_BLOOD = Style(color=BLOOD)
STYLE_IRON = Style(color=IRON)
STYLE_DULL_BRASS = Style(color=DULL_BRASS)
STYLE_GREEN = Style(color=GREEN)
STYLE_DARK_GREEN = Style(color=DARK_GREEN)
STYLE_DIM = Style(color=DIM_GREEN)

TYPEWRITER_DELAY = 0.012

STAGE_MAX_WIDTH = 132
PANEL_AUTO_WIDTH = 120
SCRAMBLE_GLYPHS = "▓▒░█▚▞◇◈┼╳"
SCRAMBLE_TICK = 0.045
SCRAMBLE_STEP = 2
FLICKER_MIN = 3.0
FLICKER_MAX = 8.0
FLICKER_HOLD_MIN = 0.06
FLICKER_HOLD_MAX = 0.1
SCANLINE_PERIOD = 5.0
SCANLINE_TICK = 0.12
STATUS_FLASH = 0.15
PULSE_TICK = 0.45
HEADER_TICK = 0.03
BANNER_TICK = 0.12
BOOT_TYPE_TICK = 0.03
BOOT_TYPE_STEP = 2

SESSION_LABELS = {
    "s-epoch": "ЭПОХА:",
    "s-count": "ФРАГМЕНТОВ:",
    "s-relevance": "РЕЛЕВАНТНОСТЬ:",
    "s-theme": "ТЕМА:",
}

THEME_NAMES = ("mechanicus", "mechanicus-dark", "mechanicus-void")
THEME_DISPLAY_NAMES = {
    "mechanicus": "Механикус",
    "mechanicus-dark": "Механикус тёмный",
    "mechanicus-void": "Механикус угольный",
}

EXIT_COMMANDS = ("/exit", "/quit")

# Команды чата в порядке перебора по Tab — используются автодополнением
# в QuestionInput. Описания показываются в статусной строке при дополнении.
COMMANDS = (
    "/clear",
    "/exit",
    "/help",
    "/quit",
    "/sources",
    "/theme",
)

COMMAND_DESCRIPTIONS = {
    "/clear": "очистить журнал",
    "/exit": "завершить сеанс",
    "/help": "справка по клавишам",
    "/quit": "завершить сеанс",
    "/sources": "источники последнего ответа",
    "/theme": "сменить тему",
}

CONFIG_PATH = Path(__file__).resolve().parent.parent / "terminal_config.json"
CONFIG_DEFAULTS = {
    "theme": "mechanicus",
    "sounds": True,
    "typing": True,
    "music": True,
    "panel": False,
    "crt": True,
}

EXPECTED_BOOT_SECONDS = [1.0, 4.0, 5.0, 90.0]

_DEVNULL_STDERR = open(os.devnull, "w", encoding="utf-8")


def _make_theme(
    name: str, bg: str, surface: str, panel: str, fg: str, frame: str, scanline: str
) -> Theme:
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
            "phosphor-bright": PHOSPHOR_BRIGHT,
            "alert": ALERT,
            "frame": frame,
            "scanline": scanline,
        },
    )


def _all_themes() -> list[Theme]:
    return [
        _make_theme("mechanicus", "#0a0c0a", "#0f130f", "#161d16", BONE, "#445544", "#1a2b12"),
        _make_theme("mechanicus-dark", "#030403", "#080a08", "#0e120e", "#e8c9a5", "#5f8c3a", "#243d18"),
        _make_theme("mechanicus-void", "#101308", "#161c0f", "#1d2512", "#d9bfa0", "#3a4434", "#0f1a0a"),
    ]


def _build_input_bindings() -> list[Binding]:
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
    keep.append(Binding("ctrl+k", "app_command_palette", "Палитра", show=False, priority=True))
    keep.append(Binding("ctrl+u", "app_scroll_log_up", "Вверх", show=False, priority=True))
    keep.append(Binding("ctrl+d", "app_scroll_log_down", "Вниз", show=False, priority=True))
    keep.append(Binding("up", "history_prev", "Предыдущий вопрос", show=False))
    keep.append(Binding("down", "history_next", "Следующий вопрос", show=False))
    keep.append(Binding("tab", "complete_next", "Дополнить команду", show=False))
    keep.append(Binding("shift+tab", "complete_prev", "Предыдущий вариант", show=False))
    return keep


class QuestionInput(Input):
    BINDINGS = _build_input_bindings()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._complete_base: str | None = None
        self._complete_matches: list[str] = []
        self._complete_idx: int = 0
        self._complete_value: str | None = None

    def _reset_completion(self) -> None:
        self._complete_base = None
        self._complete_matches = []
        self._complete_idx = 0
        self._complete_value = None

    def _on_key(self, event: events.Key) -> None:
        # Любое редактирование, кроме Tab/Shift+Tab, начинает дополнение заново.
        if event.key not in ("tab", "shift+tab"):
            self._reset_completion()

    def _on_paste(self, event: events.Paste) -> None:
        self._reset_completion()

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

    def action_complete_next(self) -> None:
        self._complete_command(1)

    def action_complete_prev(self) -> None:
        self._complete_command(-1)

    def _complete_command(self, direction: int) -> None:
        """Дополнение команд по Tab.

        Работает только в командном контексте (строка начинается с «/»):
        первый Tab дополняет до общего префикса совпадений, следующие —
        перебирают варианты вперёд (Tab) и назад (Shift+Tab). Вне командного
        контекста Tab ведёт себя как обычно — переводит фокус.
        """
        value = self.value
        if not value.startswith("/"):
            self._reset_completion()
            if direction >= 0:
                self.app.action_focus_next()
            else:
                self.app.action_focus_previous()
            return

        cursor = self.cursor_position
        prefix = value[:cursor]
        if not prefix.startswith("/"):
            # Курсор стоит до «/» — дополнять нечего, Tab работает как обычно.
            self._reset_completion()
            if direction >= 0:
                self.app.action_focus_next()
            else:
                self.app.action_focus_previous()
            return
        tail = value[cursor:]

        if self._complete_base is not None and value == self._complete_value:
            # Продолжаем начатый перебор вариантов.
            matches = self._complete_matches
            if not matches:
                self._reset_completion()
                return
            index = (self._complete_idx + direction) % len(matches)
        else:
            matches = [cmd for cmd in COMMANDS if cmd.startswith(prefix)]
            if not matches:
                self._reset_completion()
                self.app._set_status(
                    f"Нет команд по «{prefix}». Доступно: {' '.join(COMMANDS)}."
                )
                return
            common = os.path.commonprefix(matches)
            if len(common) > len(prefix) and len(matches) > 1:
                # Первый Tab дополняет до общего префикса, следующие — перебирают.
                self._complete_base = prefix
                self._complete_matches = matches
                self._complete_idx = -1 if direction >= 0 else len(matches)
                self._complete_value = common + tail
                self.value = self._complete_value
                self.cursor_position = len(common)
                self.app._set_status(
                    f"Дополнено: {common} · Tab — следующий вариант"
                )
                return
            self._complete_base = prefix
            self._complete_matches = matches
            index = 0 if direction >= 0 else len(matches) - 1

        command = matches[index % len(matches)]
        self._complete_idx = index
        self._complete_value = command + tail
        self.value = self._complete_value
        self.cursor_position = len(command)
        if len(matches) > 1:
            self.app._set_status(f"Дополнено: {command} · Tab — следующий вариант")
        else:
            description = COMMAND_DESCRIPTIONS.get(command, "")
            suffix = f" — {description}" if description else ""
            self.app._set_status(f"Дополнено: {command}{suffix}")


class FillerPanel(Static):
    def render(self) -> Text:
        width, height = self.size
        if width < 2 or height < 1:
            return Text("")
        glyphs = " ░▒▓▚▞"
        out = Text()
        for y in range(height):
            row = []
            for x in range(width):
                value = (x * 7 + y * 13) % 97
                if value < 44:
                    row.append(" ")
                elif value < 92:
                    row.append(glyphs[(x * 5 + y * 11) % len(glyphs)])
                else:
                    row.append("▓")
            out.append("".join(row) + "\n", style=STYLE_IRON)
        return out


class EraModal(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, by_era: dict[str, list[dict]]):
        super().__init__()
        self.by_era = by_era

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
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
        dialog = self.query_one("#dialog")
        dialog.border_title = " НАЙДЕНЫ ДАННЫЕ ИЗ РАЗНЫХ ЭПОХ "
        self.query_one("#era-list", SelectionList).focus()

    @on(SelectionList.SelectedChanged)
    def _on_selected(self, event: SelectionList.SelectedChanged) -> None:
        selected = event.selection_list.selected
        if selected:
            self.dismiss(selected[0])


class HelpModal(ModalScreen):
    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("enter", "close", "Закрыть", show=False),
    ]

    def action_close(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                yield Static(self._help_text(), markup=False)
                yield Static(
                    Text("Esc / Enter — закрыть", style=STYLE_DIM),
                    markup=False,
                )

    def on_mount(self) -> None:
        dialog = self.query_one("#dialog")
        dialog.border_title = " СПРАВКА "

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
            ("Tab / Shift+Tab", "автодополнение команд (например, /so → /sources)"),
            ("Ctrl+L", "очистить журнал"),
            ("F1", "эта справка"),
            ("Ctrl+C / Ctrl+Q", "выход"),
            ("/sources", "источники последнего ответа"),
            ("/exit", "выход"),
        ]
        out = Text()
        for key, desc in rows:
            lines = HelpModal._wrap_desc(key, desc) or [""]
            key_disp = f"{key:<24}" if len(key) <= 24 else f"{key} "
            out.append("  ", style=STYLE_IRON)
            out.append(key_disp, style=STYLE_BRASS)
            out.append(f"{lines[0]}\n", style=STYLE_BONE)
            indent = " " * (2 + len(key_disp))
            for extra in lines[1:]:
                out.append(indent + extra + "\n", style=STYLE_BONE)
        return out

    @staticmethod
    def _wrap_desc(key: str, desc: str) -> list[str]:
        key_disp = f"{key:<24}" if len(key) <= 24 else f"{key} "
        capacity = max(10, 60 - 2 - len(key_disp))
        words = desc.split(" ")
        wrapped: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= capacity:
                current = candidate
                continue
            if current:
                wrapped.append(current)
                current = ""
            while len(word) > capacity:
                wrapped.append(word[:capacity])
                word = word[capacity:]
            current = word
        if current:
            wrapped.append(current)
        return wrapped


class SettingsModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Закрыть")]

    def action_close(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
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
                with Horizontal(classes="setting-row"):
                    yield Static("Эффекты CRT", classes="setting-label")
                    yield Switch(value=False, id="sw-crt")
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
                with Horizontal(id="done-row"):
                    yield Button("Готово", id="done-btn", variant="primary")

    def on_mount(self) -> None:
        app = self.app
        dialog = self.query_one("#dialog")
        dialog.border_title = " НАСТРОЙКИ ТЕРМИНАЛА "
        self.query_one("#sw-sounds", Switch).value = app._sounds
        self.query_one("#sw-typing", Switch).value = app._typing_delay > 0
        self.query_one("#sw-music", Switch).value = app._music_on
        self.query_one("#sw-panel", Switch).value = app._panel_on
        self.query_one("#sw-crt", Switch).value = app._crt_on
        self.query_one("#theme-list", SelectionList).select(app.theme)

    @on(Switch.Changed)
    def _on_switch(self, event: Switch.Changed) -> None:
        app = self.app
        switch_id = event.switch.id
        value = event.value

        if switch_id == "sw-sounds":
            if app._sounds == value:
                return
            app._sounds = value
        elif switch_id == "sw-typing":
            new_delay = TYPEWRITER_DELAY if value else 0.0
            if app._typing_delay == new_delay:
                return
            app._typing_delay = new_delay
        elif switch_id == "sw-music":
            if app._music_on == value:
                return
            app._music_on = value
            if value:
                start_background_music()
            else:
                stop_background_music()
        elif switch_id == "sw-panel":
            if app._panel_on == value:
                return
            app._set_panel(value)
        elif switch_id == "sw-crt":
            if app._crt_on == value:
                return
            app._crt_on = value
            if value:
                app._start_crt_effects()
            else:
                app._stop_crt_effects()
        else:
            return
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
        self._typed = 0
        self._stage_idx = -1
        self.set_interval(0.15, self._refresh)
        self.set_interval(BOOT_TYPE_TICK, self._type_tick)
        self._refresh()

    def _type_tick(self) -> None:
        app = self.app
        idx = app._boot_idx
        if idx != self._stage_idx:
            self._stage_idx = idx
            self._typed = 0
        label = BOOT_STAGES[idx][1]
        self._typed = min(self._typed + BOOT_TYPE_STEP, len(label))
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
                out.append(f"  ▌ {label[: self._typed]}\n", style=STYLE_BONE)
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

    #header {
        height: 3;
        background: $bg;
        border-bottom: solid $frame;
    }

    #header-line {
        width: 100%;
        height: 1;
        text-align: center;
        color: $phosphor-bright;
        text-style: bold;
    }

    #session-tag {
        width: 100%;
        height: 1;
        text-align: center;
        color: $dim-green;
    }

    #main-row {
        height: 1fr;
    }

    #main {
        height: 1fr;
    }

    .gutter {
        width: 1fr;
        height: 1fr;
        background: $bg;
        color: $iron;
        text-opacity: 0.35;
    }

    #log-frame {
        width: 1fr;
        height: 1fr;
        border: double $frame;
        border-title-color: $phosphor-bright;
        border-title-background: $bg;
        border-title-style: bold;
        border-subtitle-color: $dim-green;
        border-subtitle-align: center;
        padding: 0 1;
    }

    #chat-scroll {
        width: 1fr;
        height: 1fr;
        background: $bg;
        padding: 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-background: $panel;
        scrollbar-color: $frame;
        scrollbar-color-hover: $phosphor-bright;
        scrollbar-color-active: $phosphor-bright;
    }

    #scanline {
        layer: overlay;
        width: 100%;
        height: 1;
        background: $scanline;
        offset: 0 0;
        display: none;
    }

    #scanline.-on {
        display: block;
    }

    .hl-band {
        width: 100%;
        height: auto;
        background: $phosphor-bright;
        color: $bg;
        text-style: bold;
        padding: 0 1;
        margin-top: 1;
        margin-bottom: 1;
    }

    .hl-band.-flicker {
        background: $dim-green;
    }

    .banner {
        text-align: center;
        width: 100%;
    }

    #sources-col {
        width: 40;
        height: 1fr;
        display: none;
        background: $surface;
        border: double $frame;
        border-title-color: $phosphor-bright;
        border-title-background: $bg;
        border-title-style: bold;
        padding: 0 1;
    }

    #sources-col.visible {
        display: block;
    }

    #sources-list {
        background: $surface;
        height: 1fr;
    }

    #session-data {
        height: 5;
        background: $surface;
        border-top: solid $frame;
    }

    #session-data Static {
        width: 100%;
        height: 1;
        content-align: right middle;
    }

    #status-row {
        height: 1;
        background: $alert;
    }

    #status {
        width: 1fr;
        height: 1;
        color: #3a2200;
        padding: 0 1;
        opacity: 0.65;
    }

    #status-row.-busy #status {
        opacity: 1;
        color: #0d0800;
        text-style: bold;
    }

    #status-row.-pulse-alt {
        background: #d6660f;
    }

    #status-row.-flash {
        background: $phosphor-bright;
    }

    #status-row.-flash #status {
        opacity: 1;
        color: #061200;
    }

    #spinner {
        width: 2;
        height: 1;
        color: #0d0800;
        display: none;
    }

    #spinner.busy {
        display: block;
    }

    #input-row {
        height: 4;
        background: $surface;
        border-top: double $frame;
        padding: 0 1;
    }

    #prompt {
        width: 2;
        color: $phosphor-bright;
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

    #footer-row {
        height: 2;
        background: $panel;
        border-top: solid $frame;
    }

    #footer-row Footer {
        width: 1fr;
        height: 1;
        dock: none;
        background: $panel;
        color: $dull-brass;
    }

    FooterKey {
        background: $iron;
        color: $brass;
    }

    FooterKey.-command-palette {
        background: $alert;
        color: $bg;
    }

    FooterLabel {
        color: $bone;
    }

    #dialog {
        width: 66;
        height: auto;
        background: $surface;
        border: heavy $frame;
        padding: 1 2;
        border-title-color: $phosphor-bright;
        border-title-background: $bg;
        border-title-style: bold;
        border-subtitle-color: $dim-green;
        border-subtitle-align: center;
    }

    #boot-box {
        width: 62;
        height: auto;
        background: $bg;
        border: heavy $frame;
        padding: 1 2;
    }

    #boot-title {
        color: $phosphor-bright;
        text-style: bold;
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
        border: solid $frame;
        padding: 0 1;
        height: auto;
    }

    SelectionList > .selection-list--button {
        color: $bone;
        background: $surface;
    }

    SelectionList > .selection-list--button-highlighted {
        color: $bg;
        background: $phosphor-bright;
        text-style: bold;
    }

    SelectionList > .selection-list--button-selected {
        color: $phosphor-bright;
        text-style: bold;
    }

    Switch {
        border: tall $frame;
        background: $surface;
    }

    Switch .switch--slider {
        color: $iron;
        background: $bg;
    }

    Switch.-on .switch--slider {
        color: $phosphor-bright;
    }

    Switch:focus {
        border: tall $phosphor-bright;
    }

    Button {
        width: 22;
        margin-top: 1;
    }

    Button.-primary {
        background: $phosphor-bright;
        color: $bg;
    }

    #done-row {
        width: 100%;
        height: 3;
    }

    #done-btn {
        dock: right;
    }

    CommandPalette {
        background: $bg 60%;
    }

    CommandPalette #--container {
        width: 66;
        height: auto;
        border: heavy $frame;
        background: $surface;
        padding: 1 2;
    }

    CommandPalette #--input {
        border: hkey $frame;
        background: $panel;
    }

    CommandPalette #--input Label {
        color: $phosphor-bright;
        margin-top: 1;
        margin-left: 1;
    }

    CommandPalette #--input Button {
        min-width: 7;
        margin-right: 1;
    }

    CommandPalette #--results {
        overlay: none;
        height: auto;
    }

    SearchIcon {
        display: none;
    }

    CommandPalette Input {
        color: $phosphor-bright;
        background: $panel;
    }

    CommandPalette Input:focus {
        border: none;
        background: $panel;
    }

    CommandList {
        background: $surface;
    }

    CommandList > .option-list--option {
        color: $bone;
        background: $surface;
    }

    CommandList > .option-list--option-highlighted {
        color: $bg;
        background: $phosphor-bright;
        text-style: bold;
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
        self._crt_on: bool = bool(self._config["crt"])

        self._busy = False
        self._typing_active = False
        self._skip_typing = False
        self._last_chunks: list[dict] = []
        self._input_history: list[str] = []
        self._hist_idx: int | None = None
        self._hist_draft = ""
        self._pending_question = ""
        self._pending_by_era: dict[str, list[dict]] = {}

        self._panel_manual: bool = False
        self._status_text: str = ""
        self._headline_widget: Static | None = None
        self._typing_widget: Static | None = None
        self._scan_y: float = 0.0
        self._session_targets: dict[str, str] = {}
        self._scramble_states: dict[str, dict] = {}
        self._header_target: str = ""
        self._header_pos: int = 0
        self._banner_pairs: list[tuple[str, Style]] = []
        self._banner_idx: int = 0

        self._fx_scan_timer: Timer | None = None
        self._fx_flicker_timer: Timer | None = None
        self._fx_pulse_timer: Timer | None = None
        self._fx_scramble_timer: Timer | None = None
        self._fx_flash_timer: Timer | None = None
        self._fx_header_timer: Timer | None = None
        self._fx_banner_timer: Timer | None = None

        self._boot_screen: BootScreen | None = None
        self._boot_idx = 0
        self._boot_stage_started = time.monotonic()
        self._handled_worker_events: set[tuple[int, str]] = set()

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
            crt=self._crt_on,
        )
        try:
            CONFIG_PATH.write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def compose(self) -> ComposeResult:
        with Vertical():
            with Vertical(id="header"):
                yield Static("", id="header-line")
                yield Static("", id="session-tag")
            with Horizontal(id="main-row"):
                yield FillerPanel(id="gutter-left", classes="gutter")
                with Horizontal(id="main"):
                    with Vertical(id="log-frame"):
                        yield VerticalScroll(id="chat-scroll")
                        yield Static("", id="scanline")
                    with Vertical(id="sources-col"):
                        yield VerticalScroll(id="sources-list")
                        with Vertical(id="session-data"):
                            yield Static(markup=False, id="s-epoch")
                            yield Static(markup=False, id="s-count")
                            yield Static(markup=False, id="s-relevance")
                            yield Static(markup=False, id="s-theme")
                yield FillerPanel(id="gutter-right", classes="gutter")
            with Horizontal(id="status-row"):
                yield Static(markup=False, id="status")
                yield LoadingIndicator(id="spinner")
            with Horizontal(id="input-row"):
                yield Static(">", id="prompt")
                yield QuestionInput(
                    placeholder="Вопрос к архиву…  (/help — справка · Tab — автодополнение)",
                    id="question",
                )
            with Horizontal(id="footer-row"):
                yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log-frame").border_title = " ЖУРНАЛ "
        self.query_one("#sources-col").border_title = " ИСТОЧНИКИ ПОСЛЕДНЕГО ОТВЕТА "

        self._panel_manual = bool(self._config["panel"])
        self._apply_stage_width()
        if not self._panel_manual:
            self._panel_on = self._stage_width() >= PANEL_AUTO_WIDTH
        self._apply_panel_visibility()
        self._refresh_sources_placeholder()
        self._init_session_data()

        self._set_status("Готов к приёму запроса.")
        self._start_header_reveal()

        if self._sounds:
            play_sound("startup")
        if self._music_on:
            start_background_music()

        if not models_cached():
            self._start_boot()
        else:
            self.query_one("#question", QuestionInput).focus()

        if self._crt_on:
            self._start_crt_effects()

    def on_unmount(self) -> None:
        self._stop_timer(self._fx_header_timer)
        self._stop_timer(self._fx_banner_timer)
        self._fx_header_timer = None
        self._fx_banner_timer = None
        self._stop_crt_effects()
        if not self._panel_manual:
            self._panel_on = bool(self._config["panel"])
        self._save_config()
        stop_background_music()
        if self._sounds:
            play_sound("exit")

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width if event.size is not None else self._term_width()
        self._apply_stage_width(width)
        self._auto_panel(min(STAGE_MAX_WIDTH, width))

    @staticmethod
    def _stop_timer(timer: Timer | None) -> None:
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass

    def _term_width(self) -> int:
        size = self.size
        if size is not None and size.width > 0:
            return size.width
        return 80

    def _stage_width(self) -> int:
        return min(STAGE_MAX_WIDTH, self._term_width())

    def _apply_stage_width(self, width: int | None = None) -> None:
        if width is None:
            width = self._term_width()
        self.query_one("#main").styles.width = min(STAGE_MAX_WIDTH, max(40, width))

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

    def _banner_lines(self, width: int) -> list[tuple[str, Style]]:
        hint = "Вводите вопрос внизу. /help — справка, /sources — источники."
        if width >= 100:
            return [
                ("    ╱       ╲", STYLE_IRON),
                ("  •═════════════•", STYLE_BLOOD),
                ("  ARCHIVE TERMINAL", STYLE_BRASS),
                ("  ADEPTUS MECHANICUS", STYLE_BONE),
                ("", STYLE_DIM),
                (self._greeting, STYLE_DIM),
                (hint, STYLE_DIM),
            ]
        if width >= 60:
            return [
                ("  ARCHIVE TERMINAL", STYLE_BRASS),
                ("  ADEPTUS MECHANICUS", STYLE_BONE),
                ("  •═════════════•", STYLE_BLOOD),
                ("", STYLE_DIM),
                (self._greeting, STYLE_DIM),
                (hint, STYLE_DIM),
            ]
        return [("ARCHIVE TERMINAL", STYLE_BRASS)]

    def _print_banner(self) -> None:
        pairs = self._banner_lines(self._term_width())
        chat = self._chat()
        at_end = chat.is_vertical_scroll_end
        self._banner_pairs = pairs
        self._banner_idx = 0
        for text, _style in pairs:
            chat.mount(Static(Text(text, style=STYLE_DIM), markup=False, classes="banner"))
        if at_end:
            chat.scroll_end(force=True)
        self._fx_banner_timer = self.set_interval(BANNER_TICK, self._banner_tick)

    def _banner_tick(self) -> None:
        try:
            pairs = self._banner_pairs
            if self._banner_idx >= len(pairs):
                self._stop_timer(self._fx_banner_timer)
                self._fx_banner_timer = None
                return
            text, style = pairs[self._banner_idx]
            widgets = self._chat().query(".banner")
            if self._banner_idx < len(widgets):
                widgets[self._banner_idx].update(Text(text, style=style))
            self._banner_idx += 1
        except Exception:
            self._stop_timer(self._fx_banner_timer)
            self._fx_banner_timer = None

    def _start_header_reveal(self) -> None:
        self._header_target = "ARCHIVE TERMINAL · ADEPTUS MECHANICUS"
        self._header_pos = 0
        self._fx_header_timer = self.set_interval(HEADER_TICK, self._header_tick)

    def _header_tick(self) -> None:
        try:
            self._header_pos += 1
            line = self.query_one("#header-line")
            line.update(self._header_target[: self._header_pos])
            if self._header_pos >= len(self._header_target):
                self._stop_timer(self._fx_header_timer)
                self._fx_header_timer = None
                self.query_one("#session-tag").update(
                    f"┌ ПАРАМЕТРЫ СЕАНСА · {self._greeting} ┐"
                )
                self._print_banner()
        except Exception:
            self._stop_timer(self._fx_header_timer)
            self._fx_header_timer = None

    def _set_status(self, text: str) -> None:
        self.query_one("#status").update(Text(text))
        if text != self._status_text:
            self._flash_status()
        self._status_text = text

    def _flash_status(self) -> None:
        row = self.query_one("#status-row")
        row.add_class("-flash")
        self._stop_timer(self._fx_flash_timer)
        self._fx_flash_timer = self.set_timer(STATUS_FLASH, self._end_flash)

    def _end_flash(self) -> None:
        self._fx_flash_timer = None
        try:
            row = self.query_one("#status-row")
        except Exception:
            return
        row.remove_class("-flash")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.query_one("#question", QuestionInput).disabled = busy
        spinner = self.query_one("#spinner")
        row = self.query_one("#status-row")
        if busy:
            spinner.add_class("busy")
            row.add_class("-busy")
        else:
            spinner.remove_class("busy")
            row.remove_class("-busy")
            row.remove_class("-pulse-alt")
        if self._crt_on:
            if busy and self._fx_pulse_timer is None:
                self._fx_pulse_timer = self.set_interval(PULSE_TICK, self._pulse_tick)
            elif not busy:
                self._stop_timer(self._fx_pulse_timer)
                self._fx_pulse_timer = None

    def _pulse_tick(self) -> None:
        try:
            row = self.query_one("#status-row")
            row.toggle_class("-pulse-alt")
        except Exception:
            pass

    def _apply_panel_visibility(self) -> None:
        col = self.query_one("#sources-col")
        if self._panel_on:
            col.add_class("visible")
        else:
            col.remove_class("visible")

    def _auto_panel(self, width: int | None = None) -> None:
        if self._panel_manual:
            return
        if width is None:
            width = self._stage_width()
        show = width >= PANEL_AUTO_WIDTH
        if show != self._panel_on:
            self._panel_on = show
            self._apply_panel_visibility()

    def _set_panel(self, on: bool) -> None:
        self._panel_on = on
        self._panel_manual = True
        self._apply_panel_visibility()
        self._save_config()

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

    def _session_row(self, label: str, value: str) -> Text:
        return Text.assemble(
            (f"{label}  ", STYLE_DIM),
            (value, STYLE_BONE),
        )

    def _init_session_data(self) -> None:
        self._apply_session_values(
            {
                "s-epoch": "—",
                "s-count": "0",
                "s-relevance": "—",
                "s-theme": THEME_DISPLAY_NAMES.get(self.theme, self.theme),
            }
        )

    def _update_session_data(self, chunks: list[dict]) -> None:
        scores = [float(c.get("score", 0.0)) for c in chunks if c.get("score") is not None]
        relevance = f"{max(scores):.2f}" if scores else "—"
        self._apply_session_values(
            {
                "s-epoch": self._era_short(chunks),
                "s-count": str(len(chunks)),
                "s-relevance": relevance,
                "s-theme": THEME_DISPLAY_NAMES.get(self.theme, self.theme),
            }
        )

    @staticmethod
    def _era_short(chunks: list[dict]) -> str:
        counts: dict[str, int] = {}
        for chunk in chunks:
            era = chunk.get("era")
            if era:
                counts[era] = counts.get(era, 0) + 1
        if not counts:
            return "—"
        era = max(counts, key=counts.get)
        return {"heresy": "M30-31", "current": "M41-42"}.get(era, "—")

    def _apply_session_values(self, values: dict[str, str]) -> None:
        for widget_id, value in values.items():
            if self._session_targets.get(widget_id) == value:
                continue
            self._session_targets[widget_id] = value
            widget = self.query_one(f"#{widget_id}", Static)
            if self._crt_on:
                self._scramble_value(widget_id, SESSION_LABELS[widget_id], value)
            else:
                widget.update(self._session_row(SESSION_LABELS[widget_id], value))

    def _scramble_value(self, widget_id: str, label: str, value: str) -> None:
        state = self._scramble_states.get(widget_id)
        if state is None:
            state = {"label": label, "target": value, "shown": 0}
            self._scramble_states[widget_id] = state
        else:
            state["label"] = label
            state["target"] = value
            state["shown"] = min(state["shown"], len(value))
        if self._fx_scramble_timer is None:
            self._fx_scramble_timer = self.set_interval(SCRAMBLE_TICK, self._scramble_tick)

    def _scramble_tick(self) -> None:
        if not self._crt_on:
            self._scramble_finish_all()
            return
        done: list[str] = []
        for widget_id, state in list(self._scramble_states.items()):
            target = state["target"]
            shown = min(state["shown"] + SCRAMBLE_STEP, len(target))
            state["shown"] = shown
            widget = self.query_one(f"#{widget_id}", Static)
            if shown >= len(target):
                done.append(widget_id)
                widget.update(self._session_row(state["label"], target))
            else:
                tail = "".join(
                    random.choice(SCRAMBLE_GLYPHS) for _ in range(len(target) - shown)
                )
                widget.update(self._session_row(state["label"], target[:shown] + tail))
        for widget_id in done:
            self._scramble_states.pop(widget_id, None)
        if not self._scramble_states:
            self._stop_timer(self._fx_scramble_timer)
            self._fx_scramble_timer = None

    def _scramble_finish_all(self) -> None:
        for widget_id, state in list(self._scramble_states.items()):
            try:
                self.query_one(f"#{widget_id}", Static).update(
                    self._session_row(state["label"], state["target"])
                )
            except Exception:
                pass
        self._scramble_states.clear()
        self._stop_timer(self._fx_scramble_timer)
        self._fx_scramble_timer = None

    def action_toggle_sources(self) -> None:
        self._set_panel(not self._panel_on)
        if self._panel_on and not self._last_chunks:
            self._set_status("Источников пока нет — сначала задайте вопрос.")
        else:
            self._set_status(
                "Панель источников " + ("открыта." if self._panel_on else "скрыта.")
            )

    def set_theme(self, name: str) -> None:
        if name not in THEME_NAMES:
            return
        self.theme = name
        self._save_config()
        self._set_status(f"Тема: {THEME_DISPLAY_NAMES[name]}.")
        self._apply_session_values({"s-theme": THEME_DISPLAY_NAMES[name]})

    def action_cycle_theme(self) -> None:
        idx = (THEME_NAMES.index(self.theme) + 1) % len(THEME_NAMES)
        self.set_theme(THEME_NAMES[idx])

    def action_scroll_log_up(self) -> None:
        self._chat().scroll_page_up()

    def action_scroll_log_down(self) -> None:
        self._chat().scroll_page_down()

    def action_clear_log(self) -> None:
        self._chat().remove_children()
        self._set_status("Журнал очищен.")

    def action_settings(self) -> None:
        self.push_screen(SettingsModal())

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_command_palette(self) -> None:
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

    @staticmethod
    def _format_reply(text: str) -> tuple[str | None, Text]:
        lines = text.split("\n")
        heading = None
        body = text
        for i, line in enumerate(lines):
            s = line.strip()
            if not s:
                continue
            if s.startswith("**") and s.endswith("**") and s.count("**") == 2:
                heading = s[2:-2].strip()
                body = "\n".join(lines[i + 1:])
            break

        out = Text()
        for i, part in enumerate(body.strip().split("**")):
            if not part:
                continue
            if i % 2 == 1:
                out.append(
                    part,
                    style=Style.combine((STYLE_DARK_GREEN, Style(bold=True))),
                )
            else:
                out.append(part, style=STYLE_DARK_GREEN)
        return heading, out

    def _type_reply(self, text: str) -> None:
        heading, body = self._format_reply(text)
        self._typing_active = True
        chat = self._chat()
        at_end = chat.is_vertical_scroll_end

        self._headline_widget = None
        if heading:
            band = Static(Text(""), markup=False, classes="hl-band")
            chat.mount(band)
            self._headline_widget = band

        self._typing_widget = Static(Text(""), markup=False)
        chat.mount(self._typing_widget)
        if at_end:
            chat.scroll_end(force=True)
        self._type_out_worker(heading, body)

    @work(thread=False, group="typing", exclusive=True)
    async def _type_out_worker(self, heading: str | None, body: Text) -> None:
        band = self._headline_widget
        widget = self._typing_widget
        chat = self._chat()
        delay = self._typing_delay
        try:
            if heading is not None and band is not None:
                full = Text(heading)
                if delay <= 0:
                    band.update(full)
                else:
                    n = 0
                    total = len(full)
                    while n < total:
                        if self._skip_typing:
                            break
                        n = min(n + 4, total)
                        at_end = chat.is_vertical_scroll_end
                        band.update(full[:n])
                        if at_end:
                            chat.scroll_end(force=True)
                        await asyncio.sleep(delay * 4)
                    band.update(full)

            if delay <= 0:
                widget.update(body)
            else:
                n = 0
                total = len(body)
                while n < total:
                    if self._skip_typing:
                        break
                    n = min(n + 4, total)
                    at_end = chat.is_vertical_scroll_end
                    widget.update(body[:n])
                    if at_end:
                        chat.scroll_end(force=True)
                    await asyncio.sleep(delay * 4)
                widget.update(body)
        finally:
            try:
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
            except Exception:
                self._skip_typing = False
                self._typing_active = False

    def on_key(self, event: events.Key) -> None:
        if self._typing_active:
            self._skip_typing = True
            event.stop()

    @work(thread=True, group="answer", exclusive=True)
    def _ask_worker(self, question: str, era_override: list[dict] | None):
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
            return
        self._handled_worker_events.add(key)
        if worker.name == "_boot_worker_run":
            self._finish_boot_worker(worker)
        elif worker.name == "_ask_worker":
            self._finish_ask(worker)

    @work(thread=False, group="boot-finish", exclusive=True)
    async def _finish_boot_worker(self, worker: Worker) -> None:
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
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.query_one("#chat-scroll", VerticalScroll)
                return
            except Exception:
                await asyncio.sleep(0.01)
        self.query_one("#chat-scroll", VerticalScroll)

    def _finish_ask(self, worker: Worker) -> None:
        result = worker.result
        kind = result.get("kind")

        if kind == "ok":
            chunks = result.get("chunks", [])
            self._update_session_data(chunks)
            self._update_sources(chunks)
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

    def _scan_tick(self) -> None:
        if not self._crt_on:
            return
        try:
            frame = self.query_one("#log-frame")
            height = max(1, frame.content_size.height - 1)
            self._scan_y += height / (SCANLINE_PERIOD / SCANLINE_TICK)
            self.query_one("#scanline").styles.offset = Offset(0, int(self._scan_y) % height)
        except Exception:
            pass

    def _schedule_flicker(self) -> None:
        if not self._crt_on:
            return
        self._fx_flicker_timer = self.set_timer(
            random.uniform(FLICKER_MIN, FLICKER_MAX), self._do_flicker
        )

    def _do_flicker(self) -> None:
        self._fx_flicker_timer = None
        if not self._crt_on:
            return
        band = self._headline_widget
        if band is not None and band.is_attached:
            band.add_class("-flicker")
            self._fx_flicker_timer = self.set_timer(
                random.uniform(FLICKER_HOLD_MIN, FLICKER_HOLD_MAX), self._end_flicker
            )
        else:
            self._schedule_flicker()

    def _end_flicker(self) -> None:
        self._fx_flicker_timer = None
        band = self._headline_widget
        if band is not None and band.is_attached:
            band.remove_class("-flicker")
        self._schedule_flicker()

    def _start_crt_effects(self) -> None:
        if not self._crt_on:
            return
        self.query_one("#scanline").add_class("-on")
        if self._fx_scan_timer is None:
            self._fx_scan_timer = self.set_interval(SCANLINE_TICK, self._scan_tick)
        self._schedule_flicker()
        if self._busy and self._fx_pulse_timer is None:
            self._fx_pulse_timer = self.set_interval(PULSE_TICK, self._pulse_tick)

    def _stop_crt_effects(self) -> None:
        for timer in (
            self._fx_scan_timer,
            self._fx_flicker_timer,
            self._fx_pulse_timer,
            self._fx_scramble_timer,
            self._fx_flash_timer,
        ):
            self._stop_timer(timer)
        self._fx_scan_timer = None
        self._fx_flicker_timer = None
        self._fx_pulse_timer = None
        self._fx_scramble_timer = None
        self._fx_flash_timer = None
        self._scramble_finish_all()
        try:
            self.query_one("#scanline").remove_class("-on")
            self.query_one("#status-row").remove_class("-pulse-alt", "-flash")
        except Exception:
            pass
        if self._headline_widget is not None and self._headline_widget.is_attached:
            self._headline_widget.remove_class("-flicker")


def _prewarm_subprocess_primitives() -> None:
    try:
        from tqdm import tqdm as _tqdm

        _tqdm.get_lock()
    except Exception:
        pass


def run() -> None:
    _prewarm_subprocess_primitives()
    MechanicusApp().run()


if __name__ == "__main__":
    run()
