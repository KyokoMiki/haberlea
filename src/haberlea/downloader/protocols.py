from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from haberlea.core.module_registry import RegistryState
    from haberlea.plugins.base import ModuleBase
    from haberlea.utils.models import ModuleFlags, ModuleModes


@runtime_checkable
class ModuleProvider(Protocol):
    """Read-only interface for module registry access."""

    state: RegistryState

    async def load_module(self, module: str, account_index: int = 0) -> ModuleBase:
        """Loads a module by name with the specified account index."""
        ...

    def get_module_flags(self, name: str) -> ModuleFlags | None:
        """Returns the flags for the named module, or None if unknown."""
        ...

    def supports_mode(self, name: str, mode: ModuleModes) -> bool:
        """Returns True if the named module supports the given mode."""
        ...
