"""Base classes for Haberlea plugins.

This module defines the abstract base classes that all modules and extensions
must implement to be compatible with the Haberlea plugin system.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

if TYPE_CHECKING:
    from collections.abc import Callable

    from haberlea.download_queue import DownloadJob
    from haberlea.utils.models import (
        AlbumInfo,
        ArtistInfo,
        CodecOptions,
        CoverInfo,
        CoverOptions,
        CreditsInfo,
        DownloadTypeEnum,
        LyricsInfo,
        ModuleController,
        PlaylistInfo,
        QualityEnum,
        SearchResult,
        TrackDownloadInfo,
        TrackInfo,
        VideoInfo,
        VideoQualityEnum,
    )


class TrackCompleteEvent(msgspec.Struct, frozen=True):
    """Track completion event data — replaces 3-arg on_track_complete."""

    job: Any  # DownloadJob — avoid circular import
    track_id: str
    track_info: Any  # TrackInfo


class WebUIPageBase(ABC):
    """Abstract base class for extension WebUI pages.

    Extensions can provide a WebUI page by implementing this class and
    registering it via ExtensionInformation.webui_page.

    Class Attributes:
        page_id: Unique identifier for the page (used in URL routing).
        page_label: Display label for the page in navigation.
        page_icon: Material icon name for the page.
        page_order: Order in navigation (lower numbers appear first).
    """

    page_id: str
    page_label: str
    page_icon: str
    page_order: int = 100

    @abstractmethod
    def render(self) -> None:
        """Render the page content.

        This method is called when the page is displayed. Use nicegui
        components to build the UI.
        """
        ...


class ModuleBase(ABC):
    """Abstract base class for music service modules.

    All modules must inherit from this class to be compatible with Haberlea.
    Modules handle authentication, metadata retrieval, and track downloading
    from music streaming services.
    """

    def __init__(self, module_controller: "ModuleController") -> None:
        """Initialize the module.

        Args:
            module_controller: Controller providing access to settings and resources.
        """
        self.module_controller = module_controller

    async def close(self) -> None:
        """Close the module and release resources."""
        return None

    async def login(self, email: str, password: str) -> None:
        """Authenticate with the music service.

        Args:
            email: User email or username.
            password: User password.
        """
        return None

    @abstractmethod
    async def get_track_info(
        self,
        track_id: str,
        quality_tier: "QualityEnum",
        codec_options: "CodecOptions",
        data: dict[str, Any] | None = None,
    ) -> "TrackInfo":
        """Get track metadata and streaming information.

        Args:
            track_id: Unique identifier for the track.
            quality_tier: Desired audio quality.
            codec_options: Codec preference options.
            data: Optional pre-fetched track data.

        Returns:
            TrackInfo containing metadata and download information.
        """
        ...

    @abstractmethod
    async def get_track_download(
        self,
        target_path: Path,
        url: str = "",
        data: "dict | None" = None,
    ) -> "TrackDownloadInfo":
        """Get download information for a track.

        Args:
            target_path: Target file path for direct download.
            url: The URL to download the track from.
            data: Optional extra data for download (e.g., audio_track, file_url).

        Returns:
            TrackDownloadInfo with download URL or file path.
        """
        ...

    async def search(
        self,
        query_type: "DownloadTypeEnum",
        query: str,
        track_info: "TrackInfo | None" = None,
        limit: int = 10,
    ) -> "list[SearchResult]":
        """Search for content on the service.

        Args:
            query_type: Type of content to search for.
            query: Search query string.
            track_info: Optional track info for ISRC-based search.
            limit: Maximum number of results.

        Returns:
            List of search results.
        """
        return []

    def custom_url_parse(self, url: str) -> Any:
        """Parse a custom URL format for this service.

        Args:
            url: The URL to parse.

        Returns:
            MediaIdentification or similar object with parsed media info.
        """
        return None

    async def get_album_info(
        self, album_id: str, data: dict[str, Any] | None = None
    ) -> "AlbumInfo":
        """Get album metadata and track list.

        Args:
            album_id: Unique identifier for the album.
            data: Optional pre-fetched album data.

        Returns:
            AlbumInfo containing metadata and track list.
        """
        raise NotImplementedError

    async def get_playlist_info(self, playlist_id: str) -> "PlaylistInfo":
        """Get playlist metadata and track list.

        Args:
            playlist_id: Unique identifier for the playlist.

        Returns:
            PlaylistInfo containing metadata and track list.
        """
        raise NotImplementedError

    async def get_artist_info(
        self, artist_id: str, get_credited_albums: bool = False
    ) -> "ArtistInfo":
        """Get artist metadata and discography.

        Args:
            artist_id: Unique identifier for the artist.
            get_credited_albums: Whether to include albums where artist is credited.

        Returns:
            ArtistInfo containing metadata and album/track lists.
        """
        raise NotImplementedError

    async def get_video_info(
        self,
        video_id: str,
        quality_tier: "VideoQualityEnum",
        data: dict[str, Any] | None = None,
    ) -> "VideoInfo":
        """Get music video metadata and HLS stream information.

        Modules implementing this method must populate ``download_data`` so
        that ``get_video_download`` can resume the download without redoing
        the master/variant playlist resolution.

        Args:
            video_id: Unique identifier for the music video.
            quality_tier: Desired video quality tier.
            data: Optional pre-fetched video data.

        Returns:
            VideoInfo containing metadata and resolved variant payload.

        Raises:
            NotImplementedError: If the module does not support video downloads.
        """
        raise NotImplementedError

    async def get_video_download(
        self,
        target_path: Path,
        url: str = "",
        data: dict | None = None,
    ) -> "TrackDownloadInfo":
        """Download a music video to ``target_path``.

        The default contract is ``DownloadEnum.DIRECT``: the module fetches
        all HLS segments, optionally remuxes the result, and places the
        final file at ``target_path``.

        Args:
            target_path: Final output file path (extension already chosen
                by the framework based on the configured container).
            url: Optional source URL hint.
            data: Module payload from ``VideoInfo.download_data``.

        Returns:
            TrackDownloadInfo describing the download outcome
            (``different_codec`` is unused for videos).

        Raises:
            NotImplementedError: If the module does not support video downloads.
        """
        raise NotImplementedError

    async def get_track_cover(
        self,
        track_id: str,
        cover_options: "CoverOptions",
        data: dict[str, Any] | None = None,
    ) -> "CoverInfo":
        """Get track cover image information.

        Args:
            track_id: Unique identifier for the track.
            cover_options: Cover image options (resolution, format, etc.).
            data: Optional pre-fetched data.

        Returns:
            CoverInfo with cover URL and file type.
        """
        raise NotImplementedError

    async def get_track_lyrics(
        self, track_id: str, data: dict[str, Any] | None = None
    ) -> "LyricsInfo":
        """Get track lyrics.

        Args:
            track_id: Unique identifier for the track.
            data: Optional pre-fetched data.

        Returns:
            LyricsInfo with embedded and/or synced lyrics.
        """
        raise NotImplementedError

    async def get_track_credits(
        self, track_id: str, data: dict[str, Any] | None = None
    ) -> "list[CreditsInfo]":
        """Get track credits information.

        Args:
            track_id: Unique identifier for the track.
            data: Optional pre-fetched data.

        Returns:
            List of CreditsInfo with contributor information.
        """
        return []


# Type alias for extension log callback
ExtensionLogCallback = "Callable[[str], None] | None"


class ExtensionBase(ABC):
    """Abstract base class for post-download extensions."""

    # Class-level log callback shared by all extensions
    _log_callback: "Callable[[str], None] | None" = None

    def __init__(self, settings: dict[str, Any]) -> None:
        """Initialize the extension.

        Args:
            settings: Extension-specific configuration dictionary.
        """
        self.settings = settings

    @classmethod
    def set_log_callback(cls, callback: "Callable[[str], None] | None") -> None:
        """Sets the log callback for all extensions.

        Args:
            callback: Function to call with log messages, or None to use print.
        """
        cls._log_callback = callback

    def log(self, message: str) -> None:
        """Logs a message using the configured callback or logger.

        Args:
            message: Message to log.
        """
        if ExtensionBase._log_callback:
            ExtensionBase._log_callback(message)
        else:
            logging.getLogger(__name__).info("%s", message)

    @abstractmethod
    async def on_job_complete(self, job: "DownloadJob") -> None:
        """Called when a download job completes.

        Args:
            job: The completed download job containing all track information.
        """
        ...

    async def on_track_complete(self, event: "TrackCompleteEvent") -> None:
        """Called when a single track download completes.

        Args:
            event: TrackCompleteEvent with job, track_id, and track_info.
        """
        return None

    async def on_all_complete(self) -> None:
        """Called after all download jobs have completed.

        This method is called once after all download tasks and per-job
        on_job_complete calls have finished. Useful for batch operations
        like uploading all downloaded files together.
        """
        return None
