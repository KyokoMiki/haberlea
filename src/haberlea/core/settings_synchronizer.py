"""Settings synchronization — pure computation functions.

These functions compute merged settings without performing I/O.
The actual persistence is handled by ``bootstrap.persist_and_check()``.
"""

import logging
from typing import Any

from haberlea.utils.models import ModuleInformation
from haberlea.utils.settings import AppSettings

logger = logging.getLogger(__name__)


def sync_extension_settings(
    extension_list: set[str],
    discovered_extensions: dict[str, Any],
    current_settings: AppSettings,
) -> tuple[AppSettings, bool]:
    """Merges extension defaults into current settings (pure function).

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


def sync_module_settings(
    module_list: set[str],
    module_settings: dict[str, ModuleInformation],
    current_settings: AppSettings,
) -> tuple[AppSettings, bool]:
    """Merges module defaults into current settings (pure function).

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
