import asyncio
import contextlib
import ctypes
import json
import os
import random
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from rich.style import Style
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.timer import Timer
from textual.widgets import (
    Button,
    Input,
    LoadingIndicator,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, WorkerState

from auth import change_password, verify_credentials
from generator import (
    DEEPSEEK_BASE_URL,
    ERA_LABELS,
    AccessDeniedError,
    EraClarificationNeeded,
    TokensExhaustedError,
    answer,
)
from persona import IDENTITY_RESPONSES, _is_identity_question
from retriever import BOOT_STAGES, models_cached, warm_up
from startup import RevealEffects, StartupManager
from ui import (
    GREETING_VARIANTS,
    play_sound,
    start_background_music,
    stop_background_music,
)

try:
    import psutil
except ImportError:
    psutil = None

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
STYLE_PHOSPHOR = Style(color=PHOSPHOR_BRIGHT)

TYPEWRITER_DELAY = 0.012

STAGE_MAX_WIDTH = 132
PANEL_AUTO_WIDTH = 120
SCENE_WIDTH_DOCKED = 158
DOCK_WIDTH = 26
DOCK_MIN_LOG_WIDTH = 50

CLOCK_INTERVAL = 1.0
DOCK_DATA_INTERVAL = 5.0
PING_INTERVAL = 10.0
PING_PORT = 443
PING_TIMEOUT = 2.0
PING_GOOD_MS = 150
PING_WARN_MS = 400
PING_HOST = urlsplit(DEEPSEEK_BASE_URL).hostname or "api.deepseek.com"

WEEKDAY_NAMES = ("ПОНЕДЕЛЬНИК", "ВТОРНИК", "СРЕДА", "ЧЕТВЕРГ", "ПЯТНИЦА", "СУББОТА", "ВОСКРЕСЕНЬЕ")
MONTH_NAMES = ("ЯНВАРЯ", "ФЕВРАЛЯ", "МАРТА", "АПРЕЛЯ", "МАЯ", "ИЮНЯ", "ИЮЛЯ", "АВГУСТА", "СЕНТЯБРЯ", "ОКТЯБРЯ", "НОЯБРЯ", "ДЕКАБРЯ")

STATUS_FLASH = 0.15
HEADER_TICK = 0.05
BANNER_TICK = 0.25
BOOT_TYPE_TICK = 0.03
BOOT_TYPE_STEP = 2

LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_WIDTH = 40
STARTUP_LINE_TEXT = "▌ ЗАПУСК АРХИВНОГО ТЕРМИНАЛА"
STARTUP_BOOT_PAUSE = 0.8
STARTUP_FRAME_FADE = 0.3
STARTUP_ROW_FADE = 0.15
STARTUP_GUTTER_FADE = 0.3
STARTUP_STATUS_PAUSE = 0.2

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
    "/passwd",
    "/quit",
    "/sources",
    "/theme",
)

COMMAND_DESCRIPTIONS = {
    "/clear": "очистить журнал",
    "/exit": "завершить сеанс",
    "/help": "справка по клавишам",
    "/passwd": "сменить пароль",
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
    "left_dock": False,
    "startup_reveal": True,
}

EXPECTED_BOOT_SECONDS = [1.0, 4.0, 5.0, 90.0]

_DEVNULL_STDERR = open(os.devnull, "w", encoding="utf-8")


class _WinPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _battery_text() -> str:
    if psutil is not None:
        try:
            battery = psutil.sensors_battery()
        except Exception:
            battery = None
        if battery is not None:
            return f"{round(battery.percent)}%"
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(["pmset", "-g", "batt"], text=True)
            for part in out.split():
                if part.rstrip(";,").endswith("%"):
                    digits = part.rstrip(";,%")
                    if digits.isdigit():
                        return f"{int(digits)}%"
            return "ОТ СЕТИ"
        if sys.platform == "linux":
            for base in ("/sys/class/power_supply/BAT0", "/sys/class/power_supply/BAT1"):
                cap_file = Path(base) / "capacity"
                if cap_file.exists():
                    return f"{int(cap_file.read_text(encoding='utf-8').strip())}%"
        if sys.platform == "win32":
            status = _WinPowerStatus()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                if status.BatteryLifePercent == 255:
                    return "ОТ СЕТИ"
                return f"{status.BatteryLifePercent}%"
    except Exception:
        pass
    return "Н/Д"


def _measure_ping(host: str) -> tuple[str, Style]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, PING_PORT), timeout=PING_TIMEOUT):
            pass
    except OSError:
        return "ОФЛАЙН", STYLE_BLOOD
    ms = (time.monotonic() - started) * 1000
    if ms < PING_GOOD_MS:
        style = STYLE_GREEN
    elif ms <= PING_WARN_MS:
        style = STYLE_BRASS
    else:
        style = STYLE_BLOOD
    return f"{ms:.0f} мс", style


def _make_theme(
    name: str, bg: str, surface: str, panel: str, fg: str, frame: str
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
        },
    )


def _all_themes() -> list[Theme]:
    return [
        _make_theme("mechanicus", "#0a0c0a", "#0f130f", "#161d16", BONE, "#445544"),
        _make_theme("mechanicus-dark", "#030403", "#080a08", "#0e120e", "#e8c9a5", "#5f8c3a"),
        _make_theme("mechanicus-void", "#101308", "#161c0f", "#1d2512", "#d9bfa0", "#3a4434"),
    ]


def _build_input_bindings() -> list[Binding]:
    removed = {"ctrl+u", "ctrl+d"}
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
    def __init__(self, *args, seed: int = 0, **kwargs) -> None:
        self._seed = seed
        super().__init__(*args, **kwargs)

    def render(self) -> Text:
        width, height = self.size
        if width < 2 or height < 1:
            return Text("")
        glyphs = " ░▒▓▚▞"
        out = Text()
        for y in range(height):
            row = []
            for x in range(width):
                value = (x * 7 + y * 13 + self._seed * 37) % 97
                if value < 44:
                    row.append(" ")
                elif value < 92:
                    row.append(glyphs[(x * 5 + y * 11 + self._seed * 17) % len(glyphs)])
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
    """Расширенная справка: вкладки «Команды» и «Клавиши» в окне 72 колонки.

    Описания получают всю ширину вкладки (68 колонок); в описаниях команд
    упомянуты клавиши-дублёры, в описаниях клавиш — команды-дублёры.
    """

    CSS = """
    #dialog {
        width: 72;
        max-width: 100%;
        padding: 1 1;
    }

    TabbedContent {
        height: auto;
        border: none;
        background: $surface;
    }

    TabbedContent TabPane {
        padding: 1 0 0 0;
        border: none;
        background: $surface;
    }

    TabbedContent Tab {
        padding: 0 2;
        color: $dim-green;
    }

    TabbedContent Tab.-active {
        color: $phosphor-bright;
        text-style: bold;
    }

    TabbedContent Underline > .underline--bar {
        color: $phosphor-bright;
        background: $frame;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("enter", "close", "Закрыть", show=False),
        Binding("left", "previous_tab", "Предыдущая вкладка", show=False),
        Binding("right", "next_tab", "Следующая вкладка", show=False),
        Binding("tab", "next_tab", "Следующая вкладка", show=False),
        Binding("shift+tab", "previous_tab", "Предыдущая вкладка", show=False),
    ]

    def action_close(self) -> None:
        self.dismiss(None)

    def action_next_tab(self) -> None:
        self._cycle_tab(1)

    def action_previous_tab(self) -> None:
        self._cycle_tab(-1)

    def _cycle_tab(self, step: int) -> None:
        content = self.query_one(TabbedContent)
        tab_ids = ["commands", "keys"]
        try:
            idx = tab_ids.index(content.active)
        except ValueError:
            idx = 0
        content.active = tab_ids[(idx + step) % len(tab_ids)]

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                with TabbedContent(initial="commands"):
                    with TabPane("КОМАНДЫ", id="commands"):
                        yield Static(self._commands_text(), markup=False)
                    with TabPane("КЛАВИШИ", id="keys"):
                        yield Static(self._keys_text(), markup=False)
                yield Static(
                    Text(
                        "← / → или Tab — вкладки · Esc / Enter — закрыть",
                        style=STYLE_DIM,
                    ),
                    markup=False,
                )

    def on_mount(self) -> None:
        dialog = self.query_one("#dialog")
        dialog.border_title = " СПРАВКА "
        dialog.border_subtitle = " КОМАНДЫ И ГОРЯЧИЕ КЛАВИШИ "
        self.query_one(TabbedContent).focus()

    @staticmethod
    def _commands_text() -> Text:
        """Вкладка «Команды»: слэш-команды, в описаниях — клавиши-дублёры."""
        rows = [
            ("/clear", "очистить журнал", "Ctrl+L"),
            ("/exit", "завершить сеанс", "Ctrl+C / Ctrl+Q"),
            ("/quit", "синоним /exit", ""),
            ("/help", "открыть эту справку", "F1"),
            ("/passwd", "сменить пароль", ""),
            ("/sources", "источники последнего ответа", "Ctrl+F"),
            ("/theme", "следующая тема", "Ctrl+T"),
        ]
        out = Text()
        out.append(
            "Команды вводятся в строке вопроса; клавиша-дублёр — в описании.\n\n",
            style=STYLE_DIM,
        )
        for command, desc, key in rows:
            HelpModal._append_row(
                out, command, STYLE_PHOSPHOR, desc, "— то же, что", key
            )
        return out

    @staticmethod
    def _keys_text() -> Text:
        """Вкладка «Клавиши»: горячие клавиши, в описаниях — команды-дублёры."""
        rows = [
            ("Ctrl+F", "панель источников последнего ответа", "/sources"),
            ("Ctrl+B", "левая панель «Система»", ""),
            ("Ctrl+S", "настройки: звук, печатная машинка, музыка", ""),
            ("Ctrl+T", "следующая тема", "/theme"),
            ("Ctrl+U/D", "прокрутка журнала вверх/вниз", ""),
            ("PgUp/PgDn", "прокрутка журнала (и колесо мыши)", ""),
            ("↑ / ↓", "история вопросов в строке ввода", ""),
            ("Tab", "автодополнение команд: /so → /sources, Shift+Tab — назад", ""),
            ("Ctrl+L", "очистить журнал", "/clear"),
            ("F1", "эта справка", "/help"),
            ("Ctrl+C/Q", "выход", "/exit"),
            ("Esc", "закрыть окно справки", ""),
        ]
        out = Text()
        out.append(
            "Клавиши работают в главном окне; команда-дублёр — в описании.\n\n",
            style=STYLE_DIM,
        )
        for key, desc, command in rows:
            HelpModal._append_row(
                out, key, STYLE_BRASS, desc, "— команда", command
            )
        out.append("\n", style=STYLE_IRON)
        out.append(
            "Любая клавиша во время печати ответа допечатывает его мгновенно.",
            style=STYLE_DIM,
        )
        out.append("\n")
        return out

    @staticmethod
    def _append_row(
        out: Text,
        key: str,
        key_style: Style,
        desc: str,
        ref_prefix: str,
        ref: str,
    ) -> None:
        """Строка «ключ | описание» с переносом описания висячим отступом.

        Ключ занимает колонку 10, описание переносится по словам, упоминание
        клавиши/команды-дублёра пристыковывается к описанию приглушённым
        цветом.
        """
        capacity = 68 - 2 - 10
        out.append("  ", style=STYLE_IRON)
        out.append(f"{key:<10}", style=key_style)
        line_len = 2 + 10

        # Описание переносится по словам с висячим отступом.
        for word in desc.split(" "):
            if line_len + 1 + len(word) > capacity and line_len > 2 + 10:
                out.append("\n", style=STYLE_IRON)
                out.append(" " * 12, style=STYLE_IRON)
                line_len = 12
            else:
                if line_len > 2 + 10:
                    out.append(" ", style=STYLE_BONE)
                    line_len += 1
            out.append(word, style=STYLE_BONE)
            line_len += len(word)

        # Упоминание клавиши/команды-дублёра — неразрывный блок: если не
        # помещается на текущей строке, переносится целиком на следующую.
        if ref:
            block = (ref_prefix + " " + ref).split(" ")
            block_len = sum(len(word) for word in block) + (len(block) - 1)
            if line_len + 1 + block_len > capacity and line_len > 2 + 10:
                out.append("\n", style=STYLE_IRON)
                out.append(" " * 12, style=STYLE_IRON)
                line_len = 12
            for word in block:
                if line_len > 2 + 10:
                    out.append(" ", style=STYLE_DIM)
                    line_len += 1
                out.append(word, style=STYLE_DIM)
                line_len += len(word)
        out.append("\n")


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
                    yield Static("Левая панель", classes="setting-label")
                    yield Switch(value=False, id="sw-dock")
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
        self.query_one("#sw-dock", Switch).value = app._dock_on
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
        elif switch_id == "sw-dock":
            if app._dock_on == value:
                return
            if not app._set_dock(value):
                self.query_one("#sw-dock", Switch).value = app._dock_on
                return
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


class LoginScreen(Screen):
    BINDINGS = [
        Binding("escape", "quit_login", "Выход"),
        Binding("up", "focus_login", "Логин", show=False),
        Binding("down", "focus_password", "Пароль", show=False),
    ]

    CSS = """
    Screen {
        background: $bg;
    }
    #login-window {
        width: 40;
        height: auto;
    }
    #login-top, #login-bottom {
        width: 100%;
        height: auto;
        color: $phosphor-bright;
        text-style: bold;
    }
    #login-body {
        width: 100%;
        height: auto;
        background: $bg;
        padding: 1 2;
    }
    #login-label, #password-label {
        width: 100%;
        color: $dim-green;
        margin-top: 1;
    }
    #login-error {
        width: 100%;
        height: 1;
        color: $blood;
        margin-top: 1;
    }
    #login-hint {
        width: 100%;
        height: 1;
        color: $dim-green;
        margin-top: 1;
    }
    #login-body Input {
        width: 100%;
        background: $surface;
        color: $bone;
        border: none;
        padding: 0 1;
    }
    #login-body Input:focus {
        background: $panel;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._attempts = 0
        self._collapsing = False

    def compose(self) -> ComposeResult:
        outer = "╔" + "═" * (LOGIN_WINDOW_WIDTH - 2) + "╗"
        inner = "┌" + "─" * (LOGIN_WINDOW_WIDTH - 2) + "┐"
        outer_bottom = "╚" + "═" * (LOGIN_WINDOW_WIDTH - 2) + "╝"
        inner_bottom = "└" + "─" * (LOGIN_WINDOW_WIDTH - 2) + "┘"
        with Center():
            with Vertical(id="login-window"):
                yield Static(f"{outer}\n{inner}", id="login-top", markup=False)
                with Vertical(id="login-body"):
                    yield Static("LOGIN", id="login-label", markup=False)
                    yield Input(id="login-input", placeholder="введите логин")
                    yield Static("PASSWORD", id="password-label", markup=False)
                    yield Input(id="password-input", password=True, placeholder="введите пароль")
                    yield Static("", id="login-error", markup=False)
                    yield Static("↑/↓ / Tab — переключение · Enter — вход · Esc — выход", id="login-hint", markup=False)
                yield Static(f"{inner_bottom}\n{outer_bottom}", id="login-bottom", markup=False)

    def on_mount(self) -> None:
        self.query_one("#login-input", Input).focus()

    def action_quit_login(self) -> None:
        self.dismiss(False)

    def action_focus_password(self) -> None:
        self.query_one("#password-input", Input).focus()

    def action_focus_login(self) -> None:
        self.query_one("#login-input", Input).focus()

    @on(Input.Submitted)
    async def _on_submitted(self, event: Input.Submitted) -> None:
        if self._collapsing:
            return
        self._collapsing = True
        login = self.query_one("#login-input", Input).value.strip()
        password = self.query_one("#password-input", Input).value
        self.query_one("#login-input", Input).disabled = True
        self.query_one("#password-input", Input).disabled = True
        self._check_worker(login, password)

    @work(thread=True, group="login-check", exclusive=True)
    def _check_worker(self, login: str, password: str) -> None:
        self.app.call_from_thread(self._process_login, verify_credentials(login, password))

    @work(thread=False, group="login-apply", exclusive=True)
    async def _process_login(self, ok: bool) -> None:
        login_input = self.query_one("#login-input", Input)
        password_input = self.query_one("#password-input", Input)
        if ok:
            self.query_one("#login-error").update(Text("ДОСТУП РАЗРЕШЁН", style=STYLE_GREEN))
            await self._collapse()
            return
        self._attempts += 1
        left = max(0, LOGIN_MAX_ATTEMPTS - self._attempts)
        if left == 0:
            self.query_one("#login-error").update(Text("Доступ запрещён. Завершение.", style=STYLE_BLOOD))
            await asyncio.sleep(1.0)
            self.dismiss(False)
            return
        self.query_one("#login-error").update(
            Text(f"Неверный логин или пароль. Осталось попыток: {left}", style=STYLE_BLOOD)
        )
        password_input.value = ""
        await asyncio.sleep(self._attempts)
        login_input.disabled = False
        password_input.disabled = False
        login_input.focus()
        self._collapsing = False

    async def _collapse(self) -> None:
        self.query_one("#login-body").styles.display = "none"
        top = self.query_one("#login-top")
        bottom = self.query_one("#login-bottom")
        band = "╔" + "═" * (LOGIN_WINDOW_WIDTH - 2) + "╗"
        thin = "┌" + "─" * (LOGIN_WINDOW_WIDTH - 2) + "┐"
        band_bottom = "╚" + "═" * (LOGIN_WINDOW_WIDTH - 2) + "╝"
        thin_bottom = "└" + "─" * (LOGIN_WINDOW_WIDTH - 2) + "┘"
        steps = 5
        for step in range(1, steps + 1):
            top.update(Text("\n".join(band if i % 2 == 0 else thin for i in range(step))))
            bottom.update(Text("\n".join(band_bottom if (step - 1 - i) % 2 == 0 else thin_bottom for i in range(step))))
            await asyncio.sleep(0.09)
        self.dismiss(True)


class PasswordModal(ModalScreen):
    BINDINGS = [Binding("escape", "close", "Закрыть")]

    def action_close(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="dialog"):
                yield Static("Текущий пароль", classes="setting-label", markup=False)
                yield Input(password=True, id="pw-old")
                yield Static("Новый пароль", classes="setting-label", markup=False)
                yield Input(password=True, id="pw-new")
                yield Static("Повторите новый пароль", classes="setting-label", markup=False)
                yield Input(password=True, id="pw-repeat")
                yield Static("", id="pw-error", markup=False)
                yield Static("Esc — отмена", id="pw-hint", markup=False)
                with Horizontal(id="done-row"):
                    yield Button("Сменить", id="done-btn", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#dialog").border_title = " СМЕНА ПАРОЛЯ "
        self.query_one("#pw-old", Input).focus()

    @on(Button.Pressed)
    def _on_done(self) -> None:
        current = self.query_one("#pw-old", Input).value
        new = self.query_one("#pw-new", Input).value
        repeat = self.query_one("#pw-repeat", Input).value
        if new != repeat:
            self.query_one("#pw-error").update(Text("Новые пароли не совпадают.", style=STYLE_BLOOD))
            return
        if len(new) < 4:
            self.query_one("#pw-error").update(Text("Пароль слишком короткий.", style=STYLE_BLOOD))
            return
        self._change_worker(current, new)

    @work(thread=True, group="passwd-check", exclusive=True)
    def _change_worker(self, current: str, new: str) -> None:
        self.app.call_from_thread(self._apply_change, change_password(current, new))

    def _apply_change(self, ok: bool) -> None:
        if ok:
            self.app._set_status("Пароль изменён.")
            self.dismiss(None)
        else:
            self.query_one("#pw-error").update(Text("Неверный текущий пароль.", style=STYLE_BLOOD))


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

    .reveal-hidden {
        opacity: 0;
        visibility: hidden;
    }

    #startup-line {
        width: 100%;
        height: 1;
        text-align: center;
        color: $dim-green;
        display: none;
    }

    #pw-error {
        width: 100%;
        height: 1;
        color: $blood;
    }

    #pw-hint {
        width: 100%;
        height: 1;
        color: $dim-green;
    }

    #dialog Input {
        width: 100%;
        background: $surface;
        color: $bone;
        border: none;
        padding: 0 1;
    }

    #dialog Input:focus {
        background: $panel;
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

    #dock-col {
        width: 26;
        height: 1fr;
        display: none;
        background: $surface;
        border: double $frame;
        border-title-color: $phosphor-bright;
        border-title-background: $bg;
        border-title-style: bold;
        padding: 0 1;
    }

    #dock-col.visible {
        display: block;
    }

    #clock {
        width: 100%;
        height: 1;
        text-align: center;
        color: $phosphor-bright;
        text-style: bold;
    }

    #dock-dow, #dock-date {
        width: 100%;
        height: 1;
        text-align: center;
        color: $dim-green;
    }

    #dock-filler {
        width: 100%;
        height: 1fr;
        background: $surface;
    }

    #dock-data {
        width: 100%;
        height: 3;
        background: $surface;
        border-top: solid $frame;
    }

    .dock-row {
        width: 100%;
        height: 1;
    }

    .dock-label {
        width: 8;
        color: $dim-green;
    }

    .dock-value {
        width: 1fr;
        color: $bone;
        content-align: right middle;
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
        height: 2;
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
        border: none;
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
        background: $panel;
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
    """

    BINDINGS = [
        Binding("ctrl+f", "toggle_sources", "Источники"),
        Binding("ctrl+b", "toggle_dock", "Система"),
        Binding("ctrl+t", "cycle_theme", "Тема"),
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
        self._dock_on: bool = bool(self._config["left_dock"])
        self._startup_reveal: bool = bool(self._config["startup_reveal"])
        self._startup_done: bool = not self._startup_reveal

        self._busy = False
        self._typing_active = False
        self._skip_typing = False
        self._last_chunks: list[dict] = []
        self._input_history: list[str] = []
        self._hist_idx: int | None = None
        self._hist_draft = ""
        self._pending_question = ""
        self._pending_by_era: dict[str, list[dict]] = {}

        self._panel_manual: bool = bool(self._config["panel"])
        self._dock_manual: bool = bool(self._config["left_dock"])
        self._last_clock: str = ""
        self._last_dow: str = ""
        self._last_date: str = ""
        self._last_power: str = ""
        self._last_ping: str = ""
        self._status_text: str = ""
        self._headline_widget: Static | None = None
        self._typing_widget: Static | None = None
        self._header_target: str = ""
        self._header_pos: int = 0
        self._banner_pairs: list[tuple[str, Style]] = []
        self._banner_idx: int = 0
        self._header_event: asyncio.Event | None = None
        self._banner_event: asyncio.Event | None = None
        self._startup = StartupManager(self)

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
            left_dock=self._dock_on,
            startup_reveal=self._startup_reveal,
        )
        try:
            CONFIG_PATH.write_text(
                json.dumps(self._config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def compose(self) -> ComposeResult:
        reveal = self._startup_reveal
        with Vertical():
            with Vertical(id="header"):
                yield Static("", id="header-line")
                yield Static("", id="session-tag")
            yield Static(STARTUP_LINE_TEXT, id="startup-line", markup=False)
            with Horizontal(id="main-row"):
                yield FillerPanel(id="gutter-left", classes="gutter reveal-hidden" if reveal else "gutter")
                with Horizontal(id="main"):
                    with Vertical(id="dock-col", classes="reveal-hidden" if reveal else ""):
                        yield Static("", id="clock", markup=False, classes="reveal-hidden" if reveal else "")
                        yield Static("", id="dock-dow", markup=False, classes="reveal-hidden" if reveal else "")
                        yield Static("", id="dock-date", markup=False, classes="reveal-hidden" if reveal else "")
                        yield FillerPanel(id="dock-filler", seed=7, classes="gutter reveal-hidden" if reveal else "gutter")
                        with Vertical(id="dock-data", classes="reveal-hidden" if reveal else ""):
                            with Horizontal(classes="dock-row reveal-hidden" if reveal else "dock-row"):
                                yield Static("Питание", classes="dock-label")
                                yield Static("Н/Д", id="d-power", classes="dock-value", markup=False)
                            with Horizontal(classes="dock-row reveal-hidden" if reveal else "dock-row"):
                                yield Static("Пинг", classes="dock-label")
                                yield Static("—", id="d-ping", classes="dock-value", markup=False)
                    with Vertical(id="log-frame", classes="reveal-hidden" if reveal else ""):
                        yield VerticalScroll(id="chat-scroll")
                    with Vertical(id="sources-col", classes="reveal-hidden" if reveal else ""):
                        yield VerticalScroll(id="sources-list")
                yield FillerPanel(id="gutter-right", classes="gutter reveal-hidden" if reveal else "gutter")
            with Horizontal(id="status-row", classes="reveal-hidden" if reveal else ""):
                yield Static(markup=False, id="status", classes="reveal-hidden" if reveal else "")
                yield LoadingIndicator(id="spinner")
            with Horizontal(id="input-row", classes="reveal-hidden" if reveal else ""):
                yield Static(">", id="prompt", classes="reveal-hidden" if reveal else "")
                yield QuestionInput(
                    placeholder="Вопрос к архиву…  (/help — справка · Tab — автодополнение)",
                    id="question",
                    classes="reveal-hidden" if reveal else "",
                )

    def on_mount(self) -> None:
        self.query_one("#log-frame").border_title = " ЖУРНАЛ "
        self.query_one("#sources-col").border_title = " ИСТОЧНИКИ ПОСЛЕДНЕГО ОТВЕТА "
        self.query_one("#dock-col").border_title = " СИСТЕМА "

        self._panel_manual = bool(self._config["panel"])
        self._dock_manual = bool(self._config["left_dock"])
        if not self._dock_manual:
            self._dock_on = self._dock_auto_for(self._term_width())
        self._apply_dock_visibility()
        self._apply_stage_width()
        if not self._panel_manual:
            self._panel_on = self._stage_width() - (DOCK_WIDTH if self._dock_on else 0) >= PANEL_AUTO_WIDTH
        self._apply_panel_visibility()
        self._refresh_sources_placeholder()
        self._init_dock_data()
        self.set_interval(CLOCK_INTERVAL, self._tick_clock)
        self.set_interval(DOCK_DATA_INTERVAL, self._tick_dock_data)
        self.set_interval(PING_INTERVAL, self._tick_ping)

        self._set_status("")
        if self._startup_reveal:
            self.query_one("#question", QuestionInput).disabled = True

        if self._sounds:
            play_sound("startup")

        self.push_screen(LoginScreen(), self._on_login_done)

    def _on_login_done(self, ok: bool | None) -> None:
        if not ok:
            self.exit()
            return
        if self._music_on:
            start_background_music()
        if not models_cached():
            self._start_boot()
        elif self._startup_reveal:
            self._startup.start()
        else:
            self._set_status("Готов к приёму запроса.")
            self._start_header_reveal()
            self.query_one("#question", QuestionInput).focus()

    def on_unmount(self) -> None:
        self._stop_timer(self._fx_header_timer)
        self._stop_timer(self._fx_banner_timer)
        self._fx_header_timer = None
        self._fx_banner_timer = None
        self._startup.cancel()
        self._stop_timer(self._fx_flash_timer)
        self._fx_flash_timer = None
        if not self._panel_manual:
            self._panel_on = bool(self._config["panel"])
        if not self._dock_manual:
            self._dock_on = bool(self._config["left_dock"])
        self._save_config()
        stop_background_music()
        if self._sounds:
            play_sound("exit")

    def on_resize(self, event: events.Resize) -> None:
        width = event.size.width if event.size is not None else self._term_width()
        self._auto_dock(width)
        self._apply_stage_width(width)
        self._auto_panel(self._stage_width())

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

    def _scene_max(self) -> int:
        return SCENE_WIDTH_DOCKED if self._dock_on else STAGE_MAX_WIDTH

    def _stage_width(self) -> int:
        return min(self._scene_max(), self._term_width())

    def _apply_stage_width(self, width: int | None = None) -> None:
        if width is None:
            width = self._term_width()
        self.query_one("#main").styles.width = min(self._scene_max(), max(40, width))

    def _log_width_for(self, width: int | None = None, dock: bool | None = None, panel: bool | None = None) -> int:
        term = width if width is not None else self._term_width()
        dock = self._dock_on if dock is None else dock
        panel = self._panel_on if panel is None else panel
        scene = min(SCENE_WIDTH_DOCKED if dock else STAGE_MAX_WIDTH, term)
        if dock:
            scene -= DOCK_WIDTH
        if panel:
            scene -= 40
        return scene

    def _dock_fits(self) -> bool:
        return self._log_width_for(dock=True) >= DOCK_MIN_LOG_WIDTH

    def _dock_auto_for(self, width: int) -> bool:
        return width >= SCENE_WIDTH_DOCKED and self._log_width_for(width, dock=True) >= DOCK_MIN_LOG_WIDTH

    def _apply_dock_visibility(self) -> None:
        col = self.query_one("#dock-col")
        if self._dock_on:
            col.add_class("visible")
        else:
            col.remove_class("visible")

    def _auto_dock(self, width: int | None = None) -> None:
        if self._dock_manual:
            return
        if width is None:
            width = self._term_width()
        show = self._dock_auto_for(width)
        if show != self._dock_on:
            self._dock_on = show
            self._apply_dock_visibility()

    def _set_dock(self, on: bool) -> bool:
        if on and not self._dock_on and not self._dock_fits():
            self._set_status("Недостаточно ширины терминала.")
            return False
        self._dock_on = on
        self._dock_manual = True
        self._apply_dock_visibility()
        self._apply_stage_width()
        self._auto_panel(self._stage_width())
        self._save_config()
        self._set_status("Левая панель открыта." if on else "Левая панель скрыта.")
        return True

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
        chat = self._chat()
        width = chat.content_size.width
        if width <= 0:
            width = self._term_width()
        pairs = self._banner_lines(width)
        at_end = chat.is_vertical_scroll_end
        self._banner_pairs = pairs
        self._banner_idx = 0
        self._banner_event = asyncio.Event()
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
                if self._banner_event is not None:
                    self._banner_event.set()
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
        self._header_event = asyncio.Event()
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
                if self._header_event is not None:
                    self._header_event.set()
                self._print_banner()
        except Exception:
            self._stop_timer(self._fx_header_timer)
            self._fx_header_timer = None

    async def _wait_header(self) -> None:
        if self._header_event is not None:
            await self._header_event.wait()

    async def _wait_banner(self) -> None:
        if self._banner_event is not None:
            await self._banner_event.wait()

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
        show = width - (DOCK_WIDTH if self._dock_on else 0) >= PANEL_AUTO_WIDTH
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
                    id="sources-empty",
                    classes="reveal-hidden" if not self._startup_done else "",
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
            score = float(chunk.get("score") or 0.0)
            percent = (
                int(round(min(max(score, 0.0), 1.0) * 100))
                if score <= 1.0
                else int(round(min(max(score, 0.0), 100.0)))
            )
            meta = f"{chapter} · релевантность {percent}%"
            row = Text.assemble(
                (f"{i}. ", STYLE_BRASS),
                (title, STYLE_BONE),
                ("\n   ", STYLE_DIM),
                (meta, STYLE_DIM),
            )
            box.mount(Static(row, markup=False))
            box.mount(Static("", markup=False))

    def _init_dock_data(self) -> None:
        self._tick_clock()
        self._tick_dock_data()
        self._tick_ping()

    def _apply_dock_value(self, widget_id: str, value: str, style: Style | None = None) -> None:
        widget = self.query_one(f"#{widget_id}", Static)
        if style is None:
            widget.update(value)
        else:
            widget.update(Text(value, style=style))

    def _tick_clock(self) -> None:
        now = datetime.now()
        clock = now.strftime("%H:%M:%S")
        if clock != self._last_clock:
            self._last_clock = clock
            self._apply_dock_value("clock", clock)
        dow = WEEKDAY_NAMES[now.weekday()]
        if dow != self._last_dow:
            self._last_dow = dow
            self._apply_dock_value("dock-dow", dow)
        date = f"{now.day} {MONTH_NAMES[now.month - 1]} {now.year}"
        if date != self._last_date:
            self._last_date = date
            self._apply_dock_value("dock-date", date)

    def _tick_dock_data(self) -> None:
        power = _battery_text()
        if power != self._last_power:
            self._last_power = power
            self._apply_dock_value("d-power", power)

    def _tick_ping(self) -> None:
        self._ping_worker()

    @work(thread=True, group="sysinfo-ping", exclusive=True)
    def _ping_worker(self) -> None:
        text, style = _measure_ping(PING_HOST)
        self.call_from_thread(self._apply_ping, text, style)

    def _apply_ping(self, text: str, style: Style) -> None:
        if text != self._last_ping:
            self._last_ping = text
            self._apply_dock_value("d-ping", text, style)


    def action_toggle_sources(self) -> None:
        self._set_panel(not self._panel_on)
        if self._panel_on and not self._last_chunks:
            self._set_status("Источников пока нет — сначала задайте вопрос.")
        else:
            self._set_status(
                "Панель источников " + ("открыта." if self._panel_on else "скрыта.")
            )

    def action_toggle_dock(self) -> None:
        self._set_dock(not self._dock_on)

    def set_theme(self, name: str) -> None:
        if name not in THEME_NAMES:
            return
        self.theme = name
        self._save_config()
        self._set_status(f"Тема: {THEME_DISPLAY_NAMES[name]}.")

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

    def action_passwd(self) -> None:
        self.push_screen(PasswordModal())

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
        if question == "/passwd":
            self.action_passwd()
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
            if self._sounds:
                play_sound("identity")
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
        if self._startup.running:
            self._startup.skip()
            event.stop()
            event.prevent_default()
            return
        if self._typing_active:
            self._skip_typing = True
            event.stop()

    @on(events.MouseDown)
    def _on_reveal_mouse(self, event: events.MouseDown) -> None:
        if self._startup.running:
            self._startup.skip()
            event.stop()
            event.prevent_default()

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
            if self._startup_reveal:
                self._startup.start()
            else:
                self._set_status("Готов к приёму запроса.")
                self._start_header_reveal()
                self.query_one("#question", QuestionInput).focus()
        else:
            self._append_msg(
                Text(f"Ошибка прогрева моделей: {result.get('error')}", style=STYLE_BLOOD)
            )
            if self._startup_reveal and not self._startup_done:
                self._finish_reveal("Ошибка прогрева. См. журнал.")
            else:
                self._set_status("Ошибка прогрева. См. журнал.")
                self._start_header_reveal()
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

    def _is_shown(self, widget) -> bool:
        node = widget
        while node is not None:
            if not getattr(node, "display", True):
                return False
            node = node.parent
        return True

    def _reveal_widgets(self) -> list:
        widgets: list = []
        for widget_id in (
            "#dock-col", "#clock", "#dock-dow", "#dock-date", "#dock-filler", "#dock-data",
            "#log-frame", "#sources-col", "#gutter-left", "#gutter-right",
            "#status-row", "#status", "#input-row", "#prompt", "#question", "#startup-line",
        ):
            widgets.extend(self.query(widget_id))
        widgets.extend(self.query(".dock-row"))
        widgets.extend(self.query("#sources-empty"))
        return widgets

    async def _stage1_boot(self) -> None:
        line = self.query_one("#startup-line")
        line.styles.display = "block"
        await RevealEffects.fade(line, STARTUP_ROW_FADE)
        await asyncio.sleep(STARTUP_BOOT_PAUSE)
        line.styles.clear_rule("display")
        RevealEffects.show(line)

    async def _stage2_identify(self) -> None:
        self._start_header_reveal()
        await self._wait_header()

    async def _stage3_status(self) -> None:
        await RevealEffects.fade(self.query_one("#status-row"), STARTUP_FRAME_FADE)
        await RevealEffects.fade(self.query_one("#status"), STARTUP_ROW_FADE)
        await asyncio.sleep(STARTUP_STATUS_PAUSE)
        self._set_status("Проверка подсистем...")

    async def _stage4_main(self) -> None:
        await RevealEffects.fade(self.query_one("#log-frame"), STARTUP_FRAME_FADE)
        await self._wait_banner()

    async def _reveal_dock_panel(self) -> None:
        dock = self.query_one("#dock-col")
        if not self._is_shown(dock):
            return
        await RevealEffects.fade(dock, STARTUP_FRAME_FADE)
        for widget_id in ("#clock", "#dock-dow", "#dock-date"):
            await RevealEffects.rise(self.query_one(widget_id), STARTUP_ROW_FADE)
        await RevealEffects.fade(self.query_one("#dock-data"), STARTUP_FRAME_FADE)
        for widget in self.query(".dock-row"):
            if self._is_shown(widget):
                await RevealEffects.rise(widget, STARTUP_ROW_FADE)

    async def _reveal_sources_panel(self) -> None:
        col = self.query_one("#sources-col")
        if not self._is_shown(col):
            return
        await RevealEffects.fade(col, STARTUP_FRAME_FADE)
        for widget in self.query("#sources-empty"):
            if self._is_shown(widget):
                await RevealEffects.rise(widget, STARTUP_ROW_FADE)

    async def _stage5_panels(self) -> None:
        await asyncio.gather(self._reveal_dock_panel(), self._reveal_sources_panel())

    async def _stage6_data(self) -> None:
        await asyncio.gather(
            *[
                RevealEffects.fade(self.query_one(widget_id), STARTUP_GUTTER_FADE)
                for widget_id in ("#gutter-left", "#gutter-right", "#dock-filler")
                if self._is_shown(self.query_one(widget_id))
            ]
        )

    async def _stage7_navigation(self) -> None:
        await RevealEffects.fade(self.query_one("#input-row"), STARTUP_FRAME_FADE)
        await RevealEffects.rise(self.query_one("#prompt"), STARTUP_ROW_FADE)
        await RevealEffects.rise(self.query_one("#question"), STARTUP_ROW_FADE)

    async def _stage8_ready(self) -> None:
        self._startup_done = True
        question = self.query_one("#question", QuestionInput)
        question.disabled = self._busy
        question.focus()

    def _finish_header_now(self) -> None:
        if self._fx_header_timer is not None:
            self._stop_timer(self._fx_header_timer)
            self._fx_header_timer = None
        if not self._header_target:
            self._header_target = "ARCHIVE TERMINAL · ADEPTUS MECHANICUS"
        self.query_one("#header-line").update(self._header_target)
        self.query_one("#session-tag").update(f"┌ ПАРАМЕТРЫ СЕАНСА · {self._greeting} ┐")
        if self._header_event is not None:
            self._header_event.set()

    def _finish_banner_now(self) -> None:
        if not self._banner_pairs:
            self._print_banner()
        if self._fx_banner_timer is not None:
            self._stop_timer(self._fx_banner_timer)
            self._fx_banner_timer = None
        widgets = self._chat().query(".banner")
        for index, widget in enumerate(widgets):
            if index < len(self._banner_pairs):
                text, style = self._banner_pairs[index]
                widget.update(Text(text, style=style))
        self._banner_idx = len(self._banner_pairs)
        if self._banner_event is not None:
            self._banner_event.set()

    def _finish_reveal(self, final_status: str | None = "Готов к приёму запроса.") -> None:
        self._startup_done = True
        self._finish_header_now()
        self._finish_banner_now()
        self.query_one("#startup-line").styles.clear_rule("display")
        for widget in self._reveal_widgets():
            RevealEffects.show(widget)
        if final_status is not None:
            self._set_status(final_status)
        question = self.query_one("#question", QuestionInput)
        question.disabled = self._busy
        question.focus()


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
