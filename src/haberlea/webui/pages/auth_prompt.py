"""Global interactive-login prompt dialog for Haberlea WebUI.

The dialog is a thin view over ``ServiceSnapshot.auth_prompt``: it subscribes
to ``download_service`` and mirrors the single active prompt. The worker logs
in serially and in-band, so at most one prompt is active at a time and a single
shared dialog suffices.

Two prompt kinds are handled:

- ``"waiting"``: display a link the user must open (e.g. TIDAL device-code
    authorization) plus a spinner while the background flow polls. No input is
    required; the dialog closes automatically when authorization completes.
- ``"input"``: display an optional link plus a text field the user fills in
    (e.g. Amazon callback URL, KKBOX remote-login JSON).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from nicegui import background_tasks, ui

from haberlea.i18n import _
from haberlea.webui import download_service

if TYPE_CHECKING:
    from nicegui import Client

    from haberlea.webui.download_service import AuthPromptSnapshot, ServiceSnapshot


class AuthPromptDialog:
    """Global modal mirroring the service's active interactive-login prompt."""

    def __init__(self) -> None:
        """Initialize dialog UI references and subscription state."""
        self._client: Client | None = None
        self._sub_id: int | None = None

        # Coalescing state for snapshot push -> UI update.
        self._latest: ServiceSnapshot | None = None
        self._scheduled: bool = False

        self._dialog: ui.dialog | None = None
        self._input: ui.textarea | None = None
        # Identifier of the prompt currently rendered, to avoid rebuilding the
        # dialog on every unrelated snapshot push.
        self._active_prompt_id: int | None = None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> None:
        """Create the dialog and subscribe to the service."""
        self._client = ui.context.client
        self._dialog = ui.dialog().props("persistent")

        # Initial paint from current snapshot (e.g. on browser refresh while a
        # prompt is already active).
        self._apply(download_service.get_snapshot())

        self._sub_id = download_service.subscribe(self._on_snapshot)
        self._client.on_disconnect(self._teardown)

    def _build(self, prompt: AuthPromptSnapshot) -> None:
        """Build the dialog body for a prompt.

        Args:
            prompt: The active prompt snapshot to render.
        """
        with ui.card().classes("min-w-96 max-w-2xl gap-3"):
            ui.label(_("Interactive Login")).classes("text-lg font-bold")
            ui.label(prompt.message).classes("whitespace-pre-wrap text-sm")

            if prompt.url:
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.link(prompt.url, prompt.url, new_tab=True).classes(
                        "break-all text-primary"
                    )
                    url = prompt.url
                    ui.button(
                        icon="content_copy",
                        on_click=lambda: self._copy(url),
                    ).props("flat dense round")

            if prompt.kind == "waiting":
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="sm")
                    ui.label(_("Waiting for authorization...")).classes(
                        "text-sm text-gray-500"
                    )
            else:
                self._input = (
                    ui.textarea(label=_("Paste here"))
                    .classes("w-full")
                    .props("rows=2 autofocus")
                )
                prompt_id = prompt.prompt_id
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button(
                        _("Cancel"),
                        on_click=lambda: self._cancel(prompt_id),
                    ).props("flat")
                    ui.button(
                        _("Submit"),
                        on_click=lambda: self._submit(prompt_id),
                    ).props("color=primary")

    def _copy(self, text: str) -> None:
        """Copy text to the user's clipboard and notify.

        Args:
            text: The text to copy.
        """
        ui.clipboard.write(text)
        ui.notify(_("Copied"), type="info")

    # ------------------------------------------------------------------
    # Snapshot subscription (same coalescing pattern as DownloadPage)
    # ------------------------------------------------------------------

    def _on_snapshot(self, snapshot: ServiceSnapshot) -> None:
        """Receive a snapshot and schedule a coalesced rerender.

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
                self._rerender_latest(), name="haberlea-auth-prompt-rerender"
            )

    async def _rerender_latest(self) -> None:
        """Drain the latest snapshot and apply it."""
        try:
            snapshot = self._latest
            self._latest = None
            if snapshot is None:
                return
            self._apply(snapshot)
        finally:
            self._scheduled = False
            if self._latest is not None:
                self._on_snapshot(self._latest)

    def _apply(self, snapshot: ServiceSnapshot) -> None:
        """Open, rebuild, or close the dialog to match the snapshot.

        Args:
            snapshot: The snapshot to render.
        """
        if self._dialog is None:
            return
        prompt = snapshot.auth_prompt
        with contextlib.suppress(RuntimeError):
            if prompt is None:
                if self._active_prompt_id is not None:
                    self._active_prompt_id = None
                    self._input = None
                    self._dialog.clear()
                    self._dialog.close()
                return
            if prompt.prompt_id == self._active_prompt_id:
                return
            self._active_prompt_id = prompt.prompt_id
            self._input = None
            self._dialog.clear()
            with self._dialog:
                self._build(prompt)
            self._dialog.open()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _submit(self, prompt_id: int) -> None:
        """Resolve an input prompt with the field's value.

        Args:
            prompt_id: Identifier of the prompt to resolve.
        """
        value = self._input.value if self._input is not None else ""
        await download_service.resolve_prompt(prompt_id, value or "")

    async def _cancel(self, prompt_id: int) -> None:
        """Resolve an input prompt with an empty value to abandon login.

        Args:
            prompt_id: Identifier of the prompt to resolve.
        """
        await download_service.resolve_prompt(prompt_id, "")

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _teardown(self) -> None:
        """Unsubscribe when the client disconnects."""
        if self._sub_id is not None:
            download_service.unsubscribe(self._sub_id)
            self._sub_id = None
