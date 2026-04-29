"""Bootstrap and reconciliation for Haberlea initialization.

Three-phase initialization:
1. bootstrap() — discover modules/extensions, load settings (no mutation)
2. reconcile() — merge defaults into settings, compute sessions (pure)
3. persist_and_check() — write to disk, return whether new settings detected
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import msgspec

from haberlea.plugins.loader import discover_extensions, discover_modules
from haberlea.utils.exceptions import ConfigurationError
from haberlea.utils.settings import (
    CONFIG_DIR,
    SESSION_PATH,
    SETTINGS_PATH,
    AppSettings,
    save_settings,
    set_settings,
)

from .session_manager import SessionManager

if TYPE_CHECKING:
    from pathlib import Path

    from haberlea.utils.models import ExtensionInformation, ModuleInformation

logger = logging.getLogger(__name__)


# =========================================================================
# Data structures
# =========================================================================


class BootstrapResult(msgspec.Struct, frozen=True):
    """Immutable result of the bootstrap phase.

    Attributes:
        discovered_modules: All modules found via entry points.
        discovered_extensions: All extensions found via entry points.
    """

    discovered_modules: dict[str, ModuleInformation]
    discovered_extensions: dict[str, ExtensionInformation]


class ReconcileResult(msgspec.Struct, frozen=True):
    """Immutable result of the reconciliation phase.

    Attributes:
        new_settings: Merged AppSettings with all defaults applied.
        has_new_settings: True if user must edit settings before continuing.
        sessions: Updated session storage dictionary.
        registered_modules: Set of module names that passed filtering.
        module_settings: Module information keyed by name.
        module_netloc_constants: Netloc-to-module mapping.
        extension_list: Set of discovered extension names.
        discovered_extensions: Extension information keyed by name.
    """

    new_settings: AppSettings
    has_new_settings: bool
    sessions: dict[str, Any]
    registered_modules: set[str]
    module_settings: dict[str, ModuleInformation]
    module_netloc_constants: dict[str, str]
    extension_list: set[str]
    discovered_extensions: dict[str, ExtensionInformation]


# =========================================================================
# Phase 1: Bootstrap
# =========================================================================


def bootstrap() -> BootstrapResult:
    """Discovers modules/extensions and loads settings.

    This is the first initialization phase. It performs entry-point
    scanning and settings file loading, but does NOT mutate global
    state beyond the settings singleton (required for downstream code).

    Returns:
        BootstrapResult with all discovered data.

    Raises:
        SystemExit: If no modules are installed.
    """
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    discovered_modules = discover_modules()
    if not discovered_modules:
        logger.warning("No modules are installed, quitting")
        raise SystemExit(1)

    discovered_extensions = discover_extensions()

    return BootstrapResult(
        discovered_modules=discovered_modules,
        discovered_extensions=discovered_extensions,
    )


# =========================================================================
# Phase 2: Reconcile (pure functions)
# =========================================================================


def filter_modules(
    discovered: dict[str, ModuleInformation],
) -> tuple[set[str], dict[str, ModuleInformation]]:
    """Filters discovered modules, excluding empty entries.

    Args:
        discovered: All discovered module information.

    Returns:
        Tuple of (module name set, filtered module settings dict).
    """
    module_list: set[str] = set()
    module_settings: dict[str, ModuleInformation] = {}

    for name, info in discovered.items():
        if not info:
            continue

        module_list.add(name)
        module_settings[name] = info

    return module_list, module_settings


def validate_netloc_constants(
    module_list: set[str],
    module_settings: dict[str, ModuleInformation],
    current_settings: AppSettings,
) -> dict[str, str]:
    """Validates netlocation constants and builds the mapping.

    Args:
        module_list: Set of registered module names.
        module_settings: Module information dictionary.
        current_settings: Current app settings for resolving setting-based constants.

    Returns:
        Dictionary mapping netloc patterns to module names.

    Raises:
        ConfigurationError: If duplicate netlocation constants are found.
    """
    netloc_map: dict[str, str] = {}
    duplicates: set[tuple[str, str]] = set()

    for module in module_list:
        module_info = module_settings[module]
        url_constants = module_info.netlocation_constant

        if url_constants is None:
            continue

        constants = (
            url_constants if isinstance(url_constants, list) else [url_constants]
        )

        for constant in constants:
            resolved = _resolve_netloc_constant(constant, module, current_settings)
            if not resolved:
                continue

            _check_duplicate_constant(resolved, module, netloc_map, duplicates)

    if duplicates:
        dup_str = ", ".join(f"{a} and {b}" for a, b in duplicates)
        raise ConfigurationError(
            f"Multiple modules connect to the same service names: {dup_str}"
        )

    return netloc_map


def _resolve_netloc_constant(
    constant: str,
    module: str,
    current_settings: AppSettings,
) -> str | None:
    """Resolves a netlocation constant, possibly from settings.

    Args:
        constant: The constant string, possibly prefixed with 'setting.'.
        module: Module name for settings lookup.
        current_settings: Current app settings.

    Returns:
        Resolved constant string, or None if unresolvable.
    """
    if not constant.startswith("setting."):
        return constant

    setting_key = constant.split("setting.", 1)[1]
    accounts = current_settings.modules.get(module, [])
    if not accounts:
        return None
    return accounts[0].get(setting_key)


def _check_duplicate_constant(
    constant: str,
    module: str,
    netloc_map: dict[str, str],
    duplicates: set[tuple[str, str]],
) -> None:
    """Checks for and records duplicate netlocation constants.

    Args:
        constant: The resolved constant.
        module: Current module name.
        netloc_map: Existing netloc-to-module mapping (mutated).
        duplicates: Set to collect duplicate pairs (mutated).
    """
    if constant not in netloc_map:
        netloc_map[constant] = module
        return

    existing_module = netloc_map[constant]
    m1, m2 = sorted((module, existing_module))
    duplicates.add((m1, m2))


def sync_extension_settings_pure(
    extension_list: set[str],
    discovered_extensions: dict[str, ExtensionInformation],
    current_settings: AppSettings,
) -> tuple[AppSettings, bool]:
    """Merges extension defaults into settings (pure function).

    Args:
        extension_list: Set of registered extension names.
        discovered_extensions: Discovered extension information.
        current_settings: Current application settings.

    Returns:
        Tuple of (new AppSettings, whether new settings were detected).
    """
    new_detected = False
    current_extensions = dict(current_settings.extensions)

    for ext_name in extension_list:
        ext_info = discovered_extensions.get(ext_name)
        if not ext_info:
            continue

        ext_type = ext_info.extension_type
        if ext_type not in current_extensions:
            current_extensions[ext_type] = {}

        if ext_name not in current_extensions[ext_type]:
            current_extensions[ext_type][ext_name] = dict(ext_info.settings)
            new_detected = True
        else:
            for key, default_value in ext_info.settings.items():
                if key not in current_extensions[ext_type][ext_name]:
                    current_extensions[ext_type][ext_name][key] = default_value
                    new_detected = True

    new_settings = AppSettings(
        global_settings=current_settings.global_settings,
        extensions=current_extensions,
        modules=current_settings.modules,
    )
    return new_settings, new_detected


def sync_module_settings_pure(
    module_list: set[str],
    module_settings: dict[str, ModuleInformation],
    current_settings: AppSettings,
) -> tuple[AppSettings, bool]:
    """Merges module defaults into settings (pure function).

    Args:
        module_list: Set of registered module names.
        module_settings: Module information dictionary.
        current_settings: Current application settings.

    Returns:
        Tuple of (new AppSettings, whether new settings were detected).
    """
    new_detected = False
    current_modules = dict(current_settings.modules)

    for module_name in module_list:
        module_info = module_settings[module_name]

        settings_to_parse = {
            **module_info.global_settings,
            **module_info.session_settings,
        }

        if not settings_to_parse:
            continue

        existing_accounts = current_modules.get(module_name, [])

        if not existing_accounts:
            current_modules[module_name] = [dict(settings_to_parse)]
            new_detected = True
        else:
            for account in existing_accounts:
                for key, default_value in settings_to_parse.items():
                    if key not in account:
                        account[key] = default_value
                        new_detected = True

    new_settings = AppSettings(
        global_settings=current_settings.global_settings,
        extensions=current_settings.extensions,
        modules=current_modules,
    )
    return new_settings, new_detected


def _compute_sessions(
    module_list: set[str],
    module_settings: dict[str, ModuleInformation],
    module_accounts: dict[str, list[dict[str, Any]]],
    session_path: Path,
) -> dict[str, Any]:
    """Computes updated session storage.

    Args:
        module_list: Set of registered module names.
        module_settings: Module information dictionary.
        module_accounts: Module settings with list of accounts per module.
        session_path: Path to the session storage file.

    Returns:
        Updated sessions dictionary.
    """
    sm = SessionManager(session_path)
    return sm.update_sessions(module_list, module_settings, module_accounts)


def reconcile(
    bootstrap_result: BootstrapResult,
    current_settings: AppSettings,
    session_path: Path = SESSION_PATH,
) -> ReconcileResult:
    """Reconciles discovered plugins with current settings.

    This is the second initialization phase. It is a pure computation
    that merges module/extension defaults into the current settings,
    validates netlocation constants, and computes session updates.

    Args:
        bootstrap_result: Result from the bootstrap phase.
        current_settings: Current application settings.
        session_path: Path to the session storage file.

    Returns:
        ReconcileResult with merged settings and metadata.
    """
    # Filter modules
    module_list, module_settings = filter_modules(
        bootstrap_result.discovered_modules,
    )

    # Validate netlocation constants
    netloc_map = validate_netloc_constants(
        module_list, module_settings, current_settings
    )

    # Build extension list
    extension_list: set[str] = set(bootstrap_result.discovered_extensions.keys())

    # Sync extension settings (pure)
    after_ext, ext_new = sync_extension_settings_pure(
        extension_list,
        bootstrap_result.discovered_extensions,
        current_settings,
    )

    # Sync module settings (pure)
    after_mod, mod_new = sync_module_settings_pure(
        module_list, module_settings, after_ext
    )

    # Compute sessions
    sessions = _compute_sessions(
        module_list,
        module_settings,
        after_mod.modules,
        session_path,
    )

    return ReconcileResult(
        new_settings=after_mod,
        has_new_settings=ext_new or mod_new,
        sessions=sessions,
        registered_modules=module_list,
        module_settings=module_settings,
        module_netloc_constants=netloc_map,
        extension_list=extension_list,
        discovered_extensions=bootstrap_result.discovered_extensions,
    )


# =========================================================================
# Phase 3: Persist and check
# =========================================================================


def persist_and_check(
    reconcile_result: ReconcileResult,
    settings_path: Path = SETTINGS_PATH,
    session_path: Path = SESSION_PATH,
) -> bool:
    """Persists reconciled settings and sessions to disk.

    This is the third initialization phase — the only place where
    settings and sessions are written to disk.

    Args:
        reconcile_result: Result from the reconcile phase.
        settings_path: Path to the settings TOML file.
        session_path: Path to the session storage file.

    Returns:
        True if new settings were detected (caller should exit).
    """
    # Update global singleton
    set_settings(reconcile_result.new_settings)

    # Write settings.toml
    save_settings(settings_path, reconcile_result.new_settings)

    # Write loginstorage.json
    sm = SessionManager(session_path)
    sm.save_sessions(reconcile_result.sessions)

    return reconcile_result.has_new_settings
