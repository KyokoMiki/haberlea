"""Global state management for Haberlea WebUI using NiceGUI app.storage and msgspec.

All public functions operate on typed structs. Serialization to/from
NiceGUI's dict-based storage happens only at the boundary layer
(get_app_storage / _save_app_storage).
"""

from typing import TYPE_CHECKING, Any

import msgspec
from nicegui import app

if TYPE_CHECKING:
    from haberlea.core.haberlea import Haberlea

    from .pages.download import DownloadPage
    from .pages.logs import LogsPage
    from .pages.search import SearchPage
    from .pages.settings import SettingsPage


# ---------------------------------------------------------------------------
# Haberlea singleton
# ---------------------------------------------------------------------------

_haberlea: "Haberlea | None" = None


def init_haberlea(instance: "Haberlea") -> None:
    """Stores the global Haberlea instance (called once at startup).

    Args:
        instance: The fully initialized Haberlea instance.
    """
    global _haberlea
    _haberlea = instance


def get_haberlea() -> "Haberlea":
    """Returns the global Haberlea instance.

    Returns:
        The Haberlea singleton.

    Raises:
        RuntimeError: If Haberlea has not been initialized.
    """
    if _haberlea is None:
        raise RuntimeError(
            "Haberlea not initialized. "
            "Call init_haberlea() before accessing the instance."
        )
    return _haberlea


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class DownloadTask(msgspec.Struct, kw_only=True, frozen=True):
    """Represents a download task in the queue.

    Attributes:
        url: The download URL.
        status: Task status (pending, downloading, completed, failed).
        progress: Download progress from 0.0 to 1.0.
        message: Status message or error description.
        media_type: Type of media (track, album, playlist, artist, video).
        media_id: Media identifier.
        service: Service name (qobuz, tidal, etc.).
        data: Pre-fetched data for the download task.
    """

    url: str
    status: str = "pending"
    progress: float = 0.0
    message: str = ""
    media_type: str = ""
    media_id: str = ""
    service: str = ""
    data: dict[str, Any] | None = None


class UserPreferences(msgspec.Struct, kw_only=True, frozen=True):
    """User preferences stored per browser session.

    Attributes:
        dark_mode: Whether dark mode is enabled.
        sidebar_open: Whether sidebar is expanded.
    """

    dark_mode: bool = False
    sidebar_open: bool = True


class AppStorage(msgspec.Struct, kw_only=True):
    """Application-wide storage container.

    Attributes:
        download_queue: List of download tasks.
        logs: Application log messages.
        is_downloading: Whether a download is in progress.
    """

    download_queue: list[DownloadTask] = msgspec.field(default_factory=list)
    logs: list[str] = msgspec.field(default_factory=list)
    is_downloading: bool = False


# ---------------------------------------------------------------------------
# NiceGUI storage boundary — serialize/deserialize here only
# ---------------------------------------------------------------------------

_encoder = msgspec.json.Encoder()
_app_storage_decoder = msgspec.json.Decoder(AppStorage)
_prefs_decoder = msgspec.json.Decoder(UserPreferences)


def _raw_storage() -> dict[str, Any]:
    """Returns the raw NiceGUI general storage dict, initializing if needed."""
    if "haberlea" not in app.storage.general:
        app.storage.general["haberlea"] = msgspec.structs.asdict(AppStorage())
    return app.storage.general["haberlea"]


def get_app_storage() -> AppStorage:
    """Loads application storage as a typed struct.

    Returns:
        AppStorage instance deserialized from NiceGUI storage.
    """
    raw = _raw_storage()
    return msgspec.convert(raw, AppStorage)


def _save_app_storage(storage: AppStorage) -> None:
    """Persists an AppStorage struct back to NiceGUI storage.

    Args:
        storage: The AppStorage to persist.
    """
    app.storage.general["haberlea"] = msgspec.structs.asdict(storage)


def get_user_preferences() -> UserPreferences:
    """Loads user preferences as a typed struct.

    Returns:
        UserPreferences instance.
    """
    if "preferences" not in app.storage.user:
        app.storage.user["preferences"] = msgspec.structs.asdict(UserPreferences())
    return msgspec.convert(app.storage.user["preferences"], UserPreferences)


# ---------------------------------------------------------------------------
# Public API — all struct-based
# ---------------------------------------------------------------------------


def add_download_task(
    url: str,
    service: str = "",
    media_type: str = "",
    media_id: str = "",
    data: dict[str, Any] | None = None,
) -> DownloadTask:
    """Adds a new download task to the queue.

    Args:
        url: The download URL.
        service: The service name.
        media_type: The media type (track, album, etc.).
        media_id: The media ID.
        data: Pre-fetched data for the download task.

    Returns:
        The created DownloadTask instance.
    """
    task = DownloadTask(
        url=url,
        service=service,
        media_type=media_type,
        media_id=media_id,
        data=data,
    )
    storage = get_app_storage()
    updated = AppStorage(
        download_queue=[*storage.download_queue, task],
        logs=storage.logs,
        is_downloading=storage.is_downloading,
    )
    _save_app_storage(updated)
    return task


def get_task(index: int) -> DownloadTask | None:
    """Gets a download task by index.

    Args:
        index: The task index in the queue.

    Returns:
        The DownloadTask instance or None if not found.
    """
    storage = get_app_storage()
    if 0 <= index < len(storage.download_queue):
        return storage.download_queue[index]
    return None


def update_task_status(
    index: int,
    status: str | None = None,
    progress: float | None = None,
    message: str | None = None,
) -> None:
    """Updates a download task's status by creating a new task with changed fields.

    Args:
        index: The task index in the queue.
        status: New status value.
        progress: New progress value (0.0-1.0).
        message: New message value.
    """
    storage = get_app_storage()
    if not (0 <= index < len(storage.download_queue)):
        return

    old = storage.download_queue[index]
    updated_task = DownloadTask(
        url=old.url,
        status=status if status is not None else old.status,
        progress=progress if progress is not None else old.progress,
        message=message if message is not None else old.message,
        media_type=old.media_type,
        media_id=old.media_id,
        service=old.service,
        data=old.data,
    )
    new_queue = [
        updated_task if i == index else t for i, t in enumerate(storage.download_queue)
    ]
    _save_app_storage(
        AppStorage(
            download_queue=new_queue,
            logs=storage.logs,
            is_downloading=storage.is_downloading,
        )
    )


def remove_task(index: int) -> None:
    """Removes a task from the download queue.

    Args:
        index: The task index to remove.
    """
    storage = get_app_storage()
    if not (0 <= index < len(storage.download_queue)):
        return

    new_queue = [t for i, t in enumerate(storage.download_queue) if i != index]
    _save_app_storage(
        AppStorage(
            download_queue=new_queue,
            logs=storage.logs,
            is_downloading=storage.is_downloading,
        )
    )


def add_log(message: str) -> None:
    """Adds a log message.

    Args:
        message: The log message to add.
    """
    storage = get_app_storage()
    logs = [*storage.logs, message]
    # Keep only last 500 logs
    if len(logs) > 500:
        logs = logs[-500:]
    _save_app_storage(
        AppStorage(
            download_queue=storage.download_queue,
            logs=logs,
            is_downloading=storage.is_downloading,
        )
    )


def clear_logs() -> None:
    """Clears all log messages."""
    storage = get_app_storage()
    _save_app_storage(
        AppStorage(
            download_queue=storage.download_queue,
            logs=[],
            is_downloading=storage.is_downloading,
        )
    )


# ---------------------------------------------------------------------------
# Page registry
# ---------------------------------------------------------------------------


class PageInstances(msgspec.Struct):
    """Container for page instances with type hints.

    Core pages are typed explicitly, extension pages are stored in a dict.

    Attributes:
        download: The download page instance.
        search: The search page instance.
        settings: The settings page instance.
        logs: The logs page instance.
        extensions: Dictionary of extension page instances by page_id.
    """

    download: "DownloadPage | None" = None
    search: "SearchPage | None" = None
    settings: "SettingsPage | None" = None
    logs: "LogsPage | None" = None
    extensions: dict[str, Any] = msgspec.field(default_factory=dict)


# Page instances registry for cross-page communication
_pages = PageInstances()


def register_page(name: str, instance: Any) -> None:
    """Registers a page instance for cross-page access.

    Args:
        name: The page name identifier (download, search, settings, logs,
              or extension page_id).
        instance: The page instance.
    """
    if hasattr(_pages, name):
        object.__setattr__(_pages, name, instance)
    else:
        _pages.extensions[name] = instance


def get_pages() -> PageInstances:
    """Gets the page instances container.

    Returns:
        The PageInstances struct with all registered pages.
    """
    return _pages
