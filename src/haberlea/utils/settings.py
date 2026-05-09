"""Settings data structures for Haberlea.

This module defines all settings as msgspec.Struct classes for type-safe
configuration management. Settings are organized hierarchically and support
TOML serialization/deserialization via msgspec.
"""

import secrets
from pathlib import Path
from typing import Any

import msgspec
import platformdirs

# Canonical configuration paths (single source of truth)
CONFIG_DIR: Path = Path(platformdirs.user_config_dir("Haberlea"))
SETTINGS_PATH: Path = CONFIG_DIR / "settings.toml"
SESSION_PATH: Path = CONFIG_DIR / "loginstorage.json"
NICEGUI_STORAGE_DIR: Path = CONFIG_DIR / "storage"

# =============================================================================
# Global Settings Structures
# =============================================================================


class RuntimeSettings(msgspec.Struct, kw_only=True):
    """Runtime environment settings — paths, concurrency, debug.

    Not consumed by the per-track download pipeline directly; used by
    bootstrap, search, logging, and queue concurrency control.

    Attributes:
        download_path: Default download directory path.
        temp_path: Temporary files directory path. Defaults to "" (empty string),
            which triggers fallback to the system temp directory.
        search_limit: Maximum number of search results to return.
        concurrent_downloads: Maximum concurrent track downloads.
        debug_mode: Enable debug logging.
    """

    download_path: str = "./downloads"
    temp_path: str = ""
    search_limit: int = 10
    concurrent_downloads: int = 5
    debug_mode: bool = False


class QualitySettings(msgspec.Struct, kw_only=True):
    """Audio quality and codec preferences.

    Consumed directly as the quality configuration of TrackDownloader and
    used to build CodecOptions/QualityEnum passed to modules.

    Attributes:
        tier: Audio quality tier (minimum/low/medium/high/lossless/hifi).
        spatial_codecs: Allow spatial audio codecs (Atmos, 360RA, etc.).
        proprietary_codecs: Allow proprietary codecs (MQA, Dolby, etc.).
        video_tier: Video quality tier (minimum/low/medium/high/max).
        video_container: Video container format (mp4/mkv).
    """

    tier: str = "hifi"
    spatial_codecs: bool = True
    proprietary_codecs: bool = False
    video_tier: str = "max"
    video_container: str = "mkv"


class ArtistDownloadingSettings(msgspec.Struct, kw_only=True):
    """Artist downloading behavior settings.

    Attributes:
        return_credited_albums: Include albums where artist is credited.
        separate_tracks_skip_downloaded: Skip tracks already downloaded in albums.
        ignore_different_artists: Skip tracks from different artists.
    """

    return_credited_albums: bool = True
    separate_tracks_skip_downloaded: bool = True
    ignore_different_artists: bool = True


class FormattingSettings(msgspec.Struct, kw_only=True):
    """File and folder naming format settings.

    Attributes:
        album_format: Format string for album folder names.
        playlist_format: Format string for playlist folder names.
        track_filename_format: Format string for track file names.
        single_full_path_format: Format string for single track downloads.
        video_format: Format string for music video file paths
            (relative to the base download path, no extension).
        enable_zfill: Zero-pad track numbers.
        force_album_format: Always use album format even for singles.
    """

    album_format: str = "{name}{explicit}"
    playlist_format: str = "{name}{explicit}"
    track_filename_format: str = "{track_number}. {name}"
    single_full_path_format: str = "{name}"
    video_format: str = "Videos/{artist} - {name}{explicit}"
    enable_zfill: bool = True
    force_album_format: bool = False


class ModuleDefaultsSettings(msgspec.Struct, kw_only=True):
    """Default module selection for various features.

    Attributes:
        lyrics: Default module for lyrics fetching.
        covers: Default module for cover art fetching.
        credits: Default module for credits fetching.
    """

    lyrics: str = "default"
    covers: str = "default"
    credits: str = "default"


class LyricsSettings(msgspec.Struct, kw_only=True):
    """Lyrics handling settings.

    Attributes:
        embed_lyrics: Embed plain lyrics in audio files.
        embed_synced_lyrics: Embed synced (timed) lyrics in audio files.
        save_synced_lyrics: Save synced lyrics as separate .lrc files.
    """

    embed_lyrics: bool = True
    embed_synced_lyrics: bool = False
    save_synced_lyrics: bool = True


class CoversSettings(msgspec.Struct, kw_only=True):
    """Cover art handling settings.

    Attributes:
        embed_cover: Embed cover art in audio files.
        compress_embed: If True, process the embedded cover with
            main_resolution + main_compression (jpg). If False, embed the
            raw downloaded cover with no processing.
        main_compression: Compression level for embedded covers (low/high).
        main_resolution: Resolution for embedded covers in pixels.
        save_external: Save cover art as separate files.
        compress_external: If True, compress external covers using the same
            settings as embedded covers (jpg + main_resolution +
            main_compression). If False, save the raw downloaded cover with
            no processing.
        save_animated_cover: Save animated covers when available.
        cover_variance_threshold: Threshold for cover image comparison.
    """

    embed_cover: bool = True
    compress_embed: bool = True
    main_compression: str = "high"
    main_resolution: int = 1400
    save_external: bool = True
    compress_external: bool = False
    save_animated_cover: bool = True
    cover_variance_threshold: int = 8


class PlaylistSettings(msgspec.Struct, kw_only=True):
    """Playlist export settings.

    Attributes:
        save_m3u: Generate M3U playlist files.
        extended_m3u: Use extended M3U format with metadata.
    """

    save_m3u: bool = True
    extended_m3u: bool = True


class DownloadBehaviorSettings(msgspec.Struct, kw_only=True):
    """Per-track download behavior control settings.

    Attributes:
        dry_run: Skip actual downloads, only collect track info.
        download_to_temp: Download to temporary directory first, then move to
            final location after tagging.
        force_redownload_existing: Force re-download and overwrite existing files.
        abort_download_when_single_failed: Stop on first track failure.
    """

    dry_run: bool = False
    download_to_temp: bool = True
    force_redownload_existing: bool = False
    abort_download_when_single_failed: bool = False


class WebuiSettings(msgspec.Struct, kw_only=True):
    """WebUI settings.

    Attributes:
        host: Server host address (empty string for all interfaces).
        port: Server port number.
        auth_enabled: Whether authentication is enabled.
        username: Login username.
        password: Login password.
        storage_secret: Secret key for session storage encryption.
        language: Interface language (zh_CN or en_US).
    """

    host: str = ""
    port: int = 7628
    auth_enabled: bool = False
    username: str = "admin"
    password: str = msgspec.field(default_factory=lambda: secrets.token_urlsafe(32))
    storage_secret: str = msgspec.field(
        default_factory=lambda: secrets.token_urlsafe(32)
    )
    language: str = "zh_CN"


class GlobalSettings(msgspec.Struct, kw_only=True):
    """Complete global settings container.

    Sections are organized to match how they are consumed by the download
    pipeline. Each section is passed to its corresponding collaborator
    without field-level repackaging.

    Attributes:
        runtime: Runtime environment (paths, concurrency, debug).
        quality: Audio quality and codec preferences.
        formatting: File/folder naming formats.
        covers: Cover art settings.
        lyrics: Lyrics handling settings.
        playlist: Playlist export settings (M3U).
        download_behavior: Per-track download behavior.
        artist_downloading: Artist downloading behavior.
        module_defaults: Default module selections.
        webui: WebUI settings.
    """

    runtime: RuntimeSettings = msgspec.field(default_factory=RuntimeSettings)
    quality: QualitySettings = msgspec.field(default_factory=QualitySettings)
    formatting: FormattingSettings = msgspec.field(default_factory=FormattingSettings)
    covers: CoversSettings = msgspec.field(default_factory=CoversSettings)
    lyrics: LyricsSettings = msgspec.field(default_factory=LyricsSettings)
    playlist: PlaylistSettings = msgspec.field(default_factory=PlaylistSettings)
    download_behavior: DownloadBehaviorSettings = msgspec.field(
        default_factory=DownloadBehaviorSettings
    )
    artist_downloading: ArtistDownloadingSettings = msgspec.field(
        default_factory=ArtistDownloadingSettings
    )
    module_defaults: ModuleDefaultsSettings = msgspec.field(
        default_factory=ModuleDefaultsSettings
    )
    webui: WebuiSettings = msgspec.field(default_factory=WebuiSettings)


# =============================================================================
# Application Settings Container
# =============================================================================


class AppSettings(msgspec.Struct, kw_only=True):
    """Complete application settings.

    Attributes:
        global_settings: Global settings (serialized as "global" in TOML).
        extensions: Extension-specific settings by type and name.
        modules: Module-specific settings by name, each module has a list of accounts.
    """

    global_settings: GlobalSettings = msgspec.field(
        default_factory=GlobalSettings, name="global"
    )
    extensions: dict[str, dict[str, dict[str, Any]]] = msgspec.field(
        default_factory=dict
    )
    modules: dict[str, list[dict[str, Any]]] = msgspec.field(default_factory=dict)


# =============================================================================
# Settings I/O Utilities
# =============================================================================


def load_settings(path: Path) -> AppSettings:
    """Loads settings from a TOML file.

    Args:
        path: Path to the settings TOML file.

    Returns:
        AppSettings instance. Returns defaults if file doesn't exist.
    """
    if not path.exists():
        return AppSettings()
    return msgspec.toml.decode(path.read_bytes(), type=AppSettings)


def save_settings(path: Path, settings: AppSettings) -> None:
    """Saves settings to a TOML file.

    Args:
        path: Path to save the settings file.
        settings: AppSettings instance to save.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = msgspec.toml.encode(settings)
    path.write_bytes(data)


def get_default_settings() -> AppSettings:
    """Creates default application settings.

    Returns:
        AppSettings instance with all default values.
    """
    return AppSettings()


# Global settings singleton
_app_settings: AppSettings | None = None


class _SettingsProxy:
    """Proxy class for lazy-loading global settings."""

    @property
    def current(self) -> AppSettings:
        """Gets the current global settings, loading if needed."""
        global _app_settings
        if _app_settings is None:
            _app_settings = load_settings(SETTINGS_PATH)
        return _app_settings

    @property
    def global_settings(self) -> GlobalSettings:
        """Gets the global settings section."""
        return self.current.global_settings

    @property
    def extensions(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Gets the extensions settings section."""
        return self.current.extensions

    @property
    def modules(self) -> dict[str, list[dict[str, Any]]]:
        """Gets the modules settings section."""
        return self.current.modules


settings = _SettingsProxy()


def set_settings(new_settings: AppSettings) -> None:
    """Sets the global settings directly.

    Use this to sync settings when they are modified elsewhere (e.g., by Haberlea).

    Args:
        new_settings: The new AppSettings instance.
    """
    global _app_settings
    _app_settings = new_settings


def reload_settings() -> AppSettings:
    """Reloads settings from the current path.

    Returns:
        The reloaded AppSettings instance.
    """
    global _app_settings
    _app_settings = load_settings(SETTINGS_PATH)
    return _app_settings
