"""Process-wide download service for the WebUI.

Owns an immutable ``ServiceSnapshot`` of download state plus a serial worker
that runs at most one ``haberlea_core_download`` at a time. All UI state is
derived from snapshots pushed to subscribers, which makes reconnect/multi-tab
trivial: every client reads the same snapshot.

Design notes:
    - State mutation is funnelled through a single ``_apply_event`` coroutine
      guarded by an ``anyio.Lock``; all reducers are pure.
    - ``_snapshot`` reads are lock-free — reference assignment is atomic in
      CPython and the struct is frozen, so readers never see a torn object.
    - Subscribers are notified OUTSIDE the lock to avoid deadlocks when a
      subscriber synchronously re-enters subscribe/unsubscribe.
    - The ``utils.progress`` callback is registered exactly once at startup;
      the WebUI is mutually exclusive with the CLI Rich renderer, which is
      already the case today.
"""

from __future__ import annotations

import collections
import logging
import threading
import traceback
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import anyio
import msgspec
from nicegui import background_tasks

from haberlea.cli import resolve_urls_to_media
from haberlea.core import haberlea_core_download
from haberlea.downloader.contexts import DownloadRequest
from haberlea.plugins.base import ExtensionBase
from haberlea.utils.models import ModuleModes
from haberlea.utils.progress import (
    ProgressEvent,
    ProgressStatus,
    clear_all,
    set_callback,
)
from haberlea.utils.settings import settings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from haberlea.core.haberlea import Haberlea
    from haberlea.download_queue import DownloadJob, DownloadQueue
    from haberlea.downloader.results import DownloadSummary

logger = logging.getLogger(__name__)

# Maximum number of log lines kept in the snapshot.
_LOG_TAIL_LIMIT = 200


# ---------------------------------------------------------------------------
# Immutable snapshot and event structs
# ---------------------------------------------------------------------------


class TrackSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    """Immutable per-track progress view.

    Attributes:
        task_id: Unique track task identifier.
        job_id: Job identifier the track belongs to.
        name: Track display name.
        artist: Track artist.
        album: Album or playlist name.
        service: Source service (module name).
        status: "pending" | "downloading" | "completed" | "failed" | "skipped".
        progress: Progress ratio in [0.0, 1.0].
        message: Optional human-readable status text.
        quality: Audio quality tier — "hires" | "lossless" | "lossy" | "".
    """

    task_id: str
    job_id: str
    name: str = ""
    artist: str = ""
    album: str = ""
    service: str = ""
    status: str = "pending"
    progress: float = 0.0
    message: str = ""
    quality: str = ""


class JobSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    """Immutable per-job aggregated view.

    Attributes:
        job_id: Unique job identifier.
        original_url: Source URL submitted by the user.
        media_type: "track" | "album" | "playlist" | "artist".
        name: Album/playlist/track display name.
        artist: Main artist name.
        status: "pending" | "downloading" | "completed" | "partial" | "failed".
        total_tracks: Total track count for this job.
        completed: Count of completed tracks.
        failed: Count of failed tracks.
        skipped: Count of skipped tracks.
        progress: Progress ratio in [0.0, 1.0].
        track_ids: Ordered tuple of track task identifiers.
    """

    job_id: str
    original_url: str = ""
    media_type: str = ""
    name: str = ""
    artist: str = ""
    status: str = "pending"
    total_tracks: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    progress: float = 0.0
    track_ids: tuple[str, ...] = ()


class ServiceSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    """Immutable top-level service state.

    Attributes:
        jobs: All jobs in insertion order.
        tracks: Read-only mapping from task_id to TrackSnapshot.
        is_downloading: True when the worker holds an active batch.
        pending_batches: URL batches waiting for the worker.
        logs_tail: Last ``_LOG_TAIL_LIMIT`` log lines.
        revision: Monotonic counter bumped on every reducer output.
    """

    jobs: tuple[JobSnapshot, ...] = ()
    tracks: Mapping[str, TrackSnapshot] = msgspec.field(
        default_factory=lambda: MappingProxyType({})
    )
    is_downloading: bool = False
    pending_batches: tuple[tuple[str, ...], ...] = ()
    logs_tail: tuple[str, ...] = ()
    revision: int = 0


EMPTY_SNAPSHOT: ServiceSnapshot = ServiceSnapshot()


class TrackEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Progress update for a single track."""

    task_id: str
    job_id: str
    name: str = ""
    artist: str = ""
    album: str = ""
    service: str = ""
    status: str = "pending"
    progress: float = 0.0
    message: str = ""
    quality: str = ""


class QueueReadyEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Full jobs+tracks snapshot produced when the DownloadQueue is ready."""

    jobs: tuple[JobSnapshot, ...]
    tracks: Mapping[str, TrackSnapshot]


class JobFinishedEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Terminal counts for a job."""

    job_id: str
    status: str
    completed: int
    failed: int
    skipped: int


class LogEvent(msgspec.Struct, frozen=True, kw_only=True):
    """A single log line appended to the snapshot tail."""

    line: str


class BatchQueuedEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Batch of URLs appended to the pending queue."""

    urls: tuple[str, ...]


class BatchStartedEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Batch picked up by the worker; removed from pending_batches."""

    urls: tuple[str, ...]


class BatchFinishedEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Batch finished processing (either success or failure)."""


class ClearCompletedEvent(msgspec.Struct, frozen=True, kw_only=True):
    """Drop all terminal jobs and their tracks."""


# ---------------------------------------------------------------------------
# Pure reducers — no I/O, no globals, no mutation of inputs
# ---------------------------------------------------------------------------


def _bump(snapshot: ServiceSnapshot, **overrides: Any) -> ServiceSnapshot:
    """Return a new snapshot with ``revision`` incremented.

    Args:
        snapshot: The current snapshot.
        **overrides: Field overrides applied via ``msgspec.structs.replace``.

    Returns:
        A new ``ServiceSnapshot`` with revision bumped.
    """
    return msgspec.structs.replace(
        snapshot, revision=snapshot.revision + 1, **overrides
    )


def _freeze_tracks(tracks: dict[str, TrackSnapshot]) -> Mapping[str, TrackSnapshot]:
    """Wrap a fresh dict in a ``MappingProxyType`` for read-only exposure."""
    return MappingProxyType(tracks)


def classify_quality(lossless: bool, bit_depth: int) -> str:
    """Classify a track's audio quality into a display tier.

    Args:
        lossless: Whether the codec is lossless.
        bit_depth: Bit depth of the source (e.g., 16, 24).

    Returns:
        "hires" for lossless >= 24-bit, "lossless" for lossless < 24-bit,
        "lossy" for non-lossless codecs.
    """
    if lossless and bit_depth >= 24:
        return "hires"
    if lossless:
        return "lossless"
    return "lossy"


def _count_track_outcomes(
    track_ids: tuple[str, ...], tracks: dict[str, TrackSnapshot]
) -> tuple[int, int, int]:
    """Count completed/failed/skipped from the tracks dict.

    Args:
        track_ids: Ordered track IDs for the job.
        tracks: Mutable working dict of all tracks.

    Returns:
        Tuple of (completed, failed, skipped).
    """
    completed = 0
    failed = 0
    skipped = 0
    for tid in track_ids:
        track = tracks.get(tid)
        if track is None:
            continue
        if track.status == "completed":
            completed += 1
        elif track.status == "failed":
            failed += 1
        elif track.status == "skipped":
            skipped += 1
    return completed, failed, skipped


def _derive_job_status(
    current_status: str, total: int, finished: int, failed: int
) -> str:
    """Compute the new job status from counters.

    Args:
        current_status: Existing job status string.
        total: Total track count.
        finished: Completed + failed + skipped count.
        failed: Failed track count.

    Returns:
        Updated status string.
    """
    if total > 0 and finished >= total:
        if failed == total:
            return "failed"
        if failed > 0:
            return "partial"
        return "completed"
    if current_status == "pending":
        return "downloading"
    return current_status


def apply_track_event(s: ServiceSnapshot, e: TrackEvent) -> ServiceSnapshot:
    """Update or insert a track's progress and recompute its job's counters.

    If the track's task_id is unknown (a progress event may race ahead of
    ``QueueReadyEvent``) a minimal TrackSnapshot is inserted and the job
    counters are not recomputed until ``QueueReadyEvent`` merges the
    authoritative state.

    Args:
        s: The current snapshot.
        e: The track event.

    Returns:
        A new snapshot with the track and (when possible) its job updated.
    """
    existing = s.tracks.get(e.task_id)
    updated_track = TrackSnapshot(
        task_id=e.task_id,
        job_id=e.job_id,
        name=e.name or (existing.name if existing else ""),
        artist=e.artist or (existing.artist if existing else ""),
        album=e.album or (existing.album if existing else ""),
        service=e.service or (existing.service if existing else ""),
        status=e.status,
        progress=e.progress,
        message=e.message,
        quality=e.quality or (existing.quality if existing else ""),
    )
    new_tracks = dict(s.tracks)
    new_tracks[e.task_id] = updated_track

    job_index: int | None = None
    for i, j in enumerate(s.jobs):
        if j.job_id == e.job_id:
            job_index = i
            break

    if job_index is None:
        return _bump(s, tracks=_freeze_tracks(new_tracks))

    job = s.jobs[job_index]
    completed, failed, skipped = _count_track_outcomes(job.track_ids, new_tracks)
    total = job.total_tracks
    finished = completed + failed + skipped
    job_progress = finished / total if total > 0 else 0.0
    new_status = _derive_job_status(job.status, total, finished, failed)

    updated_job = msgspec.structs.replace(
        job,
        completed=completed,
        failed=failed,
        skipped=skipped,
        progress=job_progress,
        status=new_status,
    )
    new_jobs = tuple(
        updated_job if i == job_index else existing_job
        for i, existing_job in enumerate(s.jobs)
    )
    return _bump(s, jobs=new_jobs, tracks=_freeze_tracks(new_tracks))


def apply_queue_ready(s: ServiceSnapshot, e: QueueReadyEvent) -> ServiceSnapshot:
    """Merge the authoritative jobs/tracks produced when the queue is built.

    Existing jobs with the same ``job_id`` are replaced; new jobs are
    appended. Tracks are merged — events already applied for a track
    preserve their progress unless the authoritative snapshot has a
    non-default value.

    Args:
        s: The current snapshot.
        e: The queue-ready event.

    Returns:
        A new snapshot with merged jobs/tracks.
    """
    existing_by_id = {j.job_id: j for j in s.jobs}
    merged_jobs: list[JobSnapshot] = []
    seen: set[str] = set()

    for new_job in e.jobs:
        merged_jobs.append(new_job)
        seen.add(new_job.job_id)

    for old_job in s.jobs:
        if old_job.job_id not in seen:
            merged_jobs.append(old_job)

    # Preserve any existing job counters that already saw progress events.
    # The new_job has zero counters (built from the queue before downloads
    # start) so we overlay existing counts where present.
    final_jobs: list[JobSnapshot] = []
    for j in merged_jobs:
        prior = existing_by_id.get(j.job_id)
        if prior is not None and prior.completed + prior.failed + prior.skipped > 0:
            final_jobs.append(
                msgspec.structs.replace(
                    j,
                    completed=prior.completed,
                    failed=prior.failed,
                    skipped=prior.skipped,
                    progress=prior.progress,
                    status=prior.status,
                )
            )
        else:
            final_jobs.append(j)

    # Merge tracks: new event tracks overlay existing ones only when the
    # existing one has no progress signal yet.
    merged_tracks = dict(s.tracks)
    for tid, new_track in e.tracks.items():
        old = merged_tracks.get(tid)
        if old is None or old.status == "pending":
            merged_tracks[tid] = new_track

    return _bump(s, jobs=tuple(final_jobs), tracks=_freeze_tracks(merged_tracks))


def apply_job_finished(s: ServiceSnapshot, e: JobFinishedEvent) -> ServiceSnapshot:
    """Set terminal counts and status on a specific job.

    Args:
        s: The current snapshot.
        e: The job-finished event.

    Returns:
        A new snapshot with the job updated.
    """
    updated = False
    new_jobs: list[JobSnapshot] = []
    for job in s.jobs:
        if job.job_id == e.job_id:
            total = job.total_tracks
            finished = e.completed + e.failed + e.skipped
            progress = finished / total if total > 0 else 1.0
            new_jobs.append(
                msgspec.structs.replace(
                    job,
                    status=e.status,
                    completed=e.completed,
                    failed=e.failed,
                    skipped=e.skipped,
                    progress=progress,
                )
            )
            updated = True
        else:
            new_jobs.append(job)

    if not updated:
        return s
    return _bump(s, jobs=tuple(new_jobs))


def apply_log(s: ServiceSnapshot, e: LogEvent) -> ServiceSnapshot:
    """Append a log line, truncating to the configured tail length.

    Args:
        s: The current snapshot.
        e: The log event.

    Returns:
        A new snapshot with the log line appended.
    """
    new_tail = (*s.logs_tail, e.line)
    if len(new_tail) > _LOG_TAIL_LIMIT:
        new_tail = new_tail[-_LOG_TAIL_LIMIT:]
    return _bump(s, logs_tail=new_tail)


def apply_batch_queued(s: ServiceSnapshot, e: BatchQueuedEvent) -> ServiceSnapshot:
    """Append a URL batch to the pending queue.

    Args:
        s: The current snapshot.
        e: The batch-queued event.

    Returns:
        A new snapshot with the batch appended.
    """
    return _bump(s, pending_batches=(*s.pending_batches, e.urls))


def apply_batch_started(s: ServiceSnapshot, e: BatchStartedEvent) -> ServiceSnapshot:
    """Mark downloading and remove the matching batch from pending.

    Args:
        s: The current snapshot.
        e: The batch-started event.

    Returns:
        A new snapshot with is_downloading=True and the batch removed.
    """
    remaining: list[tuple[str, ...]] = []
    removed = False
    for batch in s.pending_batches:
        if not removed and batch == e.urls:
            removed = True
            continue
        remaining.append(batch)
    return _bump(s, is_downloading=True, pending_batches=tuple(remaining))


def apply_batch_finished(
    s: ServiceSnapshot,
    e: BatchFinishedEvent,  # noqa: ARG001 — unused by design
) -> ServiceSnapshot:
    """Mark the service idle when a batch finishes processing.

    Args:
        s: The current snapshot.
        e: The batch-finished event (no payload).

    Returns:
        A new snapshot with is_downloading=False.
    """
    return _bump(s, is_downloading=False)


def apply_clear_completed(
    s: ServiceSnapshot,
    e: ClearCompletedEvent,  # noqa: ARG001 — unused by design
) -> ServiceSnapshot:
    """Drop jobs in a terminal status and their tracks.

    Args:
        s: The current snapshot.
        e: The clear-completed event (no payload).

    Returns:
        A new snapshot with terminal jobs/tracks removed.
    """
    terminal = {"completed", "partial", "failed"}
    kept_jobs: list[JobSnapshot] = []
    dropped_track_ids: set[str] = set()
    for job in s.jobs:
        if job.status in terminal:
            dropped_track_ids.update(job.track_ids)
        else:
            kept_jobs.append(job)
    new_tracks = {tid: t for tid, t in s.tracks.items() if tid not in dropped_track_ids}
    return _bump(s, jobs=tuple(kept_jobs), tracks=_freeze_tracks(new_tracks))


def reduce(s: ServiceSnapshot, event: object) -> ServiceSnapshot:
    """Dispatch an event to its reducer.

    Args:
        s: The current snapshot.
        event: One of the event structs defined in this module.

    Returns:
        The new snapshot, or the input snapshot if the event is unknown.
    """
    if isinstance(event, TrackEvent):
        return apply_track_event(s, event)
    if isinstance(event, QueueReadyEvent):
        return apply_queue_ready(s, event)
    if isinstance(event, JobFinishedEvent):
        return apply_job_finished(s, event)
    if isinstance(event, LogEvent):
        return apply_log(s, event)
    if isinstance(event, BatchQueuedEvent):
        return apply_batch_queued(s, event)
    if isinstance(event, BatchStartedEvent):
        return apply_batch_started(s, event)
    if isinstance(event, BatchFinishedEvent):
        return apply_batch_finished(s, event)
    if isinstance(event, ClearCompletedEvent):
        return apply_clear_completed(s, event)
    logger.warning("Unknown event type: %r", type(event).__name__)
    return s


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def build_job_snapshot_from_job(job: DownloadJob) -> JobSnapshot:
    """Build a JobSnapshot from a DownloadJob (read-only consumer).

    Args:
        job: The source DownloadJob.

    Returns:
        A new ``JobSnapshot`` populated from ``job``.
    """
    progress = job.progress.get_progress()
    definition = job.definition
    return JobSnapshot(
        job_id=job.job_id,
        original_url=definition.original_url,
        media_type=definition.media_type.value,
        name=definition.name,
        artist=definition.artist,
        status=job.status.value,
        total_tracks=progress.total,
        completed=progress.completed,
        failed=progress.failed,
        skipped=progress.skipped,
        progress=progress.progress,
        track_ids=tuple(job.progress.track_ids),
    )


def build_queue_ready_from_queue(queue: DownloadQueue) -> QueueReadyEvent:
    """Build a QueueReadyEvent by reading the queue's current state.

    The queue is treated as a read-only source; no mutation occurs.

    Args:
        queue: The active DownloadQueue.

    Returns:
        A QueueReadyEvent with all jobs and per-track placeholders.
    """
    source_jobs = queue.get_all_jobs()
    jobs = tuple(build_job_snapshot_from_job(j) for j in source_jobs)
    infos_by_job = {j.job_id: j.progress.track_infos for j in source_jobs}
    tracks: dict[str, TrackSnapshot] = {}
    for job_snapshot in jobs:
        infos = infos_by_job.get(job_snapshot.job_id, {})
        for track_id in job_snapshot.track_ids:
            info = infos.get(track_id)
            quality = (
                classify_quality(info.codec.lossless, info.bit_depth)
                if info is not None
                else ""
            )
            tracks[track_id] = TrackSnapshot(
                task_id=track_id,
                job_id=job_snapshot.job_id,
                album=job_snapshot.name,
                artist=job_snapshot.artist,
                quality=quality,
            )
    return QueueReadyEvent(jobs=jobs, tracks=_freeze_tracks(tracks))


# ---------------------------------------------------------------------------
# Subscriber model
# ---------------------------------------------------------------------------


class Subscriber(msgspec.Struct, frozen=True, kw_only=True):
    """A registered subscriber.

    Attributes:
        sub_id: Opaque subscriber identifier allocated by ``subscribe``.
        notify: Synchronous callback receiving the latest snapshot. The
            callback must not raise and must not block.
    """

    sub_id: int
    notify: Callable[[ServiceSnapshot], None]


# ---------------------------------------------------------------------------
# Module-level mutable state — the single mutation boundary
# ---------------------------------------------------------------------------

_state_lock: anyio.Lock = anyio.Lock()
_snapshot: ServiceSnapshot = EMPTY_SNAPSHOT
_pending: collections.deque[tuple[str, ...]] = collections.deque()
_worker_running: bool = False
_current_queue: DownloadQueue | None = None
_haberlea: Haberlea | None = None
_initialized: bool = False

# Plain threading.Lock for the subscriber registry because subscribe/
# unsubscribe are called from NiceGUI sync contexts (Client.on_disconnect).
_sub_lock: threading.Lock = threading.Lock()
_subscribers: dict[int, Subscriber] = {}
_next_sub_id: int = 0


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def get_snapshot() -> ServiceSnapshot:
    """Return the current snapshot.

    Lock-free: reference assignment is atomic in CPython and the struct is
    frozen, so readers never observe a torn object.

    Returns:
        The current ``ServiceSnapshot``.
    """
    return _snapshot


# ---------------------------------------------------------------------------
# Subscription API
# ---------------------------------------------------------------------------


def subscribe(notify: Callable[[ServiceSnapshot], None]) -> int:
    """Register a subscriber and return its id.

    The caller is responsible for painting the current snapshot themselves;
    this function does not invoke ``notify`` on registration.

    Args:
        notify: Synchronous callback receiving the latest snapshot. Must not
            raise; exceptions are logged and swallowed in ``_notify_all``.

    Returns:
        The allocated subscriber id, used for ``unsubscribe``.
    """
    global _next_sub_id
    with _sub_lock:
        _next_sub_id += 1
        sub_id = _next_sub_id
        _subscribers[sub_id] = Subscriber(sub_id=sub_id, notify=notify)
    return sub_id


def unsubscribe(sub_id: int) -> None:
    """Remove a subscriber; idempotent.

    Args:
        sub_id: Identifier previously returned by ``subscribe``.
    """
    with _sub_lock:
        _subscribers.pop(sub_id, None)


def _notify_all(snapshot: ServiceSnapshot, subs: tuple[Subscriber, ...]) -> None:
    """Deliver a snapshot to every subscriber, swallowing per-sub exceptions.

    Args:
        snapshot: The snapshot to deliver.
        subs: A pre-captured tuple of subscribers (captured under lock).
    """
    for sub in subs:
        try:
            sub.notify(snapshot)
        except Exception:  # noqa: BLE001 — per-subscriber isolation
            logger.exception("Subscriber %d notify raised; continuing", sub.sub_id)


# ---------------------------------------------------------------------------
# Single writer + dispatch
# ---------------------------------------------------------------------------


async def _apply_event(event: object) -> None:
    """Apply an event to the snapshot under the lock, then notify outside.

    Args:
        event: One of the event structs.
    """
    global _snapshot
    async with _state_lock:
        _snapshot = reduce(_snapshot, event)
        new = _snapshot
    with _sub_lock:
        subs = tuple(_subscribers.values())
    _notify_all(new, subs)


# ---------------------------------------------------------------------------
# Command API
# ---------------------------------------------------------------------------


async def submit_urls(urls: tuple[str, ...]) -> None:
    """Append a URL batch to the pending queue and ensure the worker runs.

    Args:
        urls: Raw URL strings; non-http entries are filtered.
    """
    cleaned = tuple(u for u in urls if u.startswith("http"))
    if not cleaned:
        return

    async with _state_lock:
        _pending.append(cleaned)
        should_start = not _worker_running

    await _apply_event(BatchQueuedEvent(urls=cleaned))

    if should_start:
        background_tasks.create(_run_worker(), name="haberlea-download-worker")


async def clear_completed() -> None:
    """Drop all jobs in a terminal status and their tracks."""
    await _apply_event(ClearCompletedEvent())


# ---------------------------------------------------------------------------
# Serial worker
# ---------------------------------------------------------------------------


async def _run_worker() -> None:
    """Drain ``_pending`` sequentially; at most one instance runs at a time."""
    global _worker_running, _current_queue

    async with _state_lock:
        if _worker_running:
            return
        _worker_running = True

    try:
        while True:
            async with _state_lock:
                if not _pending:
                    break
                batch = _pending.popleft()

            await _apply_event(BatchStartedEvent(urls=batch))
            await _apply_event(
                LogEvent(line=f"Starting download of {len(batch)} URL(s)")
            )

            try:
                await _invoke_core_download(batch)
            except Exception as exc:  # noqa: BLE001 — boundary
                logger.exception("Download batch failed")
                await _apply_event(LogEvent(line=f"Download error: {exc}"))
                await _apply_event(LogEvent(line=f"Details: {traceback.format_exc()}"))
            finally:
                _current_queue = None
                await _apply_event(BatchFinishedEvent())
    finally:
        async with _state_lock:
            _worker_running = False


async def _invoke_core_download(batch: tuple[str, ...]) -> None:
    """Resolve URLs and run ``haberlea_core_download`` for the batch.

    Args:
        batch: URL strings that have already passed basic filtering.
    """
    if _haberlea is None:
        await _apply_event(LogEvent(line="Haberlea not initialized; aborting"))
        return

    ExtensionBase.set_log_callback(_on_extension_log)
    try:
        media_to_download = await resolve_urls_to_media(_haberlea, batch)
        for service, media_list in media_to_download.items():
            await _apply_event(
                LogEvent(line=f"Service {service}: {len(media_list)} item(s)")
            )

        output_path = Path(settings.global_settings.runtime.download_path)

        tpm: dict[ModuleModes, str] = {}
        for mode_name in ("covers", "lyrics", "credits"):
            mode_value: str | None = getattr(
                settings.global_settings.module_defaults, mode_name, "default"
            )
            if mode_value and mode_value != "default":
                tpm[ModuleModes[mode_name]] = mode_value

        request = DownloadRequest(
            session=_haberlea,
            media_to_download=media_to_download,
            third_party_modules=tpm,
            separate_download_module="default",
            output_path=output_path,
            on_queue_ready=_on_queue_ready,
        )
        summary: DownloadSummary = await haberlea_core_download(request)

        await _apply_event(
            LogEvent(
                line=(
                    f"Done: {len(summary.completed)} success, "
                    f"{len(summary.failed)} failed"
                )
            )
        )
    finally:
        ExtensionBase.set_log_callback(None)
        clear_all()


def _on_queue_ready(queue: DownloadQueue) -> None:
    """Sync callback from the orchestrator after queueing completes.

    Captures the queue reference for the progress callback and schedules a
    ``QueueReadyEvent`` to publish the authoritative job/track list.

    Args:
        queue: The populated ``DownloadQueue``.
    """
    global _current_queue
    _current_queue = queue
    event = build_queue_ready_from_queue(queue)
    background_tasks.create(_apply_event(event), name="haberlea-queue-ready")


def _on_extension_log(message: str) -> None:
    """Route extension logs into the snapshot log tail.

    Args:
        message: Log line from an extension.
    """
    background_tasks.create(
        _apply_event(LogEvent(line=message)), name="haberlea-extension-log"
    )


# ---------------------------------------------------------------------------
# Global progress callback
# ---------------------------------------------------------------------------


def _lookup_quality(queue: DownloadQueue, job_id: str, task_id: str) -> str:
    """Look up the classified quality for a track from the queue.

    Args:
        queue: Active download queue.
        job_id: Owning job identifier.
        task_id: Track task identifier.

    Returns:
        Classified quality tier, or "" if the TrackInfo is not yet available.
    """
    job = queue.get_job(job_id)
    if job is None:
        return ""
    info = job.progress.track_infos.get(task_id)
    if info is None:
        return ""
    return classify_quality(info.codec.lossless, info.bit_depth)


def _on_progress_event(event: ProgressEvent) -> None:
    """Translate a ``utils.progress`` event into a ``TrackEvent``.

    Called synchronously from the download coroutine. Must not block.

    Args:
        event: The raw progress event.
    """
    queue = _current_queue
    if queue is None:
        return
    task = queue.get_track_task(event.task_id)
    if task is None:
        return

    status_str = (
        event.status.value
        if isinstance(event.status, ProgressStatus)
        else str(event.status)
    )
    track_event = TrackEvent(
        task_id=event.task_id,
        job_id=task.job_id,
        name=event.name,
        artist=event.artist,
        album=event.album,
        service=event.service,
        status=status_str,
        progress=event.progress,
        message=event.message,
        quality=_lookup_quality(queue, task.job_id, event.task_id),
    )
    background_tasks.create(_apply_event(track_event), name="haberlea-progress")


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------


def init_download_service(haberlea: Haberlea) -> None:
    """Register the global progress callback and cache the Haberlea instance.

    Must be called exactly once, after ``init_haberlea``.

    Args:
        haberlea: The initialized Haberlea instance.
    """
    global _haberlea, _initialized
    if _initialized:
        logger.warning("init_download_service called more than once; ignoring")
        return
    _haberlea = haberlea
    _initialized = True
    set_callback(_on_progress_event)
    logger.info("Download service initialized")
