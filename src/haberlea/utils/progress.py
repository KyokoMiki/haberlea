"""Unified progress reporting system.

Simple callback-based progress system that works for both CLI and WebUI.
All progress updates (track status, file download, etc.) flow through
a single callback interface.
"""

from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from enum import Enum
from inspect import iscoroutine
from typing import Any

import humanfriendly
import msgspec
from rich import get_console
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Column
from rich.text import Text


class ProgressStatus(Enum):
    """Progress status."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProgressMode(Enum):
    """Progress display mode."""

    PERCENT = "percent"
    BYTES = "bytes"


class ProgressEvent(msgspec.Struct, kw_only=True):
    """Progress event data.

    Attributes:
        task_id: Unique identifier for the task.
        status: Current status.
        name: Display name (e.g., track name).
        artist: Artist name.
        album: Album name.
        service: Service/module name.
        current: Current progress value.
        total: Total value.
        message: Status message.
        mode: Display mode (percent or bytes).
    """

    task_id: str
    status: ProgressStatus = ProgressStatus.PENDING
    name: str = ""
    artist: str = ""
    album: str = ""
    service: str = ""
    current: int = 0
    total: int = 0
    message: str = ""
    mode: ProgressMode = ProgressMode.PERCENT

    @property
    def progress(self) -> float:
        """Progress ratio (0.0 to 1.0)."""
        return self.current / self.total if self.total > 0 else 0.0


# Callback type: receives ProgressEvent
ProgressCallback = Callable[[ProgressEvent], Coroutine[Any, Any, None] | None]

# Global state
_callback: ProgressCallback | None = None
_tasks: dict[str, ProgressEvent] = {}
_current_task: ContextVar[str | None] = ContextVar("current_task", default=None)
_current_mode: ContextVar[ProgressMode] = ContextVar(
    "current_mode", default=ProgressMode.PERCENT
)

# Throttle settings
_last_reported: dict[str, int] = {}
_THROTTLE_BYTES = 102400
_THROTTLE_PERCENT = 1  # Minimum change in units before reporting


def set_callback(callback: ProgressCallback | None) -> None:
    """Set the global progress callback.

    Args:
        callback: Progress callback function, or None to disable.
    """
    global _callback
    _callback = callback


def get_callback() -> ProgressCallback | None:
    """Get the current progress callback.

    Returns:
        The current callback or None.
    """
    return _callback


def set_current_task(task_id: str | None) -> None:
    """Set current task in context.

    Args:
        task_id: Task identifier or None.
    """
    _current_task.set(task_id)


def get_current_task() -> str | None:
    """Get current task from context.

    Returns:
        Current task ID or None.
    """
    return _current_task.get()


def set_current_mode(mode: ProgressMode) -> None:
    """Set current progress mode in context.

    Args:
        mode: Progress display mode.
    """
    _current_mode.set(mode)


def get_current_mode() -> ProgressMode:
    """Get current progress mode from context.

    Returns:
        Current progress mode.
    """
    return _current_mode.get()


async def create_task(
    task_id: str,
    name: str = "",
    artist: str = "",
    album: str = "",
    service: str = "",
) -> None:
    """Create a new progress task.

    Args:
        task_id: Unique identifier.
        name: Display name.
        artist: Artist name.
        album: Album name.
        service: Service/module name.
    """
    event = ProgressEvent(
        task_id=task_id,
        status=ProgressStatus.PENDING,
        name=name,
        artist=artist,
        album=album,
        service=service,
    )
    _tasks[task_id] = event
    _last_reported[task_id] = 0
    await _dispatch(event)


def _collect_non_none(**kwargs: Any) -> dict[str, Any]:
    """Collect keyword arguments that are not None.

    Args:
        **kwargs: Keyword arguments to filter.

    Returns:
        Dictionary of non-None arguments.
    """
    return {k: v for k, v in kwargs.items() if v is not None}


def _should_dispatch_event(
    event: ProgressEvent,
    task_id: str,
    force: bool,
    status: ProgressStatus | None,
    name: str | None,
    mode: ProgressMode | None,
) -> bool:
    """Check if event should be dispatched.

    Args:
        event: Progress event.
        task_id: Task identifier.
        force: Force dispatch flag.
        status: New status.
        name: Display name.
        mode: Progress display mode.

    Returns:
        True if event should be dispatched.
    """
    last = _last_reported.get(task_id, 0)
    new_current = event.current
    throttle = (
        _THROTTLE_BYTES if event.mode == ProgressMode.BYTES else _THROTTLE_PERCENT
    )
    return (
        force
        or status is not None
        or name is not None
        or mode is not None
        or new_current - last >= throttle
        or (event.total > 0 and new_current >= event.total)
    )


async def update(
    task_id: str,
    status: ProgressStatus | None = None,
    name: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    current: int | None = None,
    total: int | None = None,
    message: str | None = None,
    mode: ProgressMode | None = None,
    force: bool = False,
) -> None:
    """Update task progress.

    Args:
        task_id: Task identifier.
        status: New status.
        name: Display name.
        artist: Artist name.
        album: Album name.
        current: Current progress value.
        total: Total value.
        message: Status message.
        mode: Progress display mode.
        force: Force dispatch even if throttle not reached.
    """
    event = _tasks.get(task_id)
    if event is None:
        event = ProgressEvent(task_id=task_id)
        _tasks[task_id] = event
        _last_reported[task_id] = 0

    # Build updates and apply via struct replace
    updates = _collect_non_none(
        status=status,
        name=name,
        artist=artist,
        album=album,
        current=current,
        total=total,
        message=message,
        mode=mode,
    )

    if updates:
        event = msgspec.structs.replace(event, **updates)
        _tasks[task_id] = event

    # Check throttle for progress updates
    if _should_dispatch_event(event, task_id, force, status, name, mode):
        _last_reported[task_id] = event.current
        await _dispatch(event)


async def advance(task_id: str, amount: int, total: int = 0) -> None:
    """Advance progress by amount.

    Args:
        task_id: Task identifier.
        amount: Amount to advance.
        total: Total value (optional, updates if provided).
    """
    event = _tasks.get(task_id)
    current = (event.current if event else 0) + amount
    await update(task_id, current=current, total=total if total > 0 else None)


def reset(task_id: str) -> None:
    """Reset progress tracking for a task.

    Args:
        task_id: Task identifier.
    """
    _last_reported[task_id] = 0
    if task_id in _tasks:
        _tasks[task_id] = msgspec.structs.replace(_tasks[task_id], current=0)


def remove_task(task_id: str) -> None:
    """Remove a task from tracking.

    Args:
        task_id: Task identifier.
    """
    _tasks.pop(task_id, None)
    _last_reported.pop(task_id, None)


def clear_all() -> None:
    """Clear all tasks."""
    _tasks.clear()
    _last_reported.clear()


async def _dispatch(event: ProgressEvent) -> None:
    """Dispatch event to callback.

    Args:
        event: The event to dispatch.
    """
    if _callback is not None:
        result = _callback(event)
        if iscoroutine(result):
            await result


class BinaryTransferSpeedColumn(ProgressColumn):
    """Renders human readable transfer speed using binary units (MiB/s)."""

    def render(self, task: Task) -> Text:
        """Show data transfer speed in binary units.

        Args:
            task: The Rich task to render.

        Returns:
            Formatted transfer speed text.
        """
        speed = task.finished_speed or task.speed
        if speed is None:
            return Text("?", style="progress.data.speed")
        # Use humanfriendly for binary units (KiB, MiB, GiB)
        data_speed = humanfriendly.format_size(int(speed), binary=True)
        return Text(f"{data_speed}/s", style="progress.data.speed")


class PercentColumn(ProgressColumn):
    """Custom column showing percentage or download size based on task mode."""

    def render(self, task: Task) -> Text:
        """Render progress based on task's mode field.

        Args:
            task: The Rich task to render.

        Returns:
            Formatted progress text.
        """
        if task.total is None or task.total == 0:
            return Text("0%", style="progress.percentage")

        # Check if this task uses bytes mode (stored in task.fields)
        use_bytes = task.fields.get("use_bytes", False)

        if use_bytes:
            # Format as file size using humanfriendly
            completed = humanfriendly.format_size(task.completed, binary=True)
            total = humanfriendly.format_size(task.total, binary=True)
            return Text(f"{completed}/{total}", style="progress.download")
        else:
            # Format as percentage
            percent = task.completed / task.total * 100
            return Text(f"{percent:.0f}%", style="progress.percentage")


class RichProgressCallback:
    """Rich-based CLI progress renderer.

    Usage:
        with RichProgressCallback() as callback:
            set_callback(callback)
            # do work...
    """

    def __init__(self) -> None:
        """Initialize Rich progress callback."""
        self._progress: Progress | None = None
        self._tasks: dict[str, TaskID] = {}

    def __enter__(self) -> "RichProgressCallback":
        """Start Rich progress display.

        Returns:
            Self for use as callback.
        """
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn(
                "[bold blue]{task.description}",
                table_column=Column(ratio=1, no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(bar_width=40),
            PercentColumn(),
            TimeRemainingColumn(),
            BinaryTransferSpeedColumn(),
            console=get_console(),
            expand=True,
            transient=True,
        )
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Stop Rich progress display."""
        if self._progress:
            self._progress.stop()
        self._tasks.clear()

    def __call__(self, event: ProgressEvent) -> None:
        """Handle progress event.

        Args:
            event: Progress event.
        """
        if self._progress is None:
            return

        task_id = event.task_id
        desc = event.name or event.message or task_id[:20]
        use_bytes = event.mode == ProgressMode.BYTES

        # Create task if needed
        if task_id not in self._tasks:
            self._tasks[task_id] = self._progress.add_task(
                desc, total=event.total or 100, use_bytes=use_bytes
            )

        rich_task = self._tasks[task_id]

        # Handle completion
        if event.status in (
            ProgressStatus.COMPLETED,
            ProgressStatus.FAILED,
            ProgressStatus.SKIPPED,
        ):
            self._progress.update(rich_task, completed=event.total or 100)
            self._progress.remove_task(rich_task)
            del self._tasks[task_id]
        else:
            self._progress.update(
                rich_task,
                completed=event.current,
                total=event.total or 100,
                description=desc[:30],
                use_bytes=use_bytes,
            )
