"""Downloader facade — thin orchestrator for the download pipeline.

Delegates to focused collaborators:
- QueueBuilder: queue population
- TrackDownloader: audio file download
- AssetManager: cover/lyrics/credits
- TrackFinalizer: tag/move
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

import anyio

from haberlea.downloader.asset_manager import AssetManager
from haberlea.downloader.contexts import (
    AlbumQueueRequest,
    ArtistQueueRequest,
    PlaylistQueueRequest,
    ProgressUpdate,
    TrackQueueRequest,
)
from haberlea.downloader.finalizer import PlaylistContext, TrackFinalizer, TrackMetadata
from haberlea.downloader.queue_builder import QueueBuilder
from haberlea.downloader.results import DownloadSummary, TrackDownloadOutput
from haberlea.downloader.track_downloader import TrackDownloader
from haberlea.i18n import _
from haberlea.plugins.base import TrackCompleteEvent
from haberlea.utils.models import DownloadTypeEnum, ModuleModes
from haberlea.utils.path_builder import PathBuilder
from haberlea.utils.progress import ProgressStatus
from haberlea.utils.settings import settings
from haberlea.utils.tempfile_manager import TempFileManager

if TYPE_CHECKING:
    from pathlib import Path

    from haberlea.download_queue import DownloadJob, DownloadQueue, TrackTask
    from haberlea.downloader.protocols import ModuleProvider
    from haberlea.utils.models import AlbumInfo, ArtistInfo, PlaylistInfo

logger = logging.getLogger(__name__)


class Downloader:
    """Thin facade orchestrating the download pipeline.

    Delegates to QueueBuilder, TrackDownloader, AssetManager, TrackFinalizer.
    ~150 lines. Four collaborator fields.
    """

    def __init__(
        self,
        modules: ModuleProvider,
        extensions: list[Any],
        path: Path,
        queue: DownloadQueue,
        third_party_modules: dict[ModuleModes, str],
    ) -> None:
        """Initialize the downloader facade.

        Args:
            modules: Module provider interface.
            extensions: List of loaded extension instances.
            path: Base download path.
            queue: Global download queue.
            third_party_modules: Third-party module mappings.
        """
        gs = settings.global_settings
        temp = TempFileManager(base_dir=gs.runtime.temp_path or None)
        path_builder = PathBuilder(path, gs.formatting)

        self._queue_builder = QueueBuilder(
            queue=queue,
            path_builder=path_builder,
            modules=modules,
            download_behavior=gs.download_behavior,
            artist_downloading=gs.artist_downloading,
            cover_config=gs.covers,
        )

        self._track_downloader = TrackDownloader(
            queue=queue,
            temp=temp,
            runtime=gs.runtime,
            quality=gs.quality,
            download_behavior=gs.download_behavior,
            formatting=gs.formatting,
            artist_downloading=gs.artist_downloading,
            modules=modules,
            path_builder=path_builder,
        )

        self._asset_manager = AssetManager(
            third_party_modules=third_party_modules,
            cover_config=gs.covers,
            lyrics_config=gs.lyrics,
            modules=modules,
            temp=temp,
        )

        self._finalizer = TrackFinalizer(
            temp=temp,
            covers=gs.covers,
            lyrics=gs.lyrics,
            playlist=gs.playlist,
        )

        self._extensions = extensions
        self._queue = queue
        self._abort_on_failure = gs.download_behavior.abort_download_when_single_failed

    async def __aenter__(self) -> Downloader:
        """Enter async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit async context manager."""

    # ------------------------------------------------------------------
    # Queue population (delegates to QueueBuilder)
    # ------------------------------------------------------------------

    async def queue_track(self, request: TrackQueueRequest) -> str:
        """Queues a single track for download."""
        return await self._queue_builder.queue_track(request)

    async def queue_album(
        self,
        request: AlbumQueueRequest,
    ) -> AlbumInfo | None:
        """Queues an album's tracks for download."""
        return await self._queue_builder.queue_album(request)

    async def queue_playlist(
        self,
        request: PlaylistQueueRequest,
    ) -> PlaylistInfo | None:
        """Queues a playlist's tracks for download."""
        return await self._queue_builder.queue_playlist(request)

    async def queue_artist(
        self,
        request: ArtistQueueRequest,
    ) -> ArtistInfo | None:
        """Queues an artist's albums and tracks for download."""
        return await self._queue_builder.queue_artist(request)

    # ------------------------------------------------------------------
    # Queue processing
    # ------------------------------------------------------------------

    async def process_queue(self) -> DownloadSummary:
        """Processes all queued tracks concurrently."""
        tasks = self._queue.get_all_track_tasks()
        if not tasks:
            logger.info("No tracks in queue")
            return DownloadSummary(completed=[], failed=[])

        logger.info("=== Processing %d tracks ===", len(tasks))

        async with anyio.create_task_group() as tg:
            for task in tasks:
                tg.start_soon(self._process_track, task)

        return self._queue.get_results()

    async def _process_track(self, task: TrackTask) -> None:
        """Single track pipeline: download → assets → finalize.

        Acquires the per-module limiter (if any) before the global limiter
        so that modules with internal parallelism (e.g. TIDAL) serialise at
        the track level without blocking a global concurrency slot while
        waiting. Tracks from other modules remain free to run in parallel.
        """
        track_id = task.track_id

        module_limiter = self._queue.get_module_limiter(task.module_name)

        async with AsyncExitStack() as stack:
            if module_limiter is not None:
                await stack.enter_async_context(module_limiter)
            await stack.enter_async_context(self._queue.limiter)
            await self._queue.update_progress(
                ProgressUpdate(track_id=track_id, message=_("Fetching track info..."))
            )
            try:
                output = await self._track_downloader.download(task)

                await self._queue.update_progress(
                    ProgressUpdate(
                        track_id=track_id,
                        status=output.status,
                        progress=1.0,
                        message=_("Download completed")
                        if output.status == ProgressStatus.COMPLETED
                        else _("Skipped"),
                    )
                )
                await self._queue.mark_track_complete(track_id, output.status)

                if (
                    output.status == ProgressStatus.COMPLETED
                    and output.ctx is not None
                    and output.file_result is not None
                ):
                    await self._run_post_download(task, output)

                job = self._queue.get_job(task.job_id)
                if job:
                    track_info = job.progress.track_infos.get(track_id)
                    if track_info:
                        await self._notify_extensions_track_complete(
                            job, track_id, track_info
                        )

            except Exception as e:
                logger.exception("Track download failed: %s", track_id)
                await self._queue.update_progress(
                    ProgressUpdate(
                        track_id=track_id,
                        status=ProgressStatus.FAILED,
                        message=str(e),
                    )
                )
                await self._queue.mark_track_complete(track_id, ProgressStatus.FAILED)
                if self._abort_on_failure:
                    raise

    async def _run_post_download(
        self, task: TrackTask, output: TrackDownloadOutput
    ) -> None:
        """Fetch assets and finalize after successful audio download."""
        ctx = output.ctx
        file_result = output.file_result
        if ctx is None or file_result is None:
            return

        job = self._queue.get_job(task.job_id)
        if not job:
            return

        # Fetch assets using TrackContext
        await self._queue.update_progress(
            ProgressUpdate(track_id=ctx.task.track_id, message=_("Downloading cover"))
        )
        cover = await self._asset_manager.download_cover(ctx)

        await self._queue.update_progress(
            ProgressUpdate(track_id=ctx.task.track_id, message=_("Fetching lyrics"))
        )
        lyrics = await self._asset_manager.get_lyrics(ctx)
        credits_list = await self._asset_manager.get_credits(ctx)

        metadata = TrackMetadata(
            cover_path=cover,
            lyrics=lyrics,
            credits=credits_list,
        )

        playlist = None
        if job.definition.media_type == DownloadTypeEnum.playlist:
            playlist = PlaylistContext(
                download_path=job.definition.download_path,
                name=job.definition.name,
            )

        await self._queue.update_progress(
            ProgressUpdate(track_id=ctx.task.track_id, message=_("Writing tags"))
        )
        await self._finalizer.finalize(ctx, file_result, metadata, playlist)

    async def _notify_extensions_track_complete(
        self,
        job: DownloadJob,
        track_id: str,
        track_info: Any,
    ) -> None:
        """Notify all extensions about track completion."""
        event = TrackCompleteEvent(job=job, track_id=track_id, track_info=track_info)
        for ext in self._extensions:
            try:
                await ext.on_track_complete(event)
            except Exception:
                logger.warning(
                    "Extension on_track_complete failed for %s",
                    ext.__class__.__name__,
                )
