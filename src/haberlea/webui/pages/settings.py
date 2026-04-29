"""Settings page for Haberlea WebUI."""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from nicegui import ui

from haberlea.i18n import SUPPORTED_LANGUAGES, _, ngettext, set_language
from haberlea.utils.settings import (
    SETTINGS_PATH,
    AppSettings,
    reload_settings,
    save_settings,
    set_settings,
    settings,
)
from haberlea.webui.state import get_haberlea


class SettingsPage:
    """Settings page component for configuring application options.

    Uses a local copy of settings for editing. Changes are only applied
    to the global settings singleton when the user clicks save.
    """

    def __init__(self) -> None:
        """Initializes the settings page."""
        self._current_tab: str = "general"
        # Local copy of settings for editing (not affecting global until save)
        self._edit_settings: AppSettings
        # Store UI references for dynamic updates
        self._account_containers: dict[str, ui.column] = {}
        self._account_cards: dict[str, list[ui.card]] = {}
        self._module_expansions: dict[str, ui.expansion] = {}

    def _load_edit_settings(self) -> AppSettings:
        """Loads a deep copy of current settings for editing.

        Returns:
            A deep copy of the current AppSettings.
        """
        self._edit_settings = deepcopy(settings.current)
        return self._edit_settings

    def render(self) -> None:
        """Renders the settings page."""
        # Load a copy of settings for editing
        self._load_edit_settings()

        with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
            ui.label(_("Settings")).classes("text-2xl font-bold")

            with ui.tabs().classes("w-full") as tabs:
                ui.tab("general", label=_("General"), icon="settings")
                ui.tab("output", label=_("Output"), icon="folder")
                ui.tab("behavior", label=_("Behavior"), icon="tune")
                ui.tab("webui", label="WebUI", icon="web")
                ui.tab("modules", label=_("Modules"), icon="extension")
                ui.tab("extensions", label=_("Extensions"), icon="power")

            with ui.tab_panels(tabs, value="general").classes("w-full"):
                with (
                    ui.tab_panel("general"),
                    ui.column().classes("w-full gap-4"),
                ):
                    self._render_runtime_settings()
                    self._render_quality_settings()

                with (
                    ui.tab_panel("output"),
                    ui.column().classes("w-full gap-4"),
                ):
                    self._render_formatting_settings()
                    self._render_cover_settings()
                    self._render_lyrics_settings()

                with (
                    ui.tab_panel("behavior"),
                    ui.column().classes("w-full gap-4"),
                ):
                    self._render_download_behavior_settings()
                    self._render_artist_downloading_settings()
                    self._render_playlist_settings()
                    self._render_module_defaults_settings()

                with ui.tab_panel("modules"):
                    self._render_module_settings()

                with ui.tab_panel("extensions"):
                    self._render_extension_settings()

                with ui.tab_panel("webui"):
                    self._render_webui_settings()

            # Save button
            with ui.row().classes("w-full justify-end"):
                ui.button(
                    _("Reload"), icon="refresh", on_click=self._reload_settings
                ).props("flat")
                ui.button(
                    _("Save Settings"), icon="save", on_click=self._save_settings
                ).props("color=primary")

    def _render_runtime_settings(self) -> None:
        """Renders runtime environment settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Runtime Options")).classes("text-lg font-semibold mb-4")

            ui.input(
                label=_("Download Path"),
                value=gs.runtime.download_path,
            ).classes("w-full mb-2").bind_value(gs.runtime, "download_path")

            ui.input(
                label=_("Temporary Files Path"),
                value=gs.runtime.temp_path,
                placeholder=_("Leave empty to use system temp directory"),
            ).classes("w-full mb-2").bind_value(gs.runtime, "temp_path")

            ui.number(
                label=_("Search Results Limit"),
                value=gs.runtime.search_limit,
                min=1,
                max=50,
            ).classes("w-48 mb-2").bind_value(gs.runtime, "search_limit")

            ui.number(
                label=_("Concurrent Downloads"),
                value=gs.runtime.concurrent_downloads,
                min=1,
                max=10,
            ).classes("w-48 mb-4").bind_value(gs.runtime, "concurrent_downloads")

            ui.checkbox(
                _("Debug Mode"),
                value=gs.runtime.debug_mode,
            ).bind_value(gs.runtime, "debug_mode")

    def _render_quality_settings(self) -> None:
        """Renders audio quality and codec preferences section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Quality Options")).classes("text-lg font-semibold mb-4")

            ui.select(
                label=_("Download Quality"),
                options=["minimum", "low", "medium", "high", "lossless", "hifi"],
                value=gs.quality.tier,
            ).classes("w-48 mb-2").bind_value(gs.quality, "tier")

            ui.checkbox(
                _("Enable Spatial Audio Codecs (Dolby Atmos, etc.)"),
                value=gs.quality.spatial_codecs,
            ).bind_value(gs.quality, "spatial_codecs")

            ui.checkbox(
                _("Enable Proprietary Codecs (MQA, etc.)"),
                value=gs.quality.proprietary_codecs,
            ).bind_value(gs.quality, "proprietary_codecs")

    def _render_artist_downloading_settings(self) -> None:
        """Renders artist downloading behavior section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Artist Downloading")).classes("text-lg font-semibold mb-4")

            ui.checkbox(
                _("Return Credited Albums"),
                value=gs.artist_downloading.return_credited_albums,
            ).bind_value(gs.artist_downloading, "return_credited_albums")

            ui.checkbox(
                _("Skip Downloaded Separate Tracks"),
                value=gs.artist_downloading.separate_tracks_skip_downloaded,
            ).bind_value(gs.artist_downloading, "separate_tracks_skip_downloaded")

            ui.checkbox(
                _("Ignore Different Artists"),
                value=gs.artist_downloading.ignore_different_artists,
            ).bind_value(gs.artist_downloading, "ignore_different_artists")

    def _render_module_defaults_settings(self) -> None:
        """Renders default third-party module selections section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Module Defaults")).classes("text-lg font-semibold mb-4")

            modules = self._get_available_modules()
            module_options = ["default"] + modules

            ui.select(
                label=_("Lyrics Source"),
                options=module_options,
                value=gs.module_defaults.lyrics,
            ).classes("w-48 mb-2").bind_value(gs.module_defaults, "lyrics")

            ui.select(
                label=_("Covers Source"),
                options=module_options,
                value=gs.module_defaults.covers,
            ).classes("w-48 mb-2").bind_value(gs.module_defaults, "covers")

            ui.select(
                label=_("Credits Source"),
                options=module_options,
                value=gs.module_defaults.credits,
            ).classes("w-48").bind_value(gs.module_defaults, "credits")

    def _render_playlist_settings(self) -> None:
        """Renders M3U playlist export settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Playlist Settings")).classes("text-lg font-semibold mb-4")

            ui.checkbox(
                _("Save M3U Playlist"),
                value=gs.playlist.save_m3u,
            ).bind_value(gs.playlist, "save_m3u")

            ui.checkbox(
                _("Extended M3U Format"),
                value=gs.playlist.extended_m3u,
            ).bind_value(gs.playlist, "extended_m3u")

    def _render_formatting_settings(self) -> None:
        """Renders formatting settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("File Naming Format")).classes("text-lg font-semibold mb-4")
            ui.label(
                _("Available variables: {name}, {artist}, {track_number}, ...")
            ).classes("text-sm text-gray-500 mb-4")

            ui.input(
                label=_("Album Folder Format"),
                value=gs.formatting.album_format,
            ).classes("w-full mb-2").bind_value(gs.formatting, "album_format")

            ui.input(
                label=_("Playlist Folder Format"),
                value=gs.formatting.playlist_format,
            ).classes("w-full mb-2").bind_value(gs.formatting, "playlist_format")

            ui.input(
                label=_("Track Filename Format"),
                value=gs.formatting.track_filename_format,
            ).classes("w-full mb-2").bind_value(gs.formatting, "track_filename_format")

            ui.input(
                label=_("Single Full Path Format"),
                value=gs.formatting.single_full_path_format,
            ).classes("w-full mb-4").bind_value(
                gs.formatting, "single_full_path_format"
            )

            ui.checkbox(
                _("Enable Zero Padding"),
                value=gs.formatting.enable_zfill,
            ).bind_value(gs.formatting, "enable_zfill")

            ui.checkbox(
                _("Force Album Format"),
                value=gs.formatting.force_album_format,
            ).bind_value(gs.formatting, "force_album_format")

    def _render_cover_settings(self) -> None:
        """Renders cover settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Cover Settings")).classes("text-lg font-semibold mb-4")

            ui.checkbox(
                _("Embed Cover"),
                value=gs.covers.embed_cover,
            ).bind_value(gs.covers, "embed_cover")

            ui.checkbox(
                _("Compress Embedded Cover"),
                value=gs.covers.compress_embed,
            ).bind_value(gs.covers, "compress_embed")

            ui.select(
                label=_("Main Cover Compression"),
                options=["low", "high"],
                value=gs.covers.main_compression,
            ).classes("w-32 mb-2").bind_value(gs.covers, "main_compression")

            ui.number(
                label=_("Main Cover Resolution"),
                value=gs.covers.main_resolution,
                min=100,
                max=5000,
            ).classes("w-48 mb-4").bind_value(gs.covers, "main_resolution")

            ui.checkbox(
                _("Save External Cover"),
                value=gs.covers.save_external,
            ).bind_value(gs.covers, "save_external")

            ui.checkbox(
                _("Compress External Covers"),
                value=gs.covers.compress_external,
            ).bind_value(gs.covers, "compress_external")

            ui.checkbox(
                _("Save Animated Cover"),
                value=gs.covers.save_animated_cover,
            ).bind_value(gs.covers, "save_animated_cover")

            ui.number(
                label=_("Cover Variance Threshold"),
                value=gs.covers.cover_variance_threshold,
                min=0,
                max=100,
            ).classes("w-48 mb-4").bind_value(gs.covers, "cover_variance_threshold")

    def _render_lyrics_settings(self) -> None:
        """Renders lyrics settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Lyrics Settings")).classes("text-lg font-semibold mb-4")

            ui.checkbox(
                _("Embed Lyrics"),
                value=gs.lyrics.embed_lyrics,
            ).bind_value(gs.lyrics, "embed_lyrics")

            ui.checkbox(
                _("Embed Synced Lyrics"),
                value=gs.lyrics.embed_synced_lyrics,
            ).bind_value(gs.lyrics, "embed_synced_lyrics")

            ui.checkbox(
                _("Save Synced Lyrics File (.lrc)"),
                value=gs.lyrics.save_synced_lyrics,
            ).bind_value(gs.lyrics, "save_synced_lyrics")

    def _render_download_behavior_settings(self) -> None:
        """Renders download behavior settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("Download Behavior")).classes("text-lg font-semibold mb-4")

            ui.checkbox(
                _("Dry Run (Collect info only, no download)"),
                value=gs.download_behavior.dry_run,
            ).bind_value(gs.download_behavior, "dry_run")

            ui.checkbox(
                _("Download to Temp Directory First"),
                value=gs.download_behavior.download_to_temp,
            ).bind_value(gs.download_behavior, "download_to_temp")

            ui.checkbox(
                _("Force Re-download Existing Files"),
                value=gs.download_behavior.force_redownload_existing,
            ).bind_value(gs.download_behavior, "force_redownload_existing")

            ui.checkbox(
                _("Abort Download When Single Failed"),
                value=gs.download_behavior.abort_download_when_single_failed,
            ).bind_value(gs.download_behavior, "abort_download_when_single_failed")

    def _render_webui_settings(self) -> None:
        """Renders WebUI settings section."""
        gs = self._edit_settings.global_settings

        with ui.card().classes("w-full"):
            ui.label(_("WebUI Settings")).classes("text-lg font-semibold mb-4")

            ui.label(_("Server Settings")).classes(
                "text-md font-medium text-gray-600 mb-2"
            )

            ui.input(
                label=_("Host Address"),
                value=gs.webui.host,
                placeholder=_("Leave empty to listen on all interfaces (0.0.0.0)"),
            ).classes("w-full mb-2").bind_value(gs.webui, "host")

            ui.number(
                label=_("Port"),
                value=gs.webui.port,
                min=1,
                max=65535,
            ).classes("w-48 mb-4").bind_value(gs.webui, "port")

            ui.label(_("Authentication Settings")).classes(
                "text-md font-medium text-gray-600 mb-2 mt-4"
            )

            ui.checkbox(
                _("Enable Login Authentication"),
                value=gs.webui.auth_enabled,
            ).bind_value(gs.webui, "auth_enabled")

            ui.input(
                _("Username"),
                value=gs.webui.username,
            ).classes("w-full mb-2").bind_value(gs.webui, "username")

            ui.input(
                _("Password"),
                value=gs.webui.password,
                password=True,
                password_toggle_button=True,
            ).classes("w-full mb-4").bind_value(gs.webui, "password")

            ui.label(_("Language Settings")).classes(
                "text-md font-medium text-gray-600 mb-2 mt-4"
            )

            # Language selector with change handler
            language_names = {
                "zh_CN": "简体中文",
                "en_US": "English",
            }

            def on_language_change(e: object) -> None:
                """Handle language change."""
                new_lang = getattr(e, "value", gs.webui.language)
                gs.webui.language = new_lang
                set_language(new_lang)
                ui.notify(
                    _("Language will be applied after saving and refreshing"),
                    type="info",
                )

            ui.select(
                label=_("Interface Language"),
                options={lang: language_names[lang] for lang in SUPPORTED_LANGUAGES},
                value=gs.webui.language,
                on_change=on_language_change,
            ).classes("w-48")

    def _render_module_settings(self) -> None:
        """Renders module-specific settings section.

        Supports multi-account structure where each module has a list of accounts.
        Provides add/delete account functionality with dynamic UI updates.
        """
        modules = self._edit_settings.modules

        if not modules:
            with ui.card().classes("w-full"):
                ui.label(_("No configured modules")).classes("text-gray-500")
            return

        for module_name, accounts in modules.items():
            with ui.expansion(
                f"{module_name.upper()} ({len(accounts)} "
                f"{ngettext('account', 'accounts', len(accounts))})",
                icon="extension",
            ).classes("w-full mb-2") as expansion:
                # Store expansion reference for updating title later
                self._module_expansions[module_name] = expansion

                # Container for account cards
                account_container = ui.column().classes("w-full")
                self._account_containers[module_name] = account_container
                self._account_cards[module_name] = []

                with account_container:
                    for account_index, account_config in enumerate(accounts):
                        self._create_account_card(
                            module_name, account_index, account_config
                        )

                # Action buttons row
                with ui.row().classes("mt-2 gap-2"):
                    ui.button(
                        _("Add Account"),
                        icon="add",
                        on_click=lambda _, m=module_name: self._add_account(m),
                    ).props("flat")
                    ui.button(
                        _("Re-login"),
                        icon="refresh",
                        on_click=lambda _, m=module_name: self._clear_module_session(m),
                    ).props("flat color=warning").tooltip(
                        _("Clear login data, will need to re-login on next use")
                    )

    def _render_account_fields(
        self, module_name: str, account_index: int, account_config: dict[str, Any]
    ) -> None:
        """Renders input fields for a single account configuration.

        Renders built-in fields (name, region) first with translated labels,
        then module-specific fields.

        Args:
            module_name: Name of the module.
            account_index: Index of the account in the accounts list.
            account_config: Account configuration dictionary.
        """
        builtin_keys = ("name", "region")
        builtin_labels = {
            "name": _("Display Name"),
            "region": _("Region Tag"),
        }

        # Render builtin fields first
        for key in builtin_keys:
            if key in account_config:
                ui.input(
                    label=builtin_labels.get(key, key),
                    value=str(account_config[key]) if account_config[key] else "",
                    on_change=lambda e, m=module_name, idx=account_index, k=key: (
                        self._set_module_account_value(m, idx, k, e.value)
                    ),
                ).classes("w-full mb-2")

        # Render module-specific fields
        for key, value in account_config.items():
            if key in builtin_keys:
                continue
            if isinstance(value, bool):
                ui.checkbox(
                    key,
                    value=value,
                    on_change=lambda e, m=module_name, idx=account_index, k=key: (
                        self._set_module_account_value(m, idx, k, e.value)
                    ),
                )
            elif isinstance(value, (int, float)):
                ui.number(
                    label=key,
                    value=value,
                    on_change=lambda e, m=module_name, idx=account_index, k=key: (
                        self._set_module_account_value(m, idx, k, e.value)
                    ),
                ).classes("w-full mb-2")
            else:
                # Password fields
                is_password = (
                    "password" in key.lower()
                    or "secret" in key.lower()
                    or "arl" in key.lower()
                )
                ui.input(
                    label=key,
                    value=str(value) if value else "",
                    password=is_password,
                    password_toggle_button=is_password,
                    on_change=lambda e, m=module_name, idx=account_index, k=key: (
                        self._set_module_account_value(m, idx, k, e.value)
                    ),
                ).classes("w-full mb-2")

    def _create_account_card(
        self, module_name: str, account_index: int, account_config: dict[str, Any]
    ) -> ui.card:
        """Creates a single account card with fields and delete button.

        Args:
            module_name: Name of the module.
            account_index: Index of the account in the accounts list.
            account_config: Account configuration dictionary.

        Returns:
            The created ui.card element.
        """
        card = ui.card().classes("w-full mb-2")
        with card:
            account_name = account_config.get("name", "")
            account_region = account_config.get("region", "")
            if account_name:
                title = f"{_('Account')} {account_index + 1} - {account_name}"
            else:
                title = f"{_('Account')} {account_index + 1}"

            with ui.row().classes("w-full justify-between items-center"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(title).classes("text-sm font-semibold text-gray-600")
                    if account_region:
                        ui.badge(account_region, color="blue").props("outline")
                with ui.row().classes("items-center gap-1"):
                    if self._get_autofill_parser(module_name) is not None:
                        ui.button(
                            icon="auto_fix_high",
                            on_click=lambda _, m=module_name, idx=account_index: (
                                self._open_autofill_dialog(m, idx)
                            ),
                        ).props("flat dense color=primary").tooltip(
                            _("Auto-fill from pasted text")
                        )
                    ui.button(
                        icon="delete",
                        on_click=lambda _, m=module_name, idx=account_index: (
                            self._delete_account(m, idx)
                        ),
                    ).props("flat dense color=negative").tooltip(
                        _("Delete this account")
                    )
            self._render_account_fields(module_name, account_index, account_config)

        self._account_cards[module_name].append(card)
        return card

    def _set_module_account_value(
        self, module_name: str, account_index: int, key: str, value: object
    ) -> None:
        """Sets a value in a module's account configuration.

        Args:
            module_name: Name of the module.
            account_index: Index of the account in the accounts list.
            key: Configuration key to set.
            value: Value to set.
        """
        modules = self._edit_settings.modules
        if module_name in modules and account_index < len(modules[module_name]):
            modules[module_name][account_index][key] = value

    def _add_account(self, module_name: str) -> None:
        """Adds a new empty account to the specified module.

        Dynamically creates a new account card without page reload.
        Only updates edit settings, does not save to file.

        Args:
            module_name: Name of the module to add account to.
        """
        modules = self._edit_settings.modules
        if module_name not in modules:
            return

        accounts = modules[module_name]
        if not accounts:
            return

        # Create new account with same keys as first account but empty values
        template = accounts[0]
        new_account: dict[str, object] = {}
        for key, value in template.items():
            if isinstance(value, bool):
                new_account[key] = False
            elif isinstance(value, (int, float)):
                new_account[key] = 0
            else:
                new_account[key] = ""

        # Ensure builtin fields are always present
        new_account.setdefault("name", "")
        new_account.setdefault("region", "")

        accounts.append(new_account)
        new_index = len(accounts) - 1

        # Dynamically add new account card to the container
        container = self._account_containers.get(module_name)
        if container is not None:
            with container:
                self._create_account_card(module_name, new_index, new_account)

    def _delete_account(self, module_name: str, account_index: int) -> None:
        """Deletes an account from the specified module.

        Dynamically removes the account card without page reload.
        Only updates edit settings, does not save to file.

        Args:
            module_name: Name of the module.
            account_index: Index of the account to delete.
        """
        modules = self._edit_settings.modules
        if module_name not in modules:
            return

        accounts = modules[module_name]
        if len(accounts) <= 1:
            ui.notify(_("At least one account must be kept"), type="warning")
            return

        if account_index < len(accounts):
            accounts.pop(account_index)
            self._rebuild_account_cards(module_name)

            # Update expansion title
            expansion = self._module_expansions.get(module_name)
            if expansion is not None:
                expansion.props(
                    f'label="{module_name.upper()} ({len(accounts)} '
                    f'{ngettext("account", "accounts", len(accounts))})"'
                )

            ui.notify(_("Account deleted"), type="positive")

    def _rebuild_account_cards(self, module_name: str) -> None:
        """Rebuilds all account cards for a module from current edit settings.

        Args:
            module_name: Name of the module whose cards should be rebuilt.
        """
        accounts = self._edit_settings.modules.get(module_name, [])
        for card in self._account_cards.get(module_name, []):
            card.delete()
        self._account_cards[module_name] = []

        container = self._account_containers.get(module_name)
        if container is None:
            return
        with container:
            for idx, account_config in enumerate(accounts):
                self._create_account_card(module_name, idx, account_config)

    def _get_autofill_parser(
        self, module_name: str
    ) -> Callable[[str], dict[str, str]] | None:
        """Returns the auto-fill text parser for a module, if any.

        Looks up the parser declared on the module's ``ModuleInformation``
        via the registry. Returns ``None`` when the registry is unavailable
        or the module did not register a parser.

        Args:
            module_name: Name of the module.

        Returns:
            The parser callable, or ``None`` when none is configured.
        """
        try:
            haberlea = get_haberlea()
        except Exception:
            return None
        info = haberlea.module_registry.state.module_settings.get(module_name)
        if info is None:
            return None
        return info.account_autofill_parser

    def _open_autofill_dialog(self, module_name: str, account_index: int) -> None:
        """Opens a dialog to auto-fill account fields from pasted text.

        Args:
            module_name: Name of the module.
            account_index: Index of the account to fill.
        """
        modules = self._edit_settings.modules
        if module_name not in modules or account_index >= len(modules[module_name]):
            return

        with ui.dialog() as dialog, ui.card().classes("w-[32rem]"):
            ui.label(_("Auto-fill account from text")).classes("text-lg font-semibold")
            ui.label(
                _(
                    "Paste account info. Recognized labels: "
                    "Token, User ID, Email, Region, App ID, App Secret, Name."
                )
            ).classes("text-sm text-gray-500")
            textarea = ui.textarea().classes("w-full").props("rows=10 outlined")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dialog.close).props("flat")
                ui.button(
                    _("Apply"),
                    on_click=lambda: self._apply_autofill(
                        module_name, account_index, textarea.value or "", dialog
                    ),
                ).props("color=primary")
        dialog.open()

    def _apply_autofill(
        self,
        module_name: str,
        account_index: int,
        text: str,
        dialog: ui.dialog,
    ) -> None:
        """Parses pasted text and applies recognized fields to the account.

        Args:
            module_name: Name of the module.
            account_index: Index of the account to fill.
            text: Pasted text to parse.
            dialog: The dialog instance to close on success.
        """
        modules = self._edit_settings.modules
        if module_name not in modules or account_index >= len(modules[module_name]):
            return

        parser = self._get_autofill_parser(module_name)
        if parser is None:
            return

        account_config = modules[module_name][account_index]
        valid_keys = set(account_config.keys())
        parsed = {k: v for k, v in parser(text).items() if k in valid_keys}
        if not parsed:
            ui.notify(_("No recognizable fields found"), type="warning")
            return

        for key, value in parsed.items():
            account_config[key] = value

        self._rebuild_account_cards(module_name)
        dialog.close()
        ui.notify(
            _("Auto-filled: {fields}").format(fields=", ".join(parsed.keys())),
            type="positive",
        )

    def _clear_module_session(self, module_name: str) -> None:
        """Clears session data for a module, forcing re-login.

        Args:
            module_name: Name of the module to clear session for.
        """
        haberlea = get_haberlea()
        if haberlea.clear_module_session(module_name):
            ui.notify(
                _("Login data cleared, will re-login on next use"),
                type="positive",
            )
        else:
            ui.notify(_("Failed to clear login data"), type="negative")

    def _get_available_modules(self) -> list[str]:
        """Gets list of available modules.

        Returns:
            List of module names.
        """
        return list(self._edit_settings.modules.keys())

    def _save_settings(self) -> None:
        """Saves edit settings to file and updates global singleton."""
        set_settings(self._edit_settings)
        save_settings(SETTINGS_PATH, self._edit_settings)
        ui.notify(_("Settings saved"), type="positive")

    def _reload_settings(self) -> None:
        """Reloads settings from file and refreshes the page."""
        reload_settings()
        ui.notify(_("Settings reloaded"), type="info")
        ui.navigate.reload()

    def _render_extension_settings(self) -> None:
        """Renders extension settings section.

        Extensions are grouped by type (e.g., post_download).
        Each extension has its own settings card.
        """
        extensions = self._edit_settings.extensions

        if not extensions:
            with ui.card().classes("w-full"):
                ui.label(_("No installed extensions")).classes("text-gray-500")
            return

        for ext_type, ext_configs in extensions.items():
            if not ext_configs:
                continue

            # Extension type header
            type_labels: dict[str, str] = {
                "post_download": _("Post-Download Processing"),
            }
            type_label: str = type_labels.get(ext_type) or ext_type

            ui.label(type_label).classes("text-lg font-semibold mt-4 mb-2")

            for ext_name, ext_settings in ext_configs.items():
                self._render_extension_card(ext_type, ext_name, ext_settings)

    def _render_extension_card(
        self, ext_type: str, ext_name: str, ext_settings: dict[str, Any]
    ) -> None:
        """Renders a single extension settings card.

        Args:
            ext_type: Extension type (e.g., post_download).
            ext_name: Extension name.
            ext_settings: Extension settings dictionary.
        """
        with (
            ui.expansion(
                f"{ext_name.upper()}",
                icon="power",
            ).classes("w-full mb-2"),
            ui.card().classes("w-full"),
        ):
            for key, value in ext_settings.items():
                self._render_extension_field(ext_type, ext_name, key, value)

    def _render_extension_field(
        self, ext_type: str, ext_name: str, key: str, value: object
    ) -> None:
        """Renders a single extension setting field.

        Args:
            ext_type: Extension type.
            ext_name: Extension name.
            key: Setting key.
            value: Setting value.
        """
        # Field labels
        field_labels = {
            "priority": _("Priority"),
            "rar_enabled": _("Enable RAR Compression"),
            "rar_path": _("RAR Executable Path"),
            "compression_level": _("Compression Level (0-5)"),
            "delete_after_upload": _("Delete archive and source files after upload"),
            "password": _("Compression Password"),
            "upload_enabled": _("Enable Baidu Netdisk Upload"),
            "baidupcs_path": _("BaiduPCS-Go Path"),
            "upload_path": _("Upload Target Path"),
        }
        label = field_labels.get(key, key)

        if isinstance(value, bool):
            ui.checkbox(
                label,
                value=value,
                on_change=lambda e, t=ext_type, n=ext_name, k=key: (
                    self._set_extension_value(t, n, k, e.value)
                ),
            )
        elif isinstance(value, int):
            ui.number(
                label=label,
                value=value,
                on_change=lambda e, t=ext_type, n=ext_name, k=key: (
                    self._set_extension_value(t, n, k, int(e.value or 0))
                ),
            ).classes("w-full mb-2")
        else:
            is_password = "password" in key.lower()
            ui.input(
                label=label,
                value=str(value) if value else "",
                password=is_password,
                password_toggle_button=is_password,
                on_change=lambda e, t=ext_type, n=ext_name, k=key: (
                    self._set_extension_value(t, n, k, e.value)
                ),
            ).classes("w-full mb-2")

    def _set_extension_value(
        self, ext_type: str, ext_name: str, key: str, value: object
    ) -> None:
        """Sets a value in an extension's configuration.

        Args:
            ext_type: Extension type.
            ext_name: Extension name.
            key: Configuration key to set.
            value: Value to set.
        """
        extensions = self._edit_settings.extensions
        if ext_type in extensions and ext_name in extensions[ext_type]:
            extensions[ext_type][ext_name][key] = value
