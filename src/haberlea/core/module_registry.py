"""Module discovery, registration, and loading."""

import logging
from typing import Any

from haberlea.downloader.contexts import LoginContext
from haberlea.downloader.results import ModuleWithAccount
from haberlea.plugins.base import ModuleBase
from haberlea.plugins.loader import load_module
from haberlea.utils.exceptions import ConfigurationError, InvalidModuleError
from haberlea.utils.models import (
    CoverCompressionEnum,
    CoverOptions,
    HabeleaOptions,
    ImageFileTypeEnum,
    ModuleController,
    ModuleFlags,
    ModuleInformation,
    ModuleModes,
    QualityEnum,
    TemporarySettingsController,
)
from haberlea.utils.settings import CONFIG_DIR, settings

from .session_manager import SessionManager, get_utc_timestamp

logger = logging.getLogger(__name__)


class RegistryState:
    """Mutable runtime state of the module registry.

    Separated from deps so ModuleRegistry itself has few fields.
    """

    def __init__(self) -> None:
        self.module_list: set[str] = set()
        self.module_settings: dict[str, ModuleInformation] = {}
        self.module_netloc_constants: dict[str, str] = {}
        self.loaded_modules: dict[str, ModuleBase] = {}
        self.current_account_index: dict[str, int] = {}


class ModuleRegistry:
    """Discovers, registers, and loads music service modules.

    Single responsibility: module lifecycle management.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        initial_state: RegistryState | None = None,
    ) -> None:
        self._config_dir = CONFIG_DIR
        self._session_manager = session_manager
        self.state = initial_state or RegistryState()

    def get_module_flags(self, name: str) -> ModuleFlags | None:
        """Returns the flags for the named module.

        Args:
            name: Module name.

        Returns:
            ModuleFlags, or None if module is unknown.
        """
        info = self.state.module_settings.get(name)
        if info is None:
            return None
        return info.flags

    def supports_mode(self, name: str, mode: ModuleModes) -> bool:
        """Returns True if the named module supports the given mode.

        Args:
            name: Module name.
            mode: The ModuleModes value to check.

        Returns:
            True if the module supports the mode.
        """
        info = self.state.module_settings.get(name)
        if info is None:
            return False
        return mode in info.module_supported_modes

    def register_modules(
        self,
        discovered_modules: dict[str, ModuleInformation],
    ) -> None:
        """Registers discovered modules.

        Args:
            discovered_modules: Dictionary of discovered module information.

        Raises:
            SystemExit: If no modules are installed.
        """
        if not discovered_modules:
            logger.warning("No modules are installed, quitting")
            raise SystemExit(1)

        for name, info in discovered_modules.items():
            if not info:
                continue

            self.state.module_list.add(name)
            self.state.module_settings[name] = info

    def validate_netloc_constants(self) -> None:
        """Validates that no duplicate netlocation constants exist.

        Raises:
            ConfigurationError: If duplicate netlocation constants are found.
        """
        duplicates: set[tuple[str, str]] = set()

        for module in self.state.module_list:
            module_info = self.state.module_settings[module]
            url_constants = module_info.netlocation_constant

            if url_constants is None:
                continue

            constants = (
                url_constants if isinstance(url_constants, list) else [url_constants]
            )

            for constant in constants:
                resolved = self._resolve_netloc_constant(constant, module)
                if not resolved:
                    continue

                self._check_duplicate_constant(resolved, module, duplicates)

        if duplicates:
            dup_str = ", ".join(f"{a} and {b}" for a, b in duplicates)
            raise ConfigurationError(
                f"Multiple modules connect to the same service names: {dup_str}"
            )

    def _resolve_netloc_constant(self, constant: str, module: str) -> str | None:
        """Resolves a netlocation constant.

        Args:
            constant: The constant string, possibly prefixed with 'setting.'.
            module: Module name for settings lookup.

        Returns:
            Resolved constant string, or None if unresolvable.
        """
        if not constant.startswith("setting."):
            return constant

        setting_key = constant.split("setting.", 1)[1]
        accounts = settings.modules.get(module, [])
        if not accounts:
            return None
        module_settings = accounts[0]
        return module_settings.get(setting_key)

    def _check_duplicate_constant(
        self,
        constant: str,
        module: str,
        duplicates: set[tuple[str, str]],
    ) -> None:
        """Checks for duplicate netlocation constants.

        Args:
            constant: The resolved constant.
            module: Current module name.
            duplicates: Set to collect duplicate pairs.
        """
        if constant not in self.state.module_netloc_constants:
            self.state.module_netloc_constants[constant] = module
            return

        existing_module = self.state.module_netloc_constants[constant]
        m1, m2 = sorted((module, existing_module))
        duplicates.add((m1, m2))

    async def load_module(self, module: str, account_index: int = 0) -> ModuleBase:
        """Loads and initializes a module by name with specified account.

        Args:
            module: Module name to load.
            account_index: Index of the account to use.

        Returns:
            Loaded module instance.

        Raises:
            InvalidModuleError: If module doesn't exist or can't be loaded.
        """
        module = module.lower()

        if module not in self.state.module_list:
            raise InvalidModuleError(module)

        cache_key = f"{module}:{account_index}"

        if cache_key in self.state.loaded_modules:
            return self.state.loaded_modules[cache_key]

        module_class = load_module(module)
        if not module_class:
            raise InvalidModuleError(module)

        controller = self._build_module_controller(module, account_index)
        loaded_module = module_class(controller)
        self.state.loaded_modules[cache_key] = loaded_module

        module_info = self.state.module_settings[module]
        accounts = settings.modules.get(module, [])
        account_settings = (
            accounts[account_index] if account_index < len(accounts) else {}
        )

        await self._session_manager.handle_module_auth(
            LoginContext(
                module_name=module,
                loaded_module=loaded_module,
                module_info=module_info,
                account_settings=account_settings,
                account_index=account_index,
            ),
        )
        self._ensure_module_data_folder(module)

        logger.debug("Module loaded: %s (account %d)", module, account_index)
        return loaded_module

    def _build_module_controller(
        self, module: str, account_index: int = 0
    ) -> ModuleController:
        """Builds a ModuleController for the specified module.

        Args:
            module: Module name.
            account_index: Index of the account to use.

        Returns:
            Configured ModuleController instance.
        """
        accounts = settings.modules.get(module, [])

        if accounts and account_index < len(accounts):
            module_settings = accounts[account_index]
        else:
            module_settings = {}

        data_folder = str(self._config_dir / "modules" / module)

        return ModuleController(
            module_settings=module_settings,
            data_folder=data_folder,
            extensions=[],  # Extensions passed separately via Downloader
            temporary_settings_controller=TemporarySettingsController(
                module, str(self._session_manager.session_path), account_index
            ),
            get_current_timestamp=get_utc_timestamp,
            haberlea_options=self._build_haberlea_options(),
        )

    def _build_haberlea_options(self) -> HabeleaOptions:
        """Builds HabeleaOptions from current settings.

        Returns:
            HabeleaOptions instance.
        """
        gs = settings.global_settings
        covers = gs.covers

        return HabeleaOptions(
            debug_mode=gs.runtime.debug_mode,
            quality_tier=QualityEnum[gs.quality.tier.upper()],
            default_cover_options=CoverOptions(
                file_type=ImageFileTypeEnum.jpg,
                resolution=covers.main_resolution,
                compression=CoverCompressionEnum[covers.main_compression],
            ),
            concurrent_downloads=gs.runtime.concurrent_downloads,
        )

    def _ensure_module_data_folder(self, module: str) -> None:
        """Creates module data folder if needed.

        Args:
            module: Module name.
        """
        module_info = self.state.module_settings[module]
        if not (module_info.flags & ModuleFlags.uses_data):
            return

        data_folder = self._config_dir / "modules" / module
        data_folder.mkdir(parents=True, exist_ok=True)

    def get_module_account_count(self, module: str) -> int:
        """Gets the number of configured accounts for a module.

        Args:
            module: Module name.

        Returns:
            Number of configured accounts.
        """
        accounts = settings.modules.get(module, [])
        return len(accounts)

    def get_module_accounts(self, module: str) -> list[dict[str, Any]]:
        """Gets all configured accounts for a module.

        Args:
            module: Module name.

        Returns:
            List of account settings dictionaries.
        """
        return settings.modules.get(module, [])

    async def load_module_with_fallback(
        self,
        module: str,
        preferred_account_index: int = 0,
    ) -> ModuleWithAccount:
        """Loads a module, trying alternative accounts if preferred fails.

        Args:
            module: Module name to load.
            preferred_account_index: Preferred account index to try first.

        Returns:
            ModuleWithAccount with loaded module and actual account index.

        Raises:
            InvalidModuleError: If module doesn't exist or all accounts fail.
        """
        account_count = self.get_module_account_count(module)
        if account_count == 0:
            return ModuleWithAccount(
                module=await self.load_module(module, 0), account_index=0
            )

        try:
            return ModuleWithAccount(
                module=await self.load_module(module, preferred_account_index),
                account_index=preferred_account_index,
            )
        except Exception as e:
            logger.warning(
                "Failed to load module %s with account %d: %s",
                module,
                preferred_account_index,
                e,
            )

        for idx in range(account_count):
            if idx == preferred_account_index:
                continue
            try:
                return ModuleWithAccount(
                    module=await self.load_module(module, idx),
                    account_index=idx,
                )
            except Exception as e:
                logger.warning(
                    "Failed to load module %s with account %d: %s",
                    module,
                    idx,
                    e,
                )

        raise InvalidModuleError(module)
