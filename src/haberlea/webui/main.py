"""Main entry point for Haberlea WebUI."""

import logging
import os
import sys
from typing import Any

from nicegui import app, ui
from rich.logging import RichHandler

from haberlea.core import cleanup_modules
from haberlea.core.bootstrap import bootstrap, persist_and_check, reconcile
from haberlea.core.haberlea import Haberlea
from haberlea.i18n import _, set_language
from haberlea.utils.settings import NICEGUI_STORAGE_DIR, SETTINGS_PATH, settings

from .auth import AuthMiddleware, create_login_page
from .download_service import init_download_service
from .pages.download import DownloadPage
from .pages.logs import LogsPage
from .pages.search import SearchPage
from .pages.settings import SettingsPage
from .state import get_haberlea, get_user_preferences, init_haberlea, register_page

logger = logging.getLogger(__name__)


def install_event_loop() -> None:
    """Installs the appropriate event loop for the current platform."""
    if sys.platform == "win32":
        try:
            import winloop  # type: ignore[import-not-found]  # noqa: PLC0415

            winloop.install()
        except ImportError:
            logger.info("winloop not available, using default asyncio event loop")
    else:
        try:
            import uvloop  # type: ignore[import-not-found]  # noqa: PLC0415

            uvloop.install()
        except ImportError:
            logger.info("uvloop not available, using default asyncio event loop")


def configure_logging() -> None:
    """Configures logging based on debug mode setting."""
    level = (
        logging.DEBUG if settings.global_settings.runtime.debug_mode else logging.INFO
    )
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


def _get_extension_webui_pages() -> dict[str, type]:
    """Gets WebUI pages from the initialized extension manager.

    Returns:
        Dictionary mapping extension names to their WebUI page classes.
    """
    haberlea = get_haberlea()
    return haberlea.extension_manager.get_webui_pages()


# Cache for extension page instances
_extension_pages: dict[str, Any] = {}


@ui.page("/")
def index_page() -> None:
    """Renders the main single-page application with tabs."""
    # Clear stale extension page instances from previous renders
    _extension_pages.clear()

    # Initialize language from settings
    user_language = settings.global_settings.webui.language
    set_language(user_language)

    # Apply dark mode from user preferences
    prefs = get_user_preferences()
    if prefs.dark_mode:
        ui.dark_mode().enable()
    else:
        ui.dark_mode().disable()

    # Create core page instances
    download_page = DownloadPage()
    search_page = SearchPage()
    settings_page = SettingsPage()
    logs_page = LogsPage()

    # Register core pages for cross-page access
    register_page("download", download_page)
    register_page("search", search_page)
    register_page("settings", settings_page)
    register_page("logs", logs_page)

    # Create extension pages from initialized manager
    extension_page_classes = _get_extension_webui_pages()
    for ext_name, page_class in extension_page_classes.items():
        page_instance = page_class()
        page_id = getattr(page_class, "page_id", ext_name)
        _extension_pages[page_id] = page_instance
        register_page(page_id, page_instance)

    # Header
    with ui.header().classes("bg-primary text-white items-center justify-between"):
        with ui.row().classes("items-center gap-4"):
            ui.icon("music_note", size="lg")
            ui.label("Haberlea").classes("text-xl font-bold")

        # Tab navigation in header
        tabs = ui.tabs().classes("text-white").props("inline-label")
        with tabs:
            ui.tab("download", label=_("Download"), icon="download")
            ui.tab("search", label=_("Search"), icon="search")

            # Add extension tabs (sorted by page_order)
            sorted_ext_pages = sorted(
                _extension_pages.items(),
                key=lambda x: getattr(x[1], "page_order", 50),
            )
            for page_id, page_instance in sorted_ext_pages:
                label = getattr(page_instance, "page_label", page_id)
                icon = getattr(page_instance, "page_icon", "extension")
                ui.tab(page_id, label=label, icon=icon)

            ui.tab("settings", label=_("Settings"), icon="settings")
            ui.tab("logs", label=_("Logs"), icon="article")
        # User profile and logout
        if settings.global_settings.webui.auth_enabled:

            def logout() -> None:
                app.storage.user.clear()
                ui.navigate.to("/login")

            with ui.row().classes(
                "items-center ml-4 gap-2 border-l pl-4 border-gray-400"
            ):
                ui.icon("person", size="sm")
                ui.label(app.storage.user.get("username", "")).classes(
                    "text-sm font-medium"
                )
                ui.button(on_click=logout, icon="logout").props(
                    "flat round dense color=white"
                ).tooltip(_("Logout"))
    with (
        ui.column().classes("w-full min-h-screen"),
        ui.tab_panels(tabs, value="download").classes("w-full"),
    ):
        with ui.tab_panel("download"):
            download_page.render()

        with ui.tab_panel("search"):
            search_page.render()

        # Render extension pages
        for page_id, page_instance in _extension_pages.items():
            with ui.tab_panel(page_id):
                page_instance.render()

        with ui.tab_panel("settings"):
            settings_page.render()

        with ui.tab_panel("logs"):
            logs_page.render()


def main() -> None:
    """Main entry point for Haberlea WebUI.

    Runs the three-phase initialization pipeline before starting NiceGUI:
    1. bootstrap — discover modules/extensions, load settings
    2. reconcile — merge defaults, compute sessions
    3. persist_and_check — write to disk, exit if new settings detected
    """
    install_event_loop()

    # Three-phase initialization
    bootstrap_result = bootstrap()
    configure_logging()

    reconcile_result = reconcile(bootstrap_result, settings.current)
    has_new = persist_and_check(reconcile_result)

    if has_new:
        logger.warning(
            "New settings detected, or the configuration has been reset. "
            "Please update settings file: %s",
            SETTINGS_PATH,
        )
        raise SystemExit(0)

    # Build and store global Haberlea singleton
    haberlea = Haberlea.from_reconciled(bootstrap_result, reconcile_result)
    init_haberlea(haberlea)
    init_download_service(haberlea)

    async def _shutdown() -> None:
        await cleanup_modules(haberlea)

    app.on_shutdown(_shutdown)

    # Configure NiceGUI middleware and auth
    if settings.global_settings.webui.auth_enabled:
        app.add_middleware(AuthMiddleware)
        create_login_page()

    # Storage secret for NiceGUI
    storage_secret = os.environ.get("NICEGUI_STORAGE_SECRET", "haberlea-webui-secret")

    # Redirect NiceGUI's `.nicegui` storage directory to live under CONFIG_DIR
    NICEGUI_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    app.storage.path = NICEGUI_STORAGE_DIR

    ui.run(
        title="Haberlea",
        favicon="♪",
        storage_secret=storage_secret,
        host=settings.global_settings.webui.host,
        port=settings.global_settings.webui.port,
        show=True,
        reload=False,
    )


if __name__ == "__main__":
    main()
