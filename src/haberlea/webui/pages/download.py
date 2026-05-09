"""Download page for Haberlea WebUI.

The page is a thin view over ``download_service``: it subscribes to the
service on render and receives snapshot pushes. All business state lives in
the service so that browser refreshes and multiple tabs paint from the same
source of truth.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from nicegui import background_tasks, ui

from haberlea.i18n import _, ngettext
from haberlea.utils.settings import SETTINGS_PATH, save_settings, settings
from haberlea.webui import download_service
from haberlea.webui.state import add_log

if TYPE_CHECKING:
    from nicegui import Client

    from haberlea.webui.download_service import JobSnapshot, ServiceSnapshot

# Maximum number of tracks to display initially before the "Show all" button.
MAX_VISIBLE_TRACKS = 50


class DownloadPage:
    """Download page — pure view over the module-level download service."""

    def __init__(self) -> None:
        """Initialize download page UI references."""
        self.url_input: ui.textarea | None = None
        self.download_log: ui.log | None = None
        self.start_button: ui.button | None = None

        self._client: Client | None = None
        self._sub_id: int | None = None

        # Coalescing state for snapshot push -> UI update.
        self._latest: ServiceSnapshot | None = None
        self._scheduled: bool = False

        # Log tail cursor so we only push new lines to the ui.log widget.
        self._log_cursor: int = 0

        # Per-client, view-only: which job expansions are "show all" tracks.
        self._job_show_all: dict[str, bool] = {}
        # Preserve which expansions are open across refreshes.
        self._job_expanded: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> None:
        """Render the download page and subscribe to the service."""
        self._client = ui.context.client

        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
            ui.label(_("Music Download")).classes("text-2xl font-bold")
            self._render_url_input()
            self._render_quick_options()
            self._render_queue_card()
            self._render_log_card()

        # Initial paint from current snapshot.
        self._rerender(download_service.get_snapshot())

        # Subscribe for future pushes.
        self._sub_id = download_service.subscribe(self._on_snapshot)
        self._client.on_disconnect(self._teardown)

    def _render_url_input(self) -> None:
        """Render the URL input section."""
        with ui.card().classes("w-full"):
            ui.label(_("Enter Download URL")).classes("text-lg font-semibold mb-2")
            with ui.row().classes("w-full gap-2 items-end"):
                self.url_input = (
                    ui.textarea(
                        label=_("URL (one per line)"), placeholder="https://..."
                    )
                    .classes("flex-grow")
                    .props("rows=3")
                )
                self.start_button = ui.button(
                    _("Start Download"),
                    icon="download",
                    on_click=self._start_download,
                ).props("color=primary")

    def _render_quick_options(self) -> None:
        """Render quick option toggles."""
        with ui.card().classes("w-full"):
            ui.label(_("Quick Options")).classes("text-lg font-semibold mb-2")
            with ui.row().classes("gap-4 flex-wrap items-center"):
                ui.select(
                    label=_("Download Quality"),
                    options=["minimum", "low", "medium", "high", "lossless", "hifi"],
                    value=settings.global_settings.quality.tier,
                    on_change=self._on_download_quality_change,
                ).classes("w-40")

                ui.checkbox(
                    _("Dry Run (Collect info only, no download)"),
                    value=settings.global_settings.download_behavior.dry_run,
                    on_change=self._on_dry_run_change,
                )

                archiver_settings = settings.extensions.get("post_download", {}).get(
                    "archiver"
                )
                if archiver_settings is not None:
                    ui.checkbox(
                        _("Archiver"),
                        value=archiver_settings.get("enabled", True),
                        on_change=self._on_archiver_change,
                    )

                gofile_settings = settings.extensions.get("post_download", {}).get(
                    "gofile_uploader"
                )
                if gofile_settings is not None:
                    ui.checkbox(
                        _("Gofile Uploader"),
                        value=gofile_settings.get("enabled", True),
                        on_change=self._on_gofile_change,
                    )

    def _render_queue_card(self) -> None:
        """Render the download queue container."""
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full justify-between items-center mb-2"):
                ui.label(_("Download Queue")).classes("text-lg font-semibold")
                ui.button(
                    _("Clear"), icon="delete_sweep", on_click=self._clear_queue
                ).props("flat dense")
            self._render_queue(download_service.get_snapshot())

    def _render_log_card(self) -> None:
        """Render the log card and prime it from the current snapshot."""
        with ui.card().classes("w-full"):
            ui.label(_("Download Log")).classes("text-lg font-semibold mb-2")
            self.download_log = ui.log(max_lines=100).classes("w-full h-64")
            snapshot = download_service.get_snapshot()
            for line in snapshot.logs_tail:
                self.download_log.push(line)
            self._log_cursor = len(snapshot.logs_tail)

    # ------------------------------------------------------------------
    # Settings callbacks (unchanged semantics)
    # ------------------------------------------------------------------

    def _on_dry_run_change(self, e: Any) -> None:
        """Handle the dry-run checkbox change and persist settings.

        Args:
            e: The change event carrying the new value.
        """
        settings.global_settings.download_behavior.dry_run = e.value
        save_settings(SETTINGS_PATH, settings.current)
        ui.notify(
            f"{_('Dry Run')} {_('enabled') if e.value else _('disabled')}", type="info"
        )

    def _on_download_quality_change(self, e: Any) -> None:
        """Handle the download-quality select change and persist settings.

        Args:
            e: The change event carrying the new value.
        """
        settings.global_settings.quality.tier = e.value
        save_settings(SETTINGS_PATH, settings.current)
        ui.notify(
            f"{_('Download Quality')}: {e.value}",
            type="info",
        )

    def _on_extension_toggle(self, key: str, label: str, e: Any) -> None:
        """Handle an extension toggle and persist settings.

        Args:
            key: Extension key under ``extensions.post_download``.
            label: Display label for the notification.
            e: The change event carrying the new value.
        """
        ext_settings = settings.extensions.get("post_download", {}).get(key)
        if ext_settings is not None:
            ext_settings["enabled"] = e.value
            save_settings(SETTINGS_PATH, settings.current)
            ui.notify(
                f"{_(label)} {_('enabled') if e.value else _('disabled')}",
                type="info",
            )

    def _on_archiver_change(self, e: Any) -> None:
        """Handle archiver checkbox change."""
        self._on_extension_toggle("archiver", "Archiver", e)

    def _on_gofile_change(self, e: Any) -> None:
        """Handle gofile uploader checkbox change."""
        self._on_extension_toggle("gofile_uploader", "Gofile Uploader", e)

    # ------------------------------------------------------------------
    # Snapshot subscription
    # ------------------------------------------------------------------

    def _on_snapshot(self, snapshot: ServiceSnapshot) -> None:
        """Receive a snapshot from the service and schedule a rerender.

        Called from the service/worker coroutine context — not this client's
        UI context. Coalesces rapid updates so only the latest snapshot is
        painted when the timer fires.

        Args:
            snapshot: The latest service snapshot.
        """
        if self._client is None:
            return
        self._latest = snapshot
        if self._scheduled:
            return
        self._scheduled = True
        client = self._client
        with client:
            background_tasks.create(
                self._rerender_latest(), name="haberlea-download-rerender"
            )

    async def _rerender_latest(self) -> None:
        """Drain the latest snapshot and rerender."""
        try:
            snapshot = self._latest
            self._latest = None
            if snapshot is None:
                return
            self._rerender(snapshot)
        finally:
            self._scheduled = False
            # If a newer snapshot arrived while rendering, process it.
            if self._latest is not None:
                self._on_snapshot(self._latest)

    def _rerender(self, snapshot: ServiceSnapshot) -> None:
        """Apply a snapshot to the UI widgets.

        Args:
            snapshot: The snapshot to render.
        """
        with contextlib.suppress(RuntimeError):
            if self.start_button is not None:
                if snapshot.is_downloading:
                    self.start_button.props("color=grey")
                    self.start_button.set_text(_("Downloading..."))
                else:
                    self.start_button.props("color=primary")
                    self.start_button.set_text(_("Start Download"))

            # Append new log lines.
            if self.download_log is not None and len(snapshot.logs_tail) > 0:
                # If the tail got truncated below the cursor (shouldn't happen
                # in practice but be safe), reset the cursor.
                if self._log_cursor > len(snapshot.logs_tail):
                    self._log_cursor = 0
                for line in snapshot.logs_tail[self._log_cursor :]:
                    self.download_log.push(line)
                self._log_cursor = len(snapshot.logs_tail)

            self._render_queue.refresh(snapshot)

    # ------------------------------------------------------------------
    # Queue rendering (driven from snapshot)
    # ------------------------------------------------------------------

    @ui.refreshable_method
    def _render_queue(self, snapshot: ServiceSnapshot) -> None:
        """Render the queue cards from a snapshot.

        Args:
            snapshot: The snapshot to render.
        """
        if not snapshot.jobs and not snapshot.pending_batches:
            ui.label(_("Queue is empty")).classes("text-gray-500 py-4")
            return

        # Summary stats
        total_jobs = len(snapshot.jobs)
        total_tracks = sum(j.total_tracks for j in snapshot.jobs)
        completed_tracks = sum(j.completed for j in snapshot.jobs)
        failed_tracks = sum(j.failed for j in snapshot.jobs)

        ui.label(
            f"{_('Total')}: {total_jobs} {ngettext('job', 'jobs', total_jobs)} | "
            f"{total_tracks} {ngettext('track', 'tracks', total_tracks)} | "
            f"{_('Completed')}: {completed_tracks} | {_('Failed')}: {failed_tracks}"
        ).classes("text-sm text-gray-600 mb-2")

        # Pending batches (queued, not yet started)
        if snapshot.pending_batches:
            with ui.card().classes("w-full bg-gray-50 mb-2"):
                ui.label(
                    f"{_('Queued')}: {len(snapshot.pending_batches)} "
                    f"{ngettext('batch', 'batches', len(snapshot.pending_batches))}"
                ).classes("text-sm font-medium")
                for i, batch in enumerate(snapshot.pending_batches, start=1):
                    preview = ", ".join(batch[:3])
                    if len(batch) > 3:
                        preview += f" (+{len(batch) - 3})"
                    ui.label(f"  {i}. {preview}").classes(
                        "text-xs text-gray-600 truncate"
                    )

        # Render each job.
        for job in snapshot.jobs:
            expanded = self._job_expanded.get(job.job_id, True)
            self._render_job_card(job, snapshot, expanded)

    def _render_job_card(
        self, job: JobSnapshot, snapshot: ServiceSnapshot, expanded: bool
    ) -> None:
        """Render a single job card with its tracks.

        Args:
            job: The job snapshot.
            snapshot: The full service snapshot (for track lookups).
            expanded: Whether the track list should be expanded.
        """
        type_icons = {
            "track": "music_note",
            "album": "album",
            "playlist": "queue_music",
            "artist": "person",
            "video": "movie",
        }
        status_colors = {
            "pending": "bg-gray-100",
            "downloading": "bg-blue-50",
            "completed": "bg-green-50",
            "partial": "bg-orange-50",
            "failed": "bg-red-50",
        }

        card_class = status_colors.get(job.status, "bg-gray-100")
        with ui.card().classes(f"w-full {card_class} mb-2"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.icon(type_icons.get(job.media_type, "help")).classes("text-gray-600")
                with ui.column().classes("flex-grow min-w-0"):
                    display_name = (
                        f"{job.artist} - {job.name}" if job.artist else job.name
                    )
                    if not display_name:
                        display_name = job.original_url[:50]
                    ui.label(display_name).classes("font-medium truncate")

                    finished = job.completed + job.failed + job.skipped
                    status_text = (
                        f"{finished}/{job.total_tracks} "
                        f"{ngettext('track', 'tracks', job.total_tracks)}"
                    )
                    if job.failed > 0:
                        status_text += f" ({job.failed} {_('failed')})"
                    ui.label(status_text).classes("text-xs text-gray-500")

                ui.linear_progress(
                    value=round(job.progress, 2), show_value=True
                ).classes("w-32")

            if job.track_ids:
                expansion = ui.expansion(
                    f"{_('Track list')} ({len(job.track_ids)})", value=expanded
                ).classes("w-full")
                expansion.on_value_change(
                    lambda e, jid=job.job_id: self._on_expansion_change(jid, e.value)
                )
                with expansion:
                    show_all = self._job_show_all.get(job.job_id, False)
                    visible_ids = (
                        job.track_ids
                        if show_all
                        else job.track_ids[:MAX_VISIBLE_TRACKS]
                    )
                    for track_id in visible_ids:
                        track = snapshot.tracks.get(track_id)
                        if track is not None:
                            self._render_track_row(track)

                    remaining = len(job.track_ids) - MAX_VISIBLE_TRACKS
                    if remaining > 0 and not show_all:
                        ui.button(
                            f"{_('Show all')} ({remaining} {_('more')})",
                            icon="expand_more",
                            on_click=lambda _e, jid=job.job_id: self._show_all_tracks(
                                jid
                            ),
                        ).props("flat dense").classes("w-full mt-1")

    def _render_track_row(self, track: Any) -> None:
        """Render a single track row.

        Args:
            track: The ``TrackSnapshot`` to render.
        """
        status_icons = {
            "pending": "schedule",
            "downloading": "downloading",
            "completed": "check_circle",
            "failed": "error",
            "skipped": "skip_next",
        }
        status_colors = {
            "pending": "text-gray-400",
            "downloading": "text-blue-500",
            "completed": "text-green-500",
            "failed": "text-red-500",
            "skipped": "text-orange-500",
        }
        quality_badges = {
            "hires": ("Hi-Res", "bg-green-500 text-white"),
            "lossless": ("Lossless", "bg-sky-600 text-white"),
            "lossy": ("Lossy", "bg-orange-500 text-white"),
        }

        with ui.row().classes(
            "w-full items-center gap-2 py-1 border-b border-gray-100"
        ):
            ui.icon(status_icons.get(track.status, "help")).classes(
                f"text-sm {status_colors.get(track.status, 'text-gray-400')}"
            )

            badge = quality_badges.get(track.quality)
            if badge is not None:
                label, badge_class = badge
                ui.label(label).classes(f"text-xs px-1.5 py-0.5 rounded {badge_class}")

            display = f"{track.artist} - {track.name}" if track.artist else track.name
            if not display:
                display = track.task_id[:20]
            ui.label(display).classes("text-sm flex-grow truncate")

            if track.message:
                ui.label(track.message).classes("text-xs text-gray-500")

            if track.status == "downloading":
                ui.linear_progress(value=round(track.progress, 2)).classes("w-20")

    def _on_expansion_change(self, job_id: str, value: bool) -> None:
        """Record expansion state so refreshes preserve it.

        Args:
            job_id: Job identifier.
            value: New expansion state.
        """
        self._job_expanded[job_id] = value

    def _show_all_tracks(self, job_id: str) -> None:
        """Flip the show-all flag for a job and rerender.

        Args:
            job_id: Job identifier.
        """
        self._job_show_all[job_id] = True
        self._render_queue.refresh(download_service.get_snapshot())

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _start_download(self) -> None:
        """Submit the URLs currently in the input to the service."""
        if not self.url_input or not self.url_input.value:
            ui.notify(_("Please enter download URL"), type="warning")
            return

        urls = tuple(
            url for url in self.url_input.value.split() if url.startswith("http")
        )
        if not urls:
            ui.notify(_("Please enter valid download URL"), type="warning")
            return

        self.url_input.value = ""
        add_log(f"{_('Submitted')} {len(urls)} {_('links')}")
        await download_service.submit_urls(urls)

    async def _clear_queue(self) -> None:
        """Clear completed jobs from the service."""
        await download_service.clear_completed()
        ui.notify(_("Queue cleared"), type="info")

    async def download_urls(self, urls: list[str]) -> None:
        """Public entry point used by other pages/extensions.

        Args:
            urls: URLs to download.
        """
        await download_service.submit_urls(tuple(urls))

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _teardown(self) -> None:
        """Unsubscribe when the client disconnects."""
        if self._sub_id is not None:
            download_service.unsubscribe(self._sub_id)
            self._sub_id = None
