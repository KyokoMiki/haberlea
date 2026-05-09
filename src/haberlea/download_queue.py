"""Global download queue system with job-based organization.

This module provides a unified queue system that organizes downloads by job
(original URL/request) while executing track downloads concurrently.
Jobs represent the original download request (album, playlist, artist, track),
while tracks are the actual execution units.
"""

import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from enum import Enum
from pathlib import Path
from typing import Any

import anyio
import msgspec

from .downloader.contexts import JobDefinition, ProgressUpdate
from .downloader.results import DownloadSummary, FailedTrack
from .plugins.base import ModuleBase
from .utils.models import (
    CodecOptions,
    MediaKindEnum,
    QualityEnum,
    TrackInfo,
    VideoInfo,
)
from .utils.progress import (
    ProgressStatus,
    create_task,
    update,
)

logger = logging.getLogger(__name__)


class JobStatus(Enum):
    """Status of a download job."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    PARTIAL = "partial"  # Some tracks failed
    FAILED = "failed"


class JobProgress(msgspec.Struct, frozen=True):
    """Progress statistics for a download job.

    Attributes:
        completed: Number of successfully completed tracks.
        failed: Number of failed tracks.
        skipped: Number of skipped tracks.
        total: Total number of tracks in the job.
    """

    completed: int
    failed: int
    skipped: int
    total: int

    @property
    def finished(self) -> int:
        """Total number of finished tracks (completed + failed + skipped)."""
        return self.completed + self.failed + self.skipped

    @property
    def progress(self) -> float:
        """Progress ratio (0.0 to 1.0)."""
        if self.total == 0:
            return 0.0
        return self.finished / self.total

    @property
    def is_finished(self) -> bool:
        """Whether all tracks have finished."""
        return self.finished >= self.total and self.total > 0


class QualityConfig(msgspec.Struct, frozen=True):
    """Immutable quality configuration for the download queue.

    Attributes:
        quality_tier: Desired quality tier.
        codec_options: Codec preference options.
    """

    quality_tier: QualityEnum
    codec_options: CodecOptions


class TrackProgressMap:
    """Tracks per-track outcomes for a single job.

    Centralises the three outcome sets and track_infos so DownloadJob
    itself stays at ≤3 fields.
    """

    def __init__(self) -> None:
        self.track_ids: list[str] = []
        self.track_infos: dict[str, TrackInfo] = {}
        self.video_infos: dict[str, VideoInfo] = {}
        self._outcomes: dict[str, ProgressStatus] = {}

    # ------------------------------------------------------------------
    # Mutation helpers (called only by DownloadQueue under lock)
    # ------------------------------------------------------------------

    def add_track(self, track_id: str) -> None:
        """Register a new track in this job."""
        self.track_ids.append(track_id)

    def record_outcome(self, track_id: str, status: ProgressStatus) -> None:
        """Record the final outcome for a track."""
        self._outcomes[track_id] = status

    def store_info(self, track_id: str, info: TrackInfo) -> None:
        """Store fetched TrackInfo for extension callbacks."""
        self.track_infos[track_id] = info

    def store_video_info(self, video_id: str, info: VideoInfo) -> None:
        """Store fetched VideoInfo for extension callbacks."""
        self.video_infos[video_id] = info

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def completed_tracks(self) -> set[str]:
        """Set of successfully completed track IDs."""
        return {
            tid for tid, s in self._outcomes.items() if s == ProgressStatus.COMPLETED
        }

    @property
    def failed_tracks(self) -> set[str]:
        """Set of failed track IDs."""
        return {tid for tid, s in self._outcomes.items() if s == ProgressStatus.FAILED}

    @property
    def skipped_tracks(self) -> set[str]:
        """Set of skipped track IDs."""
        return {tid for tid, s in self._outcomes.items() if s == ProgressStatus.SKIPPED}

    @property
    def is_finished(self) -> bool:
        """Whether all registered tracks have a recorded outcome."""
        return len(self._outcomes) >= len(self.track_ids) > 0

    def get_progress(self) -> JobProgress:
        """Return a frozen progress snapshot."""
        completed = sum(
            1 for s in self._outcomes.values() if s == ProgressStatus.COMPLETED
        )
        failed = sum(1 for s in self._outcomes.values() if s == ProgressStatus.FAILED)
        skipped = sum(1 for s in self._outcomes.values() if s == ProgressStatus.SKIPPED)
        return JobProgress(
            completed=completed,
            failed=failed,
            skipped=skipped,
            total=len(self.track_ids),
        )

    def get_results(self) -> DownloadSummary:
        """Return a DownloadSummary for this job."""
        completed = [
            tid for tid, s in self._outcomes.items() if s == ProgressStatus.COMPLETED
        ]
        failed = [
            FailedTrack(track_id=tid, reason="Download failed")
            for tid, s in self._outcomes.items()
            if s == ProgressStatus.FAILED
        ]
        return DownloadSummary(completed=completed, failed=failed)


class DownloadJob:
    """Mutable execution state for one download job.

    Only DownloadQueue may mutate this object (guarded by _job_lock).
    """

    def __init__(self, definition: JobDefinition, job_id: str) -> None:
        self.definition = definition
        self.job_id = job_id
        self.created_at: float = time.time()
        self.status: JobStatus = JobStatus.PENDING
        self.progress = TrackProgressMap()

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def total_tracks(self) -> int:
        """Total number of tracks in this job."""
        return len(self.progress.track_ids)

    @property
    def finished_tracks(self) -> int:
        """Number of tracks that have finished."""
        return (
            len(self.progress.completed_tracks)
            + len(self.progress.failed_tracks)
            + len(self.progress.skipped_tracks)
        )

    @property
    def is_finished(self) -> bool:
        """Whether all tracks in this job have finished."""
        return self.progress.is_finished

    @property
    def job_progress(self) -> float:
        """Job progress as a ratio (0.0 to 1.0)."""
        if self.total_tracks == 0:
            return 0.0
        return self.finished_tracks / self.total_tracks

    @property
    def has_successful_downloads(self) -> bool:
        """Whether any tracks were successfully downloaded."""
        return len(self.progress.completed_tracks) > 0


class TrackTask(msgspec.Struct):
    """A single download task (audio track or music video).

    Music videos reuse this struct with ``media_kind=MediaKindEnum.video``
    so the queue, orchestrator, and webui can stay media-kind agnostic.
    Video tasks ignore the audio-specific ``track_index`` / ``total_tracks``
    fields.

    Attributes:
        track_id: The track or video identifier.
        job_id: The job this task belongs to.
        module_name: The module to use for downloading.
        module: The loaded module instance.
        download_path: Path to save the file.
        track_index: Track number in album/playlist (audio only).
        total_tracks: Total tracks in album/playlist (audio only).
        main_artist: Main artist name for filtering / display.
        track_data: Pre-fetched payload (audio TrackInfo data or video
            metadata, interpreted by the module).
        account_index: Account index used for this task.
        media_kind: Audio or video execution path.
    """

    track_id: str
    job_id: str
    module_name: str
    module: ModuleBase
    download_path: Path = msgspec.field(default_factory=Path)
    track_index: int = 0
    total_tracks: int = 0
    main_artist: str = ""
    track_data: dict[str, Any] | None = None
    account_index: int = 0
    media_kind: MediaKindEnum = MediaKindEnum.audio


# Type alias for job completion callback
JobCompletionCallback = Callable[[DownloadJob], Coroutine[Any, Any, None]]


class QueueState:
    """Mutable runtime state of the download queue.

    Extracted so DownloadQueue itself has ≤4 fields.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, DownloadJob] = {}
        self.track_tasks: dict[str, TrackTask] = {}
        self.job_lock = anyio.Lock()


class DownloadQueue:
    """Global download queue with job-based organization.

    Four fields: semaphore (dep) + _on_job_complete (dep) + _config + _state.
    """

    def __init__(
        self,
        max_concurrent: int,
        quality_tier: QualityEnum,
        codec_options: CodecOptions,
        on_job_complete: JobCompletionCallback | None = None,
        module_limits: dict[str, int] | None = None,
    ) -> None:
        """Initializes the download queue.

        Args:
            max_concurrent: Maximum concurrent downloads.
            quality_tier: Quality tier for downloads.
            codec_options: Codec options for downloads.
            on_job_complete: Optional callback when a job completes.
            module_limits: Optional per-module concurrency caps. Maps module
                name to the maximum number of tracks that may be downloading
                simultaneously for that module. Modules absent from this dict
                are only bounded by ``max_concurrent``.
        """
        self.limiter = anyio.CapacityLimiter(max_concurrent)
        self._on_job_complete = on_job_complete
        self._config = QualityConfig(
            quality_tier=quality_tier, codec_options=codec_options
        )
        self._state = QueueState()
        self._module_limits: dict[str, int] = dict(module_limits or {})
        self._module_limiters: dict[str, anyio.CapacityLimiter] = {}

    def get_module_limiter(self, module_name: str) -> anyio.CapacityLimiter | None:
        """Returns the per-module concurrency limiter, if any.

        Lazily instantiates a ``CapacityLimiter`` on first access so that
        tasks for the same module share a single limiter.

        Args:
            module_name: The module name.

        Returns:
            A ``CapacityLimiter`` bounding concurrent downloads for this
            module, or ``None`` if the module has no declared cap.
        """
        limit = self._module_limits.get(module_name)
        if limit is None or limit <= 0:
            return None
        limiter = self._module_limiters.get(module_name)
        if limiter is None:
            limiter = anyio.CapacityLimiter(limit)
            self._module_limiters[module_name] = limiter
        return limiter

    def _generate_job_id(self, original_url: str) -> str:
        """Generates a unique job ID."""
        return str(uuid.uuid4())

    async def create_job(self, definition: JobDefinition) -> str:
        """Creates a new download job from an immutable definition.

        Args:
            definition: Immutable job definition containing all metadata.

        Returns:
            The generated job ID.
        """
        job_id = self._generate_job_id(definition.original_url)
        job = DownloadJob(definition=definition, job_id=job_id)
        async with self._state.job_lock:
            self._state.jobs[job_id] = job
        logger.debug("Created job %s for %s", job_id, definition.original_url)
        return job_id

    async def add_track(self, job_id: str, task: TrackTask) -> None:
        """Adds a track task to a job.

        Args:
            job_id: The job ID to add the track to.
            task: The track task to add.

        Raises:
            KeyError: If the job ID doesn't exist.
        """
        async with self._state.job_lock:
            if job_id not in self._state.jobs:
                raise KeyError(f"Job {job_id} not found")

            job = self._state.jobs[job_id]
            job.progress.add_track(task.track_id)
            self._state.track_tasks[task.track_id] = task
            job_name = job.definition.name

        await create_task(
            task_id=task.track_id,
            service=task.module_name,
            album=job_name,
            artist=task.main_artist,
        )

    def get_job(self, job_id: str) -> DownloadJob | None:
        """Gets a job by ID.

        Args:
            job_id: The job ID.

        Returns:
            A DownloadJob or None if not found.
        """
        return self._state.jobs.get(job_id)

    def get_track_task(self, track_id: str) -> TrackTask | None:
        """Gets a track or video task by ID."""
        return self._state.track_tasks.get(track_id)

    def get_all_jobs(self) -> list[DownloadJob]:
        """Gets all jobs in the queue."""
        return list(self._state.jobs.values())

    def get_all_track_tasks(self) -> list[TrackTask]:
        """Gets all tasks in the queue (audio + video)."""
        return list(self._state.track_tasks.values())

    @property
    def job_count(self) -> int:
        """Returns number of jobs in the queue."""
        return len(self._state.jobs)

    @property
    def track_count(self) -> int:
        """Returns number of tasks in the queue (audio + video)."""
        return len(self._state.track_tasks)

    async def mark_track_complete(
        self,
        track_id: str,
        status: ProgressStatus = ProgressStatus.COMPLETED,
    ) -> None:
        """Marks a task as complete and updates job status.

        Args:
            track_id: The task identifier (audio track or video).
            status: The completion status (COMPLETED, FAILED, or SKIPPED).
        """
        task = self._state.track_tasks.get(track_id)
        if not task:
            logger.warning("Track %s not found in queue", track_id)
            return

        job = self._state.jobs.get(task.job_id)
        if not job:
            logger.warning("Job %s not found for track %s", task.job_id, track_id)
            return

        async with self._state.job_lock:
            job.progress.record_outcome(track_id, status)

            if job.progress.is_finished:
                if job.progress.failed_tracks:
                    job.status = JobStatus.PARTIAL
                else:
                    job.status = JobStatus.COMPLETED

                p = job.progress.get_progress()
                logger.info(
                    "Job %s finished: %d completed, %d failed, %d skipped",
                    job.job_id,
                    p.completed,
                    p.failed,
                    p.skipped,
                )

                if self._on_job_complete:
                    await self._on_job_complete(job)

    async def update_progress(self, progress_update: ProgressUpdate) -> None:
        """Updates progress for a track.

        Args:
            progress_update: Progress update payload with all fields.
        """
        current: int | None = None
        total: int | None = None

        if progress_update.file_size is not None:
            total = progress_update.file_size
        if progress_update.downloaded_size is not None:
            current = progress_update.downloaded_size
        elif progress_update.progress is not None and progress_update.file_size:
            current = int(progress_update.progress * progress_update.file_size)

        await update(
            task_id=progress_update.track_id,
            status=progress_update.status,
            name=progress_update.name,
            artist=progress_update.artist,
            album=progress_update.album,
            current=current,
            total=total,
            message=progress_update.message,
        )

        # Update job status to DOWNLOADING when first task starts
        async with self._state.job_lock:
            task = self._state.track_tasks.get(progress_update.track_id)
            if task and progress_update.status == ProgressStatus.DOWNLOADING:
                job = self._state.jobs.get(task.job_id)
                if job and job.status == JobStatus.PENDING:
                    job.status = JobStatus.DOWNLOADING

    def get_job_progress(self, job_id: str) -> JobProgress:
        """Gets progress for a job.

        Args:
            job_id: The job ID.

        Returns:
            JobProgress with completed, failed, skipped, and total counts.
        """
        job = self._state.jobs.get(job_id)
        if not job:
            return JobProgress(completed=0, failed=0, skipped=0, total=0)
        return job.progress.get_progress()

    def get_results(self) -> DownloadSummary:
        """Returns download results for all tracks.

        Returns:
            DownloadSummary with completed track IDs and failed track details.
        """
        summaries = [j.progress.get_results() for j in self._state.jobs.values()]
        return DownloadSummary(
            completed=[tid for s in summaries for tid in s.completed],
            failed=[f for s in summaries for f in s.failed],
        )

    async def clear(self) -> None:
        """Clears all jobs and tasks from the queue."""
        async with self._state.job_lock:
            self._state.jobs.clear()
            self._state.track_tasks.clear()

    async def remove_job(self, job_id: str) -> bool:
        """Removes a job and its tracks from the queue.

        Args:
            job_id: The job ID to remove.

        Returns:
            True if the job was removed, False if not found.
        """
        async with self._state.job_lock:
            job = self._state.jobs.pop(job_id, None)
            if not job:
                return False

            for track_id in job.progress.track_ids:
                self._state.track_tasks.pop(track_id, None)

            return True
