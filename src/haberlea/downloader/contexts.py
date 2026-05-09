"""Parameter objects replacing long parameter lists in the download pipeline.

Each struct bundles related parameters that travel together through
multiple function calls, eliminating 5-10 parameter signatures.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from haberlea.utils.models import ModuleFlags

if TYPE_CHECKING:
    from collections.abc import Callable

    from haberlea.core import Haberlea
    from haberlea.download_queue import DownloadQueue, TrackTask
    from haberlea.downloader.facade import Downloader
    from haberlea.plugins.base import ModuleBase
    from haberlea.utils.models import (
        DownloadTypeEnum,
        MediaIdentification,
        ModuleInformation,
        ModuleModes,
        TrackInfo,
        VideoContainerEnum,
        VideoInfo,
    )
    from haberlea.utils.progress import ProgressStatus
    from haberlea.utils.settings import CoversSettings


class ModuleRef(msgspec.Struct, frozen=True):
    """A reference to a loaded module.

    Replaces the recurring (module_name, module) parameter pair.
    """

    name: str
    instance: ModuleBase


class TrackContext(msgspec.Struct):
    """TrackTask enriched with runtime data.

    Created once after metadata fetch, passed through the entire pipeline.
    For audio tasks ``track_info`` is populated; for video tasks
    ``video_info`` + ``video_container`` are populated. Exactly one of
    the info fields is non-None.
    """

    task: TrackTask
    location_name: Path  # Path without extension
    track_info: TrackInfo | None = None
    video_info: VideoInfo | None = None
    video_container: VideoContainerEnum | None = None

    @property
    def location(self) -> Path:
        """Full path with the appropriate extension."""
        if self.video_info is not None and self.video_container is not None:
            ext = self.video_container.value
        elif self.track_info is not None:
            ext = self.track_info.codec.container.name
        else:
            raise RuntimeError(
                "TrackContext has neither track_info nor video_info populated"
            )
        return self.location_name.parent / (self.location_name.name + f".{ext}")


class AlbumQueueRequest(msgspec.Struct, frozen=True):
    """Album queue request — replaces queue_album's 8 parameters."""

    module: ModuleRef
    album_id: str
    original_url: str = ""
    artist_name: str = ""
    base_path: Path | None = None
    album_data: dict[str, Any] | None = None
    parent_job_id: str | None = None


class PlaylistQueueRequest(msgspec.Struct, frozen=True):
    """Playlist queue request — replaces queue_playlist's 6 parameters."""

    module: ModuleRef
    playlist_id: str
    original_url: str = ""
    custom_module: ModuleRef | None = None


class TrackQueueRequest(msgspec.Struct, frozen=True):
    """Single track queue request."""

    module: ModuleRef
    track_id: str
    original_url: str = ""
    track_data: dict[str, Any] | None = None


class ArtistQueueRequest(msgspec.Struct, frozen=True):
    """Artist queue request."""

    module: ModuleRef
    artist_id: str
    original_url: str = ""


class VideoQueueRequest(msgspec.Struct, frozen=True):
    """Single music video queue request.

    The queue builder converts this into a regular ``TrackTask`` with
    ``media_kind=MediaKindEnum.video`` so the rest of the pipeline can
    stay media-kind agnostic.
    """

    module: ModuleRef
    video_id: str
    original_url: str = ""
    video_data: dict[str, Any] | None = None


class DownloadRequest(msgspec.Struct, frozen=True):
    """Download orchestration request."""

    session: Haberlea
    media_to_download: dict[str, list[MediaIdentification]]
    third_party_modules: dict[ModuleModes, str]
    separate_download_module: str
    output_path: Path
    on_queue_ready: Callable[[DownloadQueue], None] | None = None


class QueueingContext(msgspec.Struct, frozen=True):
    """Immutable context for the queueing loop.

    Bundles parameters that are constant across all items in a download session,
    eliminating pass-through parameters in _queue_module_items/_queue_media_item.
    """

    session: Haberlea
    downloader: Downloader
    separate_download_module: str


class LoginContext(msgspec.Struct, frozen=True):
    """Login context — replaces handle_module_auth/perform_login parameters."""

    module_name: str
    loaded_module: ModuleBase
    module_info: ModuleInformation
    account_settings: dict[str, Any]
    account_index: int = 0


class JobDefinition(msgspec.Struct, frozen=True):
    """Immutable job definition for DownloadQueue.create_job."""

    original_url: str
    media_type: DownloadTypeEnum
    media_id: str
    module_name: str
    name: str = ""
    artist: str = ""
    download_path: Path = msgspec.field(default_factory=Path)
    cover_url: str = ""


class ProgressUpdate(msgspec.Struct, frozen=True, kw_only=True):
    """Progress update payload — replaces update_progress's 9 parameters."""

    track_id: str
    status: ProgressStatus | None = None
    name: str | None = None
    artist: str | None = None
    album: str | None = None
    progress: float | None = None
    message: str | None = None
    file_size: int | None = None
    downloaded_size: int | None = None


class ArtworkSettings(msgspec.Struct, frozen=True):
    """Artwork download settings.

    Moved here from music_downloader to avoid circular imports.
    """

    should_resize: bool
    resolution: int
    compression: str
    format: str


def build_artwork_settings(
    module_flags: Any | None,
    cover_config: CoversSettings,
    is_external: bool = False,
) -> ArtworkSettings:
    """Build ArtworkSettings from module flags and cover config.

    Args:
        module_flags: Module flags (ModuleFlags | None).
        cover_config: CoversSettings instance.
        is_external: Whether to use external cover settings.

    Returns:
        ArtworkSettings instance.
    """
    should_resize = bool(
        module_flags is not None and (module_flags & ModuleFlags.needs_cover_resize)
    )
    # Original mode (no processing) when compression is disabled for this path.
    skip_processing = (is_external and not cover_config.compress_external) or (
        not is_external and not cover_config.compress_embed
    )
    if skip_processing:
        return ArtworkSettings(
            should_resize=False,
            resolution=cover_config.main_resolution,
            compression=cover_config.main_compression,
            format="jpg",
        )
    return ArtworkSettings(
        should_resize=should_resize,
        resolution=cover_config.main_resolution,
        compression=cover_config.main_compression,
        format="jpg",
    )
