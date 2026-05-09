from collections.abc import Callable
from enum import Enum, Flag, auto
from typing import TYPE_CHECKING, Any

import msgspec

from .exceptions import InvalidInput
from .utils import read_temporary_setting, set_temporary_setting

if TYPE_CHECKING:
    from haberlea.plugins.base import ExtensionBase, WebUIPageBase


class EncoderEnum(Enum):
    LIBFLAC = "libflac"  # Lossless, free
    LAVF = "lavf"
    MUTAGEN = "mutagen"


class ContainerEnum(Enum):
    flac = "flac"
    wav = "wav"
    opus = "opus"
    ogg = "ogg"
    m4a = "m4a"
    mp3 = "mp3"


class CodecData(msgspec.Struct, frozen=True):
    """Audio codec metadata.

    Attributes:
        pretty_name: Display name for the codec.
        container: Container format for the codec.
        lossless: Whether the codec is lossless.
        spatial: Whether the codec supports spatial audio.
        proprietary: Whether the codec is proprietary.
    """

    pretty_name: str
    container: ContainerEnum
    lossless: bool
    spatial: bool
    proprietary: bool


class CodecEnum(Enum):
    """Audio codec enumeration with embedded metadata.

    Each codec contains its display name, container format, and codec properties.
    Note: spatial has priority over proprietary when deciding if a codec is enabled.

    Access codec data via .value attribute:
        CodecEnum.FLAC.value.pretty_name  # "FLAC"
        CodecEnum.FLAC.value.lossless     # True
    """

    FLAC = CodecData("FLAC", ContainerEnum.flac, True, False, False)
    ALAC = CodecData("ALAC", ContainerEnum.m4a, True, False, False)
    WAV = CodecData("WAVE", ContainerEnum.wav, True, False, False)
    MQA = CodecData("MQA", ContainerEnum.flac, False, False, True)
    OPUS = CodecData("Opus", ContainerEnum.opus, False, False, False)
    VORBIS = CodecData("Vorbis", ContainerEnum.ogg, False, False, False)
    MP3 = CodecData("MP3", ContainerEnum.mp3, False, False, False)
    AAC = CodecData("AAC-LC", ContainerEnum.m4a, False, False, False)
    HEAAC = CodecData("HE-AAC", ContainerEnum.m4a, False, False, False)
    MHA1 = CodecData("MPEG-H 3D (MHA1)", ContainerEnum.m4a, False, True, False)
    MHM1 = CodecData("MPEG-H 3D (MHM1)", ContainerEnum.m4a, False, True, False)
    EAC3 = CodecData("E-AC-3 JOC", ContainerEnum.m4a, False, True, True)
    AC4 = CodecData("AC-4 IMS", ContainerEnum.m4a, False, True, True)
    AC3 = CodecData("Dolby Digital", ContainerEnum.m4a, False, True, True)
    NONE = CodecData("Error", ContainerEnum.m4a, False, False, False)

    @property
    def pretty_name(self) -> str:
        """Get the display name of the codec."""
        return self.value.pretty_name

    @property
    def container(self) -> ContainerEnum:
        """Get the container format of the codec."""
        return self.value.container

    @property
    def lossless(self) -> bool:
        """Check if the codec is lossless."""
        return self.value.lossless

    @property
    def spatial(self) -> bool:
        """Check if the codec supports spatial audio."""
        return self.value.spatial

    @property
    def proprietary(self) -> bool:
        """Check if the codec is proprietary."""
        return self.value.proprietary


class SearchResult(msgspec.Struct, kw_only=True):
    """Search result from a music service."""

    result_id: str
    name: str | None = None
    artists: list[str] | None = None
    year: str | None = None
    explicit: bool = False
    duration: int | None = None  # Duration in whole seconds
    additional: list[str] | None = None
    data: dict[str, Any] | None = None


class DownloadEnum(Enum):
    URL = "url"
    DIRECT = "direct"  # Downloaded directly to target path
    MPD = "mpd"


class TemporarySettingsController:
    """Controller for managing temporary module settings.

    Provides read/write access to temporary settings stored in JSON files,
    supporting custom, global, and JWT setting types. Supports multi-account
    by using account_index to select which session to use.
    """

    def __init__(
        self, module: str, settings_location: str, account_index: int = 0
    ) -> None:
        """Initializes the temporary settings controller.

        Args:
            module: The module name.
            settings_location: Path to the settings JSON file.
            account_index: Index of the account to use (for multi-account support).
        """
        self.module = module
        self.settings_location = settings_location
        self.account_index = account_index

    def _get_session_name(self) -> str:
        """Gets the session name for the current account index.

        Returns:
            Session name string (e.g., "default", "account_1", "account_2").
        """
        if self.account_index == 0:
            return "default"
        return f"account_{self.account_index}"

    def read(self, setting: str, setting_type: str = "custom") -> Any:
        """Reads a temporary setting.

        Args:
            setting: The setting name to read.
            setting_type: Type of setting ("custom", "global", or "jwt").

        Returns:
            The setting value.

        Raises:
            InvalidInput: If an invalid setting type is requested.
        """
        if setting_type == "custom":
            return read_temporary_setting(
                self.settings_location,
                self.module,
                "custom_data",
                setting,
                session_name=self._get_session_name(),
            )
        elif setting_type == "global":
            return read_temporary_setting(
                self.settings_location,
                self.module,
                "custom_data",
                setting,
                global_mode=True,
            )
        elif setting_type == "jwt" and (setting == "bearer" or setting == "refresh"):
            return read_temporary_setting(
                self.settings_location,
                self.module,
                setting,
                None,
                session_name=self._get_session_name(),
            )
        else:
            raise InvalidInput(
                f"Invalid temporary setting type: {setting_type}",
                field="setting_type",
                value=setting_type,
            )

    def set(
        self, setting: str, value: str | object, setting_type: str = "custom"
    ) -> None:
        """Sets a temporary setting.

        Args:
            setting: The setting name to set.
            value: The value to set.
            setting_type: Type of setting ("custom", "global", or "jwt").

        Raises:
            InvalidInput: If an invalid setting type is requested.
        """
        if setting_type == "custom":
            set_temporary_setting(
                self.settings_location,
                self.module,
                "custom_data",
                setting,
                value,
                session_name=self._get_session_name(),
            )
        elif setting_type == "global":
            set_temporary_setting(
                self.settings_location,
                self.module,
                "custom_data",
                setting,
                value,
                global_mode=True,
            )
        elif setting_type == "jwt" and (setting == "bearer" or setting == "refresh"):
            set_temporary_setting(
                self.settings_location,
                self.module,
                setting,
                None,
                value,
                session_name=self._get_session_name(),
            )
        else:
            raise InvalidInput(
                f"Invalid temporary setting type: {setting_type}",
                field="setting_type",
                value=setting_type,
            )

    def read_session(self) -> dict | None:
        """Reads the complete session data for the current account.

        Returns:
            The session dictionary, or None if not found.
        """
        return read_temporary_setting(
            self.settings_location,
            self.module,
            session_name=self._get_session_name(),
        )

    def set_raw(self, root_setting: str, value: Any) -> None:
        """Sets a root-level setting directly (not nested under custom_data).

        Args:
            root_setting: The root setting key to set.
            value: The value to set.
        """
        set_temporary_setting(
            self.settings_location,
            self.module,
            root_setting,
            None,
            value,
            session_name=self._get_session_name(),
        )


class ModuleFlags(Flag):
    startup_load = auto()
    hidden = auto()
    enable_jwt_system = auto()
    private = auto()
    uses_data = auto()
    needs_cover_resize = auto()


class ModuleModes(Flag):
    download = auto()
    playlist = auto()
    lyrics = auto()
    credits = auto()
    covers = auto()


class ManualEnum(Enum):
    haberlea = "haberlea"
    manual = "manual"


class ModuleInformation(msgspec.Struct, kw_only=True):
    """Module configuration and metadata."""

    service_name: str
    module_supported_modes: ModuleModes
    global_settings: dict[str, Any] = msgspec.field(default_factory=dict)
    global_storage_variables: list[str] = msgspec.field(default_factory=list)
    session_settings: dict[str, Any] = msgspec.field(default_factory=dict)
    session_storage_variables: list[str] = msgspec.field(default_factory=list)
    flags: ModuleFlags = ModuleFlags(0)
    netlocation_constant: str | list[str] | None = None
    # note that by setting netlocation_constant to setting.X,
    # it will use that setting instead
    url_constants: dict[str, Any] = msgspec.field(default_factory=dict)
    test_url: str | None = None
    url_decoding: ManualEnum = ManualEnum.haberlea
    login_behaviour: ManualEnum = ManualEnum.haberlea
    max_concurrent_downloads: int | None = None
    # Per-module cap on concurrent track downloads. ``None`` means no extra
    # limit beyond the global concurrency setting. Use this for modules that
    # internally parallelise a single track download (e.g. TIDAL).
    account_autofill_parser: Callable[[str], dict[str, str]] | None = None
    # Optional parser that converts pasted account-share text into a mapping
    # of account-config keys to values. When set, the WebUI exposes an
    # auto-fill button on each account card for this module.


class ExtensionInformation(msgspec.Struct):
    """Extension configuration information.

    Attributes:
        extension_type: Type of extension (e.g., "post_download", "search").
        settings: Default settings for the extension.
        webui_page: Optional WebUI page class (must inherit from WebUIPageBase).
    """

    extension_type: str
    settings: dict[str, Any]
    webui_page: "type[WebUIPageBase] | None" = None


class ExtensionInstance(msgspec.Struct):
    """Loaded extension instance with metadata.

    Attributes:
        name: Extension name.
        priority: Execution priority (lower numbers run first).
        instance: The instantiated extension object.
    """

    name: str
    priority: int
    instance: "ExtensionBase"


class DownloadTypeEnum(Enum):
    track = "track"
    playlist = "playlist"
    artist = "artist"
    album = "album"
    video = "video"


class MediaKindEnum(Enum):
    """Execution-level media kind for a download task.

    Distinguishes how a single ``TrackTask`` should be processed:
    audio downloads go through tagging/cover/lyrics/finalize; videos
    fetch HLS segments and remux directly to the configured container.
    """

    audio = "audio"
    video = "video"


class MediaIdentification(msgspec.Struct, kw_only=True):
    """Media identification for download requests.

    Attributes:
        media_type: Type of media (track, album, playlist, artist).
        media_id: The media identifier from the service.
        original_url: The original URL that initiated this download request.
        url_region: Region hint extracted from the URL path (e.g., "us-en").
    """

    media_type: DownloadTypeEnum
    media_id: str
    original_url: str = ""
    url_region: str = ""


class QualityEnum(Enum):
    MINIMUM = "minimum"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    LOSSLESS = "lossless"
    HIFI = "hifi"


class VideoQualityEnum(Enum):
    """Video quality tier (5 tiers parallel to QualityEnum).

    Modules map these to concrete vertical resolutions and pick the
    closest available HLS variant.
    """

    MINIMUM = "minimum"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class VideoContainerEnum(Enum):
    """Video container format for music video downloads.

    Attributes:
        mp4: MP4 container (best compatibility).
        mkv: Matroska container (codec-agnostic, lossless remux of any track).
    """

    mp4 = "mp4"
    mkv = "mkv"


class CodecOptions(msgspec.Struct):
    """Codec preference options."""

    proprietary_codecs: bool
    spatial_codecs: bool


class ImageFileTypeEnum(Enum):
    jpg = "jpg"
    png = "png"
    webp = "webp"


class CoverCompressionEnum(Enum):
    low = "low"
    high = "high"


class CoverOptions(msgspec.Struct):
    """Cover image options."""

    file_type: ImageFileTypeEnum
    resolution: int
    compression: CoverCompressionEnum


class HabeleaOptions(msgspec.Struct):
    """Global Haberlea options passed to modules."""

    debug_mode: bool
    quality_tier: QualityEnum  # Here because of subscription checking
    default_cover_options: CoverOptions
    concurrent_downloads: int
    video_quality_tier: VideoQualityEnum = VideoQualityEnum.HIGH
    video_container: VideoContainerEnum = VideoContainerEnum.mkv


class ModuleController(msgspec.Struct):
    """Controller passed to modules for accessing shared resources."""

    module_settings: dict[str, Any]
    data_folder: str
    extensions: list[ExtensionInstance]
    temporary_settings_controller: TemporarySettingsController
    haberlea_options: HabeleaOptions
    get_current_timestamp: Callable[[], int]


class Tags(msgspec.Struct, kw_only=True):
    """Audio file metadata tags."""

    album_artist: str | None = None
    composer: str | None = None
    track_number: int | None = None
    total_tracks: int | None = None
    copyright: str | None = None
    isrc: str | None = None
    upc: str | None = None
    disc_number: int | None = None
    total_discs: int | None = None
    replay_gain: float | None = None
    replay_peak: float | None = None
    genres: list[str] | None = None
    release_date: str | None = None  # Format: YYYY-MM-DD
    description: str | None = None
    comment: str | None = None
    label: str | None = None
    extra_tags: dict[str, Any] = msgspec.field(default_factory=dict)


class CoverInfo(msgspec.Struct):
    """Cover image information."""

    url: str
    file_type: ImageFileTypeEnum


class LyricsInfo(msgspec.Struct, kw_only=True):
    """Lyrics information."""

    embedded: str | None = None
    synced: str | None = None


class CreditsInfo(msgspec.Struct):
    """Credits information for a track.

    Attributes:
        type: The type of credit (e.g., "composer", "producer").
        names: List of names associated with this credit type.
    """

    type: str
    names: list[str]


class AlbumInfo(msgspec.Struct, kw_only=True):
    """Album metadata information."""

    name: str
    artist: str
    tracks: list[str]
    release_year: int
    duration: int | None = None  # Duration in whole seconds
    explicit: bool = False
    artist_id: str | None = None
    quality: str | None = None
    booklet_url: str | None = None
    cover_url: str | None = None
    upc: str | None = None
    cover_type: ImageFileTypeEnum = ImageFileTypeEnum.jpg
    all_track_cover_jpg_url: str | None = None
    animated_cover_url: str | None = None
    description: str | None = None
    track_data: dict[str, Any] | None = None


class ArtistInfo(msgspec.Struct, kw_only=True):
    """Artist metadata information."""

    name: str
    albums: list[str] = msgspec.field(default_factory=list)
    album_data: dict[str, Any] | None = None
    tracks: list[str] = msgspec.field(default_factory=list)
    track_data: dict[str, Any] | None = None


class PlaylistInfo(msgspec.Struct, kw_only=True):
    """Playlist metadata information."""

    name: str
    creator: str
    tracks: list[str]
    release_year: int
    duration: int | None = None  # Duration in whole seconds
    explicit: bool = False
    creator_id: str | None = None
    cover_url: str | None = None
    cover_type: ImageFileTypeEnum = ImageFileTypeEnum.jpg
    animated_cover_url: str | None = None
    description: str | None = None
    track_data: dict[str, Any] | None = None


class MediaInfo(msgspec.Struct, kw_only=True):
    """Common metadata for any downloadable media item.

    Base struct shared by ``TrackInfo`` (audio) and ``VideoInfo`` (music
    videos). Holds the 8 fields that are semantically identical across
    both kinds; subclasses add kind-specific fields without redeclaring
    the common ones. Inheritance preserves ``isinstance`` and ``match``
    narrowing for downstream code.

    Attributes:
        name: Display title of the media item.
        artists: List of artist display names (first is the main artist).
        release_year: Release year (0 if unknown).
        cover_url: Cover image URL (mandatory; modules must always
            resolve at least a generic cover).
        artist_id: Main artist identifier.
        duration: Duration in whole seconds.
        explicit: Explicit flag (``None`` when unknown).
        download_data: Module-specific payload reused by the matching
            ``get_*_download`` call.
        error: Error message if metadata fetch failed.
    """

    name: str
    artists: list[str]
    release_year: int
    cover_url: str
    artist_id: str | None = None
    duration: int | None = None
    explicit: bool | None = None
    download_data: dict[str, Any] | None = None
    error: str | None = None


class TrackInfo(MediaInfo, kw_only=True):
    """Audio track metadata information."""

    album: str
    album_id: str
    tags: Tags
    codec: CodecEnum
    animated_cover_url: str | None = None
    description: str | None = None
    bit_depth: int = 16
    sample_rate: float = 44.1
    bitrate: int | None = None
    download_url: str | None = None
    cover_data: dict[str, Any] | None = None
    credits_data: dict[str, Any] | None = None
    lyrics_data: dict[str, Any] | None = None


class TrackDownloadInfo(msgspec.Struct, kw_only=True):
    """Download payload returned by ``get_track_download`` /
    ``get_video_download``.

    Modules return ``DownloadEnum.DIRECT`` after writing the file to the
    target path, or ``DownloadEnum.URL`` to let the framework fetch the
    file. Video downloads always leave ``different_codec`` as ``None``.

    Attributes:
        download_type: How the framework should treat this payload.
        file_url: Direct file URL (only for ``URL`` type).
        file_url_headers: Optional HTTP headers for ``URL`` type.
        temp_file_path: Optional temporary path for ``DIRECT`` type.
        different_codec: Audio-only — set when the module decides to
            deliver a different codec than requested (e.g. MQA → FLAC
            fall-back). Always ``None`` for video downloads.
    """

    download_type: DownloadEnum
    file_url: str | None = None
    file_url_headers: dict[str, str] | None = None
    temp_file_path: str | None = None
    different_codec: CodecEnum | None = None


class VideoInfo(MediaInfo, kw_only=True):
    """Music video metadata.

    Inherits all common fields from :class:`MediaInfo` (including the
    mandatory ``cover_url``) and adds the video-specific ``quality`` and
    ``container`` fields. The ``quality`` field reports the *actually
    selected* tier after variant matching by the module.

    Attributes:
        quality: Resolved video quality tier (closest match).
        container: Output container chosen for this download.
    """

    quality: VideoQualityEnum = VideoQualityEnum.HIGH
    container: VideoContainerEnum = VideoContainerEnum.mkv
