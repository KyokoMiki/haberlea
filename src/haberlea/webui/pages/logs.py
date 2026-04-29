"""Logs page for Haberlea WebUI."""

from nicegui import ui

from haberlea.i18n import _
from haberlea.webui.state import clear_logs, get_app_storage


class LogsPage:
    """Logs page component for viewing application logs."""

    def __init__(self) -> None:
        """Initializes the logs page."""
        self.log_display: ui.log | None = None

    def render(self) -> None:
        """Renders the logs page."""
        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(_("Application Logs")).classes("text-2xl font-bold")
                with ui.row().classes("gap-2"):
                    ui.button(
                        _("Refresh"), icon="refresh", on_click=self._refresh_logs
                    ).props("flat")
                    ui.button(
                        _("Clear Logs"), icon="delete", on_click=self._clear_logs
                    ).props("flat color=negative")

            with ui.card().classes("w-full"):
                self.log_display = ui.log(max_lines=500).classes("w-full h-[600px]")
                self._load_logs()

    def _load_logs(self) -> None:
        """Loads existing logs into the display."""
        if not self.log_display:
            return

        storage = get_app_storage()
        logs = storage.logs

        for log_msg in logs:
            self.log_display.push(log_msg)

    def _refresh_logs(self) -> None:
        """Refreshes the log display."""
        if not self.log_display:
            return

        self.log_display.clear()
        self._load_logs()
        ui.notify(_("Logs refreshed"), type="info")

    def _clear_logs(self) -> None:
        """Clears all logs."""
        clear_logs()
        if self.log_display:
            self.log_display.clear()
        ui.notify(_("Logs cleared"), type="info")
