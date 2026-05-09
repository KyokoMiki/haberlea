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
        self.extension_list: set[str] = extension_list or set()
        self.discovered_extensions: dict[str, Any] = discovered or {}

    def discover(self) -> None:
        """Discovers available extensions via entry points."""
        self.discovered_extensions = discover_extensions()
        for ext_name in self.discovered_extensions:
            self.extension_list.add(ext_name)
            logger.debug("Extension detected: %s", ext_name)

    def build_instances(self) -> list[ExtensionInstance]:
        """Builds a fresh batch of extension instances from current settings.

        Each call reads the latest values from ``settings.extensions`` and
        instantiates new objects, sorted by priority. This is intended to
        be invoked once per download batch so configuration changes made
        via the WebUI take effect on the next download without restarting
        the application or interfering with batches already in flight.

        Returns:
            A new list of ``ExtensionInstance`` sorted by priority (asc).
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

        return sorted(extension_instances, key=lambda x: x.priority)

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

    @staticmethod
    async def run_for_job(extensions: list[ExtensionInstance], job: Any) -> None:
        """Runs the given extension instances for a completed job.

        Extensions are executed in priority order.

        Args:
            extensions: Extension instances to invoke (typically the per-batch
                list returned by :meth:`build_instances`).
            job: The completed download job.
        """
        for ext in extensions:
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

    @staticmethod
    async def run_finalize(extensions: list[ExtensionInstance]) -> None:
        """Runs ``on_all_complete`` on the given extension instances.

        Args:
            extensions: Extension instances to finalize (typically the same
                per-batch list passed to :meth:`run_for_job`).
        """
        for ext in extensions:
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
