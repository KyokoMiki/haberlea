"""Extension discovery, registration, and lifecycle management."""

import logging
from typing import Any

from haberlea.plugins.base import WebUIPageBase
from haberlea.plugins.loader import discover_extensions, load_extension
from haberlea.utils.models import ExtensionInstance
from haberlea.utils.settings import settings

logger = logging.getLogger(__name__)


class ExtensionManager:
    """Discovers, registers, initializes, and executes extensions.

    Single responsibility: extension lifecycle management.
    """

    def __init__(
        self,
        discovered: dict[str, Any] | None = None,
        extension_list: set[str] | None = None,
    ) -> None:
        """Initializes the extension manager.

        Args:
            discovered: Pre-discovered extension information (from bootstrap).
            extension_list: Pre-built set of extension names (from bootstrap).
        """
        self.extensions: list[ExtensionInstance] = []
        self.extension_list: set[str] = extension_list or set()
        self.discovered_extensions: dict[str, Any] = discovered or {}

    def discover(self) -> None:
        """Discovers available extensions via entry points."""
        self.discovered_extensions = discover_extensions()
        for ext_name in self.discovered_extensions:
            self.extension_list.add(ext_name)
            logger.debug("Extension detected: %s", ext_name)

    def initialize(self) -> None:
        """Initializes extension instances sorted by priority.

        Priority is read from user settings, defaulting to 100.
        """
        extension_instances: list[ExtensionInstance] = []

        for ext_name in self.extension_list:
            ext_info = self.discovered_extensions[ext_name]
            ext_settings = self._get_extension_settings(ext_name, ext_info)

            ext_class = load_extension(ext_name)
            if ext_class:
                instance = ext_class(ext_settings)
                priority = ext_settings.get("priority", 100)
                extension_instances.append(
                    ExtensionInstance(
                        name=ext_name,
                        priority=priority,
                        instance=instance,
                    )
                )

        self.extensions = sorted(extension_instances, key=lambda x: x.priority)

    def _get_extension_settings(self, ext_name: str, ext_info: Any) -> dict[str, Any]:
        """Gets extension settings from config or defaults.

        Args:
            ext_name: Extension name.
            ext_info: Extension information object.

        Returns:
            Extension settings dictionary.
        """
        ext_type = ext_info.extension_type
        type_config = settings.extensions.get(ext_type, {})
        return type_config.get(ext_name) or ext_info.settings

    async def run_for_job(self, job: Any) -> None:
        """Runs all extensions for a completed job.

        Extensions are executed in priority order.

        Args:
            job: The completed download job.
        """
        for ext in self.extensions:
            try:
                logger.info(
                    "=== Running Extension %s (priority: %d) ===",
                    ext.name,
                    ext.priority,
                )
                await ext.instance.on_job_complete(job)
                logger.info("=== Extension %s Completed ===", ext.name)
            except Exception:
                logger.exception(
                    "Extension %s failed for %s",
                    ext.name,
                    getattr(job, "download_path", "unknown"),
                )

    async def run_finalize(self) -> None:
        """Runs on_all_complete method for all extensions.

        Extensions are executed in priority order.
        """
        for ext in self.extensions:
            try:
                await ext.instance.on_all_complete()
            except Exception:
                logger.exception("Extension %s on_all_complete failed", ext.name)

    def get_webui_pages(self) -> dict[str, type[WebUIPageBase]]:
        """Returns extension WebUI pages for registration.

        Returns:
            Dictionary mapping extension names to WebUI page classes.
        """
        pages: dict[str, type[WebUIPageBase]] = {}
        for name, info in self.discovered_extensions.items():
            if hasattr(info, "webui_page") and info.webui_page is not None:
                pages[name] = info.webui_page
        return pages
