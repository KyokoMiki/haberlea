"""Track downloader — fetches track info and downloads audio files.

Single responsibility: get track metadata + download audio bytes.
No tagging, transcoding, or cover art.
"""

from __future__ import annotations

import contextlib
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
    MediaKindEnum,
    QualityEnum,
    TrackDownloadInfo,
    TrackInfo,
    VideoContainerEnum,
    VideoInfo,
    VideoQualityEnum,
)
from haberlea.utils.progress import ProgressStatus, set_current_task
from haberlea.utils.utils import DownloadConfig, download_file, move_file

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Sequence
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


# ---------------------------------------------------------------------------
# Pure / side-effecting module-level helpers shared by audio + video paths
# ---------------------------------------------------------------------------


async def _dispatch_download_payload(
    task_id: str,
    download_type: DownloadEnum,
    file_url: str | None,
    headers: dict[str, str] | None,
    target_path: Path,
    label: str,
) -> None:
    """Resolve a ``URL`` / ``DIRECT`` download payload to ``target_path``.

    Shared by track and video pipelines. Modules returning ``DIRECT``
    have already placed the file at ``target_path``; ``URL`` payloads
    are fetched here.

    Args:
        task_id: Task identifier (used as progress key + error context).
        download_type: ``DownloadEnum`` discriminator from the payload.
        file_url: Direct file URL (only required for ``URL`` type).
        headers: Optional HTTP headers for ``URL`` type.
        target_path: Destination path on disk.
        label: Human-readable label ("Track" / "Video") for error messages.

    Raises:
        ValueError: If ``URL`` is requested without ``file_url`` or the
            download type is not supported.
    """
    match download_type:
        case DownloadEnum.URL:
            if file_url is None:
                raise ValueError(
                    f"{label} {task_id}: file_url is None for URL download type"
                )
            await download_file(
                file_url,
                target_path,
                config=DownloadConfig(
                    headers=headers or {},
                    task_id=task_id,
                ),
            )
        case DownloadEnum.DIRECT:
            return
        case _:
            raise ValueError(
                f"Unsupported {label.lower()} download type: {download_type}"
            )


@contextlib.asynccontextmanager
async def _active_task(track_id: str) -> AsyncGenerator[None]:
    """Mark ``track_id`` as the current progress task for the duration."""
    set_current_task(track_id)
    try:
        yield
    finally:
        set_current_task(None)


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
        """Downloads a single track or music video.

        Both kinds share the same skeleton (fetch info → register → emit
        progress → log → build ctx → skip if existing → fetch payload).
        Per-kind details are dispatched via ``match`` against the
        returned ``info`` type.
        """
        track_id = task.track_id

        # ---- 1. Fetch metadata (kind-specific module call)
        info, video_container = await self._fetch_media_info(task)
        if info is None:
            raise InvalidTrackError(track_id, f"{task.media_kind.name} info is None")

        # ---- 2. Register info on the job for extension callbacks
        job = self._queue.get_job(task.job_id)
        if job is not None:
            match info:
                case TrackInfo():
                    job.progress.track_infos[track_id] = info
                case VideoInfo():
                    job.progress.video_infos[track_id] = info

        # ---- 3. Emit "started" progress
        await self._emit_started(
            track_id,
            name=info.name,
            artists=info.artists,
            album=info.album if isinstance(info, TrackInfo) else "",
        )

        # ---- 4. Audio-only early-skip + numbering side mutations
        match info:
            case TrackInfo():
                if self._check_artist_filter(task, info, job):
                    logger.debug("Skipping %s: different artist", info.name)
                    return TrackDownloadOutput(path=None, status=ProgressStatus.SKIPPED)
                self._update_track_numbering(task, info)
            case VideoInfo():
                pass

        self._log_media_header(info, track_id, video_container)

        if info.error:
            raise InvalidTrackError(track_id, info.error)

        # ---- 5. Resolve location + build context
        ctx = await self._build_media_context(
            task, info, video_container=video_container
        )

        # ---- 6. Skip if file already exists
        kind_label = "Video" if isinstance(info, VideoInfo) else "Track"
        skipped = self._skip_if_existing(ctx, kind_label=kind_label)
        if skipped is not None:
            return skipped

        # ---- 7. Fetch payload (kind-specific module call)
        return await self._fetch_media_payload(ctx, info)

    async def _fetch_media_info(
        self, task: TrackTask
    ) -> tuple[TrackInfo | VideoInfo | None, VideoContainerEnum | None]:
        """Fetch track or video metadata via the module's API.

        Returns:
            ``(info, video_container)`` — ``video_container`` is None for
            audio tasks. The video container reflects user preference
            possibly overridden by the module-resolved one.
        """
        module = task.module
        match task.media_kind:
            case MediaKindEnum.audio:
                track_info = await module.get_track_info(
                    task.track_id,
                    QualityEnum[self._quality.tier.upper()],
                    CodecOptions(
                        spatial_codecs=self._quality.spatial_codecs,
                        proprietary_codecs=self._quality.proprietary_codecs,
                    ),
                    data=task.track_data,
                )
                return track_info, None
            case MediaKindEnum.video:
                quality_tier, container = self._resolve_video_quality_settings()
                video_info = await module.get_video_info(
                    task.track_id, quality_tier, data=task.track_data
                )
                if video_info is not None and video_info.container is not None:
                    container = video_info.container
                return video_info, container

    def _log_media_header(
        self,
        info: TrackInfo | VideoInfo,
        track_id: str,
        video_container: VideoContainerEnum | None,
    ) -> None:
        """Log the ``=== Track/Video: ... ===`` header block."""
        match info:
            case TrackInfo():
                logger.info("=== Track: %s (%s) ===", info.name, track_id)
                if info.album:
                    logger.info("Album: %s", info.album)
                logger.info("Artists: %s", ", ".join(info.artists))
                logger.info("Codec: %s", info.codec.pretty_name)
            case VideoInfo():
                logger.info("=== Music Video: %s (%s) ===", info.name, track_id)
                if info.artists:
                    logger.info("Artists: %s", ", ".join(info.artists))
                if video_container is not None:
                    logger.info("Container: %s", video_container.value)

    async def _build_media_context(
        self,
        task: TrackTask,
        info: TrackInfo | VideoInfo,
        *,
        video_container: VideoContainerEnum | None,
    ) -> TrackContext:
        """Compute the location path and build the per-task TrackContext."""
        match info:
            case TrackInfo():
                location_name = await self._prepare_track_location(
                    info, task.download_path, task
                )
                return TrackContext(
                    task=task, track_info=info, location_name=location_name
                )
            case VideoInfo():
                location_name = self._path_builder.build_video_path(
                    info, video_id=task.track_id
                )
                return TrackContext(
                    task=task,
                    location_name=location_name,
                    video_info=info,
                    video_container=video_container,
                )

    async def _fetch_media_payload(
        self,
        ctx: TrackContext,
        info: TrackInfo | VideoInfo,
    ) -> TrackDownloadOutput:
        """Fetch the actual media bytes and return the final output."""
        track_id = ctx.task.track_id
        match info:
            case TrackInfo():
                await self._queue.update_progress(
                    ProgressUpdate(
                        track_id=track_id,
                        message=_("Downloading audio file"),
                    )
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
            case VideoInfo():
                return await self._fetch_video_payload(ctx, info)

    async def _fetch_video_payload(
        self, ctx: TrackContext, video_info: VideoInfo
    ) -> TrackDownloadOutput:
        """Fetch a music video payload via the module's video download API."""
        track_id = ctx.task.track_id
        await self._queue.update_progress(
            ProgressUpdate(track_id=track_id, message=_("Downloading video"))
        )
        target_path = ctx.location
        target_path.parent.mkdir(parents=True, exist_ok=True)

        async with _active_task(track_id):
            download_info: TrackDownloadInfo = await ctx.task.module.get_video_download(
                target_path=target_path,
                url="",
                data=video_info.download_data,
            )
            await _dispatch_download_payload(
                track_id,
                download_info.download_type,
                download_info.file_url,
                download_info.file_url_headers,
                target_path,
                label="Video",
            )

        if not target_path.is_file():
            raise InvalidTrackError(
                track_id, f"Video download produced no file at {target_path}"
            )
        return TrackDownloadOutput(
            path=target_path,
            status=ProgressStatus.COMPLETED,
            ctx=ctx,
            file_result=None,
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

    async def _emit_started(
        self,
        track_id: str,
        *,
        name: str,
        artists: Sequence[str],
        album: str = "",
    ) -> None:
        """Push a ``DOWNLOADING`` progress update for a task.

        Used by both the audio and video pipelines as the canonical
        "started fetching the actual payload" notification.
        """
        await self._queue.update_progress(
            ProgressUpdate(
                track_id=track_id,
                status=ProgressStatus.DOWNLOADING,
                name=name,
                artist=", ".join(artists) if artists else "",
                album=album,
                message="",
            )
        )

    def _skip_if_existing(
        self, ctx: TrackContext, *, kind_label: str
    ) -> TrackDownloadOutput | None:
        """Return a ``SKIPPED`` output if the target file already exists.

        Args:
            ctx: Built context whose ``location`` is checked.
            kind_label: "Track" / "Video" — used only in the log line.

        Returns:
            A ``TrackDownloadOutput`` with ``SKIPPED`` status when an
            existing file is found (or in dry-run mode); otherwise None.
        """
        existing = self.check_file_exists(ctx)
        if existing is None:
            return None
        if self._download_behavior.dry_run:
            logger.info("Dry run mode: skipping %s download", kind_label.lower())
        else:
            logger.info("%s already exists, skipping", kind_label)
        return TrackDownloadOutput(
            path=existing,
            status=ProgressStatus.SKIPPED,
            ctx=ctx,
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
        track_info = ctx.track_info
        if track_info is None:
            raise RuntimeError("_download_track_file requires audio track_info")
        codec = track_info.codec
        container = track_info.codec.container
        track_location = ctx.location

        if self._download_behavior.download_to_temp:
            download_location = self._temp.get_temp_filename(
                suffix=f".{container.name}"
            )
        else:
            download_location = track_location

        try:
            async with _active_task(ctx.task.track_id):
                download_info: TrackDownloadInfo = (
                    await ctx.task.module.get_track_download(
                        target_path=download_location,
                        url=track_info.download_url or "",
                        data=track_info.download_data,
                    )
                )

                await _dispatch_download_payload(
                    ctx.task.track_id,
                    download_info.download_type,
                    download_info.file_url,
                    download_info.file_url_headers,
                    download_location,
                    label="Track",
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

    def _resolve_video_quality_settings(
        self,
    ) -> tuple[VideoQualityEnum, VideoContainerEnum]:
        """Read user video quality / container preferences with safe fallbacks.

        Returns:
            ``(VideoQualityEnum, VideoContainerEnum)`` — defaults to
            ``HIGH`` / ``mkv`` if the persisted value cannot be parsed.
        """
        try:
            quality_tier = VideoQualityEnum[self._quality.video_tier.upper()]
        except KeyError:
            quality_tier = VideoQualityEnum.HIGH
        try:
            container = VideoContainerEnum(self._quality.video_container.lower())
        except ValueError:
            container = VideoContainerEnum.mkv
        return quality_tier, container
