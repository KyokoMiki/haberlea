"""Track downloader — fetches track info and downloads audio files.

Single responsibility: get track metadata + download audio bytes.
No tagging, transcoding, or cover art.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from haberlea.downloader.contexts import ProgressUpdate, TrackContext
from haberlea.downloader.results import TrackDownloadOutput, TrackFileResult
from haberlea.i18n import _
from haberlea.utils.exceptions import InvalidTrackError
from haberlea.utils.models import (
    CodecEnum,
    CodecOptions,
    DownloadEnum,
    DownloadTypeEnum,
    QualityEnum,
    TrackDownloadInfo,
    TrackInfo,
)
from haberlea.utils.progress import ProgressStatus, set_current_task
from haberlea.utils.utils import DownloadConfig, download_file, move_file

if TYPE_CHECKING:
    from pathlib import Path

    from haberlea.download_queue import DownloadJob, DownloadQueue, TrackTask
    from haberlea.downloader.protocols import ModuleProvider
    from haberlea.utils.path_builder import PathBuilder
    from haberlea.utils.settings import (
        ArtistDownloadingSettings,
        DownloadBehaviorSettings,
        FormattingSettings,
        QualitySettings,
        RuntimeSettings,
    )
    from haberlea.utils.tempfile_manager import TempFileManager

logger = logging.getLogger(__name__)


class TrackDownloader:
    """Downloads track audio files.

    Single responsibility: fetch track info + download audio bytes.
    """

    def __init__(
        self,
        queue: DownloadQueue,
        temp: TempFileManager,
        runtime: RuntimeSettings,
        quality: QualitySettings,
        download_behavior: DownloadBehaviorSettings,
        formatting: FormattingSettings,
        artist_downloading: ArtistDownloadingSettings,
        modules: ModuleProvider,
        path_builder: PathBuilder,
    ) -> None:
        """Initialize the track downloader.

        Args:
            queue: The global download queue.
            temp: Temporary file manager.
            runtime: Runtime settings (debug_mode is consumed).
            quality: Quality and codec preferences.
            download_behavior: Per-track download behavior settings.
            formatting: File/folder naming format settings.
            artist_downloading: Artist downloading behavior.
            modules: Module provider.
            path_builder: Path builder for track locations.
        """
        self._queue = queue
        self._temp = temp
        self._runtime = runtime
        self._quality = quality
        self._download_behavior = download_behavior
        self._formatting = formatting
        self._artist_downloading = artist_downloading
        self._modules = modules
        self._path_builder = path_builder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type((aiohttp.ClientError, TimeoutError, OSError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def download(self, task: TrackTask) -> TrackDownloadOutput:
        """Downloads a single track.

        Args:
            task: The track task containing all download information.

        Returns:
            TrackDownloadOutput with result, TrackContext, and file result.
        """
        track_id = task.track_id
        module = task.module

        track_info = await module.get_track_info(
            track_id,
            QualityEnum[self._quality.tier.upper()],
            CodecOptions(
                spatial_codecs=self._quality.spatial_codecs,
                proprietary_codecs=self._quality.proprietary_codecs,
            ),
            data=task.track_data,
        )

        if track_info is None:
            raise InvalidTrackError(track_id, "Track info is None")

        # Store track_info in job for extensions
        job = self._queue.get_job(task.job_id)
        if job:
            job.progress.track_infos[track_id] = track_info

        await self._queue.update_progress(
            ProgressUpdate(
                track_id=track_id,
                status=ProgressStatus.DOWNLOADING,
                name=track_info.name,
                artist=", ".join(track_info.artists),
                album=track_info.album,
                message="",
            )
        )

        if self._check_artist_filter(task, track_info, job):
            logger.debug("Skipping %s: different artist", track_info.name)
            return TrackDownloadOutput(
                path=None,
                status=ProgressStatus.SKIPPED,
            )

        self._update_track_numbering(task, track_info)

        logger.info("=== Track: %s (%s) ===", track_info.name, track_id)
        if track_info.album:
            logger.info("Album: %s", track_info.album)
        logger.info("Artists: %s", ", ".join(track_info.artists))
        logger.info("Codec: %s", track_info.codec.pretty_name)

        if track_info.error:
            raise InvalidTrackError(track_id, track_info.error)

        download_location = task.download_path
        location_name = await self._prepare_track_location(
            track_info, download_location, task
        )

        # Build TrackContext — all downstream methods use this
        ctx = TrackContext(
            task=task, track_info=track_info, location_name=location_name
        )

        existing_path = self.check_file_exists(ctx)
        if existing_path is not None:
            logger.info(
                "Dry run mode: skipping download"
                if self._download_behavior.dry_run
                else "Track already exists, skipping"
            )
            return TrackDownloadOutput(
                path=existing_path,
                status=ProgressStatus.SKIPPED,
                ctx=ctx,
            )

        await self._queue.update_progress(
            ProgressUpdate(track_id=track_id, message=_("Downloading audio file"))
        )
        file_result = await self._download_track_file(ctx)
        if file_result.path is None:
            raise InvalidTrackError(track_id, "Track download failed")

        return TrackDownloadOutput(
            path=file_result.path,
            status=ProgressStatus.COMPLETED,
            ctx=ctx,
            file_result=file_result,
        )

    def check_file_exists(self, ctx: TrackContext) -> Path | None:
        """Checks if track file already exists.

        Args:
            ctx: Track context with codec and location info.

        Returns:
            The existing file path, or None if not found.
        """
        check_location = ctx.location

        if self._download_behavior.dry_run:
            return check_location

        if (
            check_location.is_file()
            and not self._download_behavior.force_redownload_existing
        ):
            return check_location

        return None

    def build_track_context(
        self, task: TrackTask, track_info: TrackInfo, location_name: Path
    ) -> TrackContext:
        """Builds a TrackContext from task + fetched track info.

        Args:
            task: The track task.
            track_info: Fetched track metadata.
            location_name: Computed location name without extension.

        Returns:
            TrackContext for use in the pipeline.
        """
        return TrackContext(
            task=task,
            track_info=track_info,
            location_name=location_name,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_artist_filter(
        self, task: TrackTask, track_info: TrackInfo, job: DownloadJob | None
    ) -> bool:
        """Check if track should be skipped due to artist filter."""
        is_artist_download = (
            job and job.definition.media_type == DownloadTypeEnum.artist
        )
        if not is_artist_download or not task.main_artist:
            return False
        if not self._artist_downloading.ignore_different_artists:
            return False
        return task.main_artist.lower() not in [a.lower() for a in track_info.artists]

    def _update_track_numbering(self, task: TrackTask, track_info: TrackInfo) -> None:
        """Update track numbering if needed."""
        if self._formatting.force_album_format:
            return
        if task.track_index:
            track_info.tags.track_number = task.track_index
        if task.total_tracks:
            track_info.tags.total_tracks = task.total_tracks

    async def _prepare_track_location(
        self,
        track_info: TrackInfo,
        download_location: Path,
        task: TrackTask,
    ) -> Path:
        """Prepare track file location.

        Returns:
            The location name (path without extension).
        """

        job = self._queue.get_job(task.job_id)
        is_single_track_download = (
            job is None or job.definition.media_type == DownloadTypeEnum.track
        )

        if is_single_track_download:
            if self._formatting.force_album_format:
                album_info = await task.module.get_album_info(track_info.album_id)
                if album_info:
                    download_location = self._path_builder.build_album_path(
                        track_info.album_id, album_info
                    )
                    if not self._download_behavior.dry_run:
                        download_location.mkdir(parents=True, exist_ok=True)
                    track_location_name = self._path_builder.build_track_path(
                        track_info, download_location, DownloadTypeEnum.album
                    )
                else:
                    track_location_name = self._path_builder.build_track_path(
                        track_info, download_location, DownloadTypeEnum.track
                    )
            else:
                track_location_name = self._path_builder.build_track_path(
                    track_info, download_location, DownloadTypeEnum.track
                )
        else:
            track_location_name = self._path_builder.build_track_path(
                track_info, download_location, DownloadTypeEnum.album
            )

        return track_location_name

    async def _execute_download(
        self,
        track_id: str,
        download_info: TrackDownloadInfo,
        download_location: Path,
    ) -> None:
        """Execute the actual file download."""
        match download_info.download_type:
            case DownloadEnum.URL:
                if download_info.file_url is None:
                    raise ValueError(
                        f"Track {track_id}: file_url is None for URL download type"
                    )
                await download_file(
                    download_info.file_url,
                    download_location,
                    config=DownloadConfig(
                        headers=download_info.file_url_headers or {},
                        task_id=track_id,
                    ),
                )
            case DownloadEnum.DIRECT:
                pass
            case _:
                raise ValueError(
                    f"Unsupported download type: {download_info.download_type}"
                )

    async def _handle_codec_change(
        self,
        download_location: Path,
        track_location_name: Path,
        new_codec: CodecEnum,
    ) -> Path:
        """Handle codec change by moving file to new location."""
        new_container = new_codec.container
        if self._download_behavior.download_to_temp:
            new_location = self._temp.get_temp_filename(suffix=f".{new_container.name}")
        else:
            new_location = track_location_name.parent / (
                track_location_name.name + f".{new_container.name}"
            )
        await move_file(download_location, new_location)
        return new_location

    async def _download_track_file(self, ctx: TrackContext) -> TrackFileResult:
        """Download the actual track file.

        Args:
            ctx: Track context with codec, container, location derived from properties.

        Returns:
            TrackFileResult with downloaded file path, codec, and container.
        """
        codec = ctx.track_info.codec
        container = ctx.track_info.codec.container
        track_location = ctx.location

        if self._download_behavior.download_to_temp:
            download_location = self._temp.get_temp_filename(
                suffix=f".{container.name}"
            )
        else:
            download_location = track_location

        set_current_task(ctx.task.track_id)
        try:
            download_info: TrackDownloadInfo = await ctx.task.module.get_track_download(
                target_path=download_location,
                url=ctx.track_info.download_url or "",
                data=ctx.track_info.download_data,
            )

            await self._execute_download(
                ctx.task.track_id, download_info, download_location
            )

            if download_info.different_codec:
                codec = download_info.different_codec
                container = codec.container
                download_location = await self._handle_codec_change(
                    download_location, ctx.location_name, codec
                )

            return TrackFileResult(
                path=download_location, codec=codec, container=container
            )

        except KeyboardInterrupt:
            raise
        except Exception:
            if self._runtime.debug_mode:
                raise
            logger.exception("Track download failed for %s", ctx.task.track_id)
            if self._download_behavior.abort_download_when_single_failed:
                raise
            return TrackFileResult(path=None, codec=codec, container=container)
        finally:
            set_current_task(None)
