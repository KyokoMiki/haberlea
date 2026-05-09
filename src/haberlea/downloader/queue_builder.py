"""Queue builder — converts media requests into DownloadQueue entries.

Single responsibility: metadata fetching + job/task creation.
No audio file I/O.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import anyio

from haberlea.download_queue import DownloadQueue, TrackTask
from haberlea.downloader.contexts import (
    AlbumQueueRequest,
    ArtistQueueRequest,
    JobDefinition,
    PlaylistQueueRequest,
    TrackQueueRequest,
    VideoQueueRequest,
    build_artwork_settings,
)
from haberlea.utils.models import DownloadTypeEnum, MediaKindEnum
from haberlea.utils.settings import (
    ArtistDownloadingSettings,
    CoversSettings,
    DownloadBehaviorSettings,
)
from haberlea.utils.utils import (
    _process_artwork,
    download_file,
    format_duration,
    sanitise_name,
)

if TYPE_CHECKING:
    from pathlib import Path

    from haberlea.downloader.protocols import ModuleProvider
    from haberlea.utils.models import AlbumInfo, ArtistInfo, PlaylistInfo
    from haberlea.utils.path_builder import PathBuilder

logger = logging.getLogger(__name__)


class QueueBuilder:
    """Builds download queue from user requests.

    Single responsibility: convert media requests into queue entries.
    No file I/O beyond metadata fetching via module.
    """

    def __init__(
        self,
        queue: DownloadQueue,
        path_builder: PathBuilder,
        modules: ModuleProvider,
        download_behavior: DownloadBehaviorSettings | None = None,
        artist_downloading: ArtistDownloadingSettings | None = None,
        cover_config: CoversSettings | None = None,
    ) -> None:
        """Initialize the queue builder.

        Args:
            queue: The global download queue.
            path_builder: Path builder for constructing file paths.
            modules: Module provider for artwork settings.
            download_behavior: Download behavior settings (dry_run is consumed).
            artist_downloading: Artist downloading behavior settings.
            cover_config: External cover art configuration.
        """
        self.queue = queue
        self._path_builder = path_builder
        self._modules = modules
        self._download_behavior = download_behavior or DownloadBehaviorSettings()
        self._artist_downloading = artist_downloading or ArtistDownloadingSettings()
        self._cover_config = cover_config or CoversSettings()

    # ------------------------------------------------------------------
    # Public queue methods
    # ------------------------------------------------------------------

    async def queue_track(self, request: TrackQueueRequest) -> str:
        """Queues a single track for download.

        Args:
            request: Track queue request.

        Returns:
            The job ID for this download.
        """
        module_name = request.module.name
        module = request.module.instance
        track_id = request.track_id

        job_id = await self.queue.create_job(
            JobDefinition(
                original_url=request.original_url or f"{module_name}:track:{track_id}",
                media_type=DownloadTypeEnum.track,
                media_id=track_id,
                module_name=module_name,
                download_path=self._path_builder.base_path,
            )
        )

        task = TrackTask(
            track_id=track_id,
            job_id=job_id,
            module_name=module_name,
            module=module,
            download_path=self._path_builder.base_path,
            track_data=request.track_data,
        )
        await self.queue.add_track(job_id, task)
        logger.debug("Queued track: %s", track_id)
        return job_id

    async def queue_video(self, request: VideoQueueRequest) -> str:
        """Queues a single music video for download.

        Builds a ``TrackTask`` with ``media_kind=MediaKindEnum.video`` so
        the queue, orchestrator, and webui can stay media-kind agnostic.

        Args:
            request: Video queue request.

        Returns:
            The job ID for this download.
        """
        module_name = request.module.name
        module = request.module.instance
        video_id = request.video_id

        job_id = await self.queue.create_job(
            JobDefinition(
                original_url=request.original_url or f"{module_name}:video:{video_id}",
                media_type=DownloadTypeEnum.video,
                media_id=video_id,
                module_name=module_name,
                download_path=self._path_builder.base_path,
            )
        )

        task = TrackTask(
            track_id=video_id,
            job_id=job_id,
            module_name=module_name,
            module=module,
            download_path=self._path_builder.base_path,
            track_data=request.video_data,
            media_kind=MediaKindEnum.video,
        )
        await self.queue.add_track(job_id, task)
        logger.debug("Queued video: %s", video_id)
        return job_id

    async def queue_album(
        self,
        request: AlbumQueueRequest,
    ) -> AlbumInfo | None:
        """Queues an album's tracks for download.

        Args:
            request: Album queue request.

        Returns:
            AlbumInfo if successful, None otherwise.
        """
        module_name = request.module.name
        module = request.module.instance
        album_id = request.album_id

        album_info = await module.get_album_info(album_id, data=request.album_data)
        if not album_info:
            logger.warning("Failed to get album info: %s", album_id)
            return None
        album_path = self._path_builder.build_album_path(album_id, album_info)
        if request.base_path is not None:
            album_path = request.base_path / album_path.name

        # Create job (unless part of artist download)
        job_id = request.parent_job_id
        if not job_id:
            job_id = await self.queue.create_job(
                JobDefinition(
                    original_url=request.original_url
                    or f"{module_name}:album:{album_id}",
                    media_type=DownloadTypeEnum.album,
                    media_id=album_id,
                    module_name=module_name,
                    name=album_info.name,
                    artist=request.artist_name or album_info.artist,
                    download_path=album_path,
                    cover_url=album_info.cover_url or "",
                )
            )

            if not self._download_behavior.dry_run:
                await self._download_album_assets(album_path, album_info, module_name)

        logger.info("=== Album: %s (%s) ===", album_info.name, album_id)
        logger.info("Artist: %s", album_info.artist)
        if album_info.release_year:
            logger.info("Year: %s", album_info.release_year)
        logger.info("Tracks: %d", len(album_info.tracks))

        for index, track_id in enumerate(album_info.tracks, start=1):
            task = TrackTask(
                track_id=track_id,
                job_id=job_id,
                module_name=module_name,
                module=module,
                download_path=album_path,
                track_index=index,
                total_tracks=len(album_info.tracks),
                main_artist=request.artist_name or album_info.artist,
                track_data=album_info.track_data,
            )
            await self.queue.add_track(job_id, task)

        logger.info("Queued %d tracks from album", len(album_info.tracks))
        return album_info

    async def queue_playlist(
        self,
        request: PlaylistQueueRequest,
    ) -> PlaylistInfo | None:
        """Queues a playlist's tracks for download.

        Args:
            request: Playlist queue request.

        Returns:
            PlaylistInfo if successful, None otherwise.
        """
        module_name = request.module.name
        module = request.module.instance
        playlist_id = request.playlist_id

        playlist_info = await module.get_playlist_info(playlist_id)
        if not playlist_info:
            logger.warning("Failed to get playlist info: %s", playlist_id)
            return None

        playlist_path = self._path_builder.build_playlist_path(playlist_info)

        dl_module = request.custom_module.instance if request.custom_module else module
        dl_module_name = (
            request.custom_module.name if request.custom_module else module_name
        )

        job_id = await self.queue.create_job(
            JobDefinition(
                original_url=request.original_url
                or f"{module_name}:playlist:{playlist_id}",
                media_type=DownloadTypeEnum.playlist,
                media_id=playlist_id,
                module_name=dl_module_name,
                name=playlist_info.name,
                artist=playlist_info.creator,
                download_path=playlist_path,
                cover_url=playlist_info.cover_url or "",
            )
        )

        if not self._download_behavior.dry_run:
            await self._download_playlist_assets(
                playlist_path, playlist_info, dl_module_name
            )

        logger.info("=== Playlist: %s (%s) ===", playlist_info.name, playlist_id)
        logger.info("Creator: %s", playlist_info.creator)
        if playlist_info.duration:
            logger.info("Duration: %s", format_duration(playlist_info.duration))
        logger.info("Tracks: %d", len(playlist_info.tracks))

        for index, track_id in enumerate(playlist_info.tracks, start=1):
            task = TrackTask(
                track_id=track_id,
                job_id=job_id,
                module_name=dl_module_name,
                module=dl_module,
                download_path=playlist_path,
                track_index=index,
                total_tracks=len(playlist_info.tracks),
                track_data=playlist_info.track_data,
            )
            await self.queue.add_track(job_id, task)

        logger.info("Queued %d tracks from playlist", len(playlist_info.tracks))
        return playlist_info

    async def queue_artist(
        self,
        request: ArtistQueueRequest,
    ) -> ArtistInfo | None:
        """Queues an artist's albums and tracks for download.

        Args:
            request: Artist queue request.

        Returns:
            ArtistInfo if successful, None otherwise.
        """
        module_name = request.module.name
        module = request.module.instance
        artist_id = request.artist_id

        artist_info = await module.get_artist_info(
            artist_id,
            self._artist_downloading.return_credited_albums,
        )
        if not artist_info:
            logger.warning("Failed to get artist info: %s", artist_id)
            return None

        artist_name = artist_info.name
        artist_path = self._path_builder.base_path / sanitise_name(artist_name)

        job_id = await self.queue.create_job(
            JobDefinition(
                original_url=request.original_url
                or f"{module_name}:artist:{artist_id}",
                media_type=DownloadTypeEnum.artist,
                media_id=artist_id,
                module_name=module_name,
                name=artist_name,
                artist=artist_name,
                download_path=artist_path,
            )
        )

        logger.info("=== Artist: %s (%s) ===", artist_name, artist_id)
        if artist_info.albums:
            logger.info("Albums: %d", len(artist_info.albums))
        if artist_info.tracks:
            logger.info("Tracks: %d", len(artist_info.tracks))

        album_track_ids: set[str] = set()

        for album_id in artist_info.albums:
            album_info = await self.queue_album(
                AlbumQueueRequest(
                    module=request.module,
                    album_id=album_id,
                    artist_name=artist_name,
                    base_path=artist_path,
                    album_data=artist_info.album_data,
                    parent_job_id=job_id,
                )
            )
            if album_info is not None:
                album_track_ids.update(album_info.tracks)

        skip_downloaded = self._artist_downloading.separate_tracks_skip_downloaded
        for track_id in artist_info.tracks:
            if skip_downloaded and track_id in album_track_ids:
                logger.debug("Skipping track %s: already in album", track_id)
                continue

            task = TrackTask(
                track_id=track_id,
                job_id=job_id,
                module_name=module_name,
                module=module,
                download_path=artist_path,
                main_artist=artist_name,
                track_data=artist_info.track_data,
            )
            await self.queue.add_track(job_id, task)

        return artist_info

    # ------------------------------------------------------------------
    # Asset helpers (album/playlist cover, booklet, description)
    # ------------------------------------------------------------------

    async def _download_album_assets(
        self, album_path: Path, album_info: AlbumInfo, module_name: str
    ) -> None:
        """Download album cover, booklet, and description."""
        album_path.mkdir(parents=True, exist_ok=True)
        cc = self._cover_config

        if cc.save_external and album_info.cover_url:
            cover_path = album_path / "cover.jpg"
            if not cover_path.exists():
                await download_file(
                    album_info.cover_url,
                    cover_path,
                )
                artwork_settings = build_artwork_settings(
                    self._modules.get_module_flags(module_name),
                    self._cover_config,
                    is_external=True,
                )
                _process_artwork(cover_path, artwork_settings)

        if cc.save_animated_cover and album_info.animated_cover_url:
            animated_path = album_path / "cover_animated.mp4"
            if not animated_path.exists():
                await download_file(album_info.animated_cover_url, animated_path)

        if album_info.booklet_url:
            booklet_path = album_path / "Booklet.pdf"
            if not booklet_path.exists():
                await download_file(album_info.booklet_url, booklet_path)

        if album_info.description:
            desc_path = album_path / "description.txt"
            if not desc_path.exists():
                await anyio.Path(desc_path).write_text(
                    album_info.description, encoding="utf-8"
                )

    async def _download_playlist_assets(
        self, playlist_path: Path, playlist_info: Any, module_name: str
    ) -> None:
        """Download playlist cover and description."""
        playlist_path.mkdir(parents=True, exist_ok=True)
        cc = self._cover_config

        if cc.save_external and playlist_info.cover_url:
            cover_path = playlist_path / "cover.jpg"
            if not cover_path.exists():
                await download_file(
                    playlist_info.cover_url,
                    cover_path,
                )
                artwork_settings = build_artwork_settings(
                    self._modules.get_module_flags(module_name),
                    self._cover_config,
                    is_external=True,
                )
                _process_artwork(cover_path, artwork_settings)

        if cc.save_animated_cover and playlist_info.animated_cover_url:
            animated_path = playlist_path / "cover_animated.mp4"
            if not animated_path.exists():
                await download_file(playlist_info.animated_cover_url, animated_path)

        if playlist_info.description:
            desc_path = playlist_path / "description.txt"
            if not desc_path.exists():
                await anyio.Path(desc_path).write_text(
                    playlist_info.description, encoding="utf-8"
                )
