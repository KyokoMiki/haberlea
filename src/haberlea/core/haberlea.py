"""Haberlea facade — thin orchestrator composing collaborators.

The constructor no longer performs I/O, plugin discovery, or settings
synchronization.  Use ``Haberlea.from_reconciled()`` (or the three-phase
``bootstrap → reconcile → persist_and_check`` pipeline) to build an
instance from the outside.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from .extension_manager import ExtensionManager
from .module_registry import ModuleRegistry, RegistryState
from .session_manager import SessionManager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from haberlea.downloader.protocols import ModuleProvider
    from haberlea.downloader.results import ModuleWithAccount
    from haberlea.plugins.base import ModuleBase

    from .bootstrap import BootstrapResult, ReconcileResult

logger = logging.getLogger(__name__)


class Haberlea:
    """Thin facade for music downloading orchestration.

    Composes 3 collaborators: SessionManager, ModuleRegistry,
    ExtensionManager.  The constructor is side-effect-free.
    """

    __slots__ = (
        "session_manager",
        "module_registry",
        "extension_manager",
    )

    def __init__(
        self,
        module_registry: ModuleRegistry,
        session_manager: SessionManager,
        extension_manager: ExtensionManager,
    ) -> None:
        """Initializes the Haberlea orchestrator from pre-built collaborators.

        Args:
            module_registry: Pre-configured module registry.
            session_manager: Pre-configured session manager.
            extension_manager: Pre-configured extension manager.
        """
        self.module_registry = module_registry
        self.session_manager = session_manager
        self.extension_manager = extension_manager

    @classmethod
    def from_reconciled(
        cls,
        bootstrap_result: BootstrapResult,
        reconcile_result: ReconcileResult,
    ) -> Haberlea:
        """Builds a Haberlea instance from bootstrap + reconcile results.

        Args:
            bootstrap_result: Result from the bootstrap phase.
            reconcile_result: Result from the reconcile phase.

        Returns:
            Fully configured Haberlea instance.
        """
        session_manager = SessionManager()

        # Build pre-populated registry state
        state = RegistryState()
        state.module_list = reconcile_result.registered_modules
        state.module_settings = reconcile_result.module_settings
        state.module_netloc_constants = reconcile_result.module_netloc_constants

        module_registry = ModuleRegistry(
            session_manager=session_manager,
            initial_state=state,
        )

        extension_manager = ExtensionManager(
            discovered=reconcile_result.discovered_extensions,
            extension_list=reconcile_result.extension_list,
        )

        return cls(
            module_registry=module_registry,
            session_manager=session_manager,
            extension_manager=extension_manager,
        )

    # --- Public API (delegates to collaborators) ---

    @property
    def module_provider(self) -> ModuleProvider:
        """Module provider interface backed by the registry."""
        return self.module_registry

    async def load_module(self, module: str, account_index: int = 0) -> ModuleBase:
        """Loads and initializes a module by name.

        Args:
            module: Module name to load.
            account_index: Index of the account to use.

        Returns:
            Loaded module instance.
        """
        return await self.module_registry.load_module(module, account_index)

    async def load_module_with_fallback(
        self,
        module: str,
        preferred_account_index: int = 0,
    ) -> ModuleWithAccount:
        """Loads a module, trying alternative accounts if preferred fails.

        Args:
            module: Module name to load.
            preferred_account_index: Preferred account index.

        Returns:
            ModuleWithAccount with loaded module and actual account index.
        """
        return await self.module_registry.load_module_with_fallback(
            module, preferred_account_index
        )

    def get_module_account_count(self, module: str) -> int:
        """Gets the number of configured accounts for a module.

        Args:
            module: Module name.

        Returns:
            Number of configured accounts.
        """
        return self.module_registry.get_module_account_count(module)

    def get_module_accounts(self, module: str) -> list[dict[str, Any]]:
        """Gets all configured accounts for a module.

        Args:
            module: Module name.

        Returns:
            List of account settings dictionaries.
        """
        return self.module_registry.get_module_accounts(module)

    def clear_module_session(self, module: str) -> bool:
        """Clears session data for a module to force re-login.

        Args:
            module: Module name.

        Returns:
            True if session was cleared successfully.
        """
        return self.session_manager.clear_module_session(module)


@asynccontextmanager
async def create_haberlea_session(
    bootstrap_result: BootstrapResult,
    reconcile_result: ReconcileResult,
) -> AsyncGenerator[Haberlea]:
    """Creates a Haberlea session with automatic cleanup.

    Args:
        bootstrap_result: Result from the bootstrap phase.
        reconcile_result: Result from the reconcile phase.

    Yields:
        Configured Haberlea session instance.
    """
    session = Haberlea.from_reconciled(bootstrap_result, reconcile_result)
    try:
        yield session
    finally:
        loaded = session.module_registry.state.loaded_modules
        for module_name, module_instance in loaded.items():
            try:
                await module_instance.close()
            except Exception:
                logger.debug("Error closing module %s", module_name)
