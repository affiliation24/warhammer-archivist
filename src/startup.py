import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from textual.geometry import Offset
from textual.widget import Widget


class StageState(Enum):
    BOOT = "BOOT"
    INITIALIZING = "INITIALIZING"
    LOADING = "LOADING"
    READY = "READY"


@dataclass
class Stage:
    key: str
    state: StageState
    status: str = ""
    prepare: Callable[[], Awaitable[None]] | None = None
    reveal: Callable[[], Awaitable[None]] | None = None


class RevealEffects:
    @staticmethod
    def _visible(widget: Widget) -> bool:
        node: Any = widget
        while node is not None:
            if not getattr(node, "display", True):
                return False
            node = node.parent
        return True

    @staticmethod
    def hide(widget: Widget) -> None:
        widget.add_class("reveal-hidden")

    @staticmethod
    def show(widget: Widget) -> None:
        widget.remove_class("reveal-hidden")
        widget.styles.clear_rule("opacity")
        widget.styles.clear_rule("visibility")
        widget.styles.clear_rule("offset")

    @staticmethod
    async def fade(widget: Widget, duration: float) -> None:
        if not RevealEffects._visible(widget):
            return
        widget.remove_class("reveal-hidden")
        widget.styles.opacity = 0
        widget.styles.animate("opacity", value=1.0, duration=duration)
        await asyncio.sleep(duration)
        RevealEffects.show(widget)

    @staticmethod
    async def rise(widget: Widget, duration: float) -> None:
        if not RevealEffects._visible(widget):
            return
        widget.remove_class("reveal-hidden")
        widget.styles.offset = Offset(0, 1)
        widget.styles.opacity = 0
        widget.styles.animate("opacity", value=1.0, duration=duration)
        await asyncio.sleep(duration / 2)
        widget.styles.offset = Offset(0, 0)
        await asyncio.sleep(duration / 2)
        RevealEffects.show(widget)


class StartupManager:
    def __init__(self, app: Any) -> None:
        self._app = app
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def _build_stages(self) -> list[Stage]:
        app = self._app
        return [
            Stage("boot", StageState.BOOT, "", None, app._stage1_boot),
            Stage("identify", StageState.INITIALIZING, "Идентификация терминала...", None, app._stage2_identify),
            Stage("status", StageState.INITIALIZING, "", None, app._stage3_status),
            Stage("main", StageState.LOADING, "Развёртывание рабочей области...", None, app._stage4_main),
            Stage("panels", StageState.LOADING, "Синхронизация панелей...", None, app._stage5_panels),
            Stage("data", StageState.LOADING, "", None, app._stage6_data),
            Stage("nav", StageState.LOADING, "Калибровка навигации...", None, app._stage7_navigation),
            Stage("ready", StageState.READY, "Готов к приёму запроса.", None, app._stage8_ready),
        ]

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        current = asyncio.current_task()
        self._running = True
        try:
            for stage in self._build_stages():
                if stage.status:
                    self._app._set_status(stage.status)
                if stage.prepare is not None:
                    await stage.prepare()
                if stage.reveal is not None:
                    await stage.reveal()
        finally:
            if self._task is current:
                self._running = False

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._running = False

    def skip(self) -> None:
        self.cancel()
        self._app._finish_reveal()
